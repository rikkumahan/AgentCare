import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import Department, PatientProfile, User, UserRole, WorkflowRun, WorkflowStatus
from app.rbac import require_role
from app.tools.appointment_tools import (
    appointment_display_details,
    check_slot_availability,
    list_patient_appointments,
)
from app.workflow_runner import (
    continue_as_appointment_action,
    continue_as_booking,
    continue_as_booking_with_department,
    continue_as_intent_selection,
    continue_as_staff_escalation,
    continue_with_selected_slot,
    run_workflow,
)


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# A double-click, a slow page prompting a repeat click, or a browser
# resubmitting a POST on refresh must never create a second real booking
# for the same request - confirmed as a real risk, not hypothetical.
DUPLICATE_SUBMIT_WINDOW_SECONDS = 15


def _get_or_create_profile(db: Session, user: User) -> PatientProfile:
    """Route-level identity resolution — not the audited get_or_create_patient
    tool, which is reserved for real agent tool calls. Calling the audited
    version here would log an AuditEvent on every page view, misrepresenting
    the audit trail as agent activity."""
    profile = db.query(PatientProfile).filter(PatientProfile.user_id == user.id).first()
    if profile is None:
        profile = PatientProfile(user_id=user.id)
        db.add(profile)
        db.commit()
    return profile


def _render_patient_message(db: Session, user: User, workflow_run: WorkflowRun) -> str:
    """Plain Python, not an LLM call - built from real persisted rows, same
    principle as CLAUDE.md's "no hardcoded final responses": the confirmation
    text is rendered from rows just read back from the database, never a
    free-standing LLM string asserting success. Never exposes raw escalation
    reasons or ids to the patient - those stay in the DB/audit trail for staff."""
    state = workflow_run.state or {}
    workflow_status = workflow_run.status

    if workflow_status == WorkflowStatus.needs_intent_selection:
        if "," in state.get("intent", ""):
            message = "It sounds like you're asking about a few things. Which one should I help with first?"
        else:
            message = "Got it. Now let's take care of the rest of your request — which one's next?"

    elif workflow_status == WorkflowStatus.needs_clarification:
        message = f"Hi {user.name}! I want to make sure I help you with the right thing."

    elif workflow_status == WorkflowStatus.needs_appointment_selection:
        message = "Which appointment is this about?"
    elif workflow_status == WorkflowStatus.needs_appointment_reason:
        message = "What's this appointment for? Pick a department below, or describe it in your own words."
    elif workflow_status == WorkflowStatus.needs_slot_selection:
        message = "Here are the real open times - pick whichever works for you."
    elif workflow_status == WorkflowStatus.completed:
        pending_action = state.get("pending_appointment_action")
        appointment_id = state.get("appointment_id")
        details = appointment_display_details(db, appointment_id) if appointment_id else None

        if pending_action == "cancel":
            if details:
                message = (
                    f"Your appointment with {details['doctor_name']} "
                    f"in {details['department_name']} on {details['formatted_time']} has been cancelled."
                )
            else:
                message = "You don't have any upcoming appointments to cancel."
        elif pending_action == "reschedule":
            if details:
                message = (
                    f"Done! Your appointment is now with {details['doctor_name']} "
                    f"in {details['department_name']} on {details['formatted_time']}."
                )
            else:
                message = "You don't have any upcoming appointments to reschedule."
        elif details:
            message = (
                f"Great news, {user.name}! You're booked with {details['doctor_name']} "
                f"in {details['department_name']} on {details['formatted_time']}."
            )
        else:
            message = "I couldn't find any open slots right now. Please try again later or contact our staff."
    elif workflow_status == WorkflowStatus.needs_review:
        message = "I've passed your request to our staff team - they'll follow up with you soon."
    elif workflow_status == WorkflowStatus.failed:
        message = (
            "Something went wrong on our end while handling your request. Please try again, or contact our staff directly."
        )
    else:
        message = "I'm still working on this - check back in a moment."

    if state.get("document_ids"):
        message += " I've also saved your uploaded document."

    return message


@router.get("/requests/new", response_class=HTMLResponse)
def new_request_form(request: Request, user: User = Depends(require_role(UserRole.patient.value))):
    return templates.TemplateResponse(request, "request_new.html", {"user": user})


