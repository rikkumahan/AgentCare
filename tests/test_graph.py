import pytest

from app.graph import needs_clarification_node, route_after_document


@pytest.mark.parametrize(
    "intent,expected",
    [
        ("book_appointment", "routing_agent"),
        ("booking", "routing_agent"),
        ("I want to book something", "routing_agent"),
        ("BOOK_APPOINTMENT", "routing_agent"),
        ("reschedule_appointment", "needs_clarification"),
        ("cancel_appointment", "needs_clarification"),
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
