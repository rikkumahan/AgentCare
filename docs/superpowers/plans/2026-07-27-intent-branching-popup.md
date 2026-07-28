# Intent Branching + Patient Clarification Popup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the Coordinator agent's classified intent isn't a confident
"book an appointment," stop the workflow run as `needs_clarification`
instead of guessing or silently escalating, show the patient a
human-readable question with two buttons ("Book an appointment" / "Talk to
hospital staff") on the existing status page, and act deterministically on
whichever they click.

**Architecture:** One new conditional edge in the parent LangGraph
(`document_agent` → `routing_agent` or a new terminal `needs_clarification`
node, based on `state["intent"]`), one new `WorkflowStatus` enum value +
migration, two new plain-Python continuation functions in
`app/workflow_runner.py` invoked only from a new
`POST /requests/{id}/clarify` route (never from `run_workflow` itself), and
a `_render_patient_message` helper that replaces the status page's raw
field dump with a sentence built from real DB rows.

**Tech Stack:** FastAPI + Jinja2, LangGraph (`StateGraph`), SQLAlchemy +
Alembic (Postgres), pytest with a real Postgres `db_session` fixture and
`FakeToolCallingModel`.

## Scope note (read before starting)

The source spec (`docs/superpowers/specs/2026-07-27-intent-branching-clarification-design.md`)
covers **two** pause points: (A) "intent unclear" and (B) "confirm before
booking." **This plan builds ONLY (A).** (B) — the Appointment agent giving
up automatic booking in favor of a candidate-slot proposal + a
`needs_booking_confirmation` status + a `commit_confirmed_booking` function +
a `POST /confirm-booking` route — is explicitly deferred to its own later
plan (decided in `docs/memory/status.md`: every previous touch to the
Appointment agent's core LLM logic this session surfaced a new real bug,
so it's isolated into its own review cycle). Every task below reflects an
(A)-only reading of the spec; where the spec's own pseudocode assumes both
(A) and (B) ship together, this plan does **not** follow it literally — see
the Self-Review section for the itemized list of what's intentionally
excluded.

## Global Constraints

(From `CLAUDE.md` — apply to every task below.)

- Persistent SQL only — no in-memory dicts/session vars for workflow state.
  Everything durable lives in Postgres via `WorkflowRun.state`/`status`.
- No tool may return a fixed response regardless of input.
- No hardcoded final responses — `_render_patient_message` is plain Python
  that reads real persisted rows (patient name, doctor, department,
  appointment time), never a free-standing LLM string.
- RBAC is enforced in backend route/dependency code (`require_role`), not
  hidden in templates.
- Never diagnose, prescribe, or claim to replace a clinician — unaffected by
  this plan (no agent prompts change).
- Alembic is the source of truth for schema — one new migration, chained
  off the real current head (verified below), no dump files.

---

## Preliminary facts verified before writing this plan

- **Current Alembic head:** `b7e2f4a91c3d` (`add_required_document_types`,
  `down_revision = '1dd0ad4bbe02'`). Only two migration files exist in
  `alembic/versions/`; the `PatientDocument` checksum unique constraint is
  already part of the initial migration, not a separate one. The new
  migration in Task 1 chains `down_revision = 'b7e2f4a91c3d'`.
- **Current `app/graph.py`** already has `document_agent` and
  `routing_agent` as real wired nodes (`coordinator_agent` →
  `document_agent` → `routing_agent` → conditional → `appointment_agent` →
  `END`), from the just-completed Document agent build. This plan replaces
  the single unconditional `document_agent` → `routing_agent` edge with a
  conditional edge based on `state["intent"]`.
- **Current `app/workflow_runner.py`**'s final status-resolution block
  (lines 70-78) still reads:
  ```python
  if full_state.get("escalation"):
      workflow_run.status = WorkflowStatus.needs_review
  else:
      workflow_run.status = WorkflowStatus.running
      workflow_run.current_step = "document_agent"
  ```
  — a placeholder from before `document_agent`/`routing_agent` were wired
  into the graph. This plan replaces it with a real three-way resolution
  that uses `WorkflowStatus.completed` for the first time.
- **Current `app/routes/request_routes.py`** has no `_render_patient_message`
  helper yet (confirmed by reading the file in full) — this plan creates it.
- **Current `app/templates/request_status.html`** is a raw field dump
  (`Status:`, `Current step:`, and a `<ul>` of raw state fields including
  `document_ids`) — this plan replaces it with the rendered sentence plus
  conditional buttons.

---

### Task 1: `WorkflowStatus.needs_clarification` — model + migration

**Files:**
- Modify: `app/models.py:141-146`
- Create: `alembic/versions/c48e9a271f36_add_needs_clarification_status.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces: `WorkflowStatus.needs_clarification` (value `"needs_clarification"`),
  consumed by every later task in this plan.

- [ ] **Step 1: Write the failing test**

Read `tests/test_models.py` first to match its existing style, then add:

```python
def test_workflow_status_has_needs_clarification_value():
    assert WorkflowStatus.needs_clarification.value == "needs_clarification"
```

Add `WorkflowStatus` to the existing `from app.models import ...` line at
the top of `tests/test_models.py` if it isn't already imported (check the
current import list — if `WorkflowStatus` isn't present, add it there
rather than a second import line).

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_models.py::test_workflow_status_has_needs_clarification_value -v`
Expected: FAIL with `AttributeError: needs_clarification`

- [ ] **Step 3: Add the enum value**

In `app/models.py`, the current `WorkflowStatus` class (lines 141-146) is:

```python
class WorkflowStatus(str, enum.Enum):
    running = "running"
    completed = "completed"
    failed = "failed"
    needs_review = "needs_review"
```

Change it to:

```python
class WorkflowStatus(str, enum.Enum):
    running = "running"
    completed = "completed"
    failed = "failed"
    needs_review = "needs_review"
    needs_clarification = "needs_clarification"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_models.py::test_workflow_status_has_needs_clarification_value -v`
Expected: PASS

- [ ] **Step 5: Write the Alembic migration**

Create `alembic/versions/c48e9a271f36_add_needs_clarification_status.py`:

```python
"""add needs_clarification to workflow_status

Revision ID: c48e9a271f36
Revises: b7e2f4a91c3d
Create Date: 2026-07-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c48e9a271f36'
down_revision: Union[str, Sequence[str], None] = 'b7e2f4a91c3d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TYPE workflow_status ADD VALUE IF NOT EXISTS 'needs_clarification'")


def downgrade() -> None:
    """Downgrade schema."""
    # Postgres has no direct "drop enum value" operation - reversing this
    # would require recreating the workflow_status type without the value
    # and remapping every existing row. Left as a no-op: this migration is
    # purely additive (a new allowed status value, no column/table change),
    # and nothing in this phase requires a working downgrade path for it.
    pass
```

- [ ] **Step 6: Apply the migration against the dev database**

Run: `alembic upgrade head`
Expected output ends with: `Running upgrade b7e2f4a91c3d -> c48e9a271f36, add needs_clarification to workflow_status`

(Per `docs/memory/gotchas.md`, run this again after any `pytest` run before
manual/app use — pytest's schema fixture drops and recreates via
`Base.metadata`, not via Alembic, and also drops `alembic_version`.)

- [ ] **Step 7: Commit**

```bash
git add app/models.py alembic/versions/c48e9a271f36_add_needs_clarification_status.py tests/test_models.py
git commit -m "feat: add needs_clarification WorkflowStatus value"
```

---

### Task 2: Graph routing after `document_agent`

**Files:**
- Modify: `app/agents/state.py:1-15`
- Modify: `app/graph.py` (full file, 45 lines)
- Test: `tests/test_graph.py` (new file)

**Interfaces:**
- Consumes: `WorkflowState` (from Task-independent existing code).
- Produces: `route_after_document(state) -> Literal["routing_agent", "needs_clarification"]`,
  `needs_clarification_node(state, config) -> dict` (returns
  `{"needs_clarification": True}`) — both used by `build_graph()` and by
  Task 3's `workflow_runner` status resolution, which reads
  `full_state.get("needs_clarification")`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_graph.py`:

```python
import pytest

from app.graph import needs_clarification_node, route_after_document


@pytest.mark.parametrize(
    "intent,expected",
    [
        ("book_appointment", "routing_agent"),
        ("booking", "routing_agent"),
        ("I want to book something", "routing_agent"),
        ("BOOK_APPOINTMENT", "routing_agent"),
        ("reschedule_appointment", "needs_clarification"),
        ("cancel_appointment", "needs_clarification"),
        ("general_inquiry", "needs_clarification"),
        ("submit_document", "needs_clarification"),
        (None, "needs_clarification"),
        ("", "needs_clarification"),
        ("asdkjfh garbage", "needs_clarification"),
    ],
)
def test_route_after_document(intent, expected):
    state = {"intent": intent}
    assert route_after_document(state) == expected


def test_needs_clarification_node_sets_flag():
    update = needs_clarification_node({"intent": "general_inquiry"}, config={"configurable": {}})
    assert update == {"needs_clarification": True}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_graph.py -v`
Expected: FAIL with `ImportError: cannot import name 'route_after_document'`

- [ ] **Step 3: Add `needs_clarification` to `WorkflowState`**

`app/agents/state.py` currently (all 15 lines):

```python
from typing import TypedDict


class WorkflowState(TypedDict):
    workflow_run_id: str
    patient_id: str
    user_id: str
    request_text: str
    uploaded_files: list[str]
    intent: str | None
    department_id: str | None
    appointment_id: str | None
    document_ids: list[str]
    reminder_ids: list[str]
    escalation: dict | None
    status: str
```

Change the last field line (`status: str`) to add the new key after it:

```python
    status: str
    needs_clarification: bool
```

- [ ] **Step 4: Rewrite `app/graph.py`**

Replace the full contents of `app/graph.py` with:

```python
from typing import Literal

from langgraph.graph import END, StateGraph

from app.agents.appointment import appointment_agent_node
from app.agents.coordinator import coordinator_agent_node
from app.agents.document import document_agent_node
from app.agents.routing import routing_agent_node
from app.agents.safety import safety_agent_node
from app.agents.state import WorkflowState


def route_after_safety(state: WorkflowState) -> Literal["coordinator_agent", "__end__"]:
    if state.get("escalation"):
        return "__end__"
    return "coordinator_agent"


def route_after_document(state: WorkflowState) -> Literal["routing_agent", "needs_clarification"]:
    intent = (state.get("intent") or "").strip().lower()
    if intent == "book_appointment" or "book" in intent:
        return "routing_agent"
    return "needs_clarification"


def needs_clarification_node(state: WorkflowState, config) -> dict:
    return {"needs_clarification": True}


def route_after_routing(state: WorkflowState) -> Literal["appointment_agent", "__end__"]:
    if state.get("escalation"):
        return "__end__"
    return "appointment_agent"


def build_graph():
    graph = StateGraph(WorkflowState)

    graph.add_node("safety_agent", safety_agent_node)
    graph.add_node("coordinator_agent", coordinator_agent_node)
    graph.add_node("document_agent", document_agent_node)
    graph.add_node("needs_clarification", needs_clarification_node)
    graph.add_node("routing_agent", routing_agent_node)
    graph.add_node("appointment_agent", appointment_agent_node)

    graph.set_entry_point("safety_agent")
    graph.add_conditional_edges(
        "safety_agent", route_after_safety, {"coordinator_agent": "coordinator_agent", "__end__": END}
    )
    graph.add_edge("coordinator_agent", "document_agent")
    graph.add_conditional_edges(
        "document_agent",
        route_after_document,
        {"routing_agent": "routing_agent", "needs_clarification": "needs_clarification"},
    )
    graph.add_edge("needs_clarification", END)
    graph.add_conditional_edges(
        "routing_agent", route_after_routing, {"appointment_agent": "appointment_agent", "__end__": END}
    )
    graph.add_edge("appointment_agent", END)

    return graph.compile()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_graph.py -v`
Expected: PASS (12 tests)

- [ ] **Step 6: Commit**

```bash
git add app/agents/state.py app/graph.py tests/test_graph.py
git commit -m "feat: branch graph after document_agent on intent, add needs_clarification node"
```

---

### Task 3: `workflow_runner` status resolution (three-way) + regression fixes

**Files:**
- Modify: `app/workflow_runner.py:1-5` (imports) and `:26-39,70-78` (initial
  state + status resolution)
- Modify: `tests/test_workflow_runner.py` (regression assertion updates +
  one new test)

**Interfaces:**
- Consumes: `route_after_document`/`needs_clarification_node` from Task 2
  (indirectly, via the compiled graph), `WorkflowStatus.needs_clarification`
  from Task 1.
- Produces: no new public function yet (that's Task 4) — this task only
  changes `run_workflow`'s terminal status resolution, which Task 4 and the
  routes in Task 6 rely on being correct (`needs_review` /
  `needs_clarification` / `completed`).

- [ ] **Step 1: Update the failing/changed assertions first (regression)**

`tests/test_workflow_runner.py`'s `test_full_workflow_books_appointment_end_to_end`
(around line 128-131) currently asserts:

```python
    assert workflow_run.status == WorkflowStatus.running
    assert workflow_run.current_step == "document_agent"
```

Change to:

```python
    assert workflow_run.status == WorkflowStatus.completed
    assert workflow_run.current_step == "appointment_agent"
    assert workflow_run.state.get("needs_clarification") is None
```

(The other assertions in that test — `department_id`, `appointment_id`,
the `Appointment`/`AppointmentSlot` DB checks — are unchanged; the booking
behavior itself does not change in this plan.)

- [ ] **Step 2: Add the new failing test for the needs_clarification path**

Add to `tests/test_workflow_runner.py`:

```python
def test_non_booking_intent_ends_needs_clarification(monkeypatch, db_session):
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)

    safety_model = FakeToolCallingModel([ai_message_text("SAFE")])
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: safety_model)

    coordinator_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("get_or_create_patient_tool", {"profile_fields": {}}),
            ai_message_text("general_inquiry"),
        ]
    )
    monkeypatch.setattr("app.agents.coordinator.get_llm", lambda: coordinator_model)

    workflow_run = run_workflow(
        db_session,
        patient_id=str(profile.id),
        user_id=str(user.id),
        request_text="what are your visiting hours?",
    )

    assert workflow_run.status == WorkflowStatus.needs_clarification
    assert workflow_run.current_step == "needs_clarification"
    assert workflow_run.state["needs_clarification"] is True
    assert workflow_run.state["intent"] == "general_inquiry"
    assert workflow_run.state["appointment_id"] is None
