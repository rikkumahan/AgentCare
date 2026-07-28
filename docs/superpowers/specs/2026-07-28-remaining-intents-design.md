# Remember and Re-Offer the Second Intent — Design Spec

Status: approved by user through conversational review ("option1" — remember
and re-offer, confirmed directly).

Source of truth: builds directly on
`docs/superpowers/specs/2026-07-28-multi-intent-detection-design.md`
(already implemented, committed). That spec explicitly scoped out sequential
handling: the patient picks which of several detected intents to handle
first, and the other(s) are simply never revisited — confirmed live and
flagged by the user as a real gap ("it asked me which one first but, forgot
the second one!"). This spec closes that gap.

## 1. Goal

When `needs_intent_selection` detects multiple intents and the patient picks
one, remember the other(s). Once the chosen action reaches a real terminal
`completed` state, don't stop there — check for a leftover intent and, if
one exists, land back on the same selection screen for it instead of ending
the workflow. The patient works through all originally-detected actionable
intents one at a time, in one continuous flow, instead of having to notice
the first one silently dropped and start a brand new request for the rest.

## 2. Scope

**In scope:**
- `WorkflowState` gains `remaining_intents: list[str]`.
- `continue_as_intent_selection` (existing, from the prior spec) computes
  the non-chosen actionable intents from the original comma list and stores
  them in `full_state["remaining_intents"]` before dispatching to the
  chosen action's existing continuation — no change to which continuation
  function gets called or how.
- One new shared helper, `_finalize_or_continue_intents(workflow_run,
  full_state)`, replacing every place in `workflow_runner.py` that currently
  sets `workflow_run.status = WorkflowStatus.completed` as a *terminal*
  outcome (not an intermediate step like `needs_slot_selection`): if
  `remaining_intents` is non-empty, pop the next one and land back on
  `needs_intent_selection` instead of `completed`; otherwise, `completed` as
  today. This also happens to collapse several near-identical
  status/state/commit blocks already flagged in an earlier ponytail pass —
  addressed here as a side effect of needing one real chokepoint for this
  feature, not as unrelated cleanup.
- `_render_patient_message` distinguishes the *first* multi-intent landing
  (`state["intent"]` still contains a comma — several actionable intents
  detected at once) from a *continuation* landing (`state["intent"]` is a
  single label — one intent left over from an earlier pick) with different
  wording, reusing the exact same template block and buttons either way.

**Explicitly out of scope:**
- Automatically executing the remaining intent without asking again — the
  patient still clicks to confirm each one; this only removes the "it got
  forgotten" problem, not the "click once per action" pattern already
  established everywhere else in this app.
- Any change to `continue_as_booking`, `continue_as_appointment_action`,
  `_land_on_slots_or_no_slots`'s core booking/cancel/reschedule logic, or
  the Coordinator prompt — all unchanged. This spec only changes what
  happens at the moment a `completed` status would otherwise be set.

## 3. Architecture

### `WorkflowState` (`app/agents/state.py`)

Add `remaining_intents: list[str]` (append below `rescheduling_appointment_id`).

### `continue_as_intent_selection` (`app/workflow_runner.py`)

Current implementation (from the prior spec) only dispatches to the chosen
action. Updated to first compute and store what's left over:

```python
def continue_as_intent_selection(db, workflow_run: WorkflowRun, chosen_intent: str) -> WorkflowRun:
    """needs_intent_selection -> patient picked which of the detected
    intents to handle first (or next, if this is a continuation after an
    earlier pick already completed - see _finalize_or_continue_intents).
    Stores whatever's left over in remaining_intents before dispatching, so
    the leftover gets re-offered instead of silently dropped once the
    chosen action finishes. Dispatches to whichever existing, unmodified
    continuation the single-intent graph path would have used for that
    label - no new booking/cancel/reschedule logic here."""
    chosen = chosen_intent.strip().lower()
    full_state = dict(workflow_run.state)

    original = [label.strip().lower() for label in full_state.get("intent", "").split(",")]
    remaining = [
        label for label in original
        if label != chosen and any(kw in label for kw in ("book", "cancel", "reschedule"))
    ]
    full_state["remaining_intents"] = remaining
    workflow_run.state = full_state
    db.commit()

    if "book" in chosen:
        return continue_as_booking(db, workflow_run)

    action = "cancel" if "cancel" in chosen else "reschedule"
    full_state["pending_appointment_action"] = action
    workflow_run.state = full_state
    db.commit()
    return _land_on_appointment_selection_or_none(db, workflow_run, full_state)
```

### `_finalize_or_continue_intents` (new, `app/workflow_runner.py`)

```python
def _finalize_or_continue_intents(workflow_run: WorkflowRun, full_state: dict) -> None:
    """Called at every point that would otherwise set a terminal `completed`
    status. If the patient had a leftover intent from an earlier
    needs_intent_selection pick, land back there for the next one instead
    of ending the run - closes the "asked which one first, then forgot the
    second" gap. Mutates workflow_run/full_state in place; caller still
    does workflow_run.state = dict(full_state) and db.commit() same as
    before, this only decides which status to set."""
    remaining = full_state.get("remaining_intents") or []
    if remaining:
        next_intent, *rest = remaining
        full_state["intent"] = next_intent
        full_state["remaining_intents"] = rest
        full_state["needs_intent_selection"] = True
        full_state["pending_appointment_action"] = None
        full_state["rescheduling_appointment_id"] = None
        full_state["department_id"] = None
        workflow_run.status = WorkflowStatus.needs_intent_selection
        workflow_run.current_step = "needs_intent_selection"
    else:
        workflow_run.status = WorkflowStatus.completed
```

Call sites that currently do `workflow_run.status = WorkflowStatus.completed`
as a genuine terminal outcome, replaced with `_finalize_or_continue_intents(workflow_run, full_state)`:
- `_land_on_appointment_selection_or_none` (zero appointments to act on)
- `_land_on_slots_or_no_slots` (zero open slots)
- `continue_with_selected_slot` (successful book/reschedule)
- `continue_as_appointment_action`'s cancel branch (successful cancel)

Each of these keeps its existing `full_state["appointment_id"] = ...` (or
whatever result-specific field it already sets) **before** calling
`_finalize_or_continue_intents` — the helper only decides the *status*, not
the result data. `needs_slot_selection` and `needs_appointment_selection`
(the non-terminal statuses these same functions can also land on, e.g. when
slots/appointments genuinely exist) are untouched — `remaining_intents`
simply stays in `full_state` unexamined until a real `completed` moment is
reached later in that same sub-flow.

### Wording (`_render_patient_message`, `app/routes/request_routes.py`)

```python
if workflow_status == WorkflowStatus.needs_intent_selection:
    if "," in state.get("intent", ""):
        message = "It sounds like you're asking about a few things. Which one should I help with first?"
    else:
        message = "Got it. Now let's take care of the rest of your request — which one's next?"
```

No template change needed — `request_status.html`'s existing
`needs_intent_selection` block already loops over `detected_intents` and
renders one button per label; a single-item list just renders one button,
same code path.

The `GET /requests/{id}` route's existing `detected_intents` computation
(splitting `state["intent"]` on comma, filtering to actionable labels)
already works correctly for a single-label continuation string with no
comma — `"book_appointment".split(",")` is `["book_appointment"]`, one
button. No route change needed beyond what the prior spec already built.

## 4. Data flow

1. Patient submits "cancel my appointment and book a new one."
2. `needs_intent_selection`: two buttons, "Cancel an appointment" / "Book an
   appointment."
3. Patient clicks "Cancel." `continue_as_intent_selection` stores
   `remaining_intents = ["book_appointment"]`, dispatches to the cancel path.
4. Cancel completes (real appointment found and cancelled) →
   `continue_as_appointment_action`'s success branch calls
   `_finalize_or_continue_intents` → sees `remaining_intents` non-empty →
   sets `intent = "book_appointment"`, `remaining_intents = []`, status back
   to `needs_intent_selection`.
5. Patient reloads the same status page: "Got it. Now let's take care of the
   rest of your request — which one's next?" with one button, "Book an
   appointment."
6. Patient clicks it → `continue_as_intent_selection` again, this time
   `remaining_intents` computes to `[]` (nothing left) → dispatches to
   booking as normal.
7. Booking completes for real (or hits "no slots") →
   `_finalize_or_continue_intents` sees `remaining_intents` empty → sets
   `completed` for real this time.

## 5. Error handling

- If the *first* chosen action escalates (`needs_review`) instead of
  completing, the leftover intent in `remaining_intents` is simply never
  revisited — an escalation means a human needs to look at this run, so
  quietly moving on to auto-offer a second action would be the wrong call;
  this is accepted as correct, not a gap.
- Same ownership/404/stale-status-no-op guards as every other route —
  nothing new here since no new route is added by this spec.

## 6. Testing

- `continue_as_intent_selection`: given `state["intent"] =
  "cancel_appointment,book_appointment"` and `chosen_intent =
  "cancel_appointment"`, asserts `remaining_intents == ["book_appointment"]`
  after the call.
- `_finalize_or_continue_intents`: given `remaining_intents =
  ["book_appointment"]`, asserts the resulting status is
  `needs_intent_selection` and `state["intent"] == "book_appointment"`;
  given `remaining_intents = []`, asserts `completed`.
- Full regression: `continue_as_appointment_action`'s cancel branch,
  `continue_with_selected_slot`, `_land_on_slots_or_no_slots`, and
  `_land_on_appointment_selection_or_none`'s existing tests must all still
  pass unchanged for the common case (`remaining_intents` absent/empty) —
  this is the key check that the shared helper doesn't disturb single-intent
  behavior.
- End-to-end: full two-intent flow from `run_workflow` through both picks to
  final `completed`, asserting the patient never has to submit a second
  free-text request.
- Live Groq validation: re-run "cancel my current appointment and book a new
  one with cardiology" end-to-end (pick cancel, confirm it lands back on
  book, pick book, confirm real booking) against the real API.

## 7. Open items resolved during self-review

- Confirmed `_finalize_or_continue_intents` takes `workflow_run` and
  `full_state` (not `db`) and does not commit — callers already have their
  own `workflow_run.state = dict(full_state); db.commit()` line right after
  setting status; this helper only decides *what* status/state to set, kept
  consistent with how those call sites already work rather than
  restructuring their commit timing.
- Confirmed the wording distinction (`"," in intent` vs. not) requires no
  new state field — the shape of `state["intent"]` itself (comma-joined vs.
  single label) already encodes "first landing" vs. "continuation."
- Confirmed this does not interact with `needs_appointment_selection`'s or
  `needs_slot_selection`'s own logic at all — `remaining_intents` rides
  along in `full_state` untouched through those intermediate steps and is
  only ever read at the four genuine-`completed` call sites listed above.
