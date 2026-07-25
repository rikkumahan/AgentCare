# Routing + Appointment Agents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the 2-node parent graph (`safety_agent` → `coordinator_agent`) with a `routing_agent` and an `appointment_agent`, so a non-emergency booking request is classified into a real `Department`, matched to a real open `AppointmentSlot`, and turned into a real, conflict-checked `Appointment` row — no stubs, no hardcoded outcomes.

**Architecture:** Same subgraph-per-agent shape as Safety/Coordinator (`docs/memory/decisions.md` → "Each agent is a private LangGraph subgraph"): each new agent is one parent-graph node backed by its own compiled subgraph with private `messages`. Routing's only tool is read-only (`lookup_departments`), so — unlike Coordinator's patient_id, which comes from a write-tool's artifact — its finalize step gets the department by exact name from the LLM's own final reply, then the parent-facing wrapper resolves that name to a real `department_id` via a case-insensitive DB lookup (never trusts an LLM-transcribed UUID). When Routing can't resolve a department, the wrapper calls the plain `create_escalation()` function directly (the same audited function Safety's tool uses) instead of looping back into the Safety LLM node — a loop-back risks an infinite cycle (the same unroutable request would never resolve) and wrongly couples Routing's failure mode into Safety's diagnosis/emergency-focused prompt. Appointment's LLM turn binds two tools (`check_slot_availability`, `book_or_modify_appointment`) in a Coordinator-style loop (tool → capture → back to LLM) since it needs multiple turns: look, then book.

**Tech Stack:** Same as Phase 2 — LangGraph `StateGraph`/`ToolNode`/`InjectedState`, `langchain-groq` `ChatGroq`, SQLAlchemy 2.x, pytest with the LLM mocked via `tests/fakes.FakeToolCallingModel`.

## Global Constraints

- Persistent SQL only — no in-memory dicts/session vars for domain data (CLAUDE.md).
- Never diagnose, prescribe, or claim clinical judgment; Department Routing's prompt is scoped to administrative mapping only (design spec §5.3, §8).
- No tool may return a fixed response regardless of input — every tool does real DB logic (CLAUDE.md).
- Every tool function is wrapped by `@audited(action, entity_type)` (`app/audit.py`) — writes an `AuditEvent` row on every call, success or failure.
- No hardcoded final responses — deferred this phase by design decision: there is no UI yet to show a confirmation to (Phase 6), so no agent asserts success in free text this phase; `WorkflowState`/`WorkflowRun` stay structured-data-only.
- Each agent has its own system prompt (module-level constant) and its own bound tool(s) — never shared across agents (CLAUDE.md).
- Groq calls go through `invoke_with_retry` (`app/llm.py`) — already built, reuse as-is.
- Tests: pytest, mock the LLM (`FakeToolCallingModel`), assert real DB state changes — don't test prompt wording.
- `book_or_modify_appointment`'s `reschedule`/`cancel` actions are fully implemented and tested this phase, but the Appointment agent's **prompt** only drives `action="book"` — there is no tool yet (and none is in the approved 8-tool list) for the LLM to look up an existing appointment's id for this workflow run, and design spec §7 shows reschedule/cancel as direct routes (`POST /appointments/{id}/reschedule`) that will call this same tool with a known id from the URL, not through the LLM. Wiring the agent prompt to drive reschedule/cancel is out of scope until those routes exist.

---

### Task 1: Department lookup tool

**Files:**
- Create: `app/tools/department_tools.py`
- Modify: `tests/fakes.py` (add `make_department`)
- Test: `tests/test_department_tools.py`

**Interfaces:**
- Consumes: `app.audit.audited` (existing decorator), `app.models.Department`.
- Produces: `lookup_departments(db: Session, query_hint: str) -> list[dict]` (each dict: `{"id": str, "name": str, "description": str | None}`), `lookup_departments_tool` (`@tool(response_format="content_and_artifact")`, model-facing arg `query_hint: str`, no injected state). Task 3 binds `lookup_departments_tool` to the Routing agent.

- [ ] **Step 1: Add `make_department` fake**

Add to `tests/fakes.py` (keep existing content, add this import and function):

```python
# add to the existing `from app.models import ...` import line:
from app.models import Department, PatientProfile, User, UserRole, WorkflowRun
```

```python
def make_department(db_session, name: str | None = None, active: bool = True) -> Department:
    department = Department(
        name=name or f"Dept-{uuid.uuid4().hex[:8]}",
        description="Test department",
        active=active,
    )
    db_session.add(department)
    db_session.commit()
    return department
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_department_tools.py`:

```python
import uuid

from app.models import AuditEvent
from app.tools.department_tools import lookup_departments
from tests.fakes import make_department


def test_lookup_departments_filters_by_hint(db_session):
    token = uuid.uuid4().hex[:8]
    cardiology = make_department(db_session, name=f"Cardiology {token}")
    make_department(db_session, name=f"General Medicine {uuid.uuid4().hex[:8]}")

    result = lookup_departments(db_session, f"need {token} please")

    ids = {d["id"] for d in result}
    assert str(cardiology.id) in ids
    assert len(result) == 1


def test_lookup_departments_falls_back_to_full_list_when_hint_matches_nothing(db_session):
    token = uuid.uuid4().hex[:8]
    cardiology = make_department(db_session, name=f"Cardiology {token}")
    general = make_department(db_session, name=f"General Medicine {token}")

    result = lookup_departments(db_session, f"unmatched-hint-{uuid.uuid4().hex[:8]}")

    ids = {d["id"] for d in result}
    assert str(cardiology.id) in ids
    assert str(general.id) in ids


def test_lookup_departments_excludes_inactive_departments(db_session):
    token = uuid.uuid4().hex[:8]
    active_dept = make_department(db_session, name=f"Active Dept {token}", active=True)
    inactive_dept = make_department(db_session, name=f"Retired Dept {token}", active=False)

    result = lookup_departments(db_session, "")

    ids = {d["id"] for d in result}
    assert str(active_dept.id) in ids
    assert str(inactive_dept.id) not in ids


def test_lookup_departments_writes_audit_event(db_session):
    make_department(db_session, name=f"Cardiology {uuid.uuid4().hex[:8]}")

    lookup_departments(db_session, "cardiology")

    audit_actions = {e.action for e in db_session.query(AuditEvent).all()}
    assert "lookup_departments" in audit_actions
```

