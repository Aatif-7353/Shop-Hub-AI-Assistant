"""Single place that builds httpx clients.

Production: plain network clients. Tests/evals: `OVERRIDES` maps a base URL to an ASGI
transport so the three services run in one process but still talk over (simulated) HTTP.
"""
import httpx

OVERRIDES: dict[str, httpx.AsyncBaseTransport] = {}


def client(timeout: float = 10.0) -> httpx.AsyncClient:
    mounts = {k: v for k, v in OVERRIDES.items()}
    return httpx.AsyncClient(timeout=timeout, mounts=mounts or None)
