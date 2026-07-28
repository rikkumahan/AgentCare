from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from app.agents.state import WorkflowState
from app.llm import get_llm, invoke_with_retry
from app.models import Department
from app.tools.department_tools import lookup_departments_tool
from app.tools.escalation_tools import create_escalation

ROUTING_SYSTEM_PROMPT = (
    "You are the Department Routing Agent for AgentCare, an administrative "
    "healthcare workflow assistant. Call lookup_departments with a short "
    "hint describing what the patient's request is about, then look at the "
    "returned list of active departments. Reply with exactly one of these "
    "three things, nothing else:\n"
    "1. If the patient described what kind of care/specialty they need and "
    "exactly one department is a clear administrative fit, reply with ONLY "
    "that department's exact name.\n"
    "2. If the patient described what kind of care they need but no "
    "department in the list is a reasonable fit for it, reply with the "
    "single word UNMATCHED.\n"
    "3. If the patient's request never actually said what kind of care or "
    "specialty they need at all (e.g. just \"I want to book an "
    "appointment\", with no symptom, condition, or department named), "
    "reply with the single word NEEDS_MORE_INFO — do NOT guess a "
    "department just because one sounds like a general catch-all; a "
    "genuinely unspecified request is not the same as a request you "
    "couldn't match.\n"
    "Never reason about medical severity, urgency, or diagnosis — only "
    "match the request to an administrative department."
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
    if text.upper() in ("UNMATCHED", "NEEDS_MORE_INFO"):
        return {"department_name": text.upper()}
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

    if department_name == "NEEDS_MORE_INFO":
        # The patient never said what kind of care/specialty they need at
        # all (e.g. "I'm here to book an appointment") - reuse the same
        # "what's this appointment for" flow used from the clarification
        # popup, showing real department buttons, rather than letting the
        # model guess one (confirmed against the real Groq API: an
        # unspecified request silently defaulted to "General Medicine"
        # every time, since it reads as a safe catch-all guess - not a
        # genuine match, just the least-wrong-sounding option).
        return {"department_id": None, "needs_appointment_reason": True}

    department_id = None
    if department_name and department_name != "UNMATCHED":
        normalized_reply = department_name.strip().lower()
        candidates = db.query(Department).filter(Department.active.is_(True)).all()
        # Prefer an exact match; fall back to substring containment either
        # direction. A real LLM sometimes echoes "Cardiology Department"
        # instead of the bare name "Cardiology" despite being told to reply
        # with only the exact name - confirmed against the real Groq API,
        # not a hypothetical. Still never trusts an LLM-transcribed id.
        for candidate in candidates:
            if candidate.name.strip().lower() == normalized_reply:
                department_id = str(candidate.id)
                break
        else:
            for candidate in candidates:
                candidate_name = candidate.name.strip().lower()
                if candidate_name in normalized_reply or normalized_reply in candidate_name:
                    department_id = str(candidate.id)
                    break

    if department_id is None:
        escalation = create_escalation(
            db,
            state["workflow_run_id"],
            f"Could not confidently match request to an active department: {state['request_text']!r}",
        )
        return {"department_id": None, "escalation": escalation}

    return {"department_id": department_id}
