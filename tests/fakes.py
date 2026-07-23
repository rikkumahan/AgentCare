import uuid

from app.models import User, UserRole


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