```

(No `routing`/`appointment` model mocks needed — `document_agent` is a
no-op with no uploaded files, and the graph never reaches `routing_agent`
for a non-booking intent.)

- [ ] **Step 3: Run both to verify current (pre-fix) failure state**

Run: `pytest tests/test_workflow_runner.py::test_full_workflow_books_appointment_end_to_end tests/test_workflow_runner.py::test_non_booking_intent_ends_needs_clarification -v`
Expected: both FAIL — the first because `status`/`current_step` don't match
yet (old code still sets `running`/`document_agent`), the second because
`workflow_run.state["needs_clarification"]` is absent (`KeyError`) since
the resolution block never sets it.

- [ ] **Step 4: Replace the status-resolution block**

`app/workflow_runner.py` lines 70-78 currently:

```python
    if full_state.get("escalation"):
        workflow_run.status = WorkflowStatus.needs_review
    else:
        workflow_run.status = WorkflowStatus.running
        workflow_run.current_step = "document_agent"

    full_state["status"] = workflow_run.status.value
    workflow_run.state = dict(full_state)
    db.commit()
    return workflow_run
```

Replace with:

```python
    if full_state.get("escalation"):
        workflow_run.status = WorkflowStatus.needs_review
    elif full_state.get("needs_clarification"):
        workflow_run.status = WorkflowStatus.needs_clarification
    else:
        workflow_run.status = WorkflowStatus.completed

    full_state["status"] = workflow_run.status.value
    workflow_run.state = dict(full_state)
    db.commit()
    return workflow_run
