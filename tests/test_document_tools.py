import uuid

import hashlib

from app.models import AppointmentStatus, AuditEvent, DocumentType, PatientDocument
from app.tools.document_tools import (
    _checksum_file,
    _document_summary,
    _missing_required_documents,
    store_and_classify_document,
)
from tests.fakes import make_appointment, make_department, make_doctor, make_patient_profile


def test_missing_required_documents_flags_gap_for_confirmed_cardiology_appointment(db_session):
    department = make_department(db_session, name=f"Cardiology {uuid.uuid4().hex[:8]}")
    department.required_document_types = ["ecg"]
    db_session.commit()
    doctor = make_doctor(db_session, department=department)
    profile = make_patient_profile(db_session)
    make_appointment(db_session, patient=profile, doctor=doctor, status=AppointmentStatus.confirmed)

    result = _missing_required_documents(db_session, str(profile.id))

    assert result == ["ecg"]


def test_missing_required_documents_returns_empty_for_patient_with_no_appointments(db_session):
    profile = make_patient_profile(db_session)

    result = _missing_required_documents(db_session, str(profile.id))

    assert result == []


def test_missing_required_documents_ignores_a_cancelled_appointment(db_session):
    # Regression test for the cross-check bug: a cancelled appointment's
    # department must not keep flagging the patient for paperwork tied to
    # a visit that isn't happening.
    department = make_department(db_session, name=f"Cardiology {uuid.uuid4().hex[:8]}")
    department.required_document_types = ["ecg"]
    db_session.commit()
    doctor = make_doctor(db_session, department=department)
    profile = make_patient_profile(db_session)
    make_appointment(db_session, patient=profile, doctor=doctor, status=AppointmentStatus.cancelled)

    result = _missing_required_documents(db_session, str(profile.id))

    assert result == []


def test_missing_required_documents_still_flags_gap_for_rescheduled_appointment(db_session):
    # Regression test for a second cross-check bug: "rescheduled" is NOT a
    # cancelled/inactive status - book_or_modify_appointment's reschedule
    # branch mutates the SAME row's slot/doctor and sets status=rescheduled
    # permanently, it never flips back to confirmed. A rescheduled
    # appointment is still an active, upcoming visit and must still count.
    department = make_department(db_session, name=f"Cardiology {uuid.uuid4().hex[:8]}")
    department.required_document_types = ["ecg"]
    db_session.commit()
    doctor = make_doctor(db_session, department=department)
    profile = make_patient_profile(db_session)
    make_appointment(db_session, patient=profile, doctor=doctor, status=AppointmentStatus.rescheduled)

    result = _missing_required_documents(db_session, str(profile.id))

    assert result == ["ecg"]


def test_checksum_file_returns_sha256_of_real_bytes(tmp_path):
    file_path = tmp_path / "sample.txt"
    file_path.write_bytes(b"hello world")

    checksum = _checksum_file(str(file_path))

    assert checksum == hashlib.sha256(b"hello world").hexdigest()


def test_store_and_classify_document_saves_new_document(tmp_path, db_session):
    profile = make_patient_profile(db_session)
    file_path = tmp_path / "ecg_scan.pdf"
    file_path.write_bytes(b"ecg-bytes-1")

    result = store_and_classify_document(db_session, str(profile.id), str(file_path), "ecg")

    assert result["status"] == "saved"
    assert result["document_type"] == "ecg"
    document = db_session.query(PatientDocument).filter(PatientDocument.id == uuid.UUID(result["id"])).one()
    assert document.patient_id == profile.id
    assert document.document_type == DocumentType.ecg


def test_store_and_classify_document_detects_duplicate_by_content_not_filename(tmp_path, db_session):
    profile = make_patient_profile(db_session)
    file_a = tmp_path / "first.pdf"
    file_a.write_bytes(b"same-bytes")
    file_b = tmp_path / "second.pdf"
    file_b.write_bytes(b"same-bytes")

    first = store_and_classify_document(db_session, str(profile.id), str(file_a), "lab_report")
    second = store_and_classify_document(db_session, str(profile.id), str(file_b), "lab_report")

    assert first["status"] == "saved"
    assert second["status"] == "duplicate"
    assert second["id"] == first["id"]
    count = db_session.query(PatientDocument).filter(PatientDocument.patient_id == profile.id).count()
    assert count == 1


