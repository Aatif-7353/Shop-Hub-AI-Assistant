"""R5 - refund/cancel never executes without an explicit approve."""
from common import inproc
from tests.conftest import answer, of


async def test_refund_creates_proposal_and_does_not_execute(acme):
    ev = await acme.say("Refund order 1001")
    [p] = of(ev, "proposal")
    pr = p["proposal"]
    assert (pr["order_id"], pr["merchant"], pr["action"]) == (1001, "acme", "refund")
    assert (pr["current_status"], pr["new_status"], pr["amount"]) == ("PAID", "REFUNDED", 129.99)
    assert of(ev, "done")[0]["pending_proposal"] is True
    assert inproc.order(1001)["status"] == "PAID"           # untouched


async def test_approve_executes_exactly_once(acme):
    await acme.say("refund order 1001")
    ev = await acme.decide("approve")
    assert inproc.order(1001)["status"] == "REFUNDED"
    assert "now REFUNDED" in answer(ev)
    assert [e["name"] for e in of(ev, "tool_finished")][-1] == "refund_order"


async def test_reject_does_not_execute(acme):
    await acme.say("refund order 1001")
    ev = await acme.decide("reject")
    assert inproc.order(1001)["status"] == "PAID"
    assert "did not change order 1001" in answer(ev)
    assert "refund_order" not in [e["name"] for e in of(ev, "tool_started")]


async def test_cancel_follows_the_same_flow(acme):
    ev = await acme.say("cancel order 1005")                 # PLACED
    assert of(ev, "proposal")[0]["proposal"]["new_status"] == "CANCELLED"
    assert inproc.order(1005)["status"] == "PLACED"
    await acme.decide("approve")
    assert inproc.order(1005)["status"] == "CANCELLED"


async def test_invalid_transition_never_becomes_a_proposal(acme):
    ev = await acme.say("refund order 1006")                 # already CANCELLED
    assert of(ev, "proposal") == []
    assert "can't be done" in answer(ev)


async def test_conversation_continues_after_decision(acme):
    await acme.say("refund order 1001")
    await acme.decide("reject")
    assert "Order 1002" in answer(await acme.say("show order 1002"))
