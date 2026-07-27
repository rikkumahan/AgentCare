# Document Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Document Agent — the fourth of AgentCare's six required agents — so a patient can attach one file to their request, have it classified, checksummed, deduplicated, and checked against their upcoming appointment department's paperwork requirements, all through a real filesystem write and a real DB row, wired into the existing LangGraph workflow.

**Architecture:** A new `Department.required_document_types` JSON column drives an administrative (not clinical) "please upload X" completeness check. A new `app/tools/document_tools.py` does the real checksum/dedup/missing-doc logic behind an `@audited` + `@tool(response_format="content_and_artifact")` wrapper, mirroring `appointment_tools.py` exactly. A new `app/agents/document.py` subgraph (one LLM node + one tool call, looped until final text) mirrors `app/agents/appointment.py`'s capture-node shape. `app/graph.py` gets `document_agent` inserted unconditionally between `coordinator_agent` and `routing_agent`. The upload path is a new optional file field on the existing `/requests/new` form.

**Tech Stack:** FastAPI + Jinja2, LangGraph (`StateGraph`, `ToolNode`), LangChain `@tool`, SQLAlchemy + Alembic, Postgres, pytest with a real `db_session` fixture (no rollback isolation — unique data per test).

## Global Constraints

(Copied verbatim from `CLAUDE.md` — every task below implicitly inherits these.)

- Persistent SQL only — no in-memory dicts/session vars for patient, appointment, document, or workflow data.
- No tool may return a fixed response regardless of input — every tool does real DB/filesystem logic.
- No hardcoded final responses — any confirmation text must be rendered from rows just read back from the database, never a free-standing LLM string asserting success.
- RBAC is enforced in backend route/dependency code, not templates.
- Each agent gets its own system prompt and its own bound tool(s) — no sharing.
- Never diagnose, prescribe, change dosage, or claim to replace a clinician — administrative routing/paperwork-completeness language only (see spec §3 guardrail).
- No real PII, credentials, or secrets committed.
- Env-based config via `pydantic-settings`, reading `.env` — never hardcode `DATABASE_URL`, API keys, or `storage_dir`.

## Sequencing note (read before Task 7)

The source spec (`docs/superpowers/specs/2026-07-27-document-agent-design.md`) §5 shows the graph change as `graph.add_conditional_edges("document_agent", route_after_coordinator, {...})`. That function does not exist yet — it belongs to a separate, not-yet-built spec (`docs/superpowers/specs/2026-07-27-intent-branching-clarification-design.md`) that will be implemented in a later plan. **This plan inserts `document_agent` with two plain, unconditional edges**: `coordinator_agent -> document_agent` and `document_agent -> routing_agent`. A later plan will change `document_agent`'s outgoing edge to a conditional one; that change is out of scope here.

Similarly, the spec's §5 "Wording" section describes extending a `_render_patient_message` function on the status page. **That function does not exist in the current codebase** (`app/routes/request_routes.py` has no such helper — it belongs to the same later intent-branching plan). This plan does **not** build it. Task 9 instead adds one plain `<li>` line to the existing raw field-dump status page, exactly matching that page's current (deliberately unstyled) convention.

---

### Task 1: `Department.required_document_types` data model + migration + seed data

**Files:**
- Modify: `app/models.py:60-66` (the `Department` class)
- Create: `alembic/versions/<new_revision>_add_required_document_types.py`
- Modify: `seed/seed_data.py:23-24`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces: `Department.required_document_types: Mapped[list[str]]` (JSON column, default `[]`) — consumed by Task 3's `_missing_required_documents` and by seed data.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_models.py`:

```python
def test_department_required_document_types_defaults_to_empty_list(db_session):
    dept = Department(name=f"General-{uuid.uuid4().hex[:8]}", description="General care", active=True)
    db_session.add(dept)
    db_session.commit()

    fetched = db_session.get(Department, dept.id)
    assert fetched.required_document_types == []


def test_department_required_document_types_can_be_set(db_session):
    dept = Department(
        name=f"Cardiology-{uuid.uuid4().hex[:8]}",
        description="Heart care",
        active=True,
        required_document_types=["ecg"],
    )
    db_session.add(dept)
    db_session.commit()

    fetched = db_session.get(Department, dept.id)
    assert fetched.required_document_types == ["ecg"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_models.py -v`
Expected: FAIL — `TypeError: 'required_document_types' is an invalid keyword argument for Department` (or `AttributeError` on read).

- [ ] **Step 3: Add the column to `app/models.py`**

In `app/models.py`, the `Department` class currently reads (lines 60-66):

```python
class Department(Base):
    __tablename__ = "departments"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
```

Change it to:

```python
class Department(Base):
    __tablename__ = "departments"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    required_document_types: Mapped[list[str]] = mapped_column(JSON, default=list)
```

