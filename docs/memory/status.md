# Status

At-a-glance build status. Update the relevant line whenever a phase
starts, finishes, or its plan changes — this is the fastest way for a
fresh session (human or agent) to know what's real right now versus what's
just written down in a plan.

| Phase | Status | Plan doc |
|---|---|---|
| 1. Foundation (Docker+Postgres, 11-table schema+Alembic, auth+RBAC, seed data) | ✅ Done | `docs/superpowers/plans/2026-07-22-foundation.md` |
| 2. Core agent loop (Safety + Coordinator agents as private LangGraph subgraphs, 2-node parent graph, per-node checkpointing, audited tools) | ✅ Done — validated against real Groq API | `docs/superpowers/plans/2026-07-23-core-agent-loop.md` |
| 3. Department Routing + Appointment agents | ⬜ Not started | not yet written |
| 4. Document agent | ⬜ Not started | not yet written |
| 5. Follow-up agent + second Safety pass + audit/error-handling hardening | ⬜ Not started | not yet written |
| 6. UI polish, seed data realism, demo pass | ⬜ Not started | not yet written |

Design spec (source of truth for the full architecture, all 6 agents,
data model): `docs/superpowers/specs/2026-07-22-agentcare-design.md`.

Current graph shape (Phase 2): parent `StateGraph(WorkflowState)` with
exactly two nodes — `safety_agent` → (escalate? `END` : `coordinator_agent`)
→ `END`. A non-escalated run currently ends with `status=running`,
`current_step="routing_agent"` — that's the seam Phase 3 extends from.

36/36 tests passing as of commit `025d13d` (branch `master`, no
feature branches/worktrees in use, no remote configured yet).
