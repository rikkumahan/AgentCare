from datetime import datetime, timedelta, timezone

from app.models import Appointment, AppointmentSlot, AuditEvent, Doctor, SlotStatus
from app.tools.appointment_tools import _slots_summary, book_or_modify_appointment, check_slot_availability
from tests.fakes import make_appointment_slot, make_department, make_doctor, make_patient_profile


def test_slots_summary_includes_real_slot_id_the_model_can_copy():
    # This string is the only part of the tool result the model actually
    # sees on its next turn - confirmed against the real API that a
    # count-only summary causes the model to invent a fake slot_id instead
    # of copying a real one, since it was never shown one.
    slots = [{"slot_id": "abc-123", "doctor_name": "Dr. Rao", "start_time": "2026-08-01T17:00:00+00:00"}]

    summary = _slots_summary(slots)

    assert "abc-123" in summary
    assert "Dr. Rao" in summary


def test_slots_summary_handles_empty_list():
    assert _slots_summary([]) == "Found 0 open slot(s)."


def test_check_slot_availability_returns_open_slots_in_department_within_window(db_session):
    department = make_department(db_session)
    doctor = make_doctor(db_session, department=department)
    slot = make_appointment_slot(db_session, doctor=doctor)

    result = check_slot_availability(db_session, str(department.id), {})

    assert len(result) == 1
    assert result[0]["slot_id"] == str(slot.id)
    assert result[0]["doctor_name"] == doctor.name


def test_check_slot_availability_excludes_booked_slots_and_other_departments(db_session):
    department = make_department(db_session)
    other_department = make_department(db_session)
    doctor = make_doctor(db_session, department=department)
    make_appointment_slot(db_session, doctor=doctor, status=SlotStatus.booked)
    make_appointment_slot(db_session, doctor=make_doctor(db_session, department=other_department))

    result = check_slot_availability(db_session, str(department.id), {})

    assert result == []


def test_check_slot_availability_respects_date_window(db_session):
    department = make_department(db_session)
    doctor = make_doctor(db_session, department=department)
    far_future = datetime.now(timezone.utc) + timedelta(days=60)
    make_appointment_slot(db_session, doctor=doctor, start_time=far_future)

    result = check_slot_availability(db_session, str(department.id), {})

    assert result == []


def test_book_or_modify_appointment_rejects_malformed_slot_id(db_session):
    profile = make_patient_profile(db_session)

    result = book_or_modify_appointment(db_session, str(profile.id), "not-a-real-uuid", "book", None)

    assert result["status"] == "error"
    assert "not a valid slot id" in result["error"]


def test_book_or_modify_appointment_rejects_malformed_existing_appointment_id(db_session):
    profile = make_patient_profile(db_session)
    slot = make_appointment_slot(db_session)

    result = book_or_modify_appointment(db_session, str(profile.id), str(slot.id), "cancel", "not-a-real-uuid")

    assert result["status"] == "error"
    assert "not a valid appointment id" in result["error"]


def test_book_or_modify_appointment_books_open_slot(db_session):
    profile = make_patient_profile(db_session)
    slot = make_appointment_slot(db_session)

    result = book_or_modify_appointment(db_session, str(profile.id), str(slot.id), "book", None)

    assert result["status"] == "confirmed"
    booked_slot = db_session.query(AppointmentSlot).filter(AppointmentSlot.id == slot.id).one()
    assert booked_slot.status == SlotStatus.booked
    appointment = db_session.query(Appointment).filter(Appointment.id == result["id"]).one()
    assert str(appointment.patient_id) == str(profile.id)


def test_book_or_modify_appointment_rejects_conflicting_booking(db_session):
    profile = make_patient_profile(db_session)
    doctor = make_doctor(db_session)
    start = datetime.now(timezone.utc) + timedelta(days=2)
    slot_a = make_appointment_slot(db_session, doctor=doctor, start_time=start)
    slot_b = make_appointment_slot(db_session, doctor=doctor, start_time=start + timedelta(minutes=10))

    first = book_or_modify_appointment(db_session, str(profile.id), str(slot_a.id), "book", None)
    assert first["status"] == "confirmed"

    second = book_or_modify_appointment(db_session, str(profile.id), str(slot_b.id), "book", None)
    assert second["status"] == "error"
    assert "conflicting" in second["error"]


