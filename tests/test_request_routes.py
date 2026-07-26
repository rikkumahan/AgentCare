import uuid

from fastapi.testclient import TestClient

from app.auth import hash_password
from app.main import app
from app.models import Appointment, User, UserRole, WorkflowRun
from tests.fakes import (
    FakeToolCallingModel,
    ai_message_text,
    ai_message_with_tool_call,
    make_appointment_slot,
    make_department,
    make_doctor,
)

client = TestClient(app)


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
