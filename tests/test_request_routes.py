import os
import uuid

from fastapi.testclient import TestClient

from app.auth import hash_password
from app.main import app
from app.models import Appointment, PatientDocument, PatientProfile, User, UserRole, WorkflowRun, WorkflowStatus
from app.routes.request_routes import _render_patient_message
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

client = TestClient(app)


def _document_model() -> FakeToolCallingModel:
    # file_path is server-injected (InjectedState) now, not an LLM-settable
    # tool arg - the route generates the saved path with a fresh uuid4() we
    # can't predict in advance, but that's fine, the model doesn't need it
    # any more; the real path comes from graph state regardless of what's
    # in the tool call args.
    return FakeToolCallingModel(
        [
            ai_message_with_tool_call(
                "store_and_classify_document_tool",
                {"document_type": "insurance"},
            ),
            ai_message_text("Saved your document."),
        ]
    )


def _unique_email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


def _register_patient(name: str) -> str:
    email = _unique_email("req")
    resp = client.post(
        "/register",
        data={"name": name, "email": email, "password": "supersecret1"},
        follow_redirects=False,
    )
    return resp.cookies.get("agentcare_session")


def test_unauthenticated_cannot_access_new_request_form():
    client.cookies.clear()
    resp = client.get("/requests/new")
    assert resp.status_code == 401


def test_staff_cannot_access_new_request_form(db_session):
    email = _unique_email("staffreq")
    staff = User(name="Staff Req", email=email, password_hash=hash_password("staffpass1"), role=UserRole.staff)
    db_session.add(staff)
    db_session.commit()

    login_resp = client.post("/login", data={"email": email, "password": "staffpass1"}, follow_redirects=False)
    client.cookies.set("agentcare_session", login_resp.cookies.get("agentcare_session"))

    resp = client.get("/requests/new")
    assert resp.status_code == 403


