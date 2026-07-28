# Remember and Re-Offer Remaining Intent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When `needs_intent_selection` detects multiple intents and the patient picks one, remember the other(s) and re-offer them after the chosen action finishes, instead of silently dropping them once the first pick completes.

**Architecture:** One new state field (`remaining_intents: list[str]`), one small update to the existing `continue_as_intent_selection` (computes what's left over before dispatching), one new shared helper `_finalize_or_continue_intents` that every genuine-`completed` call site calls instead of setting `WorkflowStatus.completed` directly. No existing booking/cancel/reschedule business logic changes.

**Tech Stack:** FastAPI, LangGraph, SQLAlchemy, pytest. No LLM involvement in any of this — pure state-flag bookkeeping.

## Global Constraints

- No existing single-intent behavior may change — every task includes a regression check against the current passing suite (202 tests as of the last full run).
- `remaining_intents` only affects the four call sites explicitly listed in the spec (`docs/superpowers/specs/2026-07-28-remaining-intents-design.md` section 3) — `needs_slot_selection` and `needs_appointment_selection` (the intermediate, non-terminal statuses) are untouched.
- No new route, no new template block, no new `WorkflowStatus` value — this plan reuses `needs_intent_selection` and its existing template block exactly as built in the prior feature.

---

## Task 1: `remaining_intents` field + `continue_as_intent_selection` update

**Files:**
- Modify: `app/agents/state.py` (add one field)
- Modify: `app/workflow_runner.py:213-229` (`continue_as_intent_selection`)
- Test: `tests/test_workflow_runner.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `continue_as_intent_selection` now also writes `full_state["remaining_intents"]` before dispatching. Task 2 depends on this field being present in state by the time any terminal-completion call site runs.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_workflow_runner.py` (this file already has `make_workflow_run`, `workflow_state` helpers from `tests.fakes`, and imports `continue_as_intent_selection` from `app.workflow_runner` — check the existing top-of-file imports):

```python
def test_continue_as_intent_selection_stores_remaining_intent(db_session):
    workflow_run = make_workflow_run(db_session)
    workflow_run.state = workflow_state(
        workflow_run_id=str(workflow_run.id),
        intent="cancel_appointment,book_appointment",
    )
    db_session.commit()

    continue_as_intent_selection(db_session, workflow_run, "cancel_appointment")

    db_session.refresh(workflow_run)
    assert workflow_run.state["remaining_intents"] == ["book_appointment"]


def test_continue_as_intent_selection_no_remaining_when_last_pick(db_session):
    workflow_run = make_workflow_run(db_session)
    workflow_run.state = workflow_state(
        workflow_run_id=str(workflow_run.id),
        intent="cancel_appointment",
    )
    db_session.commit()

    continue_as_intent_selection(db_session, workflow_run, "cancel_appointment")

    db_session.refresh(workflow_run)
    assert workflow_run.state["remaining_intents"] == []
```

Note: `workflow_state()` (from `tests.fakes`) does not currently include a `remaining_intents` key — check its current definition; if missing, this is fine, since `continue_as_intent_selection` always sets it explicitly before any read, so no default is needed in the factory for these two tests to pass. Task 2's tests will need it in the factory though (see Task 2, Step 1).

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_workflow_runner.py::test_continue_as_intent_selection_stores_remaining_intent -v`
Expected: FAIL — `KeyError: 'remaining_intents'` (current `continue_as_intent_selection` doesn't set this key)

- [ ] **Step 3: Write minimal implementation**

Add `remaining_intents: list[str]` to `app/agents/state.py`'s `WorkflowState` TypedDict, directly below the existing `rescheduling_appointment_id: str | None` line.

Replace `app/workflow_runner.py:213-229` (the current `continue_as_intent_selection`):

```python
def continue_as_intent_selection(db, workflow_run: WorkflowRun, chosen_intent: str) -> WorkflowRun:
    """needs_intent_selection -> patient picked which of the detected
    intents to handle first (or next, if this is a continuation after an
    earlier pick already completed - see _finalize_or_continue_intents).
    Stores whatever's left over in remaining_intents before dispatching, so
    the leftover gets re-offered instead of silently dropped once the
    chosen action finishes. Dispatches to whichever existing, unmodified
    continuation the single-intent graph path would have used for that
    label - no new booking/cancel/reschedule logic here."""
    chosen = chosen_intent.strip().lower()
    full_state = dict(workflow_run.state)

    original = [label.strip().lower() for label in full_state.get("intent", "").split(",")]
    remaining = [
        label for label in original
        if label != chosen and any(kw in label for kw in ("book", "cancel", "reschedule"))
    ]
    full_state["remaining_intents"] = remaining
    workflow_run.state = full_state
    db.commit()

    if "book" in chosen:
        return continue_as_booking(db, workflow_run)

    action = "cancel" if "cancel" in chosen else "reschedule"
    full_state["pending_appointment_action"] = action
    workflow_run.state = full_state
    db.commit()
    return _land_on_appointment_selection_or_none(db, workflow_run, full_state)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_workflow_runner.py::test_continue_as_intent_selection_stores_remaining_intent tests/test_workflow_runner.py::test_continue_as_intent_selection_no_remaining_when_last_pick -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add app/agents/state.py app/workflow_runner.py tests/test_workflow_runner.py
git commit -m "feat: continue_as_intent_selection tracks remaining intents"
```

---

## Task 2: `_finalize_or_continue_intents` + wire into all four terminal call sites

**Files:**
- Modify: `app/workflow_runner.py` (add new function, replace 4 call sites)
- Modify: `tests/fakes.py` (`workflow_state()` factory — add `remaining_intents: []` default)
- Test: `tests/test_workflow_runner.py`

**Interfaces:**
- Consumes: `remaining_intents: list[str]` in `full_state` (Task 1).
- Produces: `_finalize_or_continue_intents(workflow_run: WorkflowRun, full_state: dict) -> None` — mutates `workflow_run.status`/`workflow_run.current_step`/`full_state` in place; caller still does `workflow_run.state = dict(full_state); db.commit()` itself afterward (this function does not commit).

- [ ] **Step 1: Write the failing test**

First, add `"remaining_intents": []` to `tests/fakes.py`'s `workflow_state()` factory dict (it currently ends with `"rescheduling_appointment_id": None,` — add the new key on the next line, same pattern as every other field in that factory).

Add to `tests/test_workflow_runner.py`:

```python
def test_finalize_or_continue_intents_completes_when_nothing_left():
    from app.workflow_runner import _finalize_or_continue_intents

    workflow_run = WorkflowRun(status=WorkflowStatus.needs_slot_selection)
    full_state = workflow_state(remaining_intents=[])

    _finalize_or_continue_intents(workflow_run, full_state)

    assert workflow_run.status == WorkflowStatus.completed


def test_finalize_or_continue_intents_bounces_back_when_intent_left():
    from app.workflow_runner import _finalize_or_continue_intents

    workflow_run = WorkflowRun(status=WorkflowStatus.needs_slot_selection)
    full_state = workflow_state(remaining_intents=["book_appointment"])

    _finalize_or_continue_intents(workflow_run, full_state)

    assert workflow_run.status == WorkflowStatus.needs_intent_selection
    assert full_state["intent"] == "book_appointment"
    assert full_state["remaining_intents"] == []
    assert full_state["needs_intent_selection"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_workflow_runner.py::test_finalize_or_continue_intents_bounces_back_when_intent_left -v`
Expected: FAIL with `ImportError: cannot import name '_finalize_or_continue_intents'`

- [ ] **Step 3: Write minimal implementation**

Add this function to `app/workflow_runner.py`, anywhere above its first call site (e.g. directly above `_land_on_appointment_selection_or_none`):

```python
def _finalize_or_continue_intents(workflow_run: WorkflowRun, full_state: dict) -> None:
    """Called at every point that would otherwise set a terminal `completed`
    status. If the patient had a leftover intent from an earlier
    needs_intent_selection pick, land back there for the next one instead
    of ending the run - closes the "asked which one first, then forgot the
    second" gap. Mutates workflow_run/full_state in place; caller still
    does workflow_run.state = dict(full_state) and db.commit() same as
    before, this only decides which status to set."""
    remaining = full_state.get("remaining_intents") or []
    if remaining:
        next_intent, *rest = remaining
        full_state["intent"] = next_intent
        full_state["remaining_intents"] = rest
        full_state["needs_intent_selection"] = True
        full_state["pending_appointment_action"] = None
        full_state["rescheduling_appointment_id"] = None
        full_state["department_id"] = None
        workflow_run.status = WorkflowStatus.needs_intent_selection
        workflow_run.current_step = "needs_intent_selection"
    else:
        workflow_run.status = WorkflowStatus.completed
```

Now replace the four terminal-completion call sites:

**Site 1 — `_land_on_appointment_selection_or_none`** (currently lines 115-125):
```python
def _land_on_appointment_selection_or_none(db, workflow_run: WorkflowRun, full_state: dict) -> WorkflowRun:
    appointments = list_patient_appointments(db, full_state["patient_id"])
    if appointments:
        workflow_run.status = WorkflowStatus.needs_appointment_selection
        workflow_run.current_step = "needs_appointment_selection"
    else:
        _finalize_or_continue_intents(workflow_run, full_state)
    full_state["status"] = workflow_run.status.value
    workflow_run.state = dict(full_state)
    db.commit()
    return workflow_run
```

**Site 2 — `_land_on_slots_or_no_slots`** (currently lines 128-153):
```python
def _land_on_slots_or_no_slots(
    db, workflow_run: WorkflowRun, full_state: dict, department_id: str, rescheduling_appointment_id: str | None = None
) -> WorkflowRun:
    """Shared by both continuation functions below once department_id is
    known (however it was determined - button click or Routing's LLM
    match). Queries real open slots directly via check_slot_availability
    (the plain function, not the LLM tool - no model involvement at all,
    nothing to guess). If any exist, lands at needs_slot_selection so the
    patient can pick one for real, with real doctor names and times, rather
    than the Appointment agent silently auto-picking one on their behalf.
    If genuinely none exist, lands at completed with no appointment_id (or
    bounces to the next remaining intent, if any) - same "couldn't find any
    open slots" wording branch as before, still accurate since that case
    really means what it says now."""
    full_state["department_id"] = department_id
    if rescheduling_appointment_id:
        full_state["rescheduling_appointment_id"] = rescheduling_appointment_id
    slots = check_slot_availability(db, department_id, {})
    if slots:
        workflow_run.status = WorkflowStatus.needs_slot_selection
        workflow_run.current_step = "needs_slot_selection"
    else:
        _finalize_or_continue_intents(workflow_run, full_state)
    full_state["status"] = workflow_run.status.value
    workflow_run.state = dict(full_state)
    db.commit()
    return workflow_run
```

**Site 3 — `continue_with_selected_slot`** (currently lines 243-273, the `else` branch on success):
```python
def continue_with_selected_slot(db, workflow_run: WorkflowRun, slot_id: str) -> WorkflowRun:
    """needs_slot_selection -> patient picked one specific real slot from
    the list shown. Books that exact slot directly via
    book_or_modify_appointment (the plain function, not the LLM tool) - no
    LLM call, no ToolNode involved at all, so `db` is safe to use directly
    here, unlike continue_as_booking/continue_as_booking_with_department
    above. If the slot was taken by someone else between listing and this
    click (a real, if narrow, race - the same class the original spec's
    error-handling section already anticipated for the deferred
    confirm-before-booking design), book_or_modify_appointment's existing
    conflict/no-longer-open checks catch it, no appointment gets created,
    and this stays at needs_slot_selection so the patient can pick a
    different one instead of the run silently failing."""
    patient_id = workflow_run.state["patient_id"]
    rescheduling_id = workflow_run.state.get("rescheduling_appointment_id")
    action = "reschedule" if rescheduling_id else "book"
    result = book_or_modify_appointment(db, patient_id, slot_id, action, rescheduling_id)

    full_state = dict(workflow_run.state)
    if result["status"] == "error":
        workflow_run.status = WorkflowStatus.needs_slot_selection
        workflow_run.current_step = "needs_slot_selection"
    else:
        full_state["appointment_id"] = result["id"]
        _finalize_or_continue_intents(workflow_run, full_state)
        workflow_run.current_step = workflow_run.current_step or "appointment_agent"
    full_state["status"] = workflow_run.status.value

    workflow_run.state = full_state
    db.commit()
    return workflow_run
```

**Site 4 — `continue_as_appointment_action`'s cancel branch** (currently lines 341-349):
```python
    if action == "cancel":
        result = book_or_modify_appointment(db, full_state["patient_id"], None, "cancel", appointment_id)
        full_state["appointment_id"] = result.get("id")
        _finalize_or_continue_intents(workflow_run, full_state)
        full_state["status"] = workflow_run.status.value
        workflow_run.state = full_state
        db.commit()
        return workflow_run
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_workflow_runner.py -v`
Expected: PASS (all tests in the file — this is the key regression check: every existing booking/cancel/reschedule test must still pass with `remaining_intents` empty by default)

- [ ] **Step 5: Commit**

```bash
git add app/workflow_runner.py tests/fakes.py tests/test_workflow_runner.py
git commit -m "feat: _finalize_or_continue_intents bounces to leftover intent instead of ending run"
```

---

## Task 3: Wording for the continuation screen

**Files:**
- Modify: `app/routes/request_routes.py` (`_render_patient_message`)
- Test: `tests/test_request_routes.py`

**Interfaces:**
- Consumes: `state.get("intent")` — a comma-joined string (first landing) or single label (continuation).
- Produces: no new interface — final task in this plan.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_request_routes.py`, matching the exact pattern of the
existing `test_render_patient_message_needs_appointment_selection` test
already in this file (`user = make_user(db_session)`, a bare `workflow_run`
from `make_workflow_run(db_session)`, `workflow_run.state` set directly as a
plain dict, `_render_patient_message` called directly — no HTTP client
needed for this one):

```python
def test_render_patient_message_needs_intent_selection_first_landing(db_session):
    user = make_user(db_session)
    workflow_run = make_workflow_run(db_session)
    workflow_run.status = WorkflowStatus.needs_intent_selection
    workflow_run.state = {"intent": "cancel_appointment,book_appointment"}
    db_session.commit()

    message = _render_patient_message(db_session, user, workflow_run)
    assert "few things" in message


def test_render_patient_message_needs_intent_selection_continuation(db_session):
    user = make_user(db_session)
    workflow_run = make_workflow_run(db_session)
    workflow_run.status = WorkflowStatus.needs_intent_selection
    workflow_run.state = {"intent": "book_appointment"}
    db_session.commit()

    message = _render_patient_message(db_session, user, workflow_run)
    assert "rest of your request" in message
```

`_render_patient_message` is already imported in this test file (used by
the neighboring `needs_appointment_selection` test) — no new import needed.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_request_routes.py::test_render_patient_message_needs_intent_selection_continuation -v`
Expected: FAIL — current code has no `needs_intent_selection` branch check for the comma-vs-no-comma distinction (it always renders the same "which one first" message regardless)

- [ ] **Step 3: Write minimal implementation**

In `app/routes/request_routes.py`'s `_render_patient_message`, find:
```python
    if workflow_status == WorkflowStatus.needs_intent_selection:
        message = "It sounds like you're asking about a few things. Which one should I help with first?"
```

Replace with:
```python
    if workflow_status == WorkflowStatus.needs_intent_selection:
        if "," in state.get("intent", ""):
            message = "It sounds like you're asking about a few things. Which one should I help with first?"
        else:
            message = "Got it. Now let's take care of the rest of your request — which one's next?"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_request_routes.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add app/routes/request_routes.py tests/test_request_routes.py
git commit -m "feat: distinct wording for first multi-intent landing vs. continuation"
```

---

## Final verification

- [ ] Run the full suite: `python -m pytest -q` — expect 0 failures, count higher than 202.
- [ ] Restore schema and reseed: `python -m alembic upgrade head` then `python -m seed.seed_data` (no new migration in this plan, so `alembic upgrade head` should report already at head — that's expected, not an error).
- [ ] Live-check against the real Groq API: submit "cancel my current appointment and book a new one with cardiology" (with a real seeded appointment to cancel and real open cardiology slots), pick "Cancel an appointment" first, confirm the cancellation completes AND immediately re-lands on `needs_intent_selection` with only "Book an appointment" offered (wording: "Got it. Now let's take care of the rest of your request — which one's next?"), pick it, confirm a real booking completes.
