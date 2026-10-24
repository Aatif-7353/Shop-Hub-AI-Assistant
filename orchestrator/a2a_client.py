"""Calls the orders sub-agent over HTTP. Discovery is from the agent card; no Python import of the agent."""
import httpx

from common import config
from common.http import client
from common.obs import CID_HEADER, log

_CARDS: dict[str, dict] = {}  # the card is public + credential-free, so caching it is safe


def reset_cache() -> None:
    _CARDS.clear()


class Secret:
    """Wraps the bearer token so it is never copied into checkpoint metadata or logs."""
    __slots__ = ("_v",)

    def __init__(self, v: str):
        self._v = v

    def reveal(self) -> str:
        return self._v

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__


async def discover(base: str | None = None) -> dict:
    base = (base or config.orders_agent_url()).rstrip("/")
    if base not in _CARDS:
        async with client() as c:
            r = await c.get(f"{base}/.well-known/agent-card.json")
            r.raise_for_status()
            _CARDS[base] = r.json()
    return _CARDS[base]


async def call_skill(skill: str, args: dict, token: Secret, cid: str | None = None) -> dict:
    """Returns the skill's data dict; transport/protocol problems are returned as {'ok': False,...} data."""
    try:
        card = await discover()
        if skill not in {s["id"] for s in card.get("skills", [])}:
            return {"ok": False, "code": "unsupported", "error": f"Sub-agent has no skill '{skill}'"}
        headers = {"Authorization": f"Bearer {token.reveal()}"}  # forwarded unchanged
        if cid:
            headers[CID_HEADER] = cid
        payload = {"jsonrpc": "2.0", "id": cid or "1", "method": "message/send", "params": {
            "message": {"role": "user", "parts": [{"kind": "data", "data": {"skill": skill, "args": args}}]}}}
        async with client() as c:
            r = await c.post(card["url"], json=payload, headers=headers)
        if r.status_code == 401:
            return {"ok": False, "code": "unauthorized", "error": "Unauthorized"}
        body = r.json()
        if "error" in body:
            return {"ok": False, "code": "rpc_error", "error": body["error"].get("message", "RPC error")}
        return body["result"]["parts"][0]["data"]
    except (httpx.HTTPError, KeyError, ValueError) as e:
        log("orchestrator", "subagent_unavailable", cid, skill=skill, err=type(e).__name__)
        return {"ok": False, "code": "unavailable", "error": "The orders service is unavailable right now."}
