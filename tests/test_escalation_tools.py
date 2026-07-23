from app.models import Escalation
from app.tools.escalation_tools import create_escalation
from tests.fakes import make_workflow_run


def test_create_escalation_persists_row_with_open_status(db_session):
    workflow_run = make_workflow_run(db_session)

    result = create_escalation(db_session, str(workflow_run.id), "patient describes an emergency")

    assert result["status"] == "open"
    escalation = db_session.query(Escalation).filter(Escalation.workflow_run_id == workflow_run.id).one()
    assert escalation.reason == "patient describes an emergency"
    assert escalation.status.value == "open"


def test_create_escalation_allows_multiple_escalations_per_workflow_run(db_session):
    workflow_run = make_workflow_run(db_session)

    create_escalation(db_session, str(workflow_run.id), "first reason")
    create_escalation(db_session, str(workflow_run.id), "second reason")

    count = db_session.query(Escalation).filter(Escalation.workflow_run_id == workflow_run.id).count()
    assert count == 2
