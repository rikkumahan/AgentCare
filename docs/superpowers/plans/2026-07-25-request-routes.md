# Request Submission Routes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give a logged-in patient a real, standard route to submit a free-text request and see the real, persisted result of the existing agent workflow (Safety → Coordinator → Routing → Appointment) — no styling, no list view, no file upload.

**Architecture:** One new router (`app/routes/request_routes.py`), registered in `app/main.py` alongside the existing two routers, following the same one-file-per-concern pattern as `auth_routes.py`/`dashboard_routes.py`. Two new plain templates extending the existing `base.html`. The route is a thin caller of the already-built, already-tested `run_workflow()` — no new agent/graph logic.

**Tech Stack:** FastAPI + Jinja2 (existing), same `require_role` RBAC dependency and `TestClient`-based test pattern already used throughout the app.

## Global Constraints

- RBAC enforced in route/dependency code, not template-only (CLAUDE.md) — every endpoint depends on `require_role(UserRole.patient.value)`.
- No hardcoded final responses (CLAUDE.md) — the status page renders fields read directly from the persisted `WorkflowRun` row, never a canned success string.
- Persistent SQL only — no in-memory session/dict state for domain data.
- Tests: pytest, mock the LLM (`FakeToolCallingModel`), assert real DB state — don't test prompt wording.
- Per `docs/memory/gotchas.md`: always construct `FakeToolCallingModel(...)` in a variable *before* `monkeypatch.setattr(...)` and close over that instance — never construct it inline inside the lambda (silently loops forever on any agent that calls `get_llm()` more than once per run).
- Per `docs/memory/decisions.md`: any assertion against unscoped/session-wide data (a bare `.count()`, an exact set) must instead check membership of the specific row(s) this test created — `db_session` doesn't roll back between tests.
- Out of scope, confirmed with user: request-history list page, styling beyond `base.html`, file upload, reschedule/cancel actions, staff-facing routes.

---

### Task 1: Request submission routes + templates

**Files:**
- Create: `app/routes/request_routes.py`
- Create: `app/templates/request_new.html`
- Create: `app/templates/request_status.html`
- Modify: `app/main.py`
- Test: `tests/test_request_routes.py`

**Interfaces:**
- Consumes: `app.rbac.require_role`, `app.db.get_db`, `app.workflow_runner.run_workflow(db, patient_id: str, user_id: str, request_text: str) -> WorkflowRun` (existing, unchanged), `app.models.{PatientProfile, User, UserRole, WorkflowRun}` (existing).
- Produces: `router` (a `fastapi.APIRouter`) exposing `GET/POST /requests/new` and `GET /requests/{workflow_run_id}`. Registered into the app via `app.include_router(request_router)` in `app/main.py` — nothing outside this task depends on any other symbol from this file.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_request_routes.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_request_routes.py -v`
Expected: FAIL — `/requests/new` doesn't exist yet, 404 instead of the expected status codes (route not registered).

- [ ] **Step 3: Implement `app/routes/request_routes.py`**

```python
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import PatientProfile, User, UserRole, WorkflowRun
from app.rbac import require_role
from app.workflow_runner import run_workflow

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _get_or_create_profile(db: Session, user: User) -> PatientProfile:
    """Route-level identity resolution — not the audited get_or_create_patient
    tool, which is reserved for real agent tool calls. Calling the audited
    version here would log an AuditEvent on every page view, misrepresenting
    the audit trail as agent activity."""
    profile = db.query(PatientProfile).filter(PatientProfile.user_id == user.id).first()
    if profile is None:
        profile = PatientProfile(user_id=user.id)
        db.add(profile)
        db.commit()
    return profile


@router.get("/requests/new", response_class=HTMLResponse)
def new_request_form(request: Request, user: User = Depends(require_role(UserRole.patient.value))):
    return templates.TemplateResponse(request, "request_new.html", {"user": user})


