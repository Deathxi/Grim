"""Grim's reply auditor: notice stuck, looping, or off-voice replies and recover.

The auditor sits around Grim's reply work. It never decides what Grim says on a
normal turn; it only steps in when work stalls or a mechanical loop fires, nudges
light voice slips, and gets out of the way if it fails itself.

OpSec: events hold only an event type, a work kind, a duration, and a count.
Message text, invoker names, user ids, and channel ids are never logged, saved,
or posted. Conversation keys live in memory only and are dropped when stale.
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
from collections import Counter, deque
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
    "pivot": 15.0,
}

STUCK_BACK_LINE = "was MIA for a mike. back now."
STUCK_FAIL_LINE = "was MIA for a mike and came back empty. call me again."
STUCK_FACT_LINE = (
    "was MIA for a mike on the live check. couldn't verify that right now, "
    "so i won't guess. call me again."
)
STUCK_IMAGE_LINE = "was MIA for a mike on that image. couldn't read it this time, call me again."
BURST_LINE = "that's a lot of calls in a row. give me a breath, then ask once."
AUDITOR_LINES = frozenset({
    STUCK_FAIL_LINE,
    STUCK_FACT_LINE,
    STUCK_IMAGE_LINE,
    BURST_LINE,
})

EVENT_FIELDS = ("at", "event", "kind", "duration", "count")

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
    repeat_similarity: float = 0.9
    repeat_window_seconds: float = 600.0
    pivot_cooldown_seconds: float = 120.0
    tool_repeat_limit: int = 4
    burst_limit: int = 8
    burst_window_seconds: float = 60.0
    question_streak_limit: int = 3
    stock_line_limit: int = 4
    stock_window_seconds: float = 86400.0
    intervention_cooldown_seconds: float = 300.0
    global_intervention_limit: int = 10
    global_intervention_window_seconds: float = 600.0
    event_log_size: int = 200

    @classmethod
    def from_env(cls) -> "AuditorConfig":
        timeouts = {
            kind: _env_number(f"GRIM_AUDITOR_TIMEOUT_{kind.upper()}", seconds, 1, 300)
            for kind, seconds in DEFAULT_TIMEOUTS.items()
        }
        return cls(
            enabled=_env_flag("GRIM_AUDITOR_ENABLED", True),
            timeouts=timeouts,
            repeat_similarity=_env_number("GRIM_AUDITOR_REPEAT_SIMILARITY", 0.9, 0.5, 1.0),
            repeat_window_seconds=_env_number("GRIM_AUDITOR_REPEAT_WINDOW", 600, 30, 86400),
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


def recovered_reply(answer: str) -> str:
    """One Discord message: the recovery line, a line break, then the answer."""
    return f"{STUCK_BACK_LINE}\n{answer.strip()}"


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


def _clean_event(entry: Any) -> dict | None:
    """Keep only the allowed fields, so a saved file can never carry more."""
    if not isinstance(entry, dict):
        return None
    event, kind = entry.get("event"), entry.get("kind")
    if not isinstance(event, str) or not isinstance(kind, str):
        return None
    cleaned: dict = {"event": event[:40], "kind": kind[:40]}
    try:
        cleaned["at"] = float(entry.get("at", 0))
        if entry.get("duration") is not None:
            cleaned["duration"] = round(float(entry["duration"]), 2)
        if entry.get("count") is not None:
            cleaned["count"] = int(entry["count"])
    except (TypeError, ValueError):
        return None
    return cleaned


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
        load: Callable[[], Any] | None = None,
        save: Callable[[list], None] | None = None,
        notify: Callable[[dict], None] | None = None,
    ):
        self.config = config or AuditorConfig()
        self._clock = clock
        self._wall = wall
        self._log = log
        self._load = load
        self._save = save
        self._notify = notify
        self._events: deque[dict] = deque(maxlen=max(1, self.config.event_log_size))
        self._replies: dict[str, deque[tuple[float, str]]] = {}
        self._tools: dict[str, deque[tuple[float, str]]] = {}
        self._turns: dict[str, deque[float]] = {}
        self._question_streak: dict[str, int] = {}
        self._stock: dict[str, dict[str, float]] = {}
        self._last_intervention: dict[str, float] = {}
        self._last_pivot: dict[str, float] = {}
        self._global_interventions: deque[float] = deque()
        self._burst_noticed: dict[str, float] = {}
        self._resets: list[Callable[[str], None]] = []
        self._events.extend(self._read_saved())

    # ── bookkeeping ──────────────────────────────────────────────────────────

    def register_reset(self, callback: Callable[[str], None]) -> None:
        self._resets.append(callback)

    def _read_saved(self) -> list[dict]:
        if self._load is None:
            return []
        try:
            raw = self._load()
            items = raw if isinstance(raw, list) else []
            return [entry for entry in map(_clean_event, items) if entry]
        except Exception as error:
            self._log(f"[Auditor] could not read saved events: {type(error).__name__}")
            return []

    def record(self, event: str, kind: str, *, duration: float | None = None, count: int | None = None) -> None:
        try:
            entry = _clean_event({
                "at": self._wall(),
                "event": event,
                "kind": kind,
                "duration": duration,
                "count": count,
            })
            if entry is None:
                return
            self._events.append(entry)
            details = "".join(
                f" {name}={entry[name]}" for name in ("duration", "count") if name in entry
            )
            self._log(f"[Auditor] {event} kind={kind}{details}")
        except Exception:
            return
        if self._save is not None:
            try:
                self._save(list(self._events))
            except Exception as error:
                self._log(f"[Auditor] could not save events: {type(error).__name__}")
        if self._notify is not None:
            try:
                self._notify(dict(entry))
            except Exception as error:
                self._log(f"[Auditor] could not queue audit note: {type(error).__name__}")

    def fail_open(self, where: str, error: BaseException) -> None:
        """The auditor broke and stepped aside. Grim keeps replying as before."""
        self._log(f"[Auditor] {where} failed open: {type(error).__name__}")
        self.record("auditor_error", where)

    def recent_events(self, limit: int = 15) -> list[dict]:
        return list(self._events)[-max(0, limit):]

    def saved_events(self, limit: int = 15) -> list[dict]:
        saved = self._read_saved() if self._load is not None else list(self._events)
        return saved[-max(0, limit):]

    def counts(self) -> dict[str, int]:
        return dict(Counter(entry["event"] for entry in self._events))

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
        horizon = max(self.config.repeat_window_seconds, self.config.burst_window_seconds)
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
        cooldown = max(
            self.config.intervention_cooldown_seconds,
            self.config.burst_window_seconds,
            self.config.pivot_cooldown_seconds,
        )
        for store in (self._last_intervention, self._burst_noticed, self._last_pivot):
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
            self.fail_open("guard", error)
        outcome = await self.run(kind, work, conversation=conversation)
        if not outcome.timed_out:
            return GuardedReply(outcome.value)
        try:
            return await self._recover(kind, conversation, outcome, retry, user_waiting)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.fail_open("recovery", error)
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
                return GuardedReply(recovery=recovered_reply(second.value), event="recovered")
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
            while calls and now - calls[0][0] >= self.config.repeat_window_seconds:
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

    async def review_reply(
        self,
        conversation: str,
        text: str | None,
        *,
        pivot: Callable[[str], Awaitable[str | None]] | None = None,
    ) -> Review:
        """Check a finished reply before it is sent.

        A member asking the same thing again is not a loop. When the answer would
        come out near-identical, the auditor asks once for a more explicit,
        upfront version instead of repeating it 1:1, and keeps the draft if that fails.
        """
        try:
            if not self.config.enabled or not isinstance(text, str) or not text.strip():
                return Review(text)
            if text in AUDITOR_LINES or text.startswith(STUCK_BACK_LINE):
                return Review(text)
            now = self._clock()
            self._prune(now)
            history = self._replies.setdefault(conversation, deque(maxlen=12))
            while history and now - history[0][0] >= self.config.repeat_window_seconds:
                history.popleft()
            event = None
            if pivot is not None and self._repeats_recent(history, text):
                text, event = await self._pivot(conversation, text, history, pivot, now)
            history.append((now, _normalize(text)))

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
            return Review(nudged, event or ("style" if flags else None), tuple(flags))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.fail_open("review", error)
            return Review(text)

    def _repeats_recent(self, history: deque, text: str) -> bool:
        normalized = _normalize(text)
        return any(
            _similar(seen, normalized, self.config.repeat_similarity) for _, seen in history
        )

    async def _pivot(self, conversation, text, history, pivot, now) -> tuple[str, str | None]:
        last = self._last_pivot.get(conversation)
        if last is not None and now - last < self.config.pivot_cooldown_seconds:
            return text, None
        self._last_pivot[conversation] = now
        try:
            outcome = await self.run("pivot", lambda: pivot(text), conversation=conversation)
        except Exception as error:
            self._log(f"[Auditor] pivot failed: {type(error).__name__}")
            return text, None
        rewritten = outcome.value
        if outcome.timed_out or not isinstance(rewritten, str) or not rewritten.strip():
            return text, None
        if self._repeats_recent(history, rewritten):
            return text, None
        self.record("repeat_subject", "reply")
        return rewritten.strip(), "repeat_subject"

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


# ── audit notes (posted through the /grim_updates channels) ─────────────────

DEFCON_NAMES = {
    1: "COCKED PISTOL",
    2: "FAST PACE",
    3: "ROUND HOUSE",
    4: "DOUBLE TAKE",
    5: "FADE OUT",
}
EVENT_DEFCON = {
    "auditor_error": 1,
    "recovery_failed": 2,
    "tool_loop": 2,
    "timeout": 3,
    "recovered": 3,
    "reply_chain": 3,
    "intervention_suppressed": 4,
    "repeat_subject": 4,
    "style": 5,
}
EVENT_LABELS = {
    "auditor_error": "auditor failed open",
    "recovery_failed": "stuck call, retry failed",
    "tool_loop": "tool loop blocked",
    "timeout": "stuck call cancelled",
    "recovered": "stuck call recovered",
    "reply_chain": "reply chain paused",
    "intervention_suppressed": "auditor hit its own cap",
    "repeat_subject": "repeat subject, answered more directly",
    "style": "voice slip",
}
QUICK_FIXES = {
    "auditor_error": (
        "Grim still replies without the auditor. Check `[Auditor]` lines in the VPS log; "
        "set `GRIM_AUDITOR_ENABLED=off` and restart if it keeps failing."
    ),
    "recovery_failed": (
        "xAI is stalling. Check `/status`. If it keeps up, check the provider or raise "
        "`GRIM_AUDITOR_TIMEOUT_<KIND>`."
    ),
    "tool_loop": (
        "The same search or check keeps firing. Check live search in `/status`; "
        "raise `GRIM_AUDITOR_TOOL_REPEATS` if this is normal traffic."
    ),
    "timeout": "Slow calls were cut. Raise `GRIM_AUDITOR_TIMEOUT_<KIND>` if replies are only slow.",
    "recovered": "Recovered on retry. Watch for a climb.",
    "reply_chain": "One conversation is calling fast. Raise `GRIM_AUDITOR_BURST_LIMIT` if it's normal.",
    "intervention_suppressed": "The auditor is rate-capping itself. Something upstream may be stuck.",
    "repeat_subject": "No action needed.",
    "style": "No action needed. Tune the voice prompt if one slip keeps climbing.",
}
NOTE_DELAY_SECONDS = {1: 0, 2: 0, 3: 60, 4: 900, 5: 3600}
NOTE_GAP_SECONDS = {1: 300, 2: 300, 3: 900, 4: 3600, 5: 21600}


def defcon_for(event: str) -> int:
    return EVENT_DEFCON.get(event, 1)


def build_audit_note(level: int, entries: list[dict]) -> dict:
    """Aggregate events for one DEFCON level. Only counts, kinds, and durations."""
    grouped: dict[tuple[str, str], list[dict]] = {}
    for entry in entries:
        grouped.setdefault((entry["event"], entry["kind"]), []).append(entry)
    lines = []
    for (event, kind), items in sorted(grouped.items(), key=lambda item: -len(item[1])):
        durations = [item["duration"] for item in items if "duration" in item]
        detail = f" · avg {sum(durations) / len(durations):.1f}s" if durations else ""
        lines.append(f"`x{len(items)}` {EVENT_LABELS.get(event, event)} · {kind}{detail}")
    events_seen = []
    for event, _ in sorted(grouped, key=lambda key: -len(grouped[key])):
        if event not in events_seen:
            events_seen.append(event)
    fixes = [QUICK_FIXES[event] for event in events_seen if event in QUICK_FIXES][:3]
    return {
        "level": level,
        "title": f"DEFCON {level} · {DEFCON_NAMES.get(level, '')}".strip(" ·"),
        "lines": lines[:12],
        "fixes": fixes,
        "total": len(entries),
    }


class AuditNotes:
    """Batch auditor events by DEFCON level and post them when the need arises.

    DEFCON 1 and 2 post at once, then at most every five minutes. Lower levels
    wait and roll up, so voice slips never flood the operations channel.
    """

    def __init__(
        self,
        post: Callable[[dict], Awaitable[bool]],
        *,
        delays: dict[int, float] | None = None,
        gaps: dict[int, float] | None = None,
        clock: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] = print,
    ):
        self._post = post
        self._delays = dict(NOTE_DELAY_SECONDS if delays is None else delays)
        self._gaps = dict(NOTE_GAP_SECONDS if gaps is None else gaps)
        self._clock = clock
        self._log = log
        self._pending: dict[int, list[dict]] = {}
        self._scheduled: dict[int, asyncio.Task] = {}
        self._last_posted: dict[int, float] = {}

    def add(self, entry: dict) -> None:
        cleaned = _clean_event(entry)
        if cleaned is None:
            return
        level = defcon_for(cleaned["event"])
        pending = self._pending.setdefault(level, [])
        if len(pending) < 500:
            pending.append(cleaned)
        self._schedule(level)

    def pending(self, level: int) -> list[dict]:
        return list(self._pending.get(level, []))

    def _schedule(self, level: int) -> None:
        task = self._scheduled.get(level)
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        delay = float(self._delays.get(level, 0))
        last = self._last_posted.get(level)
        if last is not None:
            delay = max(delay, float(self._gaps.get(level, 0)) - (self._clock() - last))
        self._scheduled[level] = loop.create_task(self._flush_later(level, max(0.0, delay)))

    async def _flush_later(self, level: int, delay: float) -> None:
        try:
            if delay:
                await asyncio.sleep(delay)
            await self.flush(level)
        finally:
            self._scheduled.pop(level, None)

    async def flush(self, level: int) -> bool:
        entries = self._pending.pop(level, [])
        if not entries:
            return False
        try:
            posted = bool(await self._post(build_audit_note(level, entries)))
        except Exception as error:
            self._log(f"[Auditor] audit note post failed: {type(error).__name__}")
            posted = False
        self._last_posted[level] = self._clock()
        if not posted:
            self._log(f"[Auditor] DEFCON {level} note not delivered; {len(entries)} event(s) stay saved")
        return posted


def format_events(events: list[dict], *, now: float | None = None) -> str:
    """Owner view of saved events: when, DEFCON level, type, kind, duration, count."""
    current = time.time() if now is None else now
    lines = []
    for entry in reversed(events):
        age = max(0, int(current - entry.get("at", current)))
        extra = ""
        if "duration" in entry:
            extra += f" {entry['duration']}s"
        if "count" in entry:
            extra += f" x{entry['count']}"
        level = defcon_for(entry["event"])
        lines.append(
            f"`{age // 60}m ago` D{level} {entry['event']} ({entry['kind']}){extra}"
        )
    totals = Counter(entry["event"] for entry in events)
    body = "\n".join(lines) if lines else "nothing saved yet."
    summary = ", ".join(f"{name} {count}" for name, count in sorted(totals.items()))
    return f"{body}\n\ntotals: {summary or 'none'}"[:4000]
