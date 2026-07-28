# My Appointments (Patient) + Staff Appointment Walkthrough Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a patient-facing "My Appointments" page with direct Cancel/Reschedule buttons, and a staff-facing read-only "Appointment Schedule" page grouped by doctor.

**Architecture:** Both features are additive — new routes in the existing `app/routes/dashboard_routes.py`, new templates, one new query helper per feature in `app/tools/appointment_tools.py`. The patient side does **not** reimplement cancel/reschedule logic: it seeds a `WorkflowRun` directly at the `needs_appointment_selection` status and calls the already-shipped, already-tested `continue_as_appointment_action` function (in `app/workflow_runner.py`, committed in `3b9edca`, spec at `docs/superpowers/specs/2026-07-28-cancel-reschedule-design.md`). Read that function before starting Task 1 — do not write new cancel/reschedule business logic anywhere in this plan.

**Tech Stack:** FastAPI, Jinja2 templates, SQLAlchemy, pytest. No LLM, no LangGraph, no ToolNode involvement anywhere in this plan — everything here is plain routes and plain DB queries.

## Global Constraints

- No in-memory state — every read comes from a real DB query (`CLAUDE.md`: "Persistent SQL only").
- RBAC enforced in route dependencies (`require_role`), never just hidden in templates (`CLAUDE.md`).
- No tool/route may return a fixed response regardless of input — every list here is a real query result.
- Patient routes must verify appointment ownership (`appointment.patient_id == profile.id`) before acting — 403 otherwise, matching every existing ownership check in `app/routes/request_routes.py`.
- Reuse `continue_as_appointment_action` (already exists in `app/workflow_runner.py`) unchanged. Do not modify its signature or behavior.
- Reuse `list_patient_appointments` (already exists in `app/tools/appointment_tools.py`) unchanged for the patient page.

---

## Task 1: `start_appointment_action` helper

**Files:**
- Modify: `app/workflow_runner.py` (add new function; existing imports at top already include `uuid`, `WorkflowRun`, `WorkflowStatus` from `app.models`, and `continue_as_appointment_action` is already defined lower in this same file)
- Test: `tests/test_workflow_runner.py`

**Interfaces:**
- Consumes: `continue_as_appointment_action(db, workflow_run: WorkflowRun, appointment_id: str) -> WorkflowRun` (already exists at the bottom of `app/workflow_runner.py` — do not modify it).
- Produces: `start_appointment_action(db, patient_id: str, action: str, appointment_id: str) -> WorkflowRun`, used by Task 3's routes. `action` is either `"cancel"` or `"reschedule"`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_workflow_runner.py` (this file already imports `make_appointment`, `make_appointment_slot`, `make_department`, `make_doctor`, `make_patient_profile` from `tests.fakes`, and `WorkflowStatus` from `app.models` — check the existing top-of-file imports and reuse them):

```python
def test_start_appointment_action_cancel_reaches_completed(db_session):
    from app.workflow_runner import start_appointment_action

    profile = make_patient_profile(db_session)
    doctor = make_doctor(db_session)
    slot = make_appointment_slot(db_session, doctor=doctor)
    appointment = make_appointment(db_session, patient=profile, doctor=doctor, slot=slot)

    workflow_run = start_appointment_action(db_session, str(profile.id), "cancel", str(appointment.id))

    assert workflow_run.status == WorkflowStatus.completed
    assert workflow_run.state["pending_appointment_action"] == "cancel"
    db_session.refresh(appointment)
    assert appointment.status.value == "cancelled"


def test_start_appointment_action_reschedule_reaches_slot_selection(db_session):
    from app.workflow_runner import start_appointment_action

    profile = make_patient_profile(db_session)
    department = make_department(db_session)
    doctor = make_doctor(db_session, department=department)
    slot = make_appointment_slot(db_session, doctor=doctor)
    make_appointment_slot(db_session, doctor=doctor)  # a second open slot to reschedule into
    appointment = make_appointment(db_session, patient=profile, doctor=doctor, slot=slot)

    workflow_run = start_appointment_action(db_session, str(profile.id), "reschedule", str(appointment.id))

    assert workflow_run.status == WorkflowStatus.needs_slot_selection
    assert workflow_run.state["rescheduling_appointment_id"] == str(appointment.id)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_workflow_runner.py::test_start_appointment_action_cancel_reaches_completed -v`
