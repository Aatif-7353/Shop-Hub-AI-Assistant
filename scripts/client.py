#!/usr/bin/env python
"""Minimal SSE client for the chat API (R6 demo).

  python scripts/client.py "show order 1001"
  python scripts/client.py "refund order 1001"      # -> proposal
  python scripts/client.py --approve                 # or --reject
  python scripts/client.py --history
Env: CHAT_URL (default http://localhost:8000)   Flags: --token tok_acme_dev --conv demo
"""
import argparse
import json
import os
import sys

import httpx


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("message", nargs="?")
    ap.add_argument("--token", default="tok_acme_dev")
    ap.add_argument("--conv", default="demo")
    ap.add_argument("--approve", action="store_true")
    ap.add_argument("--reject", action="store_true")
    ap.add_argument("--history", action="store_true")
    a = ap.parse_args()
    base, h = os.getenv("CHAT_URL", "http://localhost:8000"), {"Authorization": f"Bearer {a.token}"}
    if a.history:
        r = httpx.get(f"{base}/chat/{a.conv}", headers=h)
        print(json.dumps(r.json(), indent=2))
        return 0 if r.is_success else 1
    if a.approve or a.reject:
        req = ("POST", f"{base}/chat/{a.conv}/{'approve' if a.approve else 'reject'}", None)
    elif a.message:
        req = ("POST", f"{base}/chat", {"conversation_id": a.conv, "message": a.message})
    else:
        ap.error("give a message, --approve, --reject or --history")
    with httpx.stream(req[0], req[1], headers=h, json=req[2], timeout=60) as r:
        if r.status_code >= 400:
            r.read()
            print(f"HTTP {r.status_code}: {r.text}")
            return 1
        for line in r.iter_lines():
            if line.startswith("data: "):
                ev = json.loads(line[6:])
                k = ev["event"]
                if k == "token":
                    print(ev["text"], end="", flush=True)
                elif k == "tool_started":
                    print(f"\n[tool started]  {ev['name']} {ev['args']}")
                elif k == "tool_finished":
                    print(f"[tool finished] {ev['name']} ok={ev['ok']}")
                elif k == "proposal":
                    print(f"\n[PROPOSAL] {ev['proposal']['summary']}  (approve with --approve / --reject)")
                elif k == "done":
                    print(f"\n[done] pending_proposal={ev['pending_proposal']}")
                elif k == "start":
                    print(f"[start] correlation_id={ev['correlation_id']}")
                else:
                    print(f"[{k}] {ev}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except httpx.ConnectError:
        print(f"Cannot reach the chat API at {os.getenv('CHAT_URL', 'http://localhost:8000')}. Is the stack up?", file=sys.stderr)
        sys.exit(2)
