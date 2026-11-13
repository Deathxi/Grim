"""Grim's reply auditor: notice stuck, looping, or off-voice replies and recover.

The auditor sits around Grim's reply work. It never decides what Grim says on a
normal turn; it only steps in when work stalls, repeats itself, or drifts from
the voice rules, and it gets out of the way if it fails itself.

OpSec: events hold only an event type, a work kind, a duration, and a count.
Message text, user ids, and channel ids are never logged or kept in events.
Conversation keys live in memory only and are dropped when they go stale.
"""

from __future__ import annotations

import asyncio
import contextvars
import difflib
import functools
import hashlib
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, NamedTuple

DEFAULT_TIMEOUTS = {
    "reply": 45.0,
    "fact_reply": 60.0,
    "image_reply": 50.0,
    "live_search": 25.0,
    "fact_check": 25.0,
    "chime": 60.0,
    "retry": 15.0,
}

STUCK_BACK_LINE = "got stuck for a second. cut it loose, i'm back."
STUCK_FAIL_LINE = "got stuck and cut it loose. no answer this time, call me again."
STUCK_FACT_LINE = (
    "got stuck on the live check and cut it loose. couldn't verify that right now, "
    "so i won't guess. call me again."
)
STUCK_IMAGE_LINE = (
    "got stuck looking at that and cut it loose. couldn't read it this time, call me again."
)
LOOP_LINE = (
    "caught myself saying the same thing again. cleared my head. "
    "ask it fresh and i'll come at it new."
)
BURST_LINE = "that's a lot of calls in a row. give me a breath, then ask once."
AUDITOR_LINES = frozenset({
    STUCK_BACK_LINE,
    STUCK_FAIL_LINE,
    STUCK_FACT_LINE,
    STUCK_IMAGE_LINE,
    LOOP_LINE,
    BURST_LINE,
})

_CONVERSATION: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "grim_auditor_conversation", default=None
)


def _env_number(name: str, default: float, low: float, high: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value != value or value < low or value > high:
        return default
    return value


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class AuditorConfig:
    enabled: bool = True
    timeouts: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_TIMEOUTS))
    loop_similarity: float = 0.9
    loop_repeat_limit: int = 3
    loop_window_seconds: float = 600.0
    tool_repeat_limit: int = 4
    burst_limit: int = 8
    burst_window_seconds: float = 60.0
    question_streak_limit: int = 3
    stock_line_limit: int = 4
    stock_window_seconds: float = 86400.0
    intervention_cooldown_seconds: float = 300.0
    global_intervention_limit: int = 10
    global_intervention_window_seconds: float = 600.0
    event_log_size: int = 100

    @classmethod
    def from_env(cls) -> "AuditorConfig":
        timeouts = {
            kind: _env_number(f"GRIM_AUDITOR_TIMEOUT_{kind.upper()}", seconds, 1, 300)
            for kind, seconds in DEFAULT_TIMEOUTS.items()
        }
        return cls(
            enabled=_env_flag("GRIM_AUDITOR_ENABLED", True),
            timeouts=timeouts,
            loop_similarity=_env_number("GRIM_AUDITOR_LOOP_SIMILARITY", 0.9, 0.5, 1.0),
            loop_repeat_limit=int(_env_number("GRIM_AUDITOR_LOOP_REPEATS", 3, 2, 20)),
            loop_window_seconds=_env_number("GRIM_AUDITOR_LOOP_WINDOW", 600, 30, 86400),
            tool_repeat_limit=int(_env_number("GRIM_AUDITOR_TOOL_REPEATS", 4, 2, 50)),
            burst_limit=int(_env_number("GRIM_AUDITOR_BURST_LIMIT", 8, 2, 100)),
            burst_window_seconds=_env_number("GRIM_AUDITOR_BURST_WINDOW", 60, 5, 3600),
            question_streak_limit=int(_env_number("GRIM_AUDITOR_QUESTION_STREAK", 3, 2, 50)),
            stock_line_limit=int(_env_number("GRIM_AUDITOR_STOCK_LIMIT", 4, 2, 100)),
            intervention_cooldown_seconds=_env_number(
                "GRIM_AUDITOR_COOLDOWN", 300, 10, 86400
            ),
            global_intervention_limit=int(
                _env_number("GRIM_AUDITOR_GLOBAL_LIMIT", 10, 1, 1000)
            ),
        )

    def timeout_for(self, kind: str) -> float:
        return float(self.timeouts.get(kind, DEFAULT_TIMEOUTS["reply"]))