@router.post("/requests/new")
def submit_request(
    request_text: str = Form(...),
    document: UploadFile | None = File(None),
    user: User = Depends(require_role(UserRole.patient.value)),
    db: Session = Depends(get_db),
):
    profile = _get_or_create_profile(db, user)

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=DUPLICATE_SUBMIT_WINDOW_SECONDS)
    recent = (
        db.query(WorkflowRun)
        .filter(WorkflowRun.patient_id == profile.id)
        .filter(WorkflowRun.created_at >= cutoff)
        .order_by(WorkflowRun.created_at.desc())
        .first()
    )
    if recent is not None and recent.state.get("request_text") == request_text:
        # Same patient, same exact text, within the window - treat as a
        # duplicate submission and show the existing run instead of
        # starting a second real workflow (and possibly a second real
        # booking) for what is almost certainly one intended request.
        return RedirectResponse(f"/requests/{recent.id}", status_code=status.HTTP_303_SEE_OTHER)

    uploaded_files: list[str] = []
    if document is not None and document.filename:
        patient_dir = os.path.join(settings.storage_dir, str(profile.id))
        os.makedirs(patient_dir, exist_ok=True)
        safe_filename = os.path.basename(document.filename)
        saved_path = os.path.join(patient_dir, f"{uuid.uuid4().hex}_{safe_filename}")
        with open(saved_path, "wb") as f:
            f.write(document.file.read())
        uploaded_files = [saved_path]

    workflow_run = run_workflow(
        db,
        patient_id=str(profile.id),
        user_id=str(user.id),
        request_text=request_text,
        uploaded_files=uploaded_files,
    )
    return RedirectResponse(f"/requests/{workflow_run.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/requests/{workflow_run_id}", response_class=HTMLResponse)
def request_status(
    request: Request,
    workflow_run_id: str,
    user: User = Depends(require_role(UserRole.patient.value)),
    db: Session = Depends(get_db),
):
    workflow_run = db.get(WorkflowRun, workflow_run_id)
    if workflow_run is None:
        raise HTTPException(status_code=404, detail="Not found")

    profile = _get_or_create_profile(db, user)
    if workflow_run.patient_id != profile.id:
        raise HTTPException(status_code=403, detail="Not your request")

    patient_message = _render_patient_message(db, user, workflow_run)
    appointments = []
    departments = []
    slots = []
    detected_intents = []
    if workflow_run.status == WorkflowStatus.needs_intent_selection:
        raw_intent = workflow_run.state.get("intent", "")
        actionable = [
            label.strip()
            for label in raw_intent.split(",")
            if any(kw in label for kw in ("book", "cancel", "reschedule"))
        ]
        detected_intents = actionable
    elif workflow_run.status == WorkflowStatus.needs_appointment_selection:
        appointments = list_patient_appointments(db, str(profile.id))
    elif workflow_run.status == WorkflowStatus.needs_appointment_reason:
        departments = db.query(Department).filter(Department.active.is_(True)).order_by(Department.name).all()
    elif workflow_run.status == WorkflowStatus.needs_slot_selection:
        department_id = workflow_run.state.get("department_id")
        raw_slots = check_slot_availability(db, department_id, {})
        for s in raw_slots:
            s["formatted_time"] = datetime.fromisoformat(s["start_time"]).strftime("%B %d, %Y at %I:%M %p")
        slots = raw_slots
    return templates.TemplateResponse(
        request,
        "request_status.html",
        {
            "user": user,
            "workflow_run": workflow_run,
            "patient_message": patient_message,
            "appointments": appointments,
            "departments": departments,
            "slots": slots,
            "detected_intents": detected_intents,
        },
    )


@router.post("/requests/{workflow_run_id}/select-intent")
def select_intent(
    workflow_run_id: str,
    intent: str = Form(...),
    user: User = Depends(require_role(UserRole.patient.value)),
    db: Session = Depends(get_db),
):
    workflow_run = db.get(WorkflowRun, workflow_run_id)
    if workflow_run is None:
        raise HTTPException(status_code=404, detail="Not found")

    profile = _get_or_create_profile(db, user)
    if workflow_run.patient_id != profile.id:
        raise HTTPException(status_code=403, detail="Not your request")

    if workflow_run.status != WorkflowStatus.needs_intent_selection:
        return RedirectResponse(f"/requests/{workflow_run_id}", status_code=status.HTTP_303_SEE_OTHER)

    continue_as_intent_selection(db, workflow_run, intent)

    return RedirectResponse(f"/requests/{workflow_run_id}", status_code=status.HTTP_303_SEE_OTHER)



