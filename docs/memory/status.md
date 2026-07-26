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
owns styling, a request-history list, file upload, reschedule/cancel, and
staff-facing routes — none of that is here.

69/69 tests passing as of this work (branch `master`, no feature
branches/worktrees in use, no remote configured yet). Real bugs were caught
only by actually running things, not by reading the plan: in Phase 3, a
backwards substring check in `lookup_departments`'s hint matching and a
`monkeypatch.setattr(..., lambda: FakeToolCallingModel([...]))`
mock-construction mistake that caused a silent `GraphRecursionError` (see
`docs/memory/gotchas.md`); before writing the routes plan, a manual check of
`db.get(WorkflowRun, <string-id>)` and the `patient_id == profile.id`
ownership comparison against the real DB, confirming both work before
committing to the plan.
