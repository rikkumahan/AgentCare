from langchain_core.messages import HumanMessage, ToolMessage

from app.agents.appointment import (
    appointment_agent_node,
    appointment_capture_node,
    appointment_llm_node,
    route_after_appointment_llm,
)
from app.models import Appointment, AppointmentSlot, SlotStatus
from tests.fakes import (
    FakeToolCallingModel,
    ai_message_text,
    ai_message_with_tool_call,
    make_appointment_slot,
    make_department,
    make_doctor,
    make_patient_profile,
    workflow_state,
)


def _appointment_state(**overrides):
    state = {
        "messages": [HumanMessage("request: book a cardiology appointment")],
        "department_id": "11111111-1111-1111-1111-111111111111",
        "patient_id": "22222222-2222-2222-2222-222222222222",
        "appointment_id": None,
    }
    state.update(overrides)
    return state


def test_appointment_llm_node_with_tool_call_routes_to_tools(monkeypatch):
    fake_model = FakeToolCallingModel(
        [ai_message_with_tool_call("check_slot_availability_tool", {"preferred_window": {}})]
    )
    monkeypatch.setattr("app.agents.appointment.get_llm", lambda: fake_model)

    state = _appointment_state()
    update = appointment_llm_node(state, config={"configurable": {}})
    state["messages"] = state["messages"] + update["messages"]

    assert route_after_appointment_llm(state) == "appointment_tools"


def test_appointment_llm_node_with_no_tool_call_routes_to_end(monkeypatch):
    fake_model = FakeToolCallingModel([ai_message_text("Your appointment is confirmed.")])
    monkeypatch.setattr("app.agents.appointment.get_llm", lambda: fake_model)

    state = _appointment_state()
    update = appointment_llm_node(state, config={"configurable": {}})
    state["messages"] = state["messages"] + update["messages"]

    assert route_after_appointment_llm(state) == "__end__"


def test_appointment_capture_node_sets_appointment_id_on_success():
    tool_message = ToolMessage(
        content="Appointment book result: confirmed",
        artifact={"id": "a1", "status": "confirmed"},
        tool_call_id="call_1",
        name="book_or_modify_appointment_tool",
    )
    state = _appointment_state(messages=[tool_message])

    update = appointment_capture_node(state, config={"configurable": {}})

    assert update == {"appointment_id": "a1"}


def test_appointment_capture_node_ignores_error_result():
    tool_message = ToolMessage(
        content="Appointment book result: error",
        artifact={"id": None, "status": "error", "error": "Slot is no longer open"},
        tool_call_id="call_1",
        name="book_or_modify_appointment_tool",
    )
    state = _appointment_state(messages=[tool_message])

    update = appointment_capture_node(state, config={"configurable": {}})

    assert update == {}


def test_appointment_capture_node_ignores_check_slot_availability_message():
    tool_message = ToolMessage(
        content="Found 1 open slot(s)",
        artifact=[{"slot_id": "s1"}],
        tool_call_id="call_1",
        name="check_slot_availability_tool",
    )
    state = _appointment_state(messages=[tool_message])

    update = appointment_capture_node(state, config={"configurable": {}})

    assert update == {}


def test_appointment_agent_node_books_appointment_end_to_end(monkeypatch, db_session):
    department = make_department(db_session)
    doctor = make_doctor(db_session, department=department)
    slot = make_appointment_slot(db_session, doctor=doctor)
    profile = make_patient_profile(db_session)

    fake_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("check_slot_availability_tool", {"preferred_window": {}}),
            ai_message_with_tool_call(
                "book_or_modify_appointment_tool",
                {"slot_id": str(slot.id), "action": "book", "existing_appointment_id": None},
            ),
            ai_message_text("Your appointment is confirmed."),
        ]
    )
    monkeypatch.setattr("app.agents.appointment.get_llm", lambda: fake_model)

    state = workflow_state(
        department_id=str(department.id),
        patient_id=str(profile.id),
        request_text="book a cardiology appointment",
    )
    update = appointment_agent_node(state, config={"configurable": {"db": db_session}})

    assert update["appointment_id"] is not None
    appointment = db_session.query(Appointment).filter(Appointment.id == update["appointment_id"]).one()
    assert str(appointment.patient_id) == str(profile.id)
    booked_slot = db_session.query(AppointmentSlot).filter(AppointmentSlot.id == slot.id).one()
    assert booked_slot.status == SlotStatus.booked
