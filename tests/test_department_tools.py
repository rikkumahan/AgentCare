import uuid

from app.models import AuditEvent
from app.tools.department_tools import lookup_departments
from tests.fakes import make_department


def test_lookup_departments_filters_by_hint(db_session):
    token = uuid.uuid4().hex[:8]
    cardiology = make_department(db_session, name=f"Cardiology {token}")
    make_department(db_session, name=f"General Medicine {uuid.uuid4().hex[:8]}")

    result = lookup_departments(db_session, f"need {token} please")

    ids = {d["id"] for d in result}
    assert str(cardiology.id) in ids
    assert len(result) == 1


def test_lookup_departments_falls_back_to_full_list_when_hint_matches_nothing(db_session):
    token = uuid.uuid4().hex[:8]
    cardiology = make_department(db_session, name=f"Cardiology {token}")
    general = make_department(db_session, name=f"General Medicine {token}")

    result = lookup_departments(db_session, f"unmatched-hint-{uuid.uuid4().hex[:8]}")

    ids = {d["id"] for d in result}
    assert str(cardiology.id) in ids
    assert str(general.id) in ids


def test_lookup_departments_excludes_inactive_departments(db_session):
    token = uuid.uuid4().hex[:8]
    active_dept = make_department(db_session, name=f"Active Dept {token}", active=True)
    inactive_dept = make_department(db_session, name=f"Retired Dept {token}", active=False)

    result = lookup_departments(db_session, "")

    ids = {d["id"] for d in result}
    assert str(active_dept.id) in ids
    assert str(inactive_dept.id) not in ids


def test_lookup_departments_writes_audit_event(db_session):
    make_department(db_session, name=f"Cardiology {uuid.uuid4().hex[:8]}")

    lookup_departments(db_session, "cardiology")

    audit_actions = {e.action for e in db_session.query(AuditEvent).all()}
    assert "lookup_departments" in audit_actions
