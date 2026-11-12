"""Private preview before Grim posts or acts for someone. Nothing goes public before Confirm."""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from io import BytesIO
from typing import Awaitable, Callable

import discord
from discord import ui

from image_clean import METADATA_DETAIL

PREVIEW_TIMEOUT = 10 * 60
WARNING_TIMEOUT = 5 * 60
PREVIEW_COLOR = discord.Color.from_rgb(18, 18, 18)

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
    original: bytes | None = None
    removed: tuple[str, ...] = ()

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

    @property
    def can_post_original(self) -> bool:
        return bool(self.original and self.removed)

    def original_post(self) -> "PreparedPost":
        """The same post with the untouched file. The neutral filename is kept."""
        return dataclasses.replace(self, image=self.original, original=None, removed=())


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


def status_line(post: PreparedPost) -> str | None:
    if not post.image:
        return None
    if post.removed:
        return f"Metadata stripped: {', '.join(post.removed[:5])}" + (" +more" if len(post.removed) > 5 else "")
    return "No metadata found"


def preview_kwargs(post: PreparedPost, footer: str | None = None) -> dict:
    """The exact public post, plus one small preview-only status embed."""
    payload = post.kwargs()
    embeds = []
    public_embed = payload.pop("embed", None)
    if public_embed is not None:
        embeds.append(public_embed)
    lines = ["**Preview** · only you can see this"]
    status = status_line(post)
    if status:
        lines.append(status)
    note = discord.Embed(description="\n".join(lines), color=PREVIEW_COLOR)
    if footer:
        note.set_footer(text=footer)
    embeds.append(note)
    payload["embeds"] = embeds
    return payload