class Outcome(NamedTuple):
    value: Any = None
    timed_out: bool = False
    duration: float = 0.0


class GuardedReply(NamedTuple):
    """value is the work's own result. recovery is a line the auditor wants sent instead."""

    value: Any = None
    recovery: str | None = None
    event: str | None = None


class Review(NamedTuple):
    text: str | None
    event: str | None = None
    flags: tuple[str, ...] = ()


def conversation_key(guild_id: Any, channel_id: Any, user_id: Any) -> str:
    return f"{guild_id}:{channel_id}:{user_id}"


def current_conversation() -> str | None:
    return _CONVERSATION.get()


def _normalize(text: str) -> str:
    folded = re.sub(r"https?://\S+", " ", str(text or "").casefold())
    folded = re.sub(r"[^\w\s]", " ", folded)
    return " ".join(folded.split())[:600]


def _fingerprint(text: str) -> str:
    return hashlib.sha256(_normalize(text).encode("utf-8")).hexdigest()[:16]


def _similar(left: str, right: str, threshold: float) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    matcher = difflib.SequenceMatcher(None, left, right)
    return matcher.quick_ratio() >= threshold and matcher.ratio() >= threshold


_OPENER = re.compile(r"^\s*(?:ah+|ahh+)\s*[,.!]+\s*", re.IGNORECASE)
_DASH = re.compile(r"\s*[\u2014\u2013]\s*")
_BULLET = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s+\S", re.MULTILINE)
_SYCOPHANCY = re.compile(
    r"\b(?:great|good|excellent) (?:point|question)\b|\bwhat a (?:great|good) question\b",
    re.IGNORECASE,
)
_FIRST_SENTENCE = re.compile(r"^(.+?[.!?])(?:\s|$)", re.DOTALL)


def nudge_style(text: str) -> tuple[str, tuple[str, ...]]:
    """Light voice pass. Fixes only safe surface slips; everything else is a flag."""
    flags: list[str] = []
    if "```" in text:
        return text, ()
    nudged = text
    if _DASH.search(nudged):
        nudged = _DASH.sub(", ", nudged)
        flags.append("dash")
    if _OPENER.match(nudged):
        stripped = _OPENER.sub("", nudged, count=1)
        if stripped.strip():
            nudged = stripped
            flags.append("opener")
    if len(_BULLET.findall(nudged)) >= 2:
        flags.append("bullets")
    if _SYCOPHANCY.search(nudged):
        flags.append("sycophancy")
    return nudged, tuple(flags)