(`JSON` is already imported at the top of `app/models.py` — no new import needed.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_models.py -v`
Expected: PASS (both new tests).

- [ ] **Step 5: Write the Alembic migration**

`tests/conftest.py` builds its schema straight from `Base.metadata` (not via Alembic), so the tests above already pass without this step — but `docker compose up` + `alembic upgrade head` (the judged path per `CLAUDE.md`) still needs a real migration. Create `alembic/versions/b7e2f4a91c3d_add_required_document_types.py` (pick any fresh unique revision id if `b7e2f4a91c3d` collides — check `alembic/versions/` first):

```python
"""add required_document_types to departments

Revision ID: b7e2f4a91c3d
Revises: 1dd0ad4bbe02
Create Date: 2026-07-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e2f4a91c3d'
down_revision: Union[str, Sequence[str], None] = '1dd0ad4bbe02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'departments',
        sa.Column('required_document_types', sa.JSON(), nullable=False, server_default='[]'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('departments', 'required_document_types')
```

`server_default='[]'` matters here: any `Department` row already in the target database (seeded or judge-created) must get a valid, non-null value on upgrade, not just new rows going forward.

- [ ] **Step 6: Sanity-check the migration applies cleanly**

Run: `alembic upgrade head`
Expected: no errors; `alembic current` then reports the new revision id as head.
(If no local Postgres is running, note this step as deferred until a DB is available — do not skip writing the migration file itself.)

- [ ] **Step 7: Update seed data**

In `seed/seed_data.py`, the two `Department(...)` constructions (lines 23-24) currently read:

```python
        cardiology = Department(name="Cardiology", description="Heart and cardiovascular care", active=True)
        general = Department(name="General Medicine", description="General checkups and referrals", active=True)
```

Change to:

```python
        cardiology = Department(
            name="Cardiology",
            description="Heart and cardiovascular care",
            active=True,
            required_document_types=["ecg"],
        )
        general = Department(
            name="General Medicine",
            description="General checkups and referrals",
            active=True,
            required_document_types=[],
        )
```

- [ ] **Step 8: Commit**

```bash
git add app/models.py seed/seed_data.py alembic/versions/b7e2f4a91c3d_add_required_document_types.py tests/test_models.py
git commit -m "feat: add Department.required_document_types column, migration, and seed data"
```

---

### Task 2: `make_appointment` test factory helper

**Files:**
- Modify: `tests/fakes.py`

**Interfaces:**
- Consumes: `make_patient_profile`, `make_doctor`, `make_appointment_slot` (already in `tests/fakes.py`).
- Produces: `make_appointment(db_session, patient=None, doctor=None, slot=None, status=AppointmentStatus.confirmed) -> Appointment` — consumed by Task 3's tests.

This is a pure test-infrastructure addition with no independent behavior to TDD against in isolation; it's verified by the tests in Task 3 that consume it. Steps here just add the helper and prove it constructs a valid row.

- [ ] **Step 1: Add the imports and helper function**

In `tests/fakes.py`, the import block (lines 6-15) currently reads:

```python
from app.models import (
    AppointmentSlot,
    Department,
    Doctor,
    PatientProfile,
    SlotStatus,
    User,
    UserRole,
    WorkflowRun,
)
```

Change to:

```python
from app.models import (
    Appointment,
    AppointmentSlot,
    AppointmentStatus,
    Department,
    Doctor,
    PatientProfile,
    SlotStatus,
    User,
    UserRole,
    WorkflowRun,
)
```

Then add this function after `make_appointment_slot` (after line 76, before `make_workflow_run`):

```python
def make_appointment(
    db_session,
    patient: PatientProfile | None = None,
    doctor: Doctor | None = None,
    slot: AppointmentSlot | None = None,
    status: AppointmentStatus = AppointmentStatus.confirmed,
) -> Appointment:
    if patient is None:
        patient = make_patient_profile(db_session)
    if doctor is None:
        doctor = make_doctor(db_session)
    if slot is None:
        slot = make_appointment_slot(db_session, doctor=doctor)
    appointment = Appointment(
        patient_id=patient.id,
        doctor_id=doctor.id,
        slot_id=slot.id,
        status=status,
    )
    db_session.add(appointment)
    db_session.commit()
    return appointment
```

- [ ] **Step 2: Prove it works with a throwaway smoke check**

Run: `python -c "import ast; ast.parse(open('tests/fakes.py').read())"`
Expected: no output (valid syntax). Real behavioral proof comes from Task 3's tests, which call this helper directly against the real `db_session` fixture.

- [ ] **Step 3: Commit**

```bash
git add tests/fakes.py
git commit -m "test: add make_appointment factory helper for document-tool tests"
```

---

### Task 3: `_missing_required_documents`

**Files:**
- Create: `app/tools/document_tools.py`
- Test: `tests/test_document_tools.py`

**Interfaces:**
- Consumes: `Department.required_document_types` (Task 1), `make_appointment`/`make_department`/`make_doctor`/`make_patient_profile` (Task 2 + existing `tests/fakes.py`).
- Produces: `_missing_required_documents(db: Session, patient_id: str) -> list[str]` — consumed by Task 4's `store_and_classify_document`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_document_tools.py`:

```python
import uuid

from app.models import AppointmentStatus
from app.tools.document_tools import _missing_required_documents
from tests.fakes import make_appointment, make_department, make_doctor, make_patient_profile


def test_missing_required_documents_flags_gap_for_confirmed_cardiology_appointment(db_session):
    department = make_department(db_session, name=f"Cardiology {uuid.uuid4().hex[:8]}")
    department.required_document_types = ["ecg"]
    db_session.commit()
    doctor = make_doctor(db_session, department=department)
    profile = make_patient_profile(db_session)
    make_appointment(db_session, patient=profile, doctor=doctor, status=AppointmentStatus.confirmed)

    result = _missing_required_documents(db_session, str(profile.id))

    assert result == ["ecg"]


def test_missing_required_documents_returns_empty_for_patient_with_no_appointments(db_session):
    profile = make_patient_profile(db_session)

    result = _missing_required_documents(db_session, str(profile.id))

    assert result == []


def test_missing_required_documents_ignores_a_cancelled_appointment(db_session):
    # Regression test for the cross-check bug: a cancelled appointment's
    # department must not keep flagging the patient for paperwork tied to
    # a visit that isn't happening.
    department = make_department(db_session, name=f"Cardiology {uuid.uuid4().hex[:8]}")
    department.required_document_types = ["ecg"]
    db_session.commit()
    doctor = make_doctor(db_session, department=department)
    profile = make_patient_profile(db_session)
    make_appointment(db_session, patient=profile, doctor=doctor, status=AppointmentStatus.cancelled)

    result = _missing_required_documents(db_session, str(profile.id))

    assert result == []


def test_missing_required_documents_still_flags_gap_for_rescheduled_appointment(db_session):
    # Regression test for a second cross-check bug: "rescheduled" is NOT a
    # cancelled/inactive status - book_or_modify_appointment's reschedule
    # branch mutates the SAME row's slot/doctor and sets status=rescheduled
    # permanently, it never flips back to confirmed. A rescheduled
    # appointment is still an active, upcoming visit and must still count.
    department = make_department(db_session, name=f"Cardiology {uuid.uuid4().hex[:8]}")
    department.required_document_types = ["ecg"]
    db_session.commit()
    doctor = make_doctor(db_session, department=department)
    profile = make_patient_profile(db_session)
    make_appointment(db_session, patient=profile, doctor=doctor, status=AppointmentStatus.rescheduled)

    result = _missing_required_documents(db_session, str(profile.id))

    assert result == ["ecg"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_document_tools.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.tools.document_tools'`.

- [ ] **Step 3: Create `app/tools/document_tools.py` with `_missing_required_documents`**

```python
import uuid

from sqlalchemy.orm import Session

from app.models import Appointment, AppointmentStatus, Department, Doctor, PatientDocument


def _missing_required_documents(db: Session, patient_id: str) -> list[str]:
    """Departments tied to this patient's active appointments, unioned
    required_document_types, minus the document_type values the patient
    already has on file. "Active" means status in
    (pending, confirmed, rescheduled) - i.e. everything except cancelled.
    rescheduled is deliberately included: book_or_modify_appointment's
    reschedule branch mutates the SAME appointment row (new slot/doctor)
    and sets status=rescheduled permanently, it never flips back to
    confirmed - so a rescheduled appointment is still an active, upcoming
    visit, not a cancelled one, and must still count. Shared by
    store_and_classify_document (below) and the Follow-up agent's scan
    (same gap, two different callers) - not duplicated logic."""
    patient_uuid = uuid.UUID(patient_id)
    departments = (
        db.query(Department)
        .join(Doctor, Doctor.department_id == Department.id)
        .join(Appointment, Appointment.doctor_id == Doctor.id)
        .filter(Appointment.patient_id == patient_uuid)
        .filter(
            Appointment.status.in_(
                [AppointmentStatus.pending, AppointmentStatus.confirmed, AppointmentStatus.rescheduled]
            )
        )
        .distinct()
        .all()
    )
    required: set[str] = set()
    for department in departments:
        required.update(department.required_document_types or [])

    have = {
        doc.document_type.value
        for doc in db.query(PatientDocument).filter(PatientDocument.patient_id == patient_uuid).all()
    }
    return sorted(required - have)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_document_tools.py -v`
Expected: PASS (all 4 tests).

- [ ] **Step 5: Commit**

```bash
git add app/tools/document_tools.py tests/test_document_tools.py
git commit -m "feat: add _missing_required_documents with cancelled/rescheduled-appointment regression tests"
```

---

### Task 4: `_checksum_file` + `store_and_classify_document`

**Files:**
- Modify: `app/tools/document_tools.py`
- Test: `tests/test_document_tools.py`

**Interfaces:**
- Consumes: `_missing_required_documents` (Task 3), `app.audit.audited`, `app.models.DocumentType`/`PatientDocument`.
- Produces: `_checksum_file(file_path: str) -> str`, `store_and_classify_document(db, patient_id, file_path, document_type) -> dict` with shape `{"id": str|None, "status": "saved"|"duplicate"|"error", "document_type": str, "missing_document_types": list[str]}` (error case: `{"id": None, "status": "error", "error": str}`) — consumed by Task 5's tool wrapper and Task 6's agent tests.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_document_tools.py`:

```python
import hashlib

from app.models import AuditEvent, DocumentType, PatientDocument
from app.tools.document_tools import _checksum_file, store_and_classify_document


def test_checksum_file_returns_sha256_of_real_bytes(tmp_path):
    file_path = tmp_path / "sample.txt"
    file_path.write_bytes(b"hello world")

    checksum = _checksum_file(str(file_path))

    assert checksum == hashlib.sha256(b"hello world").hexdigest()


def test_store_and_classify_document_saves_new_document(tmp_path, db_session):
    profile = make_patient_profile(db_session)
    file_path = tmp_path / "ecg_scan.pdf"
    file_path.write_bytes(b"ecg-bytes-1")

    result = store_and_classify_document(db_session, str(profile.id), str(file_path), "ecg")

    assert result["status"] == "saved"
    assert result["document_type"] == "ecg"
    document = db_session.query(PatientDocument).filter(PatientDocument.id == uuid.UUID(result["id"])).one()
    assert document.patient_id == profile.id
    assert document.document_type == DocumentType.ecg


def test_store_and_classify_document_detects_duplicate_by_content_not_filename(tmp_path, db_session):
    profile = make_patient_profile(db_session)
    file_a = tmp_path / "first.pdf"
    file_a.write_bytes(b"same-bytes")
    file_b = tmp_path / "second.pdf"
    file_b.write_bytes(b"same-bytes")

    first = store_and_classify_document(db_session, str(profile.id), str(file_a), "lab_report")
    second = store_and_classify_document(db_session, str(profile.id), str(file_b), "lab_report")

    assert first["status"] == "saved"
    assert second["status"] == "duplicate"
    assert second["id"] == first["id"]
    count = db_session.query(PatientDocument).filter(PatientDocument.patient_id == profile.id).count()
    assert count == 1


def test_store_and_classify_document_different_patients_same_bytes_each_get_own_row(tmp_path, db_session):
    profile_a = make_patient_profile(db_session)
    profile_b = make_patient_profile(db_session)
    file_a = tmp_path / "a.pdf"
    file_a.write_bytes(b"shared-bytes")
    file_b = tmp_path / "b.pdf"
    file_b.write_bytes(b"shared-bytes")

    result_a = store_and_classify_document(db_session, str(profile_a.id), str(file_a), "insurance")
    result_b = store_and_classify_document(db_session, str(profile_b.id), str(file_b), "insurance")

    assert result_a["status"] == "saved"
    assert result_b["status"] == "saved"
    assert result_a["id"] != result_b["id"]


def test_store_and_classify_document_falls_back_to_other_for_unknown_type(tmp_path, db_session):
    profile = make_patient_profile(db_session)
    file_path = tmp_path / "mystery.bin"
    file_path.write_bytes(b"mystery-bytes")

    result = store_and_classify_document(db_session, str(profile.id), str(file_path), "not_a_real_type")

    assert result["status"] == "saved"
    assert result["document_type"] == "other"


def test_store_and_classify_document_rejects_missing_file(db_session):
    profile = make_patient_profile(db_session)

    result = store_and_classify_document(db_session, str(profile.id), "/no/such/file.pdf", "ecg")

    assert result["status"] == "error"
    assert result["id"] is None


def test_store_and_classify_document_rejects_empty_file(tmp_path, db_session):
    profile = make_patient_profile(db_session)
    file_path = tmp_path / "empty.pdf"
    file_path.write_bytes(b"")

    result = store_and_classify_document(db_session, str(profile.id), str(file_path), "ecg")

    assert result["status"] == "error"


def test_store_and_classify_document_writes_audit_event(tmp_path, db_session):
    profile = make_patient_profile(db_session)
    file_path = tmp_path / "audit.pdf"
    file_path.write_bytes(b"audit-bytes")

    store_and_classify_document(db_session, str(profile.id), str(file_path), "ecg")

    audit_actions = {e.action for e in db_session.query(AuditEvent).all()}
    assert "store_and_classify_document" in audit_actions


def test_store_and_classify_document_reports_missing_types_alongside_save(tmp_path, db_session):
    department = make_department(db_session, name=f"Cardiology {uuid.uuid4().hex[:8]}")
    department.required_document_types = ["ecg", "insurance"]
    db_session.commit()
    doctor = make_doctor(db_session, department=department)
    profile = make_patient_profile(db_session)
    make_appointment(db_session, patient=profile, doctor=doctor, status=AppointmentStatus.confirmed)
    file_path = tmp_path / "insurance_card.pdf"
    file_path.write_bytes(b"insurance-bytes")

    result = store_and_classify_document(db_session, str(profile.id), str(file_path), "insurance")

    assert result["status"] == "saved"
    assert result["missing_document_types"] == ["ecg"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_document_tools.py -v`
Expected: FAIL — `ImportError: cannot import name '_checksum_file'` (and `store_and_classify_document`).

- [ ] **Step 3: Add `_checksum_file` and `store_and_classify_document` to `app/tools/document_tools.py`**

At the top of `app/tools/document_tools.py`, change the imports from:

```python
import uuid

from sqlalchemy.orm import Session

from app.models import Appointment, AppointmentStatus, Department, Doctor, PatientDocument
```

to:

```python
import hashlib
import os
import uuid

from sqlalchemy.orm import Session

from app.audit import audited
from app.models import Appointment, AppointmentStatus, Department, Doctor, DocumentType, PatientDocument
```

Then append these two functions at the end of the file (after `_missing_required_documents`):

```python
def _checksum_file(file_path: str) -> str:
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


@audited("store_and_classify_document", "PatientDocument")
def store_and_classify_document(db: Session, patient_id: str, file_path: str, document_type: str) -> dict:
    if not os.path.isfile(file_path):
        return {"id": None, "status": "error", "error": f"File not found: {file_path}"}
    if os.path.getsize(file_path) == 0:
        return {"id": None, "status": "error", "error": f"File is empty: {file_path}"}

    checksum = _checksum_file(file_path)
    patient_uuid = uuid.UUID(patient_id)

    existing = (
        db.query(PatientDocument)
        .filter(PatientDocument.patient_id == patient_uuid)
        .filter(PatientDocument.checksum == checksum)
        .first()
    )
    if existing is not None:
        return {
            "id": str(existing.id),
            "status": "duplicate",
            "document_type": existing.document_type.value,
            "missing_document_types": _missing_required_documents(db, patient_id),
        }

    try:
        doc_type_enum = DocumentType(document_type)
    except ValueError:
        # The model could in principle send something off the fixed list -
        # fall back rather than crash the whole graph run over a
        # classification quibble.
        doc_type_enum = DocumentType.other

    document = PatientDocument(
        patient_id=patient_uuid,
        document_type=doc_type_enum,
        file_path=file_path,
        checksum=checksum,
    )
    db.add(document)
    db.commit()
    return {
        "id": str(document.id),
        "status": "saved",
        "document_type": document.document_type.value,
        "missing_document_types": _missing_required_documents(db, patient_id),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_document_tools.py -v`
Expected: PASS (all tests in the file).

- [ ] **Step 5: Commit**

```bash
git add app/tools/document_tools.py tests/test_document_tools.py
git commit -m "feat: add checksum-based duplicate detection to store_and_classify_document"
```

---

### Task 5: `store_and_classify_document_tool` (the LangChain tool wrapper)

**Files:**
- Modify: `app/tools/document_tools.py`
- Test: `tests/test_document_tools.py`

**Interfaces:**
- Consumes: `store_and_classify_document` (Task 4).
- Produces: `_document_summary(result: dict) -> str`, `store_and_classify_document_tool` (a `@tool(response_format="content_and_artifact")`-decorated function taking `file_path: str`, `document_type: str`, `patient_id` via `InjectedState("patient_id")`, `config: RunnableConfig`) — consumed by Task 6's `document_tools_node`/`ToolNode`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_document_tools.py`:

```python
from app.tools.document_tools import _document_summary


def test_document_summary_includes_real_status_and_missing_types_not_a_bare_word():
    # This string is the only part of the tool result the model actually
    # sees on its next turn - artifact never gets serialized back into the
    # conversation. A bare status word gives the model nothing to act on,
    # same content-vs-artifact lesson as _slots_summary/_departments_summary.
    result = {
        "id": "doc-1",
        "status": "saved",
        "document_type": "ecg",
        "missing_document_types": ["insurance"],
    }

    summary = _document_summary(result)

    assert "saved" in summary
    assert "ecg" in summary
    assert "insurance" in summary


def test_document_summary_handles_duplicate_status():
    result = {"id": "doc-1", "status": "duplicate", "document_type": "ecg", "missing_document_types": []}

    summary = _document_summary(result)

    assert "duplicate" in summary


def test_document_summary_handles_error_status():
    result = {"id": None, "status": "error", "error": "File not found: /no/such/file.pdf"}

    summary = _document_summary(result)

    assert "error" in summary
    assert "File not found" in summary
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_document_tools.py -v`
Expected: FAIL — `ImportError: cannot import name '_document_summary'`.

- [ ] **Step 3: Add `_document_summary` and `store_and_classify_document_tool`**

At the top of `app/tools/document_tools.py`, change the imports from:

```python
import hashlib
import os
import uuid

from sqlalchemy.orm import Session

from app.audit import audited
from app.models import Appointment, AppointmentStatus, Department, Doctor, DocumentType, PatientDocument
```

to:

```python
import hashlib
import os
import uuid
from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from sqlalchemy.orm import Session

from app.audit import audited
from app.models import Appointment, AppointmentStatus, Department, Doctor, DocumentType, PatientDocument
```

Then append at the end of the file:

```python
def _document_summary(result: dict) -> str:
    # Same content-vs-artifact gotcha documented in docs/memory/gotchas.md:
    # only `content` survives into the model's next turn - a bare status
    # word gives it nothing to act on when deciding whether to reply or
    # mention missing paperwork.
    if result["status"] == "error":
        return f"Document store result: error - {result['error']}"
    missing = result.get("missing_document_types") or []
    summary = f"Document store result: {result['status']} as {result['document_type']}."
    if missing:
        summary += f" Missing document types still needed: {', '.join(missing)}."
    return summary


@tool(response_format="content_and_artifact")
def store_and_classify_document_tool(
    file_path: str,
    document_type: str,
    patient_id: Annotated[str, InjectedState("patient_id")],
    config: RunnableConfig,
):
    """Save and classify a document the patient attached. document_type
    must be one of: ecg, lab_report, prescription_old, insurance, id_proof,
    other - pick the best fit based on the filename and any note the
    patient wrote in their request."""
    db = config["configurable"]["db"]
    result = store_and_classify_document(db, patient_id, file_path, document_type)
    return _document_summary(result), result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_document_tools.py -v`
Expected: PASS (entire file).

- [ ] **Step 5: Commit**

```bash
git add app/tools/document_tools.py tests/test_document_tools.py
git commit -m "feat: add store_and_classify_document_tool with real content summary"
```

---

### Task 6: `app/agents/document.py` — Document Agent subgraph

**Files:**
- Create: `app/agents/document.py`
- Test: `tests/test_document_agent.py`

**Interfaces:**
- Consumes: `store_and_classify_document_tool` (Task 5), `app.agents.state.WorkflowState` (existing — has `uploaded_files: list[str]`, `patient_id: str`, `request_text: str`, `document_ids: list[str]`), `app.llm.get_llm`/`invoke_with_retry` (existing).
- Produces: `DocumentState` (TypedDict), `document_llm_node`, `document_tools_node`, `document_capture_node`, `route_after_document_llm`, `build_document_subgraph()`, `document_agent_node(state: WorkflowState, config) -> dict` returning `{"document_ids": [...]}` or `{}` — consumed by Task 7's `app/graph.py`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_document_agent.py`:

```python
import uuid

from langchain_core.messages import AIMessage

from app.models import PatientDocument
from tests.fakes import FakeToolCallingModel, ai_message_text, ai_message_with_tool_call, make_patient_profile, workflow_state


def test_document_agent_node_is_a_no_op_when_no_files_attached(monkeypatch, db_session):
    def _explode():
        raise AssertionError("get_llm should not be called when no file is attached")

    monkeypatch.setattr("app.agents.document.get_llm", _explode)

    from app.agents.document import document_agent_node

    state = workflow_state(uploaded_files=[])
    update = document_agent_node(state, config={"configurable": {"db": db_session}})

    assert update == {}


def test_document_agent_node_saves_uploaded_file_and_returns_document_id(monkeypatch, tmp_path, db_session):
    profile = make_patient_profile(db_session)
    file_path = tmp_path / "insurance_card.pdf"
    file_path.write_bytes(b"insurance-bytes")

    fake_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call(
                "store_and_classify_document_tool",
                {"file_path": str(file_path), "document_type": "insurance"},
            ),
            ai_message_text("Saved your insurance document."),
        ]
    )
    monkeypatch.setattr("app.agents.document.get_llm", lambda: fake_model)

    from app.agents.document import document_agent_node

    state = workflow_state(patient_id=str(profile.id), uploaded_files=[str(file_path)])
    update = document_agent_node(state, config={"configurable": {"db": db_session}})

    assert len(update["document_ids"]) == 1
    document = (
        db_session.query(PatientDocument)
        .filter(PatientDocument.id == uuid.UUID(update["document_ids"][0]))
        .one()
    )
    assert document.document_type.value == "insurance"


def test_document_agent_node_skips_a_failed_upload_id(monkeypatch, db_session):
    # An error result has id=None - it must not leak a None into document_ids.
    fake_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call(
                "store_and_classify_document_tool",
                {"file_path": "/no/such/file.pdf", "document_type": "other"},
            ),
            ai_message_text("Could not save that file."),
        ]
    )
    monkeypatch.setattr("app.agents.document.get_llm", lambda: fake_model)

    from app.agents.document import document_agent_node

    state = workflow_state(uploaded_files=["/no/such/file.pdf"])
    update = document_agent_node(state, config={"configurable": {"db": db_session}})

    assert update["document_ids"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_document_agent.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.agents.document'`.

- [ ] **Step 3: Create `app/agents/document.py`**

```python
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from app.agents.state import WorkflowState
from app.llm import get_llm, invoke_with_retry
from app.tools.document_tools import store_and_classify_document_tool

DOCUMENT_SYSTEM_PROMPT = (
    "You are the Document Agent for AgentCare, an administrative healthcare "
    "workflow assistant. A patient has attached one file to their request. "
    "Call store_and_classify_document with the given file_path and your "
    "best-fit document_type, chosen from this fixed list: ecg, lab_report, "
    "prescription_old, insurance, id_proof, other. Base your choice on the "
    "filename and the patient's own request text, used only as a note — "
    "never open, read, or interpret the file's actual contents, and never "
    "diagnose or interpret what a document means medically; you are only "
    "filing paperwork, not reviewing it. Once the tool returns a result, "
    "reply with a short confirmation sentence and do not call any more "
    "tools."
)

document_tools = [store_and_classify_document_tool]
document_tools_node = ToolNode(document_tools)


class DocumentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    patient_id: str
    file_path: str
    document_result: dict | None


def document_llm_node(state: DocumentState, config):
    model = get_llm().bind_tools(document_tools)
    messages = [SystemMessage(DOCUMENT_SYSTEM_PROMPT), *state["messages"]]
    ai_message = invoke_with_retry(model, messages)
    return {"messages": [ai_message]}


def document_capture_node(state: DocumentState, config):
    last = state["messages"][-1]
    if isinstance(last, ToolMessage) and last.name == "store_and_classify_document_tool":
        return {"document_result": last.artifact}
    return {}


def route_after_document_llm(state: DocumentState) -> Literal["document_tools", "__end__"]:
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None):
        return "document_tools"
    return "__end__"


def build_document_subgraph():
    graph = StateGraph(DocumentState)
    graph.add_node("document_llm", document_llm_node)
    graph.add_node("document_tools", document_tools_node)
    graph.add_node("document_capture", document_capture_node)
    graph.set_entry_point("document_llm")
    graph.add_conditional_edges(
        "document_llm",
        route_after_document_llm,
        {"document_tools": "document_tools", "__end__": END},
    )
    graph.add_edge("document_tools", "document_capture")
    graph.add_edge("document_capture", "document_llm")
    return graph.compile()


_document_subgraph = build_document_subgraph()


def document_agent_node(state: WorkflowState, config) -> dict:
    """Parent-graph node (registered as "document_agent" in app/graph.py).
    If no file was attached to this request, returns {} immediately - a
    true no-op, no LLM call, for the overwhelmingly common case. Otherwise
    invokes the private Document subgraph once per attached file (today,
    always exactly one) and collects each successful result's real id."""
    uploaded_files = state.get("uploaded_files") or []
    if not uploaded_files:
        return {}

    document_ids = []
    for file_path in uploaded_files:
        result = _document_subgraph.invoke(
            {
                "messages": [
                    HumanMessage(f"file_path: {file_path}\nrequest: {state['request_text']}")
                ],
                "patient_id": state["patient_id"],
                "file_path": file_path,
                "document_result": None,
            },
            config=config,
        )
        document_result = result.get("document_result") or {}
        document_id = document_result.get("id")
        if document_id:
            document_ids.append(document_id)

    return {"document_ids": document_ids}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_document_agent.py -v`
Expected: PASS (all 3 tests).

- [ ] **Step 5: Commit**

```bash
git add app/agents/document.py tests/test_document_agent.py
git commit -m "feat: add Document Agent subgraph with true no-op short-circuit"
```

---

### Task 7: Wire `document_agent` into the parent graph

**Files:**
- Modify: `app/graph.py`

**Interfaces:**
- Consumes: `document_agent_node` (Task 6).
- Produces: parent graph edges `coordinator_agent -> document_agent -> routing_agent` (both unconditional) — no new interface for later tasks; this is the integration point.

- [ ] **Step 1: Edit `app/graph.py`**

The current file reads:

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

Change the import block to add `document_agent_node`:

```python
from typing import Literal

from langgraph.graph import END, StateGraph

from app.agents.appointment import appointment_agent_node
from app.agents.coordinator import coordinator_agent_node
from app.agents.document import document_agent_node
from app.agents.routing import routing_agent_node
from app.agents.safety import safety_agent_node
from app.agents.state import WorkflowState
```

Change `build_graph()` to:

```python
def build_graph():
    graph = StateGraph(WorkflowState)

    graph.add_node("safety_agent", safety_agent_node)
    graph.add_node("coordinator_agent", coordinator_agent_node)
    graph.add_node("document_agent", document_agent_node)
    graph.add_node("routing_agent", routing_agent_node)
    graph.add_node("appointment_agent", appointment_agent_node)

    graph.set_entry_point("safety_agent")
    graph.add_conditional_edges(
        "safety_agent", route_after_safety, {"coordinator_agent": "coordinator_agent", "__end__": END}
    )
    graph.add_edge("coordinator_agent", "document_agent")
    graph.add_edge("document_agent", "routing_agent")
    graph.add_conditional_edges(
        "routing_agent", route_after_routing, {"appointment_agent": "appointment_agent", "__end__": END}
    )
    graph.add_edge("appointment_agent", END)

    return graph.compile()
```

`route_after_safety` and `route_after_routing` are unchanged.

- [ ] **Step 2: Regression-check the full existing suite**

Since `document_agent` is now spliced into every workflow run (including every request with no attached file), the existing suite must still pass unchanged — `document_agent_node`'s no-op short-circuit (Task 6) is what makes this safe.

Run: `pytest tests/test_workflow_runner.py tests/test_request_routes.py -v`
Expected: PASS — all pre-existing tests in both files still pass with no modifications. In particular, `tests/test_workflow_runner.py::test_full_workflow_books_appointment_end_to_end` already asserts `workflow_run.current_step == "document_agent"`, which was previously true only because of a pre-existing hardcoded assignment in `app/workflow_runner.py` (unrelated to this plan, left untouched) — it should still pass for the same reason plus now-real reasons.

- [ ] **Step 3: Run the full test suite**

Run: `pytest -v`
Expected: PASS — every test in the repo, old and new.

- [ ] **Step 4: Commit**

```bash
git add app/graph.py
git commit -m "feat: wire document_agent unconditionally between coordinator and routing"
```

---

### Task 8: Upload path in `app/routes/request_routes.py`

**Files:**
- Modify: `app/routes/request_routes.py`

**Interfaces:**
- Consumes: `app.config.settings.storage_dir` (existing), `app.workflow_runner.run_workflow(..., uploaded_files: list[str] | None = None)` (existing signature, confirmed in `app/workflow_runner.py:10-16`).
- Produces: `POST /requests/new` now accepts an optional `document` multipart field and threads a real saved file path into `run_workflow`.

- [ ] **Step 1: Edit imports in `app/routes/request_routes.py`**

Current imports (lines 1-11):

```python
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import PatientProfile, User, UserRole, WorkflowRun
from app.rbac import require_role
from app.workflow_runner import run_workflow
```

Change to:

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

- [ ] **Step 2: Edit `submit_request` to accept and save the optional file**

Current function (lines 40-69):

```python
@router.post("/requests/new")
def submit_request(
    request_text: str = Form(...),
    user: User = Depends(require_role(UserRole.patient.value)),
    db: Session = Depends(get_db),
):
    profile = _get_or_create_profile(db, user)

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=DUPLICATE_SUBMIT_WINDOW_SECONDS)
    recent = (
        db.query(WorkflowRun)
        .filter(WorkflowRun.patient_id == profile.id)
        .filter(WorkflowRun.created_at >= cutoff)
        .order_by(WorkflowRun.created_at.desc())
        .first()
    )
    if recent is not None and recent.state.get("request_text") == request_text:
        # Same patient, same exact text, within the window - treat as a
        # duplicate submission and show the existing run instead of
        # starting a second real workflow (and possibly a second real
        # booking) for what is almost certainly one intended request.
        return RedirectResponse(f"/requests/{recent.id}", status_code=status.HTTP_303_SEE_OTHER)

    workflow_run = run_workflow(
        db,
        patient_id=str(profile.id),
        user_id=str(user.id),
        request_text=request_text,
    )
    return RedirectResponse(f"/requests/{workflow_run.id}", status_code=status.HTTP_303_SEE_OTHER)