Note: `db_session` does not roll back between tests in this project (per-session persistence, confirmed by an actual test run — see `docs/memory/gotchas.md`). Any table with a UNIQUE column (like `Department.name`) needs a fresh unique value per test, and any assertion against a query with no natural scoping key (like `lookup_departments`, which returns *all* active departments) must assert membership/non-membership of the ids this test created — never an absolute count or exact set, since other tests in the same session may have already inserted their own departments.

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_department_tools.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.tools.department_tools'`

- [ ] **Step 4: Implement `app/tools/department_tools.py`**

```python
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from sqlalchemy.orm import Session

from app.audit import audited
from app.models import Department


@audited("lookup_departments", "Department")
def lookup_departments(db: Session, query_hint: str) -> list[dict]:
    departments = db.query(Department).filter(Department.active.is_(True)).all()

    hint_words = [w for w in (query_hint or "").strip().lower().split() if w]
    if hint_words:
        matched = [
            d
            for d in departments
            if any(w in d.name.lower() or w in (d.description or "").lower() for w in hint_words)
        ]
        if matched:
            departments = matched

    return [{"id": str(d.id), "name": d.name, "description": d.description} for d in departments]


@tool(response_format="content_and_artifact")
def lookup_departments_tool(query_hint: str, config: RunnableConfig):
    """List active hospital departments that might match the patient's
    request. query_hint should be a short phrase describing what the
    request is about (e.g. "chest pain follow-up", "general checkup")."""
    db = config["configurable"]["db"]
    result = lookup_departments(db, query_hint)
    return f"Found {len(result)} department(s)", result
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_department_tools.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Commit**

```bash
git add app/tools/department_tools.py tests/test_department_tools.py tests/fakes.py
git commit -m "Add lookup_departments tool"
```

---

### Task 2: Appointment tools

**Files:**
- Create: `app/tools/appointment_tools.py`
- Modify: `tests/fakes.py` (add `make_doctor`, `make_appointment_slot`)
- Test: `tests/test_appointment_tools.py`

**Interfaces:**
- Consumes: `app.audit.audited`, `app.models.{AppointmentSlot, SlotStatus, Appointment, AppointmentStatus, Doctor}`.
- Produces: `check_slot_availability(db, department_id: str, preferred_window: dict) -> list[dict]` (each dict: `{"slot_id", "doctor_id", "doctor_name", "start_time", "end_time"}`, ISO datetime strings); `book_or_modify_appointment(db, patient_id: str, slot_id: str, action: str, existing_appointment_id: str | None) -> dict` (`{"id", "patient_id"?, "doctor_id"?, "slot_id"?, "status", "start_time"?, "end_time"?, "error"?}` — `status="error"` with an `error` message on any rejected/invalid call, never an exception). `check_slot_availability_tool` (model-facing `preferred_window: dict`; injects `department_id` from subgraph state). `book_or_modify_appointment_tool` (model-facing `slot_id: str, action: str, existing_appointment_id: str | None`; injects `patient_id` from subgraph state). Task 4 binds both to the Appointment agent.

- [ ] **Step 1: Add `make_doctor` / `make_appointment_slot` fakes**

Add to `tests/fakes.py`:

```python
# add to the top-level imports:
from datetime import datetime, timedelta, timezone
```

```python
# add to the existing `from app.models import ...` import line:
from app.models import AppointmentSlot, Department, Doctor, PatientProfile, SlotStatus, User, UserRole, WorkflowRun
```

```python
def make_doctor(db_session, department: Department | None = None, active: bool = True) -> Doctor:
    if department is None:
        department = make_department(db_session)
    doctor = Doctor(department_id=department.id, name=f"Dr. Test-{uuid.uuid4().hex[:8]}", active=active)
    db_session.add(doctor)
    db_session.commit()
    return doctor


def make_appointment_slot(
    db_session,
    doctor: Doctor | None = None,
    start_time: datetime | None = None,
    status: SlotStatus = SlotStatus.open,
) -> AppointmentSlot:
    if doctor is None:
        doctor = make_doctor(db_session)
    start_time = start_time or (datetime.now(timezone.utc) + timedelta(days=1))
    slot = AppointmentSlot(
        doctor_id=doctor.id,
        start_time=start_time,
        end_time=start_time + timedelta(minutes=30),
        status=status,
    )
    db_session.add(slot)
    db_session.commit()
    return slot
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_appointment_tools.py`:

```python
from datetime import datetime, timedelta, timezone

from app.models import Appointment, AppointmentSlot, AuditEvent, Doctor, SlotStatus
from app.tools.appointment_tools import book_or_modify_appointment, check_slot_availability
from tests.fakes import make_appointment_slot, make_department, make_doctor, make_patient_profile


def test_check_slot_availability_returns_open_slots_in_department_within_window(db_session):
    department = make_department(db_session)
    doctor = make_doctor(db_session, department=department)
    slot = make_appointment_slot(db_session, doctor=doctor)

    result = check_slot_availability(db_session, str(department.id), {})

    assert len(result) == 1
    assert result[0]["slot_id"] == str(slot.id)
    assert result[0]["doctor_name"] == doctor.name


def test_check_slot_availability_excludes_booked_slots_and_other_departments(db_session):
    department = make_department(db_session)
    other_department = make_department(db_session)
    doctor = make_doctor(db_session, department=department)
    make_appointment_slot(db_session, doctor=doctor, status=SlotStatus.booked)
    make_appointment_slot(db_session, doctor=make_doctor(db_session, department=other_department))

    result = check_slot_availability(db_session, str(department.id), {})

    assert result == []


def test_check_slot_availability_respects_date_window(db_session):
    department = make_department(db_session)
    doctor = make_doctor(db_session, department=department)
    far_future = datetime.now(timezone.utc) + timedelta(days=60)
    make_appointment_slot(db_session, doctor=doctor, start_time=far_future)

    result = check_slot_availability(db_session, str(department.id), {})

    assert result == []


def test_book_or_modify_appointment_books_open_slot(db_session):
    profile = make_patient_profile(db_session)
    slot = make_appointment_slot(db_session)

    result = book_or_modify_appointment(db_session, str(profile.id), str(slot.id), "book", None)

    assert result["status"] == "confirmed"
    booked_slot = db_session.query(AppointmentSlot).filter(AppointmentSlot.id == slot.id).one()
    assert booked_slot.status == SlotStatus.booked
    appointment = db_session.query(Appointment).filter(Appointment.id == result["id"]).one()
    assert str(appointment.patient_id) == str(profile.id)