@router.post("/requests/{workflow_run_id}/clarify")
def clarify_request(
    workflow_run_id: str,
    choice: str = Form(...),
    user: User = Depends(require_role(UserRole.patient.value)),
    db: Session = Depends(get_db),
):
    workflow_run = db.get(WorkflowRun, workflow_run_id)
    if workflow_run is None:
        raise HTTPException(status_code=404, detail="Not found")

    profile = _get_or_create_profile(db, user)
    if workflow_run.patient_id != profile.id:
        raise HTTPException(status_code=403, detail="Not your request")

    if workflow_run.status != WorkflowStatus.needs_clarification:
        # Stale click (already resolved by an earlier click, a second tab,
        # or a double-submit) - same no-op-redirect philosophy as the
        # existing duplicate-submission guard on POST /requests/new, not an
        # error.
        return RedirectResponse(f"/requests/{workflow_run_id}", status_code=status.HTTP_303_SEE_OTHER)

    if choice == "book_appointment":
        # Don't book yet - the original request_text that triggered
        # needs_clarification may have had nothing routable in it (e.g.
        # "what are your visiting hours"). Ask what the appointment is for
        # first; POST /select-reason below runs the actual booking attempt
        # once we have a real answer to route on.
        workflow_run.status = WorkflowStatus.needs_appointment_reason
        workflow_run.current_step = "needs_appointment_reason"
        state = dict(workflow_run.state)
        state["status"] = workflow_run.status.value
        workflow_run.state = state
        db.commit()
    elif choice == "staff":
        request_text = workflow_run.state.get("request_text")
        continue_as_staff_escalation(
            db, workflow_run, f"Patient asked for help with an unclear request: {request_text!r}"
        )
    else:
        raise HTTPException(status_code=400, detail="Unknown choice")

    return RedirectResponse(f"/requests/{workflow_run_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/requests/{workflow_run_id}/select-appointment")
def select_appointment(
    workflow_run_id: str,
    appointment_id: str = Form(...),
    user: User = Depends(require_role(UserRole.patient.value)),
    db: Session = Depends(get_db),
):
    workflow_run = db.get(WorkflowRun, workflow_run_id)
    if workflow_run is None:
        raise HTTPException(status_code=404, detail="Not found")

    profile = _get_or_create_profile(db, user)
    if workflow_run.patient_id != profile.id:
        raise HTTPException(status_code=403, detail="Not your request")

    if workflow_run.status != WorkflowStatus.needs_appointment_selection:
        return RedirectResponse(f"/requests/{workflow_run_id}", status_code=status.HTTP_303_SEE_OTHER)

    continue_as_appointment_action(db, workflow_run, appointment_id)

    return RedirectResponse(f"/requests/{workflow_run_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/requests/{workflow_run_id}/select-reason")
def select_appointment_reason(
    workflow_run_id: str,
    department_id: str | None = Form(None),
    reason_text: str | None = Form(None),
    user: User = Depends(require_role(UserRole.patient.value)),
    db: Session = Depends(get_db),
):
    workflow_run = db.get(WorkflowRun, workflow_run_id)
    if workflow_run is None:
        raise HTTPException(status_code=404, detail="Not found")

    profile = _get_or_create_profile(db, user)
    if workflow_run.patient_id != profile.id:
        raise HTTPException(status_code=403, detail="Not your request")

    if workflow_run.status != WorkflowStatus.needs_appointment_reason:
        # Stale click - same no-op-redirect philosophy as /clarify above.
        return RedirectResponse(f"/requests/{workflow_run_id}", status_code=status.HTTP_303_SEE_OTHER)

    if department_id:
        department = db.get(Department, uuid.UUID(department_id))
        if department is None or not department.active:
            raise HTTPException(status_code=400, detail="Unknown department")
        continue_as_booking_with_department(db, workflow_run, department_id)
    elif reason_text and reason_text.strip():
        continue_as_booking(db, workflow_run, override_request_text=reason_text.strip())
    else:
        raise HTTPException(status_code=400, detail="Pick a department or describe the appointment")

    return RedirectResponse(f"/requests/{workflow_run_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/requests/{workflow_run_id}/select-slot")
def select_slot(
    workflow_run_id: str,
    slot_id: str = Form(...),
    user: User = Depends(require_role(UserRole.patient.value)),
    db: Session = Depends(get_db),
):
    workflow_run = db.get(WorkflowRun, workflow_run_id)
    if workflow_run is None:
        raise HTTPException(status_code=404, detail="Not found")

    profile = _get_or_create_profile(db, user)
    if workflow_run.patient_id != profile.id:
        raise HTTPException(status_code=403, detail="Not your request")

    if workflow_run.status != WorkflowStatus.needs_slot_selection:
        # Stale click - same no-op-redirect philosophy as /clarify above.
        return RedirectResponse(f"/requests/{workflow_run_id}", status_code=status.HTTP_303_SEE_OTHER)

    continue_with_selected_slot(db, workflow_run, slot_id)

    return RedirectResponse(f"/requests/{workflow_run_id}", status_code=status.HTTP_303_SEE_OTHER)