```

(`current_step` is left as whatever the stream loop already set it to — the
real last node that ran — not overwritten, same as the spec's reasoning for
dropping the old `"document_agent"` placeholder.)

- [ ] **Step 5: Also seed `needs_clarification: False` in the initial state**

`app/workflow_runner.py` lines 26-39 currently:

```python
    initial_state = {
        "workflow_run_id": str(workflow_run.id),
        "patient_id": patient_id,
        "user_id": user_id,
        "request_text": request_text,
        "uploaded_files": uploaded_files or [],
        "intent": None,
        "department_id": None,
        "appointment_id": None,
        "document_ids": [],
        "reminder_ids": [],
        "escalation": None,
        "status": "running",
    }
```

Add one key:

```python
    initial_state = {
        "workflow_run_id": str(workflow_run.id),
        "patient_id": patient_id,
        "user_id": user_id,
        "request_text": request_text,
        "uploaded_files": uploaded_files or [],
        "intent": None,
        "department_id": None,
        "appointment_id": None,
        "document_ids": [],
        "reminder_ids": [],
        "escalation": None,
        "status": "running",
        "needs_clarification": False,
    }
```

- [ ] **Step 6: Run the full workflow_runner test file to verify all pass**

Run: `pytest tests/test_workflow_runner.py -v`
Expected: PASS (all tests, including `test_emergency_request_ends_needs_review_with_escalation_row`,
`test_administrative_request_reaches_routing_boundary_with_intent_set`,
`test_unroutable_request_ends_needs_review_without_booking`,
`test_unhandled_node_exception_marks_workflow_failed` — none of these are
touched by this task's change, verifying no regression)

- [ ] **Step 7: Commit**

```bash
git add app/workflow_runner.py tests/test_workflow_runner.py
git commit -m "feat: resolve workflow status to needs_clarification/completed, drop stale placeholder"
```

---

### Task 4: `continue_as_booking` and `continue_as_staff_escalation`

**Files:**
- Modify: `app/workflow_runner.py` (add imports + two new functions at end
  of file)
- Modify: `tests/test_workflow_runner.py` (new tests + imports)

**Interfaces:**
- Consumes: `routing_agent_node(state, config) -> dict` (from
  `app.agents.routing`, existing, unchanged), `appointment_agent_node(state, config) -> dict`
  (from `app.agents.appointment`, existing, unchanged), `create_escalation(db, workflow_run_id, reason) -> dict`
  (from `app.tools.escalation_tools`, existing, unchanged), `SessionLocal`
  (from `app.db`, already imported in this file).
- Produces: `continue_as_booking(db, workflow_run: WorkflowRun) -> WorkflowRun`,
  `continue_as_staff_escalation(db, workflow_run: WorkflowRun, reason: str) -> WorkflowRun` —
  both consumed by Task 6's `/clarify` route.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_workflow_runner.py`. First extend the existing imports —
the current top of the file is:

```python
import uuid

from app.models import Appointment, AppointmentSlot, AuditEvent, Escalation, SlotStatus, WorkflowStatus
from app.workflow_runner import run_workflow
from tests.fakes import (
    FakeToolCallingModel,
    ai_message_text,
    ai_message_with_tool_call,
    make_appointment_slot,
    make_department,
    make_doctor,
    make_patient_profile,
    make_user,
)
```

Change to:

```python
import uuid

from app.models import Appointment, AppointmentSlot, AuditEvent, Escalation, SlotStatus, WorkflowRun, WorkflowStatus
from app.workflow_runner import continue_as_booking, continue_as_staff_escalation, run_workflow
from tests.fakes import (
    FakeToolCallingModel,
    ai_message_text,
    ai_message_with_tool_call,
    make_appointment_slot,
    make_department,
    make_doctor,
    make_patient_profile,
    make_user,
    make_workflow_run,
)
```

Then add the tests:

