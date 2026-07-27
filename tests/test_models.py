import uuid

from app.models import Department, Doctor


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