Expected: FAIL with `ImportError: cannot import name 'start_appointment_action'`

- [ ] **Step 3: Write minimal implementation**

Add to `app/workflow_runner.py`, anywhere below `_compiled_graph = build_graph()` (e.g. directly above `continue_as_appointment_action`, since it's the function this delegates to):

```python
def start_appointment_action(db, patient_id: str, action: str, appointment_id: str) -> WorkflowRun:
    """Entry point from the My Appointments page - the patient already
    picked both the action (Cancel/Reschedule button) and the target
    appointment (which row they clicked) with zero ambiguity, so this skips
    Safety/Coordinator/the graph entirely (there is no free text to
    classify) and seeds a WorkflowRun directly at needs_appointment_selection
    - the same state run_workflow lands on after a typed "cancel my
    appointment" request. continue_as_appointment_action (unchanged) takes
    it from there; this function's only job is constructing that starting
    state."""
    workflow_run = WorkflowRun(
        patient_id=uuid.UUID(patient_id),
        current_step="needs_appointment_selection",
        state={},
        status=WorkflowStatus.needs_appointment_selection,
    )
    db.add(workflow_run)
    db.commit()

    full_state = {
        "workflow_run_id": str(workflow_run.id),
        "patient_id": patient_id,
        "user_id": None,
        "request_text": f"[My Appointments page] {action} appointment",
        "uploaded_files": [],
        "intent": f"{action}_appointment",
        "department_id": None,
        "appointment_id": None,
        "document_ids": [],
        "missing_document_types": [],
        "reminder_ids": [],
        "escalation": None,
        "status": WorkflowStatus.needs_appointment_selection.value,
        "needs_clarification": False,
        "needs_appointment_reason": False,
        "needs_appointment_selection": True,
        "pending_appointment_action": action,
        "rescheduling_appointment_id": None,
    }
    workflow_run.state = full_state
    db.commit()

    return continue_as_appointment_action(db, workflow_run, appointment_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_workflow_runner.py::test_start_appointment_action_cancel_reaches_completed tests/test_workflow_runner.py::test_start_appointment_action_reschedule_reaches_slot_selection -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add app/workflow_runner.py tests/test_workflow_runner.py
git commit -m "feat: start_appointment_action entry point for direct cancel/reschedule"
```

---

## Task 2: `list_active_appointments_grouped_by_doctor` helper

**Files:**
- Modify: `app/tools/appointment_tools.py:11` (import line — add `PatientProfile`, `User`) and add new function near `list_patient_appointments`
- Test: `tests/test_appointment_tools.py`

**Interfaces:**
- Consumes: `Appointment`, `AppointmentSlot`, `AppointmentStatus`, `Doctor`, `Department` (already imported in this file), plus `PatientProfile`, `User` (need to add to the import line at `app/tools/appointment_tools.py:11`, currently `from app.models import Appointment, AppointmentSlot, AppointmentStatus, Department, Doctor, SlotStatus`).
- Produces: `list_active_appointments_grouped_by_doctor(db) -> list[dict]`, used by Task 3's staff route. Each dict: `{"doctor_name": str, "department_name": str, "appointments": [{"patient_name": str, "formatted_time": str, "status": str}, ...]}`, sorted by `doctor_name`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_appointment_tools.py` (this file already imports `make_appointment`, `make_appointment_slot`, `make_department`, `make_doctor`, `make_patient_profile` from `tests.fakes` — check the top of the file; if `make_user` isn't imported, add it, since a real `User` row with a `name` is needed for `patient_name` to resolve):