```python
def _needs_clarification_run(db_session, profile, user, request_text="book a cardiology appointment"):
    workflow_run = make_workflow_run(db_session, profile=profile)
    workflow_run.status = WorkflowStatus.needs_clarification
    workflow_run.current_step = "needs_clarification"
    workflow_run.state = {
        "workflow_run_id": str(workflow_run.id),
        "patient_id": str(profile.id),
        "user_id": str(user.id),
        "request_text": request_text,
        "uploaded_files": [],
        "intent": "general_inquiry",
        "department_id": None,
        "appointment_id": None,
        "document_ids": [],
        "reminder_ids": [],
        "escalation": None,
        "status": "needs_clarification",
        "needs_clarification": True,
    }
    db_session.commit()
    return workflow_run


def test_continue_as_booking_completes_a_needs_clarification_run(monkeypatch, db_session):
    department = make_department(db_session, name=f"Cardiology {uuid.uuid4().hex[:8]}")
    doctor = make_doctor(db_session, department=department)
    slot = make_appointment_slot(db_session, doctor=doctor)
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)
    workflow_run = _needs_clarification_run(db_session, profile, user)

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

    result = continue_as_booking(db_session, workflow_run)

    assert result.status == WorkflowStatus.completed
    assert result.current_step == "appointment_agent"
    assert result.state["appointment_id"] is not None
    appointment = db_session.query(Appointment).filter(Appointment.id == result.state["appointment_id"]).one()
    assert appointment.status.value == "confirmed"


def test_continue_as_booking_lands_at_needs_review_when_routing_escalates(monkeypatch, db_session):
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)
    workflow_run = _needs_clarification_run(
        db_session, profile, user, request_text="I need to see someone about a rash"
    )

    routing_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("lookup_departments_tool", {"query_hint": "dermatology"}),
            ai_message_text("UNMATCHED"),
        ]
    )
    monkeypatch.setattr("app.agents.routing.get_llm", lambda: routing_model)
    # Deliberately no appointment_model mock: continue_as_booking must stop
    # after routing escalates and never reach appointment_agent_node. If it
    # did, this test would fail loudly (real get_llm() call) instead of
    # silently passing.

    result = continue_as_booking(db_session, workflow_run)

    assert result.status == WorkflowStatus.needs_review
    assert result.current_step == "routing_agent"
    escalation = db_session.query(Escalation).filter(Escalation.workflow_run_id == result.id).one()
    assert "rash" in escalation.reason


def test_continue_as_staff_escalation_creates_escalation_and_marks_needs_review(db_session):
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)
    workflow_run = _needs_clarification_run(db_session, profile, user)

    result = continue_as_staff_escalation(
        db_session, workflow_run, "Patient asked for help with an unclear request: 'what are your hours?'"
    )

    assert result.status == WorkflowStatus.needs_review
    escalation = db_session.query(Escalation).filter(Escalation.workflow_run_id == result.id).one()
    assert "unclear request" in escalation.reason
    assert result.state["escalation"]["id"] == str(escalation.id)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_workflow_runner.py::test_continue_as_booking_completes_a_needs_clarification_run tests/test_workflow_runner.py::test_continue_as_booking_lands_at_needs_review_when_routing_escalates tests/test_workflow_runner.py::test_continue_as_staff_escalation_creates_escalation_and_marks_needs_review -v`
Expected: FAIL with `ImportError: cannot import name 'continue_as_booking'`

- [ ] **Step 3: Add the imports and two functions to `app/workflow_runner.py`**

Current top of `app/workflow_runner.py` (lines 1-5):

```python
import uuid

from app.db import SessionLocal
from app.graph import build_graph
from app.models import WorkflowRun, WorkflowStatus
```

Change to:

```python
import uuid

from app.agents.appointment import appointment_agent_node
from app.agents.routing import routing_agent_node
from app.db import SessionLocal
from app.graph import build_graph
from app.models import WorkflowRun, WorkflowStatus
from app.tools.escalation_tools import create_escalation
```

Append to the end of `app/workflow_runner.py` (after the existing
`run_workflow` function):

```python


def continue_as_booking(db, workflow_run: WorkflowRun) -> WorkflowRun:
    """needs_clarification -> patient chose 'book an appointment'. Runs
    routing_agent_node then appointment_agent_node directly (both
    UNCHANGED from their current behavior - the Appointment agent still
    books automatically the moment it finds a slot, exactly as it does
    today; that only changes in the deferred confirm-before-booking plan).
    Lands at needs_review (if Routing escalates) or completed (if booking
    succeeds or no slots were found - the Appointment agent already
    handles the zero-slots case by replying without booking, unchanged).

    config MUST be built from the SessionLocal registry, never the
    caller's own `db` parameter directly - see docs/memory/gotchas.md,
    "The shared-Session/ToolNode bug (above) almost got reintroduced by a
    design spec". Both node functions called below dispatch tool calls
    through LangGraph's ToolNode, which runs them in a worker thread; a
    bare Session object is not thread-safe, and this exact mistake was
    caught during this spec's own cross-check before any code was written.
    """
    config = {"configurable": {"db": SessionLocal}}
    full_state = dict(workflow_run.state)

    routing_update = routing_agent_node(full_state, config)
    full_state.update(routing_update or {})
    workflow_run.current_step = "routing_agent"
    workflow_run.state = dict(full_state)
    db.commit()

    if full_state.get("escalation"):
        workflow_run.status = WorkflowStatus.needs_review
        full_state["status"] = workflow_run.status.value
        workflow_run.state = dict(full_state)
        db.commit()
        return workflow_run

    appointment_update = appointment_agent_node(full_state, config)
    full_state.update(appointment_update or {})
    workflow_run.current_step = "appointment_agent"
    workflow_run.status = WorkflowStatus.completed
    full_state["status"] = workflow_run.status.value
    workflow_run.state = dict(full_state)
    db.commit()
    return workflow_run


def continue_as_staff_escalation(db, workflow_run: WorkflowRun, reason: str) -> WorkflowRun:
    """Patient clicked 'talk to staff' from the needs_clarification popup.
    Calls create_escalation directly - no LLM call, no ToolNode involved,
    `db` is safe to use directly here (unlike continue_as_booking above)."""
    escalation = create_escalation(db, str(workflow_run.id), reason)
    full_state = dict(workflow_run.state)
    full_state["escalation"] = escalation
    workflow_run.status = WorkflowStatus.needs_review
    workflow_run.current_step = "staff_escalation"
    full_state["status"] = workflow_run.status.value
    workflow_run.state = full_state
    db.commit()
    return workflow_run
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_workflow_runner.py -v`
Expected: PASS (all tests, including the three new ones and every
pre-existing one from Task 3)

- [ ] **Step 5: Commit**

```bash
git add app/workflow_runner.py tests/test_workflow_runner.py
git commit -m "feat: add continue_as_booking and continue_as_staff_escalation"
```

---

### Task 5: `_render_patient_message` + wire into `GET /requests/{id}`

**Files:**
- Modify: `app/routes/request_routes.py:1-14` (imports), add helper after
  `_get_or_create_profile` (currently ends at line 35), modify
  `request_status` route (currently lines 87-102)
- Test: `tests/test_request_routes.py`

**Interfaces:**
- Produces: `_render_patient_message(db: Session, user: User, workflow_run: WorkflowRun) -> str`,
  consumed by the `GET /requests/{id}` route in this task and reused as-is
  by Task 6's `POST /clarify` redirect target (same GET route).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_request_routes.py`. Extend the existing imports — the
current top of the file is:

