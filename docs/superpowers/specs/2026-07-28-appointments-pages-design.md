# My Appointments (Patient) + Staff Appointment Walkthrough — Design Spec

Status: approved by user through conversational review (direct action buttons
for patients, grouped-by-doctor new-tab view for staff, confirmed directly).

Source of truth: `problem_statement.md`, `CLAUDE.md`. Builds directly on
`docs/superpowers/specs/2026-07-28-cancel-reschedule-design.md` (already
implemented, committed `3b9edca`) — this spec adds a second, direct entry
point into that same state machine, plus a read-only staff view. No new
`WorkflowStatus` values.

## 1. Goal

Two gaps:
1. Patients can only see appointments buried inside individual request-status
   pages (`dashboard.html`'s per-request rows) or by typing "cancel my
   appointment" and going through intent detection. There's no single page
   listing all of a patient's appointments with direct actions.
2. Staff can only see escalated requests (`staff_dashboard.html`). There's no
   view of the actual appointment schedule — which doctor has which patients
   booked, and when.

## 2. Scope

**In scope:**
- `GET /appointments` (patient-only) — lists the patient's active
  appointments with **Cancel** / **Reschedule** buttons per row.
- `POST /appointments/{appointment_id}/cancel` and
  `POST /appointments/{appointment_id}/reschedule` — ownership-checked,
  create a `WorkflowRun` pre-seeded at `needs_appointment_selection` and
  immediately delegate to the existing `continue_as_appointment_action`
  (from the already-shipped cancel/reschedule feature) — zero new
  cancel/reschedule business logic, this only adds a second way to reach it.
- `GET /staff/appointments` (staff-only) — read-only, appointments grouped by
  doctor.
- Nav link additions in `base.html`: "My Appointments" for patients (same
  tab), "Appointments" for staff (`target="_blank"`, new tab per user
  request).

**Explicitly out of scope:**
- Any change to `continue_as_appointment_action`, `continue_with_selected_slot`,
  or any part of the already-shipped cancel/reschedule flow — reused as-is.
- Staff taking action (cancel/reschedule/book) on a patient's behalf — staff
  view is read-only, per user's "walkthrough" framing.
- Filtering/search/sorting UI on either page beyond the grouping already
  specified — YAGNI, add if actually requested.
- Any change to `list_patient_appointments` — reused as-is by the new patient
  route exactly like the existing `/requests/{id}` selection screen uses it.

## 3. Architecture

### Patient side

**New helper (`app/workflow_runner.py`):**

```python
def start_appointment_action(db, patient_id: str, action: str, appointment_id: str) -> WorkflowRun:
    """Entry point from the My Appointments page - patient already picked
    both the action (Cancel/Reschedule button) and the target appointment
    (which row they clicked) with zero ambiguity, so this skips
    Safety/Coordinator/the graph entirely (nothing to detect - there is no
    free text to classify) and seeds a WorkflowRun directly at
    needs_appointment_selection, the same state run_workflow lands on after
    a typed "cancel my appointment" request. continue_as_appointment_action
    (already shipped, unchanged) takes it from there - this function's only
    job is constructing that starting state."""
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

Note: `user_id` is `None` here (the route has `user.id` available and should
pass it through — see route code below; shown as `None` in this snippet only
to mark it's a parameter, not a hardcoded value). The real route implementation
passes the actual `str(user.id)`.

**New route (`app/routes/dashboard_routes.py`** — same file as
`patient_dashboard`, keeps all patient-facing read/list routes together):

```python
from app.tools.appointment_tools import list_patient_appointments
from app.workflow_runner import start_appointment_action

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

Both routes need `uuid`, `Appointment`, `PatientProfile`, `status`,
`RedirectResponse`, `HTTPException` imports in `dashboard_routes.py` (some
already imported, some not — implementer checks current import list).

`_owned_active_appointment_or_404` raises 403 for someone else's appointment,
404 for a nonexistent one — same shape as every ownership check already in
`request_routes.py`. It does not check appointment status (already-cancelled
click would just no-op through `continue_as_appointment_action`'s existing
cancel branch, which is idempotent — cancelling an already-cancelled
appointment just re-sets the same status, harmless. Not special-cased.

**New template (`app/templates/my_appointments.html`):**

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

### Staff side

**New helper (`app/tools/appointment_tools.py`):**

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

Requires `PatientProfile`, `User` added to this file's existing model import
line (`AppointmentSlot`, `Department`, `Doctor` already imported; `User`,
`PatientProfile` are not — implementer adds them).

**New route (`app/routes/dashboard_routes.py`, alongside `staff_dashboard`):**

```python
from app.tools.appointment_tools import list_active_appointments_grouped_by_doctor

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

**New template (`app/templates/staff_appointments.html`):**

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

### Nav (`app/templates/base.html`)

Add to the existing patient nav block: a "My Appointments" link to
`/appointments` (same tab, next to "Dashboard").

Add to the existing staff nav block: an "Appointments" link to
`/staff/appointments` with `target="_blank"` (new tab, per user's explicit
request — staff likely wants the schedule open alongside the escalation
dashboard, not replacing it).

## 4. Data flow

**Patient:** `/appointments` → click Cancel/Reschedule on a row →
`start_appointment_action` seeds a `WorkflowRun` at `needs_appointment_selection`
→ `continue_as_appointment_action` (unchanged, already shipped) → redirect to
`/requests/{id}` → existing confirmation/slot-selection UI takes over exactly
as it does when reached via typed text.

**Staff:** `/staff/appointments` → one query, grouped in Python, rendered.
No writes, no state machine involvement at all.

## 5. Error handling

- Patient routes: 404 (nonexistent appointment), 403 (someone else's
  appointment) — same pattern as every other ownership check in this app.
- Clicking Cancel on an already-cancelled appointment (e.g. two tabs open):
  idempotent no-op via the existing `continue_as_appointment_action` cancel
  branch — not specially guarded, matches the "narrow race, not worse than
  existing accepted races" reasoning already used in the cancel/reschedule
  spec.
- Staff route: no error states — a query with zero rows renders the existing
  empty-state block, same as `staff_dashboard.html`'s pattern for zero
  escalations.

## 6. Testing

- `start_appointment_action`: constructs a `WorkflowRun` at
  `needs_appointment_selection` with correct `pending_appointment_action`,
  then verify it actually reaches `completed` (cancel) or
  `needs_slot_selection` (reschedule) exactly like the existing
  `continue_as_appointment_action` tests already prove for the typed-text
  entry point — same assertions, different starting point.
- `GET /appointments`: shows real appointments, empty state when none.
- `POST /appointments/{id}/cancel` and `/reschedule`: happy path (redirects
  to `/requests/{id}`, appointment state changes correctly), 404, 403.
- `list_active_appointments_grouped_by_doctor`: correct grouping with
  multiple doctors/patients, excludes cancelled appointments, empty list
  when none exist.
- `GET /staff/appointments`: renders grouped doctors/patients, empty state.
- RBAC: `/appointments` and its POST routes reject staff (403 via
  `require_role`); `/staff/appointments` rejects patients.

## 7. Open items resolved during self-review

- Confirmed `start_appointment_action` does not duplicate any cancel/reschedule
  business logic — it only constructs the same starting `WorkflowRun` state
  `run_workflow` would produce after a typed "cancel my appointment" request,
  then hands off to the already-shipped, already-tested
  `continue_as_appointment_action`.
- Confirmed no `ToolNode`/LLM involvement anywhere in either new route — both
  are either pure reads (staff view) or delegate to already-deterministic
  functions (patient actions) — no `SessionLocal`-vs-`db` threading concern
  applies here (that gotcha only matters when `ToolNode` runs tool calls in a
  worker thread; nothing here does).
- Confirmed `list_active_appointments_grouped_by_doctor` reuses the same
  active-status filter (`pending`, `confirmed`, `rescheduled`) as
  `list_patient_appointments`, kept consistent rather than reinvented.
- Flagged: `start_appointment_action`'s `request_text` value
  (`"[My Appointments page] cancel appointment"`) will appear in
  `dashboard.html`'s existing "My Requests" table alongside typed requests,
  since it creates a real `WorkflowRun` row. This is accepted as correct,
  not a bug — it's an honest record of what happened, and gives the patient
  a consistent history in one place rather than a hidden side-channel action.
