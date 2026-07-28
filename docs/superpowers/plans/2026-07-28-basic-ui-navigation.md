# Basic UI Navigation & Dashboard Connectivity Implementation Plan

> **For agentic workers:** Implement this plan task-by-task following TDD. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect all disjointed pages in the application so a patient or staff member can navigate seamlessly in a browser, track past requests and uploaded document statuses, and submit new requests without needing to type direct URLs.

**Architecture:** Extend `app/routes/dashboard_routes.py` to query `WorkflowRun` rows for patient and staff dashboards, update `app/templates/base.html` with a role-aware navigation header, update `app/templates/dashboard.html` with request history, update `app/templates/staff_dashboard.html` with escalation list, and add return navigation links to `app/templates/request_status.html`.

**Tech Stack:** FastAPI + Jinja2, SQLAlchemy ORM, pytest with DB fixtures.

---

### Task 1: Patient Dashboard Route & Template (Request History & New Request Link)

**Files:**
- Modify: `app/routes/dashboard_routes.py`
- Modify: `app/templates/dashboard.html`
- Create/Modify: `tests/test_dashboard_routes.py`

**Interfaces:**
- `patient_dashboard` route in `app/routes/dashboard_routes.py` queries `WorkflowRun` for patient's `profile.id` ordered by `created_at.desc()`.
- Passes `workflow_runs` to `dashboard.html`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_dashboard_routes.py`:

```python
import uuid
from fastapi.testclient import TestClient

from app.auth import create_session_token
from app.main import app
from app.models import WorkflowStatus
from app.rbac import SESSION_COOKIE_NAME
from tests.fakes import make_patient_profile, make_user, make_workflow_run

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
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/test_dashboard_routes.py -v`
Expected: FAIL (text not in response because `dashboard_routes.py` doesn't pass `workflow_runs`).

- [ ] **Step 3: Update `app/routes/dashboard_routes.py`**

```python
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import PatientProfile, User, UserRole, WorkflowRun
from app.rbac import require_role

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _get_or_create_profile(db: Session, user: User) -> PatientProfile:
    profile = db.query(PatientProfile).filter(PatientProfile.user_id == user.id).first()
    if profile is None:
        profile = PatientProfile(user_id=user.id)
        db.add(profile)
        db.commit()
    return profile


@router.get("/dashboard", response_class=HTMLResponse)
def patient_dashboard(
    request: Request,
    user: User = Depends(require_role(UserRole.patient.value)),
    db: Session = Depends(get_db),
):
    profile = _get_or_create_profile(db, user)
    workflow_runs = (
        db.query(WorkflowRun)
        .filter(WorkflowRun.patient_id == profile.id)
        .order_by(WorkflowRun.created_at.desc())
        .all()
    )
    return templates.TemplateResponse(
        request, "dashboard.html", {"user": user, "workflow_runs": workflow_runs}
    )
```

- [ ] **Step 4: Update `app/templates/dashboard.html`**

```html
{% extends "base.html" %}
{% block title %}Dashboard - AgentCare{% endblock %}
{% block content %}
<div style="display: flex; justify-content: space-between; align-items: center;">
    <h1>Welcome, {{ user.name }}</h1>
    <a href="/requests/new" style="padding: 8px 16px; background-color: #0066cc; color: white; text-decoration: none; border-radius: 4px;">+ Submit New Request</a>
</div>

<h2>My Requests</h2>

{% if workflow_runs %}
<table border="1" cellpadding="8" cellspacing="0" style="width: 100%; border-collapse: collapse;">
    <thead>
        <tr style="background-color: #f2f2f2;">
            <th>Date</th>
            <th>Request</th>
            <th>Documents Attached</th>
            <th>Status</th>
            <th>Action</th>
        </tr>
    </thead>
    <tbody>
        {% for run in workflow_runs %}
        <tr>
            <td>{{ run.created_at.strftime('%Y-%m-%d %H:%M') if run.created_at else 'Recent' }}</td>
            <td>{{ run.state.get('request_text', 'No text') }}</td>
            <td>{{ run.state.get('document_ids', []) | length }} file(s)</td>
            <td><strong>{{ run.status.value }}</strong></td>
            <td><a href="/requests/{{ run.id }}">View Details</a></td>
        </tr>
        {% endfor %}
    </tbody>
</table>
{% else %}
<p>You haven't submitted any healthcare requests yet.</p>
<p><a href="/requests/new">Click here to submit your first request</a></p>
{% endif %}
{% endblock %}
```

- [ ] **Step 5: Run tests to verify pass**

Run: `pytest tests/test_dashboard_routes.py -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add app/routes/dashboard_routes.py app/templates/dashboard.html tests/test_dashboard_routes.py
git commit -m "feat: add patient request history and submission links to patient dashboard"
```

---

### Task 2: Global Navigation Header in `base.html` & Return Links

**Files:**
- Modify: `app/templates/base.html`
- Modify: `app/templates/request_status.html`
- Modify: `tests/test_dashboard_routes.py`

- [ ] **Step 1: Write test for header navigation bar**

Add to `tests/test_dashboard_routes.py`:

```python
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
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/test_dashboard_routes.py::test_navigation_bar_links_for_logged_in_patient -v`
Expected: FAIL.

- [ ] **Step 3: Update `app/templates/base.html`**

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{% block title %}AgentCare{% endblock %}</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; margin: 0; padding: 0; background: #f8f9fa; color: #333; }
        header { background: #1a365d; color: white; padding: 12px 24px; display: flex; justify-content: space-between; align-items: center; }
        header a { color: white; text-decoration: none; margin-right: 16px; font-weight: 500; }
        header a:hover { text-decoration: underline; }
        main { max-width: 960px; margin: 24px auto; padding: 24px; background: white; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        .nav-links { display: flex; align-items: center; }
        .nav-links form { margin: 0; }
        .nav-links button { background: transparent; border: 1px solid white; color: white; padding: 4px 12px; border-radius: 4px; cursor: pointer; font-size: 14px; }
        .nav-links button:hover { background: rgba(255,255,255,0.2); }
    </style>
</head>
<body>
    <header>
        <div>
            <a href="/" style="font-size: 20px; font-weight: bold;">🏥 AgentCare</a>
        </div>
        <div class="nav-links">
            {% if user %}
                {% if user.role.value == 'patient' %}
                    <a href="/dashboard">Dashboard</a>
                    <a href="/requests/new">+ New Request</a>
                {% elif user.role.value == 'staff' %}
                    <a href="/staff/dashboard">Staff Dashboard</a>
                {% endif %}
                <form method="post" action="/logout" style="display:inline;">
                    <button type="submit">Log out</button>
                </form>
            {% else %}
                <a href="/login">Login</a>
                <a href="/register">Register</a>
            {% endif %}
        </div>
    </header>
    <main>
        {% block content %}{% endblock %}
    </main>
</body>
</html>
```