def test_patient_submits_request_and_sees_real_booking_result(monkeypatch, db_session):
    department = make_department(db_session, name=f"Cardiology {uuid.uuid4().hex[:8]}")
    doctor = make_doctor(db_session, department=department)
    slot = make_appointment_slot(db_session, doctor=doctor)

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
            ai_message_text(department.name),
        ]
    )
    monkeypatch.setattr("app.agents.routing.get_llm", lambda: routing_model)

    def _fail_if_appointment_llm_called():
        raise AssertionError("Appointment agent's LLM must not run automatically - slot selection is the patient's choice now")

    monkeypatch.setattr("app.agents.appointment.get_llm", _fail_if_appointment_llm_called)

    cookie = _register_patient("Req Patient")
    client.cookies.set("agentcare_session", cookie)

    resp = client.post(
        "/requests/new", data={"request_text": "book a cardiology appointment"}, follow_redirects=False
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/requests/")

    status_resp = client.get(location)
    assert status_resp.status_code == 200
    assert doctor.name in status_resp.text

    workflow_run_id = location.rsplit("/", 1)[-1]
    workflow_run = db_session.get(WorkflowRun, workflow_run_id)
    assert workflow_run.status.value == "needs_slot_selection"
    assert workflow_run.state["appointment_id"] is None

    select_resp = client.post(
        f"/requests/{workflow_run_id}/select-slot", data={"slot_id": str(slot.id)}, follow_redirects=False
    )
    assert select_resp.status_code == 303

    status_resp = client.get(location)
    assert f"booked with {doctor.name} in {department.name}" in status_resp.text

    db_session.expire_all()
    workflow_run = db_session.get(WorkflowRun, workflow_run_id)
    assert workflow_run.status.value == "completed"
    appointment = db_session.query(Appointment).filter(Appointment.id == workflow_run.state["appointment_id"]).one()
    assert appointment.status.value == "confirmed"


def test_submit_request_with_document_saves_file_and_passes_path_to_workflow(monkeypatch, db_session, tmp_path):
    monkeypatch.setattr("app.config.settings.storage_dir", str(tmp_path))

    captured = {}

    def fake_run_workflow(db, patient_id, user_id, request_text, uploaded_files=None):
        captured["patient_id"] = patient_id
        captured["uploaded_files"] = uploaded_files
        profile = db.get(PatientProfile, uuid.UUID(patient_id))
        return make_workflow_run(db, profile=profile)

    monkeypatch.setattr("app.routes.request_routes.run_workflow", fake_run_workflow)

    cookie = _register_patient("Doc Patient")
    client.cookies.set("agentcare_session", cookie)

    resp = client.post(
        "/requests/new",
        data={"request_text": "here is my insurance card"},
        files={"document": ("insurance.pdf", b"pdf-bytes-content", "application/pdf")},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    assert captured["uploaded_files"] is not None
    assert len(captured["uploaded_files"]) == 1
    saved_path = captured["uploaded_files"][0]
    assert saved_path.startswith(os.path.join(str(tmp_path), captured["patient_id"]))
    with open(saved_path, "rb") as f:
        assert f.read() == b"pdf-bytes-content"


def test_submit_request_with_path_separator_in_filename_is_sanitized(monkeypatch, db_session, tmp_path):
    monkeypatch.setattr("app.config.settings.storage_dir", str(tmp_path))

    captured = {}

    def fake_run_workflow(db, patient_id, user_id, request_text, uploaded_files=None):
        captured["patient_id"] = patient_id
        captured["uploaded_files"] = uploaded_files
        profile = db.get(PatientProfile, uuid.UUID(patient_id))
        return make_workflow_run(db, profile=profile)

    monkeypatch.setattr("app.routes.request_routes.run_workflow", fake_run_workflow)

    cookie = _register_patient("Traversal Patient")
    client.cookies.set("agentcare_session", cookie)

    resp = client.post(
        "/requests/new",
        data={"request_text": "here is my insurance card"},
        files={"document": ("../../evil.txt", b"malicious-bytes", "text/plain")},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    saved_path = captured["uploaded_files"][0]
    patient_dir = os.path.join(str(tmp_path), captured["patient_id"])
    assert saved_path.startswith(patient_dir)
    assert os.path.dirname(saved_path) == patient_dir
    assert saved_path.endswith("_evil.txt")
    with open(saved_path, "rb") as f:
        assert f.read() == b"malicious-bytes"


def test_submit_request_without_document_passes_no_uploaded_files(monkeypatch, db_session):
    captured = {}

    def fake_run_workflow(db, patient_id, user_id, request_text, uploaded_files=None):
        captured["uploaded_files"] = uploaded_files
        profile = db.get(PatientProfile, uuid.UUID(patient_id))
        return make_workflow_run(db, profile=profile)

    monkeypatch.setattr("app.routes.request_routes.run_workflow", fake_run_workflow)

    cookie = _register_patient("No Doc Patient")
    client.cookies.set("agentcare_session", cookie)

    resp = client.post("/requests/new", data={"request_text": "just a question"}, follow_redirects=False)
    assert resp.status_code == 303
    assert captured["uploaded_files"] == []


def test_resubmitting_the_same_request_quickly_does_not_run_the_workflow_twice(monkeypatch, db_session):
    department = make_department(db_session, name=f"Cardiology {uuid.uuid4().hex[:8]}")
    doctor = make_doctor(db_session, department=department)
    slot = make_appointment_slot(db_session, doctor=doctor)

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
            ai_message_text(department.name),
        ]
    )
    monkeypatch.setattr("app.agents.routing.get_llm", lambda: routing_model)

    def _fail_if_appointment_llm_called():
        raise AssertionError("Appointment agent's LLM must not run automatically - slot selection is the patient's choice now")

    monkeypatch.setattr("app.agents.appointment.get_llm", _fail_if_appointment_llm_called)

    cookie = _register_patient("Resubmit Patient")
    client.cookies.set("agentcare_session", cookie)

    request_text = "book a cardiology appointment"
    first = client.post("/requests/new", data={"request_text": request_text}, follow_redirects=False)
    assert first.status_code == 303
    first_location = first.headers["location"]

    second = client.post("/requests/new", data={"request_text": request_text}, follow_redirects=False)
    assert second.status_code == 303
    assert second.headers["location"] == first_location

    workflow_run_id = first_location.rsplit("/", 1)[-1]
    workflow_run = db_session.get(WorkflowRun, workflow_run_id)
    # No new mocks configured for a second real run - if the guard failed
    # and a second workflow actually executed, Routing's fake model would
    # run out of scripted responses (IndexError) or hit the real API.
    # Neither run auto-books anymore, so a single WorkflowRun row for this
    # patient/request_text (not a second, independent one) is the
    # meaningful proof the guard worked here, not an Appointment count.
    matching_runs = (
        db_session.query(WorkflowRun).filter(WorkflowRun.patient_id == workflow_run.patient_id).all()
    )
    assert len(matching_runs) == 1


def test_patient_cannot_view_another_patients_request(monkeypatch, db_session):
    safety_model = FakeToolCallingModel([ai_message_text("SAFE")])
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: safety_model)
    coordinator_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("get_or_create_patient_tool", {"profile_fields": {}}),
            ai_message_text("general_inquiry"),
        ]
    )
    monkeypatch.setattr("app.agents.coordinator.get_llm", lambda: coordinator_model)

    cookie_a = _register_patient("Patient A")
    client.cookies.set("agentcare_session", cookie_a)
    resp = client.post("/requests/new", data={"request_text": "book something odd"}, follow_redirects=False)
    location = resp.headers["location"]

    cookie_b = _register_patient("Patient B")
    client.cookies.set("agentcare_session", cookie_b)
    resp_b = client.get(location)
    assert resp_b.status_code == 403


