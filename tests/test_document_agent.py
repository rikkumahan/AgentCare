import uuid

from langchain_core.messages import AIMessage

from app.models import PatientDocument
from tests.fakes import FakeToolCallingModel, ai_message_text, ai_message_with_tool_call, make_patient_profile, workflow_state


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
