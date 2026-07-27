# Document Agent — Design Spec

Status: approved by user through conversational review (upload location,
classification approach, and the department-tied missing-document rule all
confirmed one decision at a time).

Source of truth: `problem_statement.md`, `CLAUDE.md` (Document agent row:
"Ingest, classify, checksum, duplicate/missing-doc detection, map to
patient" — tool `store_and_classify_document`). Builds on
`docs/superpowers/specs/2026-07-22-agentcare-design.md` §5.5 and this
session's `docs/superpowers/specs/2026-07-27-intent-branching-clarification-design.md`
(the graph this agent's node is added to).

## 1. Goal

Build the fourth of six required agents: ingest a file the patient attaches
to their request, classify what kind of document it is, detect duplicates by
content (not filename), and flag when a patient is missing a document their
upcoming appointment's department requires. This is currently 100% unbuilt —
no upload path, no agent, no tool exist yet.

## 2. Scope

**In scope:**
- Optional file field added to the existing `/requests/new` form (confirmed:
  attach to the existing form, not a separate upload page).
- `store_and_classify_document` tool: real filesystem write, real checksum,
  real duplicate check, real DB write, real missing-document check.
- `document_agent_node`, its own private subgraph (1 LLM node deciding the
  document type + 1 tool call), added to the parent graph.
- `Department.required_document_types` column (new, seeded) — required docs
  are tied to department, not universal (confirmed with user, despite the
  extra setup this needs — see Guardrail below for how this stays
  administrative, not clinical).
- Wording additions for document-related outcomes on the status page.

**Explicitly out of scope:**
- Reading a file's actual contents (OCR, PDF text extraction) — classification
  uses only the filename and the patient's own note text (the `request_text`
  they typed alongside the attachment). Real file-content parsing is a
  meaningfully bigger feature and isn't needed to satisfy "classify" here.
- A patient-facing list/browser of their own previously uploaded documents —
  UI stays limited to what the status page shows about *this* request's
  attachment. A full document history view is Phase 6 polish territory.
- Staff review/approval of documents — staff's escalation/reminder views
  (built in the Follow-up agent spec) are the staff-facing surface for now.

## 3. Guardrail: why department-tied is still administrative, not clinical

`CLAUDE.md` prohibits anything that reads as a medical judgment. "Cardiology
requires an ECG on file" is phrased and used purely as a **paperwork
completeness check** — the same way a real front-desk checklist works
("please bring your ECG results for your cardiology visit"). The system
never interprets the ECG's *contents*, never says a patient *needs* an ECG
*performed*, and never blocks or reschedules anything based on it — it only
flags, in administrative language, that a specific piece of paperwork isn't
on file yet. All wording is framed as "please upload X," never "you require
X."

## 4. Data model changes

```python
# Department gains:
required_document_types: Mapped[list[str]] = mapped_column(JSON, default=list)
```

One Alembic migration. Seed data update (`seed/seed_data.py`):
`cardiology.required_document_types = ["ecg"]`, `general.required_document_types = []`
(General Medicine has no standing paperwork requirement — illustrates the
rule is genuinely per-department, not just a relabeled universal list).

No changes to `PatientDocument` — its existing columns (`document_type`,
`file_path`, `checksum`, `document_date`) already cover everything this
agent needs to write.

## 5. Components

### Upload path (`app/routes/request_routes.py`)

`POST /requests/new` gains an optional `document: UploadFile | None = File(None)`
parameter alongside the existing `request_text: str = Form(...)`. If present:
save it to `./storage/<patient_id>/<uuid4_hex>_<original_filename>` (the
random prefix avoids collisions between patients or repeated uploads of a
same-named file — it is not a security boundary, just a naming one, since
the directory is already scoped per patient). Pass
`uploaded_files=[saved_path]` into `run_workflow` (today always `[]`, now
populated when a file is attached).

### `app/tools/document_tools.py` (new)

```python
@audited("store_and_classify_document", "PatientDocument")
def store_and_classify_document(db: Session, patient_id: str, file_path: str, document_type: str) -> dict:
    """Computes a SHA-256 checksum of the file's real bytes. If a
    PatientDocument with the same (patient_id, checksum) already exists,
    returns {"status": "duplicate", ...} and does NOT insert a second row
    (the model, more than once this session, has needed a real reason
    text rather than a bare status word - same content-vs-artifact lesson
    applies here). Otherwise inserts a new PatientDocument row and runs
    _missing_required_documents(db, patient_id) to report any remaining
    gaps against the patient's appointment departments' required lists."""

def _missing_required_documents(db: Session, patient_id: str) -> list[str]:
    """Real query: patient's appointments (filtered to
    status.in_([AppointmentStatus.pending, AppointmentStatus.confirmed]) -
    a cancelled appointment's department shouldn't keep flagging the patient
    for paperwork tied to a visit that isn't happening) -> distinct
    departments -> union of required_document_types, minus the
    document_types the patient already has on file. Shared by this tool and
    the Follow-up agent's scan (same gap, two different callers/contexts) -
    not duplicated logic."""
```

```python
@tool(response_format="content_and_artifact")
def store_and_classify_document_tool(
    file_path: str,
    document_type: str,
    patient_id: Annotated[str, InjectedState("patient_id")],
    config: RunnableConfig,
):
    """Save and classify a document the patient attached. document_type must
    be one of: ecg, lab_report, prescription_old, insurance, id_proof, other.
    Pick the best fit based on the filename and any note the patient wrote."""
```

`content` lists the real outcome (new/duplicate + any missing document
types by name) — not a bare "success", continuing this session's established
content-vs-artifact fix pattern.

### `app/agents/document.py` (new)

Mirrors the existing agent shape (Coordinator/Routing/Appointment), including
a fully-named `TypedDict`, matching every other agent's subgraph state
(previously only described in prose here — named explicitly now):

```python
class DocumentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    patient_id: str
    file_path: str
    document_result: dict | None
```

`DOCUMENT_SYSTEM_PROMPT` tells the model to look at the filename (and the
patient's own request text as context/note) and call
`store_and_classify_document_tool` with its best-fit `document_type`, then
reply with a short confirmation. One tool, one expected call, same
loop-back-until-final-text shape as the other agents for consistency.
`document_finalize_node` captures the tool's `artifact` into
`document_result` (the same content-vs-artifact capture pattern as
`coordinator_capture_node`/`appointment_capture_node`).

`document_agent_node(state, config)`:
- If `state["uploaded_files"]` is empty, returns `{}` immediately — no LLM
  call, no-op. (This keeps the parent graph edge unconditional — see below —
  while making "no file attached" the overwhelmingly common case cheap and
  side-effect-free.)
- Otherwise invokes the subgraph once per attached file (today, always
  exactly one), returns `{"document_ids": [...]}`.

### Graph change (`app/graph.py`)

```python
graph.add_edge("coordinator_agent", "document_agent")
graph.add_conditional_edges("document_agent", route_after_coordinator, {...})
```

`document_agent` runs unconditionally right after `coordinator_agent`,
**before** the intent branch — so an attached file always gets processed
regardless of whatever else the request's text is about (a patient could
type "book a cardiology appointment" and attach their insurance card in the
same submission; both happen). This matches the original design spec's
gating (`uploaded_files` non-empty), just moved earlier in the sequence so it
can never get skipped by an early `needs_clarification`/
`needs_booking_confirmation` exit.

### Wording (`app/routes/request_routes.py`)

Extends `_render_patient_message` with a document-specific clause, appended
when `document_ids` is non-empty: `"I've saved your {document_type}."`, plus,
if `_missing_required_documents` found gaps, `" Before your {department}
appointment, please also upload: {missing list}."` If the upload was a
duplicate: `"I already had that one on file — no need to upload it again."`

## 6. Error handling

- Unreadable/empty upload → tool returns a structured error (real
  `os.path`/file-size check), surfaced in `content` the same way
  `book_or_modify_appointment_tool`'s errors are today.
- No matching department context yet (patient has no appointments) →
  `_missing_required_documents` returns `[]` — nothing to flag, not an error.
- Checksum collision across two *different* patients is not a duplicate
  (uniqueness is scoped `(patient_id, checksum)`, matching the existing
  `PatientDocument.__table_args__` unique constraint already in
  `app/models.py` — no schema change needed there).

## 7. Testing

- `tests/test_document_tools.py`: real checksum computed from real file
  bytes; duplicate detection (same patient, same bytes, different filename)
  returns `status=duplicate` and does not insert a second row; different
  patients uploading identical bytes both get their own row;
  `_missing_required_documents` returns the right gap for a patient with a
  Cardiology appointment and no ECG on file, and `[]` for a patient with no
  appointments **or only a cancelled one** (regression test for the
  cancelled-appointment filter below).
- `tests/test_document_agent.py`: mocked model picks a `document_type`,
  tool gets called once, `document_ids` populated; a request with no
  attachment produces zero LLM calls (asserts the no-op short-circuit).
- `tests/test_request_routes.py`: submitting a request with an attached file
  results in a real file on disk under `./storage/<patient_id>/` and a real
  `PatientDocument` row; submitting the same bytes twice for the same
  patient does not create a second row.

## 8. Open items resolved during self-review

- Confirmed classification is the model's judgment call (filename + note),
  while checksum/dedup/missing-doc detection are deterministic code inside
  the tool — same division of labor already established for Routing
  (model matches, code resolves to a real id) and Appointment (model
  proposes, code executes).
- Confirmed `_missing_required_documents` is a plain shared function, not a
  tool itself, callable by both this agent's tool and the Follow-up agent's
  scan without either agent calling the other's tools (keeps the "each
  agent has its own tool set" distinctness rule intact).
- **Found during user cross-check:** `_missing_required_documents` originally
  had no `Appointment.status` filter, so a *cancelled* appointment's
  department would still flag the patient as missing that department's
  paperwork. Fixed inline in the tool's docstring above — the query now
  scopes to `pending`/`confirmed` appointments only.
- `DocumentState` is now a fully-named `TypedDict` (was previously only
  described in prose), matching `CoordinatorState`/`RoutingState`/
  `AppointmentState`'s existing pattern.
- Confirmed no OCR/content-parsing dependency is introduced — filename+note
  classification is sufficient for "classify" as CLAUDE.md describes it, and
  adding real parsing is flagged as a Phase 6+ enhancement, not required now.
