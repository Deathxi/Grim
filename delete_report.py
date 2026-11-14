"""Private deleted-message reports DMed only to the configured founder user.

The recipient is `FOUNDER_DISCORD_ID` (alias `GRIM_FOUNDER_ID`) in the VPS
`.env`. Grim never posts these reports to a server channel, including
`/grim_updates`. Application logs must not include deleted message content.

`/panopticon` and `/nightfall` are Founder-only CLASSIFIED commands gated
by the env founder ID (never a committed ID). State persists under `~/.grim_data`.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

import discord


FOUNDER_ID_ENV = "FOUNDER_DISCORD_ID"
FOUNDER_ID_ENV_ALIAS = "GRIM_FOUNDER_ID"
GRIM_TIMEZONE = ZoneInfo("America/Los_Angeles")
GRIM_TIMEZONE_LABEL = "PST"
MESSAGE_CACHE_LIMIT = 8000
AUDIT_MATCH_WINDOW = timedelta(seconds=90)
AUDIT_RETRY_SECONDS = 1.0
EMBED_DESCRIPTION_LIMIT = 3900

DbLookup = Callable[[int], dict | None]

PANOPTICON_REPORT_FOOTER = "Grim"
PANOPTICON_REPORT_TITLE_PREFIX = "Deleted message"
PANOPTICON_REPORT_FIELDS = ("Channel", "Deleted by")

_message_snapshots: OrderedDict[int, dict] = OrderedDict()
_invalid_founder_id_logged = False
_delete_dms_enabled = True
_delete_dms_state_path: str | None = None
_nightfall_enabled = False
_nightfall_state_path: str | None = None


def _log(message: str) -> None:
    print(message)


def reset_delete_report_state() -> None:
    global _invalid_founder_id_logged, _delete_dms_enabled, _delete_dms_state_path
    global _nightfall_enabled, _nightfall_state_path
    _message_snapshots.clear()
    _invalid_founder_id_logged = False
    _delete_dms_enabled = True
    _delete_dms_state_path = None
    _nightfall_enabled = False
    _nightfall_state_path = None


def founder_discord_id() -> int | None:
    """Return the configured founder snowflake, or None if unset/invalid."""
    global _invalid_founder_id_logged
    raw = os.environ.get(FOUNDER_ID_ENV, "").strip()
    if not raw:
        raw = os.environ.get(FOUNDER_ID_ENV_ALIAS, "").strip()
    if not raw:
        return None
    if raw.isdigit() and 17 <= len(raw) <= 20:
        value = int(raw)
        if value > 0:
            return value
    if not _invalid_founder_id_logged:
        _log("[DeleteReport] FOUNDER_DISCORD_ID is invalid; deletion DMs are disabled.")
        _invalid_founder_id_logged = True
    return None


def is_founder_user(user_id: Any) -> bool:
    """True only when user_id matches FOUNDER_DISCORD_ID / GRIM_FOUNDER_ID.

    No other role, permission, or committed ID (including CREATOR_DISCORD_ID) grants this.
    """
    founder_id = founder_discord_id()
    if founder_id is None or user_id is None:
        return False
    try:
        return int(user_id) == founder_id
    except (TypeError, ValueError):
        return False


def founder_delete_dms_enabled() -> bool:
    """Founder deletion DMs default ON; `/panopticon` toggles them off or back on."""
    return bool(_delete_dms_enabled)


def configure_founder_delete_dms_state(path: str | None) -> None:
    """Load the persisted killswitch. A missing or invalid file means ON."""
    global _delete_dms_state_path, _delete_dms_enabled
    _delete_dms_state_path = path
    _delete_dms_enabled = _load_enabled_flag(path, default=True)


def toggle_founder_delete_dms() -> bool:
    """Flip the killswitch, persist it, and return the new enabled state."""
    global _delete_dms_enabled
    _delete_dms_enabled = not bool(_delete_dms_enabled)
    _save_enabled_flag(_delete_dms_state_path, _delete_dms_enabled)
    return _delete_dms_enabled


def nightfall_enabled() -> bool:
    """True while `/nightfall` is on. Default off."""
    return bool(_nightfall_enabled)


def configure_nightfall_state(path: str | None) -> None:
    """Load the persisted nightfall switch. A missing or invalid file means OFF."""
    global _nightfall_state_path, _nightfall_enabled
    _nightfall_state_path = path
    _nightfall_enabled = _load_enabled_flag(path, default=False)


def toggle_nightfall() -> bool:
    """Flip nightfall, persist it, and return the new enabled state."""
    global _nightfall_enabled
    _nightfall_enabled = not bool(_nightfall_enabled)
    _save_enabled_flag(_nightfall_state_path, _nightfall_enabled)
    return _nightfall_enabled


def founder_reports_should_send() -> bool:
    """True only when Founder report delivery is allowed."""
    return (
        founder_discord_id() is not None
        and founder_delete_dms_enabled()
        and not nightfall_enabled()
    )


def _load_enabled_flag(path: str | None, *, default: bool) -> bool:
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, "r") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and "enabled" in data:
            return bool(data["enabled"])
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    return default


def _save_enabled_flag(path: str | None, enabled: bool) -> None:
    if not path:
        return
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
        dir=directory,
        text=True,
    )
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump({"enabled": bool(enabled)}, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)


def log_founder_delete_report_status() -> None:
    if founder_discord_id() is not None:
        if nightfall_enabled():
            _log("[DeleteReport] Nightfall is on.")
        elif founder_delete_dms_enabled():
            _log("[DeleteReport] Founder deletion DMs are enabled.")
        else:
            _log("[DeleteReport] Founder deletion DMs are off (Founder killswitch).")
        return
    raw = (
        os.environ.get(FOUNDER_ID_ENV, "").strip()
        or os.environ.get(FOUNDER_ID_ENV_ALIAS, "").strip()
    )
    if raw:
        return
    _log("[DeleteReport] FOUNDER_DISCORD_ID is unset; deletion DMs are disabled.")


def _trim_cache() -> None:
    while len(_message_snapshots) > MESSAGE_CACHE_LIMIT:
        _message_snapshots.popitem(last=False)


def snapshot_from_message(message: Any) -> dict:
    author = getattr(message, "author", None)
    created_at = getattr(message, "created_at", None)
    content = str(getattr(message, "content", "") or "")
    attachments = [
        str(getattr(attachment, "filename", "attachment"))
        for attachment in getattr(message, "attachments", []) or []
    ]
    stickers = [
        str(getattr(sticker, "name", "sticker"))
        for sticker in getattr(message, "stickers", []) or []
    ]
    guild = getattr(message, "guild", None)
    channel = getattr(message, "channel", None)
    return {
        "message_id": getattr(message, "id", None),
        "guild_id": getattr(guild, "id", None),
        "channel_id": getattr(channel, "id", None),
        "author_id": getattr(author, "id", None),
        "author_name": (
            getattr(author, "display_name", None)
            or getattr(author, "name", None)
        ),
        "content": content,
        "created_at": created_at,
        "attachments": attachments,
        "stickers": stickers,
    }


def remember_guild_message(message: Any) -> None:
    guild = getattr(message, "guild", None)
    message_id = getattr(message, "id", None)
    if guild is None or message_id is None:
        return
    _message_snapshots[int(message_id)] = snapshot_from_message(message)
    _message_snapshots.move_to_end(int(message_id))
    _trim_cache()


def take_seen_message(message_id: int, cached_message: Any = None) -> dict | None:
    snapshot = _message_snapshots.pop(int(message_id), None)
    if snapshot is not None:
        return snapshot
    if cached_message is not None:
        return snapshot_from_message(cached_message)
    return None


def _posted_label(created_at: Any) -> str | None:
    if not isinstance(created_at, datetime):
        return None
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    local = created_at.astimezone(GRIM_TIMEZONE)
    return f"{local.strftime('%A, %B %d, %Y — %I:%M %p')} {GRIM_TIMEZONE_LABEL}"


def _escape_body(text: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text))


def format_author_label(snapshot: Mapping[str, Any]) -> str:
    author_id = snapshot.get("author_id")
    author_name = snapshot.get("author_name")
    if author_id and author_name:
        return f"{author_name} (<@{author_id}>)"
    if author_id:
        return f"<@{author_id}>"
    if author_name:
        return str(author_name)
    return "unknown author"


def format_actor_label(actor: Mapping[str, Any] | None) -> str:
    if not actor:
        return "actor unknown"
    actor_id = actor.get("id")
    actor_name = actor.get("name")
    if actor_id and actor_name:
        return f"{actor_name} (<@{actor_id}>)"
    if actor_id:
        return f"<@{actor_id}>"
    if actor_name:
        return str(actor_name)
    return "actor unknown"


def format_content_block(snapshot: Mapping[str, Any]) -> str:
    if not snapshot:
        return "content unknown"
    extras = [
        *(f"[attachment: {name}]" for name in snapshot.get("attachments") or []),
        *(f"[sticker: {name}]" for name in snapshot.get("stickers") or []),
    ]
    if "content" not in snapshot or snapshot.get("content") is None:
        if extras:
            return _escape_body("\n".join(extras))
        return "content unknown"
    raw = str(snapshot.get("content") or "")
    if raw.strip() or extras:
        body = "\n".join(filter(None, [raw, *extras]))
        return _escape_body(body)
    return "[no text content]"


def channel_label(bot: Any, payload: Any, cached_message: Any = None) -> str:
    channel = getattr(cached_message, "channel", None) if cached_message is not None else None
    if channel is None:
        getter = getattr(bot, "get_channel", None)
        channel = getter(payload.channel_id) if getter else None
    guild = getattr(channel, "guild", None) if channel is not None else None
    if guild is None:
        guild_id = getattr(payload, "guild_id", None)
        get_guild = getattr(bot, "get_guild", None)
        if guild_id and get_guild:
            guild = get_guild(guild_id)
    guild_name = getattr(guild, "name", None) or "unknown server"
    if channel is None:
        return f"unknown channel (`{payload.channel_id}`) in {guild_name}"
    mention = getattr(channel, "mention", None) or f"<#{getattr(channel, 'id', payload.channel_id)}>"
    return f"{mention} in {guild_name}"


def build_delete_report_embeds(
    *,
    channel_text: str,
    actor_label: str,
    snapshots: list[dict],
) -> list[discord.Embed]:
    reports = snapshots or [
        {
            "message_id": None,
            "author_id": None,
            "author_name": None,
            "content": None,
            "created_at": None,
            "attachments": [],
            "stickers": [],
        }
    ]
    count = len(reports)
    title = "Deleted message" if count == 1 else f"Deleted messages ({count})"
    actor_text = actor_label or "actor unknown"

    blocks = []
    for index, snapshot in enumerate(reports, start=1):
        header = format_author_label(snapshot)
        posted = _posted_label(snapshot.get("created_at"))
        if posted:
            header = f"{header} · {posted}"
        message_id = snapshot.get("message_id")
        if message_id is not None:
            header = f"{header} · `{message_id}`"
        body = format_content_block(snapshot)
        if count == 1:
            blocks.append(f"{header}\n{body}")
        else:
            blocks.append(f"**{index}. {header}**\n{body}")

    embeds: list[discord.Embed] = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}".strip() if current else block
        if len(candidate) <= EMBED_DESCRIPTION_LIMIT:
            current = candidate
            continue
        if current:
            embeds.append(_report_embed(title, current, channel_text, actor_text, continued=bool(embeds)))
        if len(block) <= EMBED_DESCRIPTION_LIMIT:
            current = block
            continue
        for start in range(0, len(block), EMBED_DESCRIPTION_LIMIT):
            piece = block[start:start + EMBED_DESCRIPTION_LIMIT]
            if start == 0 and not embeds and not current:
                current = piece
            else:
                embeds.append(
                    _report_embed(title, piece, channel_text, actor_text, continued=True)
                )
                current = ""
    if current:
        embeds.append(_report_embed(title, current, channel_text, actor_text, continued=bool(embeds)))
    return embeds


def _report_embed(
    title: str,
    description: str,
    channel_text: str,
    actor_text: str,
    *,
    continued: bool,
) -> discord.Embed:
    embed = discord.Embed(
        title=title if not continued else f"{title} continued",
        description=description,
        color=discord.Color.from_rgb(18, 18, 18),
    )
    embed.add_field(name="Channel", value=channel_text, inline=False)
    embed.add_field(name="Deleted by", value=actor_text, inline=False)
    embed.set_footer(text=PANOPTICON_REPORT_FOOTER)
    return embed


def is_panopticon_report_embed(embed: Any) -> bool:
    """True for matching report embeds."""
    title = str(getattr(embed, "title", None) or "")
    if not title.startswith(PANOPTICON_REPORT_TITLE_PREFIX):
        return False
    fields = list(getattr(embed, "fields", None) or [])
    names = {getattr(field, "name", None) for field in fields}
    if any(name not in names for name in PANOPTICON_REPORT_FIELDS):
        return False
    footer = getattr(embed, "footer", None)
    footer_text = getattr(footer, "text", None) if footer is not None else None
    return footer_text == PANOPTICON_REPORT_FOOTER


def is_grim_panopticon_dm(message: Any, grim_id: Any) -> bool:
    if grim_id is None:
        return False
    author_id = getattr(getattr(message, "author", None), "id", None)
    try:
        if author_id is None or int(author_id) != int(grim_id):
            return False
    except (TypeError, ValueError):
        return False
    embeds = list(getattr(message, "embeds", None) or [])
    return any(is_panopticon_report_embed(embed) for embed in embeds)


async def _resolve_founder_user(bot: Any) -> Any:
    founder_id = founder_discord_id()
    if founder_id is None:
        return None
    getter = getattr(bot, "get_user", None)
    user = getter(founder_id) if getter else None
    if user is not None:
        return user
    fetch = getattr(bot, "fetch_user", None)
    if fetch is None:
        _log("[DeleteReport] Could not resolve founder user.")
        return None
    try:
        return await fetch(founder_id)
    except (discord.NotFound, discord.HTTPException, AttributeError) as error:
        _log(f"[DeleteReport] Could not resolve founder user ({type(error).__name__}).")
        return None


async def wipe_founder_panopticon_dms(bot: Any) -> int:
    """Remove matching Grim DMs in the Founder DM channel."""
    user = await _resolve_founder_user(bot)
    if user is None:
        return 0
    channel = getattr(user, "dm_channel", None)
    if channel is None:
        create = getattr(user, "create_dm", None)
        if create is None:
            _log("[DeleteReport] Nightfall could not open the channel.")
            return 0
        try:
            channel = await create()
        except (discord.Forbidden, discord.HTTPException, AttributeError) as error:
            _log(
                f"[DeleteReport] Nightfall could not open the channel ({type(error).__name__})."
            )
            return 0
    if channel is None or not hasattr(channel, "history"):
        _log("[DeleteReport] Nightfall could not open the channel.")
        return 0
    grim_id = getattr(getattr(bot, "user", None), "id", None)
    removed = 0
    try:
        async for message in channel.history(limit=None):
            if not is_grim_panopticon_dm(message, grim_id):
                continue
            delete = getattr(message, "delete", None)
            if delete is None:
                continue
            try:
                await delete()
                removed += 1
            except (discord.Forbidden, discord.HTTPException, AttributeError):
                _log("[DeleteReport] Nightfall could not remove a message.")
    except (discord.Forbidden, discord.HTTPException, AttributeError) as error:
        _log(f"[DeleteReport] Nightfall wipe failed ({type(error).__name__}).")
    if removed:
        _log(f"[DeleteReport] Nightfall removed {removed} message(s).")
    return removed


def _audit_channel_id(extra: Any) -> int | None:
    channel = getattr(extra, "channel", extra)
    channel_id = getattr(channel, "id", None)
    if channel_id is not None:
        return int(channel_id)
    extra_id = getattr(extra, "channel_id", None)
    if extra_id is not None:
        return int(extra_id)
    if isinstance(extra, Mapping) and extra.get("channel_id") is not None:
        return int(extra["channel_id"])
    return None


def _audit_target_id(entry: Any) -> int | None:
    target = getattr(entry, "target", None)
    target_id = getattr(target, "id", None)
    if target_id is None:
        return None
    return int(target_id)


def _actor_from_entry(entry: Any) -> dict | None:
    user = getattr(entry, "user", None)
    if user is None:
        return None
    user_id = getattr(user, "id", None)
    if user_id is None:
        return None
    return {
        "id": int(user_id),
        "name": getattr(user, "display_name", None) or getattr(user, "name", None),
    }


async def lookup_delete_actor(
    guild: Any,
    channel_id: int,
    author_id: int | None,
    *,
    bulk: bool = False,
) -> dict | None:
    if guild is None or not hasattr(guild, "audit_logs"):
        return None
    action = (
        discord.AuditLogAction.message_bulk_delete
        if bulk
        else discord.AuditLogAction.message_delete
    )
    cutoff = datetime.now(timezone.utc) - AUDIT_MATCH_WINDOW
    try:
        async for entry in guild.audit_logs(limit=8, action=action):
            created_at = getattr(entry, "created_at", None)
            if isinstance(created_at, datetime):
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                if created_at < cutoff:
                    continue
            extra_channel_id = _audit_channel_id(getattr(entry, "extra", None))
            if extra_channel_id is not None and extra_channel_id != int(channel_id):
                continue
            if not bulk and author_id is not None:
                target_id = _audit_target_id(entry)
                if target_id is not None and target_id != int(author_id):
                    continue
            actor = _actor_from_entry(entry)
            if actor is not None:
                return actor
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        return None
    return None


async def resolve_delete_actor(
    guild: Any,
    channel_id: int,
    author_id: int | None,
    *,
    bulk: bool = False,
) -> dict | None:
    actor = await lookup_delete_actor(guild, channel_id, author_id, bulk=bulk)
    if actor is not None:
        return actor
    try:
        await asyncio.sleep(AUDIT_RETRY_SECONDS)
    except Exception:
        return None
    return await lookup_delete_actor(guild, channel_id, author_id, bulk=bulk)


async def send_founder_delete_report(bot: Any, embeds: list[discord.Embed]) -> None:
    if not founder_reports_should_send() or not embeds:
        return
    user = await _resolve_founder_user(bot)
    if user is None:
        return
    send = getattr(user, "send", None)
    if send is None:
        _log("[DeleteReport] Founder DM failed; no channel fallback.")
        return
    try:
        for embed in embeds:
            await send(embed=embed)
    except discord.Forbidden:
        _log("[DeleteReport] Founder DM failed; no channel fallback.")
    except discord.HTTPException as error:
        _log(
            f"[DeleteReport] Founder DM failed ({type(error).__name__}); no channel fallback."
        )


def _guild_from_payload(bot: Any, payload: Any, cached_message: Any = None) -> Any:
    guild_id = getattr(payload, "guild_id", None)
    if guild_id:
        getter = getattr(bot, "get_guild", None)
        guild = getter(guild_id) if getter else None
        if guild is not None:
            return guild
    if cached_message is not None:
        return getattr(cached_message, "guild", None)
    return None


async def handle_raw_message_delete(
    bot: Any,
    payload: Any,
    db_lookup: DbLookup | None = None,
) -> None:
    if getattr(payload, "guild_id", None) is None:
        return
    if not founder_reports_should_send():
        return
    cached = getattr(payload, "cached_message", None)
    snapshot = take_seen_message(payload.message_id, cached)
    if snapshot is None and db_lookup is not None:
        snapshot = db_lookup(int(payload.message_id))
    if snapshot is None:
        snapshot = {
            "message_id": payload.message_id,
            "guild_id": payload.guild_id,
            "channel_id": payload.channel_id,
            "author_id": None,
            "author_name": None,
            "content": None,
            "created_at": None,
            "attachments": [],
            "stickers": [],
        }
    guild = _guild_from_payload(bot, payload, cached)
    actor = await resolve_delete_actor(
        guild,
        payload.channel_id,
        snapshot.get("author_id"),
        bulk=False,
    )
    embeds = build_delete_report_embeds(
        channel_text=channel_label(bot, payload, cached),
        actor_label=format_actor_label(actor),
        snapshots=[snapshot],
    )
    await send_founder_delete_report(bot, embeds)


async def handle_raw_bulk_message_delete(
    bot: Any,
    payload: Any,
    db_lookup: DbLookup | None = None,
) -> None:
    if getattr(payload, "guild_id", None) is None:
        return
    if not founder_reports_should_send():
        return
    cached_messages = list(getattr(payload, "cached_messages", None) or [])
    cached_by_id = {
        int(getattr(message, "id")): message
        for message in cached_messages
        if getattr(message, "id", None) is not None
    }
    snapshots = []
    for message_id in getattr(payload, "message_ids", None) or cached_by_id:
        snapshot = take_seen_message(int(message_id), cached_by_id.get(int(message_id)))
        if snapshot is None and db_lookup is not None:
            snapshot = db_lookup(int(message_id))
        if snapshot is None:
            snapshot = {
                "message_id": int(message_id),
                "guild_id": payload.guild_id,
                "channel_id": payload.channel_id,
                "author_id": None,
                "author_name": None,
                "content": None,
                "created_at": None,
                "attachments": [],
                "stickers": [],
            }
        snapshots.append(snapshot)
    if not snapshots:
        return
    cached = cached_messages[0] if cached_messages else None
    guild = _guild_from_payload(bot, payload, cached)
    author_ids = {snap.get("author_id") for snap in snapshots if snap.get("author_id") is not None}
    author_id = next(iter(author_ids)) if len(author_ids) == 1 else None
    actor = await resolve_delete_actor(
        guild,
        payload.channel_id,
        author_id,
        bulk=True,
    )
    embeds = build_delete_report_embeds(
        channel_text=channel_label(bot, payload, cached),
        actor_label=format_actor_label(actor),
        snapshots=snapshots,
    )
    await send_founder_delete_report(bot, embeds)
