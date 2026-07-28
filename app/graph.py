from typing import Literal

from langgraph.graph import END, StateGraph

from app.agents.coordinator import coordinator_agent_node
from app.agents.document import document_agent_node
from app.agents.routing import routing_agent_node
from app.agents.safety import safety_agent_node
from app.agents.state import WorkflowState


def route_after_safety(state: WorkflowState) -> Literal["coordinator_agent", "__end__"]:
    if state.get("escalation"):
        return "__end__"
    return "coordinator_agent"


def route_after_document(state: WorkflowState) -> Literal["routing_agent", "needs_clarification"]:
    intent = (state.get("intent") or "").strip().lower()
    if intent == "book_appointment" or "book" in intent:
        return "routing_agent"
    return "needs_clarification"


def needs_clarification_node(state: WorkflowState, config) -> dict:
    return {"needs_clarification": True}


def build_graph():
    # routing_agent is the last automatic node in every booking path -
    # whether it escalates (no department match) or resolves a real
    # department_id, the graph stops there. Slot availability is a real DB
    # query (check_slot_availability), never a judgment call an LLM needs to
    # make, and the actual booking is always a specific, patient-clicked
    # slot - both handled deterministically by workflow_runner.run_workflow's
    # post-stream status resolution and the /select-slot route, not by an
    # agent auto-picking and auto-booking on the patient's behalf.
    graph = StateGraph(WorkflowState)

    graph.add_node("safety_agent", safety_agent_node)
    graph.add_node("coordinator_agent", coordinator_agent_node)
    graph.add_node("document_agent", document_agent_node)
    graph.add_node("needs_clarification", needs_clarification_node)
    graph.add_node("routing_agent", routing_agent_node)

    graph.set_entry_point("safety_agent")
    graph.add_conditional_edges(
        "safety_agent", route_after_safety, {"coordinator_agent": "coordinator_agent", "__end__": END}
    )
    graph.add_edge("coordinator_agent", "document_agent")
    graph.add_conditional_edges(
        "document_agent",
        route_after_document,
        {"routing_agent": "routing_agent", "needs_clarification": "needs_clarification"},
    )
    graph.add_edge("needs_clarification", END)
    graph.add_edge("routing_agent", END)

    return graph.compile()
