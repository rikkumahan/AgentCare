# Follow-up Agent + Staff Views — Design Spec

Status: approved by user through conversational review (staff-triggered scan
shape confirmed as the primary design, over an automatic-after-booking
alternative).

Source of truth: `problem_statement.md` (line 207: "a staff user can view
requests and act on escalations or approvals" — currently **entirely
unbuilt**, `staff_dashboard.html` is a bare placeholder today), `CLAUDE.md`
(Follow-up agent row: "Appointment reminders, post-visit tasks, and scans for
missed/incomplete workflows" — tools `create_reminder`, `scan_incomplete_workflows`).

## 1. Goal

Build the fifth (final required) agent, and — since it's the natural place
for it and the app has no staff-facing views at all yet — close the gap
between what `problem_statement.md` requires ("staff can view requests and
act on escalations") and what currently exists (a blank staff dashboard).

## 2. Scope

**In scope:**
- `scan_incomplete_workflows` tool: real DB sweep across all patients,
  finding (a) confirmed appointments without a reminder yet, and (b) patients
  missing a required document for one of their departments (reusing
  `_missing_required_documents` from the Document agent spec).
- `create_reminder` tool: real DB insert, called by the scan for each gap it
  finds — not called speculatively or for gaps that already have a pending
  reminder (no duplicate reminders on repeated scans).
- `follow_up_agent_node` + its own private subgraph, invoked only by a new
  staff-triggered route, **not** wired into the per-request parent graph
  (this agent runs as an on-demand sweep across everything, not a step in a
  single patient's workflow — confirmed shape).
- Staff dashboard gets real content: a "Run follow-up scan" button, a list of
  open escalations with an approve/dismiss action, and a list of reminders
  created so far. This is the first real staff-facing functionality in the
  app.

**Explicitly out of scope (confirmed pattern from other specs — small
follow-up if time allows, not built now):**
- Automatic reminder creation the instant a booking succeeds (the
  alternative shape considered and not chosen).
- Actually *sending* reminders (SMS/email) — per `CLAUDE.md`, delivery is
  simulated (log + in-app), already the documented design; this spec doesn't
  change that.
- Any patient-facing reminder UI (e.g., "your appointment is coming up"
  banner) — reminders are staff-visible records for this pass; a patient-side
  view is Phase 6 polish.

## 3. Data model

`Escalation` already has every column this needs. `Reminder` gains one small
column:

```python
# Reminder gains:
note: Mapped[str | None] = mapped_column(String(200), nullable=True)
```

One Alembic migration. **Why this is needed** (found during user
cross-check, not in the original draft): a `missing_document` reminder needs
to say *which* document type it's about, both so staff can read it (the
dashboard table shows this) and — more importantly — so dedup can be scoped
per document type, not just per patient. Without `note`, a patient missing
two different required document types across two departments would only
ever get *one* `missing_document` reminder, ever (the naive "does this
patient already have a pending missing_document reminder" check would treat
the first gap's reminder as covering the second gap too, which it doesn't).
With `note` storing the document type (e.g. `"ecg"`), the scan's dedup check
becomes "does this patient already have a pending `missing_document`
reminder with this specific `note`" — one reminder per distinct gap, correctly.
`appointment` reminders don't need this — they already dedup correctly via
`appointment_id`, which is specific enough on its own.

## 4. Components

### `app/tools/followup_tools.py` (new)

```python
@audited("create_reminder", "Reminder")
def create_reminder(db: Session, patient_id: str, reminder_type: str, scheduled_at: str, appointment_id: str | None, note: str | None = None) -> dict:
    """Plain insert. scheduled_at is an ISO datetime string, parsed and
    stored. Real write - no fixed/fake response regardless of input."""

@audited("scan_incomplete_workflows", "WorkflowRun")
def scan_incomplete_workflows(db: Session) -> dict:
    """Two real queries, not one fixed response:
    1. Confirmed appointments with a future start_time
       (status=confirmed AND AppointmentSlot.start_time > now() - an old
       confirmed appointment with a start_time in the past shouldn't get a
       reminder scheduled in the past too, harmless as that is since
       delivery is simulated, but worth being deliberate about) and no
       existing Reminder(reminder_type=appointment, appointment_id=this one)
       -> creates one, scheduled_at = appointment's start_time minus 24h.
    2. For every patient with at least one appointment, calls
       _missing_required_documents(db, patient_id) (already filters to
       pending/confirmed appointments - see Document agent spec); for each
       missing document_type with no existing pending
       Reminder(reminder_type=missing_document, note=that document_type)
       for that patient -> creates one (note=document_type), scheduled_at =
       now. Scoping dedup by note (not just patient) means two different
       missing types both get their own reminder - see Data model section
       for why this matters.
    Returns real counts + the actual rows created, not a canned summary."""
```

```python
@tool(response_format="content_and_artifact")
def scan_incomplete_workflows_tool(config: RunnableConfig):
    """Sweep all patients and appointments for missing reminders or missing
    required documents, creating reminder records for any gaps found."""

@tool(response_format="content_and_artifact")
def create_reminder_tool(
    patient_id: str, reminder_type: str, scheduled_at: str, appointment_id: str | None,
    config: RunnableConfig,
):
    """Create one reminder record directly. Normally called by the scan
    tool's own logic, exposed as a tool so the agent's LLM step can also
    create an ad-hoc reminder if asked to during a scan review."""
```

Why this agent gets an LLM step at all, given the sweep itself is
deterministic: the scan tool does the real, deterministic finding-and-fixing.
The agent's LLM node calls it, then **summarizes the results in a short
staff-facing sentence** ("Found 3 upcoming appointments needing a reminder
and 1 patient missing a required document — all handled.") — genuinely
agentic (a real tool-using step whose output staff reads), not a rubber-stamp
wrapper, while keeping the actual database logic 100% deterministic and
auditable.

### `app/agents/followup.py` (new)

Same shape as other agents: one system prompt, `scan_incomplete_workflows_tool`
+ `create_reminder_tool` bound, a subgraph with LLM/tool/finalize nodes. Not
added to `app/graph.py`'s parent graph — invoked directly by its own route.

### Route (`app/routes/staff_routes.py`, new file)

```python
GET  /staff/dashboard                        -> extends existing route: now also
                                                 lists open Escalations and recent
                                                 Reminders (real queries)
POST /staff/scan                             -> require_role("staff"), invokes
                                                 follow_up_agent_node once,
                                                 redirects back to /staff/dashboard
POST /staff/escalations/{id}/resolve         -> require_role("staff"), body:
                                                 decision=approved|rejected,
                                                 updates Escalation.status and
                                                 reviewed_by=current staff user's id,
                                                 redirects back to /staff/dashboard
```

This is the piece that satisfies `problem_statement.md` line 207's staff
requirement ("view requests and act on escalations or approvals") for the
first time — currently nothing lets staff see or act on an `Escalation` row
at all.

### Staff dashboard template (`app/templates/staff_dashboard.html`)

Extends the current bare page with:
- A "Run follow-up scan" button (`POST /staff/scan`), and the last scan's
  summary sentence shown above it once one has run.
- A plain table of open escalations (reason, created date) each with two
  buttons, Approve / Reject, posting to `/staff/escalations/{id}/resolve`.
- A plain table of reminders (patient name, type, `note` if set, scheduled
  time, status) — `note` is what lets staff see *which* document is missing
  for a `missing_document` row, not just that one exists.

Unstyled beyond `base.html`, matching every other page in the app — real,
functional, not polished (confirmed in-scope-but-basic per this session's UI
decision).

## 5. Data flow

1. Staff logs in, visits `/staff/dashboard` — sees real open escalations and
   reminders pulled fresh from the DB.
2. Staff clicks "Run follow-up scan" → `POST /staff/scan` → the Follow-up
   subgraph runs once, its tool sweeps the whole DB, creates whatever
   `Reminder` rows are missing, returns a summary sentence → redirect back to
   the dashboard, which now shows the new reminders in its table.
3. Staff clicks Approve/Reject on an escalation → updates that row directly
   (no LLM involved — a staff decision is not something to classify) →
   redirect back, escalation moves out of the "open" list.

## 6. Error handling

- `require_role("staff")` on every new route — patients get 403, matching
  every existing RBAC pattern in the app.
- Resolving a nonexistent/already-resolved escalation id → 404 / no-op
  redirect (same "stale click is harmless" philosophy as the patient-side
  routes).
- Scan running with zero gaps found → summary sentence says so explicitly
  ("Nothing needed attention this time") — not a silent no-op, so staff can
  tell the scan actually ran.

## 7. Testing

- `tests/test_followup_tools.py`: `scan_incomplete_workflows` creates exactly
  one reminder per gap (running it twice in a row creates zero *new* rows the
  second time — the core "not a fixed response, not duplicated" proof);
  covers both reminder types (appointment, missing_document); a patient
  missing **two** distinct document types (e.g. Cardiology's ECG and a
  second department's own requirement) gets **two** separate
  `missing_document` reminders, each with a different `note` (regression
  test for the per-type dedup fix); a confirmed appointment with a
  `start_time` in the past does not get a reminder created.
- `tests/test_followup_agent.py`: mocked model summarizes a scan result;
  tool called exactly once per invocation.
- `tests/test_staff_routes.py`: patient gets 403 on all three routes; staff
  sees real escalations/reminders after seeding some; `/staff/scan` produces
  real new `Reminder` rows; resolving an escalation updates
  `status`/`reviewed_by` for real and it disappears from the "open" list.

## 8. Open items resolved during self-review

- Confirmed Follow-up is intentionally **not** a parent-graph node — it's an
  on-demand, cross-patient sweep, structurally different from the
  per-request agents, matching the "scans for..." wording in `CLAUDE.md`
  rather than forcing it into the single-request graph shape.
- Confirmed no duplicate reminders on repeated scans (checked via the
  "already has a pending Reminder for this gap" query before inserting).
- **Found during user cross-check (both fixed above):** the original draft
  deduped `missing_document` reminders per-patient only, so a patient with
  two separate document gaps would only ever get one such reminder, ever —
  fixed by adding `Reminder.note` and scoping dedup per `(patient_id,
  reminder_type, note)`. The original draft also had no lower bound on
  `start_time` for appointment reminders, so a stale confirmed appointment
  could get a reminder scheduled in the past — fixed with an explicit
  future-only filter, called out rather than left implicit.
- Confirmed escalation approve/reject is a plain staff decision, not routed
  through any LLM — consistent with every other place in this session where
  a deterministic human action shouldn't be second-guessed by a model.
- Confirmed this closes the previously-unbuilt staff-facing minimum
  requirement from `problem_statement.md` line 207, not just the Follow-up
  agent itself.
