import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from sqlalchemy.orm import Session

from app.audit import audited
from app.models import Appointment, AppointmentSlot, AppointmentStatus, Doctor, SlotStatus


def _appointment_dict(appointment: Appointment, slot: AppointmentSlot | None) -> dict:
    return {
        "id": str(appointment.id),
        "patient_id": str(appointment.patient_id),
        "doctor_id": str(appointment.doctor_id),
        "slot_id": str(appointment.slot_id),
        "status": appointment.status.value,
        "start_time": slot.start_time.isoformat() if slot else None,
        "end_time": slot.end_time.isoformat() if slot else None,
    }


@audited("check_slot_availability", "AppointmentSlot")
def check_slot_availability(db: Session, department_id: str, preferred_window: dict) -> list[dict]:
    now = datetime.now(timezone.utc)
    start_date = preferred_window.get("start_date")
    end_date = preferred_window.get("end_date")
    window_start = datetime.fromisoformat(start_date) if start_date else now
    window_end = datetime.fromisoformat(end_date) if end_date else now + timedelta(days=14)
    if window_start.tzinfo is None:
        window_start = window_start.replace(tzinfo=timezone.utc)
    if window_end.tzinfo is None:
        window_end = window_end.replace(tzinfo=timezone.utc)

    rows = (
        db.query(AppointmentSlot, Doctor)
        .join(Doctor, AppointmentSlot.doctor_id == Doctor.id)
        .filter(Doctor.department_id == uuid.UUID(department_id))
        .filter(Doctor.active.is_(True))
        .filter(AppointmentSlot.status == SlotStatus.open)
        .filter(AppointmentSlot.start_time >= window_start)
        .filter(AppointmentSlot.start_time <= window_end)
        .order_by(AppointmentSlot.start_time)
        .all()
    )
    return [
        {
            "slot_id": str(slot.id),
            "doctor_id": str(doctor.id),
            "doctor_name": doctor.name,
            "start_time": slot.start_time.isoformat(),
            "end_time": slot.end_time.isoformat(),
        }
        for slot, doctor in rows
    ]


def _conflicting_appointment(
    db: Session, patient_id: str, slot: AppointmentSlot, exclude_appointment_id: str | None = None
) -> Appointment | None:
    query = (
        db.query(Appointment)
        .join(AppointmentSlot, Appointment.slot_id == AppointmentSlot.id)
        .filter(Appointment.patient_id == uuid.UUID(patient_id))
        .filter(Appointment.status.in_([AppointmentStatus.pending, AppointmentStatus.confirmed]))
        .filter(AppointmentSlot.start_time < slot.end_time)
        .filter(AppointmentSlot.end_time > slot.start_time)
    )
    if exclude_appointment_id:
        query = query.filter(Appointment.id != uuid.UUID(exclude_appointment_id))
    return query.first()


@audited("book_or_modify_appointment", "Appointment")
def book_or_modify_appointment(
    db: Session,
    patient_id: str,
    slot_id: str,
    action: str,
    existing_appointment_id: str | None,
) -> dict:
    if action == "cancel":
        if not existing_appointment_id:
            return {"id": None, "status": "error", "error": "cancel requires existing_appointment_id"}
        appointment = db.query(Appointment).filter(Appointment.id == uuid.UUID(existing_appointment_id)).first()
        if appointment is None:
            return {"id": None, "status": "error", "error": f"Appointment {existing_appointment_id} not found"}
        old_slot = db.query(AppointmentSlot).filter(AppointmentSlot.id == appointment.slot_id).first()
        if old_slot is not None:
            old_slot.status = SlotStatus.open
        appointment.status = AppointmentStatus.cancelled
        db.commit()
        return _appointment_dict(appointment, old_slot)

    slot = db.query(AppointmentSlot).filter(AppointmentSlot.id == uuid.UUID(slot_id)).first()
    if slot is None:
        return {"id": None, "status": "error", "error": f"Slot {slot_id} not found"}
    if slot.status != SlotStatus.open:
        return {"id": None, "status": "error", "error": "Slot is no longer open"}

    if action == "book":
        conflict = _conflicting_appointment(db, patient_id, slot)
        if conflict is not None:
            return {
                "id": str(conflict.id),
                "status": "error",
                "error": "Patient already has a conflicting appointment",
            }

        slot.status = SlotStatus.booked
        appointment = Appointment(
            patient_id=uuid.UUID(patient_id),
            doctor_id=slot.doctor_id,
            slot_id=slot.id,
            status=AppointmentStatus.confirmed,
        )
        db.add(appointment)
        db.commit()
        return _appointment_dict(appointment, slot)

    if action == "reschedule":
        if not existing_appointment_id:
            return {"id": None, "status": "error", "error": "reschedule requires existing_appointment_id"}
        appointment = db.query(Appointment).filter(Appointment.id == uuid.UUID(existing_appointment_id)).first()
        if appointment is None:
            return {"id": None, "status": "error", "error": f"Appointment {existing_appointment_id} not found"}

        conflict = _conflicting_appointment(db, patient_id, slot, exclude_appointment_id=existing_appointment_id)
        if conflict is not None:
            return {
                "id": str(conflict.id),
                "status": "error",
                "error": "Patient already has a conflicting appointment",
            }

        old_slot = db.query(AppointmentSlot).filter(AppointmentSlot.id == appointment.slot_id).first()
        if old_slot is not None:
            old_slot.status = SlotStatus.open
        slot.status = SlotStatus.booked
        appointment.doctor_id = slot.doctor_id
        appointment.slot_id = slot.id
        appointment.status = AppointmentStatus.rescheduled
        db.commit()
        return _appointment_dict(appointment, slot)

    return {"id": None, "status": "error", "error": f"Unknown action: {action}"}


@tool(response_format="content_and_artifact")
def check_slot_availability_tool(
    preferred_window: dict,
    department_id: Annotated[str, InjectedState("department_id")],
    config: RunnableConfig,
):
    """Find open appointment slots for doctors in the patient's department.
    preferred_window may include start_date and/or end_date as YYYY-MM-DD
    strings if the patient mentioned a timeframe; pass {} for no preference
    (defaults to the next 14 days)."""
    db = config["configurable"]["db"]
    result = check_slot_availability(db, department_id, preferred_window)
    return f"Found {len(result)} open slot(s)", result


@tool(response_format="content_and_artifact")
def book_or_modify_appointment_tool(
    slot_id: str,
    action: str,
    existing_appointment_id: str | None,
    patient_id: Annotated[str, InjectedState("patient_id")],
    config: RunnableConfig,
):
    """Book, reschedule, or cancel an appointment. action must be one of
    "book", "reschedule", "cancel". slot_id is the id of the target slot
    from check_slot_availability's result (for "cancel", pass the existing
    appointment's current slot_id). existing_appointment_id is required for
    "reschedule"/"cancel" and must be omitted (pass null) for "book". If the
    result status is "error", pick a different slot and try again."""
    db = config["configurable"]["db"]
    result = book_or_modify_appointment(db, patient_id, slot_id, action, existing_appointment_id)
    return f"Appointment {action} result: {result['status']}", result