def test_viewing_nonexistent_request_returns_404():
    cookie = _register_patient("Patient C")
    client.cookies.set("agentcare_session", cookie)
    resp = client.get(f"/requests/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_submitting_request_with_attached_file_saves_it_to_disk_and_creates_document_row(monkeypatch, db_session):
    safety_model = FakeToolCallingModel([ai_message_text("SAFE")])
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: safety_model)
    coordinator_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("get_or_create_patient_tool", {"profile_fields": {}}),
            ai_message_text("submit_document"),
        ]
    )
    monkeypatch.setattr("app.agents.coordinator.get_llm", lambda: coordinator_model)
    document_model = _document_model()
    monkeypatch.setattr("app.agents.document.get_llm", lambda: document_model)

    cookie = _register_patient("Doc Patient")
    client.cookies.set("agentcare_session", cookie)

    resp = client.post(
        "/requests/new",
        data={"request_text": "here is my insurance card"},
        files={"document": ("insurance.pdf", b"insurance-file-bytes-1", "application/pdf")},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    workflow_run_id = resp.headers["location"].rsplit("/", 1)[-1]
    workflow_run = db_session.get(WorkflowRun, workflow_run_id)
    document_ids = workflow_run.state.get("document_ids") or []
    assert len(document_ids) == 1

    document = db_session.query(PatientDocument).filter(PatientDocument.id == uuid.UUID(document_ids[0])).one()
    assert os.path.isfile(document.file_path)
    with open(document.file_path, "rb") as f:
        assert f.read() == b"insurance-file-bytes-1"


def test_submitting_same_document_bytes_twice_does_not_create_a_second_row(monkeypatch, db_session):
    safety_model = FakeToolCallingModel([ai_message_text("SAFE"), ai_message_text("SAFE")])
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: safety_model)
    coordinator_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("get_or_create_patient_tool", {"profile_fields": {}}),
            ai_message_text("submit_document"),
            ai_message_with_tool_call("get_or_create_patient_tool", {"profile_fields": {}}),
            ai_message_text("submit_document"),
        ]
    )
    monkeypatch.setattr("app.agents.coordinator.get_llm", lambda: coordinator_model)

    cookie = _register_patient("Dup Doc Patient")
    client.cookies.set("agentcare_session", cookie)

    first_document_model = _document_model()
    monkeypatch.setattr("app.agents.document.get_llm", lambda: first_document_model)
    first = client.post(
        "/requests/new",
        data={"request_text": "here is my insurance card, first upload"},
        files={"document": ("insurance.pdf", b"same-insurance-bytes", "application/pdf")},
        follow_redirects=False,
    )
    assert first.status_code == 303

    second_document_model = _document_model()
    monkeypatch.setattr("app.agents.document.get_llm", lambda: second_document_model)
    second = client.post(
        "/requests/new",
        data={"request_text": "here is my insurance card, second upload"},
        files={"document": ("insurance.pdf", b"same-insurance-bytes", "application/pdf")},
        follow_redirects=False,
    )
    assert second.status_code == 303

    first_run = db_session.get(WorkflowRun, first.headers["location"].rsplit("/", 1)[-1])
    count = (
        db_session.query(PatientDocument)
        .filter(PatientDocument.patient_id == first_run.patient_id)
        .count()
    )
    assert count == 1


