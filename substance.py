"""Grim's substance guard: every reply says something, whatever the mood.

Grim shifts tone with the room (answering, teaching, comforting, joking,
being straight about a limit). Mood changes tone, never content. This module
holds two cheap things:

* ``SUBSTANCE_PROMPT``: one short rule per mood, added to the mention prompt.
* ``ensure_substance``: a local, no-model check on the finished reply. A thin
  reply gets one retry with a stronger instruction. If the retry is also thin,
  Grim says a short honest line instead of filler.

The guard never writes facts. The retry note repeats the live-source rule, and
the fallback line only admits the gap. Nothing here keeps or logs message text.
"""

from __future__ import annotations

import hashlib
import re
from typing import Awaitable, Callable, NamedTuple

class MoodRule(NamedTuple):
    mood: str
    rule: str


MOOD_RULES: tuple[MoodRule, ...] = (
    MoodRule("direct answer", "give the answer first, then a reason only if it earns its place"),
    MoodRule("teaching", "give the one idea worth learning in your own words, with an example or next step"),
    MoodRule("empathy and presence", "acknowledge it in your own words, then add one honest thing: a view, or a next step if they want one"),
    MoodRule("tough love", "say the hard truth and the reason for it, with no cruelty"),
    MoodRule("humor and banter", "make a real joke or take about what they said, not a bare reaction"),
    MoodRule("transparency", "say what you can't do or can't verify, why, and what you can offer instead"),
    MoodRule("playing along", "give the next line, the matching reference, or your move in the game"),
    MoodRule("working it through", "restating is allowed only here, as a mirror for someone thinking out loud, and it must add something new"),
)

SUBSTANCE_PROMPT = (
    "SUBSTANCE IN EVERY MOOD:\n"
    "Mood sets your tone and never removes the content. Whatever you are being, "
    "a reply carries something real: an answer, a concrete next step, a fact you "
    "can stand behind, a clear opinion with its reason, or something worth learning. "
    "Never reply with only a reaction, a stock line, a dodge, or a counter-question "
    "when a direct question was asked. Never echo or polish the person's own message "
    "back to them as your reply. Restating is an introspection tool, only for someone "
    "working something through, and never the default when you have nothing to say. "
    "If you have nothing real, say so in one short honest line. "
    "Small talk stays small and casual, but never empty. Do not get longer for this. "
    "Do not invent facts to sound substantial, and do not add claims from memory "
    "about current events.\n"
    + "\n".join(f"- {rule.mood}: {rule.rule}." for rule in MOOD_RULES)
)

_STOPWORDS = frozenset({
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "and", "or", "nor",
    "what", "which", "who", "whom", "whose", "where", "when", "why", "how",
    "is", "are", "was", "were", "be", "been", "being", "do", "does", "did",
    "this", "that", "these", "those", "with", "from", "by", "as", "into",
    "it", "its", "his", "her", "their", "about", "than", "then", "if", "but",
    "you", "your", "youre", "yours", "i", "im", "ive", "my", "me", "we", "our",
    "they", "them", "he", "she", "so", "just", "like", "really", "very",
    "not", "dont", "cant", "can", "will", "would", "could", "should", "have",
    "has", "had", "there", "here", "too", "also", "all", "some", "any",
})

_QUESTION_STARTS = frozenset({
    "what", "who", "when", "where", "why", "how", "which", "is", "are", "was",
    "were", "did", "does", "do", "can", "could", "will", "would", "should",
    "tell", "explain", "define", "recommend", "help", "show", "give",
    "write", "make", "list", "summarize", "summarise", "translate", "describe",
    "compare", "suggest", "find", "fix", "check", "name", "rate", "roast",
    "create", "generate", "draw", "pick",
})

_HOLLOW_WORDS = frozenset({
    "interesting", "noted", "understood", "indeed", "hmm", "hm", "hmmm", "ah",
    "oh", "aha", "well",
})

_YES_NO_WORDS = frozenset({"yes", "no", "yeah", "yep", "yup", "nope", "nah"})

_HEAVY = re.compile(
    r"\b(?:died|dead|death|dying|suicid\w*|depress\w*|addict\w*|relaps\w*|overdos\w*|"
    r"funeral|divorc\w*|cancer|panic\w*|anxi\w*|grief|grieving|lonely|alone|"
    r"hurt\w*|scared|afraid|abus\w*|fired|laid off|broke up|breakup|miscarr\w*|"
    r"hate myself|give up|can'?t do this|losing it)\b",
    re.IGNORECASE,
)

_INTROSPECTIVE = re.compile(
    r"(?:i'?ve been thinking|i keep (?:thinking|wondering)|thinking out loud|"
    r"working (?:through|out)|trying to (?:figure|work|understand|decide)|"
    r"not sure (?:if|whether|what|how)|don'?t know (?:if|whether|what|how)|"
    r"can'?t decide|torn (?:between|about)|struggling (?:with|to)|"
    r"wondering (?:if|whether)|confused (?:about|by)|"
    r"does that make sense|figure (?:it|this|that) out)",
    re.IGNORECASE,
)

