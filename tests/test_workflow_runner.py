from app.models import AuditEvent, Escalation, WorkflowStatus
from app.workflow_runner import run_workflow
from tests.fakes import (
    FakeToolCallingModel,
    ai_message_text,
    ai_message_with_tool_call,
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

    workflow_run = run_workflow(
        db_session,
        patient_id=str(profile.id),
        user_id=str(user.id),
        request_text="I'd like to book a cardiology appointment next week",
    )

    assert workflow_run.status == WorkflowStatus.running
    assert workflow_run.current_step == "routing_agent"
    assert workflow_run.state["status"] == "running"
    assert workflow_run.state["intent"] == "book_appointment"
    assert workflow_run.state["patient_id"] is not None
    assert workflow_run.state["escalation"] is None
    assert "messages" not in workflow_run.state

    escalation_count = (
        db_session.query(Escalation).filter(Escalation.workflow_run_id == workflow_run.id).count()
    )
    assert escalation_count == 0


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