```

Change to:

```python
@router.post("/requests/new")
def submit_request(
    request_text: str = Form(...),
    document: UploadFile | None = File(None),
    user: User = Depends(require_role(UserRole.patient.value)),
    db: Session = Depends(get_db),
):
    profile = _get_or_create_profile(db, user)

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=DUPLICATE_SUBMIT_WINDOW_SECONDS)
    recent = (
        db.query(WorkflowRun)
        .filter(WorkflowRun.patient_id == profile.id)
        .filter(WorkflowRun.created_at >= cutoff)
        .order_by(WorkflowRun.created_at.desc())
        .first()
    )
    if recent is not None and recent.state.get("request_text") == request_text:
        # Same patient, same exact text, within the window - treat as a
        # duplicate submission and show the existing run instead of
        # starting a second real workflow (and possibly a second real
        # booking) for what is almost certainly one intended request.
        return RedirectResponse(f"/requests/{recent.id}", status_code=status.HTTP_303_SEE_OTHER)

    uploaded_files: list[str] = []
    if document is not None and document.filename:
        patient_dir = os.path.join(settings.storage_dir, str(profile.id))
        os.makedirs(patient_dir, exist_ok=True)
        saved_path = os.path.join(patient_dir, f"{uuid.uuid4().hex}_{document.filename}")
        with open(saved_path, "wb") as f:
            f.write(document.file.read())
        uploaded_files = [saved_path]

    workflow_run = run_workflow(
        db,
        patient_id=str(profile.id),
        user_id=str(user.id),
        request_text=request_text,
        uploaded_files=uploaded_files,
    )
    return RedirectResponse(f"/requests/{workflow_run.id}", status_code=status.HTTP_303_SEE_OTHER)
