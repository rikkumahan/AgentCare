import uuid

from app.db import SessionLocal
from app.graph import build_graph
from app.models import WorkflowRun, WorkflowStatus

_compiled_graph = build_graph()


def run_workflow(
    db,
    patient_id: str,
    user_id: str,
    request_text: str,
    uploaded_files: list[str] | None = None,
) -> WorkflowRun:
    workflow_run = WorkflowRun(
        patient_id=uuid.UUID(patient_id),
        current_step="safety_agent",
        state={},
        status=WorkflowStatus.running,
    )
    db.add(workflow_run)
    db.commit()

    initial_state = {
        "workflow_run_id": str(workflow_run.id),
        "patient_id": patient_id,
        "user_id": user_id,
        "request_text": request_text,
        "uploaded_files": uploaded_files or [],
        "intent": None,
        "department_id": None,
        "appointment_id": None,
        "document_ids": [],
        "reminder_ids": [],
        "escalation": None,
        "status": "running",
    }

    # SessionLocal (the scoped_session registry), not the resolved db
    # instance: tool calls run inside ToolNode's own worker thread, and
    # scoped_session hands that thread its own session transparently. When
    # called from the same thread as run_workflow (agent node functions,
    # and this function's own bookkeeping below), it resolves to the exact
    # same session as `db` - no behavior change there, just thread-safety
    # for the tool-execution thread.
    config = {"configurable": {"db": SessionLocal}}
    full_state = dict(initial_state)

    try:
        for step in _compiled_graph.stream(initial_state, config=config, stream_mode="updates"):
            for node_name, update in step.items():
                # A node that returns {} (a true no-op, e.g. document_agent
                # when no file was attached) is reported by LangGraph's
                # "updates" stream mode as None, not {} - dict.update(None)
                # raises TypeError. Only document_agent can produce this
                # today; treat it as "no fields changed", not an error.
                full_state.update(update or {})
                workflow_run.current_step = node_name
                workflow_run.state = dict(full_state)
                db.commit()
    except Exception as exc:
        workflow_run.status = WorkflowStatus.failed
        full_state["status"] = WorkflowStatus.failed.value
        workflow_run.state = {**full_state, "error": str(exc)}
        db.commit()
        return workflow_run

    if full_state.get("escalation"):
        workflow_run.status = WorkflowStatus.needs_review
    else:
        workflow_run.status = WorkflowStatus.running
        workflow_run.current_step = "document_agent"

    full_state["status"] = workflow_run.status.value
    workflow_run.state = dict(full_state)
    db.commit()
    return workflow_run