def test_book_or_modify_appointment_rejects_conflicting_booking(db_session):
    profile = make_patient_profile(db_session)
    doctor = make_doctor(db_session)
    start = datetime.now(timezone.utc) + timedelta(days=2)
    slot_a = make_appointment_slot(db_session, doctor=doctor, start_time=start)
    slot_b = make_appointment_slot(db_session, doctor=doctor, start_time=start + timedelta(minutes=10))

    first = book_or_modify_appointment(db_session, str(profile.id), str(slot_a.id), "book", None)
    assert first["status"] == "confirmed"

    second = book_or_modify_appointment(db_session, str(profile.id), str(slot_b.id), "book", None)
    assert second["status"] == "error"
    assert "conflicting" in second["error"]


def test_book_or_modify_appointment_reschedule_frees_old_slot_and_books_new(db_session):
    profile = make_patient_profile(db_session)
    old_slot = make_appointment_slot(db_session)
    doctor = db_session.query(Doctor).filter(Doctor.id == old_slot.doctor_id).one()
    new_slot = make_appointment_slot(db_session, doctor=doctor)
    booked = book_or_modify_appointment(db_session, str(profile.id), str(old_slot.id), "book", None)

    result = book_or_modify_appointment(db_session, str(profile.id), str(new_slot.id), "reschedule", booked["id"])

    assert result["status"] == "rescheduled"
    assert result["slot_id"] == str(new_slot.id)
    freed_slot = db_session.query(AppointmentSlot).filter(AppointmentSlot.id == old_slot.id).one()
    assert freed_slot.status == SlotStatus.open


def test_book_or_modify_appointment_reschedule_rejects_conflicting_target_slot(db_session):
    profile = make_patient_profile(db_session)
    doctor = make_doctor(db_session)
    start = datetime.now(timezone.utc) + timedelta(days=2)
    slot_a = make_appointment_slot(db_session, doctor=doctor, start_time=start)
    slot_b = make_appointment_slot(db_session, doctor=doctor, start_time=start + timedelta(minutes=10))
    slot_c = make_appointment_slot(db_session, doctor=doctor, start_time=start + timedelta(days=2))

    appointment_1 = book_or_modify_appointment(db_session, str(profile.id), str(slot_a.id), "book", None)
    appointment_2 = book_or_modify_appointment(db_session, str(profile.id), str(slot_c.id), "book", None)
    assert appointment_1["status"] == "confirmed"
    assert appointment_2["status"] == "confirmed"

    result = book_or_modify_appointment(
        db_session, str(profile.id), str(slot_b.id), "reschedule", appointment_2["id"]
    )

    assert result["status"] == "error"
    assert "conflicting" in result["error"]
    untouched_slot_c = db_session.query(AppointmentSlot).filter(AppointmentSlot.id == slot_c.id).one()
    assert untouched_slot_c.status == SlotStatus.booked
    untouched_slot_b = db_session.query(AppointmentSlot).filter(AppointmentSlot.id == slot_b.id).one()
    assert untouched_slot_b.status == SlotStatus.open


def test_book_or_modify_appointment_cancel_frees_slot(db_session):
    profile = make_patient_profile(db_session)
    slot = make_appointment_slot(db_session)
    booked = book_or_modify_appointment(db_session, str(profile.id), str(slot.id), "book", None)

    result = book_or_modify_appointment(db_session, str(profile.id), str(slot.id), "cancel", booked["id"])

    assert result["status"] == "cancelled"
    freed_slot = db_session.query(AppointmentSlot).filter(AppointmentSlot.id == slot.id).one()
    assert freed_slot.status == SlotStatus.open


def test_book_or_modify_appointment_writes_audit_event(db_session):
    profile = make_patient_profile(db_session)
    slot = make_appointment_slot(db_session)

    book_or_modify_appointment(db_session, str(profile.id), str(slot.id), "book", None)

    audit_actions = {e.action for e in db_session.query(AuditEvent).all()}
    assert "book_or_modify_appointment" in audit_actions
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_appointment_tools.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.tools.appointment_tools'`

- [ ] **Step 4: Implement `app/tools/appointment_tools.py`**

