import asyncio
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import main


class FakeResponse:
    def __init__(self):
        self.messages = []

    async def send_message(self, message, ephemeral=False):
        self.messages.append((message, ephemeral))

class FakeContext:
    def __init__(self, guild_id=50, user_id=10):
        self.guild = SimpleNamespace(id=guild_id)
        self.author = SimpleNamespace(id=user_id)
        self.messages = []

    async def send(self, message=None, **kwargs):
        self.messages.append(message if message is not None else kwargs)


def interaction(*, user_id=10, owner_id=1, administrator=False, manage_channels=False):
    permissions = SimpleNamespace(
        administrator=administrator,
        manage_channels=manage_channels,
        move_members=False,
    )
    return SimpleNamespace(
        guild_id=50,
        guild=SimpleNamespace(owner_id=owner_id),
        user=SimpleNamespace(id=user_id, guild_permissions=permissions),
        response=FakeResponse(),
    )


class SecurityControlsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db = main.CHAT_DB_FILE
        self.original_moderation_file = main.MODERATION_FILE
        self.original_moderation_data = main.moderation_data
        self.original_member_log_file = main.MEMBER_LOG_CHANNELS_FILE
        self.original_member_log_channels = main.member_log_channels
        main.CHAT_DB_FILE = str(Path(self.temp_dir.name) / "chat_history.db")
        main.MODERATION_FILE = str(Path(self.temp_dir.name) / "moderation_data.json")
        main.MEMBER_LOG_CHANNELS_FILE = str(Path(self.temp_dir.name) / "member_log_channels.json")
        main.member_log_channels = {}
        main.moderation_data = {"guilds": {}, "legacy_unassigned_words": []}
        main._COMMAND_RATE_LIMITS.clear()
        main.init_chat_db()

    def fake_member(self, *, guild_id=50, member_id=10, name="Noct", display_name="Noct", roles=None):
        created = main.datetime(2025, 1, 1, tzinfo=main.timezone.utc)
        joined = main.datetime(2026, 1, 1, tzinfo=main.timezone.utc)
        return SimpleNamespace(
            id=member_id,
            guild=SimpleNamespace(id=guild_id, name="Test Server"),
            name=name,
            display_name=display_name,
            display_avatar=SimpleNamespace(url=f"https://cdn.example/{member_id}.png"),
            created_at=created,
            joined_at=joined,
            roles=roles or [
                SimpleNamespace(id=50, name="@everyone"),
                SimpleNamespace(id=51, name="Staff"),
            ],
            bot=False,
        )

    def tearDown(self):
        main.CHAT_DB_FILE = self.original_db
        main.MODERATION_FILE = self.original_moderation_file
        main.moderation_data = self.original_moderation_data
        main.MEMBER_LOG_CHANNELS_FILE = self.original_member_log_file
        main.member_log_channels = self.original_member_log_channels
        main._COMMAND_RATE_LIMITS.clear()
        self.temp_dir.cleanup()

    def test_audit_metadata_is_redacted(self):
        event = interaction(administrator=True)
        main.record_security_event(
            event,
            "github_diagnostic",
            "success",
            {"token": "must-not-appear", "message_content": "private", "status": 200},
        )
        with sqlite3.connect(main.CHAT_DB_FILE) as conn:
            metadata_json = conn.execute(
                "SELECT metadata_json FROM security_audit_events"
            ).fetchone()[0]
        metadata = json.loads(metadata_json)
        self.assertEqual(metadata, {"status": 200})
        self.assertNotIn("must-not-appear", metadata_json)

    def test_rate_limit_blocks_ninth_request(self):
        for _ in range(main.RATE_LIMIT_MAX_REQUESTS):
            self.assertTrue(main._rate_limit_allows_actor("50", "10", "external_ai"))
        self.assertFalse(main._rate_limit_allows_actor("50", "10", "external_ai"))

    def test_prefix_haiku_uses_shared_external_rate_limit(self):
        for _ in range(main.RATE_LIMIT_MAX_REQUESTS):
            main._rate_limit_allows_actor("50", "10", "external_ai")
        ctx = FakeContext()
        asyncio.run(main.haiku.callback(ctx))
        self.assertEqual(
            ctx.messages, ["Too many requests recently. Please try again in a minute."]
        )

    def test_version_migrates_old_counter_to_v2_without_skipping_baseline(self):
        original_count_file = main.VERSION_COUNT_FILE
        original_hash_file = main.MAIN_HASH_FILE
        original_version = main.VERSION
        original_cwd = Path.cwd()
        try:
            count_file = Path(self.temp_dir.name) / "version_count.txt"
            hash_file = Path(self.temp_dir.name) / "main_hash.txt"
            count_file.write_text("134")
            hash_file.write_text("old-code-hash")
            main.VERSION_COUNT_FILE = str(count_file)
            main.MAIN_HASH_FILE = str(hash_file)
            import os
            os.chdir(self.temp_dir.name)
            main.VERSION = main._load_version()
            main._bump_version()
            self.assertEqual(main.VERSION, "V2.00")
            self.assertEqual(count_file.read_text(), "200")
        finally:
            import os
            os.chdir(original_cwd)
            main.VERSION_COUNT_FILE = original_count_file
            main.MAIN_HASH_FILE = original_hash_file
            main.VERSION = original_version

    def test_current_version_does_not_regress_to_an_old_repository_snapshot(self):
        original_count_file = main.VERSION_COUNT_FILE
        original_cwd = Path.cwd()
        try:
            count_file = Path(self.temp_dir.name) / "version_count.txt"
            count_file.write_text("200")
            main.VERSION_COUNT_FILE = str(count_file)
            import os
            os.chdir(self.temp_dir.name)
            Path("version.txt").write_text("128")
            self.assertEqual(main.get_current_version(), "V2.00")
            count_file.write_text("245")
            self.assertEqual(main.get_current_version(), "V2.45")
        finally:
            import os
            os.chdir(original_cwd)
            main.VERSION_COUNT_FILE = original_count_file

    def test_grim_birthday_is_november_25(self):
        self.assertTrue(main.is_grim_birthday(main.datetime(2026, 11, 25)))
        self.assertFalse(main.is_grim_birthday(main.datetime(2026, 11, 24)))
        self.assertFalse(main.is_grim_birthday(main.datetime(2026, 11, 26)))

    def test_grim_clock_uses_pacific_timezone(self):
        current = main.get_grim_current_time()
        self.assertEqual(current.tzinfo, main.GRIM_TIMEZONE)
        self.assertIn(current.tzname(), {"PST", "PDT"})

    def test_grim_clock_always_identifies_pst(self):
        formatted = main.format_grim_current_time(main.datetime(2026, 8, 25, 12, 34))
        self.assertTrue(formatted.endswith(" PST"))
        self.assertNotIn(" PDT", formatted)

    def test_moderation_words_are_server_scoped(self):
        main.set_guild_banned_words("one", ["alpha"])
        main.set_guild_banned_words("two", ["beta"])
        self.assertEqual(main.get_guild_banned_words("one"), ["alpha"])
        self.assertEqual(main.get_guild_banned_words("two"), ["beta"])
        self.assertNotIn("alpha", main.get_guild_banned_words("two"))

    def test_regular_member_cannot_manage_channels(self):
        event = interaction()
        allowed = asyncio.run(
            main.require_permission(event, "manage_channels", "newsfeed_create")
        )
        self.assertFalse(allowed)
        self.assertTrue(event.response.messages)

    def test_moderator_can_manage_channels_but_not_administrator_controls(self):
        moderator = interaction(manage_channels=True)
        self.assertTrue(
            asyncio.run(
                main.require_permission(moderator, "manage_channels", "newsfeed_create")
            )
        )
        self.assertFalse(
            asyncio.run(
                main.require_permission(
                    moderator, "administrator", "moderation_add", administrator=True
                )
            )
        )

    def test_creator_identity_does_not_bypass_discord_admin_permission(self):
        original_creator_id = main.CREATOR_DISCORD_ID
        try:
            main.CREATOR_DISCORD_ID = 99
            creator_without_discord_permission = interaction(user_id=99)
            self.assertFalse(
                asyncio.run(
                    main.require_permission(
                        creator_without_discord_permission,
                        "administrator",
                        "memory_add",
                        administrator=True,
                    )
                )
            )
        finally:
            main.CREATOR_DISCORD_ID = original_creator_id

    def test_member_history_tracks_identity_departure_and_activity(self):
        member = self.fake_member()
        main.record_member_snapshot(member, "join")
        main.increment_member_message_count("50", "10")
        renamed = self.fake_member(name="noct", display_name="Noct Updated")
        main.record_member_snapshot(renamed, "identity_update", {"changes": ["username", "display_name"]})
        main.record_member_snapshot(renamed, "leave", present=False)

        record = main.get_member_record("50", "10")
        self.assertEqual(record["username"], "noct")
        self.assertEqual(record["display_name"], "Noct Updated")
        self.assertFalse(record["is_present"])
        self.assertEqual(record["message_count"], 1)
        self.assertEqual(record["role_names"], ["Staff"])
        self.assertIsNotNone(record["left_at"])
        self.assertEqual(
            [event["event_type"] for event in main.get_member_history_events("50", "10")],
            ["leave", "identity_update", "join"],
        )

    def test_member_directory_is_server_scoped_and_safe_reconciliation_does_not_infer_leaves(self):
        departed = self.fake_member(member_id=10)
        other_guild_member = self.fake_member(guild_id=51, member_id=10, display_name="Other Server")
        current = self.fake_member(member_id=11, display_name="Current")
        main.record_member_snapshot(departed, "join")
        main.record_member_snapshot(departed, "leave", present=False)
        main.record_member_snapshot(other_guild_member, "join")

        main.sync_member_directory([SimpleNamespace(id=50, members=[current])])

        self.assertFalse(main.get_member_record("50", "10")["is_present"])
        self.assertTrue(main.get_member_record("50", "11")["is_present"])
        self.assertEqual(len(main.get_member_directory_records("51")), 1)
        self.assertEqual(main.get_member_directory_records("51")[0]["display_name"], "Other Server")

    def test_member_departure_card_and_directory_view_are_minimal_and_navigable(self):
        member = self.fake_member()
        main.record_member_snapshot(member, "join")
        main.record_member_snapshot(member, "leave", present=False)
        record = main.get_member_record("50", "10")
        embed = main.build_member_departure_embed("Test Server", record)
        self.assertEqual(embed.title, "Member Departed")
        self.assertIn("Noct", embed.description)
        self.assertIn("Test Server", embed.description)
        self.assertIn("departure reason not provided by Discord", embed.footer.text)
        fields = {field.name: field.value for field in embed.fields}
        self.assertIn("Display name: `Noct`", fields["Identity"])
        self.assertIn("Username: `@Noct`", fields["Identity"])

        async def create_view():
            return main.MemberDirectoryView("50", "99", [record])

        view = asyncio.run(create_view())
        self.assertTrue(any(isinstance(child, main.MemberDirectorySelect) for child in view.children))
        view.selected_member_id = "10"
        view.refresh_items()
        labels = [getattr(child, "label", None) for child in view.children]
        self.assertIn("Back to members", labels)
        self.assertIn("Next member", labels)

    def test_member_departure_log_channel_must_be_private(self):
        default_role = object()
        guild = SimpleNamespace(default_role=default_role)
        public_channel = SimpleNamespace(
            permissions_for=lambda role: SimpleNamespace(view_channel=True)
        )
        private_channel = SimpleNamespace(
            permissions_for=lambda role: SimpleNamespace(view_channel=False)
        )
        self.assertFalse(main.is_private_member_log_channel(public_channel, guild))
        self.assertTrue(main.is_private_member_log_channel(private_channel, guild))

    def test_management_commands_use_the_new_names(self):
        slash_names = {command.name for command in main.bot.tree.get_commands()}
        prefix_names = {command.name for command in main.bot.commands}
        self.assertIn("server", slash_names)
        self.assertIn("members", slash_names)
        self.assertIn("memberlog", slash_names)
        self.assertNotIn("info", slash_names)
        self.assertNotIn("info-server", slash_names)
        self.assertNotIn("info-members", slash_names)
        self.assertNotIn("grim_members", slash_names)
        self.assertNotIn("grim_memberlog", slash_names)
        self.assertIn("server", prefix_names)
        self.assertNotIn("info", prefix_names)

    def test_member_log_test_card_is_marked_simulation_and_does_not_change_history(self):
        member = self.fake_member()
        main.record_member_snapshot(member, "join")
        before_events = main.get_member_history_events("50", "10")
        record = main.get_member_record("50", "10")
        embed = main.build_member_departure_embed(
            "Test Server",
            record,
            ["<t:1:f> → <t:2:f> (simulated)"],
            test_mode=True,
        )
        self.assertEqual(embed.title, "Member Departed · TEST")
        self.assertIn("TEST ONLY", embed.description)
        self.assertIn("SIMULATION ONLY", embed.footer.text)
        self.assertEqual(main.get_member_history_events("50", "10"), before_events)

    def test_member_directory_database_read_runs_off_the_interaction_loop(self):
        member = self.fake_member()
        main.record_member_snapshot(member, "join")
        record = main.get_member_record("50", "10")
        original_loader = main.get_member_directory_records
        worker_threads = []

        def delayed_loader(guild_id):
            worker_threads.append(threading.get_ident())
            time.sleep(0.01)
            return original_loader(guild_id)

        class DeferredInteraction:
            def __init__(self):
                self.edits = []

            async def edit_original_response(self, **kwargs):
                self.edits.append(kwargs)

        async def show_directory_with_delayed_read():
            view = main.MemberDirectoryView("50", "99", [record])
            interaction = DeferredInteraction()
            main.get_member_directory_records = delayed_loader
            try:
                event_loop_thread = threading.get_ident()
                await view.show_directory(interaction)
            finally:
                main.get_member_directory_records = original_loader
            return event_loop_thread, interaction.edits

        event_loop_thread, edits = asyncio.run(show_directory_with_delayed_read())
        self.assertTrue(worker_threads)
        self.assertNotEqual(worker_threads[0], event_loop_thread)
        self.assertTrue(edits)

    def test_departure_log_is_disabled_when_channel_becomes_public(self):
        member = self.fake_member()
        member.guild.default_role = object()
        main.record_member_snapshot(member, "join")
        main.member_log_channels["50"] = "777"
        sent_embeds = []
        visibility = {"public": False}
        channel = SimpleNamespace(
            guild=SimpleNamespace(id=50),
            permissions_for=lambda role: SimpleNamespace(view_channel=visibility["public"]),
        )
        self.assertTrue(main.is_private_member_log_channel(channel, member.guild))
        visibility["public"] = True

        async def send(*, embed):
            sent_embeds.append(embed)

        channel.send = send
        original_bot = main.bot
        main.bot = SimpleNamespace(
            get_channel=lambda channel_id: channel,
            fetch_channel=lambda channel_id: channel,
        )
        try:
            asyncio.run(main.send_member_departure_notification(member))
        finally:
            main.bot = original_bot

        self.assertEqual(sent_embeds, [])
        self.assertNotIn("50", main.member_log_channels)


if __name__ == "__main__":
    unittest.main()