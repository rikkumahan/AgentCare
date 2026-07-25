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

64/64 tests passing as of the Phase 3 work (branch `master`, no
feature branches/worktrees in use, no remote configured yet). Two real
bugs were caught only by actually running the tests against the live
Postgres DB, not by reading the plan: a backwards substring check in
`lookup_departments`'s hint matching, and a `monkeypatch.setattr(...,
lambda: FakeToolCallingModel([...]))` mock-construction mistake that
caused a silent `GraphRecursionError` (see `docs/memory/gotchas.md`).
