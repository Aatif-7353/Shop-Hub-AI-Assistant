"""R7 persistence across restart + R6 SSE over HTTP."""
import json

import httpx

from chat_api.main import app
from common import inproc
from orchestrator.graph import build_graph
from orchestrator.runner import open_checkpointer
from tests.conftest import ACME, Chat, answer, of


async def test_conversation_and_pending_proposal_survive_a_restart(tmp_path):
    url = f"sqlite:///{tmp_path/'ck.db'}"
    async with open_checkpointer(url) as saver:            # "process 1"
        c1 = Chat(build_graph(saver), ACME, conv="persist")
        assert of(await c1.say("refund order 1001"), "proposal")
    async with open_checkpointer(url) as saver:            # "process 2": brand-new graph + connection
        c2 = Chat(build_graph(saver), ACME, conv="persist")
        from orchestrator.runner import make_config, pending_proposal
        assert (await pending_proposal(c2.graph, make_config(c2.token, c2.ident, "persist", None)))["order_id"] == 1001
        assert inproc.order(1001)["status"] == "PAID"
        ev = await c2.decide("approve")
        assert inproc.order(1001)["status"] == "REFUNDED" and "REFUNDED" in answer(ev)
        hist = (await c2.graph.aget_state(make_config(c2.token, c2.ident, "persist", None))).values["messages"]
        assert hist[0].content == "refund order 1001"      # earlier turn is still there


def parse_sse(text: str) -> list[dict]:
    return [json.loads(b.split("data: ", 1)[1]) for b in text.strip().split("\n\n") if "data: " in b]


async def test_sse_stream_has_tool_events_tokens_proposal_and_done(graph):
    app.state.graph = graph
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://chat") as c:
        h = {"Authorization": "Bearer tok_acme_dev"}
        r = await c.post("/chat", headers=h, json={"conversation_id": "s1", "message": "show order 1002"})
        ev = parse_sse(r.text)
        kinds = [e["event"] for e in ev]
        assert kinds[0] == "start" and kinds[-1] == "done"
        assert kinds.index("tool_started") < kinds.index("tool_finished") < kinds.index("token")
        assert r.headers["x-correlation-id"] == ev[0]["correlation_id"]

        r = await c.post("/chat", headers=h, json={"conversation_id": "s2", "message": "refund order 1001"})
        assert "proposal" in [e["event"] for e in parse_sse(r.text)]
        blocked = await c.post("/chat", headers=h, json={"conversation_id": "s2", "message": "hello"})
        assert blocked.status_code == 409                   # must decide first
        r = await c.post("/chat/s2/approve", headers=h)
        assert "REFUNDED" in parse_sse(r.text)[-1]["answer"]
        assert (await c.post("/chat/s2/approve", headers=h)).status_code == 409     # no double execution
        hist = (await c.get("/chat/s2", headers=h)).json()
        assert hist["messages"][0]["text"] == "refund order 1001" and hist["pending_proposal"] is None


async def test_chat_api_fails_closed_at_the_edge(graph):
    app.state.graph = graph
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://chat") as c:
        body = {"conversation_id": "x", "message": "show order 1001"}
        assert (await c.post("/chat", json=body)).status_code == 401
        assert (await c.post("/chat", json=body, headers={"Authorization": "Bearer bogus"})).status_code == 401
        assert (await c.post("/chat", json=body, headers={"Authorization": "Bearer tok_orphan_dev"})).status_code == 403
        assert (await c.post("/chat/x/approve", headers={"Authorization": "Bearer tok_bloom_dev"})).status_code == 409


def test_orchestrator_never_imports_the_orders_agent():
    import pathlib, re
    for f in pathlib.Path("orchestrator").glob("*.py"):
        assert not re.search(r"^\s*(from|import)\s+orders_agent", f.read_text(), re.M), f
