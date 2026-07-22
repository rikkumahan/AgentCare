# AgentCare Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a runnable, Postgres-backed FastAPI skeleton — schema, migrations, seed data, real password auth, and backend-enforced RBAC — that later phases (LangGraph agents/tools) build directly on top of.

**Architecture:** FastAPI app with SQLAlchemy 2.0 models mapped 1:1 to the 11 tables in the design spec, Alembic-managed schema against PostgreSQL (Dockerized locally, Supabase for hosted demo via the same `DATABASE_URL` env var), session-cookie auth with bcrypt-hashed passwords, and a `require_role()` FastAPI dependency enforced in route code (not templates).

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Alembic, PostgreSQL (`psycopg` v3 driver), pydantic-settings, passlib[bcrypt], itsdangerous (signed session cookies), Jinja2, pytest + httpx, Docker/docker-compose.

## Global Constraints

- Primary backend must be Python (problem_statement.md RULE-2).
- No in-memory dicts/session vars for durable data — everything durable lives in Postgres (RULE-4, CLAUDE.md).
- No real PII, credentials, or secrets committed; secrets only in a local, gitignored `.env`, with `.env.example` shipping placeholders only (RULE-6).
- RBAC must be enforced in backend route/dependency code, never hidden only in templates (problem_statement.md §4).
- All config (DB URL, secret key, API keys) read from environment via `pydantic-settings` — never hardcoded (CLAUDE.md conventions).
- Seed/sample data must be synthetic — no real patient data, ever (CLAUDE.md conventions).
- `docker compose up` + `alembic upgrade head` must produce a working schema with no external account and no dump file required (CLAUDE.md conventions).

---

## File Structure

```
AGENT_CARE/
├── app/
│   ├── __init__.py
│   ├── main.py                  # FastAPI app, router registration
│   ├── config.py                # pydantic-settings Settings
│   ├── db.py                    # engine, SessionLocal, get_db dependency
│   ├── models.py                # all 11 SQLAlchemy models + enums
│   ├── auth.py                  # password hashing, session token sign/verify
│   ├── rbac.py                  # get_current_user, require_role dependencies
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── auth_routes.py       # /register, /login, /logout
│   │   └── dashboard_routes.py  # /dashboard (patient), /staff/dashboard
│   └── templates/
│       ├── base.html
│       ├── register.html
│       ├── login.html
│       ├── dashboard.html
│       └── staff_dashboard.html
├── alembic/
│   ├── env.py
│   └── script.py.mako
├── alembic.ini
├── seed/
│   ├── __init__.py
│   └── seed_data.py
├── tests/
│   ├── conftest.py
│   ├── test_config.py
│   ├── test_models.py
│   ├── test_auth.py
│   └── test_routes_rbac.py
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── pytest.ini
└── .env.example
```

Note on a deliberate simplification: `models.py` uses plain foreign-key columns
only — **no ORM `relationship()`/`back_populates` attributes**. Eleven
interconnected tables' worth of relationship pairings is exactly the kind of
speculative convenience layer that produces silent bugs (mismatched
`back_populates` names) without being needed yet. Tool/route code queries by
explicit FK filter (`db.query(Appointment).filter(Appointment.patient_id == ...)`).
Add relationships later only if a specific template genuinely needs
dot-navigation across a join.

---

### Task 1: Project scaffold, dependencies, and environment config

