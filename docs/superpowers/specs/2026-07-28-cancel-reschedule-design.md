# Cancel / Reschedule Appointment — Design Spec

Status: approved by user through conversational review (one-click pattern,
matching booking, confirmed directly).

Source of truth for rules/scope: `problem_statement.md`, `CLAUDE.md`. Builds
on the current (post-determinism-pivot) shape of `app/workflow_runner.py` and
`app/graph.py` — booking has no LLM decision step anymore; the patient picks
a real DB-backed slot from a list and clicks it. This spec extends that same
pattern to cancel and reschedule.

## 1. Goal

The Coordinator agent already classifies `cancel_appointment` and
`reschedule_appointment` as distinct intents (`app/agents/coordinator.py`) —
confirmed live. But `app/graph.py`'s `route_after_document` only recognizes
`book_appointment`; every other intent, including these two, falls through to
the generic `needs_clarification` screen. The DB-level action
(`book_or_modify_appointment` in `app/tools/appointment_tools.py`) already
supports `"cancel"` and `"reschedule"` — those code paths are written and
tested, just never called from a route. This spec wires the two missing
pieces together: recognize the intent, let the patient pick which of their
real appointments they mean, and act on it — same one-click, no-LLM-decision
pattern already used for booking.

## 2. Scope

**In scope:**
- `route_after_document` (`app/graph.py`) recognizes `cancel_appointment` and
  `reschedule_appointment` (tolerant substring match, same style as the
  existing `"book" in intent` check) and routes to a new node.
- One new `WorkflowStatus`: `needs_appointment_selection` (one migration).
- `WorkflowState` gains `pending_appointment_action: str | None` (`"cancel"`
  or `"reschedule"`) and `rescheduling_appointment_id: str | None`.
- A new plain query helper, `list_patient_appointments(db, patient_id)`, in
  `app/tools/appointment_tools.py` — active appointments only (`pending`,
  `confirmed`, `rescheduled`), enriched with doctor/department/time the same
  way `appointment_display_details` already is.
- `request_status.html` gains a `needs_appointment_selection` block: one
  button per real upcoming appointment.
- One new route, `POST /requests/{id}/select-appointment`, handling both
  actions.
- `_land_on_slots_or_no_slots` (existing helper) gains an optional
  `rescheduling_appointment_id` param, so reschedule's second step (pick a
  new slot) reuses the exact same `needs_slot_selection` screen booking
  already uses.
- `continue_with_selected_slot` (existing) branches on whether
  `rescheduling_appointment_id` is set in state: if so, calls
  `book_or_modify_appointment(action="reschedule", existing_appointment_id=...)`
  instead of `action="book"`.
- Patient-facing wording for both new terminal outcomes ("cancelled",
  "rescheduled"), plain Python string building from real rows — same
  principle as every other confirmation message in this app.

**Explicitly out of scope:**
- A confirm-before-cancel step — matches the existing one-click booking
  pattern, confirmed with user.
- Any LLM decision about *which* appointment the patient means — the patient
  picks from a real, complete list of their own appointments by clicking;
  there is never more than one reasonable interpretation to guess at, so
  there is nothing for a model to usefully decide (consistent with this
  session's established direction: "where come llm came" — deterministic
  wherever a DB query and a click can do the job).
- Any change to Coordinator's prompt or tools — `cancel_appointment` /
  `reschedule_appointment` classification already works today.
- Any change to `book_or_modify_appointment`'s cancel/reschedule branches
  themselves — both are already implemented and covered by existing tests in
  `test_appointment_tools.py`-equivalent coverage; this spec only adds
  callers.

## 3. Architecture

### Graph change (`app/graph.py`)

```python
def route_after_document(state: WorkflowState) -> Literal["routing_agent", "needs_appointment_selection", "needs_clarification"]:
    intent = (state.get("intent") or "").strip().lower()
    if intent == "book_appointment" or "book" in intent:
        return "routing_agent"
    if "cancel" in intent or "reschedule" in intent:
        return "needs_appointment_selection"
    return "needs_clarification"


def needs_appointment_selection_node(state: WorkflowState, config) -> dict:
    intent = (state.get("intent") or "").strip().lower()
    action = "cancel" if "cancel" in intent else "reschedule"
    return {"needs_appointment_selection": True, "pending_appointment_action": action}
```