```python
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from sqlalchemy.orm import Session

from app.audit import audited
from app.models import Appointment, AppointmentSlot, AppointmentStatus, Doctor, SlotStatus


def _appointment_dict(appointment: Appointment, slot: AppointmentSlot | None) -> dict:
    return {
        "id": str(appointment.id),
        "patient_id": str(appointment.patient_id),
        "doctor_id": str(appointment.doctor_id),
        "slot_id": str(appointment.slot_id),
        "status": appointment.status.value,
        "start_time": slot.start_time.isoformat() if slot else None,
        "end_time": slot.end_time.isoformat() if slot else None,
    }


@audited("check_slot_availability", "AppointmentSlot")
def check_slot_availability(db: Session, department_id: str, preferred_window: dict) -> list[dict]:
    now = datetime.now(timezone.utc)
    start_date = preferred_window.get("start_date")
    end_date = preferred_window.get("end_date")
    window_start = datetime.fromisoformat(start_date) if start_date else now
    window_end = datetime.fromisoformat(end_date) if end_date else now + timedelta(days=14)
    if window_start.tzinfo is None:
        window_start = window_start.replace(tzinfo=timezone.utc)
    if window_end.tzinfo is None:
        window_end = window_end.replace(tzinfo=timezone.utc)

    rows = (
        db.query(AppointmentSlot, Doctor)
        .join(Doctor, AppointmentSlot.doctor_id == Doctor.id)
        .filter(Doctor.department_id == uuid.UUID(department_id))
        .filter(Doctor.active.is_(True))
        .filter(AppointmentSlot.status == SlotStatus.open)
        .filter(AppointmentSlot.start_time >= window_start)
        .filter(AppointmentSlot.start_time <= window_end)
        .order_by(AppointmentSlot.start_time)
        .all()
    )
    return [
        {
            "slot_id": str(slot.id),
            "doctor_id": str(doctor.id),
            "doctor_name": doctor.name,
            "start_time": slot.start_time.isoformat(),
            "end_time": slot.end_time.isoformat(),
        }
        for slot, doctor in rows
    ]


def _conflicting_appointment(
    db: Session, patient_id: str, slot: AppointmentSlot, exclude_appointment_id: str | None = None
) -> Appointment | None:
    query = (
        db.query(Appointment)
        .join(AppointmentSlot, Appointment.slot_id == AppointmentSlot.id)
        .filter(Appointment.patient_id == uuid.UUID(patient_id))
        .filter(Appointment.status.in_([AppointmentStatus.pending, AppointmentStatus.confirmed]))
        .filter(AppointmentSlot.start_time < slot.end_time)
        .filter(AppointmentSlot.end_time > slot.start_time)
    )
    if exclude_appointment_id:
        query = query.filter(Appointment.id != uuid.UUID(exclude_appointment_id))
    return query.first()


@audited("book_or_modify_appointment", "Appointment")
def book_or_modify_appointment(
    db: Session,
    patient_id: str,
    slot_id: str,
    action: str,
    existing_appointment_id: str | None,
) -> dict:
    if action == "cancel":
        if not existing_appointment_id:
            return {"id": None, "status": "error", "error": "cancel requires existing_appointment_id"}
        appointment = db.query(Appointment).filter(Appointment.id == uuid.UUID(existing_appointment_id)).first()
        if appointment is None:
            return {"id": None, "status": "error", "error": f"Appointment {existing_appointment_id} not found"}
        old_slot = db.query(AppointmentSlot).filter(AppointmentSlot.id == appointment.slot_id).first()
        if old_slot is not None:
            old_slot.status = SlotStatus.open
        appointment.status = AppointmentStatus.cancelled
        db.commit()
        return _appointment_dict(appointment, old_slot)

    slot = db.query(AppointmentSlot).filter(AppointmentSlot.id == uuid.UUID(slot_id)).first()
    if slot is None:
        return {"id": None, "status": "error", "error": f"Slot {slot_id} not found"}
    if slot.status != SlotStatus.open:
        return {"id": None, "status": "error", "error": "Slot is no longer open"}

    if action == "book":
        conflict = _conflicting_appointment(db, patient_id, slot)
        if conflict is not None:
            return {
                "id": str(conflict.id),
                "status": "error",
                "error": "Patient already has a conflicting appointment",
            }

        slot.status = SlotStatus.booked
        appointment = Appointment(
            patient_id=uuid.UUID(patient_id),
            doctor_id=slot.doctor_id,
            slot_id=slot.id,
            status=AppointmentStatus.confirmed,
        )
        db.add(appointment)
        db.commit()
        return _appointment_dict(appointment, slot)

    if action == "reschedule":
        if not existing_appointment_id:
            return {"id": None, "status": "error", "error": "reschedule requires existing_appointment_id"}
        appointment = db.query(Appointment).filter(Appointment.id == uuid.UUID(existing_appointment_id)).first()
        if appointment is None:
            return {"id": None, "status": "error", "error": f"Appointment {existing_appointment_id} not found"}

        conflict = _conflicting_appointment(db, patient_id, slot, exclude_appointment_id=existing_appointment_id)
        if conflict is not None:
            return {
                "id": str(conflict.id),
                "status": "error",
                "error": "Patient already has a conflicting appointment",
            }

        old_slot = db.query(AppointmentSlot).filter(AppointmentSlot.id == appointment.slot_id).first()
        if old_slot is not None:
            old_slot.status = SlotStatus.open
        slot.status = SlotStatus.booked
        appointment.doctor_id = slot.doctor_id
        appointment.slot_id = slot.id
        appointment.status = AppointmentStatus.rescheduled
        db.commit()
        return _appointment_dict(appointment, slot)

    return {"id": None, "status": "error", "error": f"Unknown action: {action}"}


@tool(response_format="content_and_artifact")
def check_slot_availability_tool(
    preferred_window: dict,
    department_id: Annotated[str, InjectedState("department_id")],
    config: RunnableConfig,
):
    """Find open appointment slots for doctors in the patient's department.
    preferred_window may include start_date and/or end_date as YYYY-MM-DD
    strings if the patient mentioned a timeframe; pass {} for no preference
    (defaults to the next 14 days)."""
    db = config["configurable"]["db"]
    result = check_slot_availability(db, department_id, preferred_window)
    return f"Found {len(result)} open slot(s)", result


@tool(response_format="content_and_artifact")
def book_or_modify_appointment_tool(
    slot_id: str,
    action: str,
    existing_appointment_id: str | None,
    patient_id: Annotated[str, InjectedState("patient_id")],
    config: RunnableConfig,
):
    """Book, reschedule, or cancel an appointment. action must be one of
    "book", "reschedule", "cancel". slot_id is the id of the target slot
    from check_slot_availability's result (for "cancel", pass the existing
    appointment's current slot_id). existing_appointment_id is required for
    "reschedule"/"cancel" and must be omitted (pass null) for "book". If the
    result status is "error", pick a different slot and try again."""
    db = config["configurable"]["db"]
    result = book_or_modify_appointment(db, patient_id, slot_id, action, existing_appointment_id)
    return f"Appointment {action} result: {result['status']}", result
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_appointment_tools.py -v`
Expected: PASS (9 tests)

- [ ] **Step 6: Commit**

```bash
git add app/tools/appointment_tools.py tests/test_appointment_tools.py tests/fakes.py
git commit -m "Add check_slot_availability and book_or_modify_appointment tools"
```

---

### Task 3: Department Routing agent subgraph

**Files:**
- Create: `app/agents/routing.py`
- Test: `tests/test_routing_agent.py`

**Interfaces:**
- Consumes: `app.agents.state.WorkflowState`, `app.llm.{get_llm, invoke_with_retry}`, `app.tools.department_tools.lookup_departments_tool`, `app.tools.escalation_tools.create_escalation` (the plain audited function, not the LLM tool), `app.models.Department`.
- Produces: `routing_agent_node(state: WorkflowState, config) -> dict` — returns `{"department_id": str}` on a resolved match, or `{"department_id": None, "escalation": dict}` when unresolved (real `Escalation` row persisted via `create_escalation`). Task 5 registers this as the `routing_agent` parent-graph node.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_routing_agent.py`:

```python
import uuid

from langchain_core.messages import HumanMessage

from app.agents.routing import (
    route_after_routing_llm,
    routing_agent_node,
    routing_finalize_node,
    routing_llm_node,
)
from app.models import Escalation
from tests.fakes import (
    FakeToolCallingModel,
    ai_message_text,
    ai_message_with_tool_call,
    make_department,
    make_workflow_run,
    workflow_state,
)


