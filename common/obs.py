"""Structured JSON logs + correlation id helpers (stretch S3)."""
import json
import sys
import time
import uuid

CID_HEADER = "X-Correlation-Id"


def new_cid() -> str:
    return uuid.uuid4().hex[:16]


def log(service: str, event: str, cid: str | None = None, **fields) -> None:
    """One JSON line per event. Never pass tokens or raw PII here."""
    rec = {"ts": round(time.time(), 3), "service": service, "event": event, "cid": cid, **fields}
    print(json.dumps(rec, default=str), file=sys.stderr, flush=True)
