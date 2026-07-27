import hashlib
import os
import uuid
from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from sqlalchemy.orm import Session

from app.audit import audited
from app.models import Appointment, AppointmentStatus, Department, Doctor, DocumentType, PatientDocument


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


def _checksum_file(file_path: str) -> str:
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


@audited("store_and_classify_document", "PatientDocument")
def store_and_classify_document(db: Session, patient_id: str, file_path: str, document_type: str) -> dict:
    if not os.path.isfile(file_path):
        return {"id": None, "status": "error", "error": f"File not found: {file_path}"}
    if os.path.getsize(file_path) == 0:
        return {"id": None, "status": "error", "error": f"File is empty: {file_path}"}

    checksum = _checksum_file(file_path)
    patient_uuid = uuid.UUID(patient_id)

    existing = (
        db.query(PatientDocument)
        .filter(PatientDocument.patient_id == patient_uuid)
        .filter(PatientDocument.checksum == checksum)
        .first()
    )
    if existing is not None:
        return {
            "id": str(existing.id),
            "status": "duplicate",
            "document_type": existing.document_type.value,
            "missing_document_types": _missing_required_documents(db, patient_id),
        }

    try:
        doc_type_enum = DocumentType(document_type)
    except ValueError:
        # The model could in principle send something off the fixed list -
        # fall back rather than crash the whole graph run over a
        # classification quibble.
        doc_type_enum = DocumentType.other

    document = PatientDocument(
        patient_id=patient_uuid,
        document_type=doc_type_enum,
        file_path=file_path,
        checksum=checksum,
    )
    db.add(document)
    db.commit()
    return {
        "id": str(document.id),
        "status": "saved",
        "document_type": document.document_type.value,
        "missing_document_types": _missing_required_documents(db, patient_id),
    }


def _document_summary(result: dict) -> str:
    # Same content-vs-artifact gotcha documented in docs/memory/gotchas.md:
    # only `content` survives into the model's next turn - a bare status
    # word gives it nothing to act on when deciding whether to reply or
    # mention missing paperwork.
    if result["status"] == "error":
        return f"Document store result: error - {result['error']}"
    missing = result.get("missing_document_types") or []
    summary = f"Document store result: {result['status']} as {result['document_type']}."
    if missing:
        summary += f" Missing document types still needed: {', '.join(missing)}."
    return summary


@tool(response_format="content_and_artifact")
def store_and_classify_document_tool(
    file_path: str,
    document_type: str,
    patient_id: Annotated[str, InjectedState("patient_id")],
    config: RunnableConfig,
):
    """Save and classify a document the patient attached. document_type
    must be one of: ecg, lab_report, prescription_old, insurance, id_proof,
    other - pick the best fit based on the filename and any note the
    patient wrote in their request."""
    db = config["configurable"]["db"]
    result = store_and_classify_document(db, patient_id, file_path, document_type)
    return _document_summary(result), result
