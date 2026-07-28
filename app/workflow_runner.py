import uuid

from app.agents.routing import routing_agent_node
from app.db import SessionLocal
from app.graph import build_graph
from app.models import Appointment, Doctor, WorkflowRun, WorkflowStatus
from app.tools.appointment_tools import (
    book_or_modify_appointment,
    check_slot_availability,
    list_patient_appointments,
)
from app.tools.escalation_tools import create_escalation

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
        "missing_document_types": [],
        "reminder_ids": [],
        "escalation": None,
        "status": "running",
        "needs_clarification": False,
        "needs_appointment_reason": False,
        "needs_appointment_selection": False,
        "pending_appointment_action": None,
        "rescheduling_appointment_id": None,
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
    elif full_state.get("needs_intent_selection"):
        workflow_run.status = WorkflowStatus.needs_intent_selection
    elif full_state.get("needs_clarification"):
        workflow_run.status = WorkflowStatus.needs_clarification

    elif full_state.get("needs_appointment_selection"):
        return _land_on_appointment_selection_or_none(db, workflow_run, full_state)
    elif full_state.get("needs_appointment_reason"):
        # Routing intent was confidently "book_appointment" but the patient
        # never actually said what kind of care they need (e.g. "I'm here
        # to book an appointment") - ask, with real department buttons,
        # instead of Routing's LLM silently guessing one.
        workflow_run.status = WorkflowStatus.needs_appointment_reason
    elif full_state.get("department_id"):
        # routing_agent resolved a real department and the graph ends
        # right there now (app/graph.py) - no agent auto-picks or
        # auto-books a slot on the patient's behalf anymore. Land on the
        # real slot list (or "no slots" if genuinely none exist), same
        # deterministic helper the clarification-flow continuations use.
        return _land_on_slots_or_no_slots(db, workflow_run, full_state, full_state["department_id"])
    else:
        workflow_run.status = WorkflowStatus.completed

    full_state["status"] = workflow_run.status.value

    workflow_run.state = dict(full_state)
    db.commit()
    return workflow_run


def _finalize_or_continue_intents(workflow_run: WorkflowRun, full_state: dict) -> None:
    """Called at every point that would otherwise set a terminal `completed`
    status. If the patient had a leftover intent from an earlier
    needs_intent_selection pick, land back there for the next one instead
    of ending the run - closes the "asked which one first, then forgot the
    second" gap. Mutates workflow_run/full_state in place; caller still
    does workflow_run.state = dict(full_state) and db.commit() same as
    before, this only decides which status to set."""
    remaining = full_state.get("remaining_intents") or []
    if remaining:
        next_intent, *rest = remaining
        full_state["intent"] = next_intent
        full_state["remaining_intents"] = rest
        full_state["needs_intent_selection"] = True
        full_state["pending_appointment_action"] = None
        full_state["rescheduling_appointment_id"] = None
        full_state["department_id"] = None
        workflow_run.status = WorkflowStatus.needs_intent_selection
        workflow_run.current_step = "needs_intent_selection"
    else:
        workflow_run.status = WorkflowStatus.completed


def _land_on_appointment_selection_or_none(db, workflow_run: WorkflowRun, full_state: dict) -> WorkflowRun:
    appointments = list_patient_appointments(db, full_state["patient_id"])
    if appointments:
        workflow_run.status = WorkflowStatus.needs_appointment_selection
        workflow_run.current_step = "needs_appointment_selection"
    else:
        _finalize_or_continue_intents(workflow_run, full_state)
    full_state["status"] = workflow_run.status.value
    workflow_run.state = dict(full_state)
    db.commit()
    return workflow_run


def _land_on_slots_or_no_slots(
    db, workflow_run: WorkflowRun, full_state: dict, department_id: str, rescheduling_appointment_id: str | None = None
) -> WorkflowRun:
    """Shared by both continuation functions below once department_id is
    known (however it was determined - button click or Routing's LLM
    match). Queries real open slots directly via check_slot_availability
    (the plain function, not the LLM tool - no model involvement at all,
    nothing to guess). If any exist, lands at needs_slot_selection so the
    patient can pick one for real, with real doctor names and times, rather
    than the Appointment agent silently auto-picking one on their behalf.
    If genuinely none exist, lands at completed with no appointment_id -
    same "couldn't find any open slots" wording branch as before, still
    accurate since that case really means what it says now."""
    full_state["department_id"] = department_id
    if rescheduling_appointment_id:
        full_state["rescheduling_appointment_id"] = rescheduling_appointment_id
    slots = check_slot_availability(db, department_id, {})
    if slots:
        workflow_run.status = WorkflowStatus.needs_slot_selection
        workflow_run.current_step = "needs_slot_selection"
    else:
        _finalize_or_continue_intents(workflow_run, full_state)
    full_state["status"] = workflow_run.status.value
    workflow_run.state = dict(full_state)
    db.commit()
    return workflow_run



