"""R2 - Orders sub-agent. A separate process exposing:

  GET  /.well-known/agent-card.json   public discovery document
  POST /rpc                           JSON-RPC 2.0, method `message/send` (A2A-shaped)

It owns the order tools, talks to the store API with the caller's bearer token
(forwarded unchanged, never stored) and returns *sanitised, redacted* data.
"""
import os
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from common import config
from common.guardrails import redact_text, sanitize_note
from common.http import client
from common.obs import CID_HEADER, log

app = FastAPI(title="ShopHub orders agent")

SKILLS = [
    {"id": "whoami", "name": "whoami", "description": "Identify the caller (user, merchant).", "write": False},
    {"id": "lookup_order", "name": "lookup_order", "description": "Look up one order by id.", "write": False},
    {"id": "list_orders", "name": "list_orders", "description": "List orders, optionally by status and minimum age in days.", "write": False},
    {"id": "search_orders", "name": "search_orders", "description": "Find orders by customer name, newest first.", "write": False},
    {"id": "preview_change", "name": "preview_change", "description": "Read-only preview of a refund/cancel (no side effects).", "write": False},
    {"id": "refund_order", "name": "refund_order", "description": "EXECUTE a refund. Caller must have human approval.", "write": True},
    {"id": "cancel_order", "name": "cancel_order", "description": "EXECUTE a cancellation. Caller must have human approval.", "write": True},
]

# Domain rules for previews. The store API enforces the same rules authoritatively (409).
ACTIONS = {
    "refund": ({"PAID", "SHIPPED", "DELIVERED"}, "REFUNDED"),
    "cancel": ({"PLACED", "PAID"}, "CANCELLED"),
}


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/.well-known/agent-card.json")
def agent_card(request: Request):
    base = os.getenv("PUBLIC_URL") or config.orders_agent_url()
    return {
        "name": "ShopHub Orders Agent",
        "description": "Reads and (with approval) changes orders for the caller's merchant.",
        "version": "0.1.0",
        "url": f"{base}/rpc",
        "protocol": "jsonrpc-2.0",
        "methods": ["message/send"],
        "authentication": {"schemes": ["bearer"], "note": "Forward the end-user's bearer token unchanged."},
        "capabilities": {"streaming": False},
        "skills": SKILLS,
    }


# ---------------------------------------------------------------- helpers
def _err(code: str, msg: str) -> dict:
    return {"ok": False, "code": code, "error": msg}


