"""Discord /zork. The game itself never calls xAI."""

from __future__ import annotations

import asyncio

import discord
from discord import ui

from zork.play import (
    GAMES,
    active_game,
    bind_thread,
    command_session,
    lookup_thread,
    map_sketch,
    named_in_text,
    open_session,
    remember_active,
    restart_session,
    toggle_ui,
)

_config: dict = {}
_LOCK = asyncio.Lock()
_PICKED: dict[tuple[str, str], str] = {}
ACCENT = discord.Color.from_rgb(176, 186, 194)
PANEL_TIMEOUT = 4 * 60 * 60


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


def panel_view(user_id, game_id: str, turn) -> "ZorkPanel":
    """The same controls in every room. Objects come only from that room's text."""
    room_text = getattr(turn, "place", "") or getattr(turn, "text", "") or ""
    names = named_in_text(game_id, getattr(turn, "room", ""), room_text)
    key = (str(user_id), game_id)
    if _PICKED.get(key) not in names:
        _PICKED.pop(key, None)
    return ZorkPanel(user_id, game_id, names)


class ZorkPanel(ui.View):
    """Ephemeral controls. Only the player who opened /zork can use them."""

    def __init__(self, user_id, game_id: str, objects: list[str]):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.user_id = str(user_id)
        self.game_id = game_id
        moves = (
            ("N", "north", 0),
            ("S", "south", 0),
            ("E", "east", 0),
            ("W", "west", 0),
            ("U", "up", 0),
            ("D", "down", 1),
            ("Look", "look", 1),
            ("Inventory", "inventory", 1),
            ("Type", "", 1),
            ("Take", "take", 2),
            ("Open", "open", 2),
            ("Examine", "examine", 2),
            ("Restart", "restart", 2),
            ("Dismiss", "", 2),
        )
        for label, command, row in moves:
            self.add_item(_PanelButton(label, command, row))
        if objects:
            self.add_item(_ObjectMenu(objects))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) == self.user_id:
            return True
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "This game belongs to someone else.", ephemeral=True
            )
        return False


class _PanelButton(ui.Button):
    def __init__(self, label: str, command: str, row: int):
        self.play = command
        style = discord.ButtonStyle.secondary
        shown = label
        if label == "Type":
            style = discord.ButtonStyle.primary
        elif label == "Restart":
            style = discord.ButtonStyle.danger
        super().__init__(label=shown, style=style, row=row)

    async def callback(self, interaction: discord.Interaction):
        view: ZorkPanel = self.view
        if self.label == "Dismiss":
            await interaction.response.edit_message(content="Dismissed.", embed=None, view=None)
            return
        if self.label == "Type":
            if not await _move_allowed(interaction):
                return
            await interaction.response.send_modal(_CommandModal(view.user_id, view.game_id))
            return
        if self.play == "restart":
            if not await _move_allowed(interaction):
                return
            await interaction.response.defer()
            await _panel_turn(interaction, view, restart=True)
            return
        if self.play in {"take", "open", "examine"}:
            name = _PICKED.get((view.user_id, view.game_id), "")
            if not name:
                await interaction.response.send_message(
                    "Pick an object from this room first.", ephemeral=True
                )
                return
            if not await _move_allowed(interaction):
                return
            await interaction.response.defer()
            await _panel_turn(interaction, view, f"{self.play} {name}")
            return
        if not await _move_allowed(interaction):
            return
        await interaction.response.defer()
        await _panel_turn(interaction, view, self.play)


class _ObjectMenu(ui.Select):
    def __init__(self, names: list[str]):
        super().__init__(
            placeholder="Objects in this room",
            min_values=1,
            max_values=1,
            row=3,
            options=[discord.SelectOption(label=name[:100], value=name[:100]) for name in names[:8]],
        )

    async def callback(self, interaction: discord.Interaction):
        view: ZorkPanel = self.view
        choice = (self.values or [""])[0]
        if choice:
            _PICKED[(view.user_id, view.game_id)] = choice
        if not interaction.response.is_done():
            await interaction.response.defer()


class _CommandModal(ui.Modal, title="Zork"):
    command = ui.TextInput(
        label="Command",
        placeholder="open mailbox",
        max_length=180,
        required=True,
    )

    def __init__(self, user_id: str, game_id: str):
        super().__init__()
        self.user_id = user_id
        self.game_id = game_id

    async def on_submit(self, interaction: discord.Interaction):
        text = " ".join(str(self.command.value or "").split())[:180]
        if not text:
            await interaction.response.send_message("Type a command.", ephemeral=True)
            return
        if not await _move_allowed(interaction):
            return
        await interaction.response.defer()
        await _panel_turn(interaction, ZorkPanel(self.user_id, self.game_id, []), text)