def _routing_state(**overrides):
    state = {"messages": [HumanMessage("request: book a cardiology appointment")], "department_name": None}
    state.update(overrides)
    return state


def test_routing_llm_node_with_tool_call_routes_to_tools(monkeypatch):
    fake_model = FakeToolCallingModel(
        [ai_message_with_tool_call("lookup_departments_tool", {"query_hint": "cardiology"})]
    )
    monkeypatch.setattr("app.agents.routing.get_llm", lambda: fake_model)

    state = _routing_state()
    update = routing_llm_node(state, config={"configurable": {}})
    state["messages"] = state["messages"] + update["messages"]

    assert route_after_routing_llm(state) == "routing_tools"


def test_routing_llm_node_with_no_tool_call_routes_to_finalize(monkeypatch):
    fake_model = FakeToolCallingModel([ai_message_text("Cardiology")])
    monkeypatch.setattr("app.agents.routing.get_llm", lambda: fake_model)

    state = _routing_state()
    update = routing_llm_node(state, config={"configurable": {}})
    state["messages"] = state["messages"] + update["messages"]

    assert route_after_routing_llm(state) == "routing_finalize"


def test_routing_finalize_node_sets_department_name(): 
    state = _routing_state(messages=[ai_message_text("Cardiology")])

    update = routing_finalize_node(state, config={"configurable": {}})

    assert update == {"department_name": "Cardiology"}


def test_routing_finalize_node_returns_none_for_unmatched():
    state = _routing_state(messages=[ai_message_text("UNMATCHED")])

    update = routing_finalize_node(state, config={"configurable": {}})

    assert update == {"department_name": None}


def test_routing_agent_node_resolves_department_id_by_name(monkeypatch, db_session):
    dept_name = f"Cardiology {uuid.uuid4().hex[:8]}"
    department = make_department(db_session, name=dept_name)
    fake_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("lookup_departments_tool", {"query_hint": "cardiology"}),
            ai_message_text(dept_name),
        ]
    )
    monkeypatch.setattr("app.agents.routing.get_llm", lambda: fake_model)

    state = workflow_state(request_text="I need a cardiology checkup")
    update = routing_agent_node(state, config={"configurable": {"db": db_session}})

    assert update["department_id"] == str(department.id)
    assert "escalation" not in update


def test_routing_agent_node_escalates_when_unmatched(monkeypatch, db_session):
    workflow_run = make_workflow_run(db_session)
    make_department(db_session, name=f"Cardiology {uuid.uuid4().hex[:8]}")
    fake_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("lookup_departments_tool", {"query_hint": "dermatology"}),
            ai_message_text("UNMATCHED"),
        ]
    )
    monkeypatch.setattr("app.agents.routing.get_llm", lambda: fake_model)

    state = workflow_state(
        workflow_run_id=str(workflow_run.id),
        request_text="I need to see a dermatologist about a rash",
    )
    update = routing_agent_node(state, config={"configurable": {"db": db_session}})

    assert update["department_id"] is None
    assert update["escalation"] is not None
    escalation = db_session.query(Escalation).filter(Escalation.workflow_run_id == workflow_run.id).one()
    assert "dermatologist" in escalation.reason


def test_routing_agent_node_escalates_when_name_not_found_in_db(monkeypatch, db_session):
    workflow_run = make_workflow_run(db_session)
    fake_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("lookup_departments_tool", {"query_hint": "x"}),
            ai_message_text("Neurology"),
        ]
    )
    monkeypatch.setattr("app.agents.routing.get_llm", lambda: fake_model)

    state = workflow_state(workflow_run_id=str(workflow_run.id), request_text="book neurology")
    update = routing_agent_node(state, config={"configurable": {"db": db_session}})

    assert update["department_id"] is None
    assert update["escalation"] is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_routing_agent.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.agents.routing'`

- [ ] **Step 3: Implement `app/agents/routing.py`**

```python
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from sqlalchemy import func

from app.agents.state import WorkflowState
from app.llm import get_llm, invoke_with_retry
from app.models import Department
from app.tools.department_tools import lookup_departments_tool
from app.tools.escalation_tools import create_escalation

ROUTING_SYSTEM_PROMPT = (
    "You are the Department Routing Agent for AgentCare, an administrative "
    "healthcare workflow assistant. Call lookup_departments with a short "
    "hint describing what the patient's request is about, then look at the "
    "returned list of active departments. If exactly one department is a "
    "clear administrative fit, reply with ONLY that department's exact "
    "name, nothing else. If no department in the list is a reasonable "
    "administrative fit, reply with the single word UNMATCHED. Never "
    "reason about medical severity, urgency, or diagnosis — only match the "
    "request to an administrative department."
)

routing_tools = [lookup_departments_tool]
routing_tools_node = ToolNode(routing_tools)


class RoutingState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    department_name: str | None


def routing_llm_node(state: RoutingState, config):
    model = get_llm().bind_tools(routing_tools)
    messages = [SystemMessage(ROUTING_SYSTEM_PROMPT), *state["messages"]]
    ai_message = invoke_with_retry(model, messages)
    return {"messages": [ai_message]}


def routing_finalize_node(state: RoutingState, config):
    last = state["messages"][-1]
    text = last.content.strip()
    if text.upper() == "UNMATCHED":
        return {"department_name": None}
    return {"department_name": text}


def route_after_routing_llm(state: RoutingState) -> Literal["routing_tools", "routing_finalize"]:
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None):
        return "routing_tools"
    return "routing_finalize"


def build_routing_subgraph():
    graph = StateGraph(RoutingState)
    graph.add_node("routing_llm", routing_llm_node)
    graph.add_node("routing_tools", routing_tools_node)
    graph.add_node("routing_finalize", routing_finalize_node)
    graph.set_entry_point("routing_llm")
    graph.add_conditional_edges(
        "routing_llm",
        route_after_routing_llm,
        {"routing_tools": "routing_tools", "routing_finalize": "routing_finalize"},
    )
    graph.add_edge("routing_tools", "routing_llm")
    graph.add_edge("routing_finalize", END)
    return graph.compile()


_routing_subgraph = build_routing_subgraph()


def routing_agent_node(state: WorkflowState, config) -> dict:
    """Parent-graph node (registered as "routing_agent" in app/graph.py).
    Invokes the private Routing subgraph, then resolves the department name
    it returned to a real department_id. lookup_departments is read-only,
    so there's no write-tool artifact to capture an id from — the finalize
    step reads the LLM's own final text, and this wrapper does a
    case-insensitive DB lookup rather than trusting an LLM-transcribed
    UUID. If no department resolves, escalates via the same audited
    create_escalation function Safety's tool uses (not a second LLM call —
    "no confident match" is a deterministic outcome, not a judgment call)."""
    result = _routing_subgraph.invoke(
        {
            "messages": [HumanMessage(f"request: {state['request_text']}")],
            "department_name": None,
        },
        config=config,
    )
    db = config["configurable"]["db"]
    department_name = result.get("department_name")

    department_id = None
    if department_name:
        department = (
            db.query(Department)
            .filter(func.lower(Department.name) == department_name.strip().lower())
            .filter(Department.active.is_(True))
            .first()
        )
        if department is not None:
            department_id = str(department.id)

    if department_id is None:
        escalation = create_escalation(
            db,
            state["workflow_run_id"],
            f"Could not confidently match request to an active department: {state['request_text']!r}",
        )
        return {"department_id": None, "escalation": escalation}

    return {"department_id": department_id}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_routing_agent.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add app/agents/routing.py tests/test_routing_agent.py
git commit -m "Add Department Routing agent subgraph"
```

