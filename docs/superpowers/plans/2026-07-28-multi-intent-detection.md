# Multi-Intent Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a patient's request genuinely contains two or more distinct administrative asks (e.g. "cancel my appointment and book a new one"), stop and ask which one to handle first instead of silently merging or dropping one — instead of what happens today, where the Coordinator is forced into one label and the extra ask silently vanishes.

**Architecture:** Purely additive. One new `WorkflowStatus`, one new trivial graph node, one new dispatcher function that calls existing, unmodified continuation functions (`continue_as_booking`, `_land_on_appointment_selection_or_none`) based on which button the patient clicks, one new route, one new template block, one prompt clause. No existing function's signature or behavior changes.

**Tech Stack:** FastAPI, Jinja2, LangGraph, Groq via langchain-groq, SQLAlchemy, Alembic, pytest.

## Global Constraints

- No hardcoded final responses — all wording built from real state, never a bare LLM string (`CLAUDE.md`).
- Prompt wording is not unit tested — mock the LLM call in tests (`CLAUDE.md`, this project's existing test convention).
- Every new terminal state needs an `AuditEvent`-free, deterministic path — this feature adds no new DB writes of its own (it dispatches to functions that already have their own audited writes).
- The single-intent case (the common case) must be provably unaffected — every task that touches shared code (`route_after_document`, workflow_runner status resolution) must include a regression check alongside the new behavior.

---

## Task 1: Coordinator prompt update

**Files:**
- Modify: `app/agents/coordinator.py:12-22` (the `COORDINATOR_SYSTEM_PROMPT` string)

**Interfaces:**
- Consumes: nothing new.
- Produces: no code interface change — this only changes the string constant `COORDINATOR_SYSTEM_PROMPT`. Later tasks depend on the Coordinator LLM sometimes returning a comma-separated `intent` string (e.g. `"cancel_appointment,book_appointment"`) instead of always a single label — that behavior is tested at the graph-routing level in Task 2, not here (per `CLAUDE.md`, prompt wording itself isn't unit tested).

- [ ] **Step 1: Make the prompt edit**

Current `app/agents/coordinator.py:12-22`:
```python
COORDINATOR_SYSTEM_PROMPT = (
    "You are the Coordinator Agent for AgentCare, an administrative "
    "healthcare workflow assistant. Given the patient's free-text request, "
    "call get_or_create_patient with any contact details mentioned in the "
    "request (phone, preferred_language, emergency_contact — pass {} if "
    "none are mentioned). After the tool result comes back, reply with a "
    "one to three word administrative intent label for the request, for "
    "example: book_appointment, reschedule_appointment, "
    "cancel_appointment, submit_document, general_inquiry. Never diagnose "
    "or suggest treatment — only classify the administrative intent."
)
```

Replace with:
```python
COORDINATOR_SYSTEM_PROMPT = (
    "You are the Coordinator Agent for AgentCare, an administrative "
    "healthcare workflow assistant. Given the patient's free-text request, "
    "call get_or_create_patient with any contact details mentioned in the "
    "request (phone, preferred_language, emergency_contact — pass {} if "
    "none are mentioned). After the tool result comes back, reply with a "
    "one to three word administrative intent label for the request, for "
    "example: book_appointment, reschedule_appointment, "
    "cancel_appointment, submit_document, general_inquiry. If — and only "
    "if — the request genuinely contains two or more distinct "
    "administrative asks (e.g. \"cancel my appointment and book a new one\" "
    "or \"reschedule my visit and also cancel my other booking\"), reply "
    "instead with all the distinct intent labels separated by commas and "
    "nothing else, for example: cancel_appointment,book_appointment. Do not "
    "split a single request into multiple labels just because it has "
    "multiple sentences or extra detail — only when there are genuinely "
    "separate administrative actions being requested. Never diagnose or "
    "suggest treatment — only classify the administrative intent(s)."
)
```

- [ ] **Step 2: Run the existing Coordinator test file to confirm no regression**

Run: `python -m pytest tests/test_coordinator_agent.py -v`
Expected: PASS (all existing tests still pass — they use `FakeToolCallingModel` with scripted single-label responses, unaffected by a prompt wording change)

- [ ] **Step 3: Commit**

```bash
git add app/agents/coordinator.py
git commit -m "feat: Coordinator prompt detects genuinely multi-intent requests"
```

---

## Task 2: Graph — `needs_intent_selection` node + routing

**Files:**
- Modify: `app/graph.py` (full file — small, shown below with exact target content)
- Modify: `app/agents/state.py` (add one field)
- Modify: `app/models.py` (add one `WorkflowStatus` value)
- Create: `alembic/versions/<new_revision>_add_needs_intent_selection_status.py`
- Test: `tests/test_graph.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `route_after_document` now returns one of `"routing_agent" | "needs_appointment_selection" | "needs_intent_selection" | "needs_clarification"`. `needs_intent_selection_node(state, config) -> dict` returns `{"needs_intent_selection": True}`. `WorkflowStatus.needs_intent_selection` (string value `"needs_intent_selection"`). Task 3 depends on all three.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_graph.py` (this file already imports `needs_appointment_selection_node`, `needs_clarification_node`, `route_after_document` from `app.graph`, and has a `@pytest.mark.parametrize` test for `route_after_document` — extend the existing parametrize list and add a new node test):

```python
def test_route_after_document_comma_separated_intent_routes_to_intent_selection():
    from app.graph import route_after_document

    assert route_after_document({"intent": "cancel_appointment,book_appointment"}) == "needs_intent_selection"
    assert route_after_document({"intent": "book_appointment,cancel_appointment"}) == "needs_intent_selection"


def test_needs_intent_selection_node_sets_flag():
    from app.graph import needs_intent_selection_node

    update = needs_intent_selection_node({"intent": "cancel_appointment,book_appointment"}, config={"configurable": {}})
    assert update == {"needs_intent_selection": True}


def test_route_after_document_single_intent_unaffected_by_comma_check():
    from app.graph import route_after_document

    assert route_after_document({"intent": "book_appointment"}) == "routing_agent"
    assert route_after_document({"intent": "cancel_appointment"}) == "needs_appointment_selection"
    assert route_after_document({"intent": "reschedule_appointment"}) == "needs_appointment_selection"
    assert route_after_document({"intent": "general_inquiry"}) == "needs_clarification"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_graph.py::test_route_after_document_comma_separated_intent_routes_to_intent_selection -v`
Expected: FAIL — `route_after_document` currently returns `"needs_clarification"` for a comma-containing string with no "book"/"cancel"/"reschedule" substring match on the whole string, or could accidentally match "book" first for `"book_appointment,cancel_appointment"` (this is the exact bug being fixed) — either way, the test fails against current code.

- [ ] **Step 3: Write minimal implementation**

Replace the full content of `app/graph.py` with:

```python
from typing import Literal

from langgraph.graph import END, StateGraph

from app.agents.coordinator import coordinator_agent_node
from app.agents.document import document_agent_node
from app.agents.routing import routing_agent_node
from app.agents.safety import safety_agent_node
from app.agents.state import WorkflowState


def route_after_safety(state: WorkflowState) -> Literal["coordinator_agent", "__end__"]:
    if state.get("escalation"):
        return "__end__"
    return "coordinator_agent"


def route_after_document(
    state: WorkflowState,
) -> Literal["routing_agent", "needs_appointment_selection", "needs_intent_selection", "needs_clarification"]:
    intent = (state.get("intent") or "").strip().lower()
    if "," in intent:
        return "needs_intent_selection"
    if intent == "book_appointment" or "book" in intent:
        return "routing_agent"
    if "cancel" in intent or "reschedule" in intent:
        return "needs_appointment_selection"
    return "needs_clarification"


def needs_clarification_node(state: WorkflowState, config) -> dict:
    return {"needs_clarification": True}


def needs_appointment_selection_node(state: WorkflowState, config) -> dict:
    intent = (state.get("intent") or "").strip().lower()
    action = "cancel" if "cancel" in intent else "reschedule"
    return {"needs_appointment_selection": True, "pending_appointment_action": action}


def needs_intent_selection_node(state: WorkflowState, config) -> dict:
    return {"needs_intent_selection": True}


def build_graph():
    # routing_agent is the last automatic node in every booking path -
    # whether it escalates (no department match) or resolves a real
    # department_id, the graph stops there. Slot availability is a real DB
    # query (check_slot_availability), never a judgment call an LLM needs to
    # make, and the actual booking is always a specific, patient-clicked
    # slot - both handled deterministically by workflow_runner.run_workflow's
    # post-stream status resolution and the /select-slot route, not by an
    # agent auto-picking and auto-booking on the patient's behalf.
    graph = StateGraph(WorkflowState)

    graph.add_node("safety_agent", safety_agent_node)
    graph.add_node("coordinator_agent", coordinator_agent_node)
    graph.add_node("document_agent", document_agent_node)
    graph.add_node("needs_clarification", needs_clarification_node)
    graph.add_node("needs_appointment_selection", needs_appointment_selection_node)
    graph.add_node("needs_intent_selection", needs_intent_selection_node)
    graph.add_node("routing_agent", routing_agent_node)

    graph.set_entry_point("safety_agent")
    graph.add_conditional_edges(
        "safety_agent", route_after_safety, {"coordinator_agent": "coordinator_agent", "__end__": END}
    )
    graph.add_edge("coordinator_agent", "document_agent")
    graph.add_conditional_edges(
        "document_agent",
        route_after_document,
        {
            "routing_agent": "routing_agent",
            "needs_appointment_selection": "needs_appointment_selection",
            "needs_intent_selection": "needs_intent_selection",
            "needs_clarification": "needs_clarification",
        },
    )
    graph.add_edge("needs_clarification", END)
    graph.add_edge("needs_appointment_selection", END)
    graph.add_edge("needs_intent_selection", END)
    graph.add_edge("routing_agent", END)

    return graph.compile()
```

Add `needs_intent_selection: bool` to `app/agents/state.py`'s `WorkflowState` TypedDict (it currently ends with `rescheduling_appointment_id: str | None` — add the new field directly below that line).

Add `needs_intent_selection = "needs_intent_selection"` to `app/models.py`'s `WorkflowStatus` enum (it currently ends with `needs_appointment_selection = "needs_appointment_selection"` — add the new line directly below that).

Create a new Alembic migration. First check the current head revision:

Run: `python -m alembic heads`
Expected output: `e59f1a2b3c4d (head)` (the `needs_appointment_selection` migration, the most recent one committed)

Create `alembic/versions/f6a7b8c9d0e1_add_needs_intent_selection_status.py`:

```python
"""add needs_intent_selection to workflow_status

Revision ID: f6a7b8c9d0e1
Revises: e59f1a2b3c4d
Create Date: 2026-07-28 03:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, Sequence[str], None] = 'e59f1a2b3c4d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TYPE workflow_status ADD VALUE IF NOT EXISTS 'needs_intent_selection'")


def downgrade() -> None:
    """Downgrade schema."""
    # Same as prior workflow_status additions: Postgres has no direct
    # "drop enum value" operation. Purely additive, no downgrade needed.
    pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_graph.py -v`
Expected: PASS (all tests in the file, including the 3 new ones)

Then apply the migration:

Run: `python -m alembic upgrade head`
Expected: `Running upgrade e59f1a2b3c4d -> f6a7b8c9d0e1, add needs_intent_selection to workflow_status`

- [ ] **Step 5: Commit**

```bash
git add app/graph.py app/agents/state.py app/models.py alembic/versions/f6a7b8c9d0e1_add_needs_intent_selection_status.py tests/test_graph.py
git commit -m "feat: needs_intent_selection graph node and WorkflowStatus"
```

---

## Task 3: `continue_as_intent_selection` + status resolution

**Files:**
- Modify: `app/workflow_runner.py` (add one `elif` branch in `run_workflow`'s status resolution, add one new function)
- Test: `tests/test_workflow_runner.py`

**Interfaces:**
- Consumes: `continue_as_booking(db, workflow_run, override_request_text=None) -> WorkflowRun` (existing, unmodified — defined in this same file), `_land_on_appointment_selection_or_none(db, workflow_run, full_state) -> WorkflowRun` (existing, unmodified, private — defined in this same file, same module so callable directly).
- Produces: `continue_as_intent_selection(db, workflow_run: WorkflowRun, chosen_intent: str) -> WorkflowRun`, used by Task 4's route.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_workflow_runner.py` (this file already has `FakeToolCallingModel`, `ai_message_text`, `ai_message_with_tool_call` imported from `tests.fakes`, and `make_department`, `make_doctor`, `make_appointment_slot`, `make_appointment`, `make_patient_profile`, `make_workflow_run` helpers — check the top-of-file imports and reuse them):

```python
def test_continue_as_intent_selection_book_delegates_to_continue_as_booking(monkeypatch, db_session):
    department = make_department(db_session, name=f"Cardiology {uuid.uuid4().hex[:8]}")
    doctor = make_doctor(db_session, department=department)
    make_appointment_slot(db_session, doctor=doctor)

    fake_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("lookup_departments_tool", {"query_hint": "cardiology"}),
            ai_message_text(department.name),
        ]
    )
    monkeypatch.setattr("app.agents.routing.get_llm", lambda: fake_model)

    workflow_run = make_workflow_run(db_session)
    workflow_run.state = {
        **workflow_state(workflow_run_id=str(workflow_run.id), request_text="book a cardiology appointment"),
    }
    db_session.commit()

    result = continue_as_intent_selection(db_session, workflow_run, "book_appointment")

    assert result.status == WorkflowStatus.needs_slot_selection
    assert result.state["department_id"] == str(department.id)


def test_continue_as_intent_selection_cancel_lands_on_appointment_selection(db_session):
    profile = make_patient_profile(db_session)
    doctor = make_doctor(db_session)
    slot = make_appointment_slot(db_session, doctor=doctor)
    make_appointment(db_session, patient=profile, doctor=doctor, slot=slot)

    workflow_run = make_workflow_run(db_session, profile=profile)
    workflow_run.state = workflow_state(workflow_run_id=str(workflow_run.id), patient_id=str(profile.id))
    db_session.commit()

    result = continue_as_intent_selection(db_session, workflow_run, "cancel_appointment")

    assert result.status == WorkflowStatus.needs_appointment_selection
    assert result.state["pending_appointment_action"] == "cancel"


def test_continue_as_intent_selection_reschedule_lands_on_appointment_selection(db_session):
    profile = make_patient_profile(db_session)
    doctor = make_doctor(db_session)
    slot = make_appointment_slot(db_session, doctor=doctor)
    make_appointment(db_session, patient=profile, doctor=doctor, slot=slot)

    workflow_run = make_workflow_run(db_session, profile=profile)
    workflow_run.state = workflow_state(workflow_run_id=str(workflow_run.id), patient_id=str(profile.id))
    db_session.commit()

    result = continue_as_intent_selection(db_session, workflow_run, "reschedule_appointment")

    assert result.status == WorkflowStatus.needs_appointment_selection
    assert result.state["pending_appointment_action"] == "reschedule"
```

Add the import at the top of the test file if not already present: `from app.workflow_runner import continue_as_intent_selection` (add to the existing `from app.workflow_runner import (...)` block).

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_workflow_runner.py::test_continue_as_intent_selection_cancel_lands_on_appointment_selection -v`
Expected: FAIL with `ImportError: cannot import name 'continue_as_intent_selection'`

- [ ] **Step 3: Write minimal implementation**

In `app/workflow_runner.py`, add the new status branch to `run_workflow`'s existing resolution chain (find the line `if full_state.get("escalation"):` — add the new `elif` as the **first** `elif`, before `needs_clarification`, matching the "multi-intent flag wins over anything else" ordering from the spec):

```python
    if full_state.get("escalation"):
        workflow_run.status = WorkflowStatus.needs_review
    elif full_state.get("needs_intent_selection"):
        workflow_run.status = WorkflowStatus.needs_intent_selection
    elif full_state.get("needs_clarification"):
        workflow_run.status = WorkflowStatus.needs_clarification
    elif full_state.get("needs_appointment_selection"):
        return _land_on_appointment_selection_or_none(db, workflow_run, full_state)
    elif full_state.get("needs_appointment_reason"):
        ...  # unchanged below
```

Add the new function anywhere below `continue_as_booking` (e.g. directly after it, before `continue_as_booking_with_department`):

```python
def continue_as_intent_selection(db, workflow_run: WorkflowRun, chosen_intent: str) -> WorkflowRun:
    """needs_intent_selection -> patient picked which of the detected
    intents to handle first. Dispatches to whichever existing, unmodified
    continuation the single-intent graph path would have used for that
    label - no new booking/cancel/reschedule logic here, only routing to
    code that already exists and is already tested."""
    chosen = chosen_intent.strip().lower()
    full_state = dict(workflow_run.state)

    if "book" in chosen:
        return continue_as_booking(db, workflow_run)

    action = "cancel" if "cancel" in chosen else "reschedule"
    full_state["pending_appointment_action"] = action
    workflow_run.state = full_state
    db.commit()
    return _land_on_appointment_selection_or_none(db, workflow_run, full_state)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_workflow_runner.py -v`
Expected: PASS (all tests in the file, including the 3 new ones — this also re-runs every existing booking/cancel/reschedule test as a regression check)

- [ ] **Step 5: Commit**

```bash
git add app/workflow_runner.py tests/test_workflow_runner.py
git commit -m "feat: continue_as_intent_selection dispatches to existing continuations"
```

---

## Task 4: Route + wording + template

**Files:**
- Modify: `app/routes/request_routes.py` (import, `_render_patient_message`, `GET /requests/{id}` context block, new `POST` route)
- Modify: `app/templates/request_status.html` (new block)
- Test: `tests/test_request_routes.py`

**Interfaces:**
- Consumes: `continue_as_intent_selection(db, workflow_run, chosen_intent) -> WorkflowRun` (Task 3).
- Produces: route `POST /requests/{workflow_run_id}/select-intent`. Final task in this plan.

Current `app/routes/request_routes.py` imports (top of file):
```python
from app.workflow_runner import (
    continue_as_appointment_action,
    continue_as_booking,
    continue_as_booking_with_department,
    continue_as_staff_escalation,
    continue_with_selected_slot,
    run_workflow,
)
```

- [ ] **Step 1: Write the failing test**

Add to `tests/test_request_routes.py` (this file already has `client`, `_register_patient` helper, `FakeToolCallingModel`/`ai_message_text`/`ai_message_with_tool_call` imports, and `make_department`, `make_doctor`, `make_appointment_slot`, `make_appointment` from `tests.fakes` — check existing imports and reuse):

```python
def test_select_intent_book_option_reaches_slot_selection(monkeypatch, db_session):
    safety_model = FakeToolCallingModel([ai_message_text("SAFE")])
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: safety_model)
    coordinator_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("get_or_create_patient_tool", {"profile_fields": {}}),
            ai_message_text("cancel_appointment,book_appointment"),
        ]
    )
    monkeypatch.setattr("app.agents.coordinator.get_llm", lambda: coordinator_model)

    cookie = _register_patient("Multi Intent Route Patient")
    client.cookies.set("agentcare_session", cookie)

    resp = client.post(
        "/requests/new",
        data={"request_text": "cancel my appointment and book a new one"},
        follow_redirects=False,
    )
    workflow_run_id = resp.headers["location"].rsplit("/", 1)[-1]

    db_session.expire_all()
    workflow_run = db_session.get(WorkflowRun, workflow_run_id)
    assert workflow_run.status.value == "needs_intent_selection"

    department = make_department(db_session, name=f"Cardio {uuid.uuid4().hex[:8]}")
    doctor = make_doctor(db_session, department=department)
    make_appointment_slot(db_session, doctor=doctor)

    routing_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("lookup_departments_tool", {"query_hint": "cardio"}),
            ai_message_text(department.name),
        ]
    )
    monkeypatch.setattr("app.agents.routing.get_llm", lambda: routing_model)

    select_resp = client.post(
        f"/requests/{workflow_run_id}/select-intent",
        data={"intent": "book_appointment"},
        follow_redirects=False,
    )
    assert select_resp.status_code == 303

    db_session.expire_all()
    workflow_run = db_session.get(WorkflowRun, workflow_run_id)
    assert workflow_run.status.value == "needs_slot_selection"


