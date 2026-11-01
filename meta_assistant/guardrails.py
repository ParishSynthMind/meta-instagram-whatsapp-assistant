"""Hard rules that sit around the model, in plain code the model can't talk its way past.

- Inbound: crisis words and hand-over words skip the model entirely.
- Outbound: anything that looks like a price, a discount or a promise is
  blocked and replaced by a safe hand-over message.
- Length: each channel's limit is enforced before sending.
"""
import re
from dataclasses import dataclass, field

# Channel limits. Instagram DMs: 1000 bytes. Comments: kept short on purpose.
MAX_LENGTH = {"instagram_comment": 300, "instagram_dm": 1000, "whatsapp": 1500}

HANDOVER_REPLY = {
    "instagram_comment": "Thanks for your message! Someone from our team will get back to you shortly.",
    "instagram_dm": "Thanks, I've passed this to the team and a real person will reply here as soon as they can.",
    "whatsapp": "Thanks, I've passed this to the team and a real person will reply here as soon as they can.",
}
CRISIS_REPLY = (
    "I'm really sorry you're going through this. If you are in danger or need urgent help, "
    "please contact your local emergency services now. I've also alerted a member of our team."
)
UNSUPPORTED_REPLY = "Thanks! I can only read text messages here, so a person from the team will take a look."

_PRICE_PATTERNS = [
    r"[$£€¥]\s?\d",
    r"\b\d[\d,.]*\s?(?:usd|aed|gbp|eur|dollars?|pounds?|euros?|dirhams?|dhs?|bucks)\b",
    r"\b(?:usd|aed|gbp|eur|dhs?)\s?\d",
    r"\b\d+\s?%\s?off\b",
    r"\b(?:price|prices|cost|costs|fee|fees|rate|rates)\s+(?:is|are|start|starts|from|of)\s+"
    r"(?:about\s+|around\s+|only\s+|just\s+|at\s+|from\s+)?\d",
]
_PROMISE_PATTERNS = [
    r"\bguarantee[ds]?\b", r"\bpromise[ds]?\b", r"\bwe will refund\b", r"\brefund\b",
    r"\bdiscount\b", r"\bfree of charge\b", r"\bfor free\b", r"\b100\s?%\b",
    r"\bdelivered by\b", r"\bready by\b", r"\bwithin \d+ (?:hours?|days?)\b",
    r"\bby (?:tomorrow|tonight|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
]
_PRICE_RE = [re.compile(p, re.IGNORECASE) for p in _PRICE_PATTERNS]
_PROMISE_RE = [re.compile(p, re.IGNORECASE) for p in _PROMISE_PATTERNS]


@dataclass
class InboundDecision:
    action: str                      # "reply" | "handover" | "crisis"
    matched: str = ""


@dataclass
class OutboundCheck:
    ok: bool
    violations: list[str] = field(default_factory=list)


def _keyword_hit(text: str, keywords: list[str]) -> str:
    lowered = " ".join(text.lower().split())
    for kw in keywords:
        kw = kw.lower().strip()
        if kw and re.search(r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])", lowered):
            return kw
    return ""


def check_inbound(text: str, handover_keywords: list[str], crisis_keywords: list[str]) -> InboundDecision:
    hit = _keyword_hit(text, crisis_keywords)
    if hit:
        return InboundDecision("crisis", hit)
    hit = _keyword_hit(text, handover_keywords)
    if hit:
        return InboundDecision("handover", hit)
    return InboundDecision("reply")


def check_outbound(text: str, channel: str) -> OutboundCheck:
    violations = []
    if not text.strip():
        violations.append("empty reply")
    for rx in _PRICE_RE:
        m = rx.search(text)
        if m:
            violations.append(f"price: {m.group(0)!r}")
            break
    for rx in _PROMISE_RE:
        m = rx.search(text)
        if m:
            violations.append(f"promise: {m.group(0)!r}")
            break
    limit = MAX_LENGTH.get(channel, 1000)
    if len(text.encode("utf-8")) > limit:
        violations.append(f"too long: {len(text.encode('utf-8'))} bytes > {limit}")
    return OutboundCheck(not violations, violations)
