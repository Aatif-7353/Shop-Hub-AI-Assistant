"""R4 end-to-end: injection, PII before the model, fail-closed."""
from langchain_core.messages import AIMessage

from common import inproc
from common.llm import FakeChatModel
from orchestrator.graph import build_graph
from tests.conftest import ACME, BLOOM, ORPHAN, Chat, answer, of
from langgraph.checkpoint.memory import MemorySaver


async def test_instruction_text_in_notes_does_not_steer_the_assistant(acme, bloom):
    for chat, q, oid in [(acme, "show order 1003", 1003), (bloom, "show order 2005", 2005), (bloom, "show order 2002", 2002)]:
        ev = await chat.say(q)
        assert of(ev, "proposal") == [], q
        assert "refund_order" not in [e["name"] for e in of(ev, "tool_started")]
        assert "removed and ignored" in answer(ev)
        assert inproc.order(oid)["status"] in ("PAID", "PLACED")


async def test_even_a_fooled_model_cannot_move_money_without_approval(stack, monkeypatch):
    """Disable the sanitiser so the naive fake obeys the note. The approval gate must still hold."""
    import orders_agent.main as oa
    monkeypatch.setattr(oa, "sanitize_note", lambda n: {"text": n, "injection_detected": False, "trust": "x"})
    c = Chat(build_graph(MemorySaver()), ACME)
    ev = await c.say("show order 1003")
    assert of(ev, "proposal"), "naive model should have been fooled into asking for a refund"
    assert inproc.order(1003)["status"] == "PAID"            # ...but nothing executed
    await c.decide("reject")
    assert inproc.order(1003)["status"] == "PAID"


class SpyModel(FakeChatModel):
    seen: list = []

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        SpyModel.seen.append("\n".join(str(m.content) for m in messages))
        return super()._generate(messages, stop, run_manager, **kw)

    def _stream(self, messages, stop=None, run_manager=None, **kw):
        SpyModel.seen.append("\n".join(str(m.content) for m in messages))
        return super()._stream(messages, stop, run_manager, **kw)


async def test_emails_and_phones_never_reach_the_model(stack):
    SpyModel.seen = []
    c = Chat(build_graph(MemorySaver(), llm=SpyModel()), ACME)
    await c.say("show order 1001 for maria.lopez@example.com phone +91 98450 11001")   # user types PII too
    await c.say("what did Maria order last?")
    blob = "\n".join(SpyModel.seen)
    assert len(SpyModel.seen) >= 3 and "Maria Lopez" in blob
    assert "@" not in blob and "98450" not in blob and "example.com" not in blob


async def test_valid_token_without_merchant_is_rejected_by_the_graph(graph):
    ev = await Chat(graph, ORPHAN).say("show order 1001")
    assert "Access denied" in answer(ev)
    assert of(ev, "tool_started") == []                     # no tool, no model call


async def test_unknown_tool_and_bad_args_become_data_not_crashes(stack):
    class Weird(FakeChatModel):
        def _decide(self, messages):
            if isinstance(messages[-1], AIMessage) or messages[-1].type == "tool":
                return AIMessage(content="handled")
            return AIMessage(content="", tool_calls=[
                {"name": "drop_database", "args": {}, "id": "t1", "type": "tool_call"},
                {"name": "lookup_order", "args": {"id": "abc"}, "id": "t2", "type": "tool_call"}])
    ev = await Chat(build_graph(MemorySaver(), llm=Weird()), ACME).say("go")
    assert answer(ev) == "handled"
