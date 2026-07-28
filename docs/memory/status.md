# Status

At-a-glance build status. Update the relevant line whenever a phase
starts, finishes, or its plan changes — this is the fastest way for a
fresh session (human or agent) to know what's real right now versus what's
just written down in a plan.

| Phase | Status | Plan doc |
|---|---|---|
| 1. Foundation (Docker+Postgres, 11-table schema+Alembic, auth+RBAC, seed data) | ✅ Done | `docs/superpowers/plans/2026-07-22-foundation.md` |
| 2. Core agent loop (Safety + Coordinator agents as private LangGraph subgraphs, 2-node parent graph, per-node checkpointing, audited tools) | ✅ Done — validated against real Groq API | `docs/superpowers/plans/2026-07-23-core-agent-loop.md` |
| 3. Department Routing + Appointment agents | ✅ Done — 64/64 tests passing (real DB) | `docs/superpowers/plans/2026-07-25-routing-appointment-agents.md` |
| 4. Document agent | ✅ Done — 111/111 tests passing (real DB) | `docs/superpowers/plans/2026-07-27-document-agent.md` |
| 4a. Intent branching + "ask when unclear" popup | ✅ Done — built by Antigravity/Gemini while Claude's session limit was exhausted, reviewed by Claude after the fact | `docs/superpowers/plans/2026-07-27-intent-branching-popup.md` |
| 4b. Basic UI Navigation & Connectivity | ✅ Done | `docs/superpowers/plans/2026-07-28-basic-ui-navigation.md` |
| 5. Follow-up agent + staff escalation/reminder views | ⬜ Spec done, plan written | `docs/superpowers/plans/2026-07-27-followup-agent.md` |
| 6. UI polish, seed data realism, demo pass | ⬜ In progress | `docs/superpowers/plans/2026-07-28-basic-ui-navigation.md` |

**Time budget update (2026-07-27, later in the day): at most 12 hours
remain for all remaining work.** This cut the achievable scope hard.

**Build order reordered again (2026-07-27, still later): user explicitly
chose to de-risk the demo over chasing scoring weight.** Original order was
Document → Follow-up → intent-branching. User flagged that right now, any
request that isn't a clean "book an appointment" (reschedule, a general
question, anything the AI isn't fully confident about) silently escalates
with nothing shown to the patient — a judge typing a slightly-off-script
request would hit exactly that dead end. Explicitly said "I cannot risk"
this. New order: (1) Document agent — nearly done as of this note (8/10
tasks complete and reviewed, one Important-severity filename-sanitization
fix in flight on Task 8).

**Order finalized (2026-07-27, later still): confirm-before-booking is
NOT cut — split into its own phase, sequenced after the simpler piece.**
Final order: (1) Document agent — all 10 tasks complete as of this note,
111/111 tests passing, final whole-branch review next; (2) intent
branching + the "ask the patient when unclear" popup + human-readable
wording ONLY, from
`docs/superpowers/specs/2026-07-27-intent-branching-clarification-design.md`
sections 1-4 (not the confirm-before-booking sections) — small, low-risk,
reuses the Appointment agent exactly as it already is (proven, tested,
no changes to its LLM decision logic); (3) confirm-before-booking, as its
OWN separate implementation plan written from the same spec's remaining
sections — the Appointment-agent rework (remove
book_or_modify_appointment_tool from its tool list, add candidate-slot
validation, a new status, a new route) — deliberately isolated into its
own plan/review cycle rather than bundled with (2), since every previous
touch to the Appointment agent's core LLM logic this session surfaced a
new real bug; (4) Follow-up agent — reduced scope, decided 2026-07-27:
keep the appointment-reminder sweep and the required staff
escalation-review view (`problem_statement.md` line 207 - currently the
only thing missing this), CUT the missing-document reminder sweep
entirely (drops the `Reminder.note` column, the per-type dedup logic, and
`_document_gaps` - the more complex half). Original 11-task plan
(`docs/superpowers/plans/2026-07-27-followup-agent.md`) needs a matching
trim before execution, not written yet. (5) Reschedule/cancel plain
routes, only if time remains — small, tool already supports it. Nothing
beyond this — no visual polish, no doctor-search, no other extensions.

