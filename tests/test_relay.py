import asyncio
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from PIL import Image

import main
import post_preview
from image_clean import metadata_left, strip_image_metadata
from tests.test_image_clean import LEAKS, dirty_jpeg


def png_bytes():
    buffer = BytesIO()
    Image.new("RGB", (4, 4), (18, 18, 18)).save(buffer, format="PNG")
    return buffer.getvalue()


class RelayResponse:
    def __init__(self):
        self.messages = []
        self.deferred = []
        self.edits = []
        self.done = False

    def is_done(self):
        return self.done

    async def send_message(self, content=None, **kwargs):
        self.done = True
        self.messages.append((content, kwargs))

    async def defer(self, **kwargs):
        self.done = True
        self.deferred.append(kwargs)

    async def edit_message(self, **kwargs):
        self.done = True
        self.edits.append(kwargs)


class RelayFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        message = SimpleNamespace(edit=AsyncMock())
        if content is not None:
            kwargs = {"content": content, **kwargs}
        self.sent.append((content, kwargs, message))
        return message


class RelayChannel:
    def __init__(self, error=None):
        self.sent = []
        self.error = error

    async def send(self, **kwargs):
        if self.error is not None:
            raise self.error
        self.sent.append(kwargs)
        return SimpleNamespace(id=777)


