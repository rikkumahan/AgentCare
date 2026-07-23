from app.models import PatientProfile
from app.tools.patient_tools import get_or_create_patient
from tests.fakes import make_user


def test_creates_patient_profile_when_missing(db_session):
    user = make_user(db_session)

    result = get_or_create_patient(db_session, str(user.id), {"phone": "+1-555-0100"})

    assert result["user_id"] == str(user.id)
    assert result["phone"] == "+1-555-0100"

    profile = db_session.query(PatientProfile).filter(PatientProfile.user_id == user.id).one()
    assert profile.phone == "+1-555-0100"


def test_updates_existing_profile_fields(db_session):
    user = make_user(db_session)
    existing = PatientProfile(user_id=user.id, phone="+1-555-0000")
    db_session.add(existing)
    db_session.commit()

    result = get_or_create_patient(db_session, str(user.id), {"emergency_contact": "Jane Doe"})

    assert result["id"] == str(existing.id)
    assert result["phone"] == "+1-555-0000"
    assert result["emergency_contact"] == "Jane Doe"


def test_blank_profile_fields_do_not_overwrite_existing_values(db_session):
    user = make_user(db_session)
    existing = PatientProfile(user_id=user.id, phone="+1-555-0000")
    db_session.add(existing)
    db_session.commit()

    result = get_or_create_patient(db_session, str(user.id), {"phone": ""})

    assert result["phone"] == "+1-555-0000"
