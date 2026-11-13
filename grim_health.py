"""Grim's standing health dashboard: one DEFCON level for security and mechanical function.

DEFCON 1 is the worst, DEFCON 5 is healthy. The level is scored from counts of
auditor events (stuck calls, loops, failed recoveries, auditor fail-open) and of
security events (failed searches, image-strip failures, permission denials, rate
limits, command failures) inside a rolling window. Discord connectivity is not
part of the score; outages have their own after-report.

OpSec: this module only ever sees event types, kinds, counts, and timestamps.
No message text, user names, user ids, or channel contents.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

import discord

DEFCON_NAMES = {
    1: "COCKED PISTOL",
    2: "FAST PACE",
    3: "ROUND HOUSE",
    4: "DOUBLE TAKE",
    5: "FADE OUT",
}

DEFAULT_WINDOW_HOURS = 6.0
HEALTHY_LINE = "All checks healthy. Security and mechanical function are normal."
SCOPE_LINE = "Scored on security and mechanical events only. Connectivity is reported separately."

SEARCH_FAILURES = frozenset({
    "failed", "timeout", "provider_error", "unavailable", "parse_error",
    "invalid_request", "unverified", "loop_blocked",
})
COMMAND_FAILURES = frozenset({"failed", "post_failed", "request_failed"})


@dataclass(frozen=True)
class CheckSpec:
    """thresholds are (count, level) pairs: the first one the count reaches wins."""

    key: str
    label: str
    thresholds: tuple[tuple[int, int], ...]
    fix: str


CHECKS: tuple[CheckSpec, ...] = (
    CheckSpec(
        "recovery", "Failed recoveries", ((5, 1), (2, 2), (1, 3)),
        "xAI is stalling and the retry fails too. Check `/status` and the provider; "
        "raise `GRIM_AUDITOR_TIMEOUT_<KIND>` only if replies are merely slow.",
    ),
    CheckSpec(
        "auditor", "Auditor", ((5, 1), (2, 2), (1, 3)),
        "The auditor failed open; Grim still replies. Look for `[Auditor]` lines in the "
        "VPS log, and set `GRIM_AUDITOR_ENABLED=off` and restart if it keeps failing.",
    ),
    CheckSpec(
        "strip", "Image cleaning", ((3, 1), (1, 2)),
        "The metadata cleaner is failing, so Grim refuses those images. Check "
        "`[Relay] metadata strip failed` in the VPS log and the Pillow install.",
    ),
    CheckSpec(
        "loops", "Loops", ((6, 2), (3, 3), (1, 4)),
        "The same search or check keeps firing, or one conversation calls too fast. "
        "Raise `GRIM_AUDITOR_TOOL_REPEATS` or `GRIM_AUDITOR_BURST_LIMIT` if it is normal traffic.",
    ),
    CheckSpec(
        "stuck", "Stuck calls", ((10, 2), (4, 3), (1, 4)),
        "Slow calls were cancelled and recovered. Check `/status`; "
        "raise `GRIM_AUDITOR_TIMEOUT_<KIND>` if replies are only slow.",
    ),
    CheckSpec(
        "search", "Live search", ((10, 2), (4, 3), (1, 4)),
        "Live searches are failing or coming back unverified. Check `/status` and "
        "`XAI_SEARCH_MODEL`; Grim says plainly when a check fails.",
    ),
    CheckSpec(
        "commands", "Command failures", ((10, 2), (4, 3), (1, 4)),
        "Commands are failing after they start. Check the VPS log for the failing feature.",
    ),
    CheckSpec(
        "denials", "Permission denials", ((30, 2), (12, 3), (4, 4)),
        "Many denied admin actions. Check the security audit log for one repeated source.",
    ),
    CheckSpec(
        "rate", "Rate limits", ((40, 3), (15, 4)),
        "Members keep hitting rate limits. Raise the limits only if the traffic is real.",
    ),
    CheckSpec(
        "caps", "Auditor limits", ((5, 3), (1, 4)),
        "The auditor is rate-capping its own interventions. Something upstream may be stuck.",
    ),
)

# Event types counted per check, and informational counts that never lower the level.
AUDITOR_COUNTS = {
    "recovery": ("recovery_failed",),
    "auditor": ("auditor_error",),
    "loops": ("tool_loop", "reply_chain"),
    "stuck": ("timeout",),
    "caps": ("intervention_suppressed",),
}
INFO_COUNTS = (
    ("recovered", "recovered", "recovered"),
    ("repeat_subject", "repeat_subject", "repeat subject"),
    ("style", "style", "voice slips"),
)
EVENT_LABELS = {
    "recovery_failed": "stuck call, retry failed",
    "auditor_error": "auditor failed open",
    "tool_loop": "tool loop blocked",
    "reply_chain": "reply chain paused",
    "timeout": "stuck call cancelled",
    "recovered": "stuck call recovered",
    "intervention_suppressed": "auditor hit its own cap",
    "repeat_subject": "repeat subject answered directly",
    "style": "voice slip",
}
SECURITY_LABELS = {
    "denied": "permission denial",
    "rate_limited": "rate limit",
    "command_failed": "command failure",
    "search_failed": "live search failure",
    "strip_failed": "image strip failure",
}
SECURITY_CHECKS = {
    "strip": "strip_failed",
    "search": "search_failed",
    "commands": "command_failed",
    "denials": "denied",
    "rate": "rate_limited",
}


def health_window_seconds() -> float:
    raw = os.environ.get("GRIM_HEALTH_WINDOW_HOURS", "")
    try:
        hours = float(raw) if raw.strip() else DEFAULT_WINDOW_HOURS
    except ValueError:
        hours = DEFAULT_WINDOW_HOURS
    if hours != hours or not 1 <= hours <= 72:
        hours = DEFAULT_WINDOW_HOURS
    return hours * 3600


def classify_security_rows(rows: Iterable[tuple]) -> dict[str, tuple[int, float]]:
    """(action, outcome, count, last_at) rows -> {category: (count, last_at)}."""
    totals: dict[str, tuple[int, float]] = {}
    for row in rows:
        try:
            action, outcome, count, last_at = row
            action, outcome = str(action), str(outcome)
            count, last_at = int(count), float(last_at or 0)
        except (TypeError, ValueError):
            continue
        if action == "image_strip" and outcome == "failed":
            category = "strip_failed"
        elif action == "ai_web_research":
            if outcome == "rate_limited":
                category = "rate_limited"
            elif outcome in SEARCH_FAILURES:
                category = "search_failed"
            else:
                continue
        elif outcome == "denied":
            category = "denied"
        elif outcome == "rate_limited":
            category = "rate_limited"
        elif outcome in COMMAND_FAILURES:
            category = "command_failed"
        else:
            continue
        seen, latest = totals.get(category, (0, 0.0))
        totals[category] = (seen + count, max(latest, last_at))
    return totals


@dataclass(frozen=True)
class CheckResult:
    key: str
    label: str
    level: int
    count: int
    last_at: float
    fix: str

    @property
    def degraded(self) -> bool:
        return self.level < 5


@dataclass(frozen=True)
class Health:
    level: int
    checks: tuple[CheckResult, ...]
    info: tuple[tuple[str, int], ...]
    last_event: tuple[str, float] | None
    window_hours: float

    @property
    def degraded(self) -> list[CheckResult]:
        return sorted((c for c in self.checks if c.degraded), key=lambda c: (c.level, -c.count))

    @property
    def healthy(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.degraded]

    @property
    def signature(self) -> str:
        parts = [f"L{self.level}"]
        parts += [f"{c.key}:{c.level}:{c.count}" for c in self.checks if c.degraded]
        return "|".join(parts)


def _level_for(count: int, thresholds: tuple[tuple[int, int], ...]) -> int:
    for needed, level in sorted(thresholds, reverse=True):
        if count >= needed:
            return level
    return 5


def score_health(
    events: Iterable[dict],
    security: dict[str, tuple[int, float]] | None = None,
    *,
    now: float | None = None,
    window_seconds: float | None = None,
) -> Health:
    """Overall level is the worst check level. No degraded checks means DEFCON 5."""
    current = time.time() if now is None else now
    window = health_window_seconds() if window_seconds is None else window_seconds
    recent = [e for e in events if current - float(e.get("at", 0)) <= window]
    by_type: dict[str, list[float]] = {}
    for entry in recent:
        by_type.setdefault(entry["event"], []).append(float(entry.get("at", 0)))
    security = security or {}

    results = []
    for spec in CHECKS:
        if spec.key in AUDITOR_COUNTS:
            stamps = [t for event in AUDITOR_COUNTS[spec.key] for t in by_type.get(event, [])]
            count, last_at = len(stamps), max(stamps, default=0.0)
        else:
            count, last_at = security.get(SECURITY_CHECKS[spec.key], (0, 0.0))
        results.append(CheckResult(
            spec.key, spec.label, _level_for(count, spec.thresholds), count, last_at, spec.fix
        ))

    info = tuple(
        (label, len(by_type.get(event, [])))
        for _, event, label in INFO_COUNTS
        if by_type.get(event)
    )
    latest: tuple[str, float] | None = None
    for entry in recent:
        stamp = float(entry.get("at", 0))
        if latest is None or stamp > latest[1]:
            latest = (f"{EVENT_LABELS.get(entry['event'], entry['event'])} · {entry['kind']}", stamp)
    for category, (count, stamp) in security.items():
        if count and stamp and (latest is None or stamp > latest[1]):
            latest = (SECURITY_LABELS.get(category, category), stamp)
    level = min((r.level for r in results), default=5)
    return Health(level, tuple(results), info, latest, window / 3600)


def _stamp(epoch: float) -> str:
    return f"<t:{int(epoch)}:R>" if epoch else ""


def build_health_embed(
    health: Health,
    footer: str,
    *,
    recent: list[str] | None = None,
) -> discord.Embed:
    """The Seclude box: monochrome, compact, titled with the DEFCON level."""
    degraded = health.degraded
    if degraded:
        description = (
            f"{len(degraded)} of {len(health.checks)} checks degraded. "
            f"Worst: {degraded[0].label}.\n{SCOPE_LINE}"
        )
    else:
        description = f"{HEALTHY_LINE}\n{SCOPE_LINE}"
    embed = discord.Embed(
        title=f"Grim · Health · DEFCON {health.level} · {DEFCON_NAMES[health.level]}",
        description=description,
        color=discord.Color.from_rgb(18, 18, 18),
    )
    embed.add_field(
        name="Healthy",
        value=" · ".join(c.label for c in health.healthy) or "none",
        inline=False,
    )
    embed.add_field(
        name="Degraded",
        value="\n".join(
            f"`D{c.level}` {c.label} · `x{c.count}` {_stamp(c.last_at)}".rstrip()
            for c in degraded
        ) or "none",
        inline=False,
    )
    counts = [f"{c.label.lower()} {c.count}" for c in health.checks if c.count]
    counts += [f"{label} {count}" for label, count in health.info]
    embed.add_field(
        name=f"Counts · last {health.window_hours:g}h",
        value=" · ".join(counts)[:1000] or "none",
        inline=False,
    )
    last = health.last_event
    embed.add_field(
        name="Last Event",
        value=f"{last[0]} {_stamp(last[1])}".strip() if last else "none in this window",
        inline=False,
    )
    embed.add_field(
        name="Quick Fix",
        value="\n".join(c.fix for c in degraded[:2])[:1000] or "None needed.",
        inline=False,
    )
    if recent:
        embed.add_field(name="Recent", value="\n".join(recent)[:1000], inline=False)
    embed.set_footer(text=footer)
    return embed


# ── the standing board ───────────────────────────────────────────────────────

GAP_SECONDS = {"urgent": 60.0, "elevated": 300.0, "routine": 900.0}
DEBOUNCE_SECONDS = 5.0
REPOST_COOLDOWN_SECONDS = 1800.0


def _bucket(level: int) -> str:
    return "urgent" if level <= 2 else "elevated" if level == 3 else "routine"


class HealthBoard:
    """Keep one current board per updates channel.

    Edits the last board message in place. A drop to DEFCON 1 or 2 from a calmer
    level posts a fresh message and removes the old one, so the channel notifies
    once and still shows a single board. Changes closer together than the gap for
    their urgency wait for it instead of piling up.
    """

    def __init__(
        self,
        compute: Callable[[], Awaitable[Health]],
        deliver: Callable[[Health, dict[str, str], bool], Awaitable[dict[str, str] | None]],
        *,
        load: Callable[[], Any] | None = None,
        save: Callable[[dict], None] | None = None,
        gaps: dict[str, float] | None = None,
        debounce: float = DEBOUNCE_SECONDS,
        repost_cooldown: float = REPOST_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] = print,
    ):
        self._compute = compute
        self._deliver = deliver
        self._save = save
        self._gaps = dict(GAP_SECONDS if gaps is None else gaps)
        self._debounce = debounce
        self._repost_cooldown = repost_cooldown
        self._clock = clock
        self._log = log
        self._pending: asyncio.Task | None = None
        self._last_delivery: float | None = None
        self._last_repost: float | None = None
        self._lock = asyncio.Lock()
        self.state = self._read(load)

    @staticmethod
    def _read(load) -> dict:
        try:
            raw = load() if load else None
        except Exception:
            raw = None
        raw = raw if isinstance(raw, dict) else {}
        messages = raw.get("messages")
        return {
            "signature": str(raw.get("signature") or ""),
            "level": raw.get("level") if isinstance(raw.get("level"), int) else None,
            "messages": {
                str(k): str(v) for k, v in (messages.items() if isinstance(messages, dict) else [])
            },
        }

    def request_refresh(self, delay: float | None = None) -> None:
        """Ask for a refresh soon. Safe from any thread-less sync code; a no-op without a loop."""
        if self._pending is not None and not self._pending.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        wait = self._debounce if delay is None else max(0.0, delay)
        self._pending = loop.create_task(self._later(wait))

    async def _later(self, wait: float) -> None:
        if wait:
            await asyncio.sleep(wait)
        self._pending = None
        await self.refresh()

    async def refresh(self, *, force: bool = False) -> bool:
        """Recompute health and update the board if it changed. Never raises."""
        try:
            async with self._lock:
                return await self._refresh(force)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._log(f"[Health] refresh failed open: {type(error).__name__}")
            return False

    async def _refresh(self, force: bool) -> bool:
        health = await self._compute()
        if not force and health.signature == self.state["signature"]:
            return False
        now = self._clock()
        gap = self._gaps[_bucket(health.level)]
        if not force and self._last_delivery is not None:
            remaining = gap - (now - self._last_delivery)
            if remaining > 0:
                self.request_refresh(remaining)
                return False
        previous = self.state["level"]
        escalated = (
            previous is not None
            and previous > 2
            and health.level <= 2
            and (self._last_repost is None or now - self._last_repost >= self._repost_cooldown)
        )
        messages = await self._deliver(health, dict(self.state["messages"]), escalated)
        if messages is None:
            self._log("[Health] no updates channel took the board; will retry")
            return False
        self._last_delivery = now
        if escalated:
            self._last_repost = now
        self.state = {
            "signature": health.signature,
            "level": health.level,
            "messages": {str(k): str(v) for k, v in messages.items()},
        }
        if self._save is not None:
            try:
                self._save(dict(self.state))
            except Exception as error:
                self._log(f"[Health] could not save board state: {type(error).__name__}")
        return True
