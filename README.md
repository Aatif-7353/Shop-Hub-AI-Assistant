# ShopHub order-support assistant

Multi-tenant, tool-using support agent for an e-commerce platform. Merchant staff chat with an assistant that
answers questions about **their own store's** orders and can *propose* refunds/cancellations, which only run
after an explicit human approval. Python 3.11+, LangGraph, FastAPI. Design notes: [DESIGN.md](DESIGN.md).

```
client ──SSE──> chat API ──> orchestrator (LangGraph) ──JSON-RPC 2.0──> orders sub-agent ──HTTP──> store API
        (same bearer token forwarded unchanged at every hop)           (found via its agent card)
```

## Run it (fake LLM, no API key, fully offline)

```bash
docker compose up --build            # store-api :8001, orders-agent :8002, orchestrator/chat :8000, postgres
python scripts/client.py "show order 1001"          # needs only `pip install httpx`
python scripts/client.py "refund order 1001"        # -> a PROPOSAL, nothing executes
python scripts/client.py --approve                  # or --reject
python scripts/client.py --token tok_bloom_dev --conv b "show order 1001"   # other tenant: "couldn't find"
```

Dev tokens (fake, from `store_api/seed.py`): `tok_acme_dev` (Acme, orders 1001-1007),
`tok_bloom_dev` (Bloom, orders 2001-2007), `tok_orphan_dev` (valid token, **no** merchant -> rejected).

**Restart / persistence demo (R7):** `./scripts/restart_demo.sh` starts a refund conversation, restarts the
orchestrator container, shows the pending proposal is still there, then approves it.

### Without Docker
```bash
python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
uvicorn store_api.main:app --port 8001 &
uvicorn orders_agent.main:app --port 8002 &
uvicorn chat_api.main:app --port 8000          # checkpointer defaults to SQLite ./checkpoints.db
```
Configuration is by environment variable (see `.env.example`): `LLM_PROVIDER`, `STORE_API_URL`,
`ORDERS_AGENT_URL`, `CHECKPOINT_DB_URL` (`postgresql://...` or `sqlite:///file.db`).

## Tests and evals

```bash
pip install -e ".[dev]"
pytest                               # no API key, no network, ~6 s
python -m evals.run_evals            # fake-LLM mode (default): per-case PASS/FAIL, exit 1 on any failure
python -m evals.run_evals -v         # show every individual check
python -m evals.run_evals --llm anthropic    # optional, real model (needs ANTHROPIC_API_KEY; see caveat below)
```

**Exact fake-LLM command:** `LLM_PROVIDER=fake python -m evals.run_evals` (`fake` is also the default everywhere).

Tests spin the store API and the orders agent in-process behind ASGI transports (`common/inproc.py`), so there is
no mocking of the code under test; the same JSON travels the same code paths as over the network.
Mandatory tests: `tests/test_tenant_isolation.py` (R3) and `tests/test_human_in_the_loop.py` (R5).

## Model provider

`common/llm.py::get_llm()` is the single factory: `fake` (default) | `anthropic` | `openai` | `ollama`, chosen by
`LLM_PROVIDER` / `LLM_MODEL`. **Only the fake provider has been exercised.** I had no API key; the real-provider
branches are thin and untested. Install the matching extra (`pip install -e ".[anthropic]"`) to try one.
The fake is a rule-based policy (regex intent -> tool call, tool JSON -> sentence) that is deliberately *naive*
about injected text so the tests prove the guardrails and the approval gate, not the model's good manners.

## API

| Endpoint | Purpose |
|---|---|
| `POST /chat` `{conversation_id, message}` | SSE stream: `start`, `tool_started`, `tool_finished`, `token`, `proposal`, `done`, `error` |
| `POST /chat/{id}/approve` , `/reject` | resume the paused graph (SSE). 409 if nothing is pending |
| `GET /chat/{id}` | history + pending proposal |
| orders agent: `GET /.well-known/agent-card.json`, `POST /rpc` (`message/send`) | JSON-RPC 2.0 |

## Layout
`store_api/` mock backend + seed · `orders_agent/` JSON-RPC sub-agent · `orchestrator/` graph, A2A client, runner ·
`chat_api/` SSE API · `common/` guardrails, LLM factory + fake, http, logging · `tests/` · `evals/` · `scripts/`
