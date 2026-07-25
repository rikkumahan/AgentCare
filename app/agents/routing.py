from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from sqlalchemy import func

from app.agents.state import WorkflowState
from app.llm import get_llm, invoke_with_retry
from app.models import Department
from app.tools.department_tools import lookup_departments_tool
from app.tools.escalation_tools import create_escalation

ROUTING_SYSTEM_PROMPT = (
    "You are the Department Routing Agent for AgentCare, an administrative "
    "healthcare workflow assistant. Call lookup_departments with a short "
    "hint describing what the patient's request is about, then look at the "
    "returned list of active departments. If exactly one department is a "
    "clear administrative fit, reply with ONLY that department's exact "
    "name, nothing else. If no department in the list is a reasonable "
    "administrative fit, reply with the single word UNMATCHED. Never "
    "reason about medical severity, urgency, or diagnosis — only match the "
    "request to an administrative department."
)

routing_tools = [lookup_departments_tool]
routing_tools_node = ToolNode(routing_tools)


class RoutingState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    department_name: str | None


def routing_llm_node(state: RoutingState, config):
    model = get_llm().bind_tools(routing_tools)
    messages = [SystemMessage(ROUTING_SYSTEM_PROMPT), *state["messages"]]
    ai_message = invoke_with_retry(model, messages)
    return {"messages": [ai_message]}


def routing_finalize_node(state: RoutingState, config):
    last = state["messages"][-1]
    text = last.content.strip()
    if text.upper() == "UNMATCHED":
        return {"department_name": None}
    return {"department_name": text}


def route_after_routing_llm(state: RoutingState) -> Literal["routing_tools", "routing_finalize"]:
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None):
        return "routing_tools"
    return "routing_finalize"


def build_routing_subgraph():
    graph = StateGraph(RoutingState)
    graph.add_node("routing_llm", routing_llm_node)
    graph.add_node("routing_tools", routing_tools_node)
    graph.add_node("routing_finalize", routing_finalize_node)
    graph.set_entry_point("routing_llm")
    graph.add_conditional_edges(
        "routing_llm",
        route_after_routing_llm,
        {"routing_tools": "routing_tools", "routing_finalize": "routing_finalize"},
    )
    graph.add_edge("routing_tools", "routing_llm")
    graph.add_edge("routing_finalize", END)
    return graph.compile()


_routing_subgraph = build_routing_subgraph()


def routing_agent_node(state: WorkflowState, config) -> dict:
    """Parent-graph node (registered as "routing_agent" in app/graph.py).
    Invokes the private Routing subgraph, then resolves the department name
    it returned to a real department_id. lookup_departments is read-only,
    so there's no write-tool artifact to capture an id from — the finalize
    step reads the LLM's own final text, and this wrapper does a
    case-insensitive DB lookup rather than trusting an LLM-transcribed
    UUID. If no department resolves, escalates via the same audited
    create_escalation function Safety's tool uses (not a second LLM call —
    "no confident match" is a deterministic outcome, not a judgment call)."""
    result = _routing_subgraph.invoke(
        {
            "messages": [HumanMessage(f"request: {state['request_text']}")],
            "department_name": None,
        },
        config=config,
    )
    db = config["configurable"]["db"]
    department_name = result.get("department_name")

    department_id = None
    if department_name:
        department = (
            db.query(Department)
            .filter(func.lower(Department.name) == department_name.strip().lower())
            .filter(Department.active.is_(True))
            .first()
        )
        if department is not None:
            department_id = str(department.id)

    if department_id is None:
        escalation = create_escalation(
            db,
            state["workflow_run_id"],
            f"Could not confidently match request to an active department: {state['request_text']!r}",
        )
        return {"department_id": None, "escalation": escalation}

    return {"department_id": department_id}