def test_render_patient_message_needs_clarification(db_session):
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)
    workflow_run = make_workflow_run(db_session, profile=profile)
    workflow_run.status = WorkflowStatus.needs_clarification
    workflow_run.state = {"document_ids": []}
    db_session.commit()

    message = _render_patient_message(db_session, user, workflow_run)
    assert message == f"Hi {user.name}! I want to make sure I help you with the right thing."


def test_render_patient_message_needs_appointment_reason(db_session):
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)
    workflow_run = make_workflow_run(db_session, profile=profile)
    workflow_run.status = WorkflowStatus.needs_appointment_reason
    workflow_run.state = {"document_ids": []}
    db_session.commit()

    message = _render_patient_message(db_session, user, workflow_run)
    assert message == "What's this appointment for? Pick a department below, or describe it in your own words."


def test_render_patient_message_completed_with_appointment(db_session):
    department = make_department(db_session, name=f"Neurology {uuid.uuid4().hex[:8]}")
    doctor = make_doctor(db_session, department=department)
    slot = make_appointment_slot(db_session, doctor=doctor)
    appointment = make_appointment(db_session, doctor=doctor, slot=slot)
    user = make_user(db_session)
    workflow_run = make_workflow_run(db_session)
    workflow_run.status = WorkflowStatus.completed
    workflow_run.state = {"appointment_id": str(appointment.id), "document_ids": []}
    db_session.commit()

    message = _render_patient_message(db_session, user, workflow_run)
    formatted_time = slot.start_time.strftime("%B %d, %Y at %I:%M %p")
    assert message == (
        f"Great news, {user.name}! You're booked with {doctor.name} "
        f"in {department.name} on {formatted_time}."
    )


def test_render_patient_message_completed_without_appointment(db_session):
    user = make_user(db_session)
    workflow_run = make_workflow_run(db_session)
    workflow_run.status = WorkflowStatus.completed
    workflow_run.state = {"appointment_id": None, "document_ids": []}
    db_session.commit()

    message = _render_patient_message(db_session, user, workflow_run)
    assert message == "I couldn't find any open slots right now. Please try again later or contact our staff."


def test_render_patient_message_needs_review_hides_internal_reason(db_session):
    user = make_user(db_session)
    workflow_run = make_workflow_run(db_session)
    workflow_run.status = WorkflowStatus.needs_review
    workflow_run.state = {"escalation": {"reason": "secret internal reason"}, "document_ids": []}
    db_session.commit()

    message = _render_patient_message(db_session, user, workflow_run)
    assert message == "I've passed your request to our staff team - they'll follow up with you soon."
    assert "secret internal reason" not in message


def test_render_patient_message_failed(db_session):
    user = make_user(db_session)
    workflow_run = make_workflow_run(db_session)
    workflow_run.status = WorkflowStatus.failed
    workflow_run.state = {"document_ids": []}
    db_session.commit()

    message = _render_patient_message(db_session, user, workflow_run)
    assert message == (
        "Something went wrong on our end while handling your request. Please try again, or contact our staff directly."
    )