```

(The random `uuid4().hex` prefix avoids collisions between patients or repeated same-named uploads — it is a naming convenience, not a security boundary, since the directory is already scoped per patient, matching spec §5.)

- [ ] **Step 3: Regression-check existing route tests still pass unmodified**

Run: `pytest tests/test_request_routes.py -v`
Expected: PASS — every existing test in this file (none of which attach a `document` field) continues to pass, since `document` defaults to `None` and the new branch is skipped entirely.

- [ ] **Step 4: Commit**

```bash
git add app/routes/request_routes.py
git commit -m "feat: accept an optional file upload on POST /requests/new"
```

---

### Task 9: Templates — upload field and status display

**Files:**
- Modify: `app/templates/request_new.html`
- Modify: `app/templates/request_status.html`

**Interfaces:**
- Consumes: nothing new (pure template edits, no new Python interface).
- Produces: a working file-upload form; a visible `document_ids` line on the status page. No other task depends on the exact markup.

- [ ] **Step 1: Add the file input to `app/templates/request_new.html`**

Current file:

```html
{% extends "base.html" %}
{% block title %}New Request - AgentCare{% endblock %}
{% block content %}
<h1>Submit a request</h1>
<form method="post" action="/requests/new" onsubmit="this.querySelector('button').disabled = true;">
    <textarea name="request_text" rows="4" cols="50" required placeholder="e.g. book a cardiology appointment"></textarea>
    <br>
    <button type="submit">Submit</button>
