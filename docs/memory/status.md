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
| 4. Document agent | ⬜ Not started | not yet written |
| 5. Follow-up agent + second Safety pass + audit/error-handling hardening | ⬜ Not started | not yet written |
| 6. UI polish, seed data realism, demo pass | ⬜ Not started | not yet written |

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
