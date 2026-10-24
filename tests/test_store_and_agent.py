import httpx
import pytest

from common.http import client
from tests.conftest import ACME


def rpc(skill, args, token="tok_acme_dev", method="message/send"):
    return {"jsonrpc": "2.0", "id": 7, "method": method, "params": {"message": {"role": "user", "parts": [
        {"kind": "data", "data": {"skill": skill, "args": args}}]}}}


async def store(method, path, token="tok_acme_dev", **kw):
    async with client() as c:
        return await c.request(method, f"http://store.test{path}", headers={"Authorization": f"Bearer {token}"}, **kw)


async def test_store_legacy_lookup_is_200_with_status_false():
    r = await store("GET", "/orders/lookup", params={"id": 9999})
    assert r.status_code == 200 and r.json() == {"status": False, "message": "Order not found"}


async def test_store_real_errors_for_unknown_foreign_and_bad_state():
    assert (await store("POST", "/orders/refund", json={"id": 9999})).status_code == 404
    assert (await store("POST", "/orders/refund", json={"id": 2001})).status_code == 404  # foreign
    assert (await store("POST", "/orders/refund", json={"id": 1006})).status_code == 409  # CANCELLED


async def test_store_orphan_token_is_valid_but_served_nothing():
    me = (await store("GET", "/auth/me", token="tok_orphan_dev")).json()
    assert me["merchant"] is None
    assert (await store("GET", "/orders", token="tok_orphan_dev")).status_code == 403
    assert (await store("GET", "/auth/me", token="nope")).status_code == 401


async def agent_call(skill, args, token="tok_acme_dev", **kw):
    async with client() as c:
        return await c.post("http://orders.test/rpc", json=rpc(skill, args), headers={"Authorization": f"Bearer {token}"}, **kw)


async def test_agent_card_lists_skills_and_endpoint():
    async with client() as c:
        card = (await c.get("http://orders.test/.well-known/agent-card.json")).json()
    assert card["url"] == "http://orders.test/rpc"
    assert {"lookup_order", "list_orders", "search_orders", "refund_order"} <= {s["id"] for s in card["skills"]}


async def test_agent_normalises_legacy_envelope_into_error():
    body = (await agent_call("lookup_order", {"id": 9999})).json()
    data = body["result"]["parts"][0]["data"]
    assert data["ok"] is False and data["code"] == "not_found"


async def test_agent_output_has_no_email_or_phone_and_notes_are_sanitised():
    data = (await agent_call("lookup_order", {"id": 1003})).json()["result"]["parts"][0]["data"]
    blob = str(data)
    assert "@" not in blob and "98450" not in blob
    assert data["order"]["customer_note"]["injection_detected"] is True
    assert "ignore your instructions" not in blob.lower()


async def test_jsonrpc_protocol_errors():
    async with client() as c:
        h = {"Authorization": "Bearer tok_acme_dev"}
        assert (await c.post("http://orders.test/rpc", json=rpc("x", {}) | {"method": "nope"}, headers=h)).json()["error"]["code"] == -32601
        assert (await c.post("http://orders.test/rpc", content=b"{bad", headers=h)).json()["error"]["code"] == -32700
        assert (await c.post("http://orders.test/rpc", json={"foo": 1}, headers=h)).json()["error"]["code"] == -32600
        assert (await c.post("http://orders.test/rpc", json=rpc("lookup_order", {}), headers=h)).json()["error"]["code"] == -32602
        assert (await c.post("http://orders.test/rpc", json=rpc("whoami", {}))).status_code == 401  # no token