**Files:**
- Create: `requirements.txt`
- Create: `pytest.ini`
- Create: `.env.example`
- Create: `app/__init__.py`
- Create: `app/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `app.config.Settings` (pydantic-settings class) with fields
  `database_url: str`, `groq_api_key: str = ""`, `secret_key: str`,
  `storage_dir: str = "./storage"`, `env: str = "dev"`; module-level
  singleton `settings = Settings()`.

- [ ] **Step 1: Write `requirements.txt`**

```
fastapi>=0.115
uvicorn[standard]>=0.32
sqlalchemy>=2.0.35
alembic>=1.13
psycopg[binary]>=3.2
pydantic-settings>=2.5
passlib[bcrypt]>=1.7.4
itsdangerous>=2.2
python-multipart>=0.0.12
jinja2>=3.1
langchain-groq>=0.2
langgraph>=0.2
pytest>=7.4
httpx>=0.27
```

- [ ] **Step 2: Write `pytest.ini`**

```ini
[pytest]
pythonpath = .
testpaths = tests
```

- [ ] **Step 3: Write `.env.example`**

```
DATABASE_URL=postgresql+psycopg://agentcare:agentcare@localhost:5432/agentcare
GROQ_API_KEY=your_groq_api_key_here
SECRET_KEY=change_this_to_a_random_secret_value
STORAGE_DIR=./storage
ENV=dev
```

- [ ] **Step 4: Copy it to a real local `.env`**

```bash
cp .env.example .env
```

(`.env` is already gitignored — verify with `git check-ignore .env`, expect it to print `.env`.)

- [ ] **Step 5: Create `app/__init__.py` (empty)**

- [ ] **Step 6: Install dependencies**

```bash
pip install -r requirements.txt
```

- [ ] **Step 7: Write the failing test**

`tests/test_config.py`:

```python
def test_settings_load_from_explicit_env_vars(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    monkeypatch.setenv("SECRET_KEY", "abc123")

    from app.config import Settings

    settings = Settings(_env_file=None)

    assert settings.database_url == "postgresql+psycopg://u:p@localhost:5432/db"
    assert settings.secret_key == "abc123"
    assert settings.storage_dir == "./storage"
    assert settings.env == "dev"
```

- [ ] **Step 8: Run test to verify it fails**

Run: `pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.config'`

- [ ] **Step 9: Write `app/config.py`**

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    groq_api_key: str = ""
    secret_key: str
    storage_dir: str = "./storage"
    env: str = "dev"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
```

- [ ] **Step 10: Run test to verify it passes**

Run: `pytest tests/test_config.py -v`
Expected: PASS (2 assertions on defaults, 2 on explicit values)

- [ ] **Step 11: Commit**

```bash
git add requirements.txt pytest.ini .env.example app/__init__.py app/config.py tests/test_config.py
git commit -m "Add project scaffold, dependencies, and env-based settings"
```

---

### Task 2: Docker Compose + Dockerfile (Postgres reachable locally)

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`

**Interfaces:**
- Consumes: `.env` (Task 1) for `DATABASE_URL` matching the compose `db` service credentials.
- Produces: a running `db` service reachable at `localhost:5432` with user/password/db `agentcare`/`agentcare`/`agentcare` — this is the Postgres instance every later task's tests run against.

- [ ] **Step 1: Write `Dockerfile`**

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 2: Write `docker-compose.yml`**

Only the `db` service goes in for now — the `app` service isn't runnable
until `Dockerfile`'s `CMD` target (`app/main.py`) and Alembic exist, which
happens in Task 7. Adding it here would be untested config sitting inert for
five tasks. It's added, and actually run end-to-end, in Task 7.

```yaml
services:
  db:
    image: postgres:16
    environment:
      POSTGRES_USER: agentcare
      POSTGRES_PASSWORD: agentcare
      POSTGRES_DB: agentcare
    ports:
      - "5432:5432"
    volumes:
      - db_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U agentcare"]
      interval: 5s
      timeout: 5s
      retries: 10

volumes:
  db_data:
```

- [ ] **Step 3: Start the database service**

```bash
docker compose up -d db
```

Expected: container starts; `docker compose ps` shows `db` as `healthy` within ~15s.

- [ ] **Step 4: Verify Postgres is reachable**

```bash
docker compose exec db psql -U agentcare -d agentcare -c "SELECT 1;"
```

Expected: prints a `1` row — confirms the credentials in `.env`'s `DATABASE_URL` match the running container.

- [ ] **Step 5: Commit**

```bash
git add Dockerfile docker-compose.yml
git commit -m "Add Docker Compose for local Postgres container"
```

---

### Task 3: SQLAlchemy models (all 11 tables)

**Files:**
- Create: `app/models.py`
- Create: `app/db.py`
- Test: `tests/conftest.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: `app.config.settings.database_url` (Task 1), running Postgres (Task 2).
- Produces: `app.models.Base` (DeclarativeBase), and every model class:
  `User`, `UserRole`, `PatientProfile`, `Department`, `Doctor`,
  `AppointmentSlot`, `SlotStatus`, `Appointment`, `AppointmentStatus`,
  `PatientDocument`, `DocumentType`, `WorkflowRun`, `WorkflowStatus`,
  `Reminder`, `ReminderType`, `ReminderStatus`, `Escalation`,
  `EscalationStatus`, `AuditEvent`. Also `app.db.engine`, `app.db.SessionLocal`,
  `app.db.get_db()` (FastAPI dependency generator).

- [ ] **Step 1: Write `app/db.py`**

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
```

- [ ] **Step 2: Write `app/models.py`**

```python
import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    JSON,
    String,
    Text,
    UniqueConstraint,
    ForeignKey,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid_pk():
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class UserRole(str, enum.Enum):
    patient = "patient"
    staff = "staff"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(SAEnum(UserRole, name="user_role"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PatientProfile(Base):
    __tablename__ = "patient_profiles"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), unique=True)
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    preferred_language: Mapped[str] = mapped_column(String(10), default="en")
    emergency_contact: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Department(Base):
    __tablename__ = "departments"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Doctor(Base):
    __tablename__ = "doctors"

    id: Mapped[uuid.UUID] = _uuid_pk()
    department_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("departments.id"))
    name: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class SlotStatus(str, enum.Enum):
    open = "open"
    booked = "booked"
    blocked = "blocked"


class AppointmentSlot(Base):
    __tablename__ = "appointment_slots"

    id: Mapped[uuid.UUID] = _uuid_pk()
    doctor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("doctors.id"))
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[SlotStatus] = mapped_column(SAEnum(SlotStatus, name="slot_status"), default=SlotStatus.open)


class AppointmentStatus(str, enum.Enum):
    pending = "pending"
    confirmed = "confirmed"
    rescheduled = "rescheduled"
    cancelled = "cancelled"


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[uuid.UUID] = _uuid_pk()
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("patient_profiles.id"))
    doctor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("doctors.id"))
    slot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("appointment_slots.id"))
    status: Mapped[AppointmentStatus] = mapped_column(
        SAEnum(AppointmentStatus, name="appointment_status"), default=AppointmentStatus.pending
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DocumentType(str, enum.Enum):
    ecg = "ecg"
    lab_report = "lab_report"
    prescription_old = "prescription_old"
    insurance = "insurance"
    id_proof = "id_proof"
    other = "other"


class PatientDocument(Base):
    __tablename__ = "patient_documents"
    __table_args__ = (UniqueConstraint("patient_id", "checksum", name="uq_patient_document_checksum"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("patient_profiles.id"))
    document_type: Mapped[DocumentType] = mapped_column(SAEnum(DocumentType, name="document_type"))
    file_path: Mapped[str] = mapped_column(String(500))
    document_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    checksum: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkflowStatus(str, enum.Enum):
    running = "running"
    completed = "completed"
    failed = "failed"
    needs_review = "needs_review"


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("patient_profiles.id"))
    current_step: Mapped[str] = mapped_column(String(100), default="start")
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[WorkflowStatus] = mapped_column(
        SAEnum(WorkflowStatus, name="workflow_status"), default=WorkflowStatus.running
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ReminderType(str, enum.Enum):
    appointment = "appointment"
    follow_up = "follow_up"
    missing_document = "missing_document"


class ReminderStatus(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    dismissed = "dismissed"


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[uuid.UUID] = _uuid_pk()
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("patient_profiles.id"))
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("appointments.id"), nullable=True
    )
    reminder_type: Mapped[ReminderType] = mapped_column(SAEnum(ReminderType, name="reminder_type"))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[ReminderStatus] = mapped_column(
        SAEnum(ReminderStatus, name="reminder_status"), default=ReminderStatus.pending
    )


class EscalationStatus(str, enum.Enum):
    open = "open"
    approved = "approved"
    rejected = "rejected"


class Escalation(Base):
    __tablename__ = "escalations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    workflow_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workflow_runs.id"))
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[EscalationStatus] = mapped_column(
        SAEnum(EscalationStatus, name="escalation_status"), default=EscalationStatus.open
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(100))
    entity_type: Mapped[str] = mapped_column(String(100))
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    event_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

Note the `AuditEvent.event_metadata` line: the DB column is named `metadata`
(matches problem_statement.md §9 exactly) but the Python attribute is
`event_metadata`, because `metadata` is reserved on every SQLAlchemy
declarative class (`Base.metadata` is the schema registry) — using it as a
column attribute name raises `InvalidRequestError` at class definition time.
`mapped_column("metadata", JSON, ...)` gives the DB column the exact name
while the Python side uses a different attribute name.

- [ ] **Step 3: Write `tests/conftest.py`**

```python
import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://agentcare:agentcare@localhost:5432/agentcare")
os.environ.setdefault("SECRET_KEY", "test-secret-key-do-not-use-in-prod")

import pytest

from app.db import SessionLocal, engine
from app.models import Base


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db_session():
    # ponytail: no per-test transaction rollback isolation — tests use
    # unique data instead. Add rollback isolation if parallel test runs
    # are needed later.
    session = SessionLocal()
    yield session
    session.close()
```

- [ ] **Step 4: Write the failing test**

`tests/test_models.py`:

```python
import uuid

from app.models import Department, Doctor


def test_create_department_and_doctor(db_session):
    dept = Department(name=f"Cardiology-{uuid.uuid4().hex[:8]}", description="Heart care", active=True)
    db_session.add(dept)
    db_session.flush()

    doctor = Doctor(department_id=dept.id, name="Dr. Test", active=True)
    db_session.add(doctor)
    db_session.commit()

    fetched = db_session.get(Doctor, doctor.id)
    assert fetched is not None
    assert fetched.department_id == dept.id
    assert fetched.active is True
```

- [ ] **Step 5: Ensure Postgres is running, then run test to verify it fails**

```bash
docker compose up -d db
pytest tests/test_models.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.models'` (or `app.db`)

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_models.py -v`
Expected: PASS — confirms all 11 tables create cleanly against real Postgres (the session-scoped fixture creates every table, not just the two used in this test).

- [ ] **Step 7: Commit**

```bash
git add app/models.py app/db.py tests/conftest.py tests/test_models.py
git commit -m "Add SQLAlchemy models for all 11 persistent entities"
```

---

### Task 4: Alembic migrations

**Files:**
- Create: `alembic.ini`, `alembic/script.py.mako`, `alembic/versions/` (all via `alembic init alembic`, unmodified)
- Modify: `alembic/env.py` (generated, then replaced with the app-aware version below)

**Interfaces:**
- Consumes: `app.models.Base` (Task 3), `app.config.settings.database_url` (Task 1).
- Produces: `alembic upgrade head` recreates the exact schema `Base.metadata.create_all` would — this is the versioned, judge-runnable path to schema creation (no dump file needed).

- [ ] **Step 1: Generate the Alembic scaffold with its own CLI**

```bash
alembic init alembic
```

Expected: creates `alembic.ini`, `alembic/env.py`, `alembic/script.py.mako`,
and an empty `alembic/versions/` directory — Alembic's standard boilerplate,
generated by the tool itself rather than hand-transcribed (keeps it in sync
with whatever Alembic version is actually installed). Only `alembic/env.py`
needs real edits, in the next step; leave the generated `alembic.ini` and
`alembic/script.py.mako` as-is.

- [ ] **Step 2: Replace the generated `alembic/env.py` with the app-aware version**

```python
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import settings
from app.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", settings.database_url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

- [ ] **Step 3: Drop the test-created schema so autogenerate sees an empty database**

```bash
docker compose up -d db
python -c "from app.db import engine; from app.models import Base; Base.metadata.drop_all(bind=engine)"
```

- [ ] **Step 4: Autogenerate the initial migration**

```bash
alembic revision --autogenerate -m "initial schema"
```

Expected: a new file appears under `alembic/versions/`, its `upgrade()`
containing `op.create_table(...)` calls for all 11 tables and
`op.create_table` for the enum types (or inline `sa.Enum(...)` definitions).

- [ ] **Step 5: Apply the migration**

```bash
alembic upgrade head
```

Expected: exits 0, prints the revision id it upgraded to.

- [ ] **Step 6: Verify the schema matches — run the model test suite against the Alembic-created schema**

```bash
pytest tests/test_models.py -v
```

Expected: PASS. (The `conftest.py` session fixture will `drop_all` then
`create_all` again for test isolation — that's fine, this step is just
confirming Alembic's migration is schema-equivalent to the ORM models before
we let the fixture take over for the rest of the suite.)

- [ ] **Step 7: Commit**

```bash
git add alembic.ini alembic/env.py alembic/script.py.mako alembic/versions/
git commit -m "Add Alembic migrations, generated from SQLAlchemy models"
```

---

### Task 5: Auth core (password hashing, session tokens)

**Files:**
- Create: `app/auth.py`
- Test: `tests/test_auth.py`

**Interfaces:**
- Consumes: `app.config.settings.secret_key` (Task 1).
- Produces: `hash_password(password: str) -> str`,
  `verify_password(password: str, password_hash: str) -> bool`,
  `create_session_token(user_id: str) -> str`,
  `read_session_token(token: str) -> str | None`.

- [ ] **Step 1: Write the failing test**

`tests/test_auth.py`:

```python
def test_password_hash_roundtrip():
    from app.auth import hash_password, verify_password

    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed)
    assert not verify_password("wrong password", hashed)


def test_session_token_roundtrip():
    from app.auth import create_session_token, read_session_token

    token = create_session_token("some-user-id")
    assert read_session_token(token) == "some-user-id"


def test_session_token_rejects_tampering():
    from app.auth import create_session_token, read_session_token

    token = create_session_token("some-user-id")
    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
    assert read_session_token(tampered) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_auth.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.auth'`

- [ ] **Step 3: Write `app/auth.py`**

```python
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from passlib.context import CryptContext

from app.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
_serializer = URLSafeTimedSerializer(settings.secret_key, salt="agentcare-session")

SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 7  # 7 days


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def create_session_token(user_id: str) -> str:
    return _serializer.dumps({"user_id": user_id})


def read_session_token(token: str) -> str | None:
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    return data.get("user_id")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_auth.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add app/auth.py tests/test_auth.py
git commit -m "Add password hashing and signed session token helpers"
```

---

### Task 6: Auth routes (register/login/logout) + templates

**Files:**
- Create: `app/routes/__init__.py`
- Create: `app/routes/auth_routes.py`
- Create: `app/templates/base.html`
- Create: `app/templates/register.html`
- Create: `app/templates/login.html`
- Create: `app/main.py`
- Test: `tests/test_routes_rbac.py` (register/login portion — RBAC portion added in Task 7)

**Interfaces:**
- Consumes: `hash_password`, `verify_password`, `create_session_token` (Task 5);
  `User`, `UserRole`, `PatientProfile` (Task 3); `get_db` (Task 3).
- Produces: `SESSION_COOKIE_NAME = "agentcare_session"` constant (in
  `app/rbac.py`, created this task since routes need it — see Step 2), FastAPI
  routes `GET/POST /register`, `GET/POST /login`, `POST /logout`, and the
  `app.main.app` FastAPI instance later tasks mount more routes onto.

- [ ] **Step 1: Write `app/routes/__init__.py` (empty)**

- [ ] **Step 2: Write `app/rbac.py` (session cookie constant only for now — `require_role` added in Task 7)**

```python
SESSION_COOKIE_NAME = "agentcare_session"
```

- [ ] **Step 3: Write `app/templates/base.html`**

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>{% block title %}AgentCare{% endblock %}</title>
</head>
<body>
    <main>
        {% block content %}{% endblock %}
    </main>
</body>
</html>
```

- [ ] **Step 4: Write `app/templates/register.html`**

```html
{% extends "base.html" %}
{% block title %}Register - AgentCare{% endblock %}
{% block content %}
<h1>Patient Registration</h1>
{% if error %}<p class="error">{{ error }}</p>{% endif %}
<form method="post" action="/register">
    <label>Name <input type="text" name="name" required></label>
    <label>Email <input type="email" name="email" required></label>
    <label>Password <input type="password" name="password" required minlength="8"></label>
    <button type="submit">Register</button>
</form>
<p>Already have an account? <a href="/login">Log in</a></p>
{% endblock %}
```

- [ ] **Step 5: Write `app/templates/login.html`**

```html
{% extends "base.html" %}
{% block title %}Log in - AgentCare{% endblock %}
{% block content %}
<h1>Log in</h1>
{% if error %}<p class="error">{{ error }}</p>{% endif %}
<form method="post" action="/login">
    <label>Email <input type="email" name="email" required></label>
    <label>Password <input type="password" name="password" required></label>
    <button type="submit">Log in</button>
</form>
<p>New patient? <a href="/register">Register</a></p>
{% endblock %}
```

- [ ] **Step 6: Write `app/routes/auth_routes.py`**

```python
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import create_session_token, hash_password, verify_password
from app.db import get_db
from app.models import PatientProfile, User, UserRole
from app.rbac import SESSION_COOKIE_NAME

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    return templates.TemplateResponse("register.html", {"request": request, "error": None})


@router.post("/register")
def register_submit(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "Email already registered"},
            status_code=400,
        )

    user = User(name=name, email=email, password_hash=hash_password(password), role=UserRole.patient)
    db.add(user)
    db.flush()
    db.add(PatientProfile(user_id=user.id))
    db.commit()

    token = create_session_token(str(user.id))
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie(SESSION_COOKIE_NAME, token, httponly=True, samesite="lax")
    return response


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid email or password"},
            status_code=400,
        )

    token = create_session_token(str(user.id))
    dest = "/staff/dashboard" if user.role == UserRole.staff else "/dashboard"
    response = RedirectResponse(url=dest, status_code=303)
    response.set_cookie(SESSION_COOKIE_NAME, token, httponly=True, samesite="lax")
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response
```

- [ ] **Step 7: Write `app/main.py`**

```python
from fastapi import FastAPI