def continue_as_booking(db, workflow_run: WorkflowRun, override_request_text: str | None = None) -> WorkflowRun:
    """needs_appointment_reason -> patient answered "what's this for" with
    free text (no department listed matched, or they typed their own
    description). Runs routing_agent_node (the only LLM step left in this
    path - department matching genuinely needs judgment when given free
    text) to resolve a department, then lands on the real slot list via
    _land_on_slots_or_no_slots - no Appointment-agent LLM call at all
    anymore; slot selection is the patient's choice, not a model guess.
    Lands at needs_review if Routing escalates (couldn't match any
    department), matching the existing safety-conscious behavior.

    override_request_text replaces state["request_text"] before routing
    runs, so Routing's LLM sees the patient's actual answer to "what's this
    appointment for" instead of re-matching whatever ambiguous text
    originally triggered needs_clarification (that original text may have
    had nothing routable in it at all - e.g. "what are your visiting
    hours" - re-feeding it to Routing would just fail the same way again).

    config MUST be built from the SessionLocal registry, never the
    caller's own `db` parameter directly - see docs/memory/gotchas.md,
    "The shared-Session/ToolNode bug (above) almost got reintroduced by a
    design spec". routing_agent_node dispatches tool calls through
    LangGraph's ToolNode, which runs them in a worker thread; a bare
    Session object is not thread-safe.
    """
    config = {"configurable": {"db": SessionLocal}}
    full_state = dict(workflow_run.state)
    if override_request_text:
        full_state["request_text"] = override_request_text

    routing_update = routing_agent_node(full_state, config)
    full_state.update(routing_update or {})
    workflow_run.current_step = "routing_agent"
    workflow_run.state = dict(full_state)
    db.commit()

    if full_state.get("escalation"):
        workflow_run.status = WorkflowStatus.needs_review
        full_state["status"] = workflow_run.status.value
        workflow_run.state = dict(full_state)
        db.commit()
        return workflow_run

    if full_state.get("needs_appointment_reason"):
        # The patient's free-text answer was still too vague to route on
        # (rare - they already got one chance to describe it) - ask again
        # rather than escalate or guess; the same department-buttons +
        # free-text form is still right there.
        workflow_run.status = WorkflowStatus.needs_appointment_reason
        full_state["status"] = workflow_run.status.value
        workflow_run.state = dict(full_state)
        db.commit()
        return workflow_run

    return _land_on_slots_or_no_slots(db, workflow_run, full_state, full_state["department_id"])


def continue_as_intent_selection(db, workflow_run: WorkflowRun, chosen_intent: str) -> WorkflowRun:
    """needs_intent_selection -> patient picked which of the detected
    intents to handle first (or next, if this is a continuation after an
    earlier pick already completed - see _finalize_or_continue_intents).
    Stores whatever's left over in remaining_intents before dispatching, so
    the leftover gets re-offered instead of silently dropped once the
    chosen action finishes. Dispatches to whichever existing, unmodified
    continuation the single-intent graph path would have used for that
    label - no new booking/cancel/reschedule logic here."""
    chosen = chosen_intent.strip().lower()
    full_state = dict(workflow_run.state)

    original = [label.strip().lower() for label in full_state.get("intent", "").split(",")]
    remaining = [
        label for label in original
        if label != chosen and any(kw in label for kw in ("book", "cancel", "reschedule"))
    ]
    full_state["remaining_intents"] = remaining
    workflow_run.state = full_state
    db.commit()

    if "book" in chosen:
        return continue_as_booking(db, workflow_run)

    action = "cancel" if "cancel" in chosen else "reschedule"
    full_state["pending_appointment_action"] = action
    workflow_run.state = full_state
    db.commit()
    return _land_on_appointment_selection_or_none(db, workflow_run, full_state)




def continue_as_booking_with_department(db, workflow_run: WorkflowRun, department_id: str) -> WorkflowRun:
    """needs_appointment_reason -> patient picked a real department directly
    from the buttons shown (not free text). Skips routing_agent entirely -
    there is nothing to guess, the patient already told us the department -
    and goes straight to _land_on_slots_or_no_slots. Fully deterministic,
    no LLM call anywhere in this path."""
    full_state = dict(workflow_run.state)
    return _land_on_slots_or_no_slots(db, workflow_run, full_state, department_id)