```python
def test_list_active_appointments_grouped_by_doctor_groups_correctly(db_session):
    from app.tools.appointment_tools import list_active_appointments_grouped_by_doctor
    from tests.fakes import make_user

    department = make_department(db_session)
    doctor = make_doctor(db_session, department=department)

    user1 = make_user(db_session)
    user1.name = "Alice Patient"
    profile1 = make_patient_profile(db_session, user=user1)
    user2 = make_user(db_session)
    user2.name = "Bob Patient"
    profile2 = make_patient_profile(db_session, user=user2)
    db_session.commit()

    slot1 = make_appointment_slot(db_session, doctor=doctor)
    slot2 = make_appointment_slot(db_session, doctor=doctor)
    slot3 = make_appointment_slot(db_session, doctor=doctor)

    make_appointment(db_session, patient=profile1, doctor=doctor, slot=slot1, status=AppointmentStatus.confirmed)
    make_appointment(db_session, patient=profile2, doctor=doctor, slot=slot2, status=AppointmentStatus.rescheduled)
    make_appointment(db_session, patient=profile1, doctor=doctor, slot=slot3, status=AppointmentStatus.cancelled)

    grouped = list_active_appointments_grouped_by_doctor(db_session)

    assert len(grouped) == 1
    assert grouped[0]["doctor_name"] == doctor.name
    assert grouped[0]["department_name"] == department.name
    assert len(grouped[0]["appointments"]) == 2
    patient_names = {a["patient_name"] for a in grouped[0]["appointments"]}
    assert patient_names == {"Alice Patient", "Bob Patient"}


def test_list_active_appointments_grouped_by_doctor_empty_when_none(db_session):
    from app.tools.appointment_tools import list_active_appointments_grouped_by_doctor

    grouped = list_active_appointments_grouped_by_doctor(db_session)

    assert grouped == []
```

