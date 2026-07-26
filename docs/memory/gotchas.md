# Gotchas

Environment/tooling quirks that cost real debugging time once already —
check here before rediscovering them.

## Running pytest wipes the dev database's schema

`tests/conftest.py`'s session-scoped `_schema` fixture does
`Base.metadata.drop_all` + `create_all` at the **start** of every pytest
session, and `drop_all` again at the **end**. This runs against whatever
`DATABASE_URL` is active — the same database you use for manual testing or
running the app directly.

**Symptom:** `relation "users" does not exist` when running a manual
script (e.g. a smoke test) or the app itself, right after a `pytest` run.

**Fix:** run `alembic upgrade head` immediately before any non-pytest use
of the database (manual scripts, `docker compose up` the app service,
seeding). Idempotent, safe to run anytime.

## Python 3.14 + langchain_core produces a pydantic warning

Importing anything that pulls in `langchain_core` (e.g. `langchain_groq`)
triggers:
```
UserWarning: Core Pydantic V1 functionality isn't compatible with Python 3.14 or greater.
```
This is an upstream library/interpreter-version incompatibility, not
something fixable in this codebase. Suppressed narrowly in `pytest.ini`:
```
filterwarnings =
    ignore::UserWarning:langchain_core.utils.pydantic
```
Scoped to that one module/category — don't broaden it to blanket-suppress
all warnings, or real future warnings will go unnoticed.

## Local Postgres port conflict — 5432 vs 5433

A native Windows Postgres install can hold port 5432. This project's
canonical `docker-compose.yml` and `.env.example` still use 5432 (so a
fresh clone/judge environment with no port conflict "just works"). Locally
on this machine, a gitignored `docker-compose.override.yml` remaps the `db`
service to 5433, and `.env`'s `DATABASE_URL` points at 5433 to match.

**If you see a port-bind failure on `docker compose up`:** check whether
something else already owns 5432 before assuming the compose file is wrong.

## bcrypt must be pinned `<5` for this Python version

`passlib`'s bcrypt backend has a compatibility ceiling; `requirements.txt`
pins `bcrypt<5` accordingly (foundation phase). Don't let a dependency
upgrade silently bump past it.

## `monkeypatch.setattr(..., lambda: FakeToolCallingModel([...]))` silently infinite-loops

Construct the mock **before** the `setattr` call and close over that instance
(`model = FakeToolCallingModel([...]); monkeypatch.setattr("...get_llm", lambda: model)`).
Never construct it inline in the lambda.

**Symptom:** `langgraph.errors.GraphRecursionError: Recursion limit of ... reached`,
with the failing node stuck on whichever agent's subgraph loops more than
once per run (Coordinator/Routing/Appointment — anything with a
tool-call loop; Safety never loops so it happens to tolerate the mistake).

**Why:** any agent whose subgraph node calls `get_llm()` on every loop
iteration will, with an inline lambda, get a **brand-new** mock each call —
its response queue resets to item #1 every time, so the scripted
second/third response (the one that ends the loop) is never reached.

**Fix:** always `model = FakeToolCallingModel([...])` first, then
`monkeypatch.setattr(target, lambda: model)`. Confirmed by an actual failing
test run during Phase 3 (`docs/superpowers/plans/2026-07-25-routing-appointment-agents.md`,
Task 5) — this cost real debugging time (recursion-limit errors don't show
which mock caused them; had to add tracing to `coordinator_llm_node` to see
the model was being reconstructed fresh every call).

## A shared SQLAlchemy Session crashes only under real LLM latency, never in tests

**Symptom:** `sqlalchemy.exc.InvalidRequestError: This session is provisioning
a new connection; concurrent operations are not permitted` — but only when
hitting the real Groq API through the live app/a manual script. All 69
mocked-LLM pytest tests passed the whole time.

**Why:** LangGraph's `ToolNode` dispatches every tool call through a
`ThreadPoolExecutor` (`langgraph/prebuilt/tool_node.py`), running each tool
in a worker thread — always, even for a single tool call. We passed one
shared, plain `Session` object through `config["configurable"]["db"]`, and
SQLAlchemy `Session` objects are not safe to touch from more than one
thread. With `FakeToolCallingModel` (instant, no real I/O), the timing gap
between threads touching the session was apparently too narrow to trigger
the conflict; real Groq network latency (hundreds of ms per call) widens
that window enough that it reliably does.

