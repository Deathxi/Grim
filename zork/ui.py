"""Discord /zork. The game itself never calls xAI."""

from __future__ import annotations

import asyncio

import discord
from discord import ui

from zork.play import (
    GAMES,
    bind_thread,
    command_session,
    lookup_thread,
    open_session,
    restart_session,
)

_config: dict = {}
_LOCK = asyncio.Lock()
ACCENT = discord.Color.from_rgb(176, 186, 194)


class ZorkView(ui.View):
    """Persistent buttons. The thread binding says whose game this is."""

    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        binding = lookup_thread(_config.get("root"), interaction.channel_id)
        if not binding:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "This thread isn't a live game.", ephemeral=True
                )
            return False
        if str(interaction.user.id) != str(binding.get("user_id")):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "This game belongs to someone else.", ephemeral=True
                )
            return False
        return True

    @ui.button(label="Look", style=discord.ButtonStyle.secondary, custom_id="grim:zork:look")
    async def look(self, interaction: discord.Interaction, button: ui.Button):
        await self._act(interaction, "look")

    @ui.button(
        label="Inventory",
        style=discord.ButtonStyle.secondary,
        custom_id="grim:zork:inventory",
    )
    async def inventory(self, interaction: discord.Interaction, button: ui.Button):
        await self._act(interaction, "inventory")

    @ui.button(label="Restart", style=discord.ButtonStyle.danger, custom_id="grim:zork:restart")
    async def restart(self, interaction: discord.Interaction, button: ui.Button):
        await self._act(interaction, "restart")

    async def _act(self, interaction: discord.Interaction, command: str) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer()
        binding = lookup_thread(_config.get("root"), interaction.channel_id)
        if not binding or interaction.channel is None:
            await interaction.followup.send("This thread isn't a live game.", ephemeral=True)
            return
        await _apply(
            interaction.channel,
            binding,
            command,
            restart=command == "restart",
            message=interaction.message,
        )


def install_zork(bot, *, data_root, footer, allow_open, allow_move) -> None:
    _config["root"] = data_root
    _config["footer"] = footer
    _config["allow_open"] = allow_open
    _config["allow_move"] = allow_move

    @bot.tree.command(name="zork", description="Play Zork I, II, or III in a private thread")
    @discord.app_commands.guild_only()
    @discord.app_commands.describe(game="Zork I, II, or III")
    @discord.app_commands.choices(
        game=[
            discord.app_commands.Choice(name="Zork I", value="zork1"),
            discord.app_commands.Choice(name="Zork II", value="zork2"),
            discord.app_commands.Choice(name="Zork III", value="zork3"),
        ]
    )
    async def zork(interaction: discord.Interaction, game: str):
        await _command(interaction, game)


def register_zork_view(bot) -> None:
    if getattr(bot, "_zork_view_added", False):
        return
    bot.add_view(ZorkView())
    bot._zork_view_added = True


def embed_for(turn) -> discord.Embed:
    footer = _config.get("footer")
    credit = footer() if callable(footer) else (footer or "")
    embed = discord.Embed(
        title=(turn.title or "ZORK")[:256],
        description=(turn.description or " ")[:4096],
        color=ACCENT,
    )
    if credit:
        embed.set_footer(text=str(credit)[:2048])
    return embed


async def handle_zork_message(message) -> bool:
    """Consume a thread message so Grim never sends it to xAI. True if this thread is a game."""
    root = _config.get("root")
    if not root:
        return False
    try:
        binding = lookup_thread(root, message.channel.id)
    except Exception as exc:
        print(f"[Zork] thread lookup failed: {type(exc).__name__}")
        return False
    if not binding:
        return False
    if getattr(message.author, "bot", False):
        return True
    if str(message.author.id) != str(binding.get("user_id")):
        return True
    text = " ".join((message.content or "").split())
    if not text or text.startswith("!"):
        return True
    allow_move = _config.get("allow_move")
    guild_id = str(message.guild.id) if getattr(message, "guild", None) else "dm"
    if allow_move and not allow_move(guild_id, message.author.id):
        return True
    if len(text) > 180:
        text = text[:180]
    try:
        await message.delete()
    except (discord.Forbidden, discord.HTTPException):
        pass
    await _apply(message.channel, binding, text)
    return True


