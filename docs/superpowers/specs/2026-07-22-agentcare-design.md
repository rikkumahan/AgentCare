# AgentCare — Design Spec

Status: approved by user through conversational review (architecture, stack,
DB, agent/tool split all confirmed section-by-section). This doc consolidates
those decisions into one reference before implementation planning.

Source of truth for rules/scope: `problem_statement.md`. Project-wide
conventions: `CLAUDE.md`. This doc is the detailed architecture underneath both.

## 1. Goal

Build AgentCare: a Python, LLM-driven, multi-agent administrative healthcare
app covering registration → intent detection → department routing →
appointment booking → document coordination → confirmation/reminders →
follow-up, with persisted state, backend-enforced RBAC, human escalation, and
audit logging. Judged on genuine end-to-end wiring, not stub breadth.

## 2. Tech stack

| Concern | Choice |
|---|---|
| Backend framework | FastAPI |
| UI | Jinja2 server-rendered templates |
| LLM | Groq (`groq` SDK, one thin client wrapper) |
| Agent orchestration | LangGraph |
| Database | PostgreSQL (Supabase-hosted for demo, Dockerized `postgres:16` for local/judging) |
| Migrations | Alembic |
| ORM | SQLAlchemy 2.x |
| Deployment | Docker + docker-compose |
| Document storage | Local filesystem (`./storage/`) + DB metadata |
| Auth | Session-cookie + hashed passwords (passlib/bcrypt) |
| Config | `pydantic-settings` reading `.env` |
| Tests | pytest |

## 3. High-level architecture

```
Browser (Patient / Staff)
    │  Jinja2 pages, session cookie
    ▼
FastAPI app  (Docker "app" service)
    ├─ auth.py            — login/signup, session cookie, password hashing
    ├─ rbac.py             — require_role() FastAPI dependency
    ├─ routes/             — patient_routes, staff_routes, request_routes
    ├─ audit.py            — @audited decorator wrapping every tool call
    ├─ agents/
    │    ├─ coordinator.py
    │    ├─ safety.py
    │    ├─ routing.py
    │    ├─ appointment.py
    │    ├─ document.py
    │    └─ followup.py
    ├─ tools/
    │    ├─ patient_tools.py       (get_or_create_patient)
    │    ├─ escalation_tools.py    (create_escalation)
    │    ├─ department_tools.py    (lookup_departments)
    │    ├─ appointment_tools.py   (check_slot_availability, book_or_modify_appointment)
    │    ├─ document_tools.py      (store_and_classify_document)
    │    └─ followup_tools.py      (create_reminder, scan_incomplete_workflows)
    ├─ graph.py            — LangGraph StateGraph wiring all 6 agent nodes
    ├─ models.py           — SQLAlchemy models (11 tables)
    └─ db.py               — engine/session, reads DATABASE_URL
    ▼
PostgreSQL  (Docker "db" service locally; Supabase for hosted demo)
    ▼
./storage/<patient_id>/<filename>   (uploaded documents on disk)
```

Request lifecycle for a patient's administrative request:

1. Patient submits free-text request (+ optional file uploads) via a form.
2. Route handler creates a `WorkflowRun` row (`status=running`, `current_step=coordinator`).
3. `graph.invoke(...)` runs the LangGraph graph seeded with the request text,
   patient id, and any uploaded file paths.
4. Each node updates the shared graph state and, after it returns, the route
   handler persists the updated state into `WorkflowRun.state` (JSON) and
   `WorkflowRun.current_step` — this is the checkpoint. Graph state is never
   trusted to survive only in process memory.
5. On completion, the Coordinator's final output is built by re-reading the
   `Appointment`/`Reminder`/`PatientDocument` rows just written, and rendering
   a confirmation from those rows.
6. Patient sees the confirmation + status on their dashboard; every tool call
   along the way has already written an `AuditEvent`.

## 4. Data model

All timestamps UTC, `created_at`/`updated_at` via `server_default=func.now()`.

