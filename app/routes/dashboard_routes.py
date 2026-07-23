from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.models import User, UserRole
from app.rbac import require_role

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/dashboard", response_class=HTMLResponse)
def patient_dashboard(request: Request, user: User = Depends(require_role(UserRole.patient.value))):
    return templates.TemplateResponse(request, "dashboard.html", {"user": user})


@router.get("/staff/dashboard", response_class=HTMLResponse)
def staff_dashboard(request: Request, user: User = Depends(require_role(UserRole.staff.value))):
    return templates.TemplateResponse(request, "staff_dashboard.html", {"user": user})
