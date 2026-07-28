import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from sqlalchemy.orm import Session

from app.audit import audited
from app.models import Appointment, AppointmentSlot, AppointmentStatus, Department, Doctor, SlotStatus


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


def appointment_display_details(db: Session, appointment_id: str) -> dict | None:
    """Real DB lookup: doctor name, department name, formatted time -
    shared by every place that needs to show a booked appointment (the
    dashboard's request list wants a compact line, the request-status page
    wants a full sentence) so the query itself isn't duplicated per caller,
    even though the wording each builds from it differs."""
    appointment = db.query(Appointment).filter(Appointment.id == uuid.UUID(appointment_id)).first()
    if appointment is None:
        return None
    doctor = db.query(Doctor).filter(Doctor.id == appointment.doctor_id).first()
    department = db.query(Department).filter(Department.id == doctor.department_id).first()
    slot = db.query(AppointmentSlot).filter(AppointmentSlot.id == appointment.slot_id).first()
    return {
        "doctor_name": doctor.name,
        "department_name": department.name,
        "formatted_time": slot.start_time.strftime("%B %d, %Y at %I:%M %p"),
    }


def list_patient_appointments(db: Session, patient_id: str) -> list[dict]:
    """Real DB query: this patient's own active appointments (not
    cancelled), enriched with doctor/department/time for display - same
    enrichment shape as appointment_display_details, but for a list instead
    of one row. Used to render the needs_appointment_selection screen; the
    patient picks by clicking, nothing here is a guess."""
    rows = (
        db.query(Appointment)
        .filter(Appointment.patient_id == uuid.UUID(patient_id))
        .filter(
            Appointment.status.in_(
                [AppointmentStatus.pending, AppointmentStatus.confirmed, AppointmentStatus.rescheduled]
            )
        )
        .all()
    )
    result = []
    for appointment in rows:
        details = appointment_display_details(db, str(appointment.id))
        if details:
            result.append({"appointment_id": str(appointment.id), **details})
    return sorted(result, key=lambda r: r["formatted_time"])


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
        .filter(
            Appointment.status.in_(
                [AppointmentStatus.pending, AppointmentStatus.confirmed, AppointmentStatus.rescheduled]
            )
        )
        .filter(AppointmentSlot.start_time < slot.end_time)
        .filter(AppointmentSlot.end_time > slot.start_time)
    )
    if exclude_appointment_id:
        query = query.filter(Appointment.id != uuid.UUID(exclude_appointment_id))
    return query.first()


def _parse_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None


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
        existing_uuid = _parse_uuid(existing_appointment_id)
        if existing_uuid is None:
            return {"id": None, "status": "error", "error": f"'{existing_appointment_id}' is not a valid appointment id"}
        appointment = db.query(Appointment).filter(Appointment.id == existing_uuid).first()
        if appointment is None:
            return {"id": None, "status": "error", "error": f"Appointment {existing_appointment_id} not found"}
        old_slot = db.query(AppointmentSlot).filter(AppointmentSlot.id == appointment.slot_id).first()
        if old_slot is not None:
            old_slot.status = SlotStatus.open
        appointment.status = AppointmentStatus.cancelled
        db.commit()
        return _appointment_dict(appointment, old_slot)

    slot_uuid = _parse_uuid(slot_id)
    if slot_uuid is None:
        return {"id": None, "status": "error", "error": f"'{slot_id}' is not a valid slot id"}
    slot = db.query(AppointmentSlot).filter(AppointmentSlot.id == slot_uuid).first()
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
        existing_uuid = _parse_uuid(existing_appointment_id)
        if existing_uuid is None:
            return {"id": None, "status": "error", "error": f"'{existing_appointment_id}' is not a valid appointment id"}
        appointment = db.query(Appointment).filter(Appointment.id == existing_uuid).first()
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


def _slots_summary(slots: list[dict]) -> str:
    # This is the only part of the tool result the model actually sees on
    # its next turn - `artifact` (the real dicts with real ids) never gets
    # sent back to the model, only `content` does. A bare count here gives
    # the model nothing to copy a real slot_id from, so it invents one -
    # confirmed against the real API: it hallucinated ids like "slot_123"
    # after seeing only "Found 2 open slot(s)". List the real data instead.
    if not slots:
        return "Found 0 open slot(s)."
    lines = [
        f"- slot_id={s['slot_id']} doctor={s['doctor_name']} start={s['start_time']}" for s in slots
    ]
    return f"Found {len(slots)} open slot(s):\n" + "\n".join(lines)


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
    return _slots_summary(result), result


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
    if result["status"] == "error":
        # Same content-vs-artifact gotcha: the model never sees `artifact`,
        # so without the real error text here it only knows the word
        # "error" - not *why* - and can't make a good choice about what to
        # try next (a different slot vs. checking an existing appointment).
        return f"Appointment {action} result: error - {result['error']}", result
    return f"Appointment {action} result: {result['status']}", result