```
User
  id            UUID PK
  name          str
  email         str, unique
  password_hash str
  role          enum('patient', 'staff')
  created_at    datetime

PatientProfile
  id                  UUID PK
  user_id             FK -> User.id, unique
  date_of_birth       date
  phone               str
  preferred_language  str, default 'en'
  emergency_contact   str
  created_at          datetime
  updated_at          datetime

Department
  id           UUID PK
  name         str, unique          (e.g. "Cardiology")
  description  str
  active       bool, default true

Doctor
  id             UUID PK
  department_id  FK -> Department.id
  name           str
  active         bool, default true

AppointmentSlot
  id          UUID PK
  doctor_id   FK -> Doctor.id
  start_time  datetime
  end_time    datetime
  status      enum('open', 'booked', 'blocked')

Appointment
  id          UUID PK
  patient_id  FK -> PatientProfile.id
  doctor_id   FK -> Doctor.id
  slot_id     FK -> AppointmentSlot.id
  status      enum('pending', 'confirmed', 'rescheduled', 'cancelled')
  reason      str                  (routing agent's administrative reason, never a diagnosis)
  created_at  datetime
  updated_at  datetime

PatientDocument
  id                 UUID PK
  patient_id         FK -> PatientProfile.id
  document_type      enum('ecg', 'lab_report', 'prescription_old', 'insurance', 'id_proof', 'other')
  file_path          str            (relative path under ./storage)
  document_date      date, nullable
  checksum           str            (sha256, unique per patient — duplicate detection)
  created_at         datetime

WorkflowRun
  id            UUID PK
  patient_id    FK -> PatientProfile.id
  current_step  str                 (node name, e.g. "appointment_agent")
  state         JSON                (serialized LangGraph state — the checkpoint)
  status        enum('running', 'completed', 'failed', 'needs_review')
  created_at    datetime
  updated_at    datetime

Reminder
  id              UUID PK
  patient_id      FK -> PatientProfile.id
  appointment_id  FK -> Appointment.id, nullable
  reminder_type   enum('appointment', 'follow_up', 'missing_document')
  scheduled_at    datetime
  status          enum('pending', 'sent', 'dismissed')

Escalation
  id               UUID PK
  workflow_run_id  FK -> WorkflowRun.id
  reason           str
  status           enum('open', 'approved', 'rejected')
  reviewed_by      FK -> User.id, nullable
  created_at       datetime

AuditEvent
  id           UUID PK
  actor_id     FK -> User.id, nullable   (nullable = system/agent-initiated)
  action       str                       (tool function name)
  entity_type  str                       (e.g. "Appointment")
  entity_id    UUID, nullable
  metadata     JSON
  created_at   datetime
```

Names/fields match `problem_statement.md` §9 exactly (spec allows equivalent
naming — we're using the suggested names verbatim to avoid any ambiguity for
judges).

## 5. Agent architecture (LangGraph)

Shared graph state (`TypedDict`):

```python
class WorkflowState(TypedDict):
    workflow_run_id: str
    patient_id: str
    request_text: str
    uploaded_files: list[str]
    intent: str | None
    department_id: str | None
    appointment_id: str | None
    document_ids: list[str]
    reminder_ids: list[str]
    escalation: dict | None
    status: str            # running | completed | failed | needs_review
    trace: list[dict]      # per-node log for the confirmation summary
```

Graph shape (conditional edges, not a fixed linear chain):

```
START → safety_check (pre-screen)
      → [escalate?] → END (needs_review)
      → coordinator (intent detection + get_or_create_patient)
      → routing_agent
      → [unsupported/emergency?] → safety_check → END (needs_review)
      → appointment_agent
      → document_agent   (only if uploaded_files present)
      → followup_agent
      → safety_check (post-screen of combined output)
      → [flag?] → END (needs_review)
      → coordinator (final confirmation, rendered from persisted rows)
      → END (completed)
```

Safety & Escalation Agent is invoked as a node **both** before routing and
after followup — it is a gate, not a one-time formality. Any node may set
`escalation` in state; the graph's conditional edge checks it and routes to
END with `WorkflowRun.status = needs_review` plus a persisted `Escalation` row.

Each agent node:
- Has its own system prompt (stored as a module-level constant in its file,
  not shared).
- Calls the LLM with only its own bound tool(s) (LangGraph/Groq tool-calling).
- Returns a partial state update merged into `WorkflowState`.
- Wraps its LLM call in a retry-with-backoff helper (see §9).

### 5.1 Coordinator (`agents/coordinator.py`)
- First call: intent detection + `get_or_create_patient` tool → sets
  `intent`, `patient_id` confirmed.