def test_book_or_modify_appointment_conflict_check_includes_rescheduled_appointments(db_session):
    # Regression test: _conflicting_appointment previously only checked
    # pending/confirmed, missing "rescheduled" - a status that looks
    # terminal but actually means the appointment is still active, just
    # moved to a new slot (it never flips back to "confirmed"). A
    # rescheduled appointment must still block a new overlapping booking.
    profile = make_patient_profile(db_session)
    doctor = make_doctor(db_session)
    start = datetime.now(timezone.utc) + timedelta(days=2)
    original_slot = make_appointment_slot(db_session, doctor=doctor, start_time=start)
    rescheduled_slot = make_appointment_slot(db_session, doctor=doctor, start_time=start + timedelta(days=1))
    overlapping_slot = make_appointment_slot(
        db_session, doctor=doctor, start_time=start + timedelta(days=1, minutes=10)
    )

    booked = book_or_modify_appointment(db_session, str(profile.id), str(original_slot.id), "book", None)
    rescheduled = book_or_modify_appointment(
        db_session, str(profile.id), str(rescheduled_slot.id), "reschedule", booked["id"]
    )
    assert rescheduled["status"] == "rescheduled"

    result = book_or_modify_appointment(db_session, str(profile.id), str(overlapping_slot.id), "book", None)

    assert result["status"] == "error"
    assert "conflicting" in result["error"]


def test_book_or_modify_appointment_reschedule_frees_old_slot_and_books_new(db_session):
    profile = make_patient_profile(db_session)
    old_slot = make_appointment_slot(db_session)
    doctor = db_session.query(Doctor).filter(Doctor.id == old_slot.doctor_id).one()
    new_slot = make_appointment_slot(db_session, doctor=doctor)
    booked = book_or_modify_appointment(db_session, str(profile.id), str(old_slot.id), "book", None)

    result = book_or_modify_appointment(db_session, str(profile.id), str(new_slot.id), "reschedule", booked["id"])

    assert result["status"] == "rescheduled"
    assert result["slot_id"] == str(new_slot.id)
    freed_slot = db_session.query(AppointmentSlot).filter(AppointmentSlot.id == old_slot.id).one()
    assert freed_slot.status == SlotStatus.open


def test_book_or_modify_appointment_reschedule_rejects_conflicting_target_slot(db_session):
    profile = make_patient_profile(db_session)
    doctor = make_doctor(db_session)
    start = datetime.now(timezone.utc) + timedelta(days=2)
    slot_a = make_appointment_slot(db_session, doctor=doctor, start_time=start)
    slot_b = make_appointment_slot(db_session, doctor=doctor, start_time=start + timedelta(minutes=10))
    slot_c = make_appointment_slot(db_session, doctor=doctor, start_time=start + timedelta(days=2))

    appointment_1 = book_or_modify_appointment(db_session, str(profile.id), str(slot_a.id), "book", None)
    appointment_2 = book_or_modify_appointment(db_session, str(profile.id), str(slot_c.id), "book", None)
    assert appointment_1["status"] == "confirmed"
    assert appointment_2["status"] == "confirmed"

    result = book_or_modify_appointment(
        db_session, str(profile.id), str(slot_b.id), "reschedule", appointment_2["id"]
    )

    assert result["status"] == "error"
    assert "conflicting" in result["error"]
    untouched_slot_c = db_session.query(AppointmentSlot).filter(AppointmentSlot.id == slot_c.id).one()
    assert untouched_slot_c.status == SlotStatus.booked
    untouched_slot_b = db_session.query(AppointmentSlot).filter(AppointmentSlot.id == slot_b.id).one()
    assert untouched_slot_b.status == SlotStatus.open


def test_book_or_modify_appointment_cancel_frees_slot(db_session):
    profile = make_patient_profile(db_session)
    slot = make_appointment_slot(db_session)
    booked = book_or_modify_appointment(db_session, str(profile.id), str(slot.id), "book", None)

    result = book_or_modify_appointment(db_session, str(profile.id), str(slot.id), "cancel", booked["id"])

    assert result["status"] == "cancelled"
    freed_slot = db_session.query(AppointmentSlot).filter(AppointmentSlot.id == slot.id).one()
    assert freed_slot.status == SlotStatus.open


def test_book_or_modify_appointment_writes_audit_event(db_session):
    profile = make_patient_profile(db_session)
    slot = make_appointment_slot(db_session)

    book_or_modify_appointment(db_session, str(profile.id), str(slot.id), "book", None)

    audit_actions = {e.action for e in db_session.query(AuditEvent).all()}
    assert "book_or_modify_appointment" in audit_actions
