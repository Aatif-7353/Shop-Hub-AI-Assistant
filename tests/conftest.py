import pytest
from langgraph.checkpoint.memory import MemorySaver

from common import inproc
from orchestrator.graph import build_graph
from orchestrator.runner import make_config, stream_events, user_input

ACME = ("tok_acme_dev", {"user": "sam@acme-gadgets.test", "merchant": "acme"})
BLOOM = ("tok_bloom_dev", {"user": "lee@bloom-boutique.test", "merchant": "bloom"})
ORPHAN = ("tok_orphan_dev", {"user": "ghost@nowhere.test", "merchant": "none"})


@pytest.fixture(autouse=True)
def stack():
    """Fresh seeded store + in-process wiring for every test. Yields the request log."""
    return inproc.wire()


@pytest.fixture
def graph():
    return build_graph(MemorySaver())


class Chat:
    """Tiny driver around the graph that collects events like a client would."""

    def __init__(self, graph, who, conv="conv1"):
        self.graph, (self.token, self.ident), self.conv = graph, who, conv

    async def _run(self, payload):
        cfg = make_config(self.token, self.ident, self.conv, "test-cid")
        return [e async for e in stream_events(self.graph, payload, cfg)]

    async def say(self, text):
        return await self._run(user_input(text))

    async def decide(self, decision):
        from orchestrator.runner import resume
        return await self._run(resume(decision))


@pytest.fixture
def acme(graph):
    return Chat(graph, ACME)


@pytest.fixture
def bloom(graph):
    return Chat(graph, BLOOM)


def answer(events):
    return next(e for e in reversed(events) if e["event"] == "done")["answer"]


def of(events, kind):
    return [e for e in events if e["event"] == kind]
