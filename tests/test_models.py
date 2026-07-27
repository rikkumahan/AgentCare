import uuid

from app.models import Appointment, AppointmentStatus, Department, Doctor
from tests.fakes import make_appointment, make_appointment_slot, make_doctor, make_patient_profile


def test_create_department_and_doctor(db_session):
    dept = Department(name=f"Cardiology-{uuid.uuid4().hex[:8]}", description="Heart care", active=True)
    db_session.add(dept)
    db_session.flush()

    doctor = Doctor(department_id=dept.id, name="Dr. Test", active=True)
    db_session.add(doctor)
    db_session.commit()

    fetched = db_session.get(Doctor, doctor.id)
    assert fetched is not None
    assert fetched.department_id == dept.id
    assert fetched.active is True


def test_department_required_document_types_defaults_to_empty_list(db_session):
    dept = Department(name=f"General-{uuid.uuid4().hex[:8]}", description="General care", active=True)
    db_session.add(dept)
    db_session.commit()

    fetched = db_session.get(Department, dept.id)
    assert fetched.required_document_types == []


def test_department_required_document_types_can_be_set(db_session):
    dept = Department(
        name=f"Cardiology-{uuid.uuid4().hex[:8]}",
        description="Heart care",
        active=True,
        required_document_types=["ecg"],
    )
    db_session.add(dept)
    db_session.commit()

    fetched = db_session.get(Department, dept.id)
    assert fetched.required_document_types == ["ecg"]


def test_make_appointment_with_all_defaults(db_session):
    appointment = make_appointment(db_session)
    assert appointment.id is not None
    assert appointment.patient_id is not None
    assert appointment.doctor_id is not None
    assert appointment.slot_id is not None
    assert appointment.status == AppointmentStatus.confirmed

    fetched = db_session.get(Appointment, appointment.id)
    assert fetched is not None
    assert fetched.status == AppointmentStatus.confirmed


def test_make_appointment_with_custom_patient_and_status(db_session):
    patient = make_patient_profile(db_session)
    appointment = make_appointment(db_session, patient=patient, status=AppointmentStatus.pending)
    assert appointment.patient_id == patient.id
    assert appointment.status == AppointmentStatus.pending

    fetched = db_session.get(Appointment, appointment.id)
    assert fetched is not None
    assert fetched.patient_id == patient.id
    assert fetched.status == AppointmentStatus.pending


def test_make_appointment_with_custom_slot(db_session):
    doctor = make_doctor(db_session)
    slot = make_appointment_slot(db_session, doctor=doctor)
    appointment = make_appointment(db_session, doctor=doctor, slot=slot)
    assert appointment.slot_id == slot.id
    assert appointment.doctor_id == doctor.id

    fetched = db_session.get(Appointment, appointment.id)
    assert fetched is not None
    assert fetched.slot_id == slot.id
