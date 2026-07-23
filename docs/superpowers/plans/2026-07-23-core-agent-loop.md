# AgentCare Core Agent Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the first real, end-to-end LangGraph agent loop — Safety &
Escalation Agent and Coordinator Agent, each with their own prompt and tool,
wired into a compiled parent graph, with every tool call audited and every
run checkpointed into `WorkflowRun` — provable via tests with the LLM
mocked, not a stub.

**Architecture:** The parent graph (`StateGraph(WorkflowState)`) has exactly
one node per agent — `WorkflowState` holds only structured business fields
(`intent`, `escalation`, `patient_id`, ...), no message history. Each agent
is internally a **separate compiled subgraph** with its own private state
(its own `messages` list via `add_messages`, plus whatever scratch fields
its tools need). Inside a subgraph: an LLM node calls `ChatGroq.bind_tools([...])`,
a prebuilt `ToolNode` executes any requested tool call, and (where the agent
needs the tool's structured result, not just its text) a capture node lifts
it out of `ToolMessage.artifact`. The parent-facing wrapper function seeds
the subgraph's initial state from `WorkflowState`, invokes the compiled
subgraph, and returns only the handful of fields that belong in
`WorkflowState` — the subgraph's internal tool-calling turns never leave it.
Tools are audited plain functions; thin `@tool`-decorated adapters inject
the DB session via `RunnableConfig` and inject state fields
(`workflow_run_id`, `user_id`) via `InjectedState`, so the LLM only ever
supplies arguments it can actually know (`reason`, `profile_fields`) — never
an opaque ID it would have to hallucinate or copy correctly. A
`workflow_runner` drives `graph.stream(..., stream_mode="updates")` over the
*parent* graph, persisting `WorkflowRun.state`/`current_step` after every
agent — the checkpoint the design spec requires — and catches any exception
to flip status to `failed` instead of crashing.

**Why subgraphs, not one flat graph:** an earlier draft of this plan gave
every agent 3 nodes in one shared graph, all agents appending to one
`WorkflowState["messages"]` list. That means agent N's LLM call replays
every prior agent's tool-calling exchange as input tokens — cost grows
faster than linearly as more agents are added in later phases — and can put
a tool name in an agent's message history that isn't in *that* agent's own
bound-tools list, a real cross-provider risk. Verified against the
currently installed `langgraph` (1.2.9) and `langchain-core` (1.5.0) docs:
`InjectedState` requires execution through `ToolNode` (confirmed), and
LangGraph subgraphs with a state schema different from their parent's are a
first-class, documented feature specifically for giving each agent a
private message history (confirmed — see Task 6 sources). This plan uses
that mechanism instead of a flat shared-message graph.

**Tech Stack:** LangGraph (`StateGraph`, subgraphs, `ToolNode`,
`InjectedState`, `add_messages`), `langchain-groq` (`ChatGroq`), LangChain
core (`@tool`, `RunnableConfig`, message types), SQLAlchemy 2.x, pytest (LLM
mocked with a small fake model, per project convention).

## Global Constraints

- No tool may return a fixed response regardless of input — both tools here
  do real reads/writes against Postgres (CLAUDE.md RULE).
- No hardcoded final responses — `intent` and `escalation` are read from the
  actual (mocked-in-tests) LLM output and from rows the tools just wrote,
  never fabricated strings (CLAUDE.md RULE).
- Each agent has its own system prompt and its own bound tool(s); no shared
  prompt between agents (CLAUDE.md, agents table).
- Tool execution uses LangGraph's prebuilt `ToolNode`, not custom dispatch
  code; tools are plain functions wrapped in LangChain's `@tool` decorator,
  which auto-generates the JSON schema — never hand-write a tool schema
  (CLAUDE.md, Stack).
- Audit logging is not an agent-invoked tool: every tool function is wrapped
  by a decorator that writes an `AuditEvent` row on every call, success or
  failure, so it doesn't depend on the LLM remembering to log it (CLAUDE.md).
- Persistent SQL only — no in-memory workflow state; `WorkflowRun.state` is
  checkpointed after every graph node, not just at the end (CLAUDE.md RULE;
  design spec §3, §13).
- Safety & Escalation Agent's prompt forbids diagnosis/prescription/dosage
  output and must escalate instead; administrative routing language
  ("book a cardiology appointment") is always fine (CLAUDE.md RULE; design
  spec §8).
- Groq calls are wrapped in a retry helper (3 attempts, exponential backoff)
  for transient failures; on final failure the node raises a typed
  `AgentError` (design spec §9).
- A graph-level exception is caught and flips `WorkflowRun.status` to
  `failed`, with the error persisted into state — never an unhandled crash
  with no persisted record (design spec §9).
- Tests mock the LLM call and assert on state transitions / persisted rows —
  never assert on exact prompt wording (CLAUDE.md conventions).

---

## File Structure

```
app/
├── audit.py                    # @audited(action, entity_type) decorator
├── llm.py                      # get_llm(), invoke_with_retry(), AgentError
├── graph.py                    # build_graph() — parent StateGraph, 1 node/agent
├── workflow_runner.py          # run_workflow() — stream + checkpoint + persist
├── agents/
│   ├── __init__.py
│   ├── state.py                # WorkflowState TypedDict (no messages key)
│   ├── safety.py                # Safety subgraph + parent-facing wrapper node
│   └── coordinator.py           # Coordinator subgraph + parent-facing wrapper node
└── tools/
    ├── __init__.py
    ├── patient_tools.py          # get_or_create_patient (+ @tool adapter)
    └── escalation_tools.py       # create_escalation (+ @tool adapter)
tests/
├── fakes.py                     # shared factories (make_user, ...) + FakeToolCallingModel
├── test_audit.py
├── test_llm.py
├── test_patient_tools.py
├── test_escalation_tools.py
├── test_safety_agent.py
├── test_coordinator_agent.py
└── test_workflow_runner.py      # end-to-end: escalation path, routing-boundary path
```

`app/tools/patient_tools.py` and `app/tools/escalation_tools.py` each hold
both the audited plain function *and* its thin `@tool` adapter side by side —
splitting them into separate files would be the same two functions spread
across twice the files for no reader benefit; they change together, so they
live together. Likewise `app/agents/safety.py` holds the subgraph's internal
nodes *and* its parent-facing wrapper — they're one unit of behavior
("what the Safety agent does"), just internally two graph layers.

---

### Task 1: Audit decorator

**Files:**
- Create: `app/audit.py`
- Test: `tests/test_audit.py`

**Interfaces:**
- Consumes: `app.models.AuditEvent` (existing).
- Produces: `audited(action: str, entity_type: str)` — a decorator factory.
  The decorated function's first positional argument must be a SQLAlchemy
  `Session` (`db`); on success it writes an `AuditEvent` row (best-effort
  `entity_id` parsed from `result["id"]` if present) and returns the
  wrapped function's result unchanged; on exception it rolls back, writes an
  `AuditEvent` row with the error in `event_metadata`, and re-raises.

- [ ] **Step 1: Write the failing test**

`tests/test_audit.py`:

```python
import uuid

import pytest

from app.audit import audited
from app.models import AuditEvent


def test_audited_writes_audit_event_on_success(db_session):
    @audited("dummy_action", "DummyEntity")
    def _dummy(db, x):
        return {"id": str(uuid.uuid4()), "x": x}

    result = _dummy(db_session, "hello")

    events = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "dummy_action", AuditEvent.entity_type == "DummyEntity")
        .all()
    )
    assert len(events) == 1
    assert str(events[0].entity_id) == result["id"]
    assert events[0].event_metadata["result"]["x"] == "hello"


def test_audited_writes_audit_event_on_failure_and_reraises(db_session):
    @audited("dummy_failure", "DummyEntity")
    def _dummy(db):
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        _dummy(db_session)

    events = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "dummy_failure", AuditEvent.entity_type == "DummyEntity")
        .all()
    )
    assert len(events) == 1
    assert events[0].entity_id is None
    assert "boom" in events[0].event_metadata["error"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_audit.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.audit'`

- [ ] **Step 3: Write `app/audit.py`**

```python
import functools
import json
import uuid

from app.models import AuditEvent


def audited(action: str, entity_type: str):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(db, *args, **kwargs):
            try:
                result = fn(db, *args, **kwargs)
            except Exception as exc:
                db.rollback()
                db.add(
                    AuditEvent(
                        actor_id=None,
                        action=action,
                        entity_type=entity_type,
                        entity_id=None,
                        event_metadata={"error": str(exc)},
                    )
                )
                db.commit()
                raise

            entity_id = None
            if isinstance(result, dict) and result.get("id"):
                try:
                    entity_id = uuid.UUID(str(result["id"]))
                except ValueError:
                    entity_id = None

            db.add(
                AuditEvent(
                    actor_id=None,
                    action=action,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    event_metadata={"result": _json_safe(result)},
                )
            )
            db.commit()
            return result

        return wrapper

    return decorator


def _json_safe(value):
    return json.loads(json.dumps(value, default=str))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_audit.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add app/audit.py tests/test_audit.py
git commit -m "Add @audited decorator writing AuditEvent rows on every tool call"
```

---

### Task 2: Groq LLM wrapper with retry

**Files:**
- Create: `app/llm.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: `app.config.settings.groq_api_key`.
- Produces: `AgentError(Exception)`, `get_llm() -> ChatGroq`,
  `invoke_with_retry(model, messages) -> AIMessage` (3 attempts, exponential
  backoff, raises `AgentError` on final failure).

- [ ] **Step 1: Write the failing test**

`tests/test_llm.py`:

```python
import pytest
from langchain_groq import ChatGroq

from app.llm import AgentError, get_llm, invoke_with_retry


def test_get_llm_returns_chat_groq_instance():
    assert isinstance(get_llm(), ChatGroq)


def test_invoke_with_retry_succeeds_after_transient_failures(monkeypatch):
    monkeypatch.setattr("app.llm.time.sleep", lambda _seconds: None)

    calls = {"count": 0}

    class _FlakyModel:
        def invoke(self, messages):
            calls["count"] += 1
            if calls["count"] < 3:
                raise RuntimeError("transient 503")
            return "ok"

    result = invoke_with_retry(_FlakyModel(), messages=["hi"])
    assert result == "ok"
    assert calls["count"] == 3


def test_invoke_with_retry_raises_agent_error_after_exhausting_attempts(monkeypatch):
    monkeypatch.setattr("app.llm.time.sleep", lambda _seconds: None)

    class _AlwaysFailsModel:
        def invoke(self, messages):
            raise RuntimeError("permanent failure")

    with pytest.raises(AgentError, match="permanent failure"):
        invoke_with_retry(_AlwaysFailsModel(), messages=["hi"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_llm.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.llm'`

- [ ] **Step 3: Write `app/llm.py`**

```python
import time

from langchain_groq import ChatGroq

from app.config import settings

MODEL_NAME = "llama-3.3-70b-versatile"
MAX_RETRY_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 1.0


class AgentError(Exception):
    pass


def get_llm() -> ChatGroq:
    return ChatGroq(model=MODEL_NAME, groq_api_key=settings.groq_api_key, temperature=0)


def invoke_with_retry(model, messages):
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRY_ATTEMPTS):
        try:
            return model.invoke(messages)
        except Exception as exc:
            last_exc = exc
            if attempt < MAX_RETRY_ATTEMPTS - 1:
                time.sleep(BASE_BACKOFF_SECONDS * (2**attempt))
    raise AgentError(f"LLM call failed after {MAX_RETRY_ATTEMPTS} attempts: {last_exc}") from last_exc
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_llm.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add app/llm.py tests/test_llm.py
git commit -m "Add Groq LLM client wrapper with retry-with-backoff"
```

---

### Task 3: Patient tool

**Files:**
- Create: `app/tools/__init__.py`
- Create: `app/tools/patient_tools.py`
- Create: `tests/fakes.py` (starts with one factory here; Tasks 4/6 add more
  to the same file as later tests need them — see each task's own diff)
- Test: `tests/test_patient_tools.py`

**Interfaces:**
- Consumes: `audited` (Task 1), `app.models.PatientProfile` (existing).
- Produces: `get_or_create_patient(db, user_id: str, profile_fields: dict) ->
  dict` (audited), and `get_or_create_patient_tool` (a LangChain `@tool`,
  `response_format="content_and_artifact"`, taking `profile_fields: dict`
  from the model and `user_id` injected from subgraph state via
  `InjectedState("user_id")`, `db` injected from `RunnableConfig`). Also
  `tests.fakes.make_user(db_session, role=UserRole.patient) -> User`.

- [ ] **Step 1: Write `app/tools/__init__.py` (empty)**

- [ ] **Step 2: Write `tests/fakes.py`**

```python
import uuid

from app.models import User, UserRole


def make_user(db_session, role=UserRole.patient) -> User:
    user = User(
        name="Test Patient",
        email=f"test-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="irrelevant-hash",
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    return user
```

- [ ] **Step 3: Write the failing test**

`tests/test_patient_tools.py`:

```python
from app.models import PatientProfile
from app.tools.patient_tools import get_or_create_patient
from tests.fakes import make_user


def test_creates_patient_profile_when_missing(db_session):
    user = make_user(db_session)

    result = get_or_create_patient(db_session, str(user.id), {"phone": "+1-555-0100"})

    assert result["user_id"] == str(user.id)
    assert result["phone"] == "+1-555-0100"

    profile = db_session.query(PatientProfile).filter(PatientProfile.user_id == user.id).one()
    assert profile.phone == "+1-555-0100"


def test_updates_existing_profile_fields(db_session):
    user = make_user(db_session)
    existing = PatientProfile(user_id=user.id, phone="+1-555-0000")
    db_session.add(existing)
    db_session.commit()

    result = get_or_create_patient(db_session, str(user.id), {"emergency_contact": "Jane Doe"})

    assert result["id"] == str(existing.id)
    assert result["phone"] == "+1-555-0000"
    assert result["emergency_contact"] == "Jane Doe"


def test_blank_profile_fields_do_not_overwrite_existing_values(db_session):
    user = make_user(db_session)
    existing = PatientProfile(user_id=user.id, phone="+1-555-0000")
    db_session.add(existing)
    db_session.commit()

    result = get_or_create_patient(db_session, str(user.id), {"phone": ""})

    assert result["phone"] == "+1-555-0000"
```

- [ ] **Step 4: Run test to verify it fails**

Run: `pytest tests/test_patient_tools.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.tools.patient_tools'`

- [ ] **Step 5: Write `app/tools/patient_tools.py`**

```python
import uuid
from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from sqlalchemy.orm import Session

from app.audit import audited
from app.models import PatientProfile

_UPDATABLE_FIELDS = ("phone", "preferred_language", "emergency_contact")


@audited("get_or_create_patient", "PatientProfile")
def get_or_create_patient(db: Session, user_id: str, profile_fields: dict) -> dict:
    profile = db.query(PatientProfile).filter(PatientProfile.user_id == uuid.UUID(user_id)).first()
    if profile is None:
        profile = PatientProfile(user_id=uuid.UUID(user_id))
        db.add(profile)
        db.flush()

    for field in _UPDATABLE_FIELDS:
        value = profile_fields.get(field)
        if value:
            setattr(profile, field, value)

    db.commit()
    return {
        "id": str(profile.id),
        "user_id": str(profile.user_id),
        "phone": profile.phone,
        "preferred_language": profile.preferred_language,
        "emergency_contact": profile.emergency_contact,
    }


@tool(response_format="content_and_artifact")
def get_or_create_patient_tool(
    profile_fields: dict,
    user_id: Annotated[str, InjectedState("user_id")],
    config: RunnableConfig,
):
    """Look up the patient's profile, creating one if missing, and update it
    with any contact details mentioned in the request. profile_fields may
    include phone, preferred_language, and/or emergency_contact — omit any
    field not mentioned in the request; pass {} if none are mentioned."""
    db = config["configurable"]["db"]
    result = get_or_create_patient(db, user_id, profile_fields)
    return f"Patient profile resolved: {result['id']}", result
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_patient_tools.py -v`
Expected: PASS (3 tests)

- [ ] **Step 7: Commit**

```bash
git add app/tools/__init__.py app/tools/patient_tools.py tests/fakes.py tests/test_patient_tools.py
git commit -m "Add get_or_create_patient tool with LangChain adapter"
```

---

### Task 4: Escalation tool

**Files:**
- Create: `app/tools/escalation_tools.py`
- Modify: `tests/fakes.py` (add two more factories, building on Task 3's
  `make_user`)
- Test: `tests/test_escalation_tools.py`

**Interfaces:**
- Consumes: `audited` (Task 1), `app.models.Escalation` (existing).
- Produces: `create_escalation(db, workflow_run_id: str, reason: str) ->
  dict` (audited), and `create_escalation_tool` (a LangChain `@tool`,
  `response_format="content_and_artifact"`, taking `reason: str` from the
  model and `workflow_run_id` injected from subgraph state via
  `InjectedState("workflow_run_id")`). Also
  `tests.fakes.make_patient_profile(db_session, user=None) -> PatientProfile`
  and `tests.fakes.make_workflow_run(db_session, profile=None) -> WorkflowRun`.

- [ ] **Step 1: Rewrite `tests/fakes.py` to add the two new factories**

```python
import uuid

from app.models import PatientProfile, User, UserRole, WorkflowRun


def make_user(db_session, role=UserRole.patient) -> User:
    user = User(
        name="Test Patient",
        email=f"test-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="irrelevant-hash",
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    return user


def make_patient_profile(db_session, user: User | None = None) -> PatientProfile:
    if user is None:
        user = make_user(db_session)
    profile = PatientProfile(user_id=user.id)
    db_session.add(profile)
    db_session.commit()
    return profile


def make_workflow_run(db_session, profile: PatientProfile | None = None) -> WorkflowRun:
    if profile is None:
        profile = make_patient_profile(db_session)
    workflow_run = WorkflowRun(patient_id=profile.id)
    db_session.add(workflow_run)
    db_session.commit()
    return workflow_run
```

- [ ] **Step 2: Write the failing test**

`tests/test_escalation_tools.py`:

```python
from app.models import Escalation
from app.tools.escalation_tools import create_escalation
from tests.fakes import make_workflow_run


def test_create_escalation_persists_row_with_open_status(db_session):
    workflow_run = make_workflow_run(db_session)

    result = create_escalation(db_session, str(workflow_run.id), "patient describes an emergency")

    assert result["status"] == "open"
    escalation = db_session.query(Escalation).filter(Escalation.workflow_run_id == workflow_run.id).one()
    assert escalation.reason == "patient describes an emergency"
    assert escalation.status.value == "open"


def test_create_escalation_allows_multiple_escalations_per_workflow_run(db_session):
    workflow_run = make_workflow_run(db_session)

    create_escalation(db_session, str(workflow_run.id), "first reason")
    create_escalation(db_session, str(workflow_run.id), "second reason")

    count = db_session.query(Escalation).filter(Escalation.workflow_run_id == workflow_run.id).count()
    assert count == 2
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_escalation_tools.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.tools.escalation_tools'`

- [ ] **Step 4: Write `app/tools/escalation_tools.py`**

```python
import uuid
from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from sqlalchemy.orm import Session

from app.audit import audited
from app.models import Escalation


@audited("create_escalation", "Escalation")
def create_escalation(db: Session, workflow_run_id: str, reason: str) -> dict:
    escalation = Escalation(workflow_run_id=uuid.UUID(workflow_run_id), reason=reason)
    db.add(escalation)
    db.commit()
    return {
        "id": str(escalation.id),
        "workflow_run_id": str(escalation.workflow_run_id),
        "reason": escalation.reason,
        "status": escalation.status.value,
    }


@tool(response_format="content_and_artifact")
def create_escalation_tool(
    reason: str,
    workflow_run_id: Annotated[str, InjectedState("workflow_run_id")],
    config: RunnableConfig,
):
    """Escalate this workflow run to human staff review. Call this whenever
    the request, or a prior agent's proposed action, contains a diagnosis, a
    prescription or dosage change, or describes a medical emergency. reason
    should briefly describe what triggered the escalation in administrative
    (not clinical) language."""
    db = config["configurable"]["db"]
    result = create_escalation(db, workflow_run_id, reason)
    return f"Escalated: {reason}", result
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_escalation_tools.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Commit**

```bash
git add app/tools/escalation_tools.py tests/fakes.py tests/test_escalation_tools.py
git commit -m "Add create_escalation tool with LangChain adapter"
```

---

### Task 5: Shared parent workflow state

**Files:**
- Create: `app/agents/__init__.py`
- Create: `app/agents/state.py`

**Interfaces:**
- Produces: `WorkflowState` (TypedDict) — the parent graph's state shape.
  Deliberately has **no `messages` key** — each agent's message history is
  private to that agent's own subgraph (Tasks 6/7) and never persisted here.

- [ ] **Step 1: Write `app/agents/__init__.py` (empty)**

- [ ] **Step 2: Write `app/agents/state.py`**

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

No test for this file — it's a single type declaration with no behavior;
Task 6/7's node tests exercise it directly.

- [ ] **Step 3: Commit**

```bash
git add app/agents/__init__.py app/agents/state.py
git commit -m "Add parent WorkflowState TypedDict (no message history — that's per-agent)"
```

---

### Task 6: Safety & Escalation agent (private subgraph + parent-facing node)

**Files:**
- Create: `app/agents/safety.py`
- Modify: `tests/fakes.py` (add the fake-LLM helpers and a `workflow_state`
  factory, building on Tasks 3/4's `make_user`/`make_patient_profile`/
  `make_workflow_run`)
- Test: `tests/test_safety_agent.py`

**Interfaces:**
- Consumes: `get_llm`, `invoke_with_retry` (Task 2), `create_escalation_tool`
  (Task 4), `WorkflowState` (Task 5).
- Produces (internal, subgraph-scoped): `SafetyState` (TypedDict:
  `messages`, `workflow_run_id`, `escalation`), `safety_llm_node(state,
  config)`, `safety_capture_node(state, config)`, `route_after_safety_llm(state)
  -> "safety_tools" | "__end__"`, `build_safety_subgraph()`.
- Produces (parent-facing): `safety_agent_node(state: WorkflowState, config)
  -> {"escalation": dict | None}` — the function registered as a node in the
  parent graph (Task 8). Seeds the subgraph from `state["request_text"]` and
  `state["workflow_run_id"]`, invokes it, returns only `escalation`.
- Also `tests.fakes.FakeToolCallingModel`, `ai_message_with_tool_call`,
  `ai_message_text`, `workflow_state(**overrides) -> dict` — shared by this
  task's tests and Tasks 7/9.

Sources backing this shape (checked against installed `langgraph==1.2.9`,
`langchain-core==1.5.0`): [InjectedState reference](https://reference.langchain.com/python/langgraph.prebuilt/tool_node/InjectedState)
confirms `InjectedState` is resolved by `ToolNode` during execution, which is
why the tool must run inside a real `ToolNode`-backed subgraph, not a
hand-rolled dispatch loop; [Subgraphs docs](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)
confirm a subgraph with a different (private) state schema than its parent
is the documented way to keep one agent's message history invisible to
another.

- [ ] **Step 1: Rewrite `tests/fakes.py` to add the fake-LLM helpers and `workflow_state`**

```python
import uuid

from langchain_core.messages import AIMessage

from app.models import PatientProfile, User, UserRole, WorkflowRun


def make_user(db_session, role=UserRole.patient) -> User:
    user = User(
        name="Test Patient",
        email=f"test-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="irrelevant-hash",
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    return user


def make_patient_profile(db_session, user: User | None = None) -> PatientProfile:
    if user is None:
        user = make_user(db_session)
    profile = PatientProfile(user_id=user.id)
    db_session.add(profile)
    db_session.commit()
    return profile


def make_workflow_run(db_session, profile: PatientProfile | None = None) -> WorkflowRun:
    if profile is None:
        profile = make_patient_profile(db_session)
    workflow_run = WorkflowRun(patient_id=profile.id)
    db_session.add(workflow_run)
    db_session.commit()
    return workflow_run


def workflow_state(**overrides) -> dict:
    state = {
        "workflow_run_id": "11111111-1111-1111-1111-111111111111",
        "patient_id": "22222222-2222-2222-2222-222222222222",
        "user_id": "u1",
        "request_text": "book a cardiology appointment",
        "uploaded_files": [],
        "intent": None,
        "department_id": None,
        "appointment_id": None,
        "document_ids": [],
        "reminder_ids": [],
        "escalation": None,
        "status": "running",
    }
    state.update(overrides)
    return state


class FakeToolCallingModel:
    """Stands in for a ChatGroq model bound to tools: .bind_tools() is a
    no-op returning self, .invoke() returns the next scripted response."""

    def __init__(self, responses: list[AIMessage]):
        self._responses = list(responses)

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        return self._responses.pop(0)


def ai_message_with_tool_call(name: str, args: dict, call_id: str = "call_1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])


def ai_message_text(text: str) -> AIMessage:
    return AIMessage(content=text)
```

- [ ] **Step 2: Write the failing test**

`tests/test_safety_agent.py`:

```python
from langchain_core.messages import HumanMessage, ToolMessage

from app.agents.safety import (
    route_after_safety_llm,
    safety_agent_node,
    safety_capture_node,
    safety_llm_node,
)
from app.models import Escalation
from tests.fakes import (
    FakeToolCallingModel,
    ai_message_text,
    ai_message_with_tool_call,
    make_workflow_run,
    workflow_state,
)


def _safety_state(**overrides):
    state = {
        "messages": [HumanMessage("request: I need to book a cardiology appointment")],
        "workflow_run_id": "11111111-1111-1111-1111-111111111111",
        "escalation": None,
    }
    state.update(overrides)
    return state


def test_safety_llm_node_with_no_tool_call_routes_to_end(monkeypatch):
    fake_model = FakeToolCallingModel([ai_message_text("SAFE")])
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: fake_model)

    state = _safety_state()
    update = safety_llm_node(state, config={"configurable": {}})
    state["messages"] = state["messages"] + update["messages"]

    assert route_after_safety_llm(state) == "__end__"


def test_safety_llm_node_with_tool_call_routes_to_tools(monkeypatch):
    fake_model = FakeToolCallingModel(
        [ai_message_with_tool_call("create_escalation_tool", {"reason": "describes an emergency"})]
    )
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: fake_model)

    state = _safety_state()
    update = safety_llm_node(state, config={"configurable": {}})
    state["messages"] = state["messages"] + update["messages"]

    assert route_after_safety_llm(state) == "safety_tools"


def test_safety_capture_node_sets_escalation_from_tool_message():
    tool_message = ToolMessage(
        content="Escalated: describes an emergency",
        artifact={"id": "e1", "reason": "describes an emergency", "status": "open"},
        tool_call_id="call_1",
        name="create_escalation_tool",
    )
    state = _safety_state(messages=[tool_message])

    update = safety_capture_node(state, config={"configurable": {}})

    assert update == {"escalation": {"id": "e1", "reason": "describes an emergency", "status": "open"}}


def test_safety_agent_node_returns_no_escalation_for_safe_request(monkeypatch):
    fake_model = FakeToolCallingModel([ai_message_text("SAFE")])
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: fake_model)

    update = safety_agent_node(workflow_state(), config={"configurable": {"db": None}})

    assert update == {"escalation": None}


def test_safety_agent_node_returns_escalation_and_persists_it(monkeypatch, db_session):
    workflow_run = make_workflow_run(db_session)

    fake_model = FakeToolCallingModel(
        [ai_message_with_tool_call("create_escalation_tool", {"reason": "describes chest pain, an emergency"})]
    )
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: fake_model)

    state = workflow_state(
        workflow_run_id=str(workflow_run.id),
        patient_id=str(workflow_run.patient_id),
        request_text="I have chest pain, what's wrong with me?",
    )

    update = safety_agent_node(state, config={"configurable": {"db": db_session}})

    assert update["escalation"]["reason"] == "describes chest pain, an emergency"

    escalation = db_session.query(Escalation).filter(Escalation.workflow_run_id == workflow_run.id).one()
    assert escalation.reason == "describes chest pain, an emergency"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_safety_agent.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.agents.safety'`

- [ ] **Step 4: Write `app/agents/safety.py`**

```python
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from app.agents.state import WorkflowState
from app.llm import get_llm, invoke_with_retry
from app.tools.escalation_tools import create_escalation_tool

SAFETY_SYSTEM_PROMPT = (
    "You are the Safety & Escalation Agent for AgentCare, an administrative "
    "healthcare workflow assistant. You never diagnose, prescribe, or advise "
    "on treatment. Call create_escalation whenever the patient's request "
    "describes a medical emergency, asks for a diagnosis, or asks to "
    "prescribe or change a medication dosage. Purely administrative "
    "requests (booking, rescheduling, cancelling an appointment, submitting "
    "a document) are always safe and must not be escalated. If the request "
    "is safe, reply with the single word SAFE and do not call any tool."
)

safety_tools = [create_escalation_tool]
safety_tools_node = ToolNode(safety_tools)


class SafetyState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    workflow_run_id: str
    escalation: dict | None


def safety_llm_node(state: SafetyState, config):
    model = get_llm().bind_tools(safety_tools)
    messages = [SystemMessage(SAFETY_SYSTEM_PROMPT), *state["messages"]]
    ai_message = invoke_with_retry(model, messages)
    return {"messages": [ai_message]}


def safety_capture_node(state: SafetyState, config):
    last = state["messages"][-1]
    if isinstance(last, ToolMessage) and last.name == "create_escalation_tool":
        return {"escalation": last.artifact}
    return {}


def route_after_safety_llm(state: SafetyState) -> Literal["safety_tools", "__end__"]:
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None):
        return "safety_tools"
    return "__end__"


def build_safety_subgraph():
    graph = StateGraph(SafetyState)
    graph.add_node("safety_llm", safety_llm_node)
    graph.add_node("safety_tools", safety_tools_node)
    graph.add_node("safety_capture", safety_capture_node)
    graph.set_entry_point("safety_llm")
    graph.add_conditional_edges(
        "safety_llm", route_after_safety_llm, {"safety_tools": "safety_tools", "__end__": END}
    )
    graph.add_edge("safety_tools", "safety_capture")
    graph.add_edge("safety_capture", END)
    return graph.compile()


_safety_subgraph = build_safety_subgraph()


def safety_agent_node(state: WorkflowState, config) -> dict:
    """Parent-graph node (registered as "safety_agent" in app/graph.py).
    Invokes the private Safety subgraph and returns only the field that
    belongs in WorkflowState — the subgraph's own messages never leave it."""
    result = _safety_subgraph.invoke(
        {
            "messages": [HumanMessage(f"request: {state['request_text']}")],
            "workflow_run_id": state["workflow_run_id"],
            "escalation": None,
        },
        config=config,
    )
    return {"escalation": result.get("escalation")}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_safety_agent.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Commit**

```bash
git add app/agents/safety.py tests/fakes.py tests/test_safety_agent.py
git commit -m "Add Safety & Escalation agent as a private subgraph with a parent-facing node"
```

---

### Task 7: Coordinator agent (private subgraph + parent-facing node)

**Files:**
- Create: `app/agents/coordinator.py`
- Test: `tests/test_coordinator_agent.py`

**Interfaces:**
- Consumes: `get_llm`, `invoke_with_retry` (Task 2), `get_or_create_patient_tool`
  (Task 3), `WorkflowState` (Task 5), `tests/fakes.py` (Task 6).
- Produces (internal, subgraph-scoped): `CoordinatorState` (TypedDict:
  `messages`, `user_id`, `patient_id`, `intent`), `coordinator_llm_node(state,
  config)`, `coordinator_capture_node(state, config)`,
  `coordinator_finalize_node(state, config)`, `route_after_coordinator_llm(state)
  -> "coordinator_tools" | "coordinator_finalize"`, `build_coordinator_subgraph()`.
- Produces (parent-facing): `coordinator_agent_node(state: WorkflowState,
  config) -> {"patient_id": str | None, "intent": str | None}` — the
  function registered as a node in the parent graph (Task 8).

- [ ] **Step 1: Write the failing test**

`tests/test_coordinator_agent.py`:

```python
from langchain_core.messages import HumanMessage, ToolMessage

from app.agents.coordinator import (
    coordinator_agent_node,
    coordinator_capture_node,
    coordinator_finalize_node,
    coordinator_llm_node,
    route_after_coordinator_llm,
)
from app.models import PatientProfile
from tests.fakes import FakeToolCallingModel, ai_message_text, ai_message_with_tool_call, make_user, workflow_state


def _coordinator_state(**overrides):
    state = {
        "messages": [HumanMessage("user_id: u1\nrequest: book a cardiology appointment")],
        "user_id": "u1",
        "patient_id": None,
        "intent": None,
    }
    state.update(overrides)
    return state


def test_coordinator_llm_node_with_tool_call_routes_to_tools(monkeypatch):
    fake_model = FakeToolCallingModel(
        [ai_message_with_tool_call("get_or_create_patient_tool", {"profile_fields": {}})]
    )
    monkeypatch.setattr("app.agents.coordinator.get_llm", lambda: fake_model)

    state = _coordinator_state()
    update = coordinator_llm_node(state, config={"configurable": {}})
    state["messages"] = state["messages"] + update["messages"]

    assert route_after_coordinator_llm(state) == "coordinator_tools"


def test_coordinator_llm_node_with_no_tool_call_routes_to_finalize(monkeypatch):
    fake_model = FakeToolCallingModel([ai_message_text("book_appointment")])
    monkeypatch.setattr("app.agents.coordinator.get_llm", lambda: fake_model)

    state = _coordinator_state()
    update = coordinator_llm_node(state, config={"configurable": {}})
    state["messages"] = state["messages"] + update["messages"]

    assert route_after_coordinator_llm(state) == "coordinator_finalize"


def test_coordinator_capture_node_sets_patient_id_from_tool_message():
    tool_message = ToolMessage(
        content="Patient profile resolved: p1",
        artifact={"id": "p1", "user_id": "u1", "phone": None},
        tool_call_id="call_1",
        name="get_or_create_patient_tool",
    )
    state = _coordinator_state(messages=[tool_message])

    update = coordinator_capture_node(state, config={"configurable": {}})

    assert update == {"patient_id": "p1"}


def test_coordinator_finalize_node_sets_intent_from_final_ai_message():
    state = _coordinator_state(messages=[ai_message_text("book_appointment")])

    update = coordinator_finalize_node(state, config={"configurable": {}})

    assert update == {"intent": "book_appointment"}


def test_coordinator_agent_node_returns_patient_id_and_intent(monkeypatch, db_session):
    user = make_user(db_session)

    fake_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("get_or_create_patient_tool", {"profile_fields": {}}),
            ai_message_text("book_appointment"),
        ]
    )
    monkeypatch.setattr("app.agents.coordinator.get_llm", lambda: fake_model)

    state = workflow_state(patient_id=None, user_id=str(user.id))

    update = coordinator_agent_node(state, config={"configurable": {"db": db_session}})

    assert update["intent"] == "book_appointment"
    assert update["patient_id"] is not None

    profile = db_session.query(PatientProfile).filter(PatientProfile.user_id == user.id).one()
    assert str(profile.id) == update["patient_id"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_coordinator_agent.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.agents.coordinator'`

- [ ] **Step 3: Write `app/agents/coordinator.py`**

```python
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from app.agents.state import WorkflowState
from app.llm import get_llm, invoke_with_retry
from app.tools.patient_tools import get_or_create_patient_tool

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

coordinator_tools = [get_or_create_patient_tool]
coordinator_tools_node = ToolNode(coordinator_tools)


class CoordinatorState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    user_id: str
    patient_id: str | None
    intent: str | None


def coordinator_llm_node(state: CoordinatorState, config):
    model = get_llm().bind_tools(coordinator_tools)
    messages = [SystemMessage(COORDINATOR_SYSTEM_PROMPT), *state["messages"]]
    ai_message = invoke_with_retry(model, messages)
    return {"messages": [ai_message]}


def coordinator_capture_node(state: CoordinatorState, config):
    last = state["messages"][-1]
    if isinstance(last, ToolMessage) and last.name == "get_or_create_patient_tool":
        return {"patient_id": last.artifact["id"]}
    return {}


def coordinator_finalize_node(state: CoordinatorState, config):
    last = state["messages"][-1]
    return {"intent": last.content}


def route_after_coordinator_llm(state: CoordinatorState) -> Literal["coordinator_tools", "coordinator_finalize"]:
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None):
        return "coordinator_tools"
    return "coordinator_finalize"


def build_coordinator_subgraph():
    graph = StateGraph(CoordinatorState)
    graph.add_node("coordinator_llm", coordinator_llm_node)
    graph.add_node("coordinator_tools", coordinator_tools_node)
    graph.add_node("coordinator_capture", coordinator_capture_node)
    graph.add_node("coordinator_finalize", coordinator_finalize_node)
    graph.set_entry_point("coordinator_llm")
    graph.add_conditional_edges(
        "coordinator_llm",
        route_after_coordinator_llm,
        {"coordinator_tools": "coordinator_tools", "coordinator_finalize": "coordinator_finalize"},
    )
    graph.add_edge("coordinator_tools", "coordinator_capture")
    graph.add_edge("coordinator_capture", "coordinator_llm")
    graph.add_edge("coordinator_finalize", END)
    return graph.compile()


_coordinator_subgraph = build_coordinator_subgraph()


def coordinator_agent_node(state: WorkflowState, config) -> dict:
    """Parent-graph node (registered as "coordinator_agent" in app/graph.py).
    Invokes the private Coordinator subgraph and returns only the fields
    that belong in WorkflowState."""
    result = _coordinator_subgraph.invoke(
        {
            "messages": [HumanMessage(f"user_id: {state['user_id']}\nrequest: {state['request_text']}")],
            "user_id": state["user_id"],
            "patient_id": None,
            "intent": None,
        },
        config=config,
    )
    return {"patient_id": result.get("patient_id"), "intent": result.get("intent")}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_coordinator_agent.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add app/agents/coordinator.py tests/test_coordinator_agent.py
git commit -m "Add Coordinator agent as a private subgraph with a parent-facing node"
```

---

### Task 8: Parent graph wiring

**Files:**
- Create: `app/graph.py`

**Interfaces:**
- Consumes: `safety_agent_node` (Task 6), `coordinator_agent_node` (Task 7),
  `WorkflowState` (Task 5).
- Produces: `build_graph()` — returns a compiled parent `StateGraph` with
  exactly one node per agent: entry point `safety_agent`, ending at `END`
  either via escalation or via `coordinator_agent`.

- [ ] **Step 1: Write `app/graph.py`**

```python
from typing import Literal

from langgraph.graph import END, StateGraph

from app.agents.coordinator import coordinator_agent_node
from app.agents.safety import safety_agent_node
from app.agents.state import WorkflowState


def route_after_safety(state: WorkflowState) -> Literal["coordinator_agent", "__end__"]:
    if state.get("escalation"):
        return "__end__"
    return "coordinator_agent"


def build_graph():
    graph = StateGraph(WorkflowState)

    graph.add_node("safety_agent", safety_agent_node)
    graph.add_node("coordinator_agent", coordinator_agent_node)

    graph.set_entry_point("safety_agent")
    graph.add_conditional_edges(
        "safety_agent", route_after_safety, {"coordinator_agent": "coordinator_agent", "__end__": END}
    )
    graph.add_edge("coordinator_agent", END)

    return graph.compile()
```

Each later phase (routing, appointment, document, follow-up) extends this
file with exactly one `add_node` + one `add_edge`/`add_conditional_edges`
per agent — the parent graph never grows the 3-nodes-per-agent way the
subgraphs do internally.

No standalone test here — `build_graph()` is exercised end-to-end by Task
9's `test_workflow_runner.py`, which is the real proof this wiring works.

- [ ] **Step 2: Sanity-check the graph compiles**

```bash
python -c "from app.graph import build_graph; build_graph(); print('graph compiled OK')"
```

Expected: prints `graph compiled OK` with no exception.

- [ ] **Step 3: Commit**

```bash
git add app/graph.py
git commit -m "Wire Safety and Coordinator agent nodes into the compiled parent StateGraph"
```

---

### Task 9: Workflow runner (checkpointing + end-to-end proof)

**Files:**
- Create: `app/workflow_runner.py`
- Test: `tests/test_workflow_runner.py`

**Interfaces:**
- Consumes: `build_graph` (Task 8), `app.models.WorkflowRun`,
  `WorkflowStatus` (existing).
- Produces: `run_workflow(db, patient_id: str, user_id: str, request_text:
  str, uploaded_files: list[str] | None = None) -> WorkflowRun`. Creates the
  `WorkflowRun` row, streams the parent graph, persists `WorkflowRun.state`
  and `current_step` after every agent, and sets final `status` to
  `needs_review` (escalated), `failed` (exception), or leaves it `running`
  with `current_step="routing_agent"` (reached the end of what's wired so
  far — routing/appointment/document/follow-up agents are later phases, so
  this graph does not yet produce a `completed` run). Because `WorkflowState`
  has no `messages` key, the persisted state is just the structured business
  fields — no message serialization step needed.

- [ ] **Step 1: Write the failing test**

`tests/test_workflow_runner.py`:

```python
from app.models import AuditEvent, Escalation, WorkflowStatus
from app.workflow_runner import run_workflow
from tests.fakes import (
    FakeToolCallingModel,
    ai_message_text,
    ai_message_with_tool_call,
    make_patient_profile,
    make_user,
)


def test_emergency_request_ends_needs_review_with_escalation_row(monkeypatch, db_session):
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)

    safety_model = FakeToolCallingModel(
        [ai_message_with_tool_call("create_escalation_tool", {"reason": "patient describes chest pain and shortness of breath"})]
    )
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: safety_model)

    workflow_run = run_workflow(
        db_session,
        patient_id=str(profile.id),
        user_id=str(user.id),
        request_text="I have severe chest pain and can't breathe, what's wrong with me?",
    )

    assert workflow_run.status == WorkflowStatus.needs_review
    assert workflow_run.current_step == "safety_agent"

    escalation = db_session.query(Escalation).filter(Escalation.workflow_run_id == workflow_run.id).one()
    assert "chest pain" in escalation.reason

    audit_actions = {
        e.action for e in db_session.query(AuditEvent).filter(AuditEvent.entity_type == "Escalation").all()
    }
    assert "create_escalation" in audit_actions


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

    workflow_run = run_workflow(
        db_session,
        patient_id=str(profile.id),
        user_id=str(user.id),
        request_text="I'd like to book a cardiology appointment next week",
    )

    assert workflow_run.status == WorkflowStatus.running
    assert workflow_run.current_step == "routing_agent"
    assert workflow_run.state["intent"] == "book_appointment"
    assert workflow_run.state["patient_id"] is not None
    assert workflow_run.state["escalation"] is None
    assert "messages" not in workflow_run.state

    escalation_count = (
        db_session.query(Escalation).filter(Escalation.workflow_run_id == workflow_run.id).count()
    )
    assert escalation_count == 0


def test_unhandled_node_exception_marks_workflow_failed(monkeypatch, db_session):
    user = make_user(db_session)
    profile = make_patient_profile(db_session, user=user)

    def _boom():
        raise RuntimeError("groq is down")

    monkeypatch.setattr("app.agents.safety.get_llm", _boom)

    workflow_run = run_workflow(
        db_session,
        patient_id=str(profile.id),
        user_id=str(user.id),
        request_text="book an appointment",
    )

    assert workflow_run.status == WorkflowStatus.failed
    assert "groq is down" in workflow_run.state["error"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_workflow_runner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.workflow_runner'`

- [ ] **Step 3: Write `app/workflow_runner.py`**

```python
import uuid

from app.graph import build_graph
from app.models import WorkflowRun, WorkflowStatus

_compiled_graph = build_graph()


def run_workflow(
    db,
    patient_id: str,
    user_id: str,
    request_text: str,
    uploaded_files: list[str] | None = None,
) -> WorkflowRun:
    workflow_run = WorkflowRun(
        patient_id=uuid.UUID(patient_id),
        current_step="safety_agent",
        state={},
        status=WorkflowStatus.running,
    )
    db.add(workflow_run)
    db.commit()

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

    config = {"configurable": {"db": db}}
    full_state = dict(initial_state)

    try:
        for step in _compiled_graph.stream(initial_state, config=config, stream_mode="updates"):
            for node_name, update in step.items():
                full_state.update(update)
                workflow_run.current_step = node_name
                workflow_run.state = dict(full_state)
                db.commit()
    except Exception as exc:
        workflow_run.status = WorkflowStatus.failed
        workflow_run.state = {**full_state, "error": str(exc)}
        db.commit()
        return workflow_run

    if full_state.get("escalation"):
        workflow_run.status = WorkflowStatus.needs_review
    else:
        workflow_run.status = WorkflowStatus.running
        workflow_run.current_step = "routing_agent"

    workflow_run.state = dict(full_state)
    db.commit()
    return workflow_run
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_workflow_runner.py -v`
Expected: PASS (3 tests) — proves the escalation path persists an
`Escalation` row and flips status to `needs_review`, the benign path
reaches the routing boundary with a real (mocked-LLM-produced) `intent` and
the tool-produced `patient_id` both persisted in `WorkflowRun.state` (and
no `messages` key bloating it), and a node-level exception is caught and
persisted as `failed` rather than crashing.

- [ ] **Step 5: Run the full test suite to confirm no regressions**

```bash
pytest -v
```

Expected: all tests pass, including the pre-existing config/models/auth/rbac
suites from the foundation plan.

- [ ] **Step 6: Commit**

```bash
git add app/workflow_runner.py tests/test_workflow_runner.py
git commit -m "Add workflow runner: streams the parent graph, checkpoints WorkflowRun after every agent"
```

---

## Self-review

**Spec coverage:**
- Own prompt + own bound tool per agent (Safety, Coordinator) — Tasks 6/7. ✓
- `ToolNode` for tool execution, `@tool` for schema generation, no custom
  dispatch — Tasks 3/4/6/7. ✓
- Audit decorator writes `AuditEvent` on every tool call, success or failure,
  independent of the LLM — Task 1, used by Tasks 3/4. ✓
- `WorkflowRun.state`/`current_step` checkpointed after every agent (not
  just at the end) — Task 9. ✓
- Safety Agent blocks diagnosis/prescription/dosage/emergency language via
  escalation, administrative language passes through — Task 6, proven in
  Task 9's escalation test. ✓
- Groq calls wrapped in retry-with-backoff, typed `AgentError` on exhaustion
  — Task 2, used inside both subgraphs. ✓
- Graph-level exception caught, `WorkflowRun.status` flips to `failed` with
  the error persisted, no unhandled crash — Task 9's third test. ✓
- Tests mock the LLM (`FakeToolCallingModel`), never assert on prompt
  wording — Tasks 6/7/9. ✓
- No tool returns a fixed response — both tools do real `INSERT`/`UPDATE`
  and reads (Tasks 3/4). ✓
- No hardcoded final response — `intent` comes from the (mocked) LLM's own
  final message; `escalation`/`patient_id` come from rows the tools just
  wrote, lifted via `ToolMessage.artifact`. ✓
- Each agent's message history stays private to its own subgraph — no
  agent's LLM call ever sees another agent's tool-calling exchange, and
  `WorkflowRun.state` persists only structured fields — Tasks 6/7/9. ✓

**Explicitly out of scope for this plan** (next phases per design spec §13,
same pattern as the foundation plan deferring this one):
- Department Routing, Appointment, Document, and Follow-up agents — each
  becomes its own private subgraph + one more `add_node`/`add_edge` pair in
  `app/graph.py`, following the exact shape Tasks 6-8 establish. The parent
  graph currently ends at `coordinator_agent` with `current_step =
  "routing_agent"`, the exact seam the next phase's plan extends from.
- The second Safety & Escalation pass (post-followup) — only the pre-check
  exists yet, since there's nothing downstream to re-check.
- Any HTTP route that calls `run_workflow` — this plan proves the agent
  loop itself; wiring `/requests/new` belongs with whichever phase first has
  enough of the business workflow to be worth exposing to a patient.
- Resuming an in-progress `WorkflowRun` from its persisted `state` — today
  every call starts a fresh graph run; since `WorkflowRun.state` is now just
  structured fields (no message history to reconstruct), resuming would mean
  re-seeding the next agent's subgraph from those fields directly — simpler
  than the original messages-based design, but still not needed to prove
  the checkpointing mechanism works, which this plan's failure test already
  does.
- Any live, per-tool-call streaming view of an agent's internal reasoning —
  each subgraph's tool-calling turns are invisible to the parent graph's
  `stream_mode="updates"` by design. AgentCare's UI is server-rendered
  Jinja2 pages, not a live agent-thinking view, so this isn't needed; if a
  future phase wants it, a wrapper node can return a small `trace` list
  alongside its structured fields without exposing raw messages.

**Placeholder scan:** no TBD/TODO; every step has literal file content and
exact commands.

**Type consistency:** `WorkflowState` (Task 5) has no `messages` key;
`SafetyState` (Task 6) and `CoordinatorState` (Task 7) each declare their
own private `messages` key plus only the fields their own tools need
(`workflow_run_id`+`escalation`; `user_id`+`patient_id`+`intent`).
`safety_agent_node`/`coordinator_agent_node`'s parameter type is
`WorkflowState` (they're parent-graph nodes); every other function in
Tasks 6/7 takes the subgraph-local state type. `create_escalation_tool` /
`get_or_create_patient_tool` names used in `route_after_*`/`*_capture_node`
string comparisons match the `@tool`-decorated function names exactly.
`run_workflow`'s signature (`db, patient_id, user_id, request_text,
uploaded_files=None`) is used identically across all three of Task 9's
tests.

---

## What's next (not in this plan)

Phase 3 (Department Routing + Appointment agents, each a new subgraph +
one more parent-graph node), Phase 4 (Document agent), Phase 5 (Follow-up
agent + second Safety pass), Phase 6 (UI polish) — each gets its own plan
file once this one is reviewed and merged, per the design spec's phase
breakdown (`docs/superpowers/specs/2026-07-22-agentcare-design.md` §13).
The foundation plan's still-open Tasks 6-8 (auth routes, RBAC dashboard,
seed script) are independent of this plan and can land in either order.