</form>
{% endblock %}
```

Change to:

```html
{% extends "base.html" %}
{% block title %}New Request - AgentCare{% endblock %}
{% block content %}
<h1>Submit a request</h1>
<form method="post" action="/requests/new" enctype="multipart/form-data" onsubmit="this.querySelector('button').disabled = true;">
    <textarea name="request_text" rows="4" cols="50" required placeholder="e.g. book a cardiology appointment"></textarea>
    <br>
    <input type="file" name="document">
    <br>
    <button type="submit">Submit</button>
</form>
{% endblock %}
```

`enctype="multipart/form-data"` is required — without it the browser sends only the filename as plain text, never the file's bytes.

- [ ] **Step 2: Add a document line to `app/templates/request_status.html`**

Current file:

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
    <li>Escalation: {{ workflow_run.state.get("escalation") }}</li>
</ul>
<a href="/requests/new">Submit another request</a>
{% endblock %}
```

Change to:

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

This raw list-of-ids display is deliberately temporary — the not-yet-built intent-branching plan replaces this entire page's wording with a human-sentence renderer (per this plan's "Sequencing note" above). Do not build that renderer here.

- [ ] **Step 3: Manually verify the form renders**

Run the app (`docker compose up` or local uvicorn per project README), log in as the seeded patient user, visit `/requests/new`, and confirm a file-choose control appears below the textarea. This step has no automated assertion — it's a template markup sanity check; Task 10 covers the same path end-to-end with `TestClient`.