def install_zork(bot, *, data_root, footer, allow_open, allow_move) -> None:
    _config["root"] = data_root
    _config["footer"] = footer
    _config["allow_open"] = allow_open
    _config["allow_move"] = allow_move

    @bot.tree.command(name="zork", description="Play Zork I, II, or III on a private panel")
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

    @bot.tree.command(
        name="zork-ui",
        description="Switch your Zork panel between HUD and TERMINAL",
    )
    @discord.app_commands.guild_only()
    async def zork_ui(interaction: discord.Interaction):
        await _ui(interaction)

    @bot.tree.command(
        name="zork-map",
        description="Sketch the rooms you have walked in your Zork game",
    )
    @discord.app_commands.guild_only()
    async def zork_map(interaction: discord.Interaction):
        await _map(interaction)


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
    if interaction.guild is None or interaction.channel is None:
        await interaction.response.send_message("Open /zork in a server channel.", ephemeral=True)
        return
    allow_open = _config.get("allow_open")
    if allow_open and not await allow_open(interaction, "zork"):
        return

    await interaction.response.defer(ephemeral=True)
    try:
        async with _LOCK:
            turn = await asyncio.to_thread(open_session, _config["root"], interaction.user.id, game)
            remember_active(_config["root"], interaction.guild.id, interaction.user.id, game)
    except Exception as exc:
        print(f"[Zork] {game} failed to start: {type(exc).__name__}: {exc}")
        await interaction.followup.send("The machine couldn't start that story.", ephemeral=True)
        return
    await interaction.followup.send(
        embed=embed_for(turn),
        view=panel_view(interaction.user.id, game, turn),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def _ui(interaction: discord.Interaction) -> None:
    channel = interaction.channel
    binding = lookup_thread(_config.get("root"), getattr(channel, "id", None))
    if binding and isinstance(channel, discord.Thread):
        if str(interaction.user.id) != str(binding.get("user_id")):
            await interaction.response.send_message(
                "This game belongs to someone else.", ephemeral=True
            )
            return
        allow_open = _config.get("allow_open")
        if allow_open and not await allow_open(interaction, "zork_ui"):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            async with _LOCK:
                turn = await asyncio.to_thread(
                    toggle_ui,
                    _config["root"],
                    binding["user_id"],
                    binding["game"],
                )
                await _show(channel, binding, turn, None)
        except Exception as exc:
            print(f"[Zork] UI toggle failed: {type(exc).__name__}: {exc}")
            await interaction.followup.send("The panel didn't update.", ephemeral=True)
            return
        await interaction.followup.send(turn.ui.upper(), ephemeral=True)
        return

    game = active_game(
        _config.get("root"),
        getattr(interaction.guild, "id", "dm"),
        interaction.user.id,
    )
    if not game:
        await interaction.response.send_message("Use /zork first.", ephemeral=True)
        return
    allow_open = _config.get("allow_open")
    if allow_open and not await allow_open(interaction, "zork_ui"):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        async with _LOCK:
            turn = await asyncio.to_thread(
                toggle_ui, _config["root"], interaction.user.id, game
            )
    except Exception as exc:
        print(f"[Zork] UI toggle failed: {type(exc).__name__}: {exc}")
        await interaction.followup.send("The panel didn't update.", ephemeral=True)
        return
    await interaction.followup.send(turn.ui.upper(), ephemeral=True)


async def _map(interaction: discord.Interaction) -> None:
    channel = interaction.channel
    binding = lookup_thread(_config.get("root"), getattr(channel, "id", None))
    if binding and isinstance(channel, discord.Thread):
        if str(interaction.user.id) != str(binding.get("user_id")):
            await interaction.response.send_message(
                "This game belongs to someone else.", ephemeral=True
            )
            return
        allow_open = _config.get("allow_open")
        if allow_open and not await allow_open(interaction, "zork_map"):
            return
        try:
            async with _LOCK:
                sketch = await asyncio.to_thread(
                    map_sketch,
                    _config["root"],
                    binding["user_id"],
                    binding["game"],
                )
        except Exception as exc:
            print(f"[Zork] map failed: {type(exc).__name__}: {exc}")
            await interaction.response.send_message("The sketch didn't come out.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"```\n{sketch}\n```",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    game = active_game(
        _config.get("root"),
        getattr(interaction.guild, "id", "dm"),
        interaction.user.id,
    )
    if not game:
        await interaction.response.send_message("Use /zork first.", ephemeral=True)
        return
    allow_open = _config.get("allow_open")
    if allow_open and not await allow_open(interaction, "zork_map"):
        return
    try:
        async with _LOCK:
            sketch = await asyncio.to_thread(
                map_sketch, _config["root"], interaction.user.id, game
            )
    except Exception as exc:
        print(f"[Zork] map failed: {type(exc).__name__}: {exc}")
        await interaction.response.send_message("The sketch didn't come out.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"```\n{sketch}\n```",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def _move_allowed(interaction: discord.Interaction) -> bool:
    allow = _config.get("allow_move")
    if not allow:
        return True
    guild = str(getattr(interaction, "guild_id", None) or "dm")
    if allow(guild, interaction.user.id):
        return True
    if not interaction.response.is_done():
        await interaction.response.send_message("Too many moves. Wait a moment.", ephemeral=True)
    return False


async def _panel_turn(interaction: discord.Interaction, view: ZorkPanel, command: str = "", *, restart: bool = False) -> None:
    root = _config["root"]
    worker = restart_session if restart else command_session
    args = (root, view.user_id, view.game_id)
    if not restart:
        args = (*args, command)
    try:
        async with _LOCK:
            turn = await asyncio.to_thread(worker, *args)
    except Exception as exc:
        print(f"[Zork] command failed: {type(exc).__name__}: {exc}")
        await interaction.followup.send("The machine didn't answer.", ephemeral=True)
        return
    payload = dict(
        embed=embed_for(turn),
        view=panel_view(view.user_id, view.game_id, turn),
        allowed_mentions=discord.AllowedMentions.none(),
    )
    try:
        await interaction.edit_original_response(**payload)
    except discord.HTTPException:
        await interaction.followup.send(**payload, ephemeral=True)


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
