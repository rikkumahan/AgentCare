from langchain_core.messages import HumanMessage, ToolMessage

from app.agents.safety import (
    route_after_safety_llm,
    safety_agent_node,
    safety_capture_node,
    safety_llm_node,
)
from app.models import Escalation
from tests.fakes import (
    FakeToolCallingModel,
    ai_message_text,
    ai_message_with_tool_call,
    make_workflow_run,
    workflow_state,
)


def _safety_state(**overrides):
    state = {
        "messages": [HumanMessage("request: I need to book a cardiology appointment")],
        "workflow_run_id": "11111111-1111-1111-1111-111111111111",
        "escalation": None,
    }
    state.update(overrides)
    return state


def test_safety_llm_node_with_no_tool_call_routes_to_end(monkeypatch):
    fake_model = FakeToolCallingModel([ai_message_text("SAFE")])
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: fake_model)

    state = _safety_state()
    update = safety_llm_node(state, config={"configurable": {}})
    state["messages"] = state["messages"] + update["messages"]

    assert route_after_safety_llm(state) == "__end__"


def test_safety_llm_node_with_tool_call_routes_to_tools(monkeypatch):
    fake_model = FakeToolCallingModel(
        [ai_message_with_tool_call("create_escalation_tool", {"reason": "describes an emergency"})]
    )
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: fake_model)

    state = _safety_state()
    update = safety_llm_node(state, config={"configurable": {}})
    state["messages"] = state["messages"] + update["messages"]

    assert route_after_safety_llm(state) == "safety_tools"


def test_safety_capture_node_sets_escalation_from_tool_message():
    tool_message = ToolMessage(
        content="Escalated: describes an emergency",
        artifact={"id": "e1", "reason": "describes an emergency", "status": "open"},
        tool_call_id="call_1",
        name="create_escalation_tool",
    )
    state = _safety_state(messages=[tool_message])

    update = safety_capture_node(state, config={"configurable": {}})

    assert update == {"escalation": {"id": "e1", "reason": "describes an emergency", "status": "open"}}


def test_safety_capture_node_fails_closed_on_malformed_tool_call():
    tool_message = ToolMessage(
        content="Error: 1 validation error for create_escalation_tool\nreason\n  Field required",
        artifact=None,
        tool_call_id="call_1",
        name="create_escalation_tool",
        status="error",
    )
    state = _safety_state(messages=[tool_message])

    update = safety_capture_node(state, config={"configurable": {}})

    assert update["escalation"] is not None
    assert update["escalation"]["status"] == "open"


def test_safety_agent_node_returns_no_escalation_for_safe_request(monkeypatch):
    fake_model = FakeToolCallingModel([ai_message_text("SAFE")])
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: fake_model)

    update = safety_agent_node(workflow_state(), config={"configurable": {"db": None}})

    assert update == {"escalation": None}


def test_safety_agent_node_returns_escalation_and_persists_it(monkeypatch, db_session):
    workflow_run = make_workflow_run(db_session)

    fake_model = FakeToolCallingModel(
        [ai_message_with_tool_call("create_escalation_tool", {"reason": "describes chest pain, an emergency"})]
    )
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: fake_model)

    state = workflow_state(
        workflow_run_id=str(workflow_run.id),
        patient_id=str(workflow_run.patient_id),
        request_text="I have chest pain, what's wrong with me?",
    )

    update = safety_agent_node(state, config={"configurable": {"db": db_session}})

    assert update["escalation"]["reason"] == "describes chest pain, an emergency"

    escalation = db_session.query(Escalation).filter(Escalation.workflow_run_id == workflow_run.id).one()
    assert escalation.reason == "describes chest pain, an emergency"