_FILLER_WORDS = frozenset({
    "lol", "lmao", "lmfao", "haha", "hahaha", "heh", "hm", "hmm", "hmmm", "ha",
    "damn", "dang", "wow", "whoa", "oof", "rip", "yikes", "ouch", "bruh",
    "true", "truth", "facts", "fax", "fair", "valid", "same", "mood", "real",
    "based", "nice", "cool", "sick", "wild", "crazy", "insane", "ok", "okay",
    "alright", "right", "sure", "indeed", "totally", "exactly", "absolutely",
    "interesting", "noted", "understood", "ah", "oh", "ooh", "aha", "well",
    "big", "huge", "man", "dude", "fam", "bro", "gg",
})

_STOCK_PHRASES = (
    "i see", "i hear you", "i get it", "got it", "makes sense", "that makes sense",
    "fair enough", "good point", "great point", "great question", "good question",
    "that's a good question", "that's a good point", "that's valid", "that's fair",
    "that's interesting", "that's wild", "that's crazy", "that's deep",
    "it is what it is", "such is life", "to each their own", "everyone's different",
    "everybody's different", "life is complicated", "you do you", "no worries",
    "thanks for sharing", "i understand", "i appreciate you", "interesting take",
    "that hits", "that's real", "say more", "tell me more",
    "interesting topic", "interesting question", "big topic", "deep topic",
    "big question", "deep question", "tough question", "loaded question",
)
_STOCK_NORMALIZED = tuple(
    sorted(
        {" ".join(re.sub(r"[^a-z0-9 ]", "", p.replace("'", "").lower()).split()) for p in _STOCK_PHRASES},
        key=len,
        reverse=True,
    )
)

_DODGES = re.compile(
    r"\b(?:depends|who knows|hard to say|can'?t say|hard to tell|up to you|"
    r"it'?s complicated|everyone'?s different|everybody'?s different|"
    r"only you can (?:say|decide|know)|there'?s no (?:right|wrong)|"
    r"that'?s a (?:good|great|tough|big) question|what do you think)\b",
    re.IGNORECASE,
)

_RESTATE_FRAME = re.compile(
    r"^\W*(?:so,? |ok(?:ay)?,? so,? |basically,? |sounds like |it sounds like |"
    r"seems like you|what you'?re saying|you'?re saying|you are saying|"
    r"if i'?m hearing you|i hear you saying|i'?m hearing|what i'?m hearing|"
    r"to put it another way|in other words|put differently|"
    r"you mean (?:that )?|you'?re telling me)",
    re.IGNORECASE,
)


def _words(text: str) -> list[str]:
    cleaned = re.sub(r"[’']", "", str(text or "").casefold())
    return re.findall(r"[a-z0-9]+", cleaned)


def _significant(words: list[str]) -> set[str]:
    return {w for w in words if w not in _STOPWORDS and len(w) >= 3}


def _normalized(text: str) -> str:
    return " ".join(_words(text))


def classify_message(user_text: str | None) -> str:
    """One of: question, heavy, substantive, small_talk."""
    text = " ".join(str(user_text or "").split())
    words = _words(text)
    if not words:
        return "small_talk"
    if _HEAVY.search(text):
        return "heavy"
    if "?" in text or words[0] in _QUESTION_STARTS:
        return "question"
    if len(words) >= 9:
        return "substantive"
    return "small_talk"


def _strip_stock(normalized: str) -> str:
    remaining = f" {normalized} "
    for phrase in _STOCK_NORMALIZED:
        remaining = remaining.replace(f" {phrase} ", " ")
    return " ".join(remaining.split())


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def substance_problem(reply: str | None, user_text: str | None = None) -> str | None:
    """None when the reply has substance for what was said. No model call."""
    if not isinstance(reply, str) or not reply.strip():
        return "empty"
    kind = classify_message(user_text)
    reply_words = _words(reply)
    if not reply_words:
        return "empty"
    normalized = " ".join(reply_words)
    without_stock = _strip_stock(normalized)
    if not without_stock:
        return "stock line"

    user_words = _words(user_text)
    reply_sig = _significant(reply_words)
    user_sig = _significant(user_words)
    new_sig = reply_sig - user_sig
    introspective = bool(_INTROSPECTIVE.search(str(user_text or "")))

    if kind == "small_talk":
        if all(w in _HOLLOW_WORDS for w in without_stock.split()):
            return "stock line"
        if len(user_sig) >= 3 and len(reply_sig) >= 2 and len(new_sig) <= 1:
            overlap = len(reply_sig & user_sig) / len(reply_sig)
            if overlap >= 0.75:
                return "repeats the message"
        return None

    remaining = [w for w in without_stock.split() if w not in _FILLER_WORDS]
    if not (kind == "question" and len(user_sig) < 4):
        remaining = [w for w in remaining if w not in _YES_NO_WORDS]
    if not remaining:
        return "only reaction words"

    if kind in {"heavy", "substantive"} and len(reply_words) < 4:
        return "too thin for what was said"
    if kind == "question" and len(user_sig) >= 4 and len(reply_words) < 3:
        return "too short for a real question"

    if len(user_sig) >= 3 and len(reply_sig) >= 2:
        overlap = len(reply_sig & user_sig) / len(reply_sig)
        limit = 0 if kind == "question" else 1
        if overlap >= 0.75 and len(new_sig) <= limit:
            return "repeats the message"

    if _RESTATE_FRAME.search(reply.strip()):
        needed = 3 if introspective else 6
        if len(new_sig) < needed:
            return "restates instead of adding"

    if kind == "question":
        sentences = _sentences(reply)
        if (
            sentences
            and all(s.endswith("?") for s in sentences)
            and not introspective
        ):
            return "dodges with a question"
        if _DODGES.search(reply) and len(new_sig) < 5:
            return "dodges the question"
    return None