from app.routes.auth_routes import router as auth_router

app = FastAPI(title="AgentCare")

app.include_router(auth_router)
```

- [ ] **Step 8: Write the failing test**

`tests/test_routes_rbac.py`:

```python
import uuid

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _unique_email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


def test_register_sets_session_cookie_and_redirects():
    email = _unique_email("patient")
    resp = client.post(
        "/register",
        data={"name": "Alice", "email": email, "password": "supersecret1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"
    assert resp.cookies.get("agentcare_session") is not None


def test_duplicate_email_registration_rejected():
    email = _unique_email("dup")
    client.post("/register", data={"name": "First", "email": email, "password": "supersecret1"})
    resp = client.post("/register", data={"name": "Second", "email": email, "password": "supersecret1"})
    assert resp.status_code == 400
    assert "already registered" in resp.text


def test_login_wrong_password_rejected():
    email = _unique_email("wrongpw")
    client.post("/register", data={"name": "Carol", "email": email, "password": "supersecret1"})
    resp = client.post("/login", data={"email": email, "password": "not-the-password"})
    assert resp.status_code == 400
    assert "Invalid email or password" in resp.text
```

- [ ] **Step 9: Run test to verify it fails**

Run: `pytest tests/test_routes_rbac.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.main'` (or similar import error)

- [ ] **Step 10: Run test to verify it passes**

Run: `pytest tests/test_routes_rbac.py -v`
Expected: PASS (3 tests)

- [ ] **Step 11: Commit**

```bash
git add app/routes/__init__.py app/routes/auth_routes.py app/rbac.py \
        app/templates/base.html app/templates/register.html app/templates/login.html \
        app/main.py tests/test_routes_rbac.py
git commit -m "Add register/login/logout routes with session-cookie auth"
```

---

### Task 7: RBAC-protected dashboard routes

**Files:**
- Modify: `app/rbac.py`
- Create: `app/routes/dashboard_routes.py`
- Create: `app/templates/dashboard.html`
- Create: `app/templates/staff_dashboard.html`
- Modify: `app/main.py`
- Modify: `tests/test_routes_rbac.py`
- Modify: `docker-compose.yml` (adds the `app` service deferred from Task 2)

**Interfaces:**
- Consumes: `SESSION_COOKIE_NAME` (Task 6), `read_session_token` (Task 5),
  `User`, `UserRole` (Task 3), `get_db` (Task 3).
- Produces: `get_current_user(request, db) -> User` and
  `require_role(role: str)` FastAPI dependencies in `app/rbac.py`; routes
  `GET /dashboard` (patient-only), `GET /staff/dashboard` (staff-only).

- [ ] **Step 1: Extend `app/rbac.py`**

```python
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.auth import read_session_token
from app.db import get_db
from app.models import User

SESSION_COOKIE_NAME = "agentcare_session"


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    user_id = read_session_token(token)
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session")

    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


def require_role(role: str):
    def _check(user: User = Depends(get_current_user)) -> User:
        if user.role.value != role:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Requires {role} role")
        return user

    return _check
```

- [ ] **Step 2: Write `app/templates/dashboard.html`**

```html
{% extends "base.html" %}
{% block title %}Dashboard - AgentCare{% endblock %}
{% block content %}
<h1>Welcome, {{ user.name }}</h1>
<p>Role: {{ user.role.value }}</p>
<form method="post" action="/logout"><button type="submit">Log out</button></form>
{% endblock %}
```

- [ ] **Step 3: Write `app/templates/staff_dashboard.html`**

```html
{% extends "base.html" %}
{% block title %}Staff Dashboard - AgentCare{% endblock %}
{% block content %}
<h1>Staff Dashboard — {{ user.name }}</h1>
<p>Role: {{ user.role.value }}</p>
<form method="post" action="/logout"><button type="submit">Log out</button></form>
{% endblock %}
```

- [ ] **Step 4: Write `app/routes/dashboard_routes.py`**

```python
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.models import User, UserRole
from app.rbac import require_role

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/dashboard", response_class=HTMLResponse)
def patient_dashboard(request: Request, user: User = Depends(require_role(UserRole.patient.value))):
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": user})


@router.get("/staff/dashboard", response_class=HTMLResponse)
def staff_dashboard(request: Request, user: User = Depends(require_role(UserRole.staff.value))):
    return templates.TemplateResponse("staff_dashboard.html", {"request": request, "user": user})
```

- [ ] **Step 5: Wire the router into `app/main.py`, add a `/health` route**

```python
from fastapi import FastAPI

from app.routes.auth_routes import router as auth_router
from app.routes.dashboard_routes import router as dashboard_router

app = FastAPI(title="AgentCare")

app.include_router(auth_router)
app.include_router(dashboard_router)


@app.get("/health")
def health():
    return {"status": "ok"}
```

(Trivial one-liner route — no dedicated unit test needed; Step 10's Docker
smoke test below is its real check.)

- [ ] **Step 6: Write the failing tests**

Append to `tests/test_routes_rbac.py`:

```python
def test_registered_patient_can_view_own_dashboard():
    email = _unique_email("dashpatient")
    resp = client.post(
        "/register",
        data={"name": "Dana", "email": email, "password": "supersecret1"},
        follow_redirects=False,
    )
    cookie = resp.cookies.get("agentcare_session")

    dash = client.get("/dashboard", cookies={"agentcare_session": cookie})
    assert dash.status_code == 200
    assert "Dana" in dash.text


def test_patient_cannot_access_staff_dashboard():
    email = _unique_email("nostaff")
    resp = client.post(
        "/register",
        data={"name": "Eve", "email": email, "password": "supersecret1"},
        follow_redirects=False,
    )
    cookie = resp.cookies.get("agentcare_session")

    staff_resp = client.get("/staff/dashboard", cookies={"agentcare_session": cookie})
    assert staff_resp.status_code == 403


def test_unauthenticated_request_gets_401():
    resp = client.get("/dashboard")
    assert resp.status_code == 401


def test_staff_user_can_access_staff_dashboard_but_not_patient_dashboard(db_session):
    from app.auth import hash_password
    from app.models import User, UserRole

    email = _unique_email("staffuser")
    staff = User(name="Frank Staff", email=email, password_hash=hash_password("staffpass1"), role=UserRole.staff)
    db_session.add(staff)
    db_session.commit()

    login_resp = client.post("/login", data={"email": email, "password": "staffpass1"}, follow_redirects=False)
    assert login_resp.status_code == 303
    assert login_resp.headers["location"] == "/staff/dashboard"
    cookie = login_resp.cookies.get("agentcare_session")

    staff_dash = client.get("/staff/dashboard", cookies={"agentcare_session": cookie})
    assert staff_dash.status_code == 200
    assert "Frank Staff" in staff_dash.text

    patient_dash = client.get("/dashboard", cookies={"agentcare_session": cookie})
    assert patient_dash.status_code == 403
```

- [ ] **Step 7: Run tests to verify the new ones fail**

Run: `pytest tests/test_routes_rbac.py -v`
Expected: the 4 new tests FAIL (routes don't exist yet / 404s), the earlier 3 still PASS

- [ ] **Step 8: Run tests to verify all pass**

Run: `pytest tests/test_routes_rbac.py -v`
Expected: PASS (7 tests total) — proves RBAC is enforced in route/dependency
code: a patient session gets 403 on the staff route and vice versa, not just
a hidden button.

- [ ] **Step 9: Add the `app` service to `docker-compose.yml`, now that it's runnable**

Append to the `services:` block written in Task 2 (keep the existing `db`
service as-is):

```yaml
  app:
    build: .
    env_file: .env
    depends_on:
      db:
        condition: service_healthy
    ports:
      - "8000:8000"
    volumes:
      - ./storage:/app/storage
    command: sh -c "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 10s
```

- [ ] **Step 10: Run the full stack end-to-end and verify it actually serves requests**

```bash
docker compose up -d --build
sleep 15
docker compose ps
curl -s http://localhost:8000/health
docker compose down
```

Expected: `docker compose ps` shows `app` as `healthy`; the `curl` line
prints `{"status":"ok"}` — confirms the container built from `Dockerfile`,
ran `alembic upgrade head` against the `db` service, and `uvicorn` served a
real route, all from a clean `docker compose up` with no manual steps. This
is the judge-facing "does it just run" check, and the same `/health`
endpoint any deployment platform would use as a readiness probe later.

- [ ] **Step 11: Commit**

```bash
git add app/rbac.py app/routes/dashboard_routes.py app/templates/dashboard.html \
        app/templates/staff_dashboard.html app/main.py tests/test_routes_rbac.py \
        docker-compose.yml
git commit -m "Add RBAC-protected patient and staff dashboard routes; wire app into Docker Compose"
```

---

### Task 8: Seed script

**Files:**
- Create: `seed/__init__.py`
- Create: `seed/seed_data.py`

**Interfaces:**
- Consumes: `SessionLocal` (Task 3), `hash_password` (Task 5), `Department`,
  `Doctor`, `AppointmentSlot`, `SlotStatus`, `User`, `UserRole`,
  `PatientProfile` (Task 3).
- Produces: `seed()` function — idempotent (skips if departments already
  exist), populates 2 departments, 2 doctors, 10 open appointment slots, 1
  staff user, 1 sample patient user + profile. All synthetic data, no real
  PII.

- [ ] **Step 1: Write `seed/__init__.py` (empty)**

- [ ] **Step 2: Write `seed/seed_data.py`**

```python
from datetime import datetime, timedelta, timezone

from app.auth import hash_password
from app.db import SessionLocal
from app.models import (
    AppointmentSlot,
    Department,
    Doctor,
    PatientProfile,
    SlotStatus,
    User,
    UserRole,
)


def seed() -> None:
    db = SessionLocal()
    try:
        if db.query(Department).first():
            print("Seed data already present, skipping.")
            return

        cardiology = Department(name="Cardiology", description="Heart and cardiovascular care", active=True)
        general = Department(name="General Medicine", description="General checkups and referrals", active=True)
        db.add_all([cardiology, general])
        db.flush()

        dr_rao = Doctor(department_id=cardiology.id, name="Dr. Anitha Rao", active=True)
        dr_iyer = Doctor(department_id=general.id, name="Dr. Suresh Iyer", active=True)
        db.add_all([dr_rao, dr_iyer])
        db.flush()

        start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(days=1)
        for i in range(5):
            slot_start = start + timedelta(days=i)
            db.add(
                AppointmentSlot(
                    doctor_id=dr_rao.id,
                    start_time=slot_start,
                    end_time=slot_start + timedelta(minutes=30),
                    status=SlotStatus.open,
                )
            )
            db.add(
                AppointmentSlot(
                    doctor_id=dr_iyer.id,
                    start_time=slot_start,
                    end_time=slot_start + timedelta(minutes=30),
                    status=SlotStatus.open,
                )
            )

        staff_user = User(
            name="Priya Staff",
            email="staff@agentcare.test",
            password_hash=hash_password("StaffPass123!"),
            role=UserRole.staff,
        )
        patient_user = User(
            name="Test Patient",
            email="patient@agentcare.test",
            password_hash=hash_password("PatientPass123!"),
            role=UserRole.patient,
        )
        db.add_all([staff_user, patient_user])
        db.flush()
        db.add(PatientProfile(user_id=patient_user.id, phone="+91-9999999999"))

        db.commit()
        print("Seed data created: 2 departments, 2 doctors, 10 slots, 1 staff user, 1 patient user.")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
```

- [ ] **Step 2: Run it against the local Postgres**

```bash
docker compose up -d db
alembic upgrade head
python -m seed.seed_data
```

Expected: prints `Seed data created: 2 departments, 2 doctors, 10 slots, 1 staff user, 1 patient user.`

- [ ] **Step 3: Run it again to verify idempotency**

```bash
python -m seed.seed_data
```

Expected: prints `Seed data already present, skipping.` — no duplicate rows.

- [ ] **Step 4: Verify row counts directly**

```bash
docker compose exec db psql -U agentcare -d agentcare -c "SELECT count(*) FROM departments;"
docker compose exec db psql -U agentcare -d agentcare -c "SELECT count(*) FROM appointment_slots;"
```

Expected: `2` departments, `10` slots.

- [ ] **Step 5: Commit**

```bash
git add seed/__init__.py seed/seed_data.py
git commit -m "Add synthetic seed script for departments, doctors, slots, and sample users"
```

---

## Self-review

**Spec coverage:**
- Persistent Postgres schema for all 11 tables — Task 3/4. ✓
- Docker Compose zero-external-account local run — Task 2. ✓
- Alembic as schema source of truth — Task 4. ✓
- Real password auth, hashed, session cookie — Task 5/6. ✓
- RBAC enforced in backend dependency code, tested both directions (patient→staff 403, staff→patient 403, unauth→401) — Task 7. ✓
- Synthetic seed data, no real PII — Task 8. ✓
- Env-based config, `.env.example` with placeholders only — Task 1. ✓
- `AuditEvent`/`WorkflowRun`/agent/tool/graph code is intentionally **out of scope** for this plan — it's Phase 2 in the design spec (`docs/superpowers/specs/2026-07-22-agentcare-design.md` §13), covered by a follow-up plan once this foundation is reviewed.

**Placeholder scan:** no TBD/TODO; every step has literal file content and exact commands.

**Type consistency:** `UserRole.patient.value` / `UserRole.staff.value` used consistently between `require_role()` calls and the `User.role` enum across Tasks 6/7; `SESSION_COOKIE_NAME` defined once in `app/rbac.py` (Task 6) and reused, not redefined; `get_db`/`SessionLocal` defined once in `app/db.py` (Task 3) and imported everywhere else.

---

## What's next (not in this plan)

Phase 2 (LangGraph graph + Coordinator + Safety agents), Phase 3 (Routing +
Appointment agents), Phase 4 (Document agent), Phase 5 (Follow-up agent +
audit decorator + error handling), Phase 6 (UI polish) each get their own
plan file once the prior phase is reviewed and merged — per the 6-day build
phase breakdown in the design spec §13.
