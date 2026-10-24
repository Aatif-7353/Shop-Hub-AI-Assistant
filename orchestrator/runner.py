"""Runs one turn (or a resume) of the graph and yields client-facing events. Used by the chat API and evals."""
import hashlib
from contextlib import asynccontextmanager

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langgraph.types import Command

from common import config
from orchestrator.a2a_client import Secret


def thread_id(merchant: str, user: str, conversation_id: str) -> str:
    """Tenant+user namespaced: another tenant can never address this thread."""
    return hashlib.sha256(f"{merchant}|{user}|{conversation_id}".encode()).hexdigest()[:40]


def make_config(token: str, identity: dict, conversation_id: str, cid: str | None) -> dict:
    return {"configurable": {
        "thread_id": thread_id(identity["merchant"], identity["user"], conversation_id),
        "__token": Secret(token),          # not a primitive -> never copied into checkpoint metadata
        "merchant": identity["merchant"], "user": identity["user"], "cid": cid}}


def user_input(text: str) -> dict:
    return {"messages": [HumanMessage(content=text)]}


def resume(decision: str) -> Command:
    return Command(resume={"decision": decision})


async def pending_proposal(graph, cfg: dict) -> dict | None:
    state = await graph.aget_state(cfg)
    for t in state.tasks:
        for it in t.interrupts:
            return it.value
    return None


async def stream_events(graph, payload, cfg: dict):
    async for mode, chunk in graph.astream(payload, cfg, stream_mode=["updates", "custom", "messages"]):
        if mode == "custom":
            yield chunk
        elif mode == "messages":
            msg, meta = chunk
            if isinstance(msg, AIMessageChunk) and isinstance(msg.content, str) and msg.content \
                    and meta.get("langgraph_node") == "agent":
                yield {"event": "token", "text": msg.content}
        elif mode == "updates":
            if "__interrupt__" in chunk:
                for it in chunk["__interrupt__"]:
                    yield {"event": "proposal", "proposal": it.value}
            elif "preflight" in chunk and chunk["preflight"].get("blocked"):
                yield {"event": "message", "text": chunk["preflight"]["messages"][0].content}
    state = await graph.aget_state(cfg)
    answer = next((m.content for m in reversed(state.values.get("messages", [])) if isinstance(m, AIMessage) and not m.tool_calls), "")
    pend = await pending_proposal(graph, cfg)
    yield {"event": "done", "answer": "" if pend else answer, "pending_proposal": pend is not None}


@asynccontextmanager
async def open_checkpointer(url: str | None = None):
    url = url or config.checkpoint_db_url()
    if url.startswith("postgres"):
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        async with AsyncPostgresSaver.from_conn_string(url) as saver:
            await saver.setup()
            yield saver
    else:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        path = url.removeprefix("sqlite:///") if url.startswith("sqlite") else url
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            await saver.setup()
            yield saver
