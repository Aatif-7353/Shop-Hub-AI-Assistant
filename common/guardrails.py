"""R4 guardrails as small pure functions.

* redact_text    - strip emails and phone numbers before anything reaches a model.
* sanitize_note  - neutralise instruction-like text inside customer notes.
Everything that comes back from a tool is untrusted data.
"""
import re
import unicodedata

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
_PHONE_CAND = re.compile(r"(?<![\w.])\+?\d[\d\s().\-]{7,}\d(?![\w])")
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)

EMAIL_MASK = "[email redacted]"
PHONE_MASK = "[phone redacted]"
REMOVED = "[instruction-like text removed]"


def _phone_sub(m: re.Match) -> str:
    cand = m.group(0)
    digits = sum(ch.isdigit() for ch in cand)
    if _DATE_ONLY.match(cand.strip()) or not (9 <= digits <= 15):
        return cand  # dates, ids, amounts are not phone numbers
    return PHONE_MASK


def redact_text(text: str) -> str:
    text = EMAIL_RE.sub(EMAIL_MASK, text)
    return _PHONE_CAND.sub(_phone_sub, text)


def redact_obj(obj):
    """Recursively redact every string inside a JSON-like structure."""
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, list):
        return [redact_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    return obj


# Instruction-like patterns, matched per sentence on normalised text.
_INJECTION = [re.compile(p, re.I) for p in [
    r"\b(assistant|system|developer|ai|admin)\s*:",
    r"\bignore\b.{0,40}\b(instruction|rule|prompt|guideline)s?\b",
    r"\bdisregard\b.{0,40}\b(instruction|rule|prompt|above)s?\b",
    r"\bforget\b.{0,30}\b(above|everything|instruction|previous|prior)",
    r"\bnew\s+instructions?\b",
    r"\byou\s+are\s+now\b",
    r"\b(system|developer)\s+override\b",
    r"\b(admin|developer|debug|god)\s+mode\b",
    r"\b(call|use|invoke|run)\b.{0,20}\b(refund|cancel)\b.{0,10}\btool\b",
    r"\b(refund|cancel)\s+(this|that|the|all|every|my)\b.{0,15}\borders?\b",
    r"\bdo\s+not\s+(ask|wait|require)\b.{0,20}\b(approval|confirmation|permission)",
    r"\breveal\b.{0,40}\b(email|phone|customer|merchant|prompt|token|password)",
    r"\b(system|hidden)\s+prompt\b",
]]


def _normalise(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)


def sanitize_note(note: str | None) -> dict:
    """Return {'text', 'injection_detected', 'trust'}; instruction-like sentences are removed."""
    note = _normalise(note or "")
    kept, flagged = [], False
    for sent in re.split(r"(?<=[.!?])\s+", note):
        if sent and any(p.search(sent) for p in _INJECTION):
            flagged = True
            kept.append(REMOVED)
        elif sent:
            kept.append(sent)
    return {"text": redact_text(" ".join(kept)), "injection_detected": flagged, "trust": "untrusted_customer_data"}