---

### Task 4: Appointment agent subgraph

**Files:**
- Create: `app/agents/appointment.py`
- Test: `tests/test_appointment_agent.py`

**Interfaces:**
- Consumes: `app.agents.state.WorkflowState`, `app.llm.{get_llm, invoke_with_retry}`, `app.tools.appointment_tools.{check_slot_availability_tool, book_or_modify_appointment_tool}`.
- Produces: `appointment_agent_node(state: WorkflowState, config) -> dict` — returns `{"appointment_id": str | None}`. Task 5 registers this as the `appointment_agent` parent-graph node.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_appointment_agent.py`:

```python
from langchain_core.messages import HumanMessage, ToolMessage

from app.agents.appointment import (
    appointment_agent_node,
    appointment_capture_node,
    appointment_llm_node,
    route_after_appointment_llm,
)
from app.models import Appointment, AppointmentSlot, SlotStatus
from tests.fakes import (
    FakeToolCallingModel,
    ai_message_text,
    ai_message_with_tool_call,
    make_appointment_slot,
    make_department,
    make_doctor,
    make_patient_profile,
    workflow_state,
)


def _appointment_state(**overrides):
    state = {
        "messages": [HumanMessage("request: book a cardiology appointment")],
        "department_id": "11111111-1111-1111-1111-111111111111",
        "patient_id": "22222222-2222-2222-2222-222222222222",
        "appointment_id": None,
    }
    state.update(overrides)
    return state


def test_appointment_llm_node_with_tool_call_routes_to_tools(monkeypatch):
    fake_model = FakeToolCallingModel(
        [ai_message_with_tool_call("check_slot_availability_tool", {"preferred_window": {}})]
    )
    monkeypatch.setattr("app.agents.appointment.get_llm", lambda: fake_model)

    state = _appointment_state()
    update = appointment_llm_node(state, config={"configurable": {}})
    state["messages"] = state["messages"] + update["messages"]

    assert route_after_appointment_llm(state) == "appointment_tools"


def test_appointment_llm_node_with_no_tool_call_routes_to_end(monkeypatch):
    fake_model = FakeToolCallingModel([ai_message_text("Your appointment is confirmed.")])
    monkeypatch.setattr("app.agents.appointment.get_llm", lambda: fake_model)

    state = _appointment_state()
    update = appointment_llm_node(state, config={"configurable": {}})
    state["messages"] = state["messages"] + update["messages"]

    assert route_after_appointment_llm(state) == "__end__"


def test_appointment_capture_node_sets_appointment_id_on_success():
    tool_message = ToolMessage(
        content="Appointment book result: confirmed",
        artifact={"id": "a1", "status": "confirmed"},
        tool_call_id="call_1",
        name="book_or_modify_appointment_tool",
    )
    state = _appointment_state(messages=[tool_message])

    update = appointment_capture_node(state, config={"configurable": {}})

    assert update == {"appointment_id": "a1"}


def test_appointment_capture_node_ignores_error_result():
    tool_message = ToolMessage(
        content="Appointment book result: error",
        artifact={"id": None, "status": "error", "error": "Slot is no longer open"},
        tool_call_id="call_1",
        name="book_or_modify_appointment_tool",
    )
    state = _appointment_state(messages=[tool_message])

    update = appointment_capture_node(state, config={"configurable": {}})

    assert update == {}


def test_appointment_capture_node_ignores_check_slot_availability_message():
    tool_message = ToolMessage(
        content="Found 1 open slot(s)",
        artifact=[{"slot_id": "s1"}],
        tool_call_id="call_1",
        name="check_slot_availability_tool",
    )
    state = _appointment_state(messages=[tool_message])

    update = appointment_capture_node(state, config={"configurable": {}})

    assert update == {}


def test_appointment_agent_node_books_appointment_end_to_end(monkeypatch, db_session):
    department = make_department(db_session)
    doctor = make_doctor(db_session, department=department)
    slot = make_appointment_slot(db_session, doctor=doctor)
    profile = make_patient_profile(db_session)

    fake_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("check_slot_availability_tool", {"preferred_window": {}}),
            ai_message_with_tool_call(
                "book_or_modify_appointment_tool",
                {"slot_id": str(slot.id), "action": "book", "existing_appointment_id": None},
            ),
            ai_message_text("Your appointment is confirmed."),
        ]
    )
    monkeypatch.setattr("app.agents.appointment.get_llm", lambda: fake_model)

    state = workflow_state(
        department_id=str(department.id),
        patient_id=str(profile.id),
        request_text="book a cardiology appointment",
    )
    update = appointment_agent_node(state, config={"configurable": {"db": db_session}})

    assert update["appointment_id"] is not None
    appointment = db_session.query(Appointment).filter(Appointment.id == update["appointment_id"]).one()
    assert str(appointment.patient_id) == str(profile.id)
    booked_slot = db_session.query(AppointmentSlot).filter(AppointmentSlot.id == slot.id).one()
    assert booked_slot.status == SlotStatus.booked
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_appointment_agent.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.agents.appointment'`

- [ ] **Step 3: Implement `app/agents/appointment.py`**

