import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Appointment, PatientProfile, User, UserRole, WorkflowRun, WorkflowStatus
from app.rbac import require_role
from app.tools.appointment_tools import (
    appointment_display_details,
    list_active_appointments_grouped_by_doctor,
    list_patient_appointments,
)
from app.workflow_runner import start_appointment_action

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _get_or_create_profile(db: Session, user: User) -> PatientProfile:
    profile = db.query(PatientProfile).filter(PatientProfile.user_id == user.id).first()
    if profile is None:
        profile = PatientProfile(user_id=user.id)
        db.add(profile)
        db.commit()
    return profile


@router.get("/dashboard", response_class=HTMLResponse)
def patient_dashboard(
    request: Request,
    user: User = Depends(require_role(UserRole.patient.value)),
    db: Session = Depends(get_db),
):
    profile = _get_or_create_profile(db, user)
    workflow_runs = (
        db.query(WorkflowRun)
        .filter(WorkflowRun.patient_id == profile.id)
        .order_by(WorkflowRun.created_at.desc())
        .all()
    )
    appointment_details = {
        str(run.id): appointment_display_details(db, run.state["appointment_id"])
        for run in workflow_runs
        if run.state.get("appointment_id")
    }
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"user": user, "workflow_runs": workflow_runs, "appointment_details": appointment_details},
    )


@router.get("/appointments", response_class=HTMLResponse)
def my_appointments(
    request: Request,
    user: User = Depends(require_role(UserRole.patient.value)),
    db: Session = Depends(get_db),
):
    profile = _get_or_create_profile(db, user)
    appointments = list_patient_appointments(db, str(profile.id))
    return templates.TemplateResponse(
        request, "my_appointments.html", {"user": user, "appointments": appointments}
    )


def _owned_active_appointment_or_404(db: Session, profile: PatientProfile, appointment_id: str) -> Appointment:
    appointment = db.query(Appointment).filter(Appointment.id == uuid.UUID(appointment_id)).first()
    if appointment is None:
        raise HTTPException(status_code=404, detail="Not found")
    if appointment.patient_id != profile.id:
        raise HTTPException(status_code=403, detail="Not your appointment")
    return appointment


@router.post("/appointments/{appointment_id}/cancel")
def cancel_appointment_from_list(
    appointment_id: str,
    user: User = Depends(require_role(UserRole.patient.value)),
    db: Session = Depends(get_db),
):
    profile = _get_or_create_profile(db, user)
    _owned_active_appointment_or_404(db, profile, appointment_id)
    workflow_run = start_appointment_action(db, str(profile.id), "cancel", appointment_id, user_id=str(user.id))
    return RedirectResponse(f"/requests/{workflow_run.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/appointments/{appointment_id}/reschedule")
def reschedule_appointment_from_list(
    appointment_id: str,
    user: User = Depends(require_role(UserRole.patient.value)),
    db: Session = Depends(get_db),
):
    profile = _get_or_create_profile(db, user)
    _owned_active_appointment_or_404(db, profile, appointment_id)
    workflow_run = start_appointment_action(db, str(profile.id), "reschedule", appointment_id, user_id=str(user.id))
    return RedirectResponse(f"/requests/{workflow_run.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/staff/dashboard", response_class=HTMLResponse)
def staff_dashboard(
    request: Request,
    user: User = Depends(require_role(UserRole.staff.value)),
    db: Session = Depends(get_db),
):
    escalated_runs = (
        db.query(WorkflowRun)
        .filter(WorkflowRun.status == WorkflowStatus.needs_review)
        .order_by(WorkflowRun.created_at.desc())
        .all()
    )
    return templates.TemplateResponse(
        request, "staff_dashboard.html", {"user": user, "escalated_runs": escalated_runs}
    )


@router.get("/staff/appointments", response_class=HTMLResponse)
def staff_appointments(
    request: Request,
    user: User = Depends(require_role(UserRole.staff.value)),
    db: Session = Depends(get_db),
):
    grouped = list_active_appointments_grouped_by_doctor(db)
    return templates.TemplateResponse(
        request, "staff_appointments.html", {"user": user, "grouped": grouped}
    )
