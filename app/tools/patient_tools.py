import uuid
from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from sqlalchemy.orm import Session

from app.audit import audited
from app.models import PatientProfile

_UPDATABLE_FIELDS = ("phone", "preferred_language", "emergency_contact")


@audited("get_or_create_patient", "PatientProfile")
def get_or_create_patient(db: Session, user_id: str, profile_fields: dict) -> dict:
    profile = db.query(PatientProfile).filter(PatientProfile.user_id == uuid.UUID(user_id)).first()
    if profile is None:
        profile = PatientProfile(user_id=uuid.UUID(user_id))
        db.add(profile)
        db.flush()

    for field in _UPDATABLE_FIELDS:
        value = profile_fields.get(field)
        if value:
            setattr(profile, field, value)

    db.commit()
    return {
        "id": str(profile.id),
        "user_id": str(profile.user_id),
        "phone": profile.phone,
        "preferred_language": profile.preferred_language,
        "emergency_contact": profile.emergency_contact,
    }


@tool(response_format="content_and_artifact")
def get_or_create_patient_tool(
    profile_fields: dict,
    user_id: Annotated[str, InjectedState("user_id")],
    config: RunnableConfig,
):
    """Look up the patient's profile, creating one if missing, and update it
    with any contact details mentioned in the request. profile_fields may
    include phone, preferred_language, and/or emergency_contact — omit any
    field not mentioned in the request; pass {} if none are mentioned."""
    db = config["configurable"]["db"]
    result = get_or_create_patient(db, user_id, profile_fields)
    return f"Patient profile resolved: {result['id']}", result
