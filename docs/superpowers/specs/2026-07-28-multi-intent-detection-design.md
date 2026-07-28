# Multi-Intent Detection — Design Spec

Status: approved by user through conversational review (Option 1 — detect
and ask, reuse existing continuations — confirmed directly).

Source of truth: `problem_statement.md`, `CLAUDE.md`. Builds on the current
graph/workflow_runner shape (post cancel/reschedule, post appointments-pages,
both already shipped). Confirmed live against the real Groq API that the
current system silently picks one intent and drops the rest when a request
contains more than one distinct ask (e.g. "cancel my appointment and book a
new one with cardiology" → misclassified as a single `reschedule_appointment`
intent, silently ignoring the "with cardiology" department switch).

## 1. Goal

`COORDINATOR_SYSTEM_PROMPT` (`app/agents/coordinator.py`) currently forces a
single "one to three word" intent label no matter what the patient actually
asked for. When a request genuinely contains more than one distinct
administrative ask, the model is forced to either merge them (incorrectly)
or silently drop everything but one. This spec adds detection: when the
Coordinator genuinely sees multiple distinct intents, stop and ask the
patient which one to handle, instead of guessing or silently dropping any of
them — same "detect ambiguity, ask, don't guess" principle already applied
to department selection (`needs_appointment_reason`) and appointment
selection (`needs_appointment_selection`).

## 2. Scope

**In scope:**
- `COORDINATOR_SYSTEM_PROMPT` update: still a single label for the common
  (single-intent) case; a comma-separated list only when the request
  genuinely contains 2+ distinct administrative asks.
- `route_after_document` (`app/graph.py`) gains one new check, evaluated
  **before** the existing ones: if `intent` contains a comma, route to a new
  node instead of the existing book/cancel-reschedule/clarification checks.
- One new `WorkflowStatus`: `needs_intent_selection` (one migration).
- One new trivial graph node (`needs_intent_selection_node`), same shape as
  the existing `needs_clarification_node`.
- One new public function in `app/workflow_runner.py`,
  `continue_as_intent_selection`, that dispatches to **existing, unmodified**
  functions (`continue_as_booking`, `_land_on_appointment_selection_or_none`)
  based on which intent button the patient clicked. No new booking/cancel/
  reschedule business logic anywhere.
- `request_status.html` gains one new block: a button per detected intent.
- One new route, `POST /requests/{id}/select-intent`.
- Wording for the new status in `_render_patient_message`.

**Explicitly out of scope:**
- Actually executing multiple intents in sequence (e.g. cancel then book) —
  this spec only adds a disambiguation step; the patient picks one, exactly
  like every other selection screen already built this session.
- Any change to `continue_as_booking`, `continue_as_appointment_action`,
  `_land_on_slots_or_no_slots`, `_land_on_appointment_selection_or_none`, or
  any of the routing/appointment tool functions — all reused exactly as-is.
- Any change to the single-intent case's existing behavior — a request that
  produces one label continues through the exact same path it does today;
  `route_after_document`'s existing three branches are otherwise untouched,
  just checked after the new comma check instead of first.
- `submit_document` / `general_inquiry` combinations with book/cancel/
  reschedule — if the Coordinator's list includes a non-actionable label
  alongside an actionable one, only the actionable ones are offered as
  buttons (see wording section); this does not need special-casing beyond
  filtering the button list.

## 3. Architecture

### Coordinator prompt (`app/agents/coordinator.py`)

Current `COORDINATOR_SYSTEM_PROMPT` (lines 12-22) ends with: `"reply with a
one to three word administrative intent label for the request, for example:
book_appointment, reschedule_appointment, cancel_appointment,
submit_document, general_inquiry."`

Append one clause:

```python
COORDINATOR_SYSTEM_PROMPT = (
    "You are the Coordinator Agent for AgentCare, an administrative "
    "healthcare workflow assistant. Given the patient's free-text request, "
    "call get_or_create_patient with any contact details mentioned in the "
    "request (phone, preferred_language, emergency_contact — pass {} if "
    "none are mentioned). After the tool result comes back, reply with a "
    "one to three word administrative intent label for the request, for "
    "example: book_appointment, reschedule_appointment, "
    "cancel_appointment, submit_document, general_inquiry. If — and only "
    "if — the request genuinely contains two or more distinct "
    "administrative asks (e.g. \"cancel my appointment and book a new one\" "
    "or \"reschedule my visit and also cancel my other booking\"), reply "
    "instead with all the distinct intent labels separated by commas and "
    "nothing else, for example: cancel_appointment,book_appointment. Do not "
    "split a single request into multiple labels just because it has "
    "multiple sentences or extra detail — only when there are genuinely "
    "separate administrative actions being requested. Never diagnose or "
    "suggest treatment — only classify the administrative intent(s)."
)
```