```python
import os
import uuid

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.auth import hash_password
from app.main import app
from app.models import Appointment, PatientDocument, PatientProfile, User, UserRole, WorkflowRun
from tests.fakes import (
    FakeToolCallingModel,
    ai_message_text,
    ai_message_with_tool_call,
    make_appointment_slot,
    make_department,
    make_doctor,
    make_workflow_run,
)
```

Change to:

```python
import os
import uuid

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.auth import hash_password
from app.main import app
from app.models import Appointment, PatientDocument, PatientProfile, User, UserRole, WorkflowRun, WorkflowStatus
from app.routes.request_routes import _render_patient_message
from tests.fakes import (
    FakeToolCallingModel,
    ai_message_text,
    ai_message_with_tool_call,
    make_appointment,
    make_appointment_slot,
    make_department,
    make_doctor,
    make_patient_profile,
    make_user,
    make_workflow_run,
)
```

Then add the tests:

```python
def test_render_patient_message_needs_clarification(db_session):
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)
    workflow_run = make_workflow_run(db_session, profile=profile)
    workflow_run.status = WorkflowStatus.needs_clarification
    workflow_run.state = {"document_ids": []}
    db_session.commit()

    message = _render_patient_message(db_session, user, workflow_run)
    assert message == f"Hi {user.name}! I want to make sure I help you with the right thing."


def test_render_patient_message_completed_with_appointment(db_session):
    department = make_department(db_session, name=f"Neurology {uuid.uuid4().hex[:8]}")
    doctor = make_doctor(db_session, department=department)
    slot = make_appointment_slot(db_session, doctor=doctor)
    appointment = make_appointment(db_session, doctor=doctor, slot=slot)
    user = make_user(db_session)
    workflow_run = make_workflow_run(db_session)
    workflow_run.status = WorkflowStatus.completed
    workflow_run.state = {"appointment_id": str(appointment.id), "document_ids": []}
    db_session.commit()

    message = _render_patient_message(db_session, user, workflow_run)
    formatted_time = slot.start_time.strftime("%B %d, %Y at %I:%M %p")
    assert message == (
        f"Great news, {user.name}! You're booked with {doctor.name} "
        f"in {department.name} on {formatted_time}."
    )


def test_render_patient_message_completed_without_appointment(db_session):
    user = make_user(db_session)
    workflow_run = make_workflow_run(db_session)
    workflow_run.status = WorkflowStatus.completed
    workflow_run.state = {"appointment_id": None, "document_ids": []}
    db_session.commit()

    message = _render_patient_message(db_session, user, workflow_run)
    assert message == "I couldn't find any open slots right now. Please try again later or contact our staff."


def test_render_patient_message_needs_review_hides_internal_reason(db_session):
    user = make_user(db_session)
    workflow_run = make_workflow_run(db_session)
    workflow_run.status = WorkflowStatus.needs_review
    workflow_run.state = {"escalation": {"reason": "secret internal reason"}, "document_ids": []}
    db_session.commit()

    message = _render_patient_message(db_session, user, workflow_run)
    assert message == "I've passed your request to our staff team - they'll follow up with you soon."
    assert "secret internal reason" not in message


def test_render_patient_message_failed(db_session):
    user = make_user(db_session)
    workflow_run = make_workflow_run(db_session)
    workflow_run.status = WorkflowStatus.failed
    workflow_run.state = {"document_ids": []}
    db_session.commit()

    message = _render_patient_message(db_session, user, workflow_run)
    assert message == (
        "Something went wrong on our end while handling your request. Please try again, or contact our staff directly."
    )


def test_render_patient_message_running_fallback(db_session):
    user = make_user(db_session)
    workflow_run = make_workflow_run(db_session)
    workflow_run.status = WorkflowStatus.running
    workflow_run.state = {"document_ids": []}
    db_session.commit()

    message = _render_patient_message(db_session, user, workflow_run)
    assert message == "I'm still working on this - check back in a moment."


def test_render_patient_message_appends_document_clause(db_session):
    user = make_user(db_session)
    workflow_run = make_workflow_run(db_session)
    workflow_run.status = WorkflowStatus.needs_review
    workflow_run.state = {"document_ids": ["doc-1"]}
    db_session.commit()

    message = _render_patient_message(db_session, user, workflow_run)
    assert message == (
        "I've passed your request to our staff team - they'll follow up with you soon."
        " I've also saved your uploaded document."
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_request_routes.py::test_render_patient_message_needs_clarification -v`
Expected: FAIL with `ImportError: cannot import name '_render_patient_message'`

- [ ] **Step 3: Add the helper and imports to `app/routes/request_routes.py`**

Current imports (lines 1-14):

```python
import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import PatientProfile, User, UserRole, WorkflowRun
from app.rbac import require_role
from app.workflow_runner import run_workflow
```

Change the `app.models` and `app.workflow_runner` import lines to:

```python
from app.models import Appointment, AppointmentSlot, Department, Doctor, PatientProfile, User, UserRole, WorkflowRun, WorkflowStatus
from app.rbac import require_role
from app.workflow_runner import continue_as_booking, continue_as_staff_escalation, run_workflow
```

Add the helper function after `_get_or_create_profile` (which currently
ends at line 35, right before the blank line and `@router.get("/requests/new"...)`):

```python
def _render_patient_message(db: Session, user: User, workflow_run: WorkflowRun) -> str:
    """Plain Python, not an LLM call - built from real persisted rows, same
    principle as CLAUDE.md's "no hardcoded final responses": the confirmation
    text is rendered from rows just read back from the database, never a
    free-standing LLM string asserting success. Never exposes raw escalation
    reasons or ids to the patient - those stay in the DB/audit trail for staff."""
    state = workflow_run.state or {}
    workflow_status = workflow_run.status

    if workflow_status == WorkflowStatus.needs_clarification:
        message = f"Hi {user.name}! I want to make sure I help you with the right thing."
    elif workflow_status == WorkflowStatus.completed:
        appointment_id = state.get("appointment_id")
        if appointment_id:
            appointment = db.get(Appointment, uuid.UUID(appointment_id))
            doctor = db.get(Doctor, appointment.doctor_id)
            department = db.get(Department, doctor.department_id)
            slot = db.get(AppointmentSlot, appointment.slot_id)
            formatted_time = slot.start_time.strftime("%B %d, %Y at %I:%M %p")
            message = (
                f"Great news, {user.name}! You're booked with {doctor.name} "
                f"in {department.name} on {formatted_time}."
            )
        else:
            message = "I couldn't find any open slots right now. Please try again later or contact our staff."
    elif workflow_status == WorkflowStatus.needs_review:
        message = "I've passed your request to our staff team - they'll follow up with you soon."
    elif workflow_status == WorkflowStatus.failed:
        message = (
            "Something went wrong on our end while handling your request. Please try again, or contact our staff directly."
        )
    else:
        message = "I'm still working on this - check back in a moment."

    if state.get("document_ids"):
        message += " I've also saved your uploaded document."

    return message
```

- [ ] **Step 4: Wire the helper into the `GET /requests/{id}` route**

