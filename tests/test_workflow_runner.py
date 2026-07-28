import uuid

from app.models import (
    Appointment,
    AppointmentSlot,
    AppointmentStatus,
    AuditEvent,
    Escalation,
    SlotStatus,
    WorkflowRun,
    WorkflowStatus,
)

from app.workflow_runner import (
    continue_as_appointment_action,
    continue_as_booking,
    continue_as_booking_with_department,
    continue_as_staff_escalation,
    continue_with_selected_slot,
    run_workflow,
)
from tests.fakes import (
    FakeToolCallingModel,
    ai_message_text,
    ai_message_with_tool_call,
    make_appointment,
    make_appointment_slot,
    make_department,
    make_doctor,
    make_patient_profile,
    make_user,
    make_workflow_run,
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

    def _fail_if_appointment_llm_called():
        raise AssertionError("Appointment agent's LLM must not run automatically - slot selection is the patient's choice now")

    monkeypatch.setattr("app.agents.appointment.get_llm", _fail_if_appointment_llm_called)

    workflow_run = run_workflow(
        db_session,
        patient_id=str(profile.id),
        user_id=str(user.id),
        request_text="I'd like to book a cardiology appointment next week",
    )

    assert workflow_run.status == WorkflowStatus.needs_slot_selection
    assert workflow_run.state.get("needs_clarification") is False
    assert workflow_run.state["department_id"] == str(department.id)
    assert workflow_run.state["appointment_id"] is None

    workflow_run = continue_with_selected_slot(db_session, workflow_run, str(slot.id))

    assert workflow_run.status == WorkflowStatus.completed
    assert workflow_run.current_step == "appointment_agent"
    assert workflow_run.state["appointment_id"] is not None

    appointment = db_session.query(Appointment).filter(Appointment.id == workflow_run.state["appointment_id"]).one()
    assert appointment.status.value == "confirmed"
    booked_slot = db_session.query(AppointmentSlot).filter(AppointmentSlot.id == slot.id).one()
    assert booked_slot.status == SlotStatus.booked


def test_non_booking_intent_ends_needs_clarification(monkeypatch, db_session):
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)

    safety_model = FakeToolCallingModel([ai_message_text("SAFE")])
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: safety_model)

    coordinator_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("get_or_create_patient_tool", {"profile_fields": {}}),
            ai_message_text("general_inquiry"),
        ]
    )
    monkeypatch.setattr("app.agents.coordinator.get_llm", lambda: coordinator_model)

    workflow_run = run_workflow(
        db_session,
        patient_id=str(profile.id),
        user_id=str(user.id),
        request_text="what are your visiting hours?",
    )

    assert workflow_run.status == WorkflowStatus.needs_clarification
    assert workflow_run.current_step == "needs_clarification"
    assert workflow_run.state["needs_clarification"] is True
    assert workflow_run.state["intent"] == "general_inquiry"
    assert workflow_run.state["appointment_id"] is None


def test_confident_booking_intent_with_no_specialty_asks_instead_of_guessing(monkeypatch, db_session):
    # Regression, found via live manual testing: "I'm here to book an
    # appointment" has a confident book_appointment intent (unlike the
    # needs_clarification cases above) but never says what kind of care is
    # needed - this must land at needs_appointment_reason, not silently
    # pick a department (it was defaulting to General Medicine every time).
    make_department(db_session, name=f"General Medicine {uuid.uuid4().hex[:8]}")
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
            ai_message_with_tool_call("lookup_departments_tool", {"query_hint": "appointment"}),
            ai_message_text("NEEDS_MORE_INFO"),
        ]
    )
    monkeypatch.setattr("app.agents.routing.get_llm", lambda: routing_model)

    workflow_run = run_workflow(
        db_session,
        patient_id=str(profile.id),
        user_id=str(user.id),
        request_text="I'm here to book an appointment",
    )

    assert workflow_run.status == WorkflowStatus.needs_appointment_reason
    assert workflow_run.state["department_id"] is None
    assert workflow_run.state["appointment_id"] is None


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


