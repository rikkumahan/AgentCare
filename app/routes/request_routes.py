from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import PatientProfile, User, UserRole, WorkflowRun
from app.rbac import require_role
from app.workflow_runner import run_workflow

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


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


@router.get("/requests/new", response_class=HTMLResponse)
def new_request_form(request: Request, user: User = Depends(require_role(UserRole.patient.value))):
    return templates.TemplateResponse(request, "request_new.html", {"user": user})


@router.post("/requests/new")
def submit_request(
    request_text: str = Form(...),
    user: User = Depends(require_role(UserRole.patient.value)),
    db: Session = Depends(get_db),
):
    profile = _get_or_create_profile(db, user)
    workflow_run = run_workflow(
        db,
        patient_id=str(profile.id),
        user_id=str(user.id),
        request_text=request_text,
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

    return templates.TemplateResponse(request, "request_status.html", {"user": user, "workflow_run": workflow_run})
