import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

import delete_report
import main

FIXTURE_FOUNDER_ID = 101010101010101010
OTHER_USER_ID = 202020202020202020
SECRET_CONTENT = "do-not-log-this-deleted-line"


def forbidden_error():
    return discord.Forbidden(
        SimpleNamespace(status=403, reason="Forbidden"),
        {"code": 50007, "message": "Cannot send messages to this user"},
    )


class FakeAsyncAudit:
    def __init__(self, entries):
        self._entries = list(entries)

    def __aiter__(self):
        self._iter = iter(self._entries)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as error:
            raise StopAsyncIteration from error


class DeleteReportTests(unittest.TestCase):
    def setUp(self):
        delete_report.reset_delete_report_state()
        self.logs = []
        self.log_patch = patch.object(
            delete_report, "_log", side_effect=self.logs.append
        )
        self.sleep_patch = patch.object(
            delete_report.asyncio, "sleep", new_callable=AsyncMock
        )
        self.log_patch.start()
        self.sleep_patch.start()

    def tearDown(self):
        self.log_patch.stop()
        self.sleep_patch.stop()
        delete_report.reset_delete_report_state()
        for key in (delete_report.FOUNDER_ID_ENV, delete_report.FOUNDER_ID_ENV_ALIAS):
            os.environ.pop(key, None)

    def founder_user(self):
        user = SimpleNamespace(
            id=FIXTURE_FOUNDER_ID,
            name="FounderFixture",
            display_name="FounderFixture",
            send=AsyncMock(),
        )
        return user

    def other_user(self):
        return SimpleNamespace(
            id=OTHER_USER_ID,
            name="Staff",
            send=AsyncMock(),
        )

    def fake_bot(self, founder, *, guild=None, channel=None):
        channel = channel or SimpleNamespace(
            id=777,
            mention="<#777>",
            name="general",
            send=AsyncMock(),
            guild=guild,
        )
        if guild is None:
            guild = SimpleNamespace(
                id=50,
                name="Fixture Server",
                audit_logs=lambda **kwargs: FakeAsyncAudit([]),
            )
            channel.guild = guild
        users = {founder.id: founder}
        return SimpleNamespace(
            get_user=lambda user_id: users.get(int(user_id)),
            fetch_user=AsyncMock(side_effect=lambda user_id: users.get(int(user_id))),
            get_channel=lambda channel_id: channel if int(channel_id) == int(channel.id) else None,
            get_guild=lambda guild_id: guild if int(guild_id) == int(guild.id) else None,
            channel=channel,
            guild=guild,
        )

    def cached_message(self, *, content=SECRET_CONTENT, author_id=303):
        guild = SimpleNamespace(id=50, name="Fixture Server")
        channel = SimpleNamespace(id=777, mention="<#777>", name="general", guild=guild)
        return SimpleNamespace(
            id=9001,
            content=content,
            created_at=datetime(2026, 11, 13, 20, 16, tzinfo=timezone.utc),
            author=SimpleNamespace(id=author_id, name="Noct", display_name="Noct"),
            attachments=[SimpleNamespace(filename="proof.png")],
            stickers=[],
            guild=guild,
            channel=channel,
        )

    def test_founder_id_reads_primary_env_and_alias(self):
        with patch.dict(os.environ, {delete_report.FOUNDER_ID_ENV: str(FIXTURE_FOUNDER_ID)}, clear=False):
            self.assertEqual(delete_report.founder_discord_id(), FIXTURE_FOUNDER_ID)
        os.environ.pop(delete_report.FOUNDER_ID_ENV, None)
        with patch.dict(
            os.environ,
            {delete_report.FOUNDER_ID_ENV_ALIAS: str(FIXTURE_FOUNDER_ID)},
            clear=False,
        ):
            self.assertEqual(delete_report.founder_discord_id(), FIXTURE_FOUNDER_ID)

    def test_missing_or_invalid_founder_id_disables_reports(self):
        os.environ.pop(delete_report.FOUNDER_ID_ENV, None)
        os.environ.pop(delete_report.FOUNDER_ID_ENV_ALIAS, None)
        self.assertIsNone(delete_report.founder_discord_id())
        with patch.dict(os.environ, {delete_report.FOUNDER_ID_ENV: "not-an-id"}):
            self.assertIsNone(delete_report.founder_discord_id())
        self.assertTrue(any("invalid" in line for line in self.logs))
        self.assertFalse(any("not-an-id" in line for line in self.logs))

    def test_uncached_delete_says_actor_and_content_unknown(self):
        snapshot = {
            "message_id": 9001,
            "author_id": None,
            "author_name": None,
            "content": None,
            "created_at": None,
            "attachments": [],
            "stickers": [],
        }
        self.assertEqual(delete_report.format_content_block(snapshot), "content unknown")
        self.assertEqual(delete_report.format_actor_label(None), "actor unknown")

    def test_embed_includes_channel_actor_and_posted_content(self):
        snapshot = delete_report.snapshot_from_message(self.cached_message())
        embeds = delete_report.build_delete_report_embeds(
            channel_text="<#777> in Fixture Server",
            actor_label="actor unknown",
            snapshots=[snapshot],
        )
        self.assertEqual(len(embeds), 1)
        embed = embeds[0]
        self.assertEqual(embed.title, "Deleted message")
        self.assertIn(SECRET_CONTENT, embed.description)
        self.assertIn("proof.png", embed.description)
        self.assertIn("Noct", embed.description)
        self.assertEqual(embed.fields[0].name, "Channel")
        self.assertIn("#777", embed.fields[0].value)
        self.assertEqual(embed.fields[1].value, "actor unknown")
        self.assertEqual(embed.footer.text, "Grim")

    def test_single_delete_dms_only_the_founder(self):
        async def run():
            founder = self.founder_user()
            other = self.other_user()
            bot = self.fake_bot(founder)
            payload = SimpleNamespace(
                guild_id=50,
                channel_id=777,
                message_id=9001,
                cached_message=self.cached_message(),
            )
            with patch.dict(os.environ, {delete_report.FOUNDER_ID_ENV: str(FIXTURE_FOUNDER_ID)}):
                await delete_report.handle_raw_message_delete(bot, payload)

            founder.send.assert_awaited_once()
            other.send.assert_not_awaited()
            bot.channel.send.assert_not_awaited()
            sent_embed = founder.send.await_args.kwargs["embed"]
            self.assertEqual(sent_embed.title, "Deleted message")
            self.assertIn(SECRET_CONTENT, sent_embed.description)
            self.assertEqual(sent_embed.fields[1].value, "actor unknown")
            self.assertFalse(any(SECRET_CONTENT in line for line in self.logs))

        asyncio.run(run())

    def test_unset_founder_id_does_not_dm_or_post(self):
        async def run():
            founder = self.founder_user()
            bot = self.fake_bot(founder)
            payload = SimpleNamespace(
                guild_id=50,
                channel_id=777,
                message_id=9001,
                cached_message=self.cached_message(),
            )
            os.environ.pop(delete_report.FOUNDER_ID_ENV, None)
            os.environ.pop(delete_report.FOUNDER_ID_ENV_ALIAS, None)
            await delete_report.handle_raw_message_delete(bot, payload)
            founder.send.assert_not_awaited()
            bot.channel.send.assert_not_awaited()

        asyncio.run(run())

    def test_dm_delete_is_ignored(self):
        async def run():
            founder = self.founder_user()
            bot = self.fake_bot(founder)
            payload = SimpleNamespace(
                guild_id=None,
                channel_id=888,
                message_id=9001,
                cached_message=self.cached_message(),
            )
            with patch.dict(os.environ, {delete_report.FOUNDER_ID_ENV: str(FIXTURE_FOUNDER_ID)}):
                await delete_report.handle_raw_message_delete(bot, payload)
            founder.send.assert_not_awaited()

        asyncio.run(run())

    def test_closed_dms_fail_quietly_without_content_or_channel_fallback(self):
        async def run():
            founder = self.founder_user()
            founder.send = AsyncMock(side_effect=forbidden_error())
            bot = self.fake_bot(founder)
            payload = SimpleNamespace(
                guild_id=50,
                channel_id=777,
                message_id=9001,
                cached_message=self.cached_message(),
            )
            with patch.dict(os.environ, {delete_report.FOUNDER_ID_ENV: str(FIXTURE_FOUNDER_ID)}):
                await delete_report.handle_raw_message_delete(bot, payload)
            founder.send.assert_awaited()
            bot.channel.send.assert_not_awaited()
            self.assertTrue(any("no channel fallback" in line for line in self.logs))
            self.assertFalse(any(SECRET_CONTENT in line for line in self.logs))
            self.assertFalse(any("proof.png" in line for line in self.logs))

        asyncio.run(run())

    def test_actor_from_audit_log_is_named_in_the_dm(self):
        async def run():
            founder = self.founder_user()
            actor = SimpleNamespace(id=404, name="Mod", display_name="Mod")
            entry = SimpleNamespace(
                user=actor,
                target=SimpleNamespace(id=303),
                extra=SimpleNamespace(channel=SimpleNamespace(id=777), count=1),
                created_at=datetime.now(timezone.utc),
            )
            guild = SimpleNamespace(
                id=50,
                name="Fixture Server",
                audit_logs=lambda **kwargs: FakeAsyncAudit([entry]),
            )
            bot = self.fake_bot(founder, guild=guild)
            payload = SimpleNamespace(
                guild_id=50,
                channel_id=777,
                message_id=9001,
                cached_message=self.cached_message(),
            )
            with patch.dict(os.environ, {delete_report.FOUNDER_ID_ENV: str(FIXTURE_FOUNDER_ID)}):
                await delete_report.handle_raw_message_delete(bot, payload)
            sent_embed = founder.send.await_args.kwargs["embed"]
            self.assertIn("Mod", sent_embed.fields[1].value)
            self.assertIn("<@404>", sent_embed.fields[1].value)

        asyncio.run(run())

    def test_seen_cache_supplies_content_when_raw_event_has_no_cached_message(self):
        async def run():
            founder = self.founder_user()
            bot = self.fake_bot(founder)
            delete_report.remember_guild_message(self.cached_message())
            payload = SimpleNamespace(
                guild_id=50,
                channel_id=777,
                message_id=9001,
                cached_message=None,
            )
            with patch.dict(os.environ, {delete_report.FOUNDER_ID_ENV: str(FIXTURE_FOUNDER_ID)}):
                await delete_report.handle_raw_message_delete(bot, payload)
            sent_embed = founder.send.await_args.kwargs["embed"]
            self.assertIn(SECRET_CONTENT, sent_embed.description)

        asyncio.run(run())

    def test_bulk_delete_dms_one_report_to_the_founder(self):
        async def run():
            founder = self.founder_user()
            bot = self.fake_bot(founder)
            first = self.cached_message()
            second = self.cached_message()
            second.id = 9002
            second.content = "second vanished line"
            payload = SimpleNamespace(
                guild_id=50,
                channel_id=777,
                message_ids=[9001, 9002],
                cached_messages=[first, second],
            )
            with patch.dict(os.environ, {delete_report.FOUNDER_ID_ENV: str(FIXTURE_FOUNDER_ID)}):
                await delete_report.handle_raw_bulk_message_delete(bot, payload)
            founder.send.assert_awaited_once()
            bot.channel.send.assert_not_awaited()
            sent_embed = founder.send.await_args.kwargs["embed"]
            self.assertEqual(sent_embed.title, "Deleted messages (2)")
            self.assertIn(SECRET_CONTENT, sent_embed.description)
            self.assertIn("second vanished line", sent_embed.description)

        asyncio.run(run())

    def test_db_lookup_is_used_when_discord_cache_misses(self):
        async def run():
            founder = self.founder_user()
            bot = self.fake_bot(founder)

            def db_lookup(message_id):
                self.assertEqual(message_id, 9001)
                return {
                    "message_id": 9001,
                    "guild_id": 50,
                    "channel_id": 777,
                    "author_id": None,
                    "author_name": "Noct",
                    "content": SECRET_CONTENT,
                    "created_at": None,
                    "attachments": [],
                    "stickers": [],
                }

            payload = SimpleNamespace(
                guild_id=50,
                channel_id=777,
                message_id=9001,
                cached_message=None,
            )
            with patch.dict(os.environ, {delete_report.FOUNDER_ID_ENV: str(FIXTURE_FOUNDER_ID)}):
                await delete_report.handle_raw_message_delete(bot, payload, db_lookup)
            sent_embed = founder.send.await_args.kwargs["embed"]
            self.assertIn(SECRET_CONTENT, sent_embed.description)
            self.assertIn("Noct", sent_embed.description)

        asyncio.run(run())

    def test_history_lookup_does_not_log_message_bodies(self):
        original_db = main.CHAT_DB_FILE
        temp_dir = tempfile.TemporaryDirectory()
        try:
            main.CHAT_DB_FILE = str(Path(temp_dir.name) / "chat_history.db")
            main.init_chat_db()
            main.save_message_to_db(
                "50",
                "777",
                "9001",
                "Noct",
                SECRET_CONTENT,
                datetime(2026, 11, 13, tzinfo=timezone.utc).timestamp(),
            )
            snapshot = main.lookup_deleted_message_from_db(9001)
            self.assertEqual(snapshot["content"], SECRET_CONTENT)
            self.assertEqual(snapshot["author_name"], "Noct")
        finally:
            main.CHAT_DB_FILE = original_db
            temp_dir.cleanup()

    def test_message_content_intent_and_raw_listeners_are_wired(self):
        self.assertTrue(main.intents.message_content)
        self.assertEqual(main.bot._connection.max_messages, 10000)
        self.assertTrue(hasattr(main, "on_raw_message_delete"))
        self.assertTrue(hasattr(main, "on_raw_bulk_message_delete"))
        self.assertEqual(main.panopticon.name, "panopticon")

    def test_is_founder_user_uses_env_id_not_creator_constant(self):
        with patch.dict(os.environ, {delete_report.FOUNDER_ID_ENV: str(FIXTURE_FOUNDER_ID)}):
            self.assertTrue(delete_report.is_founder_user(FIXTURE_FOUNDER_ID))
            self.assertTrue(delete_report.is_founder_user(str(FIXTURE_FOUNDER_ID)))
            self.assertFalse(delete_report.is_founder_user(OTHER_USER_ID))
            self.assertFalse(delete_report.is_founder_user(main.CREATOR_DISCORD_ID))
        os.environ.pop(delete_report.FOUNDER_ID_ENV, None)
        self.assertFalse(delete_report.is_founder_user(FIXTURE_FOUNDER_ID))
        self.assertFalse(delete_report.is_founder_user(main.CREATOR_DISCORD_ID))

    def test_panopticon_defaults_on_and_toggles_off_then_on(self):
        self.assertTrue(delete_report.founder_delete_dms_enabled())
        self.assertFalse(delete_report.toggle_founder_delete_dms())
        self.assertFalse(delete_report.founder_delete_dms_enabled())
        self.assertTrue(delete_report.toggle_founder_delete_dms())
        self.assertTrue(delete_report.founder_delete_dms_enabled())

    def test_panopticon_off_does_not_dm_single_or_bulk_deletes(self):
        async def run():
            founder = self.founder_user()
            bot = self.fake_bot(founder)
            delete_report.toggle_founder_delete_dms()
            self.assertFalse(delete_report.founder_delete_dms_enabled())
            payload = SimpleNamespace(
                guild_id=50,
                channel_id=777,
                message_id=9001,
                cached_message=self.cached_message(),
            )
            bulk = SimpleNamespace(
                guild_id=50,
                channel_id=777,
                message_ids=[9001, 9002],
                cached_messages=[self.cached_message()],
            )
            with patch.dict(os.environ, {delete_report.FOUNDER_ID_ENV: str(FIXTURE_FOUNDER_ID)}):
                await delete_report.handle_raw_message_delete(bot, payload)
                await delete_report.handle_raw_bulk_message_delete(bot, bulk)
            founder.send.assert_not_awaited()
            bot.channel.send.assert_not_awaited()
            self.assertFalse(any(SECRET_CONTENT in line for line in self.logs))

        asyncio.run(run())

    def test_panopticon_on_resumes_founder_dms(self):
        async def run():
            founder = self.founder_user()
            bot = self.fake_bot(founder)
            self.assertFalse(delete_report.toggle_founder_delete_dms())
            self.assertTrue(delete_report.toggle_founder_delete_dms())
            payload = SimpleNamespace(
                guild_id=50,
                channel_id=777,
                message_id=9001,
                cached_message=self.cached_message(),
            )
            with patch.dict(os.environ, {delete_report.FOUNDER_ID_ENV: str(FIXTURE_FOUNDER_ID)}):
                await delete_report.handle_raw_message_delete(bot, payload)
            founder.send.assert_awaited_once()
            bot.channel.send.assert_not_awaited()
            sent_embed = founder.send.await_args.kwargs["embed"]
            self.assertIn(SECRET_CONTENT, sent_embed.description)

        asyncio.run(run())

    def test_panopticon_state_persists_off_and_on_across_reload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "panopticon.json")
            delete_report.configure_founder_delete_dms_state(path)
            self.assertTrue(delete_report.founder_delete_dms_enabled())
            self.assertFalse(os.path.exists(path))

            self.assertFalse(delete_report.toggle_founder_delete_dms())
            with open(path, "r") as handle:
                saved = json.load(handle)
            self.assertEqual(saved, {"enabled": False})
            self.assertNotIn(str(FIXTURE_FOUNDER_ID), json.dumps(saved))

            delete_report.reset_delete_report_state()
            delete_report.configure_founder_delete_dms_state(path)
            self.assertFalse(delete_report.founder_delete_dms_enabled())

            self.assertTrue(delete_report.toggle_founder_delete_dms())
            with open(path, "r") as handle:
                saved = json.load(handle)
            self.assertEqual(saved, {"enabled": True})

            delete_report.reset_delete_report_state()
            delete_report.configure_founder_delete_dms_state(path)
            self.assertTrue(delete_report.founder_delete_dms_enabled())

    def test_invalid_persisted_state_defaults_on(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "panopticon.json")
            Path(path).write_text("{not-json", encoding="utf-8")
            delete_report.configure_founder_delete_dms_state(path)
            self.assertTrue(delete_report.founder_delete_dms_enabled())

    def test_killswitch_off_status_log_omits_founder_id(self):
        delete_report.toggle_founder_delete_dms()
        with patch.dict(os.environ, {delete_report.FOUNDER_ID_ENV: str(FIXTURE_FOUNDER_ID)}):
            delete_report.log_founder_delete_report_status()
        self.assertTrue(any("killswitch" in line.lower() or "off" in line.lower() for line in self.logs))
        self.assertFalse(any(str(FIXTURE_FOUNDER_ID) in line for line in self.logs))

    def panopticon_event(self, user_id, *, administrator=False, owner_id=1):
        response = SimpleNamespace(messages=[], send_message=AsyncMock())

        async def send_message(*args, **kwargs):
            response.messages.append((args, kwargs))

        response.send_message.side_effect = send_message
        return SimpleNamespace(
            guild_id=50,
            guild=SimpleNamespace(id=50, owner_id=owner_id),
            user=SimpleNamespace(
                id=user_id,
                guild_permissions=SimpleNamespace(administrator=administrator),
            ),
            response=response,
            channel=SimpleNamespace(send=AsyncMock()),
        )

    def test_panopticon_command_founder_toggles_off_then_on(self):
        async def run():
            event = self.panopticon_event(FIXTURE_FOUNDER_ID)
            with (
                patch.dict(os.environ, {delete_report.FOUNDER_ID_ENV: str(FIXTURE_FOUNDER_ID)}),
                patch.object(main, "record_security_event") as recorded,
            ):
                await main.panopticon.callback(event)
                self.assertFalse(delete_report.founder_delete_dms_enabled())
                off_embed = event.response.send_message.await_args.kwargs["embed"]
                self.assertEqual(off_embed.title, "Panopticon Off")
                self.assertTrue(event.response.send_message.await_args.kwargs["ephemeral"])
                self.assertIn("/panopticon", off_embed.description)

                await main.panopticon.callback(event)
                self.assertTrue(delete_report.founder_delete_dms_enabled())
                on_embed = event.response.send_message.await_args.kwargs["embed"]
                self.assertEqual(on_embed.title, "Panopticon On")
                self.assertTrue(event.response.send_message.await_args.kwargs["ephemeral"])
                self.assertEqual(off_embed.footer.text, main.xsi_footer())
                self.assertEqual(on_embed.footer.text, main.xsi_footer())

            event.channel.send.assert_not_awaited()
            outcomes = [call.args[2] for call in recorded.call_args_list]
            self.assertIn("disabled", outcomes)
            self.assertIn("enabled", outcomes)
            self.assertFalse(any(str(FIXTURE_FOUNDER_ID) in line for line in self.logs))
            self.assertFalse(any(SECRET_CONTENT in line for line in self.logs))

        asyncio.run(run())

    def test_panopticon_command_rejects_non_founder_including_creator_constant(self):
        async def run():
            for user_id, kwargs in (
                (OTHER_USER_ID, {"administrator": True, "owner_id": OTHER_USER_ID}),
                (main.CREATOR_DISCORD_ID, {"administrator": True}),
            ):
                event = self.panopticon_event(user_id, **kwargs)
                with (
                    patch.dict(os.environ, {delete_report.FOUNDER_ID_ENV: str(FIXTURE_FOUNDER_ID)}),
                    patch.object(main, "record_security_event") as recorded,
                ):
                    await main.panopticon.callback(event)
                self.assertTrue(delete_report.founder_delete_dms_enabled())
                event.response.send_message.assert_awaited_once()
                args, send_kwargs = event.response.send_message.await_args
                self.assertIn("permission", args[0].lower())
                self.assertTrue(send_kwargs["ephemeral"])
                event.channel.send.assert_not_awaited()
                self.assertEqual(recorded.call_args.args[1], "panopticon")
                self.assertEqual(recorded.call_args.args[2], "denied")
                self.assertEqual(recorded.call_args.args[3]["reason"], "not_founder")

        asyncio.run(run())

    def test_panopticon_command_denies_everyone_when_founder_id_unset(self):
        async def run():
            event = self.panopticon_event(FIXTURE_FOUNDER_ID)
            os.environ.pop(delete_report.FOUNDER_ID_ENV, None)
            os.environ.pop(delete_report.FOUNDER_ID_ENV_ALIAS, None)
            with patch.object(main, "record_security_event") as recorded:
                await main.panopticon.callback(event)
            event.response.send_message.assert_awaited_once()
            self.assertTrue(event.response.send_message.await_args.kwargs["ephemeral"])
            self.assertTrue(delete_report.founder_delete_dms_enabled())
            self.assertEqual(recorded.call_args.args[2], "denied")
            self.assertEqual(recorded.call_args.args[3]["reason"], "founder_id_unset")
            self.assertFalse(any(str(FIXTURE_FOUNDER_ID) in line for line in self.logs))

        asyncio.run(run())

    def test_panopticon_command_accepts_founder_via_alias_env_in_dms(self):
        async def run():
            event = self.panopticon_event(FIXTURE_FOUNDER_ID)
            event.guild_id = None
            event.guild = None
            with (
                patch.dict(
                    os.environ,
                    {delete_report.FOUNDER_ID_ENV_ALIAS: str(FIXTURE_FOUNDER_ID)},
                    clear=False,
                ),
                patch.object(main, "record_security_event"),
            ):
                os.environ.pop(delete_report.FOUNDER_ID_ENV, None)
                await main.panopticon.callback(event)
            self.assertFalse(delete_report.founder_delete_dms_enabled())
            embed = event.response.send_message.await_args.kwargs["embed"]
            self.assertEqual(embed.title, "Panopticon Off")
            self.assertTrue(event.response.send_message.await_args.kwargs["ephemeral"])

        asyncio.run(run())

    def test_panopticon_command_source_gates_only_on_env_founder_id(self):
        source = Path(main.__file__).read_text()
        start = source.index("async def panopticon(")
        end = source.index("@bot.tree.command(name=\"memberlog\"", start)
        block = source[start:end]
        self.assertIn("require_founder(", block)
        self.assertNotIn("is_grim_creator", block)
        self.assertNotIn("CREATOR_DISCORD_ID", block)
        self.assertNotIn("require_permission", block)
        self.assertNotIn("require_opsec", block)
        helper_start = source.index("async def require_founder(")
        helper_end = source.index("async def require_external_action(", helper_start)
        helper = source[helper_start:helper_end]
        self.assertIn("is_founder_user(", helper)
        self.assertNotIn("is_grim_creator", helper)
        self.assertNotIn("CREATOR_DISCORD_ID", helper)
        self.assertNotIn("administrator", helper)
        self.assertNotIn("owner_id", helper)


if __name__ == "__main__":
    unittest.main()
