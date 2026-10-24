"""R1/R4/R5 - the orchestrator graph.

 START -> preflight --blocked--> END
              |
              v
           agent  <-----------------+
              | tool calls?         |
        read  |      write          |
              v         v           |
           tools  ->  propose (interrupt: human approve/reject)
              \_________/
"""
import json
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, SystemMessage, ToolMessage
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt
from pydantic import ValidationError

from common.guardrails import redact_obj, redact_text
from common.llm import get_llm
from common.obs import log
from orchestrator.a2a_client import Secret, call_skill
from orchestrator.tools import ARGS, READ_TOOLS, SYSTEM_PROMPT, TOOLS, WRITE_TOOLS

DENIED = "Access denied: your account is not attached to a store, so I can't help with orders."


class State(TypedDict, total=False):
    # Deliberately minimal: no token, no PII beyond what the conversation holds.
    messages: Annotated[list[AnyMessage], add_messages]
    blocked: bool


def _cfg(config) -> dict:
    return config["configurable"]


def _pending(state: State) -> list[dict]:
    msgs = state["messages"]
    idx = max((i for i, m in enumerate(msgs) if isinstance(m, AIMessage)), default=None)
    if idx is None:
        return []
    answered = {m.tool_call_id for m in msgs[idx + 1:] if isinstance(m, ToolMessage)}
    return [tc for tc in msgs[idx].tool_calls if tc["id"] not in answered]


def _tool_msg(tc: dict, data: dict) -> ToolMessage:
    # Defence in depth: redact again at the model boundary, whatever the sub-agent returned.
    return ToolMessage(content=json.dumps(redact_obj(data)), name=tc["name"], tool_call_id=tc["id"])


def _validate(tc: dict) -> tuple[dict | None, dict | None]:
    model = ARGS.get(tc["name"])
    if model is None:
        return None, {"ok": False, "code": "unknown_tool", "error": f"Unknown tool '{tc['name']}'"}
    try:
        return model(**tc["args"]).model_dump(exclude_none=True), None
    except ValidationError:
        return None, {"ok": False, "code": "bad_request", "error": "Invalid tool arguments"}


def build_graph(checkpointer, llm=None):
    model = (llm or get_llm()).bind_tools(TOOLS)

    async def preflight(state: State, config):
        c = _cfg(config)
        who = await call_skill("whoami", {}, c["__token"], c.get("cid"))
        if not who.get("ok") or not who.get("merchant"):  # R4c: fail closed
            log("orchestrator", "preflight_denied", c.get("cid"))
            return {"blocked": True, "messages": [AIMessage(content=DENIED)]}
        return {"blocked": False}

    async def agent(state: State, config):
        msgs = [m.model_copy(update={"content": redact_text(m.content)}) if isinstance(m.content, str) else m
                for m in state["messages"]]  # R4b: nothing un-redacted reaches the model
        reply = await model.ainvoke([SystemMessage(content=SYSTEM_PROMPT), *msgs])
        return {"messages": [reply]}

    async def tools(state: State, config):
        c, w, out = _cfg(config), get_stream_writer(), []
        for tc in _pending(state):
            if tc["name"] in WRITE_TOOLS:
                continue
            w({"event": "tool_started", "name": tc["name"], "args": tc["args"]})
            args, err = _validate(tc)
            data = err or await call_skill(tc["name"], args, c["__token"], c.get("cid"))
            w({"event": "tool_finished", "name": tc["name"], "ok": bool(data.get("ok"))})
            out.append(_tool_msg(tc, data))
        return {"messages": out}

    async def propose(state: State, config):
        c, w, out = _cfg(config), get_stream_writer(), []
        for tc in _pending(state):
            if tc["name"] not in WRITE_TOOLS:
                continue
            args, err = _validate(tc)
            if err:
                out.append(_tool_msg(tc, err))
                continue
            action = tc["name"].removesuffix("_order")
            w({"event": "tool_started", "name": "preview_change", "args": args})
            prev = await call_skill("preview_change", {**args, "action": action}, c["__token"], c.get("cid"))
            w({"event": "tool_finished", "name": "preview_change", "ok": bool(prev.get("ok"))})
            if not prev.get("ok"):  # foreign/unknown/invalid state: no proposal is ever created
                out.append(_tool_msg(tc, prev))
                continue
            proposal = {"proposal_id": tc["id"], "action": action, "order_id": prev["order_id"],
                        "merchant": c["merchant"], "requested_by": c["user"],
                        "customer_name": prev["customer_name"], "current_status": prev["current_status"],
                        "new_status": prev["new_status"], "amount": prev["amount"],
                        "summary": f"{action.capitalize()} order {prev['order_id']} ({prev['customer_name']}): "
                                   f"{prev['current_status']} -> {prev['new_status']}, amount {prev['amount']}"}
            decision = interrupt(proposal)  # <- graph pauses here until approve/reject
            approved = isinstance(decision, dict) and decision.get("decision") == "approve" \
                and proposal["merchant"] == c["merchant"]  # proposal must belong to the approver's tenant
            if not approved:
                out.append(_tool_msg(tc, {"ok": True, "executed": False, "decision": "rejected", "order_id": prev["order_id"]}))
                continue
            w({"event": "tool_started", "name": tc["name"], "args": args})
            res = await call_skill(tc["name"], args, c["__token"], c.get("cid"))  # approver's own token
            w({"event": "tool_finished", "name": tc["name"], "ok": bool(res.get("ok"))})
            out.append(_tool_msg(tc, res))
        return {"messages": out}

    def after_preflight(state: State):
        return END if state.get("blocked") else "agent"

    def after_agent(state: State):
        pend = _pending(state)
        if any(tc["name"] not in WRITE_TOOLS for tc in pend):
            return "tools"
        return "propose" if pend else END

    def after_tools(state: State):
        return "propose" if _pending(state) else "agent"

    g = StateGraph(State)
    for name, fn in [("preflight", preflight), ("agent", agent), ("tools", tools), ("propose", propose)]:
        g.add_node(name, fn)
    g.add_edge(START, "preflight")
    g.add_conditional_edges("preflight", after_preflight, ["agent", END])
    g.add_conditional_edges("agent", after_agent, ["tools", "propose", END])
    g.add_conditional_edges("tools", after_tools, ["propose", "agent"])
    g.add_edge("propose", "agent")
    return g.compile(checkpointer=checkpointer)
