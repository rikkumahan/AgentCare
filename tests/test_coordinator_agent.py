from langchain_core.messages import HumanMessage, ToolMessage

from app.agents.coordinator import (
    coordinator_agent_node,
    coordinator_capture_node,
    coordinator_finalize_node,
    coordinator_llm_node,
    route_after_coordinator_llm,
)
from app.models import PatientProfile
from tests.fakes import FakeToolCallingModel, ai_message_text, ai_message_with_tool_call, make_user, workflow_state


def _coordinator_state(**overrides):
    state = {
        "messages": [HumanMessage("user_id: u1\nrequest: book a cardiology appointment")],
        "user_id": "u1",
        "patient_id": None,
        "intent": None,
    }
    state.update(overrides)
    return state


def test_coordinator_llm_node_with_tool_call_routes_to_tools(monkeypatch):
    fake_model = FakeToolCallingModel(
        [ai_message_with_tool_call("get_or_create_patient_tool", {"profile_fields": {}})]
    )
    monkeypatch.setattr("app.agents.coordinator.get_llm", lambda: fake_model)

    state = _coordinator_state()
    update = coordinator_llm_node(state, config={"configurable": {}})
    state["messages"] = state["messages"] + update["messages"]

    assert route_after_coordinator_llm(state) == "coordinator_tools"


def test_coordinator_llm_node_with_no_tool_call_routes_to_finalize(monkeypatch):
    fake_model = FakeToolCallingModel([ai_message_text("book_appointment")])
    monkeypatch.setattr("app.agents.coordinator.get_llm", lambda: fake_model)

    state = _coordinator_state()
    update = coordinator_llm_node(state, config={"configurable": {}})
    state["messages"] = state["messages"] + update["messages"]

    assert route_after_coordinator_llm(state) == "coordinator_finalize"


def test_coordinator_capture_node_sets_patient_id_from_tool_message():
    tool_message = ToolMessage(
        content="Patient profile resolved: p1",
        artifact={"id": "p1", "user_id": "u1", "phone": None},
        tool_call_id="call_1",
        name="get_or_create_patient_tool",
    )
    state = _coordinator_state(messages=[tool_message])

    update = coordinator_capture_node(state, config={"configurable": {}})

    assert update == {"patient_id": "p1"}


def test_coordinator_finalize_node_sets_intent_from_final_ai_message():
    state = _coordinator_state(messages=[ai_message_text("book_appointment")])

    update = coordinator_finalize_node(state, config={"configurable": {}})

    assert update == {"intent": "book_appointment"}


def test_coordinator_agent_node_returns_patient_id_and_intent(monkeypatch, db_session):
    user = make_user(db_session)

    fake_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("get_or_create_patient_tool", {"profile_fields": {}}),
            ai_message_text("book_appointment"),
        ]
    )
    monkeypatch.setattr("app.agents.coordinator.get_llm", lambda: fake_model)

    state = workflow_state(patient_id=None, user_id=str(user.id))

    update = coordinator_agent_node(state, config={"configurable": {"db": db_session}})

    assert update["intent"] == "book_appointment"
    assert update["patient_id"] is not None

    profile = db_session.query(PatientProfile).filter(PatientProfile.user_id == user.id).one()
    assert str(profile.id) == update["patient_id"]
