import uuid

from langchain_core.messages import AIMessage

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


def workflow_state(**overrides) -> dict:
    state = {
        "workflow_run_id": "11111111-1111-1111-1111-111111111111",
        "patient_id": "22222222-2222-2222-2222-222222222222",
        "user_id": "u1",
        "request_text": "book a cardiology appointment",
        "uploaded_files": [],
        "intent": None,
        "department_id": None,
        "appointment_id": None,
        "document_ids": [],
        "reminder_ids": [],
        "escalation": None,
        "status": "running",
    }
    state.update(overrides)
    return state


class FakeToolCallingModel:
    """Stands in for a ChatGroq model bound to tools: .bind_tools() is a
    no-op returning self, .invoke() returns the next scripted response."""

    def __init__(self, responses: list[AIMessage]):
        self._responses = list(responses)

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        return self._responses.pop(0)


def ai_message_with_tool_call(name: str, args: dict, call_id: str = "call_1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])


def ai_message_text(text: str) -> AIMessage:
    return AIMessage(content=text)