- Final call (after all other nodes succeed): reads back `Appointment`,
  `Reminder`, `PatientDocument` rows for this workflow and renders the
  confirmation text from a template fed those rows — the LLM is not asked to
  invent the confirmation from scratch.

### 5.2 Safety & Escalation (`agents/safety.py`)
- Tool: `create_escalation`.
- Prompt instructs: flag anything resembling diagnosis, prescription, dosage
  change, or a self-described emergency; administrative routing language is
  fine. On flag, calls `create_escalation` and sets `state["escalation"]`.
- This is the only agent allowed to set `WorkflowRun.status = needs_review`.

### 5.3 Department Routing (`agents/routing.py`)
- Tool: `lookup_departments` (reads live `Department`/`Doctor` tables).
- Classifies `intent` into a `department_id`; if no confident match, returns
  `state["escalation"]` for the Safety Agent to act on next pass rather than
  guessing.

### 5.4 Appointment (`agents/appointment.py`)
- Tools: `check_slot_availability`, `book_or_modify_appointment`.
- Given `department_id` + patient preference (from `request_text`), finds
  candidate slots, checks for existing conflicting appointments for the
  patient, books/reschedules/cancels, sets `appointment_id`.

### 5.5 Document (`agents/document.py`)
- Tool: `store_and_classify_document`.
- Only runs when `uploaded_files` is non-empty. For each file: computes
  checksum, checks for an existing `PatientDocument` with the same checksum
  for this patient (duplicate), classifies `document_type` from filename +
  extracted text snippet, writes file to `./storage/<patient_id>/`, inserts
  row. Flags missing required documents (e.g. cardiology follow-up expects
  a prior ECG) back into state for the Coordinator's summary.

### 5.6 Follow-up (`agents/followup.py`)
- Tools: `create_reminder`, `scan_incomplete_workflows`.
- Creates an appointment reminder (`scheduled_at` = slot start minus a fixed
  offset) and a post-visit follow-up task. Also scans this patient's other
  `WorkflowRun` rows for `status='running'` and `updated_at` older than a
  threshold, creating a `missing_document`/`follow_up` reminder for those —
  real query against persisted state, not a stub.

## 6. Tools (signatures)

All tools live under `tools/`, take a SQLAlchemy `Session` plus typed args,
return a plain dict (JSON-serializable, fed back to the LLM as the tool
result), and are wrapped by `@audited("<action_name>")` which writes an
`AuditEvent` row before returning.

```python
def get_or_create_patient(db: Session, user_id: str, profile_fields: dict) -> dict: ...
def create_escalation(db: Session, workflow_run_id: str, reason: str) -> dict: ...
def lookup_departments(db: Session, query_hint: str) -> list[dict]: ...
def check_slot_availability(db: Session, department_id: str, preferred_window: dict) -> list[dict]: ...
def book_or_modify_appointment(db: Session, patient_id: str, slot_id: str, action: str, existing_appointment_id: str | None) -> dict: ...
def store_and_classify_document(db: Session, patient_id: str, file_path: str, hint: str | None) -> dict: ...
def create_reminder(db: Session, patient_id: str, appointment_id: str | None, reminder_type: str, scheduled_at: datetime) -> dict: ...
def scan_incomplete_workflows(db: Session, patient_id: str, stale_after_hours: int = 24) -> list[dict]: ...
```

## 7. API routes / UI pages

**Patient** (`role=patient` required):
- `GET/POST /register`, `GET/POST /login`, `POST /logout`
- `GET /dashboard` — profile, appointments, documents, reminders (all from DB)
- `GET/POST /requests/new` — submit administrative request (+ file upload) → kicks off graph run
- `GET /requests/{workflow_run_id}` — status/trace/confirmation
- `POST /appointments/{id}/reschedule`, `POST /appointments/{id}/cancel`
- `GET /documents` — list with status (classified/duplicate/missing flags)

**Staff** (`role=staff` required):
- `GET /staff/requests` — all workflow runs, filterable by status
- `GET /staff/escalations`, `POST /staff/escalations/{id}/approve|reject`
- `GET/POST /staff/departments`, `GET/POST /staff/doctors`, `GET/POST /staff/slots`
- `GET /staff/audit` — audit event log, filterable by entity/actor

RBAC: a `require_role("staff")` / `require_role("patient")` FastAPI
dependency on every route above — never a template-only check.