def _first_line_hash(user_text: str | None) -> int:
    digest = hashlib.sha256(str(user_text or "").encode("utf-8")).digest()
    return digest[0]


_HONEST_QUESTION = (
    "i don't have a real answer for that right now. ask it another way or give me more to work with.",
    "can't give you a straight answer on that one yet. give me more and i'll take a real shot.",
    "nothing solid from me on that yet. tighten the question and i'll answer it straight.",
)
_HONEST_HEAVY = (
    "i don't have the right words for that yet and i won't fake them. i'm here, so tell me more.",
    "that deserves better than what i've got right now. i'm not going anywhere, say more and i'll meet it.",
)
_HONEST_OTHER = (
    "don't have anything worth saying on that yet. give me more and i'll give you something real.",
    "not sure what to add there and i won't fake it. tell me more and i'll come back with something real.",
)


def honest_short_reply(user_text: str | None) -> str:
    """Short, true line for when two tries produced nothing worth sending."""
    kind = classify_message(user_text)
    pool = {
        "question": _HONEST_QUESTION,
        "heavy": _HONEST_HEAVY,
    }.get(kind, _HONEST_OTHER)
    return pool[_first_line_hash(user_text) % len(pool)]


_RETRY_BY_KIND = {
    "question": (
        "They asked a direct question. Answer it in the first sentence. "
        "Do not answer with a question or a dodge."
    ),
    "heavy": (
        "They said something heavy. Stay steady and honest. Acknowledge it in your own "
        "words, not theirs, and add one real thing: an honest view or a next step."
    ),
    "substantive": (
        "Give a clear take with its reason, or one concrete next step, or something "
        "worth learning. Do not mirror what they said."
    ),
    "small_talk": (
        "Keep it casual and short, but say something real and specific to what they said. "
        "A bare reaction word or stock line is not enough."
    ),
}


def retry_instruction(problem: str, user_text: str | None = None) -> str:
    """Stronger system note for the single retry. Contains no message text."""
    kind = classify_message(user_text)
    return (
        "SUBSTANCE RETRY:\n"
        f"Your last draft failed a substance check ({problem}). Rewrite it. "
        "Keep the same Grim voice and the same mood, but deliver something real: "
        "an answer, a concrete next step, a clear opinion with a reason, or useful "
        "teaching. Do not repeat or polish the person's own message back to them, "
        "and do not restate it unless they are thinking something through. "
        "No filler, no stock lines, no vague reaction. "
        f"{_RETRY_BY_KIND[kind]} "
        "Do not invent facts. Do not add claims from memory about current events, "
        "people, prices, or news. If something needs a live check you do not have, "
        "say so plainly in one line. Do not make the reply longer than a natural "
        "answer needs."
    )


class SubstanceResult(NamedTuple):
    text: str
    outcome: str  # "passed", "retried", or "fallback"
    problem: str | None = None


RetryCall = Callable[[str], Awaitable[str | None]]


async def ensure_substance(
    reply: str | None,
    user_text: str | None,
    retry: RetryCall,
) -> SubstanceResult:
    """Pass a substantive reply. Otherwise retry once, then say an honest short line.

    ``retry`` receives the stronger instruction and returns a new draft. Errors
    from it count as a failed retry. Timeouts belong to the caller's own wrapper.
    """
    problem = substance_problem(reply, user_text)
    if problem is None:
        return SubstanceResult(str(reply), "passed")
    print(f"[Substance] reply failed a local check: {problem}")
    try:
        second = await retry(retry_instruction(problem, user_text))
    except Exception as error:
        print(f"[Substance] retry failed: {type(error).__name__}")
        second = None
    if isinstance(second, str):
        second = second.strip()
    if second and substance_problem(second, user_text) is None:
        return SubstanceResult(second, "retried", problem)
    print("[Substance] retry also failed, using the honest short reply")
    return SubstanceResult(honest_short_reply(user_text), "fallback", problem)
