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
