from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from app.agents.state import WorkflowState
from app.llm import get_llm, invoke_with_retry
from app.tools.patient_tools import get_or_create_patient_tool

COORDINATOR_SYSTEM_PROMPT = (
    "You are the Coordinator Agent for AgentCare, an administrative "
    "healthcare workflow assistant. Given the patient's free-text request, "
    "call get_or_create_patient with any contact details mentioned in the "
    "request (phone, preferred_language, emergency_contact — pass {} if "
    "none are mentioned). After the tool result comes back, reply with a "
    "one to three word administrative intent label for the request, for "
    "example: book_appointment, reschedule_appointment, "
    "cancel_appointment, submit_document, general_inquiry. If — and only "
    "if — the request genuinely contains two or more distinct "
    "administrative asks (e.g. \"cancel my appointment and book a new one\" "
    "or \"reschedule my visit and also cancel my other booking\"), reply "
    "instead with all the distinct intent labels separated by commas and "
    "nothing else, for example: cancel_appointment,book_appointment. Do not "
    "split a single request into multiple labels just because it has "
    "multiple sentences or extra detail — only when there are genuinely "
    "separate administrative actions being requested. Never diagnose or "
    "suggest treatment — only classify the administrative intent(s)."
)


coordinator_tools = [get_or_create_patient_tool]
coordinator_tools_node = ToolNode(coordinator_tools)


class CoordinatorState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    user_id: str
    patient_id: str | None
    intent: str | None


def coordinator_llm_node(state: CoordinatorState, config):
    model = get_llm().bind_tools(coordinator_tools)
    messages = [SystemMessage(COORDINATOR_SYSTEM_PROMPT), *state["messages"]]
    ai_message = invoke_with_retry(model, messages)
    return {"messages": [ai_message]}


def coordinator_capture_node(state: CoordinatorState, config):
    last = state["messages"][-1]
    if isinstance(last, ToolMessage) and last.name == "get_or_create_patient_tool":
        return {"patient_id": last.artifact["id"]}
    return {}


def coordinator_finalize_node(state: CoordinatorState, config):
    last = state["messages"][-1]
    return {"intent": last.content}


def route_after_coordinator_llm(state: CoordinatorState) -> Literal["coordinator_tools", "coordinator_finalize"]:
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None):
        return "coordinator_tools"
    return "coordinator_finalize"


def build_coordinator_subgraph():
    graph = StateGraph(CoordinatorState)
    graph.add_node("coordinator_llm", coordinator_llm_node)
    graph.add_node("coordinator_tools", coordinator_tools_node)
    graph.add_node("coordinator_capture", coordinator_capture_node)
    graph.add_node("coordinator_finalize", coordinator_finalize_node)
    graph.set_entry_point("coordinator_llm")
    graph.add_conditional_edges(
        "coordinator_llm",
        route_after_coordinator_llm,
        {"coordinator_tools": "coordinator_tools", "coordinator_finalize": "coordinator_finalize"},
    )
    graph.add_edge("coordinator_tools", "coordinator_capture")
    graph.add_edge("coordinator_capture", "coordinator_llm")
    graph.add_edge("coordinator_finalize", END)
    return graph.compile()


_coordinator_subgraph = build_coordinator_subgraph()


def coordinator_agent_node(state: WorkflowState, config) -> dict:
    """Parent-graph node (registered as "coordinator_agent" in app/graph.py).
    Invokes the private Coordinator subgraph and returns only the fields
    that belong in WorkflowState."""
    result = _coordinator_subgraph.invoke(
        {
            "messages": [HumanMessage(f"user_id: {state['user_id']}\nrequest: {state['request_text']}")],
            "user_id": state["user_id"],
            "patient_id": None,
            "intent": None,
        },
        config=config,
    )
    return {"patient_id": result.get("patient_id"), "intent": result.get("intent")}
