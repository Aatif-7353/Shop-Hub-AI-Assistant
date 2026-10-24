#!/usr/bin/env bash
# R7 demo against the docker-compose stack: start a conversation, restart the orchestrator, continue it.
set -euo pipefail
C="python scripts/client.py --conv restart-demo"
echo "== 1. ask for a refund (creates a pending proposal; nothing executes)"; $C "refund order 1001"
echo; echo "== 2. restarting the orchestrator container (chat API + graph)"; docker compose restart orchestrator >/dev/null
for i in $(seq 1 30); do curl -sf localhost:8000/healthz >/dev/null && break; sleep 1; done
echo "== 3. history after restart: the proposal is still pending"; $C --history
echo "== 4. approve after restart"; $C --approve
