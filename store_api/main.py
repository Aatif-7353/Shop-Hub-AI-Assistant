"""R0 - tiny in-memory mock store. Everything is scoped to the caller's merchant."""
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from common.obs import CID_HEADER, log
from store_api.seed import TOKENS, seed_orders

app = FastAPI(title="ShopHub mock store")
ORDERS: list[dict] = seed_orders()

REFUNDABLE = {"PAID", "SHIPPED", "DELIVERED"}
CANCELLABLE = {"PLACED", "PAID"}


def reset() -> None:
    """Re-seed (used by tests and evals)."""
    ORDERS[:] = seed_orders()


@app.middleware("http")
async def _log(request: Request, call_next):
    resp = await call_next(request)
    # Never log the Authorization header.
    log("store", "request", request.headers.get(CID_HEADER), path=request.url.path, status=resp.status_code)
    return resp


def caller(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    info = TOKENS.get(authorization.split(" ", 1)[1].strip())
    if info is None:
        raise HTTPException(401, "Invalid token")
    return info


def merchant_of(c: dict = Depends(caller)) -> str:
    if c["merchant"] is None:  # fail closed: valid token, no store
        raise HTTPException(403, "User is not attached to a merchant")
    return c["merchant"]


def _mine(merchant: str) -> list[dict]:
    return [o for o in ORDERS if o["merchant"] == merchant]


def _find(merchant: str, oid: int) -> dict | None:
    return next((o for o in _mine(merchant) if o["id"] == oid), None)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/auth/me")
def me(c: dict = Depends(caller)):
    return {"user": c["user"], "merchant": c["merchant"]}


@app.get("/orders")
def list_orders(status: str | None = None, merchant: str = Depends(merchant_of)):
    rows = _mine(merchant)
    if status:
        rows = [o for o in rows if o["status"] == status.upper()]
    return rows


@app.get("/orders/lookup")
def lookup(id: int, c: dict = Depends(caller)):
    """LEGACY: always HTTP 200. Failure is signalled in the body, and a foreign order
    looks exactly like a missing one."""
    if c["merchant"] is None:
        return {"status": False, "message": "Order not found"}
    order = _find(c["merchant"], id)
    if order is None:
        return {"status": False, "message": "Order not found"}
    return {"status": True, "order": order}


class IdBody(BaseModel):
    id: int


def _transition(body: IdBody, merchant: str, allowed: set[str], new: str) -> dict:
    order = _find(merchant, body.id)
    if order is None:  # unknown and foreign are indistinguishable
        raise HTTPException(404, "Order not found")
    if order["status"] not in allowed:
        raise HTTPException(409, f"Cannot move order from {order['status']} to {new}")
    order["status"] = new
    return order


@app.post("/orders/refund")
def refund(body: IdBody, merchant: str = Depends(merchant_of)):
    return _transition(body, merchant, REFUNDABLE, "REFUNDED")


@app.post("/orders/cancel")
def cancel(body: IdBody, merchant: str = Depends(merchant_of)):
    return _transition(body, merchant, CANCELLABLE, "CANCELLED")