Note: `AppointmentStatus` must already be imported in this test file (check `tests/test_appointment_tools.py` top-of-file imports — it was added in the cancel/reschedule work; if missing, add `from app.models import AppointmentStatus` or extend the existing `from app.models import ...` line).

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_appointment_tools.py::test_list_active_appointments_grouped_by_doctor_groups_correctly -v`
Expected: FAIL with `ImportError: cannot import name 'list_active_appointments_grouped_by_doctor'`

- [ ] **Step 3: Write minimal implementation**

First, update the import line at `app/tools/appointment_tools.py:11`:

```python
from app.models import Appointment, AppointmentSlot, AppointmentStatus, Department, Doctor, PatientProfile, SlotStatus, User
```

Then add this function anywhere below `list_patient_appointments` (near line 67, right before `check_slot_availability`):

```python
def list_active_appointments_grouped_by_doctor(db: Session) -> list[dict]:
    """Read-only staff view: every active appointment (pending, confirmed,
    rescheduled - same status filter as list_patient_appointments), grouped
    by doctor. Plain query + Python grouping, not an agentic tool - staff
    are looking, not acting."""
    rows = (
        db.query(Appointment, AppointmentSlot, Doctor, Department, PatientProfile, User)
        .join(AppointmentSlot, Appointment.slot_id == AppointmentSlot.id)
        .join(Doctor, Appointment.doctor_id == Doctor.id)
        .join(Department, Doctor.department_id == Department.id)
        .join(PatientProfile, Appointment.patient_id == PatientProfile.id)
        .join(User, PatientProfile.user_id == User.id)
        .filter(
            Appointment.status.in_(
                [AppointmentStatus.pending, AppointmentStatus.confirmed, AppointmentStatus.rescheduled]
            )
        )
        .order_by(Doctor.name, AppointmentSlot.start_time)
        .all()
    )

    grouped: dict[str, dict] = {}
    for appointment, slot, doctor, department, profile, user in rows:
        key = str(doctor.id)
        if key not in grouped:
            grouped[key] = {"doctor_name": doctor.name, "department_name": department.name, "appointments": []}
        grouped[key]["appointments"].append(
            {
                "patient_name": user.name,
                "formatted_time": slot.start_time.strftime("%B %d, %Y at %I:%M %p"),
                "status": appointment.status.value,
            }
        )
    return sorted(grouped.values(), key=lambda d: d["doctor_name"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_appointment_tools.py::test_list_active_appointments_grouped_by_doctor_groups_correctly tests/test_appointment_tools.py::test_list_active_appointments_grouped_by_doctor_empty_when_none -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add app/tools/appointment_tools.py tests/test_appointment_tools.py
git commit -m "feat: list_active_appointments_grouped_by_doctor query helper"
```

---

## Task 3: Patient routes — `GET /appointments`, `POST /appointments/{id}/cancel`, `POST /appointments/{id}/reschedule`

**Files:**
- Modify: `app/routes/dashboard_routes.py` (add imports and three new routes)
- Test: `tests/test_dashboard_routes.py`

**Interfaces:**
- Consumes: `list_patient_appointments(db, patient_id: str) -> list[dict]` (already exists in `app/tools/appointment_tools.py`), `start_appointment_action(db, patient_id, action, appointment_id) -> WorkflowRun` (Task 1).
- Produces: routes `GET /appointments`, `POST /appointments/{appointment_id}/cancel`, `POST /appointments/{appointment_id}/reschedule`, all `require_role(UserRole.patient.value)`.

Current top of `app/routes/dashboard_routes.py`:
```python
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import PatientProfile, User, UserRole, WorkflowRun, WorkflowStatus
from app.rbac import require_role
from app.tools.appointment_tools import appointment_display_details
```

- [ ] **Step 1: Write the failing test**

Create `tests/test_dashboard_routes.py`'s new tests (append to the existing file — it already has `client`, `_register_patient`/`_register_staff` helpers or equivalent login helpers used by `test_patient_dashboard_shows_real_appointment_details_not_just_status`; check that test for the exact registration/login pattern used in this file and reuse it verbatim):

```python
def test_my_appointments_page_lists_real_appointments(db_session):
    cookie = _register_patient("Appointments Page Patient")
    client.cookies.set("agentcare_session", cookie)

    resp = client.get("/dashboard")  # forces profile creation via _get_or_create_profile
    user = db_session.query(User).filter(User.name == "Appointments Page Patient").first()
    profile = db_session.query(PatientProfile).filter(PatientProfile.user_id == user.id).first()

    doctor = make_doctor(db_session)
    slot = make_appointment_slot(db_session, doctor=doctor)
    make_appointment(db_session, patient=profile, doctor=doctor, slot=slot)

    resp = client.get("/appointments")
    assert resp.status_code == 200
    assert doctor.name in resp.text


def test_cancel_from_appointments_page_redirects_to_request_status(db_session):
    cookie = _register_patient("Cancel Page Patient")
    client.cookies.set("agentcare_session", cookie)
    client.get("/dashboard")
    user = db_session.query(User).filter(User.name == "Cancel Page Patient").first()
    profile = db_session.query(PatientProfile).filter(PatientProfile.user_id == user.id).first()

    doctor = make_doctor(db_session)
    slot = make_appointment_slot(db_session, doctor=doctor)
    appointment = make_appointment(db_session, patient=profile, doctor=doctor, slot=slot)

    resp = client.post(f"/appointments/{appointment.id}/cancel", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/requests/")

    db_session.expire_all()
    refreshed = db_session.get(Appointment, appointment.id)
    assert refreshed.status.value == "cancelled"


def test_cancel_someone_elses_appointment_returns_403(db_session):
    doctor = make_doctor(db_session)
    slot = make_appointment_slot(db_session, doctor=doctor)
    other_profile = make_patient_profile(db_session)
    appointment = make_appointment(db_session, patient=other_profile, doctor=doctor, slot=slot)

    cookie = _register_patient("Not The Owner")
    client.cookies.set("agentcare_session", cookie)

    resp = client.post(f"/appointments/{appointment.id}/cancel")
    assert resp.status_code == 403


def test_cancel_nonexistent_appointment_returns_404():
    import uuid as uuid_module
    cookie = _register_patient("404 Test Patient")
    client.cookies.set("agentcare_session", cookie)

    resp = client.post(f"/appointments/{uuid_module.uuid4()}/cancel")
    assert resp.status_code == 404
```

Check `tests/test_dashboard_routes.py`'s existing imports before writing this — it must already import `Appointment` from `app.models` and `make_appointment`, `make_appointment_slot`, `make_doctor`, `make_patient_profile` from `tests.fakes` (added for the earlier `test_patient_dashboard_shows_real_appointment_details_not_just_status` test). Add any that are missing.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_dashboard_routes.py::test_my_appointments_page_lists_real_appointments -v`
Expected: FAIL with 404 (route doesn't exist yet)

- [ ] **Step 3: Write minimal implementation**

Update the import block at the top of `app/routes/dashboard_routes.py`:

```python
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette import status

from app.db import get_db
from app.models import Appointment, PatientProfile, User, UserRole, WorkflowRun, WorkflowStatus
from app.rbac import require_role
from app.tools.appointment_tools import (
    appointment_display_details,
    list_active_appointments_grouped_by_doctor,
    list_patient_appointments,
)
from app.workflow_runner import start_appointment_action
```

Add these routes to `app/routes/dashboard_routes.py` (after `patient_dashboard`, before `staff_dashboard`):

```python
@router.get("/appointments", response_class=HTMLResponse)
def my_appointments(
    request: Request,
    user: User = Depends(require_role(UserRole.patient.value)),
    db: Session = Depends(get_db),
):
    profile = _get_or_create_profile(db, user)
    appointments = list_patient_appointments(db, str(profile.id))
    return templates.TemplateResponse(
        request, "my_appointments.html", {"user": user, "appointments": appointments}
    )


def _owned_active_appointment_or_404(db: Session, profile: PatientProfile, appointment_id: str) -> Appointment:
    appointment = db.query(Appointment).filter(Appointment.id == uuid.UUID(appointment_id)).first()
    if appointment is None:
        raise HTTPException(status_code=404, detail="Not found")
    if appointment.patient_id != profile.id:
        raise HTTPException(status_code=403, detail="Not your appointment")
    return appointment


@router.post("/appointments/{appointment_id}/cancel")
def cancel_appointment_from_list(
    appointment_id: str,
    user: User = Depends(require_role(UserRole.patient.value)),
    db: Session = Depends(get_db),
):
    profile = _get_or_create_profile(db, user)
    _owned_active_appointment_or_404(db, profile, appointment_id)
    workflow_run = start_appointment_action(db, str(profile.id), "cancel", appointment_id)
    return RedirectResponse(f"/requests/{workflow_run.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/appointments/{appointment_id}/reschedule")
def reschedule_appointment_from_list(
    appointment_id: str,
    user: User = Depends(require_role(UserRole.patient.value)),
    db: Session = Depends(get_db),
):
    profile = _get_or_create_profile(db, user)
    _owned_active_appointment_or_404(db, profile, appointment_id)
    workflow_run = start_appointment_action(db, str(profile.id), "reschedule", appointment_id)
    return RedirectResponse(f"/requests/{workflow_run.id}", status_code=status.HTTP_303_SEE_OTHER)
```

Note: if `status` (from `starlette` or `fastapi`) is already imported under a different name elsewhere in this file, use the existing import instead of adding a duplicate — check the current file first.

Create `app/templates/my_appointments.html`:

```html
{% extends "base.html" %}
{% block title %}My Appointments - AgentCare{% endblock %}
{% block content %}
<h1 style="margin-top: 0;">My Appointments</h1>
{% if appointments %}
<div style="display: flex; flex-direction: column; gap: 12px; margin-top: 16px;">
    {% for appt in appointments %}
    <div style="border: 1px solid #ddd; border-radius: 6px; padding: 16px; display: flex; justify-content: space-between; align-items: center;">
        <div>
            <strong>{{ appt.doctor_name }}</strong> — {{ appt.department_name }}<br>
            <span style="color: #555; font-size: 14px;">{{ appt.formatted_time }}</span>
        </div>
        <div style="display: flex; gap: 8px;">
            <form method="post" action="/appointments/{{ appt.appointment_id }}/reschedule">
                <button type="submit" style="padding: 8px 14px; background-color: #0066cc; color: white; border: none; border-radius: 4px; cursor: pointer;">Reschedule</button>
            </form>
            <form method="post" action="/appointments/{{ appt.appointment_id }}/cancel">
                <button type="submit" style="padding: 8px 14px; background-color: #c62828; color: white; border: none; border-radius: 4px; cursor: pointer;">Cancel</button>
            </form>
        </div>
    </div>
    {% endfor %}
</div>
{% else %}
<div style="padding: 20px; background-color: #f8f9fa; border-radius: 6px; text-align: center;">
    <p style="margin: 0;">You have no upcoming appointments.</p>
</div>
{% endif %}
{% endblock %}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_dashboard_routes.py -v`
Expected: PASS (all tests in the file, including the 4 new ones)

- [ ] **Step 5: Commit**

```bash
git add app/routes/dashboard_routes.py app/templates/my_appointments.html tests/test_dashboard_routes.py
git commit -m "feat: My Appointments page with direct cancel/reschedule buttons"
```

---

## Task 4: Staff route — `GET /staff/appointments`

**Files:**
- Modify: `app/routes/dashboard_routes.py` (add one route; imports already added in Task 3)
- Create: `app/templates/staff_appointments.html`
- Test: `tests/test_dashboard_routes.py`

**Interfaces:**
- Consumes: `list_active_appointments_grouped_by_doctor(db) -> list[dict]` (Task 2).
- Produces: route `GET /staff/appointments`, `require_role(UserRole.staff.value)`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_dashboard_routes.py`:

```python
def test_staff_appointments_page_shows_grouped_schedule(db_session):
    department = make_department(db_session)
    doctor = make_doctor(db_session, department=department)
    profile = make_patient_profile(db_session)
    slot = make_appointment_slot(db_session, doctor=doctor)
    make_appointment(db_session, patient=profile, doctor=doctor, slot=slot)

    cookie = _register_staff("Staff Viewer")
    client.cookies.set("agentcare_session", cookie)

    resp = client.get("/staff/appointments")
    assert resp.status_code == 200
    assert doctor.name in resp.text


def test_staff_appointments_page_rejects_patients(db_session):
    cookie = _register_patient("Not Staff")
    client.cookies.set("agentcare_session", cookie)

    resp = client.get("/staff/appointments")
    assert resp.status_code == 403
```

Check this test file for its existing `_register_staff` helper (used by other staff-dashboard tests in this same file) and reuse it exactly as-is.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_dashboard_routes.py::test_staff_appointments_page_shows_grouped_schedule -v`
Expected: FAIL with 404 (route doesn't exist yet)

- [ ] **Step 3: Write minimal implementation**

Add to `app/routes/dashboard_routes.py`, after `staff_dashboard`:

```python
@router.get("/staff/appointments", response_class=HTMLResponse)
def staff_appointments(
    request: Request,
    user: User = Depends(require_role(UserRole.staff.value)),
    db: Session = Depends(get_db),
):
    grouped = list_active_appointments_grouped_by_doctor(db)
    return templates.TemplateResponse(
        request, "staff_appointments.html", {"user": user, "grouped": grouped}
    )
```

Create `app/templates/staff_appointments.html`:

```html
{% extends "base.html" %}
{% block title %}Appointments - AgentCare{% endblock %}
{% block content %}
<h1 style="margin-top: 0;">Appointment Schedule</h1>
{% if grouped %}
{% for doc in grouped %}
<div style="margin-top: 20px;">
    <h2 style="margin-bottom: 4px;">{{ doc.doctor_name }} <span style="font-weight: normal; color: #666; font-size: 15px;">— {{ doc.department_name }}</span></h2>
    <table border="1" cellpadding="8" cellspacing="0" style="width: 100%; border-collapse: collapse;">
        <thead>
            <tr style="background-color: #f2f2f2;">
                <th style="text-align: left;">Patient</th>
                <th style="text-align: left;">Time</th>
                <th style="text-align: left;">Status</th>
            </tr>
        </thead>
        <tbody>
            {% for appt in doc.appointments %}
            <tr>
                <td>{{ appt.patient_name }}</td>
                <td>{{ appt.formatted_time }}</td>
                <td>{{ appt.status }}</td>
            </tr>
            {% endfor %}
        </tbody>
    </table>
</div>
{% endfor %}
{% else %}
<div style="padding: 20px; background-color: #f8f9fa; border-radius: 6px;">
    <p style="margin: 0;">No active appointments scheduled.</p>
</div>
{% endif %}
{% endblock %}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_dashboard_routes.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add app/routes/dashboard_routes.py app/templates/staff_appointments.html tests/test_dashboard_routes.py
git commit -m "feat: staff appointment schedule view grouped by doctor"
```

---

## Task 5: Nav links in `base.html`

**Files:**
- Modify: `app/templates/base.html:26-31`

**Interfaces:**
- Consumes: nothing new — pure template edit.
- Produces: nothing consumed by later tasks — this is the final task.

- [ ] **Step 1: Write the failing test**

There is no unit test for template nav links in this codebase's existing test suite (verified: `tests/test_dashboard_routes.py` and `tests/test_request_routes.py` assert on response body text for specific data, not nav chrome). Skip the TDD test/fail/pass cycle for this step and verify manually instead — this matches the ponytail rule that trivial changes don't need a test. Write a one-line manual check instead:

Run: `python -c "print('manual check: start the app, log in as patient, confirm /appointments link and staff /staff/appointments link both appear and work')"`

- [ ] **Step 2: Make the template edit**

Current `app/templates/base.html:24-39`:
```html
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
```

Replace with:
```html
        <div class="nav-links">
            {% if user %}
                {% if user.role.value == 'patient' %}
                    <a href="/dashboard">Dashboard</a>
                    <a href="/appointments">My Appointments</a>
                    <a href="/requests/new">+ New Request</a>
                {% elif user.role.value == 'staff' %}
                    <a href="/staff/dashboard">Staff Dashboard</a>
                    <a href="/staff/appointments" target="_blank">Appointments</a>
                {% endif %}
                <form method="post" action="/logout" style="display:inline;">
                    <button type="submit">Log out</button>
                </form>
            {% else %}
                <a href="/login">Login</a>
                <a href="/register">Register</a>
            {% endif %}
        </div>
```

- [ ] **Step 3: Run the full test suite to confirm nothing broke**

Run: `python -m pytest -q`
Expected: all tests pass (same count as before this task, plus everything added in Tasks 1-4)

- [ ] **Step 4: Commit**

```bash
git add app/templates/base.html
git commit -m "feat: nav links for My Appointments (patient) and staff schedule (new tab)"
```

---

## Final verification

- [ ] Run the full suite once more: `python -m pytest -q` — expect 0 failures.
- [ ] Restore schema and reseed (pytest's session-scoped fixture drops all tables): `python -m alembic upgrade head` then `python -m seed.seed_data`.
- [ ] Manually start the app, log in as the seeded patient, visit `/appointments`, click Cancel on a real seeded appointment (or book one first via `/requests/new` if none exist), confirm redirect to `/requests/{id}` shows the cancellation confirmation.
- [ ] Log in as the seeded staff user, visit `/staff/appointments`, confirm it renders grouped by doctor with real data.