def test_render_patient_message_running_fallback(db_session):
    user = make_user(db_session)
    workflow_run = make_workflow_run(db_session)
    workflow_run.status = WorkflowStatus.running
    workflow_run.state = {"document_ids": []}
    db_session.commit()

    message = _render_patient_message(db_session, user, workflow_run)
    assert message == "I'm still working on this - check back in a moment."


def test_render_patient_message_appends_document_clause(db_session):
    user = make_user(db_session)
    workflow_run = make_workflow_run(db_session)
    workflow_run.status = WorkflowStatus.needs_review
    workflow_run.state = {"escalation": {"reason": "reason"}, "document_ids": ["doc-1"]}
    db_session.commit()

    message = _render_patient_message(db_session, user, workflow_run)
    assert message == (
        "I've passed your request to our staff team - they'll follow up with you soon."
        " I've also saved your uploaded document."
    )


def test_clarify_book_appointment_choice_asks_what_its_for_instead_of_booking_blind(monkeypatch, db_session):
    # Regression for "blindly re-search the exact original sentence for a
    # department": clicking "Book an appointment" must NOT immediately try
    # to book using the original ambiguous request_text - it must first ask
    # what the appointment is for.
    safety_model = FakeToolCallingModel([ai_message_text("SAFE")])
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: safety_model)
    coordinator_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("get_or_create_patient_tool", {"profile_fields": {}}),
            ai_message_text("general_inquiry"),
        ]
    )
    monkeypatch.setattr("app.agents.coordinator.get_llm", lambda: coordinator_model)

    cookie = _register_patient("Clarify Patient")
    client.cookies.set("agentcare_session", cookie)

    resp = client.post("/requests/new", data={"request_text": "what are your hours?"}, follow_redirects=False)
    workflow_run_id = resp.headers["location"].rsplit("/", 1)[-1]

    status_resp = client.get(resp.headers["location"])
    assert "I want to make sure I help you with the right thing" in status_resp.text

    def _fail_if_routing_called():
        raise AssertionError("Routing must not run until the patient answers what the appointment is for")

    monkeypatch.setattr("app.agents.routing.get_llm", _fail_if_routing_called)

    clarify_resp = client.post(
        f"/requests/{workflow_run_id}/clarify", data={"choice": "book_appointment"}, follow_redirects=False
    )
    assert clarify_resp.status_code == 303
    assert clarify_resp.headers["location"] == f"/requests/{workflow_run_id}"

    workflow_run = db_session.get(WorkflowRun, workflow_run_id)
    assert workflow_run.status.value == "needs_appointment_reason"
    assert workflow_run.state["appointment_id"] is None

    status_resp = client.get(f"/requests/{workflow_run_id}")
    assert "this appointment for" in status_resp.text


