import uuid

from app.models import Appointment, AppointmentSlot, AuditEvent, Escalation, SlotStatus, WorkflowStatus
from app.workflow_runner import run_workflow
from tests.fakes import (
    FakeToolCallingModel,
    ai_message_text,
    ai_message_with_tool_call,
    make_appointment_slot,
    make_department,
    make_doctor,
    make_patient_profile,
    make_user,
)


def test_emergency_request_ends_needs_review_with_escalation_row(monkeypatch, db_session):
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)

    safety_model = FakeToolCallingModel(
        [ai_message_with_tool_call("create_escalation_tool", {"reason": "patient describes chest pain and shortness of breath"})]
    )
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: safety_model)

    workflow_run = run_workflow(
        db_session,
        patient_id=str(profile.id),
        user_id=str(user.id),
        request_text="I have severe chest pain and can't breathe, what's wrong with me?",
    )

    assert workflow_run.status == WorkflowStatus.needs_review
    assert workflow_run.current_step == "safety_agent"
    assert workflow_run.state["status"] == "needs_review"

    escalation = db_session.query(Escalation).filter(Escalation.workflow_run_id == workflow_run.id).one()
    assert "chest pain" in escalation.reason

    audit_actions = {
        e.action for e in db_session.query(AuditEvent).filter(AuditEvent.entity_type == "Escalation").all()
    }
    assert "create_escalation" in audit_actions


def test_administrative_request_reaches_routing_boundary_with_intent_set(monkeypatch, db_session):
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)

    safety_model = FakeToolCallingModel([ai_message_text("SAFE")])
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: safety_model)

    coordinator_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("get_or_create_patient_tool", {"profile_fields": {}}),
            ai_message_text("book_appointment"),
        ]
    )
    monkeypatch.setattr("app.agents.coordinator.get_llm", lambda: coordinator_model)

    routing_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("lookup_departments_tool", {"query_hint": "general"}),
            ai_message_text("UNMATCHED"),
        ]
    )
    monkeypatch.setattr("app.agents.routing.get_llm", lambda: routing_model)

    workflow_run = run_workflow(
        db_session,
        patient_id=str(profile.id),
        user_id=str(user.id),
        request_text="I'd like to book a cardiology appointment next week",
    )

    assert workflow_run.status == WorkflowStatus.needs_review
    assert workflow_run.current_step == "routing_agent"
    assert workflow_run.state["intent"] == "book_appointment"
    assert workflow_run.state["patient_id"] is not None


def test_full_workflow_books_appointment_end_to_end(monkeypatch, db_session):
    dept_name = f"Cardiology {uuid.uuid4().hex[:8]}"
    department = make_department(db_session, name=dept_name)
    doctor = make_doctor(db_session, department=department)
    slot = make_appointment_slot(db_session, doctor=doctor)
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)

    safety_model = FakeToolCallingModel([ai_message_text("SAFE")])
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: safety_model)

    coordinator_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("get_or_create_patient_tool", {"profile_fields": {}}),
            ai_message_text("book_appointment"),
        ]
    )
    monkeypatch.setattr("app.agents.coordinator.get_llm", lambda: coordinator_model)

    routing_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("lookup_departments_tool", {"query_hint": "cardiology"}),
            ai_message_text(dept_name),
        ]
    )
    monkeypatch.setattr("app.agents.routing.get_llm", lambda: routing_model)

    appointment_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("check_slot_availability_tool", {"preferred_window": {}}),
            ai_message_with_tool_call(
                "book_or_modify_appointment_tool",
                {"slot_id": str(slot.id), "action": "book", "existing_appointment_id": None},
            ),
            ai_message_text("Your appointment is confirmed."),
        ]
    )
    monkeypatch.setattr("app.agents.appointment.get_llm", lambda: appointment_model)

    workflow_run = run_workflow(
        db_session,
        patient_id=str(profile.id),
        user_id=str(user.id),
        request_text="I'd like to book a cardiology appointment next week",
    )

    assert workflow_run.status == WorkflowStatus.running
    assert workflow_run.current_step == "document_agent"
    assert workflow_run.state["department_id"] == str(department.id)
    assert workflow_run.state["appointment_id"] is not None

    appointment = db_session.query(Appointment).filter(Appointment.id == workflow_run.state["appointment_id"]).one()
    assert appointment.status.value == "confirmed"
    booked_slot = db_session.query(AppointmentSlot).filter(AppointmentSlot.id == slot.id).one()
    assert booked_slot.status == SlotStatus.booked


def test_unroutable_request_ends_needs_review_without_booking(monkeypatch, db_session):
    make_department(db_session, name=f"Cardiology {uuid.uuid4().hex[:8]}")
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)

    safety_model = FakeToolCallingModel([ai_message_text("SAFE")])
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: safety_model)

    coordinator_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("get_or_create_patient_tool", {"profile_fields": {}}),
            ai_message_text("book_appointment"),
        ]
    )
    monkeypatch.setattr("app.agents.coordinator.get_llm", lambda: coordinator_model)

    routing_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("lookup_departments_tool", {"query_hint": "dermatology"}),
            ai_message_text("UNMATCHED"),
        ]
    )
    monkeypatch.setattr("app.agents.routing.get_llm", lambda: routing_model)

    workflow_run = run_workflow(
        db_session,
        patient_id=str(profile.id),
        user_id=str(user.id),
        request_text="I need to see a dermatologist about a rash",
    )

    assert workflow_run.status == WorkflowStatus.needs_review
    assert workflow_run.current_step == "routing_agent"
    assert db_session.query(Appointment).filter(Appointment.patient_id == profile.id).count() == 0

    escalation = db_session.query(Escalation).filter(Escalation.workflow_run_id == workflow_run.id).one()
    assert "dermatologist" in escalation.reason


def test_unhandled_node_exception_marks_workflow_failed(monkeypatch, db_session):
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)

    def _boom():
        raise RuntimeError("groq is down")

    monkeypatch.setattr("app.agents.safety.get_llm", _boom)

    workflow_run = run_workflow(
        db_session,
        patient_id=str(profile.id),
        user_id=str(user.id),
        request_text="book an appointment",
    )

    assert workflow_run.status == WorkflowStatus.failed
    assert "groq is down" in workflow_run.state["error"]
