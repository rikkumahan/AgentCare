from typing import Literal

from langgraph.graph import END, StateGraph

from app.agents.appointment import appointment_agent_node
from app.agents.coordinator import coordinator_agent_node
from app.agents.document import document_agent_node
from app.agents.routing import routing_agent_node
from app.agents.safety import safety_agent_node
from app.agents.state import WorkflowState


def route_after_safety(state: WorkflowState) -> Literal["coordinator_agent", "__end__"]:
    if state.get("escalation"):
        return "__end__"
    return "coordinator_agent"


def route_after_routing(state: WorkflowState) -> Literal["appointment_agent", "__end__"]:
    if state.get("escalation"):
        return "__end__"
    return "appointment_agent"


def build_graph():
    graph = StateGraph(WorkflowState)

    graph.add_node("safety_agent", safety_agent_node)
    graph.add_node("coordinator_agent", coordinator_agent_node)
    graph.add_node("document_agent", document_agent_node)
    graph.add_node("routing_agent", routing_agent_node)
    graph.add_node("appointment_agent", appointment_agent_node)

    graph.set_entry_point("safety_agent")
    graph.add_conditional_edges(
        "safety_agent", route_after_safety, {"coordinator_agent": "coordinator_agent", "__end__": END}
    )
    graph.add_edge("coordinator_agent", "document_agent")
    graph.add_edge("document_agent", "routing_agent")
    graph.add_conditional_edges(
        "routing_agent", route_after_routing, {"appointment_agent": "appointment_agent", "__end__": END}
    )
    graph.add_edge("appointment_agent", END)

    return graph.compile()
