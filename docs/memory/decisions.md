# Decisions

Architectural/implementation decisions made during the build that aren't
(fully) captured in CLAUDE.md or the design spec, with the reasoning —
so nobody re-litigates them from scratch or accidentally reverses them.

## Each agent is a private LangGraph subgraph, not 3 nodes in one shared graph

The parent graph (`app/graph.py`, `WorkflowState`) has exactly one node per
agent. Internally, each agent's own LLM-call / tool-execution / capture
logic lives in a **separately compiled subgraph** with its own private
state (its own `messages` list). The parent state has **no `messages` key
at all**.

**Why:** an earlier draft gave every agent 3 top-level nodes in one shared
graph, all appending to one `WorkflowState["messages"]` list. That means
agent N's LLM call replays every prior agent's tool-calling exchange as
input tokens (cost grows faster than linearly as more agents are added),
and can put a tool name in an agent's history that isn't in *that* agent's
own bound-tools list (a real cross-provider risk). Verified against the
actually-installed `langgraph==1.2.9`/`langchain-core==1.5.0` docs (not
assumed from training data): `InjectedState` requires execution through a
real `ToolNode`, and LangGraph subgraphs with a different state schema than
their parent are the documented mechanism for giving each agent a private
message history. See `docs/superpowers/plans/2026-07-23-core-agent-loop.md`
("Why subgraphs, not one flat graph").

**How to apply:** every future agent (Routing, Appointment, Document,
Follow-up) follows this same shape — its own `<Agent>State` TypedDict, its
own compiled subgraph, one parent-facing wrapper function that seeds the
subgraph from `WorkflowState`, invokes it, and returns only the structured
fields that belong in the parent state. Never let a subgraph's internal
`messages` cross into `WorkflowState`.

## Tool arguments the LLM can't reliably know are injected, never model-supplied

`workflow_run_id`, `user_id` (and any future ID the LLM would otherwise have
to copy from context) are injected into tools via `InjectedState`, not
passed as model-facing tool arguments. The model only ever supplies things
it can actually reason about (`reason`, `profile_fields`).

**Why:** an LLM asked to echo back an opaque UUID it saw in text can
transcribe it wrong. Injection removes an entire class of failure.

**How to apply:** when adding a new tool, ask "could the LLM plausibly get
this argument wrong or hallucinate it?" — if yes, it belongs in the
subgraph's state and gets injected, not requested from the model.

## Safety agent's capture step must fail closed on a malformed tool call

If the LLM calls `create_escalation_tool` but `ToolNode` catches an
argument-validation error before the real function runs, the resulting
`ToolMessage.artifact` is `None`. `safety_capture_node` treats that the
same as "attempted escalation" (sets a truthy `escalation`), not the same
as "no escalation" — otherwise a malformed attempt to flag something unsafe
would silently be treated as safe.

**Why:** caught in the Phase 2 final whole-branch review — the original
code did `return {"escalation": last.artifact}` unconditionally, which is
`None` (falsy) in the validation-error case, routing the request onward as
if it were safe. Fixed in commit `025d13d`.

**How to apply:** any future capture node reading a tool's structured
result must treat "the tool was invoked but produced no real artifact" as
a failure to route on, not as a negative/default answer — especially for
anything gating safety.

## No stored procedures / DB-side functions anywhere in this project

All reads/writes go through plain SQLAlchemy ORM calls from Python
(`db.query(...)`, `db.add(...)`, `db.commit()`). No PL/pgSQL functions, no
Supabase RPC calls.

**Why:** no performance case for it at this scale (hackathon admin app,
modest data volume), and keeping logic in Python keeps the `@audited`
decorator and ORM-level test assertions working cleanly — a stored
procedure would bypass both.

**How to apply:** if a specific hot path genuinely needs it later, that's
a deliberate, separately-justified addition — not a default to reach for.

## Routing's escalation bypasses Safety's LLM node entirely

When Department Routing can't confidently match a request to a department,
`routing_agent_node` calls the plain `create_escalation()` function directly
(the same audited function Safety's tool uses) instead of looping back into
the `safety_agent` node.

**Why:** looping back into `safety_agent` risks an infinite cycle — the
same unroutable request would go safety → coordinator → routing → safety →
coordinator → routing… forever, since nothing about the request changes
between passes. It would also wrongly couple Routing's failure mode into
Safety's diagnosis/emergency-focused prompt. "No confident department
match" is a deterministic outcome, not a judgment call, so it doesn't need
a second LLM turn at all.

**How to apply:** any future agent that can fail to resolve something
(Document's missing-doc detection, Follow-up's stale-workflow scan) should
follow the same shape if it needs to escalate: call `create_escalation`
directly from the agent's own parent-facing wrapper, not by routing back
through Safety's node.

## Coordinator's final confirmation call stays deferred through Phase 3

Per the design spec, Coordinator's second LLM call (reading back
`Appointment`/`PatientDocument`/`Reminder` rows and rendering a real
confirmation) only fires "after all other nodes succeed" — which isn't
true yet with only Routing+Appointment built. Phase 3 ends a successful run
with structured `WorkflowState` only; no agent asserts success in free text.

**Why:** there's no UI yet (that's Phase 6) to show a confirmation to, and
building partial finalize logic now means rewriting it twice more once
Document/Follow-up add the rows it needs to read. Also avoids the CLAUDE.md
hard rule against hardcoded final responses by construction — nothing
asserts success until there's something complete to read back.

**How to apply:** wire the real finalize call once Document and/or
Follow-up exist, reading back whatever rows exist at that point.

## Test data that persists across a whole pytest session needs scoped assertions

`db_session` doesn't roll back between tests (established in Phase 2's
review — see the uniqueness note below). Phase 3 hit two flavors of the
same underlying issue: (1) a UNIQUE-constrained column (`Department.name`)
needs a fresh value per test, not a shared literal; (2) any assertion
against an *unscoped* query (a bare `.count()`, an exact `len(result)`, an
exact `names == {...}` set) silently accumulates rows from every other test
in the same session and breaks once enough other tests have run first.

**Why:** caught by actually running the Phase 3 tests against the real
Postgres DB rather than trusting a read-through — `lookup_departments`'s
own tests failed on a UNIQUE-constraint collision, and a `book_or_modify_appointment`
count assertion failed once other tests had already booked real
appointments in the same session.

**How to apply:** any query with no natural per-test scoping key (patient
id, workflow_run id, a uuid-suffixed name) must have its result asserted by
membership ("does this id appear/not appear") — never an absolute count or
exact set. See `docs/memory/gotchas.md` for the specific fix pattern.

## Building directly on `master`, no feature branches or worktrees

Both Phase 1 (foundation) and Phase 2 (core agent loop) were implemented
via subagent-driven-development committing straight to `master`, confirmed
explicitly with the user before Phase 2's execution (no remote configured
either, so there's nothing to push/PR against yet).

**How to apply:** keep doing this for Phase 3+ unless the user asks for
branch isolation.
