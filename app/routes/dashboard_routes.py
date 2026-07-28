from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import PatientProfile, User, UserRole, WorkflowRun, WorkflowStatus
from app.rbac import require_role
from app.tools.appointment_tools import appointment_display_details

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
