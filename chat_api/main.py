"""R6/R7 - Chat API. SSE streaming, approve/reject, conversation history; checkpointer in Postgres/SQLite."""
import json
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

from common.obs import CID_HEADER, log, new_cid
from orchestrator.a2a_client import Secret, call_skill
from orchestrator.graph import build_graph
from orchestrator.runner import (make_config, open_checkpointer, pending_proposal, resume, stream_events,
                                 user_input)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if getattr(app.state, "graph", None) is None:  # tests inject their own graph
        async with open_checkpointer() as saver:
            app.state.graph = build_graph(saver)
            yield
    else:
        yield


app = FastAPI(title="ShopHub support assistant", lifespan=lifespan)


class ChatIn(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    message: str = Field(min_length=1, max_length=2000)


class Caller:
    def __init__(self, token: str, identity: dict, cid: str):
        self.token, self.identity, self.cid = token, identity, cid


async def caller(authorization: str | None = Header(default=None)) -> Caller:
    """Edge authentication. Fails closed: bad token -> 401, valid token without merchant -> 403."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    cid = new_cid()  # generated here, never trusted from the client
    who = await call_skill("whoami", {}, Secret(token), cid)
    if not who.get("ok"):
        raise HTTPException(401 if who.get("code") in ("unauthorized", None) else 503, "Authentication failed")
    if not who.get("merchant"):
        log("chat-api", "rejected_no_merchant", cid)
        raise HTTPException(403, "Your account is not attached to a merchant")
    return Caller(token, {"user": who["user"], "merchant": who["merchant"]}, cid)


def sse(event: dict) -> str:
    return f"event: {event['event']}\ndata: {json.dumps(event)}\n\n"


async def _stream(graph, payload, conversation_id: str, c: Caller):
    cfg = make_config(c.token, c.identity, conversation_id, c.cid)
    log("chat-api", "turn_start", c.cid, merchant=c.identity["merchant"])
    yield sse({"event": "start", "conversation_id": conversation_id, "correlation_id": c.cid})
    try:
        async for ev in stream_events(graph, payload, cfg):
            yield sse(ev)
    except Exception as e:  # never leak internals to the client
        log("chat-api", "turn_error", c.cid, err=type(e).__name__)
        yield sse({"event": "error", "message": "Something went wrong handling this request."})
    log("chat-api", "turn_end", c.cid)


def _response(gen, c: Caller) -> StreamingResponse:
    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={CID_HEADER: c.cid, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.post("/chat")
async def chat(body: ChatIn, request: Request, c: Caller = Depends(caller)):
    graph = request.app.state.graph
    cfg = make_config(c.token, c.identity, body.conversation_id, c.cid)
    if await pending_proposal(graph, cfg):
        raise HTTPException(409, "A proposal is awaiting approval. Approve or reject it first.")
    return _response(_stream(graph, user_input(body.message), body.conversation_id, c), c)


async def _decide(conversation_id: str, decision: str, request: Request, c: Caller):
    graph = request.app.state.graph
    cfg = make_config(c.token, c.identity, conversation_id, c.cid)  # thread is derived from the caller's tenant+user
    prop = await pending_proposal(graph, cfg)
    if prop is None or prop.get("merchant") != c.identity["merchant"]:
        raise HTTPException(409, "No pending proposal for this conversation")
    log("chat-api", f"proposal_{decision}", c.cid, proposal=prop["proposal_id"], merchant=c.identity["merchant"])
    return _response(_stream(graph, resume(decision), conversation_id, c), c)


@app.post("/chat/{conversation_id}/approve")
async def approve(conversation_id: str, request: Request, c: Caller = Depends(caller)):
    return await _decide(conversation_id, "approve", request, c)


@app.post("/chat/{conversation_id}/reject")
async def reject(conversation_id: str, request: Request, c: Caller = Depends(caller)):
    return await _decide(conversation_id, "reject", request, c)


@app.get("/chat/{conversation_id}")
async def history(conversation_id: str, request: Request, c: Caller = Depends(caller)):
    graph = request.app.state.graph
    cfg = make_config(c.token, c.identity, conversation_id, c.cid)
    state = await graph.aget_state(cfg)
    msgs = [{"role": "user" if isinstance(m, HumanMessage) else "assistant", "text": m.content}
            for m in state.values.get("messages", []) if isinstance(m, HumanMessage) or (isinstance(m, AIMessage) and m.content)]
    return {"conversation_id": conversation_id, "messages": msgs, "pending_proposal": await pending_proposal(graph, cfg)}