def _age_days(iso: str) -> float:
    return round((datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() / 86400, 1)


def shape_order(o: dict) -> dict:
    """Whitelist fields: email/phone are NOT forwarded at all; free text is sanitised+redacted."""
    return {
        "id": o["id"],
        "customer_name": redact_text(o["customer_name"]),
        "status": o["status"],
        "total": o["total"],
        "placed_at": o["placed_at"],
        "age_days": _age_days(o["placed_at"]),
        "customer_note": sanitize_note(o.get("customer_note")),
    }


class Ctx:
    def __init__(self, token: str, cid: str | None):
        self.token, self.cid = token, cid

    def headers(self) -> dict:
        h = {"Authorization": f"Bearer {self.token}"}  # forwarded unchanged
        if self.cid:
            h[CID_HEADER] = self.cid
        return h

    async def store(self, method: str, path: str, **kw):
        async with client() as c:
            return await c.request(method, f"{config.store_api_url()}{path}", headers=self.headers(), **kw)


def _http_problem(r) -> dict | None:
    if r.status_code == 401:
        return _err("unauthorized", "Your session is not valid.")
    if r.status_code == 403:
        return _err("forbidden", "Your account is not attached to a store.")
    if r.status_code == 404:
        return _err("not_found", "Order not found")
    if r.status_code == 409:
        return _err("conflict", r.json().get("detail", "Invalid state transition"))
    if r.status_code >= 400:
        return _err("upstream_error", f"Store API error {r.status_code}")
    return None


async def _lookup(ctx: Ctx, oid: int) -> dict:
    r = await ctx.store("GET", "/orders/lookup", params={"id": oid})
    if (p := _http_problem(r)):
        return p
    body = r.json()
    # LEGACY ENVELOPE: HTTP 200 does not mean success. Normalise to a real error.
    if body.get("status") is not True or "order" not in body:
        return _err("not_found", body.get("message") or "Order not found")
    return {"ok": True, "order": shape_order(body["order"])}


# ---------------------------------------------------------------- skills
async def whoami(ctx: Ctx, a: dict) -> dict:
    r = await ctx.store("GET", "/auth/me")
    if (p := _http_problem(r)):
        return p
    return {"ok": True, **r.json()}


async def lookup_order(ctx: Ctx, a: dict) -> dict:
    return await _lookup(ctx, int(a["id"]))


async def list_orders(ctx: Ctx, a: dict) -> dict:
    params = {"status": a["status"]} if a.get("status") else {}
    r = await ctx.store("GET", "/orders", params=params)
    if (p := _http_problem(r)):
        return p
    rows = [shape_order(o) for o in r.json()]
    if a.get("older_than_days") is not None:
        rows = [o for o in rows if o["age_days"] >= float(a["older_than_days"])]
    rows.sort(key=lambda o: o["placed_at"], reverse=True)
    return {"ok": True, "count": len(rows), "orders": rows}


async def search_orders(ctx: Ctx, a: dict) -> dict:
    needle = str(a["customer"]).strip().lower()
    r = await ctx.store("GET", "/orders")
    if (p := _http_problem(r)):
        return p
    rows = [shape_order(o) for o in r.json() if needle and needle in o["customer_name"].lower()]
    rows.sort(key=lambda o: o["placed_at"], reverse=True)
    return {"ok": True, "count": len(rows), "orders": rows}


async def preview_change(ctx: Ctx, a: dict) -> dict:
    action = a.get("action")
    if action not in ACTIONS:
        return _err("bad_request", "action must be 'refund' or 'cancel'")
    res = await _lookup(ctx, int(a["id"]))
    if not res["ok"]:
        return res
    allowed, new_status = ACTIONS[action]
    o = res["order"]
    if o["status"] not in allowed:
        return _err("conflict", f"Order {o['id']} is {o['status']}; it cannot be {action}ed.")
    return {"ok": True, "action": action, "order_id": o["id"], "customer_name": o["customer_name"],
            "current_status": o["status"], "new_status": new_status, "amount": o["total"]}


async def _execute(ctx: Ctx, a: dict, path: str) -> dict:
    r = await ctx.store("POST", path, json={"id": int(a["id"])})
    if (p := _http_problem(r)):
        return p
    o = r.json()
    return {"ok": True, "executed": True, "order_id": o["id"], "status": o["status"], "amount": o["total"]}


async def refund_order(ctx: Ctx, a: dict) -> dict:
    return await _execute(ctx, a, "/orders/refund")


async def cancel_order(ctx: Ctx, a: dict) -> dict:
    return await _execute(ctx, a, "/orders/cancel")


HANDLERS = {s["id"]: globals()[s["id"]] for s in SKILLS}
REQUIRED = {"lookup_order": ["id"], "search_orders": ["customer"], "preview_change": ["id", "action"],
            "refund_order": ["id"], "cancel_order": ["id"]}


# ---------------------------------------------------------------- JSON-RPC
def _rpc_error(id_, code: int, message: str, status: int = 200):
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}, status_code=status)


@app.post("/rpc")
async def rpc(request: Request):
    auth = request.headers.get("authorization", "")
    cid = request.headers.get(CID_HEADER)
    try:
        req = await request.json()
    except Exception:
        return _rpc_error(None, -32700, "Parse error")
    if not isinstance(req, dict) or req.get("jsonrpc") != "2.0" or "method" not in req:
        return _rpc_error(req.get("id") if isinstance(req, dict) else None, -32600, "Invalid Request")
    id_ = req.get("id")
    if req["method"] != "message/send":
        return _rpc_error(id_, -32601, "Method not found")
    if not auth.lower().startswith("bearer "):
        return _rpc_error(id_, -32001, "Unauthorized: bearer token required", status=401)
    token = auth.split(" ", 1)[1].strip()

    try:
        part = req["params"]["message"]["parts"][0]
        skill, args = part["data"]["skill"], part["data"].get("args", {})
        assert isinstance(args, dict)
    except Exception:
        return _rpc_error(id_, -32602, "Invalid params: expected params.message.parts[0].data{skill,args}")
    if skill not in HANDLERS:
        return _rpc_error(id_, -32602, f"Unknown skill '{skill}'")
    if any(k not in args for k in REQUIRED.get(skill, [])):
        return _rpc_error(id_, -32602, f"Missing arguments for '{skill}': need {REQUIRED[skill]}")

    try:
        data = await HANDLERS[skill](Ctx(token, cid), args)
    except (ValueError, TypeError):
        return _rpc_error(id_, -32602, "Invalid argument types")
    except Exception:
        log("orders-agent", "skill_failed", cid, skill=skill)
        return _rpc_error(id_, -32603, "Internal error")
    log("orders-agent", "skill_done", cid, skill=skill, ok=data.get("ok"))
    return {"jsonrpc": "2.0", "id": id_,
            "result": {"kind": "message", "role": "agent", "parts": [{"kind": "data", "data": data}]}}
