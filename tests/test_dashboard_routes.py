import uuid
from fastapi.testclient import TestClient

from app.auth import create_session_token
from app.main import app
from app.models import UserRole, WorkflowStatus
from app.rbac import SESSION_COOKIE_NAME
from tests.fakes import (
    make_appointment,
    make_appointment_slot,
    make_department,
    make_doctor,
    make_patient_profile,
    make_user,
    make_workflow_run,
)

client = TestClient(app)


def test_patient_dashboard_shows_request_history_and_new_request_link(db_session):
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)

    run1 = make_workflow_run(db_session, profile=profile)
    run1.state = {"request_text": "Need cardiology appointment", "document_ids": []}
    run1.status = WorkflowStatus.completed

    run2 = make_workflow_run(db_session, profile=profile)
    run2.state = {"request_text": "Uploaded insurance card", "document_ids": ["doc-123"]}
    run2.status = WorkflowStatus.running
    db_session.commit()

    token = create_session_token(str(user.id))
    client.cookies.set(SESSION_COOKIE_NAME, token)

    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "Need cardiology appointment" in response.text
    assert "Uploaded insurance card" in response.text
    assert f"/requests/{run1.id}" in response.text
    assert f"/requests/{run2.id}" in response.text
    assert "/requests/new" in response.text


def test_patient_dashboard_shows_real_appointment_details_not_just_status(db_session):
    # Regression: "completed" alone doesn't tell the patient WHAT got
    # booked. The dashboard must show the real doctor/department/time for
    # any request that resulted in a booked appointment, not just a status
    # badge - found via actually looking at the rendered page, not by
    # reading the code.
    department = make_department(db_session, name=f"General Medicine {uuid.uuid4().hex[:8]}")
    doctor = make_doctor(db_session, department=department)
    slot = make_appointment_slot(db_session, doctor=doctor)
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)
    appointment = make_appointment(db_session, patient=profile, doctor=doctor, slot=slot)

    run = make_workflow_run(db_session, profile=profile)
    run.state = {"request_text": "book general medicine", "document_ids": [], "appointment_id": str(appointment.id)}
    run.status = WorkflowStatus.completed
    db_session.commit()

    token = create_session_token(str(user.id))
    client.cookies.set(SESSION_COOKIE_NAME, token)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert doctor.name in response.text
    assert department.name in response.text


def test_patient_dashboard_empty_state(db_session):
    user = make_user(db_session)
    make_patient_profile(db_session, user=user)
    db_session.commit()

    token = create_session_token(str(user.id))
    client.cookies.set(SESSION_COOKIE_NAME, token)

    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "You haven't submitted any healthcare requests yet" in response.text
    assert "/requests/new" in response.text


def test_navigation_bar_links_for_logged_in_patient(db_session):
    user = make_user(db_session)
    make_patient_profile(db_session, user=user)
    db_session.commit()

    token = create_session_token(str(user.id))
    client.cookies.set(SESSION_COOKIE_NAME, token)

    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "AgentCare" in response.text
    assert 'href="/dashboard"' in response.text
    assert 'href="/requests/new"' in response.text
    assert 'action="/logout"' in response.text


def test_staff_dashboard_lists_escalated_runs(db_session):
    staff_user = make_user(db_session, role=UserRole.staff)
    patient_user = make_user(db_session, role=UserRole.patient)

    profile = make_patient_profile(db_session, user=patient_user)

    run = make_workflow_run(db_session, profile=profile)
    run.status = WorkflowStatus.needs_review
    run.state = {"request_text": "I have chest pain", "escalation": {"reason": "Emergency keywords detected"}}
    db_session.commit()

    token = create_session_token(str(staff_user.id))
    client.cookies.set(SESSION_COOKIE_NAME, token)

    response = client.get("/staff/dashboard")
    assert response.status_code == 200
    assert "Staff Dashboard" in response.text
    assert "I have chest pain" in response.text
    assert "Emergency keywords detected" in response.text