Mirrors the existing `needs_clarification_node` shape exactly — a trivial
node that sets flags, no DB access inside the graph itself. The real "does
the patient even have any appointments to act on" query happens in
`workflow_runner.py`'s post-stream resolution, same division of
responsibility `_land_on_slots_or_no_slots` already uses for booking.

`WorkflowState` gains:
```python
pending_appointment_action: str | None   # "cancel" | "reschedule" | None
rescheduling_appointment_id: str | None  # set only mid-reschedule, between
                                          # picking the appointment and
                                          # picking its new slot
```

### New query helper (`app/tools/appointment_tools.py`)

```python
def list_patient_appointments(db: Session, patient_id: str) -> list[dict]:
    """Real DB query: this patient's own active appointments (not
    cancelled), enriched with doctor/department/time for display - same
    enrichment shape as appointment_display_details, but for a list instead
    of one row. Used to render the needs_appointment_selection screen; the
    patient picks by clicking, nothing here is a guess."""
    rows = (
        db.query(Appointment)
        .filter(Appointment.patient_id == uuid.UUID(patient_id))
        .filter(Appointment.status.in_([
            AppointmentStatus.pending, AppointmentStatus.confirmed, AppointmentStatus.rescheduled,
        ]))
        .all()
    )
    result = []
    for appointment in rows:
        details = appointment_display_details(db, str(appointment.id))
        if details:
            result.append({"appointment_id": str(appointment.id), **details})
    return sorted(result, key=lambda r: r["formatted_time"])
```

