"""Lightweight Grim loops that never spend xAI while the channel is idle.

Sticky context, repair/confirm, optional nudges, a next-step offer, and
thumbs feedback all ride the same completion the human already triggered.
Nudges default OFF. No background polling, no extra live search, no second
model call to log a thumbs-up.
"""

from __future__ import annotations

import re
from collections import deque

FEEDBACK_UP = "\N{THUMBS UP SIGN}"
FEEDBACK_DOWN = "\N{THUMBS DOWN SIGN}"
MISS_PROMPT = "what missed?"
STICKY_HEADER = "STICKY CONTEXT (only because it correlates with this ask)"
MAX_STICKY_TOPICS = 4
MAX_STICKY_PREFS = 3
MAX_STICKY_LINE = 120

_CASUAL_TOKENS = frozenset({
    "grim", "yo", "hey", "hi", "hello", "sup", "wassup", "morning", "evening",
    "night", "gn", "gm", "howdy", "bye", "later", "cya", "lol", "lmao", "lmfao",
    "haha", "bruh", "bro", "dude", "man", "fam", "ok", "okay", "yeah", "yup",
    "nah", "nope", "thanks", "thank", "cool", "nice", "same", "mood", "ping",
    "you", "u", "ya", "good", "there", "here", "up", "wsp", "wyd",
})

_HIGH_STAKES = re.compile(
    r"(?:"
    r"\$\s*\d"
    r"|\b\d+(?:\.\d+)?\s*(?:usd|dollars?|bucks|euros?)\b"
    r"|\b(?:venmo|paypal|cashapp|zelle|bitcoin|btc|ethereum|eth)\b"
    r"|\b(?:wire|iban|routing|ssn|passwords?|passcodes?|doxx?)\b"
    r"|\b(?:send|wire|transfer|donate)\s+(?:money|cash|funds?|\d|\$)"
    r"|\b(?:pay|payment|invoice|invest|bank account)\b"
    r"|\b(?:dm|message|text|call|email|ping)\s+"
    r"(?:them|him|her|someone|everybody|everyone)\b"
    r"|\b(?:buy|purchase|order)\s+(?:it|this|that|them|one)\b"
    r"|\b(?:book|schedule|cancel)\s+(?:a|the|my|this)\b"
    r"|\b(?:kick|ban)\s+(?:them|him|her|this|that)\b"
    r"|\bhome address\b|\bphone number\b|\breal[- ]world\b"
    r")",
    re.IGNORECASE,
)

_AMBIGUOUS = re.compile(
    r"(?:"
    r"\b(?:do that|do it|that one|this one|the other(?: one)?|the thing|"
    r"you know(?: what i mean)?|which one|not sure what i mean|"
    r"idk what i mean|what i meant)\b"
    r"|\b(?:this|that|it|those|these)\s*\??\s*$"
    r")",
    re.IGNORECASE,
)

_PREF_ASK = re.compile(
    r"\b(?:language|spanish|english|prefer|preference|remember(?:ed)?|"
    r"you know i|last time you missed|what you missed)\b",
    re.IGNORECASE,
)

_AGENTIC_YES = re.compile(
    r"^\s*(?:yes|yeah|yep|yea|yup|ok|okay|sure|do it|go ahead|please do|"
    r"go for it|do that|yes please)\b",
    re.IGNORECASE,
)

AGENTIC_NOTE = (
    "LIGHT AGENTIC: You may offer one next step in this same reply when it "
    "helps the current ask (example: want me to outline a Black Friday savings "
    "plan?). Do not act in the real world — no payments, no DMs to other people, "
    "no extra tools or API calls. Drafting stays text-only. Wait for a clear yes "
    "before writing the offered draft."
)
REPAIR_NOTE = (
    "This ask is ambiguous or high-stakes (money, a real-world action, or unclear "
    "intent). In THIS same reply, include one short read-back or confirm question. "
    "If the reply is already that read-back, stop there. Do not make a second call. "
    "Skip extra questions if you already confirmed the ask. Do not send money, DMs, "
    "or take a real-world step until they say yes."
)
NUDGE_ON_NOTE = (
    "NUDGES ARE ON for this member. After the answer, add at most one short "
    "follow-up suggestion in this same reply. Never a second model call. Never "
    "invent a nudge for idle channels or people who did not just speak."
)
NUDGE_OFF_NOTE = (
    "NUDGES ARE OFF. Do not add an unsolicited follow-up suggestion. A next-step "
    "offer on the current task is still allowed."
)

_pending_miss: dict[tuple[str, str], str] = {}
_prompted_message_ids: deque[str] = deque(maxlen=200)
_prompted_lookup: set[str] = set()


def _fold(text: str | None) -> str:
    raw = str(text or "")
    pieces = []
    for char in raw.casefold():
        if char.isalnum() or char in "$":
            pieces.append(char)
        else:
            pieces.append(" ")
    return " ".join("".join(pieces).split())


