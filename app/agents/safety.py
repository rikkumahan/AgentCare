from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from app.agents.state import WorkflowState
from app.llm import get_llm, invoke_with_retry
from app.tools.escalation_tools import create_escalation_tool

SAFETY_SYSTEM_PROMPT = (
    "You are the Safety & Escalation Agent for AgentCare, an administrative "
    "healthcare workflow assistant. You never diagnose, prescribe, or advise "
    "on treatment. Call create_escalation whenever the patient's request "
    "describes a medical emergency, asks for a diagnosis, or asks to "
    "prescribe or change a medication dosage. Purely administrative "
    "requests (booking, rescheduling, cancelling an appointment, submitting "
    "a document) are always safe and must not be escalated. If the request "
    "is safe, reply with the single word SAFE and do not call any tool."
)

safety_tools = [create_escalation_tool]
safety_tools_node = ToolNode(safety_tools)


class SafetyState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    workflow_run_id: str
    escalation: dict | None


def safety_llm_node(state: SafetyState, config):
    model = get_llm().bind_tools(safety_tools)
    messages = [SystemMessage(SAFETY_SYSTEM_PROMPT), *state["messages"]]
    ai_message = invoke_with_retry(model, messages)
    return {"messages": [ai_message]}


def safety_capture_node(state: SafetyState, config):
    last = state["messages"][-1]
    if isinstance(last, ToolMessage) and last.name == "create_escalation_tool":
        return {"escalation": last.artifact}
    return {}


def route_after_safety_llm(state: SafetyState) -> Literal["safety_tools", "__end__"]:
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None):
        return "safety_tools"
    return "__end__"


def build_safety_subgraph():
    graph = StateGraph(SafetyState)
    graph.add_node("safety_llm", safety_llm_node)
    graph.add_node("safety_tools", safety_tools_node)
    graph.add_node("safety_capture", safety_capture_node)
    graph.set_entry_point("safety_llm")
    graph.add_conditional_edges(
        "safety_llm", route_after_safety_llm, {"safety_tools": "safety_tools", "__end__": END}
    )
    graph.add_edge("safety_tools", "safety_capture")
    graph.add_edge("safety_capture", END)
    return graph.compile()


_safety_subgraph = build_safety_subgraph()


def safety_agent_node(state: WorkflowState, config) -> dict:
    """Parent-graph node (registered as "safety_agent" in app/graph.py).
    Invokes the private Safety subgraph and returns only the field that
    belongs in WorkflowState — the subgraph's own messages never leave it."""
    result = _safety_subgraph.invoke(
        {
            "messages": [HumanMessage(f"request: {state['request_text']}")],
            "workflow_run_id": state["workflow_run_id"],
            "escalation": None,
        },
        config=config,
    )
    return {"escalation": result.get("escalation")}