### Graph change (`app/graph.py`)

```python
def route_after_document(
    state: WorkflowState,
) -> Literal["routing_agent", "needs_appointment_selection", "needs_intent_selection", "needs_clarification"]:
    intent = (state.get("intent") or "").strip().lower()
    if "," in intent:
        return "needs_intent_selection"
    if intent == "book_appointment" or "book" in intent:
        return "routing_agent"
    if "cancel" in intent or "reschedule" in intent:
        return "needs_appointment_selection"
    return "needs_clarification"


def needs_intent_selection_node(state: WorkflowState, config) -> dict:
    return {"needs_intent_selection": True}
```

The comma check runs first so a genuinely multi-intent reply never
accidentally matches the `"book" in intent` substring check (which would
otherwise silently win, exactly the bug that prompted this spec). Register
`needs_intent_selection` as a new node in `build_graph()`, wired into the
existing `route_after_document` conditional edge dict, ending at `END` —
identical wiring shape to the existing `needs_appointment_selection` node.

`WorkflowState` gains: `needs_intent_selection: bool`.

### Workflow runner (`app/workflow_runner.py`)

Status resolution gains one branch, checked before the existing ones (order
matters — same reasoning as the graph check: a multi-intent flag must win
over any other flag that might coincidentally also be set):

```python
elif full_state.get("needs_intent_selection"):
    workflow_run.status = WorkflowStatus.needs_intent_selection
```

New public function, added near the other `continue_as_*` functions:

```python
def continue_as_intent_selection(db, workflow_run: WorkflowRun, chosen_intent: str) -> WorkflowRun:
    """needs_intent_selection -> patient picked which of the detected
    intents to handle first. Dispatches to whichever existing, unmodified
    continuation the single-intent graph path would have used for that
    label - no new booking/cancel/reschedule logic here, only routing to
    code that already exists and is already tested:

    - book_appointment: continue_as_booking (existing) - runs routing_agent
      to resolve a department from the original request_text, same as the
      confident-single-intent path.
    - cancel_appointment / reschedule_appointment: sets
      pending_appointment_action on state, then
      _land_on_appointment_selection_or_none (existing) - same as
      needs_appointment_selection_node's effect for the single-intent path.
    """
    chosen = chosen_intent.strip().lower()
    full_state = dict(workflow_run.state)

    if "book" in chosen:
        return continue_as_booking(db, workflow_run)

    action = "cancel" if "cancel" in chosen else "reschedule"
    full_state["pending_appointment_action"] = action
    workflow_run.state = full_state
    db.commit()
    return _land_on_appointment_selection_or_none(db, workflow_run, full_state)
```

### Routes (`app/routes/request_routes.py`)

```python
POST /requests/{id}/select-intent
  intent=<one of the comma-separated labels> -> continue_as_intent_selection(db, workflow_run, intent)
```

Same guard shape as every other route in this file: `require_role
("patient")`, ownership check (404/403), stale-status no-op redirect if
`workflow_run.status != needs_intent_selection`.

The `GET /requests/{id}` route's per-status context block gains one entry:
when `needs_intent_selection`, parse `state["intent"].split(",")`, filter to
only actionable labels (containing "book", "cancel", or "reschedule" —
`submit_document`/`general_inquiry` are not offered as buttons here since
they have no dedicated continuation function to dispatch to; if a detected
list contains only non-actionable labels, this is treated as
`needs_clarification` instead — see wording section), and pass
`detected_intents=[...]` into the template context.

### Wording (`_render_patient_message`)

- `needs_intent_selection` → `"It sounds like you're asking about a few
  things. Which one should I help with first?"` (buttons: one per detected
  actionable intent, human-readable — `"Book an appointment"`, `"Cancel an
  appointment"`, `"Reschedule an appointment"`)

All other existing wording branches unchanged.

## 4. Data flow

1. Patient submits free text with two distinct asks, e.g. "cancel my
   appointment and book a new one."