def is_casual_chat(text: str | None) -> bool:
    """True for greetings and short acknowledgements with no stakes."""
    if is_high_stakes_ask(text):
        return False
    folded = _fold(text)
    tokens = folded.split()
    if not tokens:
        return True
    if len(tokens) <= 8 and all(token in _CASUAL_TOKENS for token in tokens):
        return True
    return False


def is_high_stakes_ask(text: str | None) -> bool:
    folded = _fold(text)
    if not folded:
        return False
    return bool(_HIGH_STAKES.search(folded) or _HIGH_STAKES.search(text or ""))


def is_ambiguous_ask(text: str | None) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    folded = _fold(raw)
    tokens = [token for token in folded.split() if token != "grim"]
    if not tokens:
        return False
    if len(tokens) <= 3 and tokens[-1] in {"this", "that", "it", "those", "these", "which"}:
        return True
    return bool(_AMBIGUOUS.search(raw) or _AMBIGUOUS.search(folded))


def needs_repair_confirm(text: str | None) -> bool:
    """One read-back only on ambiguous or high-stakes asks, never casual chat."""
    if is_casual_chat(text):
        return False
    return is_high_stakes_ask(text) or is_ambiguous_ask(text)


def is_agentic_yes(text: str | None) -> bool:
    """Human accepted a next-step offer. Still text-only; no extra tools."""
    return bool(_AGENTIC_YES.search(str(text or "")))


def prefers_language_context(ask_text: str | None) -> bool:
    return bool(_PREF_ASK.search(str(ask_text or "")))


def format_sticky_block(
    *,
    topics: list[str] | None = None,
    prefs: list[str] | None = None,
    vibe: str | None = None,
) -> str | None:
    """Compact sticky snippet. Empty when nothing correlated."""
    topic_lines = [line.strip() for line in (topics or []) if line and line.strip()]
    pref_lines = [line.strip() for line in (prefs or []) if line and line.strip()]
    vibe_line = (vibe or "").strip()
    if not topic_lines and not pref_lines and not vibe_line:
        return None
    parts = [f"{STICKY_HEADER}:"]
    if topic_lines:
        clipped = [line[:MAX_STICKY_LINE] for line in topic_lines[:MAX_STICKY_TOPICS]]
        parts.append("recent topics: " + " | ".join(clipped))
    if pref_lines:
        clipped = [line[:MAX_STICKY_LINE] for line in pref_lines[:MAX_STICKY_PREFS]]
        parts.append("stored prefs: " + " | ".join(clipped))
    if vibe_line:
        parts.append("channel vibe: " + vibe_line[:MAX_STICKY_LINE])
    parts.append(
        "Use this only if it helps the current ask. Never dump unrelated leftovers. "
        "A new thread does not wipe this."
    )
    return "\n".join(parts)


def reply_loop_instructions(
    *,
    repair: bool,
    nudges_on: bool,
    sticky_block: str | None = None,
) -> str:
    """Prompt notes for the same completion. Empty-ish when there is nothing extra."""
    parts = [AGENTIC_NOTE]
    if sticky_block:
        parts.append(sticky_block)
    if repair:
        parts.append(REPAIR_NOTE)
        parts.append(
            "At most one question in this reply. If you already read the ask back, "
            "do not add a nudge."
        )
    elif nudges_on:
        parts.append(NUDGE_ON_NOTE)
    else:
        parts.append(NUDGE_OFF_NOTE)
    return "\n".join(parts)


def is_feedback_emoji(emoji) -> str | None:
    """Return 'up' or 'down' for Grim's feedback reactions, else None."""
    name = getattr(emoji, "name", None)
    raw = str(name if name else emoji or "")
    if raw in {FEEDBACK_UP, ":+1:", "thumbsup"}:
        return "up"
    if raw in {FEEDBACK_DOWN, ":-1:", "thumbsdown"}:
        return "down"
    return None


def miss_note_from_reaction() -> str:
    return "thumbs-down on Grim's last reply"


def miss_note_from_explanation(text: str | None) -> str:
    body = " ".join(str(text or "").split())[:300]
    if not body:
        return miss_note_from_reaction()
    return f"missed: {body}"


def _remember_prompted(message_id: str) -> None:
    mid = str(message_id)
    if mid in _prompted_lookup:
        return
    if len(_prompted_message_ids) == _prompted_message_ids.maxlen:
        dropped = _prompted_message_ids[0]
        _prompted_lookup.discard(dropped)
    _prompted_message_ids.append(mid)
    _prompted_lookup.add(mid)


def already_prompted_miss(message_id: str | None) -> bool:
    return str(message_id or "") in _prompted_lookup


def mark_miss_prompted(message_id: str | None) -> None:
    if message_id:
        _remember_prompted(str(message_id))


def set_pending_miss(channel_id: str, user_id: str, grim_message_id: str) -> None:
    _pending_miss[(str(channel_id), str(user_id))] = str(grim_message_id)
    mark_miss_prompted(grim_message_id)


def take_pending_miss(channel_id: str, user_id: str) -> str | None:
    return _pending_miss.pop((str(channel_id), str(user_id)), None)


def clear_loop_state() -> None:
    _pending_miss.clear()
    _prompted_message_ids.clear()
    _prompted_lookup.clear()
