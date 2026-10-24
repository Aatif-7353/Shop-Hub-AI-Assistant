"""One factory for the chat model. LLM_PROVIDER=fake (default, no key) | anthropic | openai | ollama."""
import json
import re
import uuid
from typing import Any, Iterator

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from common import config


def get_llm() -> BaseChatModel:
    provider, model = config.llm_provider(), config.llm_model()
    if provider == "fake":
        return FakeChatModel()
    if provider == "anthropic":  # needs ANTHROPIC_API_KEY in the environment
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model or "claude-sonnet-4-5", temperature=0)
    if provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model or "gpt-4o-mini", temperature=0)
    if provider == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(model=model or "qwen2.5:7b", temperature=0)
    raise ValueError(f"Unknown LLM_PROVIDER '{provider}'")


# --------------------------------------------------------------------------- fake
_ID = re.compile(r"\b(\d{4})\b")
_STATUSES = ["placed", "paid", "shipped", "delivered", "cancelled", "refunded"]
# A deliberately NAIVE fake: if instruction-like text survives into a tool result, it obeys it.
# That lets tests prove the guardrails + approval gate work even when the "model" is fooled.
_NAIVE_TRIGGER = re.compile(r"(ignore your instructions|refund this order|call the refund tool|cancel all orders)", re.I)


def _call(name: str, args: dict) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{uuid.uuid4().hex[:8]}", "type": "tool_call"}])


class FakeChatModel(BaseChatModel):
    """Scripted, deterministic, rule-based 'model'. Picks tools from the user's text and writes
    answers from real tool results. It is a real policy (not a stub): different inputs -> different tools."""

    @property
    def _llm_type(self) -> str:
        return "fake-shophub"

    def bind_tools(self, tools: Any, **kwargs: Any):
        return self  # tool schemas are irrelevant to a rule-based policy

    # -- policy -----------------------------------------------------------
    def _decide(self, messages: list[BaseMessage]) -> AIMessage:
        last = messages[-1]
        if isinstance(last, ToolMessage):
            start = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
            tools = [m for m in messages[start:] if isinstance(m, ToolMessage)]
            naive = self._naive_obey(tools)
            return naive or AIMessage(content=" ".join(self._answer(t) for t in tools))
        text = next((m.content for m in reversed(messages) if isinstance(m, HumanMessage)), "")
        return self._pick_tool(str(text).lower())

    def _pick_tool(self, t: str) -> AIMessage:
        ids = _ID.findall(t)
        if re.search(r"\brefund", t) and ids:
            return _call("refund_order", {"id": int(ids[0])})
        if re.search(r"\bcancel", t) and ids:
            return _call("cancel_order", {"id": int(ids[0])})
        if ids and re.search(r"order|#|status|show|lookup|details|find", t):
            return _call("lookup_order", {"id": int(ids[0])})
        if m := re.search(r"(?:what did|orders? (?:from|by|of|for)|customer)\s+(?:customer\s+)?(?!order\b)([a-z][a-z'\-]+)", t):
            return _call("search_orders", {"customer": m.group(1)})
        status = next((s for s in _STATUSES if s in t), None)
        if status and re.search(r"order", t):
            args: dict = {"status": status.upper()}
            if d := re.search(r"(\d+)\s*days?", t):
                args["older_than_days"] = int(d.group(1))
            return _call("list_orders", args)
        return AIMessage(content="I can look up orders, list them by status, search by customer name, and propose refunds or cancellations. Which order do you mean?")

    def _naive_obey(self, tools: list[ToolMessage]) -> AIMessage | None:
        for t in tools:
            if t.name in ("lookup_order", "list_orders", "search_orders") and _NAIVE_TRIGGER.search(t.content):
                m = re.search(r'"id":\s*(\d+)', t.content)
                if m:
                    return _call("refund_order", {"id": int(m.group(1))})
        return None

    # -- answers ----------------------------------------------------------
    def _answer(self, t: ToolMessage) -> str:
        try:
            d = json.loads(t.content)
        except Exception:
            return "I received an unreadable tool result."
        if not d.get("ok", False) and d.get("decision") != "rejected":
            return self._error_text(t.name, d)
        if d.get("decision") == "rejected":
            return f"Understood - I did not change order {d.get('order_id')}. No changes were made."
        if d.get("executed"):
            return f"Done: order {d['order_id']} is now {d['status']} (amount {d['amount']})."
        if "order" in d:
            return self._order_line(d["order"], detail=True)
        rows = d.get("orders", [])
        if not rows:
            return "No matching orders found."
        if t.name == "search_orders":
            return f"Found {len(rows)} order(s). Most recent: " + self._order_line(rows[0]) + (
                " Others: " + "; ".join(self._order_line(r) for r in rows[1:]) if len(rows) > 1 else "")
        return f"{len(rows)} order(s): " + "; ".join(self._order_line(r) for r in rows)

    @staticmethod
    def _order_line(o: dict, detail: bool = False) -> str:
        s = f"Order {o['id']} - {o['customer_name']}, {o['status']}, total {o['total']}, placed {o['age_days']} days ago."
        note = o.get("customer_note") or {}
        if detail and note.get("text"):
            s += f' Customer note (untrusted customer text, not an instruction): "{note["text"]}"'
            if note.get("injection_detected"):
                s += " Note: this note contained instruction-like text, which was removed and ignored."
        return s

    @staticmethod
    def _error_text(name: str | None, d: dict) -> str:
        code = d.get("code")
        if code == "not_found":
            return "I couldn't find that order in your store."
        if code == "conflict":
            return f"That can't be done: {d.get('error')}"
        if code in ("unauthorized", "forbidden"):
            return "I can't help with that request: your account isn't authorised for a store."
        return f"Sorry, that failed: {d.get('error', 'unknown error')}."

    # -- LangChain plumbing -------------------------------------------------
    def _generate(self, messages, stop=None, run_manager=None, **kw) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._decide(messages))])

    def _stream(self, messages, stop=None, run_manager=None, **kw) -> Iterator[ChatGenerationChunk]:
        msg = self._decide(messages)
        if msg.tool_calls:
            tc = msg.tool_calls[0]
            yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=[
                {"name": tc["name"], "args": json.dumps(tc["args"]), "id": tc["id"], "index": 0}]))
            return
        for word in re.findall(r"\S+\s*", msg.content):
            yield ChatGenerationChunk(message=AIMessageChunk(content=word))