async def _command(interaction: discord.Interaction, game: str) -> None:
    if game not in GAMES:
        await interaction.response.send_message("Pick Zork I, II, or III.", ephemeral=True)
        return
    channel = interaction.channel
    if interaction.guild is None or channel is None:
        await interaction.response.send_message("Open /zork in a server channel.", ephemeral=True)
        return
    if isinstance(channel, discord.Thread) or getattr(channel, "type", None) not in (
        discord.ChannelType.text,
        discord.ChannelType.news,
    ):
        await interaction.response.send_message("Open /zork from a text channel.", ephemeral=True)
        return
    allow_open = _config.get("allow_open")
    if allow_open and not await allow_open(interaction, "zork"):
        return

    await interaction.response.defer(ephemeral=True)
    title = GAMES[game].title
    who = getattr(interaction.user, "display_name", None) or interaction.user.name
    name = f"{title} · {' '.join(str(who).replace(chr(10), ' ').split()) or 'player'}"[:100]
    try:
        thread = await _open_thread(channel, name)
    except discord.HTTPException as exc:
        print(f"[Zork] thread failed: {type(exc).__name__}")
        await interaction.followup.send(
            "I couldn't open a private thread in this channel.", ephemeral=True
        )
        return

    added = True
    try:
        await thread.add_user(interaction.user)
    except discord.HTTPException:
        added = False

    try:
        async with _LOCK:
            turn = await asyncio.to_thread(open_session, _config["root"], interaction.user.id, game)
            panel = await thread.send(
                embed=embed_for(turn),
                view=ZorkView(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            bind_thread(
                _config["root"],
                thread.id,
                interaction.user.id,
                game,
                panel.id,
                interaction.guild.id,
            )
    except Exception as exc:
        print(f"[Zork] {game} failed to start: {type(exc).__name__}: {exc}")
        await interaction.followup.send("The machine couldn't start that story.", ephemeral=True)
        return

    print(f"[Zork] {game} thread {thread.id}")
    note = f"You're in {thread.mention}. Type there."
    if not added:
        note = "I opened the thread but couldn't add you to it."
    await interaction.followup.send(
        note,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def _open_thread(channel, name: str):
    error = None
    for duration in (10080, 4320, 1440, 60):
        try:
            return await channel.create_thread(
                name=name,
                type=discord.ChannelType.private_thread,
                invitable=False,
                auto_archive_duration=duration,
            )
        except discord.HTTPException as exc:
            error = exc
    raise error if error else RuntimeError("thread was not created")


async def _apply(channel, binding: dict, command: str, *, restart: bool = False, message=None) -> None:
    root = _config["root"]
    worker = restart_session if restart else command_session
    args = (root, binding["user_id"], binding["game"])
    if not restart:
        args = (*args, command)
    try:
        async with _LOCK:
            turn = await asyncio.to_thread(worker, *args)
            await _show(channel, binding, turn, message)
    except Exception as exc:
        print(f"[Zork] command failed: {type(exc).__name__}: {exc}")


async def _show(channel, binding: dict, turn, message) -> None:
    payload = dict(
        embed=embed_for(turn),
        view=ZorkView(),
        allowed_mentions=discord.AllowedMentions.none(),
    )
    target = message
    if target is None and binding.get("message_id"):
        target = channel.get_partial_message(int(binding["message_id"]))
    if target is not None:
        try:
            await target.edit(**payload)
            return
        except (discord.HTTPException, discord.NotFound):
            pass
    sent = await channel.send(**payload)
    guild_id = binding.get("guild_id") or ""
    guild = getattr(channel, "guild", None)
    if guild is not None:
        guild_id = guild.id
    bind_thread(
        _config["root"],
        channel.id,
        binding["user_id"],
        binding["game"],
        sent.id,
        guild_id,
    )
