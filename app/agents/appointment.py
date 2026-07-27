from datetime import date
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from app.agents.state import WorkflowState
from app.llm import get_llm, invoke_with_retry
from app.tools.appointment_tools import book_or_modify_appointment_tool, check_slot_availability_tool

APPOINTMENT_SYSTEM_PROMPT = (
    "You are the Appointment Agent for AgentCare, an administrative "
    "healthcare workflow assistant. Call check_slot_availability to find "
    "open slots in the patient's department. Leave preferred_window as {} "
    "unless the patient named a SPECIFIC calendar date (e.g. 'March 5th') "
    "— the tool already defaults to a sensible upcoming window, and you do "
    "not need to compute start_date/end_date yourself for vague phrases "
    "like 'next week' or 'soon'. If you do set start_date/end_date, use "
    "the current date given in the request message to compute them "
    "correctly — never guess a year.\n\n"
    "If check_slot_availability returns zero slots, do NOT call "
    "book_or_modify_appointment with a made-up slot_id. Instead call "
    "check_slot_availability exactly once more with preferred_window={} to "
    "widen the search. If that also returns zero slots, reply that no "
    "slots are currently available and stop — do not call any more tools.\n\n"
    "Only ever pass a slot_id you actually saw in a check_slot_availability "
    "result this conversation — never invent one. Pick a slot that "
    "reasonably matches the patient's request, then call "
    "book_or_modify_appointment with action='book' and "
    "existing_appointment_id=null to reserve it. If booking returns status "
    "'error', pick a different real slot from the list and try again. Once "
    "booked, reply with a short confirmation sentence and do not call any "
    "more tools. Never diagnose or suggest treatment — only handle "
    "scheduling."
)

appointment_tools = [check_slot_availability_tool, book_or_modify_appointment_tool]
appointment_tools_node = ToolNode(appointment_tools)


class AppointmentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    department_id: str
    patient_id: str
    appointment_id: str | None


def appointment_llm_node(state: AppointmentState, config):
    model = get_llm().bind_tools(appointment_tools)
    messages = [SystemMessage(APPOINTMENT_SYSTEM_PROMPT), *state["messages"]]
    ai_message = invoke_with_retry(model, messages)
    return {"messages": [ai_message]}


def appointment_capture_node(state: AppointmentState, config):
    last = state["messages"][-1]
    if isinstance(last, ToolMessage) and last.name == "book_or_modify_appointment_tool":
        if last.artifact and last.artifact.get("status") != "error":
            return {"appointment_id": last.artifact["id"]}
    return {}


def route_after_appointment_llm(state: AppointmentState) -> Literal["appointment_tools", "__end__"]:
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None):
        return "appointment_tools"
    return "__end__"


def build_appointment_subgraph():
    graph = StateGraph(AppointmentState)
    graph.add_node("appointment_llm", appointment_llm_node)
    graph.add_node("appointment_tools", appointment_tools_node)
    graph.add_node("appointment_capture", appointment_capture_node)
    graph.set_entry_point("appointment_llm")
    graph.add_conditional_edges(
        "appointment_llm",
        route_after_appointment_llm,
        {"appointment_tools": "appointment_tools", "__end__": END},
    )
    graph.add_edge("appointment_tools", "appointment_capture")
    graph.add_edge("appointment_capture", "appointment_llm")
    return graph.compile()


_appointment_subgraph = build_appointment_subgraph()


def appointment_agent_node(state: WorkflowState, config) -> dict:
    """Parent-graph node (registered as "appointment_agent" in
    app/graph.py). Invokes the private Appointment subgraph and returns
    only the field that belongs in WorkflowState."""
    result = _appointment_subgraph.invoke(
        {
            "messages": [
                HumanMessage(f"current date: {date.today().isoformat()}\nrequest: {state['request_text']}")
            ],
            "department_id": state["department_id"],
            "patient_id": state["patient_id"],
            "appointment_id": None,
        },
        config=config,
    )
    return {"appointment_id": result.get("appointment_id")}