- [ ] **Step 4: Commit**

```bash
git add app/templates/request_new.html app/templates/request_status.html
git commit -m "feat: add optional file upload field and document_ids display"
```

---

### Task 10: End-to-end route test with a real uploaded file

**Files:**
- Modify: `tests/test_request_routes.py`

**Interfaces:**
- Consumes: the fully wired upload path (Tasks 6-8), `app.tools.document_tools.store_and_classify_document_tool`.
- Produces: nothing new for later tasks — this is the final integration proof for this plan.

The `FakeToolCallingModel` in `tests/fakes.py` returns pre-scripted responses regardless of what it's asked, but this test needs the mocked Document model to call the tool with the **real** saved file path — which contains a fresh `uuid4()` generated inside the route handler and can't be predicted before the request runs. So this task adds one small custom test double, local to this file, that reads the real path out of the incoming message instead of using a value scripted in advance.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_request_routes.py`. First, extend the existing imports (current imports at lines 1-15):

```python
import uuid

from fastapi.testclient import TestClient

from app.auth import hash_password
from app.main import app
from app.models import Appointment, User, UserRole, WorkflowRun
from tests.fakes import (
    FakeToolCallingModel,
    ai_message_text,
    ai_message_with_tool_call,
    make_appointment_slot,
    make_department,
    make_doctor,
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
from app.models import Appointment, PatientDocument, User, UserRole, WorkflowRun
from tests.fakes import (
    FakeToolCallingModel,
    ai_message_text,
    ai_message_with_tool_call,
    make_appointment_slot,
    make_department,
    make_doctor,
)


class _FileAwareDocumentModel:
    """Stands in for the Document Agent's LLM in a route-level test. Unlike
    FakeToolCallingModel, it reads the real file_path out of the incoming
    HumanMessage instead of using a value scripted in advance - needed
    because app/routes/request_routes.py generates the saved path with a
    fresh uuid4() we can't predict before the request runs."""

    def __init__(self):
        self._called = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self._called += 1
        if self._called == 1:
            human_text = messages[-1].content
            file_path = human_text.split("file_path: ")[1].split("\n")[0]
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "store_and_classify_document_tool",
                        "args": {"file_path": file_path, "document_type": "insurance"},
                        "id": "call_1",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content="Saved your document.")