```python
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from app.agents.state import WorkflowState
from app.llm import get_llm, invoke_with_retry
from app.tools.appointment_tools import check_slot_availability_tool, book_or_modify_appointment_tool

APPOINTMENT_SYSTEM_PROMPT = (
    "You are the Appointment Agent for AgentCare, an administrative "
    "healthcare workflow assistant. Call check_slot_availability to find "
    "open slots in the patient's department (preferred_window may include "
    "start_date/end_date as YYYY-MM-DD strings if the patient mentioned a "
    "timeframe, otherwise pass {}). Pick a slot that reasonably matches "
    "the patient's request, then call book_or_modify_appointment with "
    "action='book' and existing_appointment_id=null to reserve it. If "
    "booking returns status 'error', pick a different slot from the list "
    "and try again. Once booked, reply with a short confirmation sentence "
    "and do not call any more tools. Never diagnose or suggest treatment — "
    "only handle scheduling."
)

appointment_tools = [check_slot_availability_tool, book_or_modify_appointment_tool]
appointment_tools_node = ToolNode(appointment_tools)


class AppointmentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    department_id: str
    patient_id: str
    appointment_id: str | None


def appointment_llm_node(state: AppointmentState, config):
    model = get_llm().bind_tools(appointment_tools)
    messages = [SystemMessage(APPOINTMENT_SYSTEM_PROMPT), *state["messages"]]
    ai_message = invoke_with_retry(model, messages)
    return {"messages": [ai_message]}


def appointment_capture_node(state: AppointmentState, config):
    last = state["messages"][-1]
    if isinstance(last, ToolMessage) and last.name == "book_or_modify_appointment_tool":
        if last.artifact and last.artifact.get("status") != "error":
            return {"appointment_id": last.artifact["id"]}
    return {}


def route_after_appointment_llm(state: AppointmentState) -> Literal["appointment_tools", "__end__"]:
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None):
        return "appointment_tools"
    return "__end__"


def build_appointment_subgraph():
    graph = StateGraph(AppointmentState)
    graph.add_node("appointment_llm", appointment_llm_node)
    graph.add_node("appointment_tools", appointment_tools_node)
    graph.add_node("appointment_capture", appointment_capture_node)
    graph.set_entry_point("appointment_llm")
    graph.add_conditional_edges(
        "appointment_llm",
        route_after_appointment_llm,
        {"appointment_tools": "appointment_tools", "__end__": END},
    )
    graph.add_edge("appointment_tools", "appointment_capture")
    graph.add_edge("appointment_capture", "appointment_llm")
    return graph.compile()


_appointment_subgraph = build_appointment_subgraph()


def appointment_agent_node(state: WorkflowState, config) -> dict:
    """Parent-graph node (registered as "appointment_agent" in
    app/graph.py). Invokes the private Appointment subgraph and returns
    only the field that belongs in WorkflowState."""
    result = _appointment_subgraph.invoke(
        {
            "messages": [HumanMessage(f"request: {state['request_text']}")],
            "department_id": state["department_id"],
            "patient_id": state["patient_id"],
            "appointment_id": None,
        },
        config=config,
    )
    return {"appointment_id": result.get("appointment_id")}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_appointment_agent.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add app/agents/appointment.py tests/test_appointment_agent.py
git commit -m "Add Appointment agent subgraph"
```

---

### Task 5: Parent graph wiring + end-to-end tests

**Files:**
- Modify: `app/graph.py`
- Modify: `app/workflow_runner.py`
- Test: `tests/test_workflow_runner.py` (extend)

**Interfaces:**
- Consumes: `routing_agent_node` (Task 3), `appointment_agent_node` (Task 4), existing `safety_agent_node`/`coordinator_agent_node`.
- Produces: `build_graph()` now returns a 4-node compiled graph: `safety_agent → coordinator_agent → routing_agent → [escalate? END : appointment_agent] → END`. `run_workflow`'s non-escalated terminal `current_step` becomes `"document_agent"` (the seam Phase 4 extends from), matching the pattern already documented in `docs/memory/status.md`.

- [ ] **Step 1: Write the failing end-to-end tests**

Add to `tests/test_workflow_runner.py` (append; keep existing tests and imports, add these to the top-of-file imports):

```python
import uuid

from app.models import Appointment, AppointmentSlot, SlotStatus
from tests.fakes import make_appointment_slot, make_department, make_doctor
```

```python
def test_full_workflow_books_appointment_end_to_end(monkeypatch, db_session):
    dept_name = f"Cardiology {uuid.uuid4().hex[:8]}"
    department = make_department(db_session, name=dept_name)
    doctor = make_doctor(db_session, department=department)
    slot = make_appointment_slot(db_session, doctor=doctor)
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)

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
            ai_message_text(dept_name),
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

    workflow_run = run_workflow(
        db_session,
        patient_id=str(profile.id),
        user_id=str(user.id),
        request_text="I'd like to book a cardiology appointment next week",
    )

    assert workflow_run.status == WorkflowStatus.running
    assert workflow_run.current_step == "document_agent"
    assert workflow_run.state["department_id"] == str(department.id)
    assert workflow_run.state["appointment_id"] is not None

    appointment = db_session.query(Appointment).filter(Appointment.id == workflow_run.state["appointment_id"]).one()
    assert appointment.status.value == "confirmed"
    booked_slot = db_session.query(AppointmentSlot).filter(AppointmentSlot.id == slot.id).one()
    assert booked_slot.status == SlotStatus.booked


def test_unroutable_request_ends_needs_review_without_booking(monkeypatch, db_session):
    make_department(db_session, name=f"Cardiology {uuid.uuid4().hex[:8]}")
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)

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
            ai_message_with_tool_call("lookup_departments_tool", {"query_hint": "dermatology"}),
            ai_message_text("UNMATCHED"),
        ]
    )
    monkeypatch.setattr("app.agents.routing.get_llm", lambda: routing_model)

    workflow_run = run_workflow(
        db_session,
        patient_id=str(profile.id),
        user_id=str(user.id),
        request_text="I need to see a dermatologist about a rash",
    )

    assert workflow_run.status == WorkflowStatus.needs_review
    assert workflow_run.current_step == "routing_agent"
    assert db_session.query(Appointment).filter(Appointment.patient_id == profile.id).count() == 0

    escalation = db_session.query(Escalation).filter(Escalation.workflow_run_id == workflow_run.id).one()
    assert "dermatologist" in escalation.reason
```

