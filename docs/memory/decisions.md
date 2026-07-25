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

## Building directly on `master`, no feature branches or worktrees

Both Phase 1 (foundation) and Phase 2 (core agent loop) were implemented
via subagent-driven-development committing straight to `master`, confirmed
explicitly with the user before Phase 2's execution (no remote configured
either, so there's nothing to push/PR against yet).

**How to apply:** keep doing this for Phase 3+ unless the user asks for
branch isolation.
