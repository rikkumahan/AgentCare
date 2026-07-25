# Request Submission Routes — Design Spec

Status: approved by user through conversational review (scope, standard-vs-test
purpose, and file organization all confirmed). This is a small, focused spec —
one new router, two pages, reusing everything the app already has.

Source of truth for rules/scope: `problem_statement.md`, `CLAUDE.md`. Full
architecture reference: `docs/superpowers/specs/2026-07-22-agentcare-design.md`
(§7 lists `GET/POST /requests/new` and `GET /requests/{workflow_run_id}` as
part of the real, final route list — this spec builds exactly those two,
early and unstyled, not a substitute for them).

## 1. Goal

Give a logged-in patient a real, standard route to submit a free-text request
and see the real result of the agent workflow that's already built (Safety →
Coordinator → Routing → Appointment). This is production code, not a test
tool — it stays in the app permanently. The only thing deferred to Phase 6 is
visual polish (styling, a request-history list, staff views, file upload,
reschedule/cancel buttons) — none of that is built here.

**Why now, not at the end (Phase 6):** CLAUDE.md's top judging criterion is
"genuine end-to-end wiring (route → service → agent → tool → database →
persisted result)," and nothing currently proves that chain works through a
real HTTP request — only through pytest and manual Python scripts. Building
this now removes that risk early, while there's still time to fix anything
that surfaces, and gives a real browser to test in instead of scratchpad
scripts.

## 2. Scope

**In scope:**
- `GET /requests/new` — render a plain form (one textarea, one submit button)
- `POST /requests/new` — read the submitted text, resolve the patient's
  profile, call the existing `run_workflow()`, redirect to the result page
- `GET /requests/{workflow_run_id}` — show that one request's real, final
  state (status, current step, intent, department, appointment, escalation)
- RBAC: patient role only (`require_role("patient")`), same dependency every
  other route already uses
- Ownership check: a patient can only view their *own* workflow run

**Explicitly out of scope (confirmed with user):**
- A page listing a patient's past requests — direct-URL access to a single
  request's status is enough for this pass; a list view is dashboard-shaped
  work that belongs with the rest of Phase 6's UI
- Any styling beyond what `base.html` already provides
- File upload (`uploaded_files` stays an empty list — Document agent doesn't
  exist yet)
- Reschedule/cancel actions, staff-facing routes, staff escalation approval
- Background/async execution or polling — `run_workflow()` already runs
  synchronously and finishes in a few seconds (a handful of Groq calls), so
  by the time the redirect lands, the run is already complete

## 3. Architecture

One new router file, `app/routes/request_routes.py`, following the existing
one-file-per-concern pattern (`auth_routes.py` for auth, `dashboard_routes.py`
for dashboards). Registered in `app/main.py` alongside the other two routers.
Two new templates (`request_new.html`, `request_status.html`), extending the
existing `base.html` exactly like `dashboard.html` does.

No new tables, no schema changes — this reads/writes only what already
exists (`PatientProfile`, `WorkflowRun`, and whatever the graph itself writes
via its existing tools).

## 4. Components

### `app/routes/request_routes.py`

```python
def _get_or_create_profile(db: Session, user: User) -> PatientProfile: ...
    # Plain (non-audited) lookup-or-create. Deliberately NOT the audited
    # get_or_create_patient tool function — that's reserved for real agent
    # tool calls; calling it here on every page view would log an
    # AuditEvent for a plain page visit, misrepresenting the audit trail.

GET  /requests/new                    -> renders request_new.html
POST /requests/new                    -> resolves profile, calls run_workflow(),
                                          redirects to /requests/{id} (303)
GET  /requests/{workflow_run_id}      -> 404 if the run doesn't exist,
                                          403 if it belongs to a different
                                          patient, else renders
                                          request_status.html
```

All three depend on `Depends(require_role(UserRole.patient.value))` — the
exact dependency `dashboard_routes.py` already uses.

### Templates

`request_new.html` — extends `base.html`; one `<form method="post"
action="/requests/new">` with a `<textarea name="request_text">` and a
submit button. No JS, no CSS beyond the base stylesheet (if any).

`request_status.html` — extends `base.html`; plain text dump of
`workflow_run.status.value`, `workflow_run.current_step`, and the relevant
`workflow_run.state` fields (`intent`, `department_id`, `appointment_id`,
`escalation`) — enough to see the real result without guessing.

## 5. Data flow

1. Patient (already logged in) visits `/requests/new`, types a request, submits.
2. Route resolves their `PatientProfile` (creating one if this is their very
   first request — same lazy-create pattern the Coordinator agent itself
   already relies on).
3. Route calls `run_workflow(db, patient_id, user_id, request_text)` — the
   exact function every test and the smoke script already exercise. No new
   entrypoint logic; this route is a thin caller.
4. `run_workflow` returns a `WorkflowRun` row (already persisted, per its
   existing checkpointing behavior).
5. Route redirects (303, so a page refresh doesn't resubmit the form) to
   `/requests/{workflow_run.id}`.
6. That page re-fetches the `WorkflowRun` fresh from the DB and renders its
   real, final fields.

## 6. Error handling

- Not logged in → `require_role` already raises 401 (existing behavior,
  nothing new to build).
- Logged in as staff → `require_role` already raises 403.
- Viewing a `workflow_run_id` that doesn't exist → 404.
- Viewing another patient's `workflow_run_id` → 403 (ownership check, new).
- `run_workflow()` itself never raises past its own boundary — Phase 2's
  `workflow_runner.py` already catches any node exception internally and
  returns a `WorkflowRun` with `status=failed` instead of propagating. The
  route doesn't need its own try/except around the call; the status page
  will simply show `status: failed` and the stored error, same as any other
  outcome.

## 7. Testing

`tests/test_request_routes.py`, using the same `TestClient(app)` +
`monkeypatch` pattern as `tests/test_routes_rbac.py` and
`tests/test_workflow_runner.py`:

- Unauthenticated request to `/requests/new` → 401
- Staff-logged-in request to `/requests/new` → 403
- Patient submits a request (LLMs mocked exactly like the existing
  end-to-end tests) → real booking happens: 303 redirect, status page shows
  200 with the real status/appointment id, and the real `Appointment` row
  exists in the DB with `status=confirmed`
- One patient cannot view a different patient's `workflow_run_id` → 403
- Viewing a nonexistent `workflow_run_id` → 404

## 8. Open items resolved during self-review

- Confirmed the ownership check uses the patient's own `PatientProfile.id`
  compared against `WorkflowRun.patient_id` — no new field needed, both
  already exist.
- Confirmed no change to `run_workflow()` or the graph itself — this spec is
  additive only (new router, new templates), zero modification to
  Phases 2-3's code.