**Fix:** `app/db.py`'s `SessionLocal` is now `scoped_session(sessionmaker(...))`
instead of a plain `sessionmaker`, and `app/workflow_runner.py` passes the
`SessionLocal` registry itself (not an already-resolved instance) into
`config["configurable"]["db"]`. `scoped_session` proxies the full `Session`
API and hands each thread its own session transparently, so every existing
tool function and agent node needed zero changes — they already just call
`db.query(...)`/`.add()`/`.commit()`.

**How to apply:** any config value handed to a LangGraph graph that a
`ToolNode` will touch must be thread-safe by construction. Never pass a bare
`Session()` instance across that boundary — pass the `scoped_session`
registry (or another thread-safe handle) instead. If you ever add a
DIFFERENT kind of shared mutable resource to `config["configurable"]`,
assume `ToolNode` will touch it from a worker thread and design for that
from the start, rather than waiting for real latency to expose it.

## An LLM told to "reply with ONLY the exact name" still paraphrases

**Symptom:** Department Routing escalated a request that should have
matched an existing, correctly-named department. The real Groq model's
final reply was `"Cardiology Department"`, not the bare `"Cardiology"` the
exact-match name lookup required — despite `ROUTING_SYSTEM_PROMPT` saying
"reply with ONLY that department's exact name, nothing else."

**Why:** confirmed against the real API, not a hypothetical: prompt
instructions asking an LLM to echo a string verbatim are not reliable
enough on their own to gate a real decision on. CLAUDE.md already says
"don't test prompt wording" — the same logic means don't *rely* on prompt
wording for correctness either.

**Fix:** `routing_agent_node` (`app/agents/routing.py`) now checks an exact
match first, then falls back to substring containment either direction
(`candidate_name in normalized_reply or normalized_reply in candidate_name`)
before giving up and escalating.

**How to apply:** any place code parses free-text LLM output to match it
against a known set of values (names, categories, labels) needs a
tolerant-matching fallback, not just an exact-string check — even when the
prompt explicitly asks for an exact echo.

## Tools must validate LLM-supplied ids before parsing them as UUIDs

**Symptom:** `ValueError: badly formed hexadecimal UUID string` inside
`book_or_modify_appointment`, crashing the whole graph run instead of
letting the Appointment agent retry with a different slot. Confirmed
against the real Groq API — the model called
`check_slot_availability_tool`, saw a real slot's id in the result, then
called `book_or_modify_appointment_tool` with a `slot_id` that wasn't a
valid UUID at all (not a transcription of the wrong-but-valid id — a
genuinely malformed string).

**Why:** an earlier design note reasoned that `slot_id` was safe to trust
because the LLM "just saw it" in the same conversation turn (unlike
`workflow_run_id`, which it never sees as text). That's still true for the
*transcription-accuracy* risk it was meant to address, but it doesn't
cover the model simply emitting a malformed value outright — a different
failure mode, and a real one.

**Fix:** added `_parse_uuid()` in `app/tools/appointment_tools.py` that
returns `None` instead of raising on an invalid string; both `slot_id` and
`existing_appointment_id` are validated before use and return the same
kind of structured `{"status": "error", ...}` dict as every other rejected
case, so the agent can loop back and retry per its own prompt instructions,
instead of the exception crashing the graph.

**How to apply:** any tool argument that's model-supplied (even ids the
model "just saw") must be validated as a real input, not assumed
well-formed — parse defensively and return a structured error, matching the
existing pattern used for "not found"/"no longer open" cases.

## Tampering the *last* character of a signed token is flaky

`itsdangerous` tokens are base64url-encoded; the final character(s) of a
base64 group can be padding-truncated, so flipping only the last character
sometimes decodes to the same underlying bytes (~1/64 false-pass rate).
`tests/test_auth.py::test_session_token_rejects_tampering` tampers the
**first** character instead — always sits in a full, non-truncated group.
General rule: when a test needs to "corrupt a byte" of an encoded blob,
don't pick the last character of the encoding.