def test_select_reason_with_department_button_lands_at_slot_selection_no_llm(monkeypatch, db_session):
    department = make_department(db_session, name=f"Cardiology {uuid.uuid4().hex[:8]}")
    doctor = make_doctor(db_session, department=department)
    slot = make_appointment_slot(db_session, doctor=doctor)

    safety_model = FakeToolCallingModel([ai_message_text("SAFE")])
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: safety_model)
    coordinator_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("get_or_create_patient_tool", {"profile_fields": {}}),
            ai_message_text("general_inquiry"),
        ]
    )
    monkeypatch.setattr("app.agents.coordinator.get_llm", lambda: coordinator_model)

    cookie = _register_patient("Department Button Patient")
    client.cookies.set("agentcare_session", cookie)
    resp = client.post("/requests/new", data={"request_text": "what are your hours?"}, follow_redirects=False)
    workflow_run_id = resp.headers["location"].rsplit("/", 1)[-1]
    client.post(f"/requests/{workflow_run_id}/clarify", data={"choice": "book_appointment"}, follow_redirects=False)

    status_resp = client.get(f"/requests/{workflow_run_id}")
    assert department.name in status_resp.text

    def _fail_if_called():
        raise AssertionError("Picking a real department directly must skip both Routing's and Appointment's LLMs")

    monkeypatch.setattr("app.agents.routing.get_llm", _fail_if_called)
    monkeypatch.setattr("app.agents.appointment.get_llm", _fail_if_called)

    select_resp = client.post(
        f"/requests/{workflow_run_id}/select-reason",
        data={"department_id": str(department.id)},
        follow_redirects=False,
    )
    assert select_resp.status_code == 303

    workflow_run = db_session.get(WorkflowRun, workflow_run_id)
    assert workflow_run.status.value == "needs_slot_selection"
    assert workflow_run.state["department_id"] == str(department.id)
    assert workflow_run.state["appointment_id"] is None

    status_resp = client.get(f"/requests/{workflow_run_id}")
    assert doctor.name in status_resp.text

    select_slot_resp = client.post(
        f"/requests/{workflow_run_id}/select-slot", data={"slot_id": str(slot.id)}, follow_redirects=False
    )
    assert select_slot_resp.status_code == 303

    # This row was already loaded into db_session's identity map by the
    # earlier db_session.get() call above - the route's booking just now
    # happened through a DIFFERENT session (FastAPI's own get_db), so
    # db_session needs an explicit refresh or it'll hand back the stale
    # cached Python object instead of re-querying.
    db_session.expire_all()
    workflow_run = db_session.get(WorkflowRun, workflow_run_id)
    assert workflow_run.status.value == "completed"
    assert workflow_run.state["appointment_id"] is not None
    appointment = db_session.query(Appointment).filter(Appointment.id == workflow_run.state["appointment_id"]).one()
    assert appointment.slot_id == slot.id


def test_select_reason_with_free_text_routes_on_new_text_lands_at_slot_selection(monkeypatch, db_session):
    department = make_department(db_session, name=f"Cardiology {uuid.uuid4().hex[:8]}")
    doctor = make_doctor(db_session, department=department)
    make_appointment_slot(db_session, doctor=doctor)

    safety_model = FakeToolCallingModel([ai_message_text("SAFE")])
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: safety_model)
    coordinator_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("get_or_create_patient_tool", {"profile_fields": {}}),
            ai_message_text("general_inquiry"),
        ]
    )
    monkeypatch.setattr("app.agents.coordinator.get_llm", lambda: coordinator_model)

    cookie = _register_patient("Free Text Patient")
    client.cookies.set("agentcare_session", cookie)
    resp = client.post("/requests/new", data={"request_text": "what are your hours?"}, follow_redirects=False)
    workflow_run_id = resp.headers["location"].rsplit("/", 1)[-1]
    client.post(f"/requests/{workflow_run_id}/clarify", data={"choice": "book_appointment"}, follow_redirects=False)

    routing_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("lookup_departments_tool", {"query_hint": "cardiology checkup"}),
            ai_message_text(department.name),
        ]
    )
    monkeypatch.setattr("app.agents.routing.get_llm", lambda: routing_model)

    select_resp = client.post(
        f"/requests/{workflow_run_id}/select-reason",
        data={"reason_text": "cardiology checkup"},
        follow_redirects=False,
    )
    assert select_resp.status_code == 303

    workflow_run = db_session.get(WorkflowRun, workflow_run_id)
    assert workflow_run.status.value == "needs_slot_selection"
    assert workflow_run.state["request_text"] == "cardiology checkup"
    assert workflow_run.state["appointment_id"] is None


def test_select_slot_stale_status_is_a_noop_redirect(db_session):
    cookie = _register_patient("Stale Slot Patient")
    client.cookies.set("agentcare_session", cookie)
    resp = client.post("/requests/new", data={"request_text": "just a question"}, follow_redirects=False)
    workflow_run_id = resp.headers["location"].rsplit("/", 1)[-1]

    select_resp = client.post(
        f"/requests/{workflow_run_id}/select-slot", data={"slot_id": str(uuid.uuid4())}, follow_redirects=False
    )
    assert select_resp.status_code == 303
    assert select_resp.headers["location"] == f"/requests/{workflow_run_id}"


