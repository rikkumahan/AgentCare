import hashlib
import uuid

from langchain_core.messages import AIMessage

from app.models import AppointmentStatus, PatientDocument
from tests.fakes import (
    FakeToolCallingModel,
    ai_message_text,
    ai_message_with_tool_call,
    make_appointment,
    make_department,
    make_doctor,
    make_patient_profile,
    workflow_state,
)


def test_document_agent_node_is_a_no_op_when_no_files_attached(monkeypatch, db_session):
    def _explode():
        raise AssertionError("get_llm should not be called when no file is attached")

    monkeypatch.setattr("app.agents.document.get_llm", _explode)

    from app.agents.document import document_agent_node

    state = workflow_state(uploaded_files=[])
    update = document_agent_node(state, config={"configurable": {"db": db_session}})

    assert update == {}


def test_document_agent_node_saves_uploaded_file_and_returns_document_id(monkeypatch, tmp_path, db_session):
    profile = make_patient_profile(db_session)
    file_path = tmp_path / "insurance_card.pdf"
    file_path.write_bytes(b"insurance-bytes")

    fake_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call(
                "store_and_classify_document_tool",
                {"file_path": str(file_path), "document_type": "insurance"},
            ),
            ai_message_text("Saved your insurance document."),
        ]
    )
    monkeypatch.setattr("app.agents.document.get_llm", lambda: fake_model)

    from app.agents.document import document_agent_node

    state = workflow_state(patient_id=str(profile.id), uploaded_files=[str(file_path)])
    update = document_agent_node(state, config={"configurable": {"db": db_session}})

    assert len(update["document_ids"]) == 1
    document = (
        db_session.query(PatientDocument)
        .filter(PatientDocument.id == uuid.UUID(update["document_ids"][0]))
        .one()
    )
    assert document.document_type.value == "insurance"


def test_document_agent_node_ignores_model_supplied_file_path_and_uses_injected_real_path(
    monkeypatch, tmp_path, db_session
):
    # Security regression for Finding 1: file_path must be server-injected
    # (InjectedState), never LLM-controlled. Have the fake model try to
    # smuggle an arbitrary path in its tool call args (a real model can't -
    # the schema no longer exposes file_path - but the fake model bypasses
    # schema validation, so this proves ToolNode's injection wins over
    # whatever the model puts in args) and confirm the real uploaded file is
    # what actually gets checksummed and stored, not the model's value.
    profile = make_patient_profile(db_session)
    file_path = tmp_path / "insurance_card.pdf"
    file_path.write_bytes(b"insurance-bytes")

    fake_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call(
                "store_and_classify_document_tool",
                {"file_path": "/etc/passwd", "document_type": "insurance"},
            ),
            ai_message_text("Saved your insurance document."),
        ]
    )
    monkeypatch.setattr("app.agents.document.get_llm", lambda: fake_model)

    from app.agents.document import document_agent_node

    state = workflow_state(patient_id=str(profile.id), uploaded_files=[str(file_path)])
    update = document_agent_node(state, config={"configurable": {"db": db_session}})

    assert len(update["document_ids"]) == 1
    document = (
        db_session.query(PatientDocument)
        .filter(PatientDocument.id == uuid.UUID(update["document_ids"][0]))
        .one()
    )
    assert document.file_path == str(file_path)
    assert document.checksum == hashlib.sha256(b"insurance-bytes").hexdigest()


def test_document_agent_node_returns_missing_document_types_when_reported(monkeypatch, tmp_path, db_session):
    # Finding 2 regression: missing_document_types must flow from the tool
    # result all the way into WorkflowState, not get dropped by
    # document_agent_node. Patient has a confirmed Cardiology appointment
    # requiring an ecg, but uploads their insurance card instead.
    department = make_department(db_session, name=f"Cardiology {uuid.uuid4().hex[:8]}")
    department.required_document_types = ["ecg"]
    db_session.commit()
    doctor = make_doctor(db_session, department=department)
    profile = make_patient_profile(db_session)
    make_appointment(db_session, patient=profile, doctor=doctor, status=AppointmentStatus.confirmed)

    file_path = tmp_path / "insurance_card.pdf"
    file_path.write_bytes(b"insurance-bytes")

    fake_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call(
                "store_and_classify_document_tool",
                {"file_path": str(file_path), "document_type": "insurance"},
            ),
            ai_message_text("Saved your insurance document."),
        ]
    )
    monkeypatch.setattr("app.agents.document.get_llm", lambda: fake_model)

    from app.agents.document import document_agent_node

    state = workflow_state(patient_id=str(profile.id), uploaded_files=[str(file_path)])
    update = document_agent_node(state, config={"configurable": {"db": db_session}})

    assert update["missing_document_types"] == ["ecg"]


def test_document_agent_node_skips_a_failed_upload_id(monkeypatch, db_session):
    # An error result has id=None - it must not leak a None into document_ids.
    fake_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call(
                "store_and_classify_document_tool",
                {"file_path": "/no/such/file.pdf", "document_type": "other"},
            ),
            ai_message_text("Could not save that file."),
        ]
    )
    monkeypatch.setattr("app.agents.document.get_llm", lambda: fake_model)

    from app.agents.document import document_agent_node

    state = workflow_state(uploaded_files=["/no/such/file.pdf"])
    update = document_agent_node(state, config={"configurable": {"db": db_session}})

    assert update["document_ids"] == []
