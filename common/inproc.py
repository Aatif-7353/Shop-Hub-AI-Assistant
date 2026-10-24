"""Run store API + orders agent in-process behind ASGI transports (used by tests and evals).
The services still talk HTTP-shaped JSON to each other; only the socket is skipped."""
import os

import httpx

from common import http


class Recorder(httpx.AsyncBaseTransport):
    """Wraps a transport and records (url path, Authorization, correlation id) of each request."""

    def __init__(self, inner: httpx.AsyncBaseTransport, name: str, log: list):
        self.inner, self.name, self.log = inner, name, log

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.log.append({"to": self.name, "path": request.url.path,
                         "auth": request.headers.get("authorization"), "cid": request.headers.get("x-correlation-id")})
        return await self.inner.handle_async_request(request)


def wire() -> list:
    """Point all services at in-process apps, reseed the store, return the request log."""
    from orchestrator import a2a_client
    from orders_agent.main import app as orders_app
    from store_api import main as store_main

    os.environ["STORE_API_URL"] = "http://store.test"
    os.environ["ORDERS_AGENT_URL"] = "http://orders.test"
    os.environ["LLM_PROVIDER"] = os.environ.get("LLM_PROVIDER", "fake")
    seen: list = []
    http.OVERRIDES.clear()
    http.OVERRIDES["http://store.test"] = Recorder(httpx.ASGITransport(app=store_main.app), "store", seen)
    http.OVERRIDES["http://orders.test"] = Recorder(httpx.ASGITransport(app=orders_app), "orders-agent", seen)
    a2a_client.reset_cache()
    store_main.reset()
    return seen


def order(order_id: int) -> dict:
    from store_api import main as store_main
    return next(o for o in store_main.ORDERS if o["id"] == order_id)
