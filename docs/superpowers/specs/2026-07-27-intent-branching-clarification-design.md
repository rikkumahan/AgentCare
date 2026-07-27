# Intent Branching + Patient Clarification + Booking Confirmation — Design Spec

Status: approved by user through conversational review (trigger, click
handling, popup-vs-page, wording-only scope, and the confirm-before-booking
addition all confirmed one decision at a time).

Source of truth for rules/scope: `problem_statement.md`, `CLAUDE.md`. Builds
on `docs/superpowers/specs/2026-07-25-request-routes-design.md` (the
`/requests/new` and `/requests/{id}` routes this spec extends) and
`docs/superpowers/specs/2026-07-22-agentcare-design.md` (original graph
shape).

## 1. Goal

Right now, every request runs through the exact same path regardless of what
the Coordinator agent decides the patient actually wants, and the Appointment
agent books a slot the moment it finds one, with no pause. This spec adds
**two** stopping points where the patient gets a say, both presented the same
way — two buttons on the status page they already land on, worded like a
receptionist asking a quick question, not a technical status dump:

1. **Intent is unclear** — the request isn't a confident "book an
   appointment," so the system stops and asks what to do instead of guessing
   or silently escalating.
2. **A candidate slot was found** — right before actually committing a
   booking, the system shows what it's about to do and waits for a yes,
   instead of booking immediately.

Both reuse the same mechanism: stop the graph, mark the run with a status,
show two buttons, act deterministically on whichever one is clicked.

## 2. Scope

**In scope:**
- Parent graph branches after `coordinator_agent`: intent `book_appointment`
  (tolerant match) continues to `routing_agent`; anything else stops the run
  as `needs_clarification`.
- Appointment agent stops after proposing **one** candidate slot instead of
  booking it immediately; the run stops as `needs_booking_confirmation`.
- Two new `WorkflowStatus` values: `needs_clarification`,
  `needs_booking_confirmation` (one migration).
- `request_status.html` shows two buttons for either status, with wording
  specific to which one it is.
- Two new routes, `POST /requests/{id}/clarify` and
  `POST /requests/{id}/confirm-booking`, each reading which button was
  clicked and acting on it directly and deterministically (no LLM call for
  either commit action).