def _needs_clarification_run(db_session, profile, user, request_text="book a cardiology appointment"):
    workflow_run = make_workflow_run(db_session, profile=profile)
    workflow_run.status = WorkflowStatus.needs_clarification
    workflow_run.current_step = "needs_clarification"
    workflow_run.state = {
        "workflow_run_id": str(workflow_run.id),
        "patient_id": str(profile.id),
        "user_id": str(user.id),
        "request_text": request_text,
        "uploaded_files": [],
        "intent": "general_inquiry",
        "department_id": None,
        "appointment_id": None,
        "document_ids": [],
        "reminder_ids": [],
        "escalation": None,
        "status": "needs_clarification",
        "needs_clarification": True,
    }
    db_session.commit()
    return workflow_run


def test_continue_as_booking_lands_at_needs_slot_selection_no_appointment_llm(monkeypatch, db_session):
    department = make_department(db_session, name=f"Cardiology {uuid.uuid4().hex[:8]}")
    doctor = make_doctor(db_session, department=department)
    slot = make_appointment_slot(db_session, doctor=doctor)
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)
    workflow_run = _needs_clarification_run(db_session, profile, user)

    routing_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("lookup_departments_tool", {"query_hint": "cardiology"}),
            ai_message_text(department.name),
        ]
    )
    monkeypatch.setattr("app.agents.routing.get_llm", lambda: routing_model)

    def _fail_if_called():
        raise AssertionError("Appointment agent's LLM must not be called - slot selection is the patient's choice now")

    monkeypatch.setattr("app.agents.appointment.get_llm", _fail_if_called)

    result = continue_as_booking(db_session, workflow_run)

    assert result.status == WorkflowStatus.needs_slot_selection
    assert result.state["department_id"] == str(department.id)
    assert result.state["appointment_id"] is None


def test_continue_as_booking_lands_at_needs_review_when_routing_escalates(monkeypatch, db_session):
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)
    workflow_run = _needs_clarification_run(
        db_session, profile, user, request_text="I need to see someone about a rash"
    )

    routing_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("lookup_departments_tool", {"query_hint": "dermatology"}),
            ai_message_text("UNMATCHED"),
        ]
    )
    monkeypatch.setattr("app.agents.routing.get_llm", lambda: routing_model)

    result = continue_as_booking(db_session, workflow_run)

    assert result.status == WorkflowStatus.needs_review
    assert result.current_step == "routing_agent"
    escalation = db_session.query(Escalation).filter(Escalation.workflow_run_id == result.id).one()
    assert "rash" in escalation.reason


def test_continue_as_staff_escalation_creates_escalation_and_marks_needs_review(db_session):
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)
    workflow_run = _needs_clarification_run(db_session, profile, user)

    result = continue_as_staff_escalation(
        db_session, workflow_run, "Patient asked for help with an unclear request: 'what are your hours?'"
    )

    assert result.status == WorkflowStatus.needs_review
    escalation = db_session.query(Escalation).filter(Escalation.workflow_run_id == result.id).one()
    assert "unclear request" in escalation.reason
    assert result.state["escalation"]["id"] == str(escalation.id)


def test_continue_as_booking_routes_on_override_text_not_stale_original(monkeypatch, db_session):
    # Regression for the "blind re-search of the exact original sentence"
    # gap: a run whose original request_text has nothing routable in it
    # (e.g. "what are your visiting hours") must route on the patient's
    # NEW answer to "what's this for", not the stale original text.
    department = make_department(db_session, name=f"Cardiology {uuid.uuid4().hex[:8]}")
    doctor = make_doctor(db_session, department=department)
    make_appointment_slot(db_session, doctor=doctor)
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)
    workflow_run = _needs_clarification_run(
        db_session, profile, user, request_text="what are your visiting hours?"
    )

    routing_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("lookup_departments_tool", {"query_hint": "cardiology checkup"}),
            ai_message_text(department.name),
        ]
    )
    monkeypatch.setattr("app.agents.routing.get_llm", lambda: routing_model)

    result = continue_as_booking(db_session, workflow_run, override_request_text="cardiology checkup")

    assert result.status == WorkflowStatus.needs_slot_selection
    assert result.state["request_text"] == "cardiology checkup"
    assert result.state["department_id"] == str(department.id)


