from typing import TypedDict


class WorkflowState(TypedDict):
    workflow_run_id: str
    patient_id: str
    user_id: str
    request_text: str
    uploaded_files: list[str]
    intent: str | None
    department_id: str | None
    appointment_id: str | None
    document_ids: list[str]
    missing_document_types: list[str]
    reminder_ids: list[str]
    escalation: dict | None
    status: str
    needs_clarification: bool
    needs_appointment_reason: bool
    needs_appointment_selection: bool
    needs_intent_selection: bool
    pending_appointment_action: str | None
    rescheduling_appointment_id: str | None