def test_select_intent_stale_status_is_a_noop_redirect(monkeypatch, db_session):
    from app.models import WorkflowStatus

    cookie = _register_patient("Stale Intent Patient")
    client.cookies.set("agentcare_session", cookie)
    resp = client.post("/requests/new", data={"request_text": "hello"}, follow_redirects=False)
    workflow_run_id = resp.headers["location"].rsplit("/", 1)[-1]

    select_resp = client.post(
        f"/requests/{workflow_run_id}/select-intent",
        data={"intent": "book_appointment"},
        follow_redirects=False,
    )
    assert select_resp.status_code == 303
    assert select_resp.headers["location"] == f"/requests/{workflow_run_id}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_request_routes.py::test_select_intent_book_option_reaches_slot_selection -v`
Expected: FAIL with 404 (route doesn't exist yet)

- [ ] **Step 3: Write minimal implementation**

Update the import block:
```python
from app.workflow_runner import (
    continue_as_appointment_action,
    continue_as_booking,
    continue_as_booking_with_department,
    continue_as_intent_selection,
    continue_as_staff_escalation,
    continue_with_selected_slot,
    run_workflow,
)
```

In `_render_patient_message`, add a branch (insert before the existing `needs_clarification` check, or anywhere in the `if/elif` chain — order among these doesn't matter since `workflow_status` is always exactly one enum value):

```python
    if workflow_status == WorkflowStatus.needs_intent_selection:
        message = "It sounds like you're asking about a few things. Which one should I help with first?"
    elif workflow_status == WorkflowStatus.needs_clarification:
        ...  # unchanged below
```

In the `GET /requests/{workflow_run_id}` route, add a new context branch (alongside the existing `if workflow_run.status == WorkflowStatus.needs_appointment_selection: ...` block):

```python
    detected_intents = []
    if workflow_run.status == WorkflowStatus.needs_intent_selection:
        raw_intent = workflow_run.state.get("intent", "")
        actionable = [
            label.strip() for label in raw_intent.split(",")
            if any(kw in label for kw in ("book", "cancel", "reschedule"))
        ]
        detected_intents = actionable
```

Add `detected_intents` to the `TemplateResponse` context dict (alongside the existing `appointments`, `departments`, `slots` keys).

Add the new route (near `select_appointment`):

```python
@router.post("/requests/{workflow_run_id}/select-intent")
def select_intent(
    workflow_run_id: str,
    intent: str = Form(...),
    user: User = Depends(require_role(UserRole.patient.value)),
    db: Session = Depends(get_db),
):
    workflow_run = db.get(WorkflowRun, workflow_run_id)
    if workflow_run is None:
        raise HTTPException(status_code=404, detail="Not found")

    profile = _get_or_create_profile(db, user)
    if workflow_run.patient_id != profile.id:
        raise HTTPException(status_code=403, detail="Not your request")

    if workflow_run.status != WorkflowStatus.needs_intent_selection:
        return RedirectResponse(f"/requests/{workflow_run_id}", status_code=status.HTTP_303_SEE_OTHER)

    continue_as_intent_selection(db, workflow_run, intent)

    return RedirectResponse(f"/requests/{workflow_run_id}", status_code=status.HTTP_303_SEE_OTHER)
```

Add to `app/templates/request_status.html`, alongside the existing `needs_appointment_selection` block:

```html
{% if workflow_run.status.value == "needs_intent_selection" %}
<div style="margin-top: 16px; margin-bottom: 24px; display: flex; flex-wrap: wrap; gap: 10px;">
    {% for label in detected_intents %}
    <form method="post" action="/requests/{{ workflow_run.id }}/select-intent" style="display:inline">
        <input type="hidden" name="intent" value="{{ label }}">
        <button type="submit" style="padding: 10px 16px; background-color: #0066cc; color: white; border: none; border-radius: 4px; font-weight: 500; cursor: pointer;">
            {% if "book" in label %}Book an appointment
            {% elif "cancel" in label %}Cancel an appointment
            {% elif "reschedule" in label %}Reschedule an appointment
            {% endif %}
        </button>
    </form>
    {% endfor %}
</div>
{% endif %}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_request_routes.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add app/routes/request_routes.py app/templates/request_status.html tests/test_request_routes.py
git commit -m "feat: needs_intent_selection route, wording, and template"
```

---

## Final verification

- [ ] Run the full suite: `python -m pytest -q` — expect 0 failures, count higher than before this plan.
- [ ] Restore schema and reseed: `python -m alembic upgrade head` then `python -m seed.seed_data`.
- [ ] Live-check against the real Groq API (no mocked LLM) with the exact three prompts from the original bug report:
  - `"cancel my current appointment and book a new one with cardiology"` — expect `needs_intent_selection` with both `cancel_appointment` and `book_appointment` offered as buttons (previously silently misclassified as a single `reschedule_appointment`).
  - `"please reschedule my appointment and also cancel my other booking"` — expect `needs_intent_selection` with both `reschedule_appointment` and `cancel_appointment` offered.
  - `"book me an appointment and also tell me my visiting hours"` — report the model's actual classification as-is (per the spec, this one legitimately may or may not trigger multi-intent detection since "tell me your visiting hours" isn't an administrative action — this is acceptable model judgment, not a bug to force-fix).