class PreviewView(ui.View):
    """Confirm, Cancel, and (when metadata was removed) Post original, for the invoker only."""

    def __init__(
        self,
        owner_id,
        post: PreparedPost,
        *,
        label: str = "post",
        allowed: Callable[[discord.Interaction], bool] | None = None,
        on_posted: Callable[..., Awaitable[None]] | None = None,
        footer: str | None = None,
        timeout: float = PREVIEW_TIMEOUT,
    ):
        super().__init__(timeout=timeout)
        self.owner_id = str(owner_id)
        self.post = post
        self.label = label
        self.allowed = allowed
        self.on_posted = on_posted
        self.footer = footer
        self.message = None
        self.finished = False
        self.posted = None
        if not post.can_post_original:
            self.remove_item(self.original_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.owner_id:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "This preview belongs to someone else.", ephemeral=True
                )
            return False
        return await self.still_allowed(interaction)

    async def still_allowed(self, interaction: discord.Interaction) -> bool:
        if self.allowed is None or self.allowed(interaction):
            return True
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "You no longer have permission. Nothing was posted.", ephemeral=True
            )
        else:
            await interaction.followup.send(
                "You no longer have permission. Nothing was posted.", ephemeral=True
            )
        return False

    @ui.button(label="Confirm", style=discord.ButtonStyle.primary)
    async def confirm_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.confirm(interaction)

    @ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.cancel(interaction)

    @ui.button(label="Post original…", style=discord.ButtonStyle.secondary)
    async def original_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.ask_original(interaction)

    async def confirm(self, interaction: discord.Interaction) -> None:
        if self.finished:
            await self._already_handled(interaction)
            return
        if not await self.still_allowed(interaction):
            return
        await self.publish(interaction, self.post, original=False)

    async def ask_original(self, interaction: discord.Interaction) -> None:
        """Warn first. Nothing is posted until the warning is confirmed."""
        if self.finished:
            await self._already_handled(interaction)
            return
        if not self.post.can_post_original:
            await interaction.response.send_message("Nothing to post but the stripped file.", ephemeral=True)
            return
        if not await self.still_allowed(interaction):
            return
        warning = OriginalWarningView(self)
        embed = discord.Embed(
            title="Post the original file?",
            description=(
                "This file still carries:\n"
                + "\n".join(f"• {METADATA_DETAIL.get(item, item)}" for item in self.post.removed)
                + "\n\nAnyone in this channel can download it and read that. "
                "Grim's post doesn't name you, but this data can point to you or to a place."
            ),
            color=PREVIEW_COLOR,
        )
        if self.footer:
            embed.set_footer(text=self.footer)
        await interaction.response.defer(ephemeral=True)
        warning.message = await interaction.followup.send(
            embed=embed,
            view=warning,
            ephemeral=True,
            wait=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def publish(self, interaction: discord.Interaction, post: PreparedPost, *, original: bool) -> None:
        self.finished = True
        self.stop()
        await interaction.response.defer()

        channel = interaction.channel
        if channel is None or not hasattr(channel, "send"):
            await self._close(interaction, "This channel is gone. Nothing was posted.")
            return
        missing = missing_permissions(interaction, post)
        if missing:
            await self._close(interaction, permission_error(missing))
            return
        try:
            self.posted = await channel.send(**post.kwargs())
        except discord.Forbidden:
            await self._close(interaction, "Discord refused the post. Check Grim's channel permissions. Nothing was posted.")
            return
        except discord.HTTPException as exc:
            print(f"[Preview] {self.label} send failed: {type(exc).__name__}")
            await self._close(interaction, "Discord didn't take the post. Nothing was posted.")
            return
        await self._close(interaction, "Posted the original." if original else "Posted.")
        if self.on_posted is not None:
            await self.on_posted(interaction, self.posted, original)

    async def cancel(self, interaction: discord.Interaction) -> None:
        if self.finished:
            await self._already_handled(interaction)
            return
        if not await self.still_allowed(interaction):
            return
        self.finished = True
        self.stop()
        await interaction.response.edit_message(
            content="Cancelled. Nothing was posted.",
            embeds=[],
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
                embeds=[],
                attachments=[],
                view=None,
            )
        except discord.HTTPException:
            pass

    async def close_preview(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(view=None)
        except discord.HTTPException:
            pass

    async def _already_handled(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.send_message("Already handled.", ephemeral=True)

    async def _close(self, interaction: discord.Interaction, note: str) -> None:
        try:
            await interaction.edit_original_response(view=None)
        except discord.HTTPException:
            pass
        await self.close_preview()
        await interaction.followup.send(note, ephemeral=True)


class OriginalWarningView(ui.View):
    """The second confirm for posting a file with its metadata. Back posts nothing."""

    def __init__(self, parent: PreviewView, *, timeout: float = WARNING_TIMEOUT):
        super().__init__(timeout=timeout)
        self.parent = parent
        self.message = None
        self.finished = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.parent.interaction_check(interaction)

    @ui.button(label="Post original", style=discord.ButtonStyle.danger)
    async def post_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.post_original(interaction)

    @ui.button(label="Back", style=discord.ButtonStyle.secondary)
    async def back_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.back(interaction)

    async def post_original(self, interaction: discord.Interaction) -> None:
        if self.finished or self.parent.finished:
            if not interaction.response.is_done():
                await interaction.response.send_message("Already handled.", ephemeral=True)
            return
        if not await self.parent.still_allowed(interaction):
            return
        self.finished = True
        self.stop()
        await self.parent.publish(interaction, self.parent.post.original_post(), original=True)

    async def back(self, interaction: discord.Interaction) -> None:
        if self.finished:
            if not interaction.response.is_done():
                await interaction.response.send_message("Already handled.", ephemeral=True)
            return
        if not await self.parent.still_allowed(interaction):
            return
        self.finished = True
        self.stop()
        await interaction.response.edit_message(
            content="Back to the preview. Nothing was posted.",
            embeds=[],
            view=None,
        )

    async def on_timeout(self) -> None:
        if self.finished:
            return
        self.finished = True
        if self.message is None:
            return
        try:
            await self.message.edit(content="Expired. Nothing was posted.", embeds=[], view=None)
        except discord.HTTPException:
            pass


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
    view.message = await interaction.followup.send(
        **preview_kwargs(post, view.footer), view=view, ephemeral=True, wait=True
    )
    return view
