import uuid

from sqlalchemy.orm import Session

from app.models import Appointment, AppointmentStatus, Department, Doctor, PatientDocument


def _missing_required_documents(db: Session, patient_id: str) -> list[str]:
    """Departments tied to this patient's active appointments, unioned
    required_document_types, minus the document_type values the patient
    already has on file. "Active" means status in
    (pending, confirmed, rescheduled) - i.e. everything except cancelled.
    rescheduled is deliberately included: book_or_modify_appointment's
    reschedule branch mutates the SAME appointment row (new slot/doctor)
    and sets status=rescheduled permanently, it never flips back to
    confirmed - so a rescheduled appointment is still an active, upcoming
    visit, not a cancelled one, and must still count. Shared by
    store_and_classify_document (below) and the Follow-up agent's scan
    (same gap, two different callers) - not duplicated logic."""
    patient_uuid = uuid.UUID(patient_id)
    # Note: no .distinct() here - Department.required_document_types is a
    # plain JSON column and Postgres has no equality operator for json (only
    # jsonb), so SELECT DISTINCT on a query touching that column raises
    # UndefinedFunction. Not needed anyway: duplicates collapse into the
    # `required` set below.
    departments = (
        db.query(Department)
        .join(Doctor, Doctor.department_id == Department.id)
        .join(Appointment, Appointment.doctor_id == Doctor.id)
        .filter(Appointment.patient_id == patient_uuid)
        .filter(
            Appointment.status.in_(
                [AppointmentStatus.pending, AppointmentStatus.confirmed, AppointmentStatus.rescheduled]
            )
        )
        .all()
    )
    required: set[str] = set()
    for department in departments:
        required.update(department.required_document_types or [])

    have = {
        doc.document_type.value
        for doc in db.query(PatientDocument).filter(PatientDocument.patient_id == patient_uuid).all()
    }
    return sorted(required - have)
