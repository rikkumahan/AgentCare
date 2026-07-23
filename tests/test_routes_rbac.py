import uuid

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _unique_email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


def test_register_sets_session_cookie_and_redirects():
    email = _unique_email("patient")
    resp = client.post(
        "/register",
        data={"name": "Alice", "email": email, "password": "supersecret1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"
    assert resp.cookies.get("agentcare_session") is not None


def test_duplicate_email_registration_rejected():
    email = _unique_email("dup")
    client.post("/register", data={"name": "First", "email": email, "password": "supersecret1"})
    resp = client.post("/register", data={"name": "Second", "email": email, "password": "supersecret1"})
    assert resp.status_code == 400
    assert "already registered" in resp.text


def test_login_wrong_password_rejected():
    email = _unique_email("wrongpw")
    client.post("/register", data={"name": "Carol", "email": email, "password": "supersecret1"})
    resp = client.post("/login", data={"email": email, "password": "not-the-password"})
    assert resp.status_code == 400
    assert "Invalid email or password" in resp.text


def test_registered_patient_can_view_own_dashboard():
    email = _unique_email("dashpatient")
    resp = client.post(
        "/register",
        data={"name": "Dana", "email": email, "password": "supersecret1"},
        follow_redirects=False,
    )
    cookie = resp.cookies.get("agentcare_session")

    client.cookies.set("agentcare_session", cookie)
    dash = client.get("/dashboard")
    assert dash.status_code == 200
    assert "Dana" in dash.text


def test_patient_cannot_access_staff_dashboard():
    email = _unique_email("nostaff")
    resp = client.post(
        "/register",
        data={"name": "Eve", "email": email, "password": "supersecret1"},
        follow_redirects=False,
    )
    cookie = resp.cookies.get("agentcare_session")

    client.cookies.set("agentcare_session", cookie)
    staff_resp = client.get("/staff/dashboard")
    assert staff_resp.status_code == 403


def test_unauthenticated_request_gets_401():
    # ponytail: module-level `client` is a stateful TestClient whose cookie
    # jar persists Set-Cookie headers from earlier tests' /register and
    # /login calls, so a bare client.get() here would silently carry a
    # leftover session cookie. Clear it to actually test the unauthenticated
    # path rather than an accidental leftover-auth path.
    client.cookies.clear()
    resp = client.get("/dashboard")
    assert resp.status_code == 401


def test_staff_user_can_access_staff_dashboard_but_not_patient_dashboard(db_session):
    from app.auth import hash_password
    from app.models import User, UserRole

    email = _unique_email("staffuser")
    staff = User(name="Frank Staff", email=email, password_hash=hash_password("staffpass1"), role=UserRole.staff)
    db_session.add(staff)
    db_session.commit()

    login_resp = client.post("/login", data={"email": email, "password": "staffpass1"}, follow_redirects=False)
    assert login_resp.status_code == 303
    assert login_resp.headers["location"] == "/staff/dashboard"
    cookie = login_resp.cookies.get("agentcare_session")

    client.cookies.set("agentcare_session", cookie)
    staff_dash = client.get("/staff/dashboard")
    assert staff_dash.status_code == 200
    assert "Frank Staff" in staff_dash.text

    patient_dash = client.get("/dashboard")
    assert patient_dash.status_code == 403