def test_continue_as_booking_with_department_skips_routing_and_appointment_llm(monkeypatch, db_session):
    # Fully deterministic path: patient picked a real department button, so
    # there is nothing left to guess - neither Routing's department match
    # nor the Appointment agent's slot pick should ever call an LLM.
    department = make_department(db_session, name=f"Cardiology {uuid.uuid4().hex[:8]}")
    doctor = make_doctor(db_session, department=department)
    make_appointment_slot(db_session, doctor=doctor)
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)
    workflow_run = _needs_clarification_run(db_session, profile, user)

    def _fail_if_routing_called():
        raise AssertionError("routing_agent's get_llm must not be called when a department was picked directly")

    def _fail_if_appointment_called():
        raise AssertionError("Appointment agent's LLM must not be called - slot selection is the patient's choice now")

    monkeypatch.setattr("app.agents.routing.get_llm", _fail_if_routing_called)
    monkeypatch.setattr("app.agents.appointment.get_llm", _fail_if_appointment_called)

    result = continue_as_booking_with_department(db_session, workflow_run, str(department.id))

    assert result.status == WorkflowStatus.needs_slot_selection
    assert result.state["department_id"] == str(department.id)
    assert result.state["appointment_id"] is None


def test_continue_as_booking_with_department_lands_completed_when_genuinely_no_slots(db_session):
    department = make_department(db_session, name=f"Empty Dept {uuid.uuid4().hex[:8]}")
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)
    workflow_run = _needs_clarification_run(db_session, profile, user)

    result = continue_as_booking_with_department(db_session, workflow_run, str(department.id))

    assert result.status == WorkflowStatus.completed
    assert result.state["appointment_id"] is None


def test_continue_with_selected_slot_books_the_exact_slot_clicked(db_session):
    department = make_department(db_session, name=f"Cardiology {uuid.uuid4().hex[:8]}")
    doctor = make_doctor(db_session, department=department)
    slot = make_appointment_slot(db_session, doctor=doctor)
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)
    workflow_run = _needs_clarification_run(db_session, profile, user)
    workflow_run.status = WorkflowStatus.needs_slot_selection
    workflow_run.state = {**workflow_run.state, "department_id": str(department.id)}
    db_session.commit()

    result = continue_with_selected_slot(db_session, workflow_run, str(slot.id))

    assert result.status == WorkflowStatus.completed
    assert result.current_step == "appointment_agent"
    assert result.state["appointment_id"] is not None
    appointment = db_session.query(Appointment).filter(Appointment.id == result.state["appointment_id"]).one()
    assert appointment.status.value == "confirmed"
    assert appointment.slot_id == slot.id


def test_continue_with_selected_slot_stays_at_selection_on_conflict(db_session):
    # If the slot was taken between listing and this click, the existing
    # book_or_modify_appointment conflict check catches it - this must
    # NOT silently fail the whole run, it should let the patient pick a
    # different real slot instead.
    department = make_department(db_session, name=f"Cardiology {uuid.uuid4().hex[:8]}")
    doctor = make_doctor(db_session, department=department)
    slot = make_appointment_slot(db_session, doctor=doctor)
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)
    workflow_run = _needs_clarification_run(db_session, profile, user)
    workflow_run.status = WorkflowStatus.needs_slot_selection
    workflow_run.state = {**workflow_run.state, "department_id": str(department.id)}
    db_session.commit()

    other_profile = make_patient_profile(db_session)
    other_run = _needs_clarification_run(db_session, other_profile, make_user(db_session))
    other_run.status = WorkflowStatus.needs_slot_selection
    db_session.commit()
    taken = continue_with_selected_slot(db_session, other_run, str(slot.id))
    assert taken.status == WorkflowStatus.completed

    result = continue_with_selected_slot(db_session, workflow_run, str(slot.id))

    assert result.status == WorkflowStatus.needs_slot_selection
    assert result.state["appointment_id"] is None


def test_run_workflow_cancel_intent_lands_on_needs_appointment_selection(monkeypatch, db_session):
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)
    make_appointment(db_session, patient=profile)

    safety_model = FakeToolCallingModel([ai_message_text("SAFE")])
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: safety_model)

    coordinator_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("get_or_create_patient_tool", {"profile_fields": {}}),
            ai_message_text("cancel_appointment"),
        ]
    )
    monkeypatch.setattr("app.agents.coordinator.get_llm", lambda: coordinator_model)

    workflow_run = run_workflow(
        db_session,
        patient_id=str(profile.id),
        user_id=str(user.id),
        request_text="I need to cancel my appointment",
    )

    assert workflow_run.status == WorkflowStatus.needs_appointment_selection
    assert workflow_run.state["pending_appointment_action"] == "cancel"


