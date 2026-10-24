# DESIGN

**AI-assistance disclosure.** I built this with an AI coding assistant (Claude) and reviewed, ran and tested
everything it produced. I can explain every module; the sections below say what I verified and what I could not.

## 1. Architecture

```
 staff client ──(Bearer T)──> CHAT API :8000 ──────────────┐   edge auth: /auth/me via sub-agent
   curl / scripts/client.py    SSE out, /approve, /reject   │   401 bad token, 403 no merchant (fail closed)
                               thread_id = H(merchant|user|conversation)
                                        │ config (token wrapped in Secret, NOT in graph state)
                                        v
                       ORCHESTRATOR (LangGraph, checkpointed in Postgres/SQLite)
   START -> preflight -> agent(LLM) -> tools ----------------+-> agent ... -> END
              |  (blocked)   ^   \                           |
              v              |    `-> propose --interrupt()--+   pause for human approve/reject
             END             |            ^ resume(Command)
                             |            └── POST /approve | /reject (same tenant+user only)
                    redact_text on every message the model sees
                                        │ JSON-RPC 2.0  message/send  (Authorization: Bearer T, X-Correlation-Id)
                                        │ discovered via GET /.well-known/agent-card.json (no Python import)
                                        v
                       ORDERS SUB-AGENT :8002 (separate process)
        skills: whoami, lookup_order, list_orders, search_orders, preview_change, refund_order, cancel_order
        normalises legacy 200+status:false -> {ok:false,code:not_found}; whitelists fields (no email/phone);
        sanitize_note() + redact_text() on all free text
                                        │ HTTP, same Bearer T
                                        v
                       STORE API :8001 (in-memory; 2 merchants, 14 orders, tenant-scoped)
```

## 2. One turn ("refund order 1001")
1. Chat API takes the bearer token, calls `whoami` (-> sub-agent -> `/auth/me`). Bad token -> 401; `merchant: null` -> 403. It mints a correlation id.
2. Thread id is derived from merchant+user+conversation id, so another tenant using the same conversation id lands on a different, empty thread.
3. `preflight` re-checks the merchant inside the graph (defence in depth; a blocked turn never reaches the model).
4. `agent`: messages are redacted (`redact_text`) and sent to the model with tool schemas. The model returns `refund_order(id=1001)`.
5. `propose`: calls the read-only `preview_change` skill. Foreign/unknown/invalid-state orders return an error *as data* and **no proposal exists**. Otherwise a proposal (order, merchant, current -> new status, amount) is built and `interrupt()` pauses the graph; the checkpoint is saved; SSE emits `proposal` + `done(pending_proposal=true)`.
6. `POST /approve`: the caller is re-authenticated, the thread is derived from *their* identity, and the proposal's merchant must match. The graph resumes; the node executes `refund_order` through the sub-agent with the **approver's own token**; the store applies it (or answers 409). `reject` records "no change".
7. `agent` writes the final answer from the tool result; tokens stream out as SSE.

## 3. Security decisions
- **Token handling (R3):** forwarded unchanged on every hop; never in graph state; held in a `Secret` wrapper in run config. LangGraph copies *primitive* config values into checkpoint metadata, so a plain string token would have been written to Postgres; a test greps the SQLite file and I checked Postgres for the token.
- **Tenant isolation:** enforced by the store API per token; the agent adds nothing that could widen it. Foreign orders get the same answer as missing ones (store 404 / legacy envelope -> identical "couldn't find"). Proved by tests in both directions, plus a thread-namespace test.
- **Injection (R4a):** (1) `sanitize_note` drops instruction-like sentences after Unicode normalisation, flags them, and labels notes `untrusted_customer_data`; (2) system prompt says tool output is data; (3) **the real safety net:** nothing mutating runs without human approval. A test disables the sanitiser so the naive fake model *is* fooled and shows the result is only a pending proposal.
- **PII (R4b):** the sub-agent never forwards email/phone fields at all; free text is regex-redacted; the orchestrator redacts again at the model boundary (including what the user types). A spy-model test asserts no `@`/phone digits in any prompt.
- **Fail closed (R4c):** at the edge (403), in the graph (`preflight`), and in the store (403 on every scoped endpoint).

## 4. Trade-offs
- **Hand-rolled JSON-RPC, A2A-shaped** (`message/send`, agent card with skills/endpoint) instead of `a2a-sdk`: ~100 lines I can defend, no SDK version risk. Not wire-compatible with every A2A client (no tasks/streaming).
- **Write skills are callable by whoever holds a token and speaks RPC.** The orchestrator is the approval gate, not the sub-agent. With more time the approval would produce a short-lived signed approval token that the sub-agent verifies.
- **Sanitiser is regex-based** and will miss paraphrased attacks; that is why approval, not filtering, is the control.
- **Redaction is at the model boundary, not at rest:** PII typed by the user is kept in the checkpoint as typed.
- `whoami` runs twice per turn (edge + preflight): +~10 ms, simpler reasoning about fail-closed.
- Approve re-runs `preview_change` (resume re-executes the node); it is read-only and idempotent. The same user who asked can approve (no four-eyes rule).
- Store API is in-memory and single-process; restarting it resets data (by design for a mock).
- Fake LLM: deterministic and offline, but it only understands the phrasings I scripted.

## 5. What I cut / could not verify (honest list)
- **Real LLM provider: untested.** No API key. Factory branches exist for anthropic/openai/ollama; evals with `--llm` were not run, and wording-based checks may need loosening for real models.
- **`docker compose up` was not run by me:** the sandbox has no Docker. I did run the identical services as three processes against a real PostgreSQL 16 (restart/persistence demo, SSE client, healthcheck command, correlation id in all three logs). Compose syntax was only parsed as YAML. Treat the first `docker compose up --build` as unverified.
- Stretch: **S3 (correlation id) done** (chat API -> JSON-RPC header -> store; structured JSON logs; asserted in a test). S1, S2, S4 not attempted.
- No rate limiting, no proposal expiry, no pagination, no auth caching, no UI.

## 6. Cost and latency per turn
- **Measured (fake LLM, HTTP + Postgres, local):** lookup turn p50 ~225 ms (10 runs), including edge auth, preflight, 1 tool hop (3 HTTP calls) and checkpoint writes.
- **Estimated, not measured (real model):** 2 model calls per read turn (choose tool, write answer), ~1.2-1.8k input tokens each (system + 5 tool schemas + redacted history) and ~100 output tokens. At mid-tier pricing of roughly $3/M input, $15/M output that is ~$0.01 per turn; a refund turn adds a third call after approval. Latency ~ 2 x 1-2 s model time + ~0.2 s plumbing = 2-5 s; first token after the tool hop. Prompt caching of the static prefix (S4) would cut input cost most.

## 7. With another week
1. Signed, expiring approval tokens verified by the sub-agent; four-eyes option; proposal TTL and audit log.
2. Run the evals against a real model, add adversarial/paraphrase injection cases and an LLM-judge pass for answer quality.
3. A second sanitiser layer (classifier) + output-side check that the final answer contains no un-redacted PII.
4. Per-merchant rate limits and token/cost accounting in the stream (S2); S1 Terraform sketch for ECS Fargate.
5. Sub-agent streaming (A2A `message/stream`), parallel read tools, pagination for large stores.
6. CI that builds the image and runs `docker compose up` plus the restart demo.
