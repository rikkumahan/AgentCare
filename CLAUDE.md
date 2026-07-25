# AgentCare — Project Instructions

This file is the authoritative brief for anyone (human or agent) working in this
repo. Read it before touching code. It reflects decisions already made during
design — don't re-litigate them without the user's sign-off.

## Also read: `docs/memory/`

This file holds static rules that don't change. It does **not** capture
decisions made *while building* (with the reasoning behind them), known
environment gotchas, or current phase status — that lives in
`docs/memory/` and gets read alongside this file, not instead of it:

- [`docs/memory/decisions.md`](docs/memory/decisions.md) — architectural
  calls made during implementation and why, so they aren't re-litigated or
  accidentally reversed
- [`docs/memory/gotchas.md`](docs/memory/gotchas.md) — environment/tooling
  quirks that already cost debugging time once — check before rediscovering
- [`docs/memory/status.md`](docs/memory/status.md) — which phase is
  actually done vs. just planned, at a glance

**Keep it updated.** When a session makes a non-obvious decision, hits a
gotcha worth remembering, or finishes/starts a phase, add a few lines to
the relevant file before wrapping up — this folder is only useful if it
stays current.

## What this is

AgentCare, a submission for the **AgentCare Build Challenge 2026** (see
`problem_statement.md` for the full, binding spec). An agentic healthcare
*administration* app: registration → intent detection → department routing →
appointment booking → document coordination → confirmation/reminders →
follow-up. Judged on genuine end-to-end wiring (route → service → agent →
tool → database → persisted result), not breadth of stubs.

## Hard rules — breaking any one scores zero

- **Python backend is primary.** No meaningful Python backend → disqualified.
- **Agentic, not CRUD.** Must be a real multi-step, tool-using LLM workflow.
  A chat box that only forwards prompts and takes no actions → disqualified.
- **Persistent SQL only.** No in-memory dicts/session vars for patient,
  appointment, document, or workflow data. Everything durable lives in Postgres.
- **Never diagnose, prescribe, change dosage, or claim to replace a
  clinician.** Administrative routing ("route to Cardiology") is fine.
  Anything that reads as a medical judgment is not — the Safety & Escalation
  Agent exists to block this in code, not just in a prompt.
- **No real PII, credentials, or secrets committed.** Secrets only in a local,
  gitignored `.env`; ship `.env.example` with placeholders only.
- **No tool may return a fixed response regardless of input.** Every tool
  must do real DB/filesystem logic. A tool that always says "success" scores
  zero on its own.
- **No hardcoded final responses.** The Coordinator's confirmation message is
  rendered from rows just read back from the database after a real write —
  never a free-standing LLM string asserting success.
- **RBAC is enforced in backend route/dependency code.** Hiding buttons in
  templates is not access control.

## Stack (decided — don't swap without asking)

- **Backend:** FastAPI + Jinja2 templates (server-rendered UI, no SPA)
- **LLM:** Groq via `langchain-groq` (`ChatGroq`) — client swappable later
  behind LangChain's model interface, but don't build a multi-provider
  abstraction speculatively — YAGNI
- **Orchestration:** LangGraph — each agent is a distinct graph node with its
  own system prompt and own bound tool(s). Tools are plain functions wrapped
  in LangChain's `@tool` decorator (auto-generates the JSON schema from the
  signature/docstring — don't hand-write tool schemas); tool execution uses
  LangGraph's prebuilt `ToolNode`, not custom dispatch code.
- **Database:** PostgreSQL, managed with Alembic migrations
  - Local/judging: `postgres:16` container via `docker-compose.yml`, zero
    external accounts needed to run the repo
  - Hosted/demo: Supabase Postgres — same schema, same migrations, just a
    different `DATABASE_URL`. Never hardcode which one; always read from env.
- **Deployment:** Dockerfile for the app + docker-compose for app+db
- **Documents:** local filesystem (`./storage/<patient_id>/...`), checksum +
  path + type recorded in `PatientDocument`
- **Reminders/notifications:** persisted `Reminder` rows + audit trail;
  delivery is simulated (log + in-app), not real SMTP/SMS
- **Auth:** real password auth, hashed (passlib/bcrypt), session cookie,
  role on the `User` row checked by a FastAPI dependency on every route

## Agents and their tools (final — 6 agents, 8 tool functions)

| Agent | Responsibility | Tools |
|---|---|---|
| **Coordinator** | Intent detection, opens/resumes `WorkflowRun`, delegates in order, combines outputs, renders final confirmation from persisted rows | `get_or_create_patient` |
| **Safety & Escalation** | Runs first and re-checks every downstream agent's proposed action for diagnosis/prescription/dosage/emergency language; blocks or escalates | `create_escalation` |
| **Department Routing** | Classifies request against the *live* department list, handles ambiguity, escalates unsupported requests | `lookup_departments` |
| **Appointment** | Availability, conflict checks, book/reschedule/cancel | `check_slot_availability`, `book_or_modify_appointment` |
| **Document** | Ingest, classify, checksum, duplicate/missing-doc detection, map to patient | `store_and_classify_document` |
| **Follow-up** | Appointment reminders, post-visit tasks, scans for missed/incomplete workflows | `create_reminder`, `scan_incomplete_workflows` |

Each agent needs its **own** system prompt and its **own** tool set —
renaming a helper function as an "agent," or having two agents share one
prompt, does not satisfy the spec's distinctness test (§5).

**Audit logging is not an agent-invoked tool.** Every tool function above is
wrapped by a decorator that writes an `AuditEvent` row automatically on every
call (success or failure), so the audit trail doesn't depend on an LLM
remembering to log it.

## Data model (matches problem_statement.md §9 — keep names aligned)

`User, PatientProfile, Department, Doctor, AppointmentSlot, Appointment,
PatientDocument, WorkflowRun, Reminder, Escalation, AuditEvent`

`WorkflowRun.state` (JSON column) is the serialized LangGraph state,
checkpointed after every node — this is what makes workflow state real and
resumable across restarts, not just an in-memory graph run.

## Conventions

- Env-based config via `pydantic-settings`, reading `.env`. Never hardcode
  `DATABASE_URL`, API keys, or which Postgres target is active.
- Alembic is the source of truth for schema — a judge running
  `docker compose up` + `alembic upgrade head` should get a working schema
  with no dump file and no Supabase account required.
- Seed script provides synthetic departments/doctors/slots/sample
  users — no real patient data, ever.
- Tests: pytest, focused on tool functions and route-level RBAC — mock the
  LLM call, don't test prompt wording.
- Error handling: Groq calls get retried with backoff on transient failures;
  an exception inside a graph node is caught and flips `WorkflowRun.status`
  to `failed`/`needs_attention` (visible to staff) instead of crashing the
  request.

## Build priority (per problem_statement.md §11 weighting)

1. Agent architecture/orchestration + safety/escalation + document
   coordination (highest weight)
2. Appointment workflow end-to-end, persistence/auditability, registration,
   reminders (substantial weight)
3. Code quality/tests, UI polish, docs (lower weight — do not skip, but
   don't over-invest ahead of #1 and #2)

## Do not

- Do not add a new LLM provider abstraction, a plugin system, or config for
  values that never change.
- Do not build insurance/billing/bed-allocation/pharmacy/staff-scheduling —
  explicitly optional extensions, out of scope unless asked.
- Do not let any agent produce diagnosis/prescription language, even as a
  "helpful suggestion." Safety & Escalation Agent's checks are mandatory
  gates, not advisory.