Note: `db_session` accumulates data across the whole test session (see the
uniqueness gotcha above) — an assertion like `db_session.query(Appointment).count() == 0`
checks the *entire* session's accumulated rows, not just what this test
created, and will fail once any earlier test has booked a real appointment.
Scope every such assertion to the specific row(s) this test created (here,
`.filter(Appointment.patient_id == profile.id)`), never a bare global count.

Note: always construct `FakeToolCallingModel(...)` **before** the `monkeypatch.setattr(...)` call and capture it in a variable that the lambda closes over (`lambda: coordinator_model`) — never construct it inline inside the lambda (`lambda: FakeToolCallingModel([...])`). Any agent whose subgraph calls `get_llm()` more than once per run (Coordinator, Routing, Appointment — anything with a tool-call loop) calls `get_llm()` fresh on every loop iteration; an inline lambda hands back a **brand-new** mock with its response queue reset to item #1 every time, so the scripted second/third response is never reached and the loop never terminates — it runs until LangGraph's recursion limit kills it. Confirmed by an actual run: this exact mistake produced `GraphRecursionError` here during Phase 3 development. Only a single-call agent (Safety, which never loops) happens to tolerate the inline form — don't use it as a template.

Also update the existing `test_administrative_request_reaches_routing_boundary_with_intent_set` test — it currently asserts `current_step == "routing_agent"` after only Safety+Coordinator run, with no mock for `app.agents.routing.get_llm`. Since `build_graph()` now always continues past `coordinator_agent` into `routing_agent`, this test will hang on a real (unmocked) `get_llm()` call. Update it to mock routing (and expect the graph to continue to the escalate-or-appointment branch) — simplest fix: mock routing to also come back `UNMATCHED` and assert the now-correct outcome:

```python
def test_administrative_request_reaches_routing_boundary_with_intent_set(monkeypatch, db_session):
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)

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
            ai_message_with_tool_call("lookup_departments_tool", {"query_hint": "general"}),
            ai_message_text("UNMATCHED"),
        ]
    )
    monkeypatch.setattr("app.agents.routing.get_llm", lambda: routing_model)

    workflow_run = run_workflow(
        db_session,
        patient_id=str(profile.id),
        user_id=str(user.id),
        request_text="I'd like to book a cardiology appointment next week",
    )

    assert workflow_run.status == WorkflowStatus.needs_review
    assert workflow_run.current_step == "routing_agent"
    assert workflow_run.state["intent"] == "book_appointment"
    assert workflow_run.state["patient_id"] is not None
```

(Remove the old assertions about `escalation_count == 0` and `status == running` from this test — they described the pre-Phase-3 boundary, which no longer exists now that routing always runs.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_workflow_runner.py -v`
Expected: FAIL — `build_graph()` still only has 2 nodes, `routing_agent`/`appointment_agent` don't exist as graph nodes yet, and the modified test's assertions don't match current behavior.

- [ ] **Step 3: Wire the parent graph**

Replace `app/graph.py`:

```python
from typing import Literal

from langgraph.graph import END, StateGraph

from app.agents.appointment import appointment_agent_node
from app.agents.coordinator import coordinator_agent_node
from app.agents.routing import routing_agent_node
from app.agents.safety import safety_agent_node
from app.agents.state import WorkflowState


def route_after_safety(state: WorkflowState) -> Literal["coordinator_agent", "__end__"]:
    if state.get("escalation"):
        return "__end__"
    return "coordinator_agent"


def route_after_routing(state: WorkflowState) -> Literal["appointment_agent", "__end__"]:
    if state.get("escalation"):
        return "__end__"
    return "appointment_agent"


def build_graph():
    graph = StateGraph(WorkflowState)

    graph.add_node("safety_agent", safety_agent_node)
    graph.add_node("coordinator_agent", coordinator_agent_node)
    graph.add_node("routing_agent", routing_agent_node)
    graph.add_node("appointment_agent", appointment_agent_node)

    graph.set_entry_point("safety_agent")
    graph.add_conditional_edges(
        "safety_agent", route_after_safety, {"coordinator_agent": "coordinator_agent", "__end__": END}
    )
    graph.add_edge("coordinator_agent", "routing_agent")
    graph.add_conditional_edges(
        "routing_agent", route_after_routing, {"appointment_agent": "appointment_agent", "__end__": END}
    )
    graph.add_edge("appointment_agent", END)

    return graph.compile()
```

- [ ] **Step 4: Update the post-run seam in `workflow_runner.py`**

In `app/workflow_runner.py`, change:

```python
    if full_state.get("escalation"):
        workflow_run.status = WorkflowStatus.needs_review
    else:
        workflow_run.status = WorkflowStatus.running
        workflow_run.current_step = "routing_agent"
```

to:

```python
    if full_state.get("escalation"):
        workflow_run.status = WorkflowStatus.needs_review
    else:
        workflow_run.status = WorkflowStatus.running
        workflow_run.current_step = "document_agent"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/ -v`
Expected: PASS — full suite green, including the two new end-to-end tests and the updated boundary test.

- [ ] **Step 6: Commit**

```bash
git add app/graph.py app/workflow_runner.py tests/test_workflow_runner.py
git commit -m "Wire Routing and Appointment agents into the parent graph"
```

---

## Self-review

- **Spec coverage:** design spec §5.3 (Routing), §5.4 (Appointment), §6 (tool signatures) all covered. §5.1's "final call reads back rows and renders confirmation" deferred by explicit decision (Global Constraints) — no UI exists yet to show it to; will be picked up when Document/Follow-up phases add the rest of what it needs to read.
- **No placeholders:** every step has complete, runnable code — no TBD/TODO.
- **Type/name consistency checked across tasks:** `department_id`/`patient_id` injected via `InjectedState` match the keys in `AppointmentState` (Task 4) and `WorkflowState` (existing); `lookup_departments_tool`, `check_slot_availability_tool`, `book_or_modify_appointment_tool` names match between tool definitions (Tasks 1–2) and agent bindings (Tasks 3–4); `routing_agent_node`/`appointment_agent_node` names match between agent files (Tasks 3–4) and `graph.py` (Task 5).
- **Speed:** 5 tasks (vs. Phase 2's 9) — tools/tests consolidated per file rather than per function, agents follow an already-proven pattern (no new architecture to invent), and the explicitly deferred finalize step avoids building something that gets rewritten twice more in Phases 4–5.