def continue_with_selected_slot(db, workflow_run: WorkflowRun, slot_id: str) -> WorkflowRun:
    """needs_slot_selection -> patient picked one specific real slot from
    the list shown. Books that exact slot directly via
    book_or_modify_appointment (the plain function, not the LLM tool) - no
    LLM call, no ToolNode involved at all, so `db` is safe to use directly
    here, unlike continue_as_booking/continue_as_booking_with_department
    above. If the slot was taken by someone else between listing and this
    click (a real, if narrow, race - the same class the original spec's
    error-handling section already anticipated for the deferred
    confirm-before-booking design), book_or_modify_appointment's existing
    conflict/no-longer-open checks catch it, no appointment gets created,
    and this stays at needs_slot_selection so the patient can pick a
    different one instead of the run silently failing."""
    patient_id = workflow_run.state["patient_id"]
    rescheduling_id = workflow_run.state.get("rescheduling_appointment_id")
    action = "reschedule" if rescheduling_id else "book"
    result = book_or_modify_appointment(db, patient_id, slot_id, action, rescheduling_id)

    full_state = dict(workflow_run.state)
    if result["status"] == "error":
        workflow_run.status = WorkflowStatus.needs_slot_selection
        workflow_run.current_step = "needs_slot_selection"
    else:
        full_state["appointment_id"] = result["id"]
        _finalize_or_continue_intents(workflow_run, full_state)
        if workflow_run.status == WorkflowStatus.completed:
            workflow_run.current_step = "appointment_agent"
    full_state["status"] = workflow_run.status.value



    workflow_run.state = full_state
    db.commit()
    return workflow_run


def start_appointment_action(
    db, patient_id: str, action: str, appointment_id: str, user_id: str | None = None
) -> WorkflowRun:
    """Entry point from the My Appointments page - the patient already
    picked both the action (Cancel/Reschedule button) and the target
    appointment (which row they clicked) with zero ambiguity, so this skips
    Safety/Coordinator/the graph entirely (there is no free text to
    classify) and seeds a WorkflowRun directly at needs_appointment_selection
    - the same state run_workflow lands on after a typed "cancel my
    appointment" request. continue_as_appointment_action (unchanged) takes
    it from there; this function's only job is constructing that starting
    state."""
    workflow_run = WorkflowRun(
        patient_id=uuid.UUID(patient_id),
        current_step="needs_appointment_selection",
        state={},
        status=WorkflowStatus.needs_appointment_selection,
    )
    db.add(workflow_run)
    db.commit()

    full_state = {
        "workflow_run_id": str(workflow_run.id),
        "patient_id": patient_id,
        "user_id": user_id,
        "request_text": f"[My Appointments page] {action} appointment",
        "uploaded_files": [],
        "intent": f"{action}_appointment",
        "department_id": None,
        "appointment_id": None,
        "document_ids": [],
        "missing_document_types": [],
        "reminder_ids": [],
        "escalation": None,
        "status": WorkflowStatus.needs_appointment_selection.value,
        "needs_clarification": False,
        "needs_appointment_reason": False,
        "needs_appointment_selection": True,
        "pending_appointment_action": action,
        "rescheduling_appointment_id": None,
    }
    workflow_run.state = full_state
    db.commit()

    return continue_as_appointment_action(db, workflow_run, appointment_id)


def continue_as_appointment_action(db, workflow_run: WorkflowRun, appointment_id: str) -> WorkflowRun:

    """needs_appointment_selection -> patient picked which real appointment
    they mean. Branches on the action recorded when the graph set
    needs_appointment_selection:

    - cancel: calls book_or_modify_appointment(action="cancel") directly -
      deterministic, no LLM, no ToolNode, `db` safe to use directly (same
      class as continue_with_selected_slot). Lands at completed immediately.
    - reschedule: looks up the appointment's current department (via its
      doctor), then reuses _land_on_slots_or_no_slots with
      rescheduling_appointment_id set, landing at needs_slot_selection - the
      exact same screen and continue_with_selected_slot path booking uses,
      just pre-scoped to this appointment's department and remembering which
      appointment to update instead of creating a new one."""
    full_state = dict(workflow_run.state)
    action = full_state.get("pending_appointment_action")

    if action == "cancel":
        result = book_or_modify_appointment(db, full_state["patient_id"], None, "cancel", appointment_id)
        full_state["appointment_id"] = result.get("id")
        _finalize_or_continue_intents(workflow_run, full_state)
        full_state["status"] = workflow_run.status.value
        workflow_run.state = full_state
        db.commit()
        return workflow_run


    # reschedule: find the department this appointment is currently in
    appointment = db.query(Appointment).filter(Appointment.id == uuid.UUID(appointment_id)).first()
    doctor = db.query(Doctor).filter(Doctor.id == appointment.doctor_id).first()
    return _land_on_slots_or_no_slots(
        db,
        workflow_run,
        full_state,
        str(doctor.department_id),
        rescheduling_appointment_id=appointment_id,
    )


def continue_as_staff_escalation(db, workflow_run: WorkflowRun, reason: str) -> WorkflowRun:
    """Patient clicked 'talk to staff' from the needs_clarification popup.
    Calls create_escalation directly - no LLM call, no ToolNode involved,
    `db` is safe to use directly here (unlike continue_as_booking above)."""
    escalation = create_escalation(db, str(workflow_run.id), reason)
    full_state = dict(workflow_run.state)
    full_state["escalation"] = escalation
    workflow_run.status = WorkflowStatus.needs_review
    workflow_run.current_step = "staff_escalation"
    full_state["status"] = workflow_run.status.value
    workflow_run.state = full_state
    db.commit()
    return workflow_run