def test_select_slot_wrong_owner_returns_403(db_session):
    cookie_a = _register_patient("Slot Owner A")
    client.cookies.set("agentcare_session", cookie_a)
    resp = client.post("/requests/new", data={"request_text": "anything"}, follow_redirects=False)
    workflow_run_id = resp.headers["location"].rsplit("/", 1)[-1]

    cookie_b = _register_patient("Slot Owner B")
    client.cookies.set("agentcare_session", cookie_b)
    select_resp = client.post(
        f"/requests/{workflow_run_id}/select-slot", data={"slot_id": str(uuid.uuid4())}, follow_redirects=False
    )
    assert select_resp.status_code == 403


def test_select_slot_nonexistent_run_returns_404():
    cookie = _register_patient("Ghost Slot Patient")
    client.cookies.set("agentcare_session", cookie)
    resp = client.post(
        f"/requests/{uuid.uuid4()}/select-slot", data={"slot_id": str(uuid.uuid4())}, follow_redirects=False
    )
    assert resp.status_code == 404


def test_select_reason_stale_status_is_a_noop_redirect(monkeypatch, db_session):
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
            ai_message_with_tool_call("lookup_departments_tool", {"query_hint": "x"}),
            ai_message_text("UNMATCHED"),
        ]
    )
    monkeypatch.setattr("app.agents.routing.get_llm", lambda: routing_model)

    cookie = _register_patient("Stale Select Patient")
    client.cookies.set("agentcare_session", cookie)
    resp = client.post("/requests/new", data={"request_text": "book a checkup"}, follow_redirects=False)
    workflow_run_id = resp.headers["location"].rsplit("/", 1)[-1]

    # This run never entered needs_appointment_reason at all (intent was
    # already a confident booking, so it escalated straight from Routing) -
    # posting to /select-reason on it must be a no-op, not an error.
    select_resp = client.post(
        f"/requests/{workflow_run_id}/select-reason", data={"reason_text": "anything"}, follow_redirects=False
    )
    assert select_resp.status_code == 303
    assert select_resp.headers["location"] == f"/requests/{workflow_run_id}"


def test_select_reason_wrong_owner_returns_403(db_session):
    cookie_a = _register_patient("Select Owner A")
    client.cookies.set("agentcare_session", cookie_a)
    resp = client.post("/requests/new", data={"request_text": "anything"}, follow_redirects=False)
    workflow_run_id = resp.headers["location"].rsplit("/", 1)[-1]

    cookie_b = _register_patient("Select Owner B")
    client.cookies.set("agentcare_session", cookie_b)
    select_resp = client.post(
        f"/requests/{workflow_run_id}/select-reason", data={"reason_text": "x"}, follow_redirects=False
    )
    assert select_resp.status_code == 403


def test_select_reason_nonexistent_run_returns_404():
    cookie = _register_patient("Ghost Select Patient")
    client.cookies.set("agentcare_session", cookie)
    resp = client.post(
        f"/requests/{uuid.uuid4()}/select-reason", data={"reason_text": "x"}, follow_redirects=False
    )
    assert resp.status_code == 404


def test_clarify_staff_choice_escalates(monkeypatch, db_session):
    safety_model = FakeToolCallingModel([ai_message_text("SAFE")])
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: safety_model)
    coordinator_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("get_or_create_patient_tool", {"profile_fields": {}}),
            ai_message_text("general_inquiry"),
        ]
    )
    monkeypatch.setattr("app.agents.coordinator.get_llm", lambda: coordinator_model)

    cookie = _register_patient("Staff Choice Patient")
    client.cookies.set("agentcare_session", cookie)
    resp = client.post("/requests/new", data={"request_text": "what are your hours?"}, follow_redirects=False)
    workflow_run_id = resp.headers["location"].rsplit("/", 1)[-1]

    clarify_resp = client.post(
        f"/requests/{workflow_run_id}/clarify", data={"choice": "staff"}, follow_redirects=False
    )
    assert clarify_resp.status_code == 303
    assert clarify_resp.headers["location"] == f"/requests/{workflow_run_id}"

    workflow_run = db_session.get(WorkflowRun, workflow_run_id)
    assert workflow_run.status.value == "needs_review"
    from app.models import Escalation

    escalation = db_session.query(Escalation).filter(Escalation.workflow_run_id == workflow_run.id).one()
    assert "unclear request" in escalation.reason


