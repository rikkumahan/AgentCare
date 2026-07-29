# AgentCare

Agentic AI for patient administration and care coordination — built for the
**AgentCare Build Challenge 2026** (see [`problem_statement.md`](problem_statement.md)
for the full spec).

AgentCare is not a diagnosis or treatment system. It handles the
administrative side of a patient's journey — registration, department
routing, appointment booking/rescheduling/cancelling, and document
collection — while keeping every medical decision under human supervision.
Reminders and automated follow-up are designed but not yet built (see
Architecture below).

## Demo

[![AgentCare demo video](https://img.youtube.com/vi/md7nzJh95NM/maxresdefault.jpg)](https://youtu.be/md7nzJh95NM)

## What it does

A patient submits a free-text request, e.g.:

> "I need a cardiology appointment next week. I also want to attach my previous ECG."

The system runs this through a LangGraph workflow of cooperating agents that:

1. identify or create the patient record,
2. check the request for diagnosis/prescription/emergency language and escalate
   if needed,
3. detect the administrative intent(s) — if the request contains more than
   one distinct ask (e.g. "cancel my old appointment and book a new one"),
   the patient is asked which to handle first, then automatically offered
   the rest once the first one finishes, instead of silently dropping it,
4. route the request to the correct department,
5. find real available slots and let the patient pick one — booking,
   rescheduling, and cancelling are deterministic actions on real DB rows
   the patient clicks through, never an LLM guessing on their behalf,
6. ingest and classify attached documents, detect duplicates/missing
   requirements,
7. persist the full workflow state after every step,
8. render a confirmation from the rows just written to the database (never a
   hardcoded string).

Patients also have a dedicated **My Appointments** page to cancel or
reschedule an existing appointment directly, without typing a new request.
Staff have a read-only **appointment schedule** view grouped by doctor.

## Architecture

**Orchestration:** LangGraph. Each LLM-driven agent below is its own graph
node with its own system prompt and its own bound tools (LangChain `@tool` +
prebuilt `ToolNode`). The top-level graph in [`app/graph.py`](app/graph.py)
wires Safety → Coordinator → Document → Routing, with dedicated nodes for
every state where the workflow pauses for patient input (clarification,
intent selection, department selection, appointment selection, slot
selection). Once a department or a target appointment is known, everything
downstream — checking availability, booking, rescheduling, cancelling — is
**deterministic, patient-driven code, not an LLM decision**: the patient
sees a real list of departments/slots/appointments queried straight from
Postgres and clicks the one they want. This was a deliberate design choice
made partway through the build (see
[`docs/memory/decisions.md`](docs/memory/decisions.md)): once real,
unambiguous data exists, letting a model guess which row to pick adds risk
with no benefit over a button.

| Agent | File | Responsibility | Tools |
|---|---|---|---|
| **Safety & Escalation** | [`app/agents/safety.py`](app/agents/safety.py) | Runs first; blocks diagnosis/prescription/dosage language and emergency requests; creates escalation records | [`create_escalation`](app/tools/escalation_tools.py) |
| **Coordinator** | [`app/agents/coordinator.py`](app/agents/coordinator.py) | Detects intent(s) (including genuinely multi-intent requests), opens/resumes the `WorkflowRun`, delegates in order | [`get_or_create_patient`](app/tools/patient_tools.py) |
| **Department Routing** | [`app/agents/routing.py`](app/agents/routing.py) | Classifies the request against the live department list, handles ambiguity, escalates unsupported requests | [`lookup_departments`](app/tools/department_tools.py) |
| **Document** | [`app/agents/document.py`](app/agents/document.py) | Ingest, classify, checksum, duplicate/missing-document detection, maps files to the patient | [`store_and_classify_document`](app/tools/document_tools.py) |

**Appointment tools** ([`app/tools/appointment_tools.py`](app/tools/appointment_tools.py)) —
`check_slot_availability`, `book_or_modify_appointment` — are real,
audited, and used on every booking/reschedule/cancel, but are invoked
directly from [`app/workflow_runner.py`](app/workflow_runner.py) once the
patient clicks a real slot or appointment, not from an LLM-driven agent
node. `app/agents/appointment.py` exists from an earlier design iteration
but is not wired into the current graph.

**Follow-up agent** — reminders and incomplete-workflow scanning — is
specced (see [`docs/superpowers/specs/`](docs/superpowers/specs)) but not
yet implemented. `Reminder` and the relevant schema exist in
[`app/models.py`](app/models.py); nothing writes to that table yet.

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

**Reminders:** schema exists (`Reminder` model, delivery designed to be
simulated — log + in-app, not real SMTP/SMS) but nothing creates rows in it
yet — see the Follow-up agent note above.

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
