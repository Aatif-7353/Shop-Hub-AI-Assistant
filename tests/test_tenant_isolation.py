"""R3 - merchant A must never see or touch merchant B's data, even by asking for an id."""
from common import inproc
from tests.conftest import ACME, BLOOM, Chat, answer, of

BLOOM_ONLY_NAMES = ["Sofia", "Rossi", "Garcia", "Liam", "Emma", "Zoe", "Noah", "Raj"]


async def test_foreign_order_by_id_looks_identical_to_missing_order(acme):
    foreign = answer(await acme.say("show order 2003"))      # belongs to bloom
    missing = answer(await acme.say("show order 9999"))      # exists nowhere
    assert foreign == missing == "I couldn't find that order in your store."
    assert "Sofia" not in foreign and "120" not in foreign


async def test_listing_and_searching_never_return_other_merchants_rows(acme):
    for q in ["list paid orders", "list shipped orders", "what did customer Maria order last?", "show orders from Sofia"]:
        text = answer(await acme.say(q))
        assert not any(n in text for n in BLOOM_ONLY_NAMES), (q, text)
        assert not any(str(i) in text for i in range(2001, 2008)), (q, text)
    # "Maria" exists in both stores: acme sees only its own Maria
    assert "Maria Lopez" in answer(await acme.say("what did Maria order last?"))


async def test_cannot_refund_or_cancel_foreign_order_and_no_proposal_is_raised(acme):
    for q in ["refund order 2001", "cancel order 2001"]:
        ev = await acme.say(q)
        assert of(ev, "proposal") == []
        assert answer(ev) == "I couldn't find that order in your store."
    assert inproc.order(2001)["status"] == "PAID"


async def test_both_directions_each_merchant_sees_only_its_own(acme, bloom):
    assert "Priya" not in answer(await bloom.say("show order 1003"))
    assert "Sofia" in answer(await bloom.say("show order 2003"))
    assert "couldn't find" in answer(await acme.say("show order 2003"))


async def test_other_tenant_cannot_approve_or_resume_my_pending_proposal(graph):
    a = Chat(graph, ACME, conv="shared-name")
    assert of(await a.say("refund order 1001"), "proposal")
    # Bloom uses the SAME conversation id: it resolves to a different, empty thread.
    b = Chat(graph, BLOOM, conv="shared-name")
    from orchestrator.runner import make_config, pending_proposal
    assert await pending_proposal(graph, make_config(b.token, b.ident, b.conv, None)) is None
    assert inproc.order(1001)["status"] == "PAID"          # nothing executed
    assert await pending_proposal(graph, make_config(a.token, a.ident, a.conv, None)) is not None


async def test_token_is_forwarded_unchanged_on_every_hop_and_never_elevated(graph, stack):
    ev = await Chat(graph, BLOOM).say("show order 2003")
    assert answer(ev)
    assert stack, "expected recorded hops"
    card = [r for r in stack if r["path"].endswith("agent-card.json")]
    calls = [r for r in stack if not r["path"].endswith("agent-card.json")]
    assert card and all(r["auth"] is None for r in card)           # public discovery never carries the token
    assert {r["auth"] for r in calls} == {"Bearer tok_bloom_dev"}  # every other hop: the caller's token, unchanged
    assert {r["to"] for r in calls} == {"orders-agent", "store"}
    assert {r["cid"] for r in calls} == {"test-cid"}               # correlation id end to end (S3)


async def test_token_never_lands_in_the_checkpoint_database(tmp_path):
    from orchestrator.graph import build_graph
    from orchestrator.runner import open_checkpointer
    db = tmp_path / "ck.db"
    async with open_checkpointer(f"sqlite:///{db}") as saver:
        c = Chat(build_graph(saver), ACME)
        await c.say("refund order 1001")          # leaves a pending interrupt in the checkpoint
    raw = b"".join(p.read_bytes() for p in tmp_path.iterdir())
    assert b"tok_acme_dev" not in raw