**Gap found during live manual testing (2026-07-28), fixed same day:**
clicking "Book an appointment" from the needs_clarification popup used to
call `continue_as_booking` immediately, which re-fed the *original*
ambiguous `request_text` into Routing — if that text had nothing routable
in it (e.g. "what are your visiting hours"), Routing correctly failed and
escalated, but the patient never got a chance to actually say what the
appointment was for. Fixed with a new intermediate step:
`WorkflowStatus.needs_appointment_reason` (migration `a1b2c3d4e5f6`).
Clicking "Book an appointment" now lands there first, showing real active
department buttons (skips Routing's LLM entirely if one is clicked —
`continue_as_booking_with_department`, deterministic, no guessing) plus a
free-text fallback (`continue_as_booking` gained an `override_request_text`
param, used instead of the stale original text). 156/156 tests passing,
migration verified. This was NOT part of either plan above — found by
actually using the app, not by re-reading the code, consistent with how
most real bugs surfaced this whole session.

Two specs (Document, Follow-up) were cross-checked twice by the user
directly against the running code before any implementation started, and
both found and fixed real gaps — see `docs/memory/gotchas.md` for the
full list (`SessionLocal`-vs-resolved-`db` regression risk in a spec, and
an `AppointmentStatus.rescheduled` filter gap that hit three call sites,
including one already-shipped Phase 3 bug in `_conflicting_appointment`,
now fixed with a regression test).

Design spec (source of truth for the full architecture, all 6 agents,
data model): `docs/superpowers/specs/2026-07-22-agentcare-design.md`.

Current graph shape (Phase 3): parent `StateGraph(WorkflowState)` with four
nodes — `safety_agent` → (escalate? `END` : `coordinator_agent`) →
`routing_agent` → (escalate? `END` : `appointment_agent`) → `END`. A
non-escalated run now books a real `Appointment` (conflict-checked,
slot flipped to `booked`) and ends with `status=running`,
`current_step="document_agent"` — that's the seam Phase 4 extends from.
Coordinator's final confirmation-rendering call is still deferred (no UI
exists yet to show it to); see `docs/memory/decisions.md`.

Ahead of Phase 6, one real (not styled) route now exists so the agent
workflow is reachable through an actual HTTP request, not just pytest/manual
scripts: `GET/POST /requests/new` and `GET /requests/{workflow_run_id}`
(`app/routes/request_routes.py`), patient-only, plain unstyled templates.
See `docs/superpowers/specs/2026-07-25-request-routes-design.md` and
`docs/superpowers/plans/2026-07-25-request-routes.md` — built early and
deliberately, not deferred to Phase 6, because CLAUDE.md's top judging
criterion is the full route→agent→DB chain, and that chain had never been
proven outside direct Python calls until this route existed. Phase 6 still
owns styling, a request-history list, file upload, and staff-facing routes.

**Reschedule/cancel — deliberately deferred until after Phase 4 (Document
agent), decided 2026-07-27.** The tool (`book_or_modify_appointment`)
already fully supports both actions; per the design spec §7 these are meant
to be plain routes (`POST /appointments/{id}/reschedule`,
`POST /appointments/{id}/cancel`) calling that same tool directly with a
known appointment id from the URL — no agent/prompt changes needed at all,
so this is small and low-risk whenever it's picked up. Deferred purely for
sequencing: problem_statement.md §11 puts document coordination in the
*highest*-weight bucket and the appointment workflow (which includes
reschedule/cancel) in the *substantial*-weight bucket, and building both at
once risked both changes touching `app/graph.py` at the same time.

81/81 tests passing as of this work (branch `master`, no feature
branches/worktrees in use, no remote configured yet). Real bugs kept
surfacing only by actually running things against the live Groq API, never
by reading the code: a shared SQLAlchemy `Session` crashing only under real
network latency (LangGraph's `ToolNode` runs tools in a worker thread —
fixed with `scoped_session`), a tool's `content` (not `artifact`) being all
the model ever sees again — both `lookup_departments_tool` and
`check_slot_availability_tool` originally showed only a bare count, so the
model had no real id/name to copy and hallucinated one — an unbounded
`ChatGroq` request timeout that caused a real multi-minute hang, and the
Appointment agent booking the same request three times before anything
stopped it (fixed by hard-stopping the graph after the first successful
booking, not by trusting the prompt). See `docs/memory/gotchas.md` for the
full list with fixes.
