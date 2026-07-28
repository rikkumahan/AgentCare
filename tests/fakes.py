import uuid
from datetime import datetime, timedelta, timezone

from langchain_core.messages import AIMessage

from app.models import (
    Appointment,
    AppointmentSlot,
    AppointmentStatus,
    Department,
    Doctor,
    PatientProfile,
    SlotStatus,
    User,
    UserRole,
    WorkflowRun,
)


def make_user(db_session, role=UserRole.patient) -> User:
    user = User(
        name="Test Patient",
        email=f"test-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="irrelevant-hash",
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    return user


def make_patient_profile(db_session, user: User | None = None) -> PatientProfile:
    if user is None:
        user = make_user(db_session)
    profile = PatientProfile(user_id=user.id)
    db_session.add(profile)
    db_session.commit()
    return profile


def make_department(db_session, name: str | None = None, active: bool = True) -> Department:
    department = Department(
        name=name or f"Dept-{uuid.uuid4().hex[:8]}",
        description="Test department",
        active=active,
    )
    db_session.add(department)
    db_session.commit()
    return department


def make_doctor(db_session, department: Department | None = None, active: bool = True) -> Doctor:
    if department is None:
        department = make_department(db_session)
    doctor = Doctor(department_id=department.id, name=f"Dr. Test-{uuid.uuid4().hex[:8]}", active=active)
    db_session.add(doctor)
    db_session.commit()
    return doctor


def make_appointment_slot(
    db_session,
    doctor: Doctor | None = None,
    start_time: datetime | None = None,
    status: SlotStatus = SlotStatus.open,
) -> AppointmentSlot:
    if doctor is None:
        doctor = make_doctor(db_session)
    start_time = start_time or (datetime.now(timezone.utc) + timedelta(days=1))
    slot = AppointmentSlot(
        doctor_id=doctor.id,
        start_time=start_time,
        end_time=start_time + timedelta(minutes=30),
        status=status,
    )
    db_session.add(slot)
    db_session.commit()
    return slot


def make_appointment(
    db_session,
    patient: PatientProfile | None = None,
    doctor: Doctor | None = None,
    slot: AppointmentSlot | None = None,
    status: AppointmentStatus = AppointmentStatus.confirmed,
) -> Appointment:
    if patient is None:
        patient = make_patient_profile(db_session)
    if doctor is None:
        doctor = make_doctor(db_session)
    if slot is None:
        slot = make_appointment_slot(db_session, doctor=doctor)
    appointment = Appointment(
        patient_id=patient.id,
        doctor_id=doctor.id,
        slot_id=slot.id,
        status=status,
    )
    db_session.add(appointment)
    db_session.commit()
    return appointment


def make_workflow_run(db_session, profile: PatientProfile | None = None) -> WorkflowRun:
    if profile is None:
        profile = make_patient_profile(db_session)
    workflow_run = WorkflowRun(patient_id=profile.id)
    db_session.add(workflow_run)
    db_session.commit()
    return workflow_run


def workflow_state(**overrides) -> dict:
    state = {
        "workflow_run_id": "11111111-1111-1111-1111-111111111111",
        "patient_id": "22222222-2222-2222-2222-222222222222",
        "user_id": "u1",
        "request_text": "book a cardiology appointment",
        "uploaded_files": [],
        "intent": None,
        "department_id": None,
        "appointment_id": None,
        "document_ids": [],
        "missing_document_types": [],
        "reminder_ids": [],
        "escalation": None,
        "status": "running",
        "needs_clarification": False,
        "needs_appointment_reason": False,
        "remaining_intents": [],
    }
    state.update(overrides)
    return state



class FakeToolCallingModel:
    """Stands in for a ChatGroq model bound to tools: .bind_tools() is a
    no-op returning self, .invoke() returns the next scripted response."""

    def __init__(self, responses: list[AIMessage]):
        self._responses = list(responses)

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        return self._responses.pop(0)


def ai_message_with_tool_call(name: str, args: dict, call_id: str = "call_1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])


def ai_message_text(text: str) -> AIMessage:
    return AIMessage(content=text)
