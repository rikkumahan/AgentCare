# AgentCare

Agentic AI for patient administration and care coordination — built for the
**AgentCare Build Challenge 2026** (see [`problem_statement.md`](problem_statement.md)
for the full spec).

AgentCare is not a diagnosis or treatment system. It handles the
administrative side of a patient's journey — registration, department
routing, appointment booking, document collection, reminders, and
follow-up — while keeping every medical decision under human supervision.

## What it does

A patient submits a free-text request, e.g.:

> "I need a cardiology appointment next week. I also want to attach my previous ECG."

The system runs this through a LangGraph workflow of cooperating agents that:

1. identify or create the patient record,
2. check the request for diagnosis/prescription/emergency language and escalate
   if needed,
3. detect the administrative intent(s) and ask the patient to disambiguate if
   more than one is present,
4. route the request to the correct department,
5. find available slots, check conflicts, and book/reschedule/cancel the
   appointment,
6. ingest and classify attached documents, detect duplicates/missing
   requirements,
7. persist the full workflow state after every step,
8. render a confirmation from the rows just written to the database (never a
   hardcoded string),
9. create reminders and flag incomplete workflows for follow-up.

## Architecture

**Orchestration:** LangGraph. Each agent below is its own graph node with its
own system prompt and its own bound tools (LangChain `@tool` + prebuilt
`ToolNode`). The Coordinator subgraph drives intent detection and delegates
to the others; the top-level graph in [`app/graph.py`](app/graph.py) wires
Safety → Coordinator → Document → Routing/Appointment, with dedicated nodes
for the states where the workflow pauses for patient input (clarification,
intent selection, slot/appointment selection).

| Agent | File | Responsibility | Tools |
|---|---|---|---|
| **Safety & Escalation** | [`app/agents/safety.py`](app/agents/safety.py) | Runs first; blocks diagnosis/prescription/dosage language and emergency requests; creates escalation records | [`create_escalation`](app/tools/escalation_tools.py) |
| **Coordinator** | [`app/agents/coordinator.py`](app/agents/coordinator.py) | Detects intent(s), opens/resumes the `WorkflowRun`, delegates in order, combines outputs, renders the final confirmation from persisted rows | [`get_or_create_patient`](app/tools/patient_tools.py) |
| **Department Routing** | [`app/agents/routing.py`](app/agents/routing.py) | Classifies the request against the live department list, handles ambiguity, escalates unsupported requests | [`lookup_departments`](app/tools/department_tools.py) |
| **Appointment** | [`app/agents/appointment.py`](app/agents/appointment.py) | Availability, conflict checks, book/reschedule/cancel | [`check_slot_availability`, `book_or_modify_appointment`](app/tools/appointment_tools.py) |
| **Document** | [`app/agents/document.py`](app/agents/document.py) | Ingest, classify, checksum, duplicate/missing-document detection, maps files to the patient | [`store_and_classify_document`](app/tools/document_tools.py) |
| **Follow-up** | [`app/tools/`](app/tools) (reminder + scan tools, invoked from the workflow runner) | Appointment reminders, post-visit tasks, scans for missed/incomplete workflows | `create_reminder`, `scan_incomplete_workflows` |

Every tool above is wrapped by an audit decorator that writes an `AuditEvent`
row on every call (success or failure) — the audit trail doesn't depend on an
agent remembering to log it.

**LLM:** Groq via `langchain-groq` (`ChatGroq`), swappable behind LangChain's
model interface.

**Backend:** FastAPI + server-rendered Jinja2 templates (no SPA). RBAC
(`patient` / `staff`) is enforced in route dependencies (see
[`app/rbac.py`](app/rbac.py)), not just hidden in the UI.

**Database:** PostgreSQL via SQLAlchemy, schema owned by Alembic migrations
([`alembic/versions/`](alembic/versions)). Core entities: `User`,
`PatientProfile`, `Department`, `Doctor`, `AppointmentSlot`, `Appointment`,
`PatientDocument`, `WorkflowRun` (persists the serialized LangGraph state,
checkpointed after every node — this is what makes a workflow resumable
across restarts), `Reminder`, `Escalation`, `AuditEvent`.

**Documents:** stored on the local filesystem under `./storage/<patient_id>/...`,
with checksum + path + type recorded on `PatientDocument`.

**Reminders:** persisted `Reminder` rows with an audit trail; delivery is
simulated (log + in-app), not real SMTP/SMS.

## Running it

### Docker (recommended — zero external accounts needed)

```bash
cp .env.example .env
# edit .env and set GROQ_API_KEY (get a free key at https://console.groq.com)
docker compose up --build
```

This starts Postgres, runs `alembic upgrade head`, and serves the app at
http://localhost:8000. On Windows, [`open-db.bat`](open-db.bat) is a personal
dev convenience that also spins up Adminer for browsing the DB — not part of
the submission itself.

Seed synthetic sample data (departments, doctors, slots, sample users):

```bash
docker compose exec app python -m seed.seed_data
```

### Local (without Docker)

Requires Python 3.11+ and a local Postgres instance.

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows; use `source .venv/bin/activate` on macOS/Linux
pip install -r requirements.txt
cp .env.example .env          # edit DATABASE_URL / GROQ_API_KEY as needed
alembic upgrade head
python -m seed.seed_data
uvicorn app.main:app --reload
```

### Tests

```bash
pytest
```

Tests mock the LLM call and focus on tool functions and route-level RBAC —
prompt wording isn't tested.

## Configuration

All config is env-based via `pydantic-settings` ([`app/config.py`](app/config.py)),
read from `.env`. See [`.env.example`](.env.example) for the full list
(`DATABASE_URL`, `GROQ_API_KEY`, `SECRET_KEY`, `STORAGE_DIR`, `ENV`). No
secrets are committed; `.env` is gitignored.

## Project layout

```
app/
  agents/        LangGraph agent nodes + subgraphs (one file per agent)
  tools/         @tool-decorated functions the agents invoke
  routes/        FastAPI routers (auth, dashboard, patient requests)
  templates/     Jinja2 templates (patient + staff views)
  graph.py       Top-level LangGraph wiring
  workflow_runner.py   Drives a WorkflowRun through the graph, persists state
  models.py      SQLAlchemy models
  audit.py       Tool-call audit decorator
  rbac.py        Backend role enforcement
alembic/         Migrations (source of truth for schema)
seed/            Synthetic sample data
tests/           pytest suite
docs/            Design specs and implementation plans written during development
```
