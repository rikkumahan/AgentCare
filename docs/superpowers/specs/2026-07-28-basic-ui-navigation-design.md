# Basic UI Navigation & Dashboard Connectivity — Design Spec

Status: Approved by user for Option A.

Source of truth for rules/scope: `problem_statement.md`, `CLAUDE.md`. Builds on `docs/superpowers/specs/2026-07-25-request-routes-design.md` (the `/requests/new` and `/requests/{id}` routes).

## 1. Goal

Connect all disjointed pages in the application so a patient or staff member can navigate seamlessly in a browser, track past requests, view uploaded document statuses, and submit new requests without knowing raw URLs.

## 2. Scope

**In scope:**
- **Global Navigation Header (`app/templates/base.html`)**: Navigation bar rendered on all pages based on the user session role:
  - Patient: `[AgentCare Logo / Home]` | `[Dashboard]` | `[New Request]` | `[Log out]`
  - Staff: `[AgentCare Logo / Home]` | `[Staff Dashboard]` | `[Log out]`
  - Guest (unauthenticated): `[Login]` | `[Register]`
- **Patient Dashboard (`app/routes/dashboard_routes.py` & `app/templates/dashboard.html`)**:
  - Query all `WorkflowRun` entries for the logged-in patient, sorted by `created_at desc`.
  - Display a clean list of past requests: `Created At`, `Request Snippet`, `Uploaded File Count`, `Status`, and a link `[View Details]` to `/requests/{id}`.
  - Display a prominent button/link: **"+ Submit New Request"** targeting `/requests/new`.
- **Staff Dashboard (`app/routes/dashboard_routes.py` & `app/templates/staff_dashboard.html`)**:
  - Query `WorkflowRun` entries where `status == "needs_review"` (or escalated runs), sorted by `created_at desc`.
  - Render a list of requests needing staff attention with patient info and escalation reason snippet.
- **Request Status Page (`app/templates/request_status.html`)**:
  - Add a clear link: **"← Back to Dashboard"** (`/dashboard`).

**Explicitly out of scope:**
- CSS framework migration (Tailwind/Bootstrap) — vanilla HTML/CSS elements following `CLAUDE.md` guidelines.
- Intent branching clarify routes — handled by `2026-07-27-intent-branching-popup.md`.
- Reschedule/cancel routes — handled by a later phase.

## 3. Architecture

Modifies existing routes and templates:
- `app/routes/dashboard_routes.py`:
  - `patient_dashboard`: Fetches `PatientProfile` for current `user`, queries `WorkflowRun` rows for that patient, passes `workflow_runs` list to `dashboard.html`.
  - `staff_dashboard`: Queries `WorkflowRun` rows filtered by `status == WorkflowStatus.needs_review`, passes `escalated_runs` list to `staff_dashboard.html`.
- `app/templates/base.html`:
  - Receives `user` context variable (already passed by template responses or helper), renders header nav bar with conditional role links.
- `app/templates/dashboard.html`:
  - Render "+ Submit New Request" link and table/list of `workflow_runs`.
- `app/templates/staff_dashboard.html`:
  - Render table/list of `escalated_runs`.

## 4. Error Handling & Security

- RBAC: Existing `require_role(UserRole.patient.value)` and `require_role(UserRole.staff.value)` dependencies strictly control dashboard access.
- Ownership: Patient dashboard queries only `patient_id == profile.id`.
- Empty state: If no requests exist for a patient, display a friendly message: *"You haven't submitted any healthcare requests yet."* with a button to submit one.

## 5. Testing Plan

`tests/test_dashboard_routes.py`:
- `test_patient_dashboard_shows_request_history`: Patient with 2 workflow runs sees both runs on `/dashboard` with correct status text and links.
- `test_patient_dashboard_empty_state`: Fresh patient sees empty state message and link to `/requests/new`.
- `test_staff_dashboard_lists_needs_review_runs`: Staff member visiting `/staff/dashboard` sees escalated workflow runs.
- `test_unauthenticated_base_nav_renders_login_register`: Unauthenticated template render shows Login/Register links.
