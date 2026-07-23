import uuid

from app.models import PatientProfile, User, UserRole, WorkflowRun


def make_user(db_session, role=UserRole.patient) -> User:
    user = User(
        name="Test Patient",
        email=f"test-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="irrelevant-hash",
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    return user


def make_patient_profile(db_session, user: User | None = None) -> PatientProfile:
    if user is None:
        user = make_user(db_session)
    profile = PatientProfile(user_id=user.id)
    db_session.add(profile)
    db_session.commit()
    return profile


def make_workflow_run(db_session, profile: PatientProfile | None = None) -> WorkflowRun:
    if profile is None:
        profile = make_patient_profile(db_session)
    workflow_run = WorkflowRun(patient_id=profile.id)
    db_session.add(workflow_run)
    db_session.commit()
    return workflow_run
