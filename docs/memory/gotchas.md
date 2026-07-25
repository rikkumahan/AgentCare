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

## Tampering the *last* character of a signed token is flaky

`itsdangerous` tokens are base64url-encoded; the final character(s) of a
base64 group can be padding-truncated, so flipping only the last character
sometimes decodes to the same underlying bytes (~1/64 false-pass rate).
`tests/test_auth.py::test_session_token_rejects_tampering` tampers the
**first** character instead — always sits in a full, non-truncated group.
General rule: when a test needs to "corrupt a byte" of an encoded blob,
don't pick the last character of the encoding.
