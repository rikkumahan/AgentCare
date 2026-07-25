import uuid

from langchain_core.messages import HumanMessage

from app.agents.routing import (
    route_after_routing_llm,
    routing_agent_node,
    routing_finalize_node,
    routing_llm_node,
)
from app.models import Escalation
from tests.fakes import (
    FakeToolCallingModel,
    ai_message_text,
    ai_message_with_tool_call,
    make_department,
    make_workflow_run,
    workflow_state,
)


def _routing_state(**overrides):
    state = {"messages": [HumanMessage("request: book a cardiology appointment")], "department_name": None}
    state.update(overrides)
    return state


def test_routing_llm_node_with_tool_call_routes_to_tools(monkeypatch):
    fake_model = FakeToolCallingModel(
        [ai_message_with_tool_call("lookup_departments_tool", {"query_hint": "cardiology"})]
    )
    monkeypatch.setattr("app.agents.routing.get_llm", lambda: fake_model)

    state = _routing_state()
    update = routing_llm_node(state, config={"configurable": {}})
    state["messages"] = state["messages"] + update["messages"]

    assert route_after_routing_llm(state) == "routing_tools"


def test_routing_llm_node_with_no_tool_call_routes_to_finalize(monkeypatch):
    fake_model = FakeToolCallingModel([ai_message_text("Cardiology")])
    monkeypatch.setattr("app.agents.routing.get_llm", lambda: fake_model)

    state = _routing_state()
    update = routing_llm_node(state, config={"configurable": {}})
    state["messages"] = state["messages"] + update["messages"]

    assert route_after_routing_llm(state) == "routing_finalize"


def test_routing_finalize_node_sets_department_name():
    state = _routing_state(messages=[ai_message_text("Cardiology")])

    update = routing_finalize_node(state, config={"configurable": {}})

    assert update == {"department_name": "Cardiology"}


def test_routing_finalize_node_returns_none_for_unmatched():
    state = _routing_state(messages=[ai_message_text("UNMATCHED")])

    update = routing_finalize_node(state, config={"configurable": {}})

    assert update == {"department_name": None}


def test_routing_agent_node_resolves_department_id_by_name(monkeypatch, db_session):
    dept_name = f"Cardiology {uuid.uuid4().hex[:8]}"
    department = make_department(db_session, name=dept_name)
    fake_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("lookup_departments_tool", {"query_hint": "cardiology"}),
            ai_message_text(dept_name),
        ]
    )
    monkeypatch.setattr("app.agents.routing.get_llm", lambda: fake_model)

    state = workflow_state(request_text="I need a cardiology checkup")
    update = routing_agent_node(state, config={"configurable": {"db": db_session}})

    assert update["department_id"] == str(department.id)
    assert "escalation" not in update


def test_routing_agent_node_escalates_when_unmatched(monkeypatch, db_session):
    workflow_run = make_workflow_run(db_session)
    make_department(db_session, name=f"Cardiology {uuid.uuid4().hex[:8]}")
    fake_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("lookup_departments_tool", {"query_hint": "dermatology"}),
            ai_message_text("UNMATCHED"),
        ]
    )
    monkeypatch.setattr("app.agents.routing.get_llm", lambda: fake_model)

    state = workflow_state(
        workflow_run_id=str(workflow_run.id),
        request_text="I need to see a dermatologist about a rash",
    )
    update = routing_agent_node(state, config={"configurable": {"db": db_session}})

    assert update["department_id"] is None
    assert update["escalation"] is not None
    escalation = db_session.query(Escalation).filter(Escalation.workflow_run_id == workflow_run.id).one()
    assert "dermatologist" in escalation.reason


def test_routing_agent_node_escalates_when_name_not_found_in_db(monkeypatch, db_session):
    workflow_run = make_workflow_run(db_session)
    fake_model = FakeToolCallingModel(
        [
            ai_message_with_tool_call("lookup_departments_tool", {"query_hint": "x"}),
            ai_message_text("Neurology"),
        ]
    )
    monkeypatch.setattr("app.agents.routing.get_llm", lambda: fake_model)

    state = workflow_state(workflow_run_id=str(workflow_run.id), request_text="book neurology")
    update = routing_agent_node(state, config={"configurable": {"db": db_session}})

    assert update["department_id"] is None
    assert update["escalation"] is not None
