import uuid
from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from sqlalchemy.orm import Session

from app.audit import audited
from app.models import Escalation


@audited("create_escalation", "Escalation")
def create_escalation(db: Session, workflow_run_id: str, reason: str) -> dict:
    escalation = Escalation(workflow_run_id=uuid.UUID(workflow_run_id), reason=reason)
    db.add(escalation)
    db.commit()
    return {
        "id": str(escalation.id),
        "workflow_run_id": str(escalation.workflow_run_id),
        "reason": escalation.reason,
        "status": escalation.status.value,
    }


@tool(response_format="content_and_artifact")
def create_escalation_tool(
    reason: str,
    workflow_run_id: Annotated[str, InjectedState("workflow_run_id")],
    config: RunnableConfig,
):
    """Escalate this workflow run to human staff review. Call this whenever
    the request, or a prior agent's proposed action, contains a diagnosis, a
    prescription or dosage change, or describes a medical emergency. reason
    should briefly describe what triggered the escalation in administrative
    (not clinical) language."""
    db = config["configurable"]["db"]
    result = create_escalation(db, workflow_run_id, reason)
    return f"Escalated: {reason}", result