def test_store_and_classify_document_different_patients_same_bytes_each_get_own_row(tmp_path, db_session):
    profile_a = make_patient_profile(db_session)
    profile_b = make_patient_profile(db_session)
    file_a = tmp_path / "a.pdf"
    file_a.write_bytes(b"shared-bytes")
    file_b = tmp_path / "b.pdf"
    file_b.write_bytes(b"shared-bytes")

    result_a = store_and_classify_document(db_session, str(profile_a.id), str(file_a), "insurance")
    result_b = store_and_classify_document(db_session, str(profile_b.id), str(file_b), "insurance")

    assert result_a["status"] == "saved"
    assert result_b["status"] == "saved"
    assert result_a["id"] != result_b["id"]


def test_store_and_classify_document_falls_back_to_other_for_unknown_type(tmp_path, db_session):
    profile = make_patient_profile(db_session)
    file_path = tmp_path / "mystery.bin"
    file_path.write_bytes(b"mystery-bytes")

    result = store_and_classify_document(db_session, str(profile.id), str(file_path), "not_a_real_type")

    assert result["status"] == "saved"
    assert result["document_type"] == "other"


def test_store_and_classify_document_rejects_missing_file(db_session):
    profile = make_patient_profile(db_session)

    result = store_and_classify_document(db_session, str(profile.id), "/no/such/file.pdf", "ecg")

    assert result["status"] == "error"
    assert result["id"] is None


def test_store_and_classify_document_rejects_empty_file(tmp_path, db_session):
    profile = make_patient_profile(db_session)
    file_path = tmp_path / "empty.pdf"
    file_path.write_bytes(b"")

    result = store_and_classify_document(db_session, str(profile.id), str(file_path), "ecg")

    assert result["status"] == "error"


def test_store_and_classify_document_writes_audit_event(tmp_path, db_session):
    profile = make_patient_profile(db_session)
    file_path = tmp_path / "audit.pdf"
    file_path.write_bytes(b"audit-bytes")

    store_and_classify_document(db_session, str(profile.id), str(file_path), "ecg")

    audit_actions = {e.action for e in db_session.query(AuditEvent).all()}
    assert "store_and_classify_document" in audit_actions


def test_store_and_classify_document_reports_missing_types_alongside_save(tmp_path, db_session):
    department = make_department(db_session, name=f"Cardiology {uuid.uuid4().hex[:8]}")
    department.required_document_types = ["ecg", "insurance"]
    db_session.commit()
    doctor = make_doctor(db_session, department=department)
    profile = make_patient_profile(db_session)
    make_appointment(db_session, patient=profile, doctor=doctor, status=AppointmentStatus.confirmed)
    file_path = tmp_path / "insurance_card.pdf"
    file_path.write_bytes(b"insurance-bytes")

    result = store_and_classify_document(db_session, str(profile.id), str(file_path), "insurance")

    assert result["status"] == "saved"
    assert result["missing_document_types"] == ["ecg"]


def test_document_summary_includes_real_status_and_missing_types_not_a_bare_word():
    # This string is the only part of the tool result the model actually
    # sees on its next turn - artifact never gets serialized back into the
    # conversation. A bare status word gives the model nothing to act on,
    # same content-vs-artifact lesson as _slots_summary/_departments_summary.
    result = {
        "id": "doc-1",
        "status": "saved",
        "document_type": "ecg",
        "missing_document_types": ["insurance"],
    }

    summary = _document_summary(result)

    assert "saved" in summary
    assert "ecg" in summary
    assert "insurance" in summary


def test_document_summary_handles_duplicate_status():
    result = {"id": "doc-1", "status": "duplicate", "document_type": "ecg", "missing_document_types": []}

    summary = _document_summary(result)

    assert "duplicate" in summary


def test_document_summary_handles_error_status():
    result = {"id": None, "status": "error", "error": "File not found: /no/such/file.pdf"}

    summary = _document_summary(result)

    assert "error" in summary
    assert "File not found" in summary