- Human-readable wording on the status page throughout, built from real rows
  (patient's name, doctor name, department, appointment time), replacing raw
  field dumps.

**Explicitly out of scope (confirmed with user):**
- A true floating/overlay popup (dimmed background, JS modal) — a styled
  inline box on the existing page. Small CSS-only upgrade later if wanted;
  not scored per `problem_statement.md` line 211.
- Multi-step guided booking (ask department, then time, as separate
  back-and-forth steps) — this is the larger "Option B" conversational
  redesign, explicitly not being built. This spec adds exactly one pause
  point before booking, not a step-by-step dialogue.
- `submit_document` and reschedule/cancel as their own clarification-popup
  options — those intents still fall into the "talk to staff" bucket for now.
  Document upload itself (attached to the request form, not through this
  popup) is covered in the separate Document agent spec.
- Any change to Safety or Coordinator's own prompts/tools.

## 3. Architecture

### Graph change (`app/graph.py`)

```python
def route_after_coordinator(state: WorkflowState) -> Literal["routing_agent", "needs_clarification"]:
    intent = (state.get("intent") or "").strip().lower()
    if intent == "book_appointment" or "book" in intent:
        return "routing_agent"
    return "needs_clarification"


def needs_clarification_node(state: WorkflowState, config) -> dict:
    return {"needs_clarification": True}
```

`coordinator_agent` → (conditional) → `routing_agent` | `needs_clarification`
→ `END`. `routing_agent` → (existing conditional on escalation) →
`appointment_agent` → `END`, structurally unchanged — only what happens
*inside* `appointment_agent` changes (below).

`WorkflowState` gains: `needs_clarification: bool`, `candidate_slot_id: str |
None` (mirrors the existing `escalation` field's pattern — a deterministic
flag/value the graph sets, not a judgment call baked into routing logic).

### Appointment agent stops before booking (`app/agents/appointment.py`)

Today, `APPOINTMENT_SYSTEM_PROMPT` tells the model to call
`book_or_modify_appointment_tool` itself once it picks a slot. That
instruction is removed. Instead:

- The model calls `check_slot_availability_tool` (unchanged), picks **one**
  slot it thinks fits, and replies with **only that slot's `slot_id`** as
  plain text — it never calls `book_or_modify_appointment_tool` at all
  anymore. That tool is removed from this agent's tool list entirely; the
  model has no way to book directly, only to propose.
- A new `appointment_finalize_node` (mirroring `routing_finalize_node`'s
  shape) reads the model's final text and **validates it against the real
  slot list already returned by `check_slot_availability_tool` this
  conversation** (captured via the tool call's artifact, same
  content-vs-artifact pattern as everywhere else) — never trusting an
  LLM-transcribed id blindly, consistent with `_parse_uuid` validation
  elsewhere. If the text doesn't match any real slot id from this
  conversation, treat it as "no confident candidate" and fall through to the
  existing "no slots available" reply path instead of booking or
  hallucinating.
- `appointment_agent_node` returns `{"candidate_slot_id": ...}` (or `None` if
  no candidate) instead of `{"appointment_id": ...}`. No `appointment_id` is
  ever set by the automatic path anymore — only by the new confirm route
  below. This also fully removes the class of bug where the model kept
  calling the booking tool repeatedly after success, since the model can
  never call it at all now.

### Workflow runner (`app/workflow_runner.py`)

Status resolution after the stream gains one more branch:

```python
if full_state.get("escalation"):
    workflow_run.status = WorkflowStatus.needs_review
elif full_state.get("needs_clarification"):
    workflow_run.status = WorkflowStatus.needs_clarification
elif full_state.get("candidate_slot_id"):
    workflow_run.status = WorkflowStatus.needs_booking_confirmation
else:
    workflow_run.status = WorkflowStatus.completed
```

Note: this drops the old `current_step = "document_agent"` placeholder (it
stood in for "the next unbuilt phase" before any of Phases 4-5 existed).
The Document agent spec wires a real `document_agent` node into the graph
itself, so by the time this final `else` is reached there's genuinely
nothing automatic left to do — `WorkflowStatus.completed` (defined in
`app/models.py` since Phase 1, never actually used until now) is the
honest terminal state. `current_step` is left as whatever the stream loop
already set it to (the real last node that ran), not overwritten.

Three new functions, called only from the two new routes, never from
`run_workflow` itself:

```python
def continue_as_booking(db, workflow_run) -> WorkflowRun:
    """needs_clarification -> patient chose 'book an appointment'. Runs
    routing_agent_node then appointment_agent_node directly (plain sequential
    calls, no second compiled graph - see prior self-review note), landing on
    either needs_booking_confirmation, needs_review, or running exactly like
    a normal run would."""

def commit_confirmed_booking(db, workflow_run) -> WorkflowRun:
    """needs_booking_confirmation -> patient clicked 'yes, book it'. Calls
    book_or_modify_appointment (the plain function, NOT the LLM tool) directly
    with the stored candidate_slot_id and action='book'. Deterministic, no
    LLM involved in the commit itself - the model already did its one job
    (proposing a slot); the human's click is what authorizes the write."""

def continue_as_staff_escalation(db, workflow_run, reason: str) -> WorkflowRun:
    """Used by both 'talk to staff' buttons (from needs_clarification and
    from needs_booking_confirmation's decline option). Calls create_escalation
    directly - no LLM call, the patient's click is itself the deterministic
    signal."""
```

All three update `workflow_run.state`/`status`/`current_step` the same way
`run_workflow`'s loop already does.

### Routes (`app/routes/request_routes.py`)

```python
POST /requests/{id}/clarify
  choice=book_appointment -> continue_as_booking(db, workflow_run)
  choice=staff            -> continue_as_staff_escalation(db, workflow_run, f"Patient asked for help with an unclear request: {request_text!r}")

POST /requests/{id}/confirm-booking
  choice=confirm -> commit_confirmed_booking(db, workflow_run)
  choice=staff   -> continue_as_staff_escalation(db, workflow_run, f"Patient declined the proposed slot for: {request_text!r}")
```

Both routes share the same guard shape as the existing status route:
`require_role("patient")`, ownership check, 404/403 as appropriate, and a
no-op redirect if the run's status no longer matches what the button expects
(stale double-click protection, same philosophy as the existing
duplicate-submission guard).

### Wording (`app/routes/request_routes.py`)

A helper, `_render_patient_message(db, user, workflow_run) -> str`, used by
`GET /requests/{id}`:

- `needs_clarification` → `"Hi {name}! I want to make sure I help you with
  the right thing."` (buttons: **Book an appointment** / **Talk to hospital
  staff**)
- `needs_booking_confirmation` → looks up the candidate slot's doctor,
  department, and time, renders `"I found an opening with {doctor} in
  {department} on {formatted_time}. Should I book it?"` (buttons: **Yes,
  book it** / **No, talk to staff instead**)
- `completed` with `appointment_id` set → `"Great news, {name}! You're
  booked with {doctor} in {department} on {formatted_time}."`
- `completed` with no `appointment_id` (intent was booking but
  `check_slot_availability` found nothing) → `"I couldn't find any open
  slots right now. Please try again later or contact our staff."`
- `needs_review` → `"I've passed your request to our staff team - they'll
  follow up with you soon."` (internal reason stays in the DB/audit trail,
  not shown to the patient)
- `failed` → `"Something went wrong on our end while handling your request.
  Please try again, or contact our staff directly."`

The Document agent spec appends one more clause to any of the above when
`document_ids` is non-empty (e.g. `"I've also saved your insurance card."`)
— see that spec's wording section.

Plain Python, not an LLM call — same principle as "final confirmation
rendered from real rows, never a free-standing model string."

## 4. Data flow

1. Patient submits free text at `/requests/new`.
2. Safety → Coordinator run as today.
3. Intent doesn't look like booking → ends `needs_clarification`.
   Intent looks like booking → Routing (unchanged) → Appointment agent
   proposes a candidate slot (no booking yet) → ends
   `needs_booking_confirmation` (or `needs_review` if Routing itself
   couldn't match a department, or "no slots available" if none were found
   — both unchanged existing paths).
4. Patient sees the matching human sentence and buttons on `/requests/{id}`.
5. Click routes to `/clarify` or `/confirm-booking`, which runs the matching
   deterministic continuation, updates the same `WorkflowRun`, and redirects
   back to the same status page showing the new real outcome.

## 5. Error handling

- Same `require_role`/ownership/404 pattern as the existing status route.
- A stale click (run no longer in the expected status) is a no-op redirect,
  not an error.
- `commit_confirmed_booking` re-validates the slot is still open before
  booking (the existing `book_or_modify_appointment` function already checks
  `slot.status != SlotStatus.open` and returns a structured error) — a slot
  could theoretically be taken by someone else between proposal and
  confirmation; that error path already exists and needs no new code, just a
  wording branch for "that slot's gone" if it happens.

## 6. Testing

- Graph routing: `book_appointment`/`"booking"` → `routing_agent`; anything
  else → `needs_clarification`.
- Appointment agent: check_slot_availability returns slots → model proposes
  one → `candidate_slot_id` set, **no** `Appointment` row created yet, **no**
  `book_or_modify_appointment_tool` call happens at all (assert on the fake
  model's call log). A proposed id that doesn't match any real slot from this
  conversation → treated as no-candidate, not booked.
- `workflow_runner`: full run ending at each terminal status —
  `needs_clarification`, `needs_booking_confirmation`, `completed` (both
  with and without a set `appointment_id`), and the unchanged `needs_review`.
- Routes: both new endpoints, happy path + wrong-owner (403) + nonexistent
  (404) + stale-status no-op, mirroring `test_resubmitting_the_same_request…`
  patterns already in `tests/test_request_routes.py`.
- Regression: confirms the old triple-booking scenario is now structurally
  impossible (the tool the model would have looped on no longer exists in
  its tool list).

## 7. Open items resolved during self-review

- Confirmed no second compiled LangGraph subgraph is needed for
  `continue_as_booking` — plain sequential function calls with an early-exit
  check on `escalation`/`candidate_slot_id` do the job.
- Confirmed `book_or_modify_appointment_tool` is fully removed from the
  Appointment agent's bound tools (not just unused) — leaving it bound but
  unused would still let a sufficiently determined model call it, defeating
  the point.
- Confirmed the patient-facing wording never exposes raw escalation reasons
  or UUIDs — those stay available to staff via the DB/audit trail.
- Confirmed this spec does not change `check_slot_availability_tool` or
  `book_or_modify_appointment`/`_tool` themselves — only what calls them and
  when.