def bot_permissions(**overrides):
    values = dict(
        send_messages=True,
        send_messages_in_threads=True,
        embed_links=True,
        attach_files=True,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def relay_interaction(*, admin=True, user_id=10, channel=None, permissions=None):
    return SimpleNamespace(
        guild_id=50,
        guild=SimpleNamespace(id=50, owner_id=1, filesize_limit=10 * 1024 * 1024),
        channel=channel if channel is not None else RelayChannel(),
        channel_id=60,
        user=SimpleNamespace(
            id=user_id,
            name="Noct",
            display_name="Noct",
            guild_permissions=SimpleNamespace(administrator=admin),
        ),
        app_permissions=permissions or bot_permissions(),
        response=RelayResponse(),
        followup=RelayFollowup(),
        edit_original_response=AsyncMock(),
    )


def attachment(data=None, filename="noct-private-photo.png"):
    data = png_bytes() if data is None else data
    return SimpleNamespace(
        size=len(data),
        read=AsyncMock(return_value=data),
        filename=filename,
        url=f"https://cdn.discordapp.com/attachments/1/2/{filename}",
        content_type="image/png",
    )


def file_bytes(payload):
    return payload["file"].fp.getvalue()


class RelayCommandTests(unittest.TestCase):
    def setUp(self):
        main._COMMAND_RATE_LIMITS.clear()
        self.audit = patch.object(main, "record_security_event")
        self.audit.start()

    def tearDown(self):
        self.audit.stop()
        main._COMMAND_RATE_LIMITS.clear()

    async def preview(self, command, *, text=None, image=None, rewrite="Polished words.", event=None):
        event = event or relay_interaction()
        model = AsyncMock(return_value=rewrite)
        with patch.object(main, "_post_xai_chat", model):
            await command.callback(event, text, image)
        return event, model

    def preview_view(self, event):
        for _, kwargs, _ in event.followup.sent:
            if isinstance(kwargs.get("view"), post_preview.PreviewView):
                return kwargs, kwargs["view"]
        self.fail("no preview was sent")

    def test_commands_are_admin_and_guild_only(self):
        for command in (main.announce, main.say):
            self.assertTrue(command.guild_only)
            self.assertTrue(command.default_permissions.administrator)

    def test_non_admin_is_denied_and_nothing_is_posted(self):
        async def run():
            for command in (main.announce, main.say):
                channel = RelayChannel()
                event = relay_interaction(admin=False, channel=channel)
                event, model = await self.preview(command, text="hello all", image=attachment(), event=event)
                self.assertEqual(len(event.response.deferred), 1)
                self.assertTrue(event.response.deferred[0]["ephemeral"])
                content, kwargs, _ = event.followup.sent[0]
                self.assertIn("Only server administrators", content)
                self.assertTrue(kwargs["ephemeral"])
                self.assertEqual(len(event.followup.sent), 1)
                self.assertEqual(channel.sent, [])
                model.assert_not_awaited()

        asyncio.run(run())

    def test_admin_gets_an_ephemeral_preview_and_nothing_goes_public(self):
        async def run():
            event, model = await self.preview(main.announce, text="meeting friday at 8")
            self.assertTrue(event.response.deferred[0]["ephemeral"])
            kwargs, view = self.preview_view(event)
            self.assertTrue(kwargs["ephemeral"])
            self.assertEqual(kwargs["embeds"][0].description, "Polished words.")
            self.assertEqual(kwargs["embeds"][0].title, "Announcement")
            self.assertIn("Powered by XSI", kwargs["embeds"][0].footer.text)
            self.assertIn("only you can see this", kwargs["embeds"][-1].description)
            self.assertEqual(view.owner_id, "10")
            self.assertEqual(event.channel.sent, [])
            model.assert_awaited_once()

        asyncio.run(run())

    def test_confirm_posts_exactly_the_preview_once(self):
        async def run():
            image = attachment()
            event, model = await self.preview(main.announce, text="meeting friday at 8", image=image)
            preview, view = self.preview_view(event)
            first = relay_interaction(channel=event.channel)
            second = relay_interaction(channel=event.channel)
            await view.confirm(first)
            await view.confirm(second)

            self.assertEqual(len(event.channel.sent), 1)
            public = event.channel.sent[0]
            self.assertEqual(public["embed"].to_dict(), preview["embeds"][0].to_dict())
            self.assertEqual(public.get("content"), preview.get("content"))
            self.assertEqual(file_bytes(public), file_bytes(preview))
            self.assertEqual(public["file"].filename, preview["file"].filename)
            self.assertEqual(public["allowed_mentions"].to_dict(), preview["allowed_mentions"].to_dict())
            model.assert_awaited_once()
            self.assertEqual(first.followup.sent[-1][0], "Posted.")
            self.assertIn("Already handled", second.response.messages[0][0])

        asyncio.run(run())

    def test_public_post_is_anonymous(self):
        async def run():
            event, _ = await self.preview(main.say, text="see you there", rewrite="see you there")
            _, view = self.preview_view(event)
            await view.confirm(relay_interaction(channel=event.channel))
            public = event.channel.sent[0]
            self.assertEqual(public["content"], "see you there")
            rendered = repr(public)
            for marker in ("Noct", "<@10>", "10>"):
                self.assertNotIn(marker, rendered)

        asyncio.run(run())

    def test_cancel_posts_nothing(self):
        async def run():
            event, _ = await self.preview(main.say, text="hello", rewrite="hello")
            _, view = self.preview_view(event)
            cancel = relay_interaction(channel=event.channel)
            await view.cancel(cancel)
            self.assertIn("Nothing was posted", cancel.response.edits[0]["content"])
            self.assertIsNone(cancel.response.edits[0]["view"])
            await view.confirm(relay_interaction(channel=event.channel))
            self.assertEqual(event.channel.sent, [])

        asyncio.run(run())

    def test_timeout_posts_nothing_and_says_it_expired(self):
        async def run():
            event, _ = await self.preview(main.say, text="hello", rewrite="hello")
            _, view = self.preview_view(event)
            await view.on_timeout()
            view.message.edit.assert_awaited_once()
            self.assertIn("expired", view.message.edit.await_args.kwargs["content"])
            await view.confirm(relay_interaction(channel=event.channel))
            self.assertEqual(event.channel.sent, [])

        asyncio.run(run())

    def test_only_the_invoker_can_press_the_buttons(self):
        async def run():
            event, _ = await self.preview(main.say, text="hello", rewrite="hello")
            _, view = self.preview_view(event)
            stranger = relay_interaction(user_id=99, channel=event.channel)
            self.assertFalse(await view.interaction_check(stranger))
            self.assertIn("someone else", stranger.response.messages[0][0])
            self.assertTrue(await view.interaction_check(relay_interaction(channel=event.channel)))

        asyncio.run(run())

    def test_confirm_checks_admin_again(self):
        async def run():
            event, _ = await self.preview(main.say, text="hello", rewrite="hello")
            _, view = self.preview_view(event)
            demoted = relay_interaction(admin=False, channel=event.channel)
            await view.confirm(demoted)
            self.assertEqual(event.channel.sent, [])
            self.assertIn("no longer have permission", demoted.response.messages[0][0])

        asyncio.run(run())

    def test_image_is_downloaded_and_reuploaded_under_a_neutral_name(self):
        async def run():
            data = png_bytes()
            image = attachment(data)
            event, _ = await self.preview(main.say, text="look", image=image, rewrite="look")
            image.read.assert_awaited_once()
            _, view = self.preview_view(event)
            await view.confirm(relay_interaction(channel=event.channel))
            public = event.channel.sent[0]
            self.assertIsInstance(public["file"], discord.File)
            self.assertEqual(file_bytes(public), data)
            self.assertEqual(public["file"].filename, "grim-say.png")
            self.assertNotIn(image.url, repr(public))
            self.assertNotIn("noct-private-photo", repr(public))

            event, _ = await self.preview(main.announce, image=attachment(data))
            preview, _ = self.preview_view(event)
            self.assertEqual(preview["embeds"][0].image.url, "attachment://grim-announce.png")

        asyncio.run(run())

    def test_reuploaded_image_has_its_metadata_stripped_in_preview_and_public_post(self):
        async def run():
            from tests.test_image_clean import LEAKS, dirty_jpeg, dirty_png, dirty_webp, dirty_gif, metadata_left

            for command in (main.say, main.announce):
                for dirty in (dirty_jpeg(), dirty_png(), dirty_gif(), dirty_webp()):
                    self.assertTrue(any(leak in dirty for leak in LEAKS))
                    event, _ = await self.preview(command, text="hi", image=attachment(dirty), rewrite="hi")
                    preview, view = self.preview_view(event)
                    shown = file_bytes(preview)
                    for leak in LEAKS:
                        self.assertNotIn(leak, shown)
                    self.assertEqual(metadata_left(shown), [])
                    await view.confirm(relay_interaction(channel=event.channel))
                    self.assertEqual(file_bytes(event.channel.sent[-1]), shown)

        asyncio.run(run())

    def test_image_that_cannot_be_cleaned_is_refused(self):
        async def run():
            from image_clean import ImageCleanError

            with patch.object(main, "clean_image_report", side_effect=ImageCleanError("boom")):
                event, _ = await self.preview(main.say, text="hi", image=attachment())
            self.assertIn("won't post it", event.followup.sent[0][0])
            self.assertEqual(event.channel.sent, [])

        asyncio.run(run())

    def test_quote_card_goes_through_the_shared_cleaner(self):
        async def run():
            from datetime import datetime, timezone

            real = main.clean_image
            seen = []

            def spy(data):
                seen.append(data)
                return real(data)

            with patch.object(main, "clean_image", side_effect=spy), \
                    patch.object(main.aiohttp, "ClientSession", side_effect=RuntimeError("offline")):
                quote = await main._build_quote_file(
                    "hello", "Noct", "https://cdn.example/a.png", datetime(2026, 1, 1, tzinfo=timezone.utc)
                )
            self.assertEqual(len(seen), 1)
            self.assertEqual(quote.filename, "grim-quote.png")
            self.assertEqual(quote.fp.getvalue()[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(metadata_left(quote.fp.getvalue()), [])

        asyncio.run(run())

    def test_text_only(self):
        async def run():
            event, _ = await self.preview(main.say, text="  teh  plan is set", rewrite="the plan is set")
            preview, view = self.preview_view(event)
            self.assertEqual(preview["content"], "the plan is set")
            self.assertNotIn("file", preview)
            await view.confirm(relay_interaction(channel=event.channel))
            self.assertNotIn("file", event.channel.sent[0])

        asyncio.run(run())

    def test_image_only_skips_the_rewrite(self):
        async def run():
            event, model = await self.preview(main.say, image=attachment())
            preview, _ = self.preview_view(event)
            self.assertNotIn("content", preview)
            self.assertIn("file", preview)
            self.assertEqual(len(preview["embeds"]), 1)
            model.assert_not_awaited()

            event, model = await self.preview(main.announce, image=attachment())
            preview, _ = self.preview_view(event)
            self.assertIsNone(preview["embeds"][0].description)
            model.assert_not_awaited()

        asyncio.run(run())

    def test_neither_text_nor_image_is_rejected(self):
        async def run():
            for command in (main.announce, main.say):
                for text in (None, "   "):
                    event, model = await self.preview(command, text=text)
                    self.assertIn("text, an image, or both", event.followup.sent[0][0])
                    self.assertTrue(event.followup.sent[0][1]["ephemeral"])
                    self.assertEqual(len(event.followup.sent), 1)
                    self.assertEqual(event.channel.sent, [])
                    model.assert_not_awaited()

        asyncio.run(run())

    def test_non_image_attachment_is_rejected(self):
        async def run():
            event, _ = await self.preview(main.say, text="hi", image=attachment(b"not an image", "x.png"))
            self.assertIn("isn't an image", event.followup.sent[0][0])
            self.assertEqual(event.channel.sent, [])

        asyncio.run(run())

    def test_missing_channel_permissions_before_preview(self):
        async def run():
            event = relay_interaction(permissions=bot_permissions(attach_files=False))
            event, model = await self.preview(main.say, text="hi", image=attachment(), event=event)
            self.assertIn("Attach Files", event.followup.sent[0][0])
            self.assertTrue(event.followup.sent[0][1]["ephemeral"])
            model.assert_not_awaited()
            self.assertEqual(event.channel.sent, [])

        asyncio.run(run())

    def test_missing_channel_permissions_at_confirm(self):
        async def run():
            event, _ = await self.preview(main.announce, text="hello")
            _, view = self.preview_view(event)
            confirm = relay_interaction(
                channel=event.channel,
                permissions=bot_permissions(embed_links=False),
            )
            await view.confirm(confirm)
            self.assertEqual(event.channel.sent, [])
            note, kwargs, _ = confirm.followup.sent[-1]
            self.assertIn("Embed Links", note)
            self.assertTrue(kwargs["ephemeral"])

            channel = RelayChannel(error=discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no"))
            event, _ = await self.preview(main.say, text="hello", rewrite="hello", event=relay_interaction(channel=channel))
            _, view = self.preview_view(event)
            confirm = relay_interaction(channel=channel)
            await view.confirm(confirm)
            self.assertIn("Nothing was posted", confirm.followup.sent[-1][0])

        asyncio.run(run())

    def test_mentions_are_silent_unless_typed(self):
        async def run():
            event, _ = await self.preview(main.say, text="hello team", rewrite="@everyone hello team")
            preview, _ = self.preview_view(event)
            mentions = preview["allowed_mentions"].to_dict()
            self.assertEqual(mentions["parse"], [])
            self.assertFalse(mentions.get("users"))
            self.assertFalse(mentions.get("roles"))

            event, _ = await self.preview(main.say, text="@everyone hello <@&123456789012345678>", rewrite="@everyone hello <@&123456789012345678>")
            preview, _ = self.preview_view(event)
            mentions = preview["allowed_mentions"].to_dict()
            self.assertIn("everyone", mentions["parse"])
            self.assertEqual(mentions["roles"], [123456789012345678])

            event, _ = await self.preview(main.announce, text="@everyone doors open at 9", rewrite="@everyone Doors open at nine.")
            preview, _ = self.preview_view(event)
            self.assertEqual(preview["content"], "@everyone")
            self.assertNotIn("@everyone", preview["embeds"][0].description)
            self.assertIn("everyone", preview["allowed_mentions"].to_dict()["parse"])

        asyncio.run(run())

    def test_say_keeps_the_original_when_the_rewrite_drifts(self):
        async def run():
            event, _ = await self.preview(
                main.say,
                text="gm everyone, servers back up",
                rewrite="Good morning! I am an AI and here is a whole new message.",
            )
            preview, _ = self.preview_view(event)
            self.assertEqual(preview["content"], "gm everyone, servers back up")
            self.assertIn("as written", event.followup.sent[-1][0])

        asyncio.run(run())

    def test_announce_falls_back_to_the_text_when_xai_is_down(self):
        async def run():
            event, _ = await self.preview(main.announce, text="servers back up", rewrite=None)
            preview, _ = self.preview_view(event)
            self.assertEqual(preview["embeds"][0].description, "servers back up")

        asyncio.run(run())


class RelayOriginalTests(unittest.TestCase):
    def setUp(self):
        main._COMMAND_RATE_LIMITS.clear()
        self.audit = patch.object(main, "record_security_event")
        self.audit_mock = self.audit.start()
        self.dirty = dirty_jpeg()
        self.cleaned = strip_image_metadata(self.dirty, "JPEG")

    def tearDown(self):
        self.audit.stop()
        main._COMMAND_RATE_LIMITS.clear()

    async def preview(self, command=None, *, image_bytes=None, text="hi"):
        event = relay_interaction()
        model = AsyncMock(return_value=text)
        with patch.object(main, "_post_xai_chat", model):
            await (command or main.say).callback(event, text, attachment(image_bytes or self.dirty))
        for _, kwargs, _ in event.followup.sent:
            if isinstance(kwargs.get("view"), post_preview.PreviewView):
                return event, kwargs, kwargs["view"]
        self.fail("no preview")

    async def open_warning(self, event, view, **overrides):
        press = relay_interaction(channel=event.channel, **overrides)
        await view.ask_original(press)
        for _, kwargs, message in press.followup.sent:
            if isinstance(kwargs.get("view"), post_preview.OriginalWarningView):
                return press, kwargs, kwargs["view"]
        return press, None, None

    def labels(self, view):
        return [child.label for child in view.children]

    def test_preview_shows_the_stripped_file_and_says_what_was_removed(self):
        async def run():
            event, preview, view = await self.preview()
            shown = file_bytes(preview)
            self.assertEqual(shown, self.cleaned)
            for leak in LEAKS:
                self.assertNotIn(leak, shown)
            status = preview["embeds"][-1].description
            self.assertIn("Metadata stripped: GPS, device, timestamp", status)
            self.assertEqual(event.channel.sent, [])

            _, clean_preview, _ = await self.preview(image_bytes=self.cleaned)
            self.assertIn("No metadata found", clean_preview["embeds"][-1].description)

        asyncio.run(run())

    def test_confirm_posts_the_stripped_file_the_preview_showed(self):
        async def run():
            event, preview, view = await self.preview()
            await view.confirm(relay_interaction(channel=event.channel))
            self.assertEqual(len(event.channel.sent), 1)
            self.assertEqual(file_bytes(event.channel.sent[0]), file_bytes(preview))
            self.assertEqual(file_bytes(event.channel.sent[0]), self.cleaned)

        asyncio.run(run())

    def test_post_original_needs_the_second_confirm_then_posts_the_original_bytes(self):
        async def run():
            event, preview, view = await self.preview(main.announce)
            press, warning_kwargs, warning = await self.open_warning(event, view)
            self.assertIsNotNone(warning)
            self.assertEqual(event.channel.sent, [])
            text = warning_kwargs["embed"].description
            self.assertIn("GPS / location", text)
            self.assertIn("Device", text)
            self.assertIn("Date and time", text)
            self.assertTrue(warning_kwargs["ephemeral"])
            self.assertEqual(self.labels(warning), ["Post original", "Back"])

            await warning.post_original(relay_interaction(channel=event.channel))
            await warning.post_original(relay_interaction(channel=event.channel))
            await view.confirm(relay_interaction(channel=event.channel))
            self.assertEqual(len(event.channel.sent), 1)
            public = event.channel.sent[0]
            self.assertEqual(file_bytes(public), self.dirty)
            self.assertEqual(public["file"].filename, "grim-announce.jpg")
            self.assertEqual(public["embed"].to_dict(), preview["embeds"][0].to_dict())
            rendered = repr(public)
            for marker in ("Noct", "10>", "noct-private-photo"):
                self.assertNotIn(marker, rendered)
            self.assertTrue(any(
                call.args[2] == "posted_original" for call in self.audit_mock.call_args_list if len(call.args) > 2
            ))

        asyncio.run(run())

    def test_back_returns_without_posting_and_confirm_still_posts_the_stripped_file(self):
        async def run():
            event, preview, view = await self.preview()
            _, _, warning = await self.open_warning(event, view)
            back = relay_interaction(channel=event.channel)
            await warning.back(back)
            self.assertEqual(event.channel.sent, [])
            self.assertIn("Nothing was posted", back.response.edits[0]["content"])
            self.assertIsNone(back.response.edits[0]["view"])
            await warning.post_original(relay_interaction(channel=event.channel))
            self.assertEqual(event.channel.sent, [])

            await view.confirm(relay_interaction(channel=event.channel))
            self.assertEqual(file_bytes(event.channel.sent[0]), self.cleaned)

        asyncio.run(run())

    def test_post_original_button_is_hidden_when_nothing_was_stripped(self):
        async def run():
            _, _, dirty_view = await self.preview()
            self.assertEqual(self.labels(dirty_view), ["Confirm", "Cancel", "Post original…"])

            _, _, clean_view = await self.preview(image_bytes=self.cleaned)
            self.assertEqual(self.labels(clean_view), ["Confirm", "Cancel"])

            event, _, _ = await self.preview()
            image_free = relay_interaction()
            with patch.object(main, "_post_xai_chat", AsyncMock(return_value="hi")):
                await main.say.callback(image_free, "hi", None)
            view = [kw["view"] for _, kw, _ in image_free.followup.sent if "view" in kw][0]
            self.assertEqual(self.labels(view), ["Confirm", "Cancel"])
            press = relay_interaction(channel=image_free.channel)
            await view.ask_original(press)
            self.assertEqual(press.followup.sent, [])
            self.assertEqual(image_free.channel.sent, [])

        asyncio.run(run())

    def test_button_styles_keep_confirm_primary_and_original_quiet(self):
        async def run():
            _, _, view = await self.preview()
            styles = {child.label: child.style for child in view.children}
            self.assertEqual(styles["Confirm"], discord.ButtonStyle.primary)
            self.assertEqual(styles["Cancel"], discord.ButtonStyle.danger)
            self.assertEqual(styles["Post original…"], discord.ButtonStyle.secondary)

        asyncio.run(run())

    def test_each_button_checks_invoker_and_administrator_again(self):
        async def run():
            event, _, view = await self.preview()
            stranger = relay_interaction(user_id=99, channel=event.channel)
            self.assertFalse(await view.interaction_check(stranger))

            demoted = relay_interaction(admin=False, channel=event.channel)
            self.assertFalse(await view.interaction_check(demoted))
            await view.ask_original(relay_interaction(admin=False, channel=event.channel))

            _, _, warning = await self.open_warning(event, view)
            self.assertFalse(await warning.interaction_check(relay_interaction(user_id=99, channel=event.channel)))
            self.assertFalse(await warning.interaction_check(relay_interaction(admin=False, channel=event.channel)))
            await warning.post_original(relay_interaction(admin=False, channel=event.channel))
            await warning.back(relay_interaction(admin=False, channel=event.channel))
            self.assertEqual(event.channel.sent, [])
            self.assertTrue(await warning.interaction_check(relay_interaction(channel=event.channel)))

        asyncio.run(run())

    def test_warning_is_dead_once_the_preview_is_handled_or_expires(self):
        async def run():
            event, _, view = await self.preview()
            _, _, warning = await self.open_warning(event, view)
            await view.confirm(relay_interaction(channel=event.channel))
            stale = relay_interaction(channel=event.channel)
            await warning.post_original(stale)
            self.assertIn("Already handled", stale.response.messages[0][0])
            self.assertEqual(len(event.channel.sent), 1)
            self.assertEqual(file_bytes(event.channel.sent[0]), self.cleaned)

            event, _, view = await self.preview()
            _, _, warning = await self.open_warning(event, view)
            await warning.on_timeout()
            warning.message.edit.assert_awaited_once()
            self.assertIn("Nothing was posted", warning.message.edit.await_args.kwargs["content"])
            await view.on_timeout()
            await warning.post_original(relay_interaction(channel=event.channel))
            self.assertEqual(event.channel.sent, [])

        asyncio.run(run())

    def test_original_post_failure_is_reported_and_posts_nothing(self):
        async def run():
            channel = RelayChannel(error=discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no"))
            event = relay_interaction(channel=channel)
            with patch.object(main, "_post_xai_chat", AsyncMock(return_value="hi")):
                await main.say.callback(event, "hi", attachment(self.dirty))
            view = [kw["view"] for _, kw, _ in event.followup.sent if "view" in kw][0]
            _, _, warning = await self.open_warning(event, view)
            press = relay_interaction(channel=channel)
            await warning.post_original(press)
            self.assertIn("Nothing was posted", press.followup.sent[-1][0])
            self.assertEqual(channel.sent, [])

        asyncio.run(run())


class RelayAcknowledgeFirstTests(unittest.TestCase):
    """Discord drops an interaction that isn't acknowledged within three seconds."""

    def setUp(self):
        main._COMMAND_RATE_LIMITS.clear()
        self.log = []
        self.audit = patch.object(
            main, "record_security_event", side_effect=lambda *a, **k: self.log.append("audit")
        )
        self.audit.start()

    def tearDown(self):
        self.audit.stop()
        main._COMMAND_RATE_LIMITS.clear()

    def event(self, **overrides):
        event = relay_interaction(**overrides)
        real_defer = event.response.defer

        async def defer(**kwargs):
            self.log.append("defer")
            await real_defer(**kwargs)

        event.response.defer = defer
        return event

    def slow_attachment(self):
        data = dirty_jpeg()
        item = attachment(data)

        async def read():
            self.log.append("download")
            await asyncio.sleep(0.01)
            return data

        item.read = read
        return item

    async def run_command(self, command, event, text, image):
        real_clean = main.clean_image_report

        def clean(data):
            self.log.append("clean")
            return real_clean(data)

        async def model(payload):
            self.log.append("model")
            await asyncio.sleep(0.01)
            return "hi"

        with patch.object(main, "clean_image_report", clean), patch.object(main, "_post_xai_chat", model):
            await command.callback(event, text, image)

    def test_defer_is_the_first_thing_that_happens_for_both_commands(self):
        async def run():
            for command in (main.say, main.announce):
                self.log.clear()
                event = self.event()
                await self.run_command(command, event, "hi", self.slow_attachment())
                self.assertEqual(self.log[0], "defer")
                self.assertEqual(self.log.count("defer"), 1)
                for step in ("download", "clean", "model"):
                    self.assertIn(step, self.log)
                    self.assertGreater(self.log.index(step), 0)
                self.assertTrue(event.response.deferred[0]["ephemeral"])
                self.assertEqual(event.response.messages, [])
                self.assertTrue(any(
                    isinstance(kwargs.get("view"), post_preview.PreviewView)
                    for _, kwargs, _ in event.followup.sent
                ))

        asyncio.run(run())

    def test_text_only_defers_before_the_model_call(self):
        async def run():
            event = self.event()
            await self.run_command(main.say, event, "hi", None)
            self.assertEqual(self.log[0], "defer")
            self.assertLess(self.log.index("defer"), self.log.index("model"))

        asyncio.run(run())

    def test_denials_and_rejections_still_acknowledge_first_and_do_no_work(self):
        async def run():
            self.log.clear()
            event = self.event(admin=False)
            await self.run_command(main.say, event, "hi", self.slow_attachment())
            self.assertEqual(self.log[0], "defer")
            for step in ("download", "clean", "model"):
                self.assertNotIn(step, self.log)
            self.assertIn("Only server administrators", event.followup.sent[0][0])
            self.assertEqual(event.channel.sent, [])

            self.log.clear()
            event = self.event()
            await self.run_command(main.announce, event, None, None)
            self.assertEqual(self.log[0], "defer")
            self.assertEqual(event.response.messages, [])

        asyncio.run(run())

    def test_an_unexpected_failure_after_the_defer_is_reported_not_left_hanging(self):
        async def run():
            event = self.event()
            with patch.object(main, "_rewrite_relay_text", AsyncMock(side_effect=RuntimeError("boom"))):
                await main.say.callback(event, "hi", None)
            self.assertEqual(self.log[0], "defer")
            note, kwargs, _ = event.followup.sent[-1]
            self.assertIn("Nothing was posted", note)
            self.assertTrue(kwargs["ephemeral"])
            self.assertEqual(event.channel.sent, [])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