- [ ] **Step 4: Update `app/templates/request_status.html` with return links**

```html
{% extends "base.html" %}
{% block title %}Request Status - AgentCare{% endblock %}
{% block content %}
<p><a href="/dashboard">← Back to Dashboard</a></p>
<h1>Request Status</h1>
<p>Status: {{ workflow_run.status.value }}</p>
<p>Current step: {{ workflow_run.current_step }}</p>
<ul>
    <li>Intent: {{ workflow_run.state.get("intent") }}</li>
    <li>Department: {{ workflow_run.state.get("department_id") }}</li>
    <li>Appointment: {{ workflow_run.state.get("appointment_id") }}</li>
    <li>Documents: {{ workflow_run.state.get("document_ids") }}</li>
    <li>Escalation: {{ workflow_run.state.get("escalation") }}</li>
</ul>
<div style="margin-top: 20px;">
    <a href="/requests/new" style="margin-right: 15px;">Submit another request</a>
    <a href="/dashboard">Return to Dashboard</a>
</div>
{% endblock %}
```

- [ ] **Step 5: Run tests to verify pass**

Run: `pytest tests/test_dashboard_routes.py -v`
Expected: PASS (all 3 tests).

- [ ] **Step 6: Commit**

```bash
git add app/templates/base.html app/templates/request_status.html tests/test_dashboard_routes.py
git commit -m "feat: add global navigation bar and status return links"
```

---

### Task 3: Staff Dashboard Escalation List

**Files:**
- Modify: `app/routes/dashboard_routes.py`
- Modify: `app/templates/staff_dashboard.html`
- Modify: `tests/test_dashboard_routes.py`

- [ ] **Step 1: Write test for Staff Dashboard**

Add to `tests/test_dashboard_routes.py`:

```python
def test_staff_dashboard_lists_escalated_runs(db_session):
    staff_user = make_user(db_session, email="staff@hospital.org", role="staff")
    patient_user = make_user(db_session, email="patient@gmail.com", name="John Doe")
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
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/test_dashboard_routes.py::test_staff_dashboard_lists_escalated_runs -v`
Expected: FAIL.

- [ ] **Step 3: Update `app/routes/dashboard_routes.py` for Staff Dashboard**

```python
@router.get("/staff/dashboard", response_class=HTMLResponse)
def staff_dashboard(
    request: Request,
    user: User = Depends(require_role(UserRole.staff.value)),
    db: Session = Depends(get_db),
):
    escalated_runs = (
        db.query(WorkflowRun)
        .filter(WorkflowRun.status == WorkflowStatus.needs_review)
        .order_by(WorkflowRun.created_at.desc())
        .all()
    )
    return templates.TemplateResponse(
        request, "staff_dashboard.html", {"user": user, "escalated_runs": escalated_runs}
    )
```

- [ ] **Step 4: Update `app/templates/staff_dashboard.html`**

```html
{% extends "base.html" %}
{% block title %}Staff Dashboard - AgentCare{% endblock %}
{% block content %}
<h1>Staff Dashboard — {{ user.name }}</h1>
<p>Role: {{ user.role.value }}</p>

<h2>Requests Needing Staff Review</h2>

{% if escalated_runs %}
<table border="1" cellpadding="8" cellspacing="0" style="width: 100%; border-collapse: collapse;">
    <thead>
        <tr style="background-color: #ffebee;">
            <th>Date</th>
            <th>Workflow ID</th>
            <th>Request Text</th>
            <th>Escalation Reason</th>
            <th>Status</th>
        </tr>
    </thead>
    <tbody>
        {% for run in escalated_runs %}
        <tr>
            <td>{{ run.created_at.strftime('%Y-%m-%d %H:%M') if run.created_at else 'Recent' }}</td>
            <td><code>{{ run.id }}</code></td>
            <td>{{ run.state.get('request_text', 'N/A') }}</td>
            <td><strong style="color: #c62828;">{{ run.state.get('escalation', {}).get('reason', 'Review required') }}</strong></td>
            <td>{{ run.status.value }}</td>
        </tr>
        {% endfor %}
    </tbody>
</table>
{% else %}
<p>No requests currently require staff review.</p>
{% endif %}
{% endblock %}
```

- [ ] **Step 5: Run all tests in test suite**

Run: `pytest -v`
Expected: PASS (all tests pass).

- [ ] **Step 6: Commit**

```bash
git add app/routes/dashboard_routes.py app/templates/staff_dashboard.html tests/test_dashboard_routes.py
git commit -m "feat: add staff review list to staff dashboard"
```