class ReplyAuditor:
    def __init__(
        self,
        config: AuditorConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        log: Callable[[str], None] = print,
    ):
        self.config = config or AuditorConfig()
        self._clock = clock
        self._wall = wall
        self._log = log
        self._events: deque[dict] = deque(maxlen=max(1, self.config.event_log_size))
        self._counts: dict[str, int] = {}
        self._replies: dict[str, deque[tuple[float, str]]] = {}
        self._tools: dict[str, deque[tuple[float, str]]] = {}
        self._turns: dict[str, deque[float]] = {}
        self._question_streak: dict[str, int] = {}
        self._stock: dict[str, dict[str, float]] = {}
        self._last_intervention: dict[str, float] = {}
        self._global_interventions: deque[float] = deque()
        self._burst_noticed: dict[str, float] = {}
        self._resets: list[Callable[[str], None]] = []

    # ── bookkeeping ──────────────────────────────────────────────────────────

    def register_reset(self, callback: Callable[[str], None]) -> None:
        self._resets.append(callback)

    def record(self, event: str, kind: str, *, duration: float | None = None, count: int | None = None) -> None:
        try:
            entry = {"at": self._wall(), "event": event, "kind": kind}
            if duration is not None:
                entry["duration"] = round(float(duration), 2)
            if count is not None:
                entry["count"] = int(count)
            self._events.append(entry)
            self._counts[event] = self._counts.get(event, 0) + 1
            details = "".join(
                f" {name}={entry[name]}" for name in ("duration", "count") if name in entry
            )
            self._log(f"[Auditor] {event} kind={kind}{details}")
        except Exception:
            pass

    def recent_events(self, limit: int = 15) -> list[dict]:
        return list(self._events)[-max(0, limit):]

    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    def reset_conversation(self, conversation: str | None) -> None:
        if not conversation:
            return
        self._replies.pop(conversation, None)
        self._tools.pop(conversation, None)
        self._question_streak.pop(conversation, None)
        for callback in list(self._resets):
            try:
                callback(conversation)
            except Exception as error:
                self._log(f"[Auditor] reset hook failed: {type(error).__name__}")

    def allow_intervention(self, conversation: str | None) -> bool:
        """At most one intervention per conversation per cooldown, and a global cap."""
        now = self._clock()
        window = self.config.global_intervention_window_seconds
        while self._global_interventions and now - self._global_interventions[0] >= window:
            self._global_interventions.popleft()
        key = conversation or "-"
        last = self._last_intervention.get(key)
        if last is not None and now - last < self.config.intervention_cooldown_seconds:
            self.record("intervention_suppressed", "cooldown")
            return False
        if len(self._global_interventions) >= self.config.global_intervention_limit:
            self.record("intervention_suppressed", "global_cap")
            return False
        self._last_intervention[key] = now
        self._global_interventions.append(now)
        return True

    def _prune(self, now: float) -> None:
        horizon = max(self.config.loop_window_seconds, self.config.burst_window_seconds)
        for store in (self._replies, self._tools):
            for key in [k for k, items in store.items() if not items or now - items[-1][0] > horizon]:
                store.pop(key, None)
        for key in [k for k, items in self._turns.items() if not items or now - items[-1] > horizon]:
            self._turns.pop(key, None)
        stale = self.config.stock_window_seconds
        for line in list(self._stock):
            seen = {c: t for c, t in self._stock[line].items() if now - t < stale}
            if seen:
                self._stock[line] = seen
            else:
                self._stock.pop(line, None)
        cooldown = max(self.config.intervention_cooldown_seconds, self.config.burst_window_seconds)
        for store in (self._last_intervention, self._burst_noticed):
            for key in [k for k, t in store.items() if now - t > cooldown]:
                store.pop(key, None)

    # ── timeouts ─────────────────────────────────────────────────────────────

    async def run(
        self,
        kind: str,
        work: Callable[[], Awaitable[Any]],
        *,
        conversation: str | None = None,
        timeout: float | None = None,
    ) -> Outcome:
        """Run work under the kind's time budget. A stalled call is cancelled."""
        if not self.config.enabled:
            return Outcome(await work())
        try:
            limit = float(timeout if timeout is not None else self.config.timeout_for(kind))
            token = _CONVERSATION.set(conversation) if conversation else None
            started = self._clock()
        except Exception:
            return Outcome(await work())
        try:
            value = await asyncio.wait_for(work(), timeout=limit)
            return Outcome(value, False, self._clock() - started)
        except asyncio.TimeoutError:
            elapsed = self._clock() - started
            self.record("timeout", kind, duration=elapsed)
            return Outcome(None, True, elapsed)
        finally:
            if token is not None:
                try:
                    _CONVERSATION.reset(token)
                except Exception:
                    pass

    async def guarded_reply(
        self,
        kind: str,
        conversation: str,
        work: Callable[[], Awaitable[Any]],
        *,
        retry: Callable[[], Awaitable[str | None]] | None = None,
        user_waiting: bool = True,
    ) -> GuardedReply:
        """Run one reply. On a stall: cancel, reset, retry once if offered, then fail plainly."""
        if not self.config.enabled:
            return GuardedReply(await work())
        try:
            within_pace = self.note_turn(conversation)
            if not within_pace:
                self.reset_conversation(conversation)
                if user_waiting and self._first_burst_notice(conversation):
                    return GuardedReply(recovery=BURST_LINE, event="reply_chain")
                return GuardedReply(event="reply_chain")
        except Exception as error:
            self._log(f"[Auditor] guard failed open: {type(error).__name__}")
        outcome = await self.run(kind, work, conversation=conversation)
        if not outcome.timed_out:
            return GuardedReply(outcome.value)
        try:
            return await self._recover(kind, conversation, outcome, retry, user_waiting)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._log(f"[Auditor] recovery failed open: {type(error).__name__}")
            return GuardedReply(event="timeout")

    async def _recover(
        self,
        kind: str,
        conversation: str,
        outcome: Outcome,
        retry: Callable[[], Awaitable[str | None]] | None,
        user_waiting: bool,
    ) -> GuardedReply:
        self.reset_conversation(conversation)
        if not user_waiting:
            return GuardedReply(event="timeout")
        if not self.allow_intervention(conversation):
            return GuardedReply(event="timeout")
        if retry is not None:
            try:
                second = await self.run("retry", retry, conversation=conversation)
            except Exception as error:
                self._log(f"[Auditor] retry failed: {type(error).__name__}")
                second = Outcome(None, True, 0.0)
            if not second.timed_out and isinstance(second.value, str) and second.value.strip():
                self.record("recovered", kind, duration=outcome.duration + second.duration)
                return GuardedReply(
                    recovery=f"{STUCK_BACK_LINE}\n\n{second.value.strip()}",
                    event="recovered",
                )
        self.record("recovery_failed", kind, duration=outcome.duration)
        return GuardedReply(recovery=self._failure_line(kind), event="recovery_failed")

    @staticmethod
    def _failure_line(kind: str) -> str:
        if kind == "fact_reply":
            return STUCK_FACT_LINE
        if kind == "image_reply":
            return STUCK_IMAGE_LINE
        return STUCK_FAIL_LINE

    def note_turn(self, conversation: str) -> bool:
        """False when one conversation calls Grim faster than a person plausibly would."""
        now = self._clock()
        self._prune(now)
        turns = self._turns.setdefault(conversation, deque())
        while turns and now - turns[0] >= self.config.burst_window_seconds:
            turns.popleft()
        turns.append(now)
        if len(turns) > self.config.burst_limit:
            self.record("reply_chain", "reply", count=len(turns))
            return False
        return True

    def _first_burst_notice(self, conversation: str) -> bool:
        if conversation in self._burst_noticed:
            return False
        if not self.allow_intervention(conversation):
            return False
        self._burst_noticed[conversation] = self._clock()
        return True

    # ── tool loops ───────────────────────────────────────────────────────────

    def allow_tool_call(self, tool: str, material: str) -> bool:
        """False when this conversation keeps firing the same tool call."""
        try:
            conversation = current_conversation()
            if not conversation or not self.config.enabled:
                return True
            now = self._clock()
            calls = self._tools.setdefault(conversation, deque())
            while calls and now - calls[0][0] >= self.config.loop_window_seconds:
                calls.popleft()
            print_key = f"{tool}:{_fingerprint(material)}"
            repeats = sum(1 for _, seen in calls if seen == print_key)
            calls.append((now, print_key))
            if repeats + 1 >= self.config.tool_repeat_limit:
                self.record("tool_loop", tool, count=repeats + 1)
                return False
            return True
        except Exception:
            return True

    def watch_tool(
        self,
        kind: str,
        func: Callable[..., Awaitable[Any]],
        *,
        material: Callable[..., str],
        on_timeout: Callable[[], Any],
        on_blocked: Callable[[], Any],
    ) -> Callable[..., Awaitable[Any]]:
        """Wrap one async tool with its time budget and repeat guard."""

        @functools.wraps(func)
        async def watched(*args, **kwargs):
            try:
                allowed = self.allow_tool_call(kind, material(*args, **kwargs))
            except Exception:
                allowed = True
            if not allowed:
                return on_blocked()
            outcome = await self.run(kind, lambda: func(*args, **kwargs))
            return on_timeout() if outcome.timed_out else outcome.value

        watched.__wrapped_by_auditor__ = True
        return watched

    # ── reply review ─────────────────────────────────────────────────────────

    def review_reply(self, conversation: str, text: str | None) -> Review:
        """Check a finished reply for loops and voice drift before it is sent."""
        try:
            if not self.config.enabled or not isinstance(text, str) or not text.strip():
                return Review(text)
            if text in AUDITOR_LINES or text.startswith(STUCK_BACK_LINE):
                return Review(text)
            now = self._clock()
            self._prune(now)
            normalized = _normalize(text)
            history = self._replies.setdefault(conversation, deque(maxlen=12))
            while history and now - history[0][0] >= self.config.loop_window_seconds:
                history.popleft()
            repeats = sum(
                1 for _, seen in history if _similar(seen, normalized, self.config.loop_similarity)
            )
            if repeats + 1 >= self.config.loop_repeat_limit:
                self.record("reply_loop", "reply", count=repeats + 1)
                if self.allow_intervention(conversation):
                    self.reset_conversation(conversation)
                    return Review(LOOP_LINE, "reply_loop")
            history.append((now, normalized))

            nudged, flags = nudge_style(text)
            flags = list(flags)
            if nudged.rstrip().endswith("?"):
                streak = self._question_streak.get(conversation, 0) + 1
                self._question_streak[conversation] = streak
                if streak >= self.config.question_streak_limit:
                    flags.append("question_streak")
            else:
                self._question_streak.pop(conversation, None)
            if self._stock_line(conversation, nudged, now):
                flags.append("stock_line")
            for flag in flags:
                self.record("style", flag)
            return Review(nudged, "style" if flags else None, tuple(flags))
        except Exception as error:
            self._log(f"[Auditor] review failed open: {type(error).__name__}")
            return Review(text)

    def _stock_line(self, conversation: str, text: str, now: float) -> bool:
        match = _FIRST_SENTENCE.match(text.strip())
        sentence = _normalize(match.group(1) if match else text)
        if len(sentence.split()) < 4:
            return False
        line = _fingerprint(sentence)
        seen = self._stock.setdefault(line, {})
        seen[_fingerprint(conversation)] = now
        if len(self._stock) > 2000:
            oldest = min(self._stock, key=lambda key: max(self._stock[key].values()))
            self._stock.pop(oldest, None)
        return len(seen) >= self.config.stock_line_limit


def format_events(auditor: ReplyAuditor, *, limit: int = 15, now: float | None = None) -> str:
    """Owner view: event type, kind, duration, counts. Nothing about who or what was said."""
    current = time.time() if now is None else now
    lines = []
    for entry in reversed(auditor.recent_events(limit)):
        age = max(0, int(current - entry.get("at", current)))
        extra = ""
        if "duration" in entry:
            extra += f" {entry['duration']}s"
        if "count" in entry:
            extra += f" x{entry['count']}"
        lines.append(f"`{age // 60}m ago` {entry['event']} ({entry['kind']}){extra}")
    totals = ", ".join(f"{name} {count}" for name, count in sorted(auditor.counts().items()))
    body = "\n".join(lines) if lines else "nothing flagged since the last restart."
    return f"{body}\n\ntotals: {totals or 'none'}"[:1900]
