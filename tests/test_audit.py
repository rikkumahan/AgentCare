import uuid

import pytest

from app.audit import audited
from app.models import AuditEvent


def test_audited_writes_audit_event_on_success(db_session):
    action = f"dummy_action-{uuid.uuid4().hex[:8]}"

    @audited(action, "DummyEntity")
    def _dummy(db, x):
        return {"id": str(uuid.uuid4()), "x": x}

    result = _dummy(db_session, "hello")

    events = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == action, AuditEvent.entity_type == "DummyEntity")
        .all()
    )
    assert len(events) == 1
    assert str(events[0].entity_id) == result["id"]
    assert events[0].event_metadata["result"]["x"] == "hello"


def test_audited_writes_audit_event_on_failure_and_reraises(db_session):
    action = f"dummy_failure-{uuid.uuid4().hex[:8]}"

    @audited(action, "DummyEntity")
    def _dummy(db):
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        _dummy(db_session)

    events = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == action, AuditEvent.entity_type == "DummyEntity")
        .all()
    )
    assert len(events) == 1
    assert events[0].entity_id is None
    assert "boom" in events[0].event_metadata["error"]