## 8. Safety boundary (RULE-5)

Enforced in three places, not just a prompt:
1. Safety & Escalation Agent's system prompt explicitly forbids diagnosis/
   prescription/dosage output and instructs escalation instead.
2. A regex/keyword pre-filter on agent outputs (defense in depth) for terms
   like dosage units or explicit diagnosis phrasing, before anything is
   persisted as a confirmation.
3. Department Routing Agent's prompt is scoped to *administrative* mapping
   only ("map to a department"), never asked to reason about condition
   severity beyond emergency/non-emergency triage for escalation purposes.

## 9. Error handling & retry

- Groq LLM calls: wrapped in a retry helper (3 attempts, exponential
  backoff) for transient network/5xx errors; on final failure the node
  raises a typed `AgentError`.
- Graph-level: the route handler wraps `graph.invoke()` in try/except; on
  `AgentError` or any unhandled exception, sets `WorkflowRun.status='failed'`
  with the error persisted into `state["trace"]`, and returns a friendly
  in-app message — never a raw 500 with no persisted record of what happened.
- Tool-level: DB operations wrapped in a transaction per tool call; on
  failure, rollback and return a structured error dict to the agent (so the
  LLM can decide to retry, pick another slot, etc.) rather than crashing the
  graph.

## 10. Config & deployment

- `.env` keys: `DATABASE_URL`, `GROQ_API_KEY`, `SECRET_KEY`,
  `STORAGE_DIR`, `ENV` (dev/prod). `.env.example` ships with placeholders.
- `docker-compose.yml`: `db` (postgres:16, named volume, healthcheck) + `app`
  (built from `Dockerfile`, runs `alembic upgrade head` then `uvicorn` on
  startup, depends_on `db` healthy).
- Supabase: same `DATABASE_URL` shape (`postgresql+psycopg://...`), just
  point at the Supabase host/port/db for hosted demo; no code change.

## 11. Testing

- `tests/test_tools/` — one test module per tool file, real logic against a
  throwaway test Postgres (or SQLite in-memory for speed, since tool logic
  is DB-portable SQLAlchemy) — assert real state changes (row inserted,
  slot flipped to booked, duplicate detected by checksum).
- `tests/test_rbac.py` — asserts a patient session gets 403 on `/staff/*`
  routes and vice versa.
- `tests/test_graph.py` — runs the LangGraph graph with the LLM mocked
  (fixed tool-call sequence), asserts state transitions and that a flagged
  "diagnose my chest pain" request ends in `needs_review`, not a confirmed
  appointment.

## 12. Repo structure

```
AGENT_CARE/
├── app/
│   ├── main.py
│   ├── db.py
│   ├── models.py
│   ├── config.py
│   ├── auth.py
│   ├── rbac.py
│   ├── audit.py
│   ├── graph.py
│   ├── agents/
│   ├── tools/
│   ├── routes/
│   └── templates/
├── alembic/
├── seed/
│   └── seed_data.py
├── storage/                # gitignored, created at runtime
├── tests/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
├── .gitignore
├── CLAUDE.md
├── README.md
└── problem_statement.md
```

## 13. Build phases (6-day hackathon pacing, matches §11 weighting)

1. **Foundation** — repo scaffold, Docker+Postgres+Alembic, models, seed
   script, auth+RBAC skeleton.
2. **Core agent loop** — LangGraph graph wiring, Coordinator + Safety agents,
   one end-to-end path (request → escalation OR routing) provable via tests.
3. **Routing + Appointment agents** — full booking/reschedule/cancel, real
   conflict checks.
4. **Document agent** — upload, classify, checksum/duplicate, missing-doc
   detection.
5. **Follow-up agent + audit + error handling** — reminders, incomplete-scan,
   audit decorator on all tools, retry/failure paths.
6. **UI polish, seed data realism, README, tests pass, demo pass.**

## 14. Open items resolved during self-review

- Confirmed `WorkflowRun.state` is the single checkpoint mechanism — no
  separate "graph checkpointer" table, to avoid duplicated persistence logic.
- Confirmed audit logging is decorator-based (§CLAUDE.md), not a
  separate LLM-invoked tool, to make it unconditional.
- Confirmed Safety & Escalation Agent runs at two points in the graph (pre
  and post), not once, so it's a real recurring gate.