```

Then append these two tests at the end of the file:

```python
def test_submitting_request_with_attached_file_saves_it_to_disk_and_creates_document_row(monkeypatch, db_session):
    safety_model = FakeToolCallingModel([ai_message_text("SAFE")])
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: safety_model)
    coordinator_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("get_or_create_patient_tool", {"profile_fields": {}}),
            ai_message_text("submit_document"),
        ]
    )
    monkeypatch.setattr("app.agents.coordinator.get_llm", lambda: coordinator_model)
    document_model = _FileAwareDocumentModel()
    monkeypatch.setattr("app.agents.document.get_llm", lambda: document_model)
    routing_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("lookup_departments_tool", {"query_hint": "x"}),
            ai_message_text("UNMATCHED"),
        ]
    )
    monkeypatch.setattr("app.agents.routing.get_llm", lambda: routing_model)

    cookie = _register_patient("Doc Patient")
    client.cookies.set("agentcare_session", cookie)

    resp = client.post(
        "/requests/new",
        data={"request_text": "here is my insurance card"},
        files={"document": ("insurance.pdf", b"insurance-file-bytes-1", "application/pdf")},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    workflow_run_id = resp.headers["location"].rsplit("/", 1)[-1]
    workflow_run = db_session.get(WorkflowRun, workflow_run_id)
    document_ids = workflow_run.state.get("document_ids") or []
    assert len(document_ids) == 1

    document = db_session.query(PatientDocument).filter(PatientDocument.id == uuid.UUID(document_ids[0])).one()
    assert os.path.isfile(document.file_path)
    with open(document.file_path, "rb") as f:
        assert f.read() == b"insurance-file-bytes-1"


def test_submitting_same_document_bytes_twice_does_not_create_a_second_row(monkeypatch, db_session):
    safety_model = FakeToolCallingModel([ai_message_text("SAFE"), ai_message_text("SAFE")])
    monkeypatch.setattr("app.agents.safety.get_llm", lambda: safety_model)
    coordinator_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("get_or_create_patient_tool", {"profile_fields": {}}),
            ai_message_text("submit_document"),
            ai_message_with_tool_call("get_or_create_patient_tool", {"profile_fields": {}}),
            ai_message_text("submit_document"),
        ]
    )
    monkeypatch.setattr("app.agents.coordinator.get_llm", lambda: coordinator_model)
    routing_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("lookup_departments_tool", {"query_hint": "x"}),
            ai_message_text("UNMATCHED"),
            ai_message_with_tool_call("lookup_departments_tool", {"query_hint": "x"}),
            ai_message_text("UNMATCHED"),
        ]
    )
    monkeypatch.setattr("app.agents.routing.get_llm", lambda: routing_model)

    cookie = _register_patient("Dup Doc Patient")
    client.cookies.set("agentcare_session", cookie)

    # Different request_text on each submission - identical text within
    # DUPLICATE_SUBMIT_WINDOW_SECONDS would trip the route's own
    # same-request dedup guard and redirect to the first run without
    # running a second workflow at all, which would defeat this test.
    monkeypatch.setattr("app.agents.document.get_llm", lambda: _FileAwareDocumentModel())
    first = client.post(
        "/requests/new",
        data={"request_text": "here is my insurance card, first upload"},
        files={"document": ("insurance.pdf", b"same-insurance-bytes", "application/pdf")},
        follow_redirects=False,
    )
    assert first.status_code == 303

    monkeypatch.setattr("app.agents.document.get_llm", lambda: _FileAwareDocumentModel())
    second = client.post(
        "/requests/new",
        data={"request_text": "here is my insurance card, second upload"},
        files={"document": ("insurance.pdf", b"same-insurance-bytes", "application/pdf")},
        follow_redirects=False,
    )
    assert second.status_code == 303

    first_run = db_session.get(WorkflowRun, first.headers["location"].rsplit("/", 1)[-1])
    count = (
        db_session.query(PatientDocument)
        .filter(PatientDocument.patient_id == first_run.patient_id)
        .count()
    )
    assert count == 1