def test_run_workflow_cancel_intent_with_no_appointments_lands_on_completed(monkeypatch, db_session):
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)

    safety_model = FakeToolCallingModel([ai_message_text("SAFE")])
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: safety_model)

    coordinator_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("get_or_create_patient_tool", {"profile_fields": {}}),
            ai_message_text("cancel_appointment"),
        ]
    )
    monkeypatch.setattr("app.agents.coordinator.get_llm", lambda: coordinator_model)

    workflow_run = run_workflow(
        db_session,
        patient_id=str(profile.id),
        user_id=str(user.id),
        request_text="I need to cancel my appointment",
    )

    assert workflow_run.status == WorkflowStatus.completed
    assert workflow_run.state["pending_appointment_action"] == "cancel"
    assert workflow_run.state["appointment_id"] is None


def test_continue_as_appointment_action_cancel_cancels_and_frees_slot(db_session):
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)
    doctor = make_doctor(db_session)
    slot = make_appointment_slot(db_session, doctor=doctor)
    appointment = make_appointment(db_session, patient=profile, doctor=doctor, slot=slot)

    workflow_run = make_workflow_run(db_session, profile=profile)
    workflow_run.status = WorkflowStatus.needs_appointment_selection
    workflow_run.state = {
        "workflow_run_id": str(workflow_run.id),
        "patient_id": str(profile.id),
        "user_id": str(user.id),
        "pending_appointment_action": "cancel",
        "status": "needs_appointment_selection",
    }
    db_session.commit()

    result = continue_as_appointment_action(db_session, workflow_run, str(appointment.id))

    assert result.status == WorkflowStatus.completed
    fetched_appointment = db_session.get(Appointment, appointment.id)
    assert fetched_appointment.status == AppointmentStatus.cancelled
    fetched_slot = db_session.get(AppointmentSlot, slot.id)
    assert fetched_slot.status == SlotStatus.open


def test_continue_as_appointment_action_reschedule_lands_on_needs_slot_selection(db_session):
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)
    department = make_department(db_session)
    doctor = make_doctor(db_session, department=department)
    slot1 = make_appointment_slot(db_session, doctor=doctor)
    make_appointment_slot(db_session, doctor=doctor)
    appointment = make_appointment(db_session, patient=profile, doctor=doctor, slot=slot1)

    workflow_run = make_workflow_run(db_session, profile=profile)
    workflow_run.status = WorkflowStatus.needs_appointment_selection
    workflow_run.state = {
        "workflow_run_id": str(workflow_run.id),
        "patient_id": str(profile.id),
        "user_id": str(user.id),
        "pending_appointment_action": "reschedule",
        "status": "needs_appointment_selection",
    }
    db_session.commit()

    result = continue_as_appointment_action(db_session, workflow_run, str(appointment.id))

    assert result.status == WorkflowStatus.needs_slot_selection
    assert result.state["department_id"] == str(department.id)
    assert result.state["rescheduling_appointment_id"] == str(appointment.id)


def test_continue_with_selected_slot_reschedules_existing_appointment(db_session):
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)
    department = make_department(db_session)
    doctor = make_doctor(db_session, department=department)
    slot1 = make_appointment_slot(db_session, doctor=doctor)
    slot2 = make_appointment_slot(db_session, doctor=doctor)
    appointment = make_appointment(db_session, patient=profile, doctor=doctor, slot=slot1)

    workflow_run = make_workflow_run(db_session, profile=profile)
    workflow_run.status = WorkflowStatus.needs_slot_selection
    workflow_run.state = {
        "workflow_run_id": str(workflow_run.id),
        "patient_id": str(profile.id),
        "user_id": str(user.id),
        "department_id": str(department.id),
        "pending_appointment_action": "reschedule",
        "rescheduling_appointment_id": str(appointment.id),
        "status": "needs_slot_selection",
    }
    db_session.commit()

    result = continue_with_selected_slot(db_session, workflow_run, str(slot2.id))

    assert result.status == WorkflowStatus.completed
    fetched_appointment = db_session.get(Appointment, appointment.id)
    assert fetched_appointment.status == AppointmentStatus.rescheduled
    assert fetched_appointment.slot_id == slot2.id
    freed_slot = db_session.get(AppointmentSlot, slot1.id)
    assert freed_slot.status == SlotStatus.open