2. Safety → Coordinator: replies `cancel_appointment,book_appointment`
   (comma-separated, per the updated prompt).
3. Document agent runs (unchanged).
4. `route_after_document` sees the comma → `needs_intent_selection_node` sets
   the flag → graph ends.
5. `workflow_runner` lands on `WorkflowStatus.needs_intent_selection`.
6. Patient sees the two buttons, picks one (say, "Cancel an appointment").
7. `POST /select-intent` → `continue_as_intent_selection` → sets
   `pending_appointment_action="cancel"` → `_land_on_appointment_selection_or_none`
   (existing) → same `needs_appointment_selection` screen the single-intent
   path already produces, patient picks which real appointment, proceeds
   exactly as today.
8. If the patient wants the *other* intent handled too, they submit a new
   request afterward — sequential execution of both in one request is
   explicitly out of scope (see Scope section).

## 5. Error handling

- Same `require_role`/ownership/404/stale-status-no-op pattern as every
  other route in this file.
- If Coordinator returns a comma-separated list where every label is
  non-actionable (e.g. `general_inquiry,submit_document` — unlikely given
  the prompt's guidance, but not impossible), the filtered `detected_intents`
  list passed to the template will be empty; treat this the same as
  `needs_clarification` (fall through to that status instead) rather than
  render a button-less screen. This is a one-line guard in the `GET
  /requests/{id}` route's context-building block, not a new status.

## 6. Testing

- Coordinator prompt (manual/live check only — prompt wording isn't unit
  tested per `CLAUDE.md`'s "mock the LLM call, don't test prompt wording").
- Graph routing: `"cancel_appointment,book_appointment"` →
  `needs_intent_selection`; single labels (`"book_appointment"`,
  `"cancel_appointment"`, etc.) → unchanged existing branches (regression
  check that the new comma check doesn't affect single-intent behavior).
- `continue_as_intent_selection`: `"book_appointment"` → same downstream
  state as calling `continue_as_booking` directly; `"cancel_appointment"` →
  lands on `needs_appointment_selection` with `pending_appointment_action ==
  "cancel"`; `"reschedule_appointment"` → same with `"reschedule"`.
- Routes: `/select-intent` happy path (both branches) + wrong-owner (403) +
  nonexistent (404) + stale-status no-op, mirroring existing selection-route
  tests.
- Regression: full existing test suite must still pass unchanged — this is
  the key check that nothing here disturbs single-intent behavior.
- Live Groq validation: re-run the exact three prompts from the original bug
  report ("cancel my current appointment and book a new one with
  cardiology", "please reschedule my appointment and also cancel my other
  booking", "book me an appointment and also tell me my visiting hours")
  and confirm the first two now land on `needs_intent_selection` with both
  labels detected, while the third (arguably a single actionable intent plus
  an unrelated question, not two administrative actions) is judged against
  the updated prompt's guidance and reported as-is, not force-fixed if the
  model reasonably treats it as single-intent.

## 7. Open items resolved during self-review

- Confirmed `continue_as_intent_selection` calls `_land_on_appointment_selection_or_none`
  directly (a private, underscore-prefixed function) rather than duplicating
  its logic — safe because both live in the same module (`workflow_runner.py`),
  matching how `continue_as_appointment_action` already does the same thing
  in the existing cancel/reschedule spec.
- Confirmed the new comma-check in `route_after_document` is evaluated first,
  specifically to prevent the existing `"book" in intent` substring check
  from ever matching a genuinely multi-intent string like
  `"cancel_appointment,book_appointment"` (which does contain the substring
  "book") — this ordering is the entire point of the fix, called out
  explicitly rather than left implicit.
- Confirmed no existing function's signature or behavior changes — this
  spec is purely additive (one new status, one new node, one new function,
  one new route, one new template block, one prompt clause).
- Flagged: a request that reasonably contains one actionable intent plus an
  unrelated non-actionable question (e.g. "book an appointment and tell me
  your visiting hours") may or may not trigger multi-intent detection
  depending on how the model reads "genuinely distinct administrative asks"
  — the prompt asks it to only split on genuine multiple *actions*, and a
  question isn't an action. This is accepted as reasonable model judgment,
  not further constrained, consistent with how Routing's NEEDS_MORE_INFO
  judgment call was already accepted without over-specifying every edge case.