Current route (lines 87-102):

```python
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

Change the last two lines to:

```python
    profile = _get_or_create_profile(db, user)
    if workflow_run.patient_id != profile.id:
        raise HTTPException(status_code=403, detail="Not your request")

    patient_message = _render_patient_message(db, user, workflow_run)
    return templates.TemplateResponse(
        request,
        "request_status.html",
        {"user": user, "workflow_run": workflow_run, "patient_message": patient_message},
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_request_routes.py -k render_patient_message -v`
Expected: PASS (7 tests)

- [ ] **Step 6: Commit**

```bash
git add app/routes/request_routes.py tests/test_request_routes.py
git commit -m "feat: add _render_patient_message and wire into GET /requests/{id}"
```

---

### Task 6: `POST /requests/{id}/clarify` route

**Files:**
- Modify: `app/routes/request_routes.py` (add new route at end of file)
- Test: `tests/test_request_routes.py`

**Interfaces:**
- Consumes: `continue_as_booking`, `continue_as_staff_escalation` (Task 4),
  `WorkflowStatus.needs_clarification` (Task 1).
- Produces: `POST /requests/{workflow_run_id}/clarify` route, consumed by
  Task 7's template form actions.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_request_routes.py`:

```python
def test_clarify_book_appointment_choice_runs_booking_and_redirects(monkeypatch, db_session):
    department = make_department(db_session, name=f"Cardiology {uuid.uuid4().hex[:8]}")
    doctor = make_doctor(db_session, department=department)
    slot = make_appointment_slot(db_session, doctor=doctor)

    safety_model = FakeToolCallingModel([ai_message_text("SAFE")])
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: safety_model)
    coordinator_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("get_or_create_patient_tool", {"profile_fields": {}}),
            ai_message_text("general_inquiry"),
        ]
    )
    monkeypatch.setattr("app.agents.coordinator.get_llm", lambda: coordinator_model)

    cookie = _register_patient("Clarify Patient")
    client.cookies.set("agentcare_session", cookie)

    resp = client.post("/requests/new", data={"request_text": "what are your hours?"}, follow_redirects=False)
    workflow_run_id = resp.headers["location"].rsplit("/", 1)[-1]

    status_resp = client.get(resp.headers["location"])
    assert "I want to make sure I help you with the right thing" in status_resp.text
    assert "Book an appointment" in status_resp.text
    assert "Talk to hospital staff" in status_resp.text

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

    clarify_resp = client.post(
        f"/requests/{workflow_run_id}/clarify", data={"choice": "book_appointment"}, follow_redirects=False
    )
    assert clarify_resp.status_code == 303
    assert clarify_resp.headers["location"] == f"/requests/{workflow_run_id}"

    workflow_run = db_session.get(WorkflowRun, workflow_run_id)
    assert workflow_run.status.value == "completed"
    assert workflow_run.state["appointment_id"] is not None


def test_clarify_staff_choice_escalates(monkeypatch, db_session):
    safety_model = FakeToolCallingModel([ai_message_text("SAFE")])
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: safety_model)
    coordinator_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("get_or_create_patient_tool", {"profile_fields": {}}),
            ai_message_text("general_inquiry"),
        ]
    )
    monkeypatch.setattr("app.agents.coordinator.get_llm", lambda: coordinator_model)

    cookie = _register_patient("Staff Choice Patient")
    client.cookies.set("agentcare_session", cookie)
    resp = client.post("/requests/new", data={"request_text": "what are your hours?"}, follow_redirects=False)
    workflow_run_id = resp.headers["location"].rsplit("/", 1)[-1]

    clarify_resp = client.post(
        f"/requests/{workflow_run_id}/clarify", data={"choice": "staff"}, follow_redirects=False
    )
    assert clarify_resp.status_code == 303
    assert clarify_resp.headers["location"] == f"/requests/{workflow_run_id}"

    workflow_run = db_session.get(WorkflowRun, workflow_run_id)
    assert workflow_run.status.value == "needs_review"
    from app.models import Escalation

    escalation = db_session.query(Escalation).filter(Escalation.workflow_run_id == workflow_run.id).one()
    assert "unclear request" in escalation.reason


def test_clarify_wrong_owner_returns_403(monkeypatch, db_session):
    safety_model = FakeToolCallingModel([ai_message_text("SAFE")])
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: safety_model)
    coordinator_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("get_or_create_patient_tool", {"profile_fields": {}}),
            ai_message_text("general_inquiry"),
        ]
    )
    monkeypatch.setattr("app.agents.coordinator.get_llm", lambda: coordinator_model)

    cookie_a = _register_patient("Owner A")
    client.cookies.set("agentcare_session", cookie_a)
    resp = client.post("/requests/new", data={"request_text": "what are your hours?"}, follow_redirects=False)
    workflow_run_id = resp.headers["location"].rsplit("/", 1)[-1]

    cookie_b = _register_patient("Owner B")
    client.cookies.set("agentcare_session", cookie_b)
    clarify_resp = client.post(
        f"/requests/{workflow_run_id}/clarify", data={"choice": "staff"}, follow_redirects=False
    )
    assert clarify_resp.status_code == 403


def test_clarify_nonexistent_run_returns_404():
    cookie = _register_patient("Ghost Patient")
    client.cookies.set("agentcare_session", cookie)
    resp = client.post(f"/requests/{uuid.uuid4()}/clarify", data={"choice": "staff"}, follow_redirects=False)
    assert resp.status_code == 404


def test_clarify_stale_status_is_a_noop_redirect(monkeypatch, db_session):
    safety_model = FakeToolCallingModel([ai_message_text("SAFE")])
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: safety_model)
    coordinator_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("get_or_create_patient_tool", {"profile_fields": {}}),
            ai_message_text("general_inquiry"),
        ]
    )
    monkeypatch.setattr("app.agents.coordinator.get_llm", lambda: coordinator_model)

    cookie = _register_patient("Stale Patient")
    client.cookies.set("agentcare_session", cookie)
    resp = client.post("/requests/new", data={"request_text": "what are your hours?"}, follow_redirects=False)
    workflow_run_id = resp.headers["location"].rsplit("/", 1)[-1]

    first = client.post(f"/requests/{workflow_run_id}/clarify", data={"choice": "staff"}, follow_redirects=False)
    assert first.status_code == 303

    from app.models import Escalation

    count_before = db_session.query(Escalation).filter(Escalation.workflow_run_id == workflow_run_id).count()
    assert count_before == 1

    second = client.post(f"/requests/{workflow_run_id}/clarify", data={"choice": "staff"}, follow_redirects=False)
    assert second.status_code == 303
    assert second.headers["location"] == f"/requests/{workflow_run_id}"

    count_after = db_session.query(Escalation).filter(Escalation.workflow_run_id == workflow_run_id).count()
    assert count_after == 1


def test_clarify_invalid_choice_returns_400(monkeypatch, db_session):
    safety_model = FakeToolCallingModel([ai_message_text("SAFE")])
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: safety_model)
    coordinator_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("get_or_create_patient_tool", {"profile_fields": {}}),
            ai_message_text("general_inquiry"),
        ]
    )
    monkeypatch.setattr("app.agents.coordinator.get_llm", lambda: coordinator_model)

    cookie = _register_patient("BadChoice Patient")
    client.cookies.set("agentcare_session", cookie)
    resp = client.post("/requests/new", data={"request_text": "what are your hours?"}, follow_redirects=False)
    workflow_run_id = resp.headers["location"].rsplit("/", 1)[-1]

    bad = client.post(f"/requests/{workflow_run_id}/clarify", data={"choice": "nonsense"}, follow_redirects=False)
    assert bad.status_code == 400
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_request_routes.py -k clarify -v`
Expected: FAIL with 404s/connection errors — the route doesn't exist yet
(FastAPI returns 404 for unmatched routes, which will make the 303/403/400
assertions fail).

- [ ] **Step 3: Add the route**

Append to the end of `app/routes/request_routes.py`:

```python


@router.post("/requests/{workflow_run_id}/clarify")
def clarify_request(
    workflow_run_id: str,
    choice: str = Form(...),
    user: User = Depends(require_role(UserRole.patient.value)),
    db: Session = Depends(get_db),
):
    workflow_run = db.get(WorkflowRun, workflow_run_id)
    if workflow_run is None:
        raise HTTPException(status_code=404, detail="Not found")

    profile = _get_or_create_profile(db, user)
    if workflow_run.patient_id != profile.id:
        raise HTTPException(status_code=403, detail="Not your request")

    if workflow_run.status != WorkflowStatus.needs_clarification:
        # Stale click (already resolved by an earlier click, a second tab,
        # or a double-submit) - same no-op-redirect philosophy as the
        # existing duplicate-submission guard on POST /requests/new, not an
        # error.
        return RedirectResponse(f"/requests/{workflow_run_id}", status_code=status.HTTP_303_SEE_OTHER)

    if choice == "book_appointment":
        continue_as_booking(db, workflow_run)
    elif choice == "staff":
        request_text = workflow_run.state.get("request_text")
        continue_as_staff_escalation(
            db, workflow_run, f"Patient asked for help with an unclear request: {request_text!r}"
        )
    else:
        raise HTTPException(status_code=400, detail="Unknown choice")

    return RedirectResponse(f"/requests/{workflow_run_id}", status_code=status.HTTP_303_SEE_OTHER)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_request_routes.py -k clarify -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add app/routes/request_routes.py tests/test_request_routes.py
git commit -m "feat: add POST /requests/{id}/clarify route"
```

---

### Task 7: `request_status.html` template + final regression pass

**Files:**
- Modify: `app/templates/request_status.html` (full file rewrite, 16 lines)
- Modify: `tests/test_request_routes.py` (update
  `test_patient_submits_request_and_sees_real_booking_result` assertions)

**Interfaces:**
- Consumes: `patient_message` (Task 5) and `workflow_run` template context
  variables from `GET /requests/{id}`; form actions target
  `POST /requests/{id}/clarify` (Task 6).

- [ ] **Step 1: Update the regression test assertions first**

`tests/test_request_routes.py`'s `test_patient_submits_request_and_sees_real_booking_result`
currently (around lines 134-143) asserts:

```python
    status_resp = client.get(location)
    assert status_resp.status_code == 200
    assert "running" in status_resp.text
    assert "document_agent" in status_resp.text

    workflow_run_id = location.rsplit("/", 1)[-1]
    workflow_run = db_session.get(WorkflowRun, workflow_run_id)
    assert workflow_run.status.value == "running"
    appointment = db_session.query(Appointment).filter(Appointment.id == workflow_run.state["appointment_id"]).one()
    assert appointment.status.value == "confirmed"
```

Change to:

```python
    status_resp = client.get(location)
    assert status_resp.status_code == 200
    assert f"You're booked with {doctor.name} in {department.name}" in status_resp.text

    workflow_run_id = location.rsplit("/", 1)[-1]
    workflow_run = db_session.get(WorkflowRun, workflow_run_id)
    assert workflow_run.status.value == "completed"
    appointment = db_session.query(Appointment).filter(Appointment.id == workflow_run.state["appointment_id"]).one()
    assert appointment.status.value == "confirmed"
```

(`doctor` and `department` are already in scope earlier in this test —
created at the top via `make_department`/`make_doctor`.)

- [ ] **Step 2: Run to verify it fails against the current template**

Run: `pytest tests/test_request_routes.py::test_patient_submits_request_and_sees_real_booking_result -v`
Expected: FAIL — the current template still prints the raw field dump, not
the "You're booked with..." sentence, and `workflow_run.status.value` is
still `"running"` prior to Task 3 (already fixed by Task 3, but the
template text won't appear until this task's template rewrite).

- [ ] **Step 3: Rewrite `app/templates/request_status.html`**

Current file (16 lines):

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
    <li>Documents: {{ workflow_run.state.get("document_ids") }}</li>
    <li>Escalation: {{ workflow_run.state.get("escalation") }}</li>
</ul>
<a href="/requests/new">Submit another request</a>
{% endblock %}
```

Replace with:

```html
{% extends "base.html" %}
{% block title %}Request Status - AgentCare{% endblock %}
{% block content %}
<h1>Request Status</h1>
<p>{{ patient_message }}</p>
{% if workflow_run.status.value == "needs_clarification" %}
<div>
    <form method="post" action="/requests/{{ workflow_run.id }}/clarify" style="display:inline">
        <input type="hidden" name="choice" value="book_appointment">
        <button type="submit">Book an appointment</button>
    </form>
    <form method="post" action="/requests/{{ workflow_run.id }}/clarify" style="display:inline">
        <input type="hidden" name="choice" value="staff">
        <button type="submit">Talk to hospital staff</button>
    </form>
</div>
{% endif %}
<a href="/requests/new">Submit another request</a>
{% endblock %}
```

- [ ] **Step 4: Run the full request-routes and workflow-runner suites**

Run: `pytest tests/test_request_routes.py tests/test_workflow_runner.py tests/test_graph.py tests/test_models.py -v`
Expected: PASS — every test in all four files, including the untouched
pre-existing tests (`test_submitting_request_with_attached_file_saves_it_to_disk_and_creates_document_row`,
`test_submitting_same_document_bytes_twice_does_not_create_a_second_row`,
`test_resubmitting_the_same_request_quickly_does_not_run_the_workflow_twice`,
`test_patient_cannot_view_another_patients_request`, etc.) — these assert
only on document/appointment rows and counts, not on status text, so they
are unaffected by the template change. If any of them fail, the failure
means intent routing broke something beyond what this plan intended —
stop and diagnose before continuing (per `superpowers:systematic-debugging`),
don't patch the assertion to match unexpected behavior.

- [ ] **Step 5: Run the full test suite**

Run: `pytest -v`
Expected: PASS, all tests (81 pre-existing + this plan's new tests).

- [ ] **Step 6: Commit**

```bash
git add app/templates/request_status.html tests/test_request_routes.py
git commit -m "feat: render human-readable status page with clarify buttons"
```

---

## Self-Review

### 1. Spec coverage

Walking `docs/superpowers/specs/2026-07-27-intent-branching-clarification-design.md`
section by section:

- **§1 Goal, item 1 ("intent is unclear")** — covered (Tasks 2, 3, 6, 7).
- **§1 Goal, item 2 ("candidate slot found / confirm before booking")** —
  **deliberately out of scope.** This is pause point (B), sequenced by the
  user as its own later plan (`docs/memory/status.md`). No task here
  touches `app/agents/appointment.py`'s LLM logic, removes
  `book_or_modify_appointment_tool` from the Appointment agent's tools,
  adds `available_slots`/`candidate_slot_id`, or adds
  `WorkflowStatus.needs_booking_confirmation`. The Appointment agent is
  used only as an unchanged black box via `appointment_agent_node` in
  `continue_as_booking` (Task 4).
- **§2 Scope, graph branching after coordinator_agent** — **implemented
  differently than the spec's literal text, per the user's corrected
  pseudocode**: this plan branches after `document_agent`, not
  `coordinator_agent`, because the actual current graph (already built by
  the Document agent phase) runs `document_agent` between `coordinator_agent`
  and `routing_agent`. Task 2 documents this explicitly.
- **§2 Scope, "two new WorkflowStatus values (one migration)"** — only one
  value (`needs_clarification`) is added; `needs_booking_confirmation` is
  out of scope per the above. Task 1.
- **§2 Scope, "request_status.html shows two buttons ... wording specific
  to which one it is"** — only the `needs_clarification` wording/buttons
  are built; there is no `needs_booking_confirmation` branch in the
  template. Task 7.
- **§2 Scope, "two new routes"** — only `POST /requests/{id}/clarify` is
  built; `POST /requests/{id}/confirm-booking` is out of scope. Task 6.
- **§2 Scope, "human-readable wording ... replacing raw field dumps"** —
  covered for every status this plan can produce
  (`needs_clarification`/`completed` with and without an appointment/`needs_review`/`failed`/fallback),
  explicitly excluding a `needs_booking_confirmation` wording branch. Task 5.
- **§2 Explicitly out of scope items (true popup/modal, multi-step guided
  booking, `submit_document`/reschedule/cancel as their own clarify
  options, Safety/Coordinator prompt changes)** — all still out of scope in
  this plan; nothing here builds any of them.
- **§3 Architecture, graph change** — covered (Task 2), with the
  after-`document_agent` correction noted above.
- **§3 Architecture, "Appointment agent stops before booking"** — entirely
  **out of scope** (pause point B). `appointment_capture_node`,
  `AppointmentState.available_slots`, `appointment_finalize_node`, and the
  `candidate_slot_id` return value described in the spec are not built.
- **§3 Architecture, workflow runner status resolution** — covered with
  the two-branch (not three-branch) version specified by the corrected
  pseudocode (Task 3).
- **§3 Architecture, `continue_as_booking`** — covered (Task 4), including
  the `SessionLocal`-vs-`db` gotcha warning verbatim from
  `docs/memory/gotchas.md`.
- **§3 Architecture, `commit_confirmed_booking`** — **out of scope**
  (pause point B only). Not built.
- **§3 Architecture, `continue_as_staff_escalation`** — covered (Task 4),
  used only from the `/clarify` route's `staff` choice — the spec's other
  use ("from `needs_booking_confirmation`'s decline option") does not
  apply since that flow doesn't exist in this plan.
- **§3 Architecture, routes** — only the `/clarify` route half is built
  (Task 6); `/confirm-booking` is out of scope.
- **§3 Architecture, wording** — covered (Task 5) minus the
  `needs_booking_confirmation` branch, as above. The Document agent's
  "append a clause when `document_ids` is non-empty" note is implemented,
  matching what actually shipped in the Document agent build (confirmed by
  reading `app/routes/request_routes.py` in full before writing Task 5 —
  no `_render_patient_message` existed yet, so this plan is the first to
  create it, exactly as the task description anticipated).
- **§4 Data flow** — steps 1-4 covered; step 5's "or `/confirm-booking`"
  half is out of scope.
- **§5 Error handling** — the `require_role`/ownership/404/stale-no-op
  pattern is covered for `/clarify` (Task 6). The `commit_confirmed_booking`
  re-validation note is out of scope (no such function in this plan).
- **§6 Testing** — graph routing, workflow_runner terminal statuses
  (excluding `needs_booking_confirmation`), routes (happy path + 403 + 404
  + stale no-op), and the regression check are all covered (Tasks 2-7). The
  spec's "Appointment agent: check_slot_availability returns slots → model
  proposes one → candidate_slot_id set..." testing bullet is out of scope
  (pause point B).
- **§7 Open items resolved during self-review** — the `SessionLocal`
  warning is carried into Task 4's docstring verbatim; the other bullets
  (`book_or_modify_appointment_tool` removal, `available_slots` naming)
  are pause-point-B-only and don't apply here.

### 2. Placeholder scan

Searched every task for "TBD"/"TODO"/"implement later"/"add appropriate
error handling"/"similar to Task N"/prose-only steps — none found. Every
code step above has complete, runnable code (no ellipses, no
`# ... rest of function`). Every existing-file modification quotes the
real current content (verified by reading each file in full before writing
this plan) alongside the exact replacement.

### 3. Signature consistency across tasks

- `route_after_document(state) -> Literal["routing_agent", "needs_clarification"]`
  and `needs_clarification_node(state, config) -> dict` — defined in Task 2,
  used identically (same names, same return shape) in Task 3's reasoning
  about `full_state.get("needs_clarification")`.
- `continue_as_booking(db, workflow_run) -> WorkflowRun` and
  `continue_as_staff_escalation(db, workflow_run, reason) -> WorkflowRun` —
  defined in Task 4, imported and called with matching argument order/count
  in Task 6's route.
- `_render_patient_message(db, user, workflow_run) -> str` — defined in
  Task 5, imported directly (not re-declared) in Task 6's test file and
  consumed via the `patient_message` template variable name used
  consistently in both Task 5's route change and Task 7's template.
- `WorkflowStatus.needs_clarification` — defined in Task 1, referenced by
  identical dotted name in Tasks 2, 3, 4, 5, 6, 7 (no `needs_clarify` or
  other drift found).
- Template form field name `choice` with values `"book_appointment"`/`"staff"` —
  consistent between Task 6's route (`choice: str = Form(...)`, checks
  `choice == "book_appointment"` / `choice == "staff"`) and Task 7's
  template (`<input type="hidden" name="choice" value="book_appointment">` /
  `value="staff"`).

No issues found; nothing required fixing during this pass.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-27-intent-branching-popup.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
