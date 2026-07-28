import pytest

from app.graph import (
    needs_appointment_selection_node,
    needs_clarification_node,
    needs_intent_selection_node,
    route_after_document,
)


@pytest.mark.parametrize(
    "intent,expected",
    [
        ("book_appointment", "routing_agent"),
        ("booking", "routing_agent"),
        ("I want to book something", "routing_agent"),
        ("BOOK_APPOINTMENT", "routing_agent"),
        ("reschedule_appointment", "needs_appointment_selection"),
        ("reschedule my visit", "needs_appointment_selection"),
        ("cancel_appointment", "needs_appointment_selection"),
        ("I need to cancel", "needs_appointment_selection"),
        ("general_inquiry", "needs_clarification"),
        ("submit_document", "needs_clarification"),
        (None, "needs_clarification"),
        ("", "needs_clarification"),
        ("asdkjfh garbage", "needs_clarification"),
    ],
)
def test_route_after_document(intent, expected):
    state = {"intent": intent}
    assert route_after_document(state) == expected


def test_needs_clarification_node_sets_flag():
    update = needs_clarification_node({"intent": "general_inquiry"}, config={"configurable": {}})
    assert update == {"needs_clarification": True}


def test_needs_appointment_selection_node_sets_flags():
    cancel_update = needs_appointment_selection_node({"intent": "cancel_appointment"}, config={"configurable": {}})
    assert cancel_update == {"needs_appointment_selection": True, "pending_appointment_action": "cancel"}

    reschedule_update = needs_appointment_selection_node(
        {"intent": "reschedule my visit"}, config={"configurable": {}}
    )
    assert reschedule_update == {"needs_appointment_selection": True, "pending_appointment_action": "reschedule"}


def test_route_after_document_comma_separated_intent_routes_to_intent_selection():
    assert route_after_document({"intent": "cancel_appointment,book_appointment"}) == "needs_intent_selection"
    assert route_after_document({"intent": "book_appointment,cancel_appointment"}) == "needs_intent_selection"


def test_needs_intent_selection_node_sets_flag():
    update = needs_intent_selection_node({"intent": "cancel_appointment,book_appointment"}, config={"configurable": {}})
    assert update == {"needs_intent_selection": True}


def test_route_after_document_single_intent_unaffected_by_comma_check():
    assert route_after_document({"intent": "book_appointment"}) == "routing_agent"
    assert route_after_document({"intent": "cancel_appointment"}) == "needs_appointment_selection"
    assert route_after_document({"intent": "reschedule_appointment"}) == "needs_appointment_selection"
    assert route_after_document({"intent": "general_inquiry"}) == "needs_clarification"