def test_clarify_wrong_owner_returns_403(monkeypatch, db_session):
    safety_model = FakeToolCallingModel([ai_message_text("SAFE")])
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: safety_model)
    coordinator_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("get_or_create_patient_tool", {"profile_fields": {}}),
            ai_message_text("general_inquiry"),
        ]
    )
    monkeypatch.setattr("app.agents.coordinator.get_llm", lambda: coordinator_model)

    cookie_a = _register_patient("Owner A")
    client.cookies.set("agentcare_session", cookie_a)
    resp = client.post("/requests/new", data={"request_text": "what are your hours?"}, follow_redirects=False)
    workflow_run_id = resp.headers["location"].rsplit("/", 1)[-1]

    cookie_b = _register_patient("Owner B")
    client.cookies.set("agentcare_session", cookie_b)
    clarify_resp = client.post(
        f"/requests/{workflow_run_id}/clarify", data={"choice": "staff"}, follow_redirects=False
    )
    assert clarify_resp.status_code == 403


def test_clarify_nonexistent_run_returns_404():
    cookie = _register_patient("Ghost Patient")
    client.cookies.set("agentcare_session", cookie)
    resp = client.post(f"/requests/{uuid.uuid4()}/clarify", data={"choice": "staff"}, follow_redirects=False)
    assert resp.status_code == 404


def test_clarify_stale_status_is_a_noop_redirect(monkeypatch, db_session):
    safety_model = FakeToolCallingModel([ai_message_text("SAFE")])
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: safety_model)
    coordinator_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("get_or_create_patient_tool", {"profile_fields": {}}),
            ai_message_text("general_inquiry"),
        ]
    )
    monkeypatch.setattr("app.agents.coordinator.get_llm", lambda: coordinator_model)

    cookie = _register_patient("Stale Patient")
    client.cookies.set("agentcare_session", cookie)
    resp = client.post("/requests/new", data={"request_text": "what are your hours?"}, follow_redirects=False)
    workflow_run_id = resp.headers["location"].rsplit("/", 1)[-1]

    first = client.post(f"/requests/{workflow_run_id}/clarify", data={"choice": "staff"}, follow_redirects=False)
    assert first.status_code == 303

    from app.models import Escalation

    count_before = db_session.query(Escalation).filter(Escalation.workflow_run_id == workflow_run_id).count()
    assert count_before == 1

    second = client.post(f"/requests/{workflow_run_id}/clarify", data={"choice": "staff"}, follow_redirects=False)
    assert second.status_code == 303
    assert second.headers["location"] == f"/requests/{workflow_run_id}"

    count_after = db_session.query(Escalation).filter(Escalation.workflow_run_id == workflow_run_id).count()
    assert count_after == 1


def test_clarify_invalid_choice_returns_400(monkeypatch, db_session):
    safety_model = FakeToolCallingModel([ai_message_text("SAFE")])
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: safety_model)
    coordinator_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("get_or_create_patient_tool", {"profile_fields": {}}),
            ai_message_text("general_inquiry"),
        ]
    )
    monkeypatch.setattr("app.agents.coordinator.get_llm", lambda: coordinator_model)

    cookie = _register_patient("BadChoice Patient")
    client.cookies.set("agentcare_session", cookie)
    resp = client.post("/requests/new", data={"request_text": "what are your hours?"}, follow_redirects=False)
    workflow_run_id = resp.headers["location"].rsplit("/", 1)[-1]

    bad = client.post(f"/requests/{workflow_run_id}/clarify", data={"choice": "nonsense"}, follow_redirects=False)
    assert bad.status_code == 400