```

- [ ] **Step 2: Run the two new tests**

This task adds tests only — no new production code. Everything they exercise (`app.agents.document`, `app.tools.document_tools`, the upload path in `app/routes/request_routes.py`) was already built in Tasks 1-9, executed strictly before this task.

Run: `pytest tests/test_request_routes.py -v -k "attached_file or same_document_bytes"`
Expected: PASS — both new tests green on the first run, since the underlying feature already exists. (If either fails, that means an earlier task's implementation doesn't actually satisfy this end-to-end path — treat it as a real regression to fix in the task that owns the broken piece, not as an expected red step to work around here.)

- [ ] **Step 3: Run the full file, then the full suite**

Run: `pytest tests/test_request_routes.py -v`
Expected: PASS — every test in the file, old and new.

Run: `pytest -v`
Expected: PASS — full green suite across all of `tests/`.

- [ ] **Step 4: Commit**

```bash
git add tests/test_request_routes.py
git commit -m "test: cover end-to-end file upload, real disk write, and duplicate-bytes dedup"
```

---

## Self-Review

**1. Spec coverage** (against `docs/superpowers/specs/2026-07-27-document-agent-design.md`):

- §1 Goal — Task 3-6 build ingest/classify/checksum/duplicate/missing-doc/patient-mapping end to end. Covered.
- §2 Scope (in-scope bullets) — upload field on existing form (Task 9), real tool logic (Tasks 3-5), subgraph + parent node (Task 6-7), `required_document_types` column (Task 1), status-page wording addition (Task 9, deliberately raw per the sequencing note). Covered.
- §2 Scope (explicitly out-of-scope bullets) — no OCR/content parsing anywhere in Tasks 3-6 (classification uses only filename + request text passed via the `HumanMessage` in `document_agent_node`); no document history browser page added; no staff review UI added. Covered by omission, as intended.
- §3 Guardrail (administrative not clinical) — `DOCUMENT_SYSTEM_PROMPT` (Task 6) explicitly forbids reading file contents or medical interpretation; `_missing_required_documents`/tool summaries only ever say "missing document types," never diagnostic language. Covered.
- §4 Data model changes — Task 1 adds exactly the specified column, migration, and seed values (`["ecg"]` / `[]`); confirms `PatientDocument` needs no changes (none made). Covered.
- §5 Upload path — Task 8, exact save-path shape (`storage_dir/<patient_id>/<uuid4hex>_<filename>`), passes `uploaded_files=[saved_path]`. Covered.
- §5 `document_tools.py` — Tasks 3-5 implement `_checksum_file`, `_missing_required_documents`, `store_and_classify_document` (with the audited decorator and the exact duplicate/new-row dict shapes), and `store_and_classify_document_tool` with a real-content summary. Covered.
- §5 `app/agents/document.py` — Task 6 implements the named `DocumentState` TypedDict, the capture-node pattern (not routing's text-parse pattern), and the no-op short-circuit. Covered.
- §5 Graph change — Task 7, with the explicit deviation from the spec's own pseudocode called out up front in "Sequencing note" (unconditional edges, not `route_after_coordinator`, which doesn't exist yet). Covered, deviation documented.
- §5 Wording — explicitly deferred; Task 9 adds only a raw `document_ids` line, matching the orchestrator's override of this spec section (the `_render_patient_message` helper it describes does not exist in the current codebase and is out of scope for this plan). Documented as a judgment call below.
- §6 Error handling — unreadable/empty file (Task 4's tests), no-appointments case returns `[]` not an error (Task 3's test), cross-patient checksum collision is not a duplicate (Task 4's test). Covered.
- §7 Testing — every named bullet has a task-3/4/5/6/10 test: checksum+duplicate-by-content (Task 4), cross-patient own-row (Task 4), missing-doc gap + cancelled-appointment regression + rescheduled-appointment regression (Task 3 — the last one added post-review, after a second cross-check round caught that `rescheduled` isn't an inactive status; see `docs/memory/gotchas.md`), tool content real-status assertion mirroring `test_slots_summary_...` (Task 5), document_agent_node tool-called-once + document_ids populated + zero-LLM-calls no-op (Task 6), route-level real-file-on-disk + real-row + duplicate-bytes-no-second-row (Task 10). Covered.
- §8 Open items — `DocumentState` named TypedDict (Task 6), `_missing_required_documents` as a plain shared function not a tool (Tasks 3-4, never `@tool`-wrapped), cancelled-appointment filter fix baked into the very first implementation (Task 3), no OCR dependency added (nowhere in this plan). Covered.

**2. Placeholder scan:** No "TBD"/"implement later"/"handle edge cases" language anywhere in the tasks above; every step shows complete, runnable code (or, for Task 9 Step 3 and Task 1 Step 6, an explicit manual-verification instruction rather than a vague placeholder, since those two steps have no automatable assertion available in this repo's current test setup). No task says "similar to Task N" without repeating the actual code.

**3. Type/signature consistency check:**
- `store_and_classify_document(db, patient_id, file_path, document_type) -> dict` — same signature used in Task 4 (definition), Task 5 (`store_and_classify_document_tool` calls it the same way), spec §5 (matches).
- `_missing_required_documents(db, patient_id) -> list[str]` — defined Task 3, called identically in Task 4 (twice, `saved`/`duplicate` branches).
- Result dict keys — `id`, `status`, `document_type`, `missing_document_types` (success cases) vs. `id`, `status`, `error` (error case) — used consistently across Task 4's tests, Task 5's `_document_summary`, and Task 6's `document_agent_node` (`document_result.get("id")`, guarded against `None`).
- `DocumentState` fields (`messages`, `patient_id`, `file_path`, `document_result`) — matches exactly between the TypedDict definition and every node function's `state[...]`/`state.get(...)` access in Task 6.
- `WorkflowState.document_ids` — already exists in `app/agents/state.py` (read during research, unchanged by this plan); `document_agent_node`'s return `{"document_ids": document_ids}` matches its type (`list[str]`).
- Tool/message names — `store_and_classify_document_tool` used identically as the `ToolMessage.name` check in `document_capture_node`, the `@tool`-decorated function name, and every test's `ai_message_with_tool_call("store_and_classify_document_tool", ...)` call.

**One judgment call made, flagged here explicitly:** the spec's §5 "Wording" section assumes a `_render_patient_message` function that doesn't exist yet in this codebase (it's part of the separate, later intent-branching plan). Per the orchestrator's explicit instruction, this plan does not build it — Task 9 adds only the raw `document_ids` field to the existing plain-field-dump status page, called out in-template and in this plan as temporary.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-27-document-agent.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
