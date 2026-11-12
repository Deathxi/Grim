"""Private preview before Grim posts or acts for someone. Nothing goes public before Confirm."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from io import BytesIO
from typing import Awaitable, Callable

import discord
from discord import ui

PREVIEW_TIMEOUT = 10 * 60

_USER_MENTION = re.compile(r"<@!?(\d{15,25})>")
_ROLE_MENTION = re.compile(r"<@&(\d{15,25})>")


@dataclass
class PreparedPost:
    """Built once. The preview and the public post both send this."""

    content: str | None = None
    embed: discord.Embed | None = None
    image: bytes | None = None
    filename: str | None = None
    allowed_mentions: discord.AllowedMentions = field(default_factory=discord.AllowedMentions.none)

    def kwargs(self) -> dict:
        payload: dict = {"allowed_mentions": self.allowed_mentions}
        if self.content:
            payload["content"] = self.content
        if self.embed is not None:
            payload["embed"] = self.embed
        if self.image:
            # A discord.File is spent after one send, so each send gets its own.
            payload["file"] = discord.File(BytesIO(self.image), filename=self.filename or "image.png")
        return payload


def typed_mentions(text: str | None) -> discord.AllowedMentions:
    """Ping only what the invoker typed. Everything else stays silent."""
    text = text or ""
    users = [discord.Object(id=int(value)) for value in dict.fromkeys(_USER_MENTION.findall(text))]
    roles = [discord.Object(id=int(value)) for value in dict.fromkeys(_ROLE_MENTION.findall(text))]
    return discord.AllowedMentions(
        everyone="@everyone" in text or "@here" in text,
        users=users or False,
        roles=roles or False,
        replied_user=False,
    )


def missing_permissions(interaction: discord.Interaction, post: PreparedPost) -> list[str]:
    """Grim's own channel permissions this post needs and doesn't have."""
    permissions = getattr(interaction, "app_permissions", None)
    if permissions is None:
        return []
    channel = getattr(interaction, "channel", None)
    missing = []
    if isinstance(channel, discord.Thread):
        if not getattr(permissions, "send_messages_in_threads", False):
            missing.append("Send Messages in Threads")
    elif not getattr(permissions, "send_messages", False):
        missing.append("Send Messages")
    if post.embed is not None and not getattr(permissions, "embed_links", False):
        missing.append("Embed Links")
    if post.image and not getattr(permissions, "attach_files", False):
        missing.append("Attach Files")
    return missing


def permission_error(missing: list[str]) -> str:
    return f"Grim can't post here. Missing: {', '.join(missing)}. Nothing was posted."


class PreviewView(ui.View):
    """Confirm or Cancel for the invoker only. Confirm sends the prepared post once."""

    def __init__(
        self,
        owner_id,
        post: PreparedPost,
        *,
        label: str = "post",
        allowed: Callable[[discord.Interaction], bool] | None = None,
        on_posted: Callable[[discord.Interaction, discord.Message], Awaitable[None]] | None = None,
        timeout: float = PREVIEW_TIMEOUT,
    ):
        super().__init__(timeout=timeout)
        self.owner_id = str(owner_id)
        self.post = post
        self.label = label
        self.allowed = allowed
        self.on_posted = on_posted
        self.message = None
        self.finished = False
        self.posted = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) == self.owner_id:
            return True
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "This preview belongs to someone else.", ephemeral=True
            )
        return False

    @ui.button(label="Confirm", style=discord.ButtonStyle.secondary)
    async def confirm_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.confirm(interaction)

    @ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.cancel(interaction)

    async def confirm(self, interaction: discord.Interaction) -> None:
        if self.finished:
            if not interaction.response.is_done():
                await interaction.response.send_message("Already handled.", ephemeral=True)
            return
        self.finished = True
        self.stop()
        await interaction.response.defer()

        if self.allowed is not None and not self.allowed(interaction):
            await self._close(interaction, "You no longer have permission. Nothing was posted.")
            return
        channel = interaction.channel
        if channel is None or not hasattr(channel, "send"):
            await self._close(interaction, "This channel is gone. Nothing was posted.")
            return
        missing = missing_permissions(interaction, self.post)
        if missing:
            await self._close(interaction, permission_error(missing))
            return
        try:
            self.posted = await channel.send(**self.post.kwargs())
        except discord.Forbidden:
            await self._close(interaction, "Discord refused the post. Check Grim's channel permissions. Nothing was posted.")
            return
        except discord.HTTPException as exc:
            print(f"[Preview] {self.label} send failed: {type(exc).__name__}")
            await self._close(interaction, "Discord didn't take the post. Nothing was posted.")
            return
        await self._close(interaction, "Posted.")
        if self.on_posted is not None:
            await self.on_posted(interaction, self.posted)

    async def cancel(self, interaction: discord.Interaction) -> None:
        if self.finished:
            if not interaction.response.is_done():
                await interaction.response.send_message("Already handled.", ephemeral=True)
            return
        self.finished = True
        self.stop()
        await interaction.response.edit_message(
            content="Cancelled. Nothing was posted.",
            embed=None,
            attachments=[],
            view=None,
        )

    async def on_timeout(self) -> None:
        if self.finished:
            return
        self.finished = True
        if self.message is None:
            return
        try:
            await self.message.edit(
                content="Preview expired. Nothing was posted.",
                embed=None,
                attachments=[],
                view=None,
            )
        except discord.HTTPException:
            pass

    async def _close(self, interaction: discord.Interaction, note: str) -> None:
        try:
            await interaction.edit_original_response(view=None)
        except discord.HTTPException:
            pass
        await interaction.followup.send(note, ephemeral=True)


async def send_preview(
    interaction: discord.Interaction,
    post: PreparedPost,
    **view_options,
) -> PreviewView | None:
    """Show the invoker the exact post. Call after a deferred ephemeral response."""
    missing = missing_permissions(interaction, post)
    if missing:
        await interaction.followup.send(permission_error(missing), ephemeral=True)
        return None
    view = PreviewView(interaction.user.id, post, **view_options)
    view.message = await interaction.followup.send(**post.kwargs(), view=view, ephemeral=True, wait=True)
    return view
