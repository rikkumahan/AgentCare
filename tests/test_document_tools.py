import uuid

from app.models import AppointmentStatus
from app.tools.document_tools import _missing_required_documents
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