Plain helper, not an `@tool`-wrapped LLM tool and not `@audited` — it's a
read used for rendering, same category as `appointment_display_details`
(neither is one of the app's 8 canonical agentic tool functions).

Note: sorting by `formatted_time` (a display string, `"%B %d, %Y at %I:%M
%p"`) is lexicographic, not chronological — acceptable here since the list is
typically small (a handful of upcoming appointments) and exact order is a
minor UX nicety, not a correctness requirement. Flagged, not fixed — would
sort by the underlying `AppointmentSlot.start_time` before formatting if this
list ever needs to be authoritative order.

### Workflow runner (`app/workflow_runner.py`)

Status resolution gains one more branch, checked before the existing
`department_id` branch:

```python
elif full_state.get("needs_appointment_selection"):
    return _land_on_appointment_selection_or_none(db, workflow_run, full_state)
```

New helper, same shape as `_land_on_slots_or_no_slots`:

```python
def _land_on_appointment_selection_or_none(db, workflow_run: WorkflowRun, full_state: dict) -> WorkflowRun:
    appointments = list_patient_appointments(db, full_state["patient_id"])
    if appointments:
        workflow_run.status = WorkflowStatus.needs_appointment_selection
    else:
        workflow_run.status = WorkflowStatus.completed
    workflow_run.current_step = "needs_appointment_selection"
    full_state["status"] = workflow_run.status.value
    workflow_run.state = dict(full_state)
    db.commit()
    return workflow_run
```

Zero active appointments → lands directly at `completed` with no
`appointment_id` set; the wording layer (below) distinguishes this from the
"no slots available" case using `pending_appointment_action`.

`_land_on_slots_or_no_slots` gains one optional parameter:

```python
def _land_on_slots_or_no_slots(db, workflow_run, full_state, department_id, rescheduling_appointment_id=None):
    full_state["department_id"] = department_id
    if rescheduling_appointment_id:
        full_state["rescheduling_appointment_id"] = rescheduling_appointment_id
    ...  # unchanged below
```

`continue_with_selected_slot` gains one branch at the top:

```python
def continue_with_selected_slot(db, workflow_run, slot_id: str) -> WorkflowRun:
    patient_id = workflow_run.state["patient_id"]
    rescheduling_id = workflow_run.state.get("rescheduling_appointment_id")
    action = "reschedule" if rescheduling_id else "book"
    result = book_or_modify_appointment(db, patient_id, slot_id, action, rescheduling_id)
    ...  # unchanged below - result["id"] is the same appointment id either way
```

New function, `continue_as_appointment_action`, called by the new route:

```python
def continue_as_appointment_action(db, workflow_run: WorkflowRun, appointment_id: str) -> WorkflowRun:
    """needs_appointment_selection -> patient picked which real appointment
    they mean. Branches on the action recorded when the graph set
    needs_appointment_selection:

    - cancel: calls book_or_modify_appointment(action="cancel") directly -
      deterministic, no LLM, no ToolNode, `db` safe to use directly (same
      class as continue_with_selected_slot). Lands at completed immediately.
    - reschedule: looks up the appointment's current department (via its
      doctor), then reuses _land_on_slots_or_no_slots with
      rescheduling_appointment_id set, landing at needs_slot_selection - the
      exact same screen and continue_with_selected_slot path booking uses,
      just pre-scoped to this appointment's department and remembering which
      appointment to update instead of creating a new one."""
    full_state = dict(workflow_run.state)
    action = full_state.get("pending_appointment_action")

    if action == "cancel":
        result = book_or_modify_appointment(db, full_state["patient_id"], None, "cancel", appointment_id)
        full_state["appointment_id"] = result.get("id")
        workflow_run.status = WorkflowStatus.completed
        workflow_run.current_step = "needs_appointment_selection"
        full_state["status"] = workflow_run.status.value
        workflow_run.state = full_state
        db.commit()
        return workflow_run

    # reschedule: find the department this appointment is currently in
    appointment = db.query(Appointment).filter(Appointment.id == uuid.UUID(appointment_id)).first()
    doctor = db.query(Doctor).filter(Doctor.id == appointment.doctor_id).first()
    return _land_on_slots_or_no_slots(
        db, workflow_run, full_state, str(doctor.department_id),
        rescheduling_appointment_id=appointment_id,
    )
```

### Routes (`app/routes/request_routes.py`)

```python
POST /requests/{id}/select-appointment
  appointment_id=<uuid> -> continue_as_appointment_action(db, workflow_run, appointment_id)
```

Same guard shape as every other action route in this file: `require_role
("patient")`, ownership check (404/403), stale-status no-op redirect if
`workflow_run.status != needs_appointment_selection`.

The `GET /requests/{id}` route's existing per-status context block gains one
more entry: when `needs_appointment_selection`, pass
`appointments=list_patient_appointments(db, patient_id)` into the template
context (same pattern as the existing `departments=`/`slots=` entries for
the other two selection screens).

### Wording (`_render_patient_message` in `app/routes/request_routes.py`)

- `needs_appointment_selection` → `"Which appointment is this about?"`
  (buttons rendered by the template, one per appointment, labeled with
  doctor/department/time)
- `completed`, `pending_appointment_action == "cancel"`, `appointment_id`
  set → look up `appointment_display_details` for that id (still resolvable
  after cancellation — the row isn't deleted, only its status changes) →
  `"Your appointment with {doctor} in {department} on {formatted_time} has
  been cancelled."`
- `completed`, `pending_appointment_action == "cancel"`, no `appointment_id`
  (patient had zero appointments) → `"You don't have any upcoming
  appointments to cancel."`
- `completed`, `pending_appointment_action == "reschedule"`, `appointment_id`
  set → `"Done! Your appointment is now with {doctor} in {department} on
  {formatted_time}."`
- `completed`, `pending_appointment_action == "reschedule"`, no
  `appointment_id` (zero appointments, never reached slot selection) →
  `"You don't have any upcoming appointments to reschedule."`
- All existing branches (`needs_clarification`, `needs_appointment_reason`,
  `needs_slot_selection`, plain booking `completed`, `needs_review`,
  `failed`) unchanged.

## 4. Data flow

1. Patient submits free text, e.g. "I need to cancel my appointment."
2. Safety → Coordinator (unchanged) → intent = `cancel_appointment`.
3. Document agent runs (unchanged, no-op if no file attached).
4. `route_after_document` sees `"cancel"` in intent → `needs_appointment_selection_node` sets flags → graph ends.
5. `workflow_runner` post-stream resolution queries the patient's real active appointments. None → `completed` with the "nothing to cancel" wording. One or more → `needs_appointment_selection`.
6. Patient sees a list of real appointments as buttons, clicks one.
7. `POST /select-appointment` → `continue_as_appointment_action`:
   - cancel → done immediately, `completed`.
   - reschedule → department resolved from the appointment's doctor → real open slots in that department shown (`needs_slot_selection`, same screen/route as booking) → patient clicks a slot → `continue_with_selected_slot` sees `rescheduling_appointment_id` set → reschedules the same appointment row → `completed`.

## 5. Error handling

- Same `require_role`/ownership/404/stale-status-no-op pattern as every
  other route in this file.
- `book_or_modify_appointment`'s existing cancel/reschedule branches already
  handle "appointment not found" and "slot no longer open" (for reschedule's
  second step) with structured `{"status": "error", ...}` returns — no new
  error-handling code needed, this spec only adds callers into paths that
  already result in `completed` (cancel) or stay at `needs_slot_selection`
  on conflict (reschedule's slot pick, via the existing
  `continue_with_selected_slot` error branch).
- If a patient has an appointment selected for reschedule and it gets
  cancelled by someone else (e.g. staff) before they pick a new slot, the
  `Appointment` row lookup in `continue_as_appointment_action` still
  succeeds (the row isn't deleted) — the eventual `book_or_modify_appointment
  (action="reschedule")` call would then resurrect a cancelled appointment
  into a rescheduled one. Narrow race, no worse than the existing "someone
  else double-books a slot" race already accepted for booking; not
  special-cased here for the same reason.

## 6. Testing

- Graph routing: `cancel_appointment` / "cancel my appointment" →
  `needs_appointment_selection`; `reschedule_appointment` / "reschedule my
  visit" → `needs_appointment_selection`; `pending_appointment_action` set
  correctly in each case.
- `workflow_runner`: full run ending at `needs_appointment_selection` when
  the patient has real appointments; ending at `completed` with the
  "nothing to cancel/reschedule" wording path when they have none.
- `continue_as_appointment_action`: cancel branch sets `Appointment.status =
  cancelled`, frees the slot (`SlotStatus.open`), lands `completed`.
  Reschedule branch resolves the correct department and lands
  `needs_slot_selection` with `rescheduling_appointment_id` set.
- `continue_with_selected_slot`: extended existing tests to cover the
  `rescheduling_appointment_id` branch — same appointment id before and
  after, new `slot_id`/`doctor_id`, status becomes `rescheduled`, old slot
  freed.
- Routes: `/select-appointment` happy path (both actions) + wrong-owner
  (403) + nonexistent (404) + stale-status no-op, mirroring
  `test_select_slot_*` patterns already in `tests/test_request_routes.py`.
- Live Groq validation: "I want to cancel my appointment" and "I need to
  reschedule my visit" against a seeded patient with a real booked
  appointment, confirming intent classification and the full click-through
  path, same reporting pattern used for every fix this session.

## 7. Open items resolved during self-review

- Confirmed `list_patient_appointments` reuses `appointment_display_details`
  per-row rather than duplicating its doctor/department/time query — same
  "one query, multiple callers" principle already applied to that function.
- Confirmed no new `WorkflowStatus` is needed for reschedule's second step —
  it reuses `needs_slot_selection` exactly as-is, distinguished only by the
  presence of `rescheduling_appointment_id` in state, not a new status value.
- Confirmed `pending_appointment_action` and `rescheduling_appointment_id`
  are plain state flags the graph/runner set, never a judgment call — same
  category as the existing `escalation`/`needs_appointment_reason` fields.
- Flagged (not fixed, noted above): `list_patient_appointments`'s sort is by
  formatted display string, not the underlying timestamp — fine at current
  scale, would need fixing before this list could be relied on for strict
  chronological order.