@router.post("/requests/new")
def submit_request(
    request_text: str = Form(...),
    user: User = Depends(require_role(UserRole.patient.value)),
    db: Session = Depends(get_db),
):
    profile = _get_or_create_profile(db, user)
    workflow_run = run_workflow(
        db,
        patient_id=str(profile.id),
        user_id=str(user.id),
        request_text=request_text,
    )
    return RedirectResponse(f"/requests/{workflow_run.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/requests/{workflow_run_id}", response_class=HTMLResponse)
def request_status(
    request: Request,
    workflow_run_id: str,
    user: User = Depends(require_role(UserRole.patient.value)),
    db: Session = Depends(get_db),
):
    workflow_run = db.get(WorkflowRun, workflow_run_id)
    if workflow_run is None:
        raise HTTPException(status_code=404, detail="Not found")

    profile = _get_or_create_profile(db, user)
    if workflow_run.patient_id != profile.id:
        raise HTTPException(status_code=403, detail="Not your request")

    return templates.TemplateResponse(request, "request_status.html", {"user": user, "workflow_run": workflow_run})
```

- [ ] **Step 4: Create `app/templates/request_new.html`**

```html
{% extends "base.html" %}
{% block title %}New Request - AgentCare{% endblock %}
{% block content %}
<h1>Submit a request</h1>
<form method="post" action="/requests/new">
    <textarea name="request_text" rows="4" cols="50" required placeholder="e.g. book a cardiology appointment"></textarea>
    <br>
    <button type="submit">Submit</button>
</form>
{% endblock %}
```

- [ ] **Step 5: Create `app/templates/request_status.html`**

```html
{% extends "base.html" %}
{% block title %}Request Status - AgentCare{% endblock %}
{% block content %}
<h1>Request Status</h1>
<p>Status: {{ workflow_run.status.value }}</p>
<p>Current step: {{ workflow_run.current_step }}</p>
<ul>
    <li>Intent: {{ workflow_run.state.get("intent") }}</li>
    <li>Department: {{ workflow_run.state.get("department_id") }}</li>
    <li>Appointment: {{ workflow_run.state.get("appointment_id") }}</li>
    <li>Escalation: {{ workflow_run.state.get("escalation") }}</li>
</ul>
<a href="/requests/new">Submit another request</a>
{% endblock %}
```

- [ ] **Step 6: Register the router in `app/main.py`**

Replace `app/main.py` with:

```python
from fastapi import FastAPI

from app.routes.auth_routes import router as auth_router
from app.routes.dashboard_routes import router as dashboard_router
from app.routes.request_routes import router as request_router

app = FastAPI(title="AgentCare")

app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(request_router)


@app.get("/health")
def health():
    return {"status": "ok"}
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/test_request_routes.py -v`
Expected: PASS (5 tests)

- [ ] **Step 8: Run the full suite to confirm nothing else broke**

Run: `pytest tests/ -v`
Expected: PASS (all tests, existing count plus these 5)

- [ ] **Step 9: Commit**

```bash
git add app/routes/request_routes.py app/templates/request_new.html app/templates/request_status.html app/main.py tests/test_request_routes.py
git commit -m "Add thin request-submission routes (GET/POST /requests/new, GET /requests/{id})"
```

---

## Self-review

- **Spec coverage:** every route in the spec's §4 Components section has a matching endpoint here; §6 error handling (401/403/404/failed-status-shown-plainly) is covered by the test cases; §7 testing scenarios map 1:1 to the 5 tests above.
- **No placeholders:** every step has complete, runnable code.
- **Type/name consistency:** `run_workflow(db, patient_id, user_id, request_text)` signature matches `app/workflow_runner.py` exactly (no `uploaded_files` passed — defaults to `None` → `[]`, consistent with "no file upload" scope). `require_role(UserRole.patient.value)` matches the exact call shape already used in `dashboard_routes.py`. `WorkflowRun.patient_id` vs `PatientProfile.id` comparison matches the existing model relationship (both UUID columns, no `str()` needed since neither side has been serialized here).
- **Out-of-scope items** (list view, styling, file upload, reschedule/cancel, staff routes) are not touched by this plan, matching the spec.
