import os
import uuid

from fastapi.testclient import TestClient

from app.auth import hash_password
from app.main import app
from app.models import Appointment, PatientDocument, PatientProfile, User, UserRole, WorkflowRun
from tests.fakes import (
    FakeToolCallingModel,
    ai_message_text,
    ai_message_with_tool_call,
    make_appointment_slot,
    make_department,
    make_doctor,
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
    assert "running" in status_resp.text
    assert "document_agent" in status_resp.text

    workflow_run_id = location.rsplit("/", 1)[-1]
    workflow_run = db_session.get(WorkflowRun, workflow_run_id)
    assert workflow_run.status.value == "running"
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
    # A double-click, a slow page prompting a repeat click, or a browser
    # resubmitting a POST on refresh must never trigger a second real
    # workflow run (and possibly a second real booking) - confirmed as a
    # real risk during manual testing, not hypothetical.
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

    cookie = _register_patient("Resubmit Patient")
    client.cookies.set("agentcare_session", cookie)

    request_text = "book a cardiology appointment"
    first = client.post("/requests/new", data={"request_text": request_text}, follow_redirects=False)
    assert first.status_code == 303
    first_location = first.headers["location"]

    # No new mocks configured for a second real run - if the guard failed
    # and a second workflow actually executed, this would either exhaust
    # the fakes' scripted responses (IndexError) or hit the real API.
    second = client.post("/requests/new", data={"request_text": request_text}, follow_redirects=False)
    assert second.status_code == 303
    assert second.headers["location"] == first_location

    workflow_run_id = first_location.rsplit("/", 1)[-1]
    workflow_run = db_session.get(WorkflowRun, workflow_run_id)
    assert db_session.query(Appointment).filter(Appointment.patient_id == workflow_run.patient_id).count() == 1


def test_patient_cannot_view_another_patients_request(monkeypatch, db_session):
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
    routing_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("lookup_departments_tool", {"query_hint": "x"}),
            ai_message_text("UNMATCHED"),
        ]
    )
    monkeypatch.setattr("app.agents.routing.get_llm", lambda: routing_model)

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
    routing_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("lookup_departments_tool", {"query_hint": "x"}),
            ai_message_text("UNMATCHED"),
            ai_message_with_tool_call("lookup_departments_tool", {"query_hint": "x"}),
            ai_message_text("UNMATCHED"),
        ]
    )
    monkeypatch.setattr("app.agents.routing.get_llm", lambda: routing_model)

    cookie = _register_patient("Dup Doc Patient")
    client.cookies.set("agentcare_session", cookie)

    # Different request_text on each submission - identical text within
    # DUPLICATE_SUBMIT_WINDOW_SECONDS would trip the route's own
    # same-request dedup guard and redirect to the first run without
    # running a second workflow at all, which would defeat this test.
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
