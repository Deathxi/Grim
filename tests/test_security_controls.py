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
        self.original_outage_state_file = main.OUTAGE_REPORT_STATE_FILE
        main.CHAT_DB_FILE = str(Path(self.temp_dir.name) / "chat_history.db")
        main.MODERATION_FILE = str(Path(self.temp_dir.name) / "moderation_data.json")
        main.MEMBER_LOG_CHANNELS_FILE = str(Path(self.temp_dir.name) / "member_log_channels.json")
        main.OUTAGE_REPORT_STATE_FILE = str(Path(self.temp_dir.name) / "outage_report_state.json")
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
        main.OUTAGE_REPORT_STATE_FILE = self.original_outage_state_file
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
        self.assertEqual(embed.title, "Test Server - Member Departed")
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
        self.assertIn("language", slash_names)
        self.assertIn("grim_language", slash_names)
        self.assertIn("quote", prefix_names)
        self.assertNotIn("info", slash_names)
        self.assertNotIn("info-server", slash_names)
        self.assertNotIn("info-members", slash_names)
        self.assertNotIn("grim_members", slash_names)
        self.assertNotIn("grim_memberlog", slash_names)
        self.assertIn("server", prefix_names)
        self.assertNotIn("info", prefix_names)

    def test_quote_card_uses_regular_24px_text_and_png_output(self):
        created = main.datetime(2026, 8, 27, tzinfo=main.timezone.utc)
        card = main._render_quote_card("the short quote", "Noct", None, created)
        self.assertTrue(card.startswith(b"\x89PNG\r\n\x1a\n"))
        image = main.Image.open(main.BytesIO(card))
        self.assertEqual(image.width, 900)
        self.assertGreaterEqual(image.height, 210)

    def test_outage_report_calculates_previous_session_and_pacific_times(self):
        recovered_at = 1_788_000_120.0
        previous = {
            "session_id": "previous-session",
            "last_seen": recovered_at - 120,
            "status": "online",
        }
        outage = main._build_process_outage(previous, recovered_at)
        self.assertEqual(outage["kind"], "Unexpected outage")
        self.assertEqual(outage["duration"], 120)

        embed = main._build_outage_report_embed(outage)
        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(fields["Approx. Downtime"], "`2m`")
        self.assertRegex(fields["Offline From"], r"P(?:S|D)T")
        self.assertRegex(fields["Back Online"], r"P(?:S|D)T")
        self.assertIsNone(
            main._build_process_outage(
                {"session_id": main.RUNTIME_SESSION_ID, "last_seen": recovered_at - 60},
                recovered_at,
            )
        )

    def test_outage_reports_are_persistently_deduplicated(self):
        outage = {
            "started_at": 1_788_000_000.0,
            "recovered_at": 1_788_000_120.0,
            "duration": 120.0,
            "kind": "Unexpected outage",
        }
        report_id = main._queue_outage_report(outage)
        self.assertIsNotNone(report_id)
        self.assertEqual(main._queue_outage_report(outage), report_id)

        main._mark_outage_channel_delivered(report_id, "777")
        state = main._load_outage_report_state()
        self.assertEqual(
            state["pending"][report_id]["delivered_channels"],
            ["777"],
        )

        main._finish_outage_report(report_id)
        self.assertIsNone(main._queue_outage_report(outage))
        self.assertEqual(main._load_pending_outage_reports(), [])

    def test_patch_notes_extract_only_new_changelog_bullets(self):
        patch = """@@ -1,2 +1,4 @@
 ## Fixed
-old note
+- new addition
+  - nested addition
 context
+++ b/CHANGELOG.md
"""
        notes = main._extract_added_changelog_notes(patch)
        self.assertEqual(notes, ["new addition", "nested addition"])
        self.assertEqual(
            main._format_patch_notes(notes),
            "• new addition\n• nested addition",
        )

    def test_creator_saved_language_preference_is_honored(self):
        guild_id = "50"
        creator_id = str(main.CREATOR_DISCORD_ID)
        main.save_member_language_preference(guild_id, creator_id, "spanish")

        instruction = main.get_language_reply_instruction(guild_id, creator_id)

        self.assertIn("Spanish", instruction)
        self.assertIn("Reply entirely in that language", instruction)
        self.assertNotIn("regardless of any older saved", instruction)

        main.clear_member_language_preference(guild_id, creator_id)
        auto_instruction = main.get_language_reply_instruction(guild_id, creator_id)
        self.assertIn("Automatically identify the language", auto_instruction)

    def test_server_info_uses_grim_layout_and_aggregate_language_data(self):
        main.save_member_language_preference("50", "10", "english")
        main.save_member_language_preference("50", "11", "english")
        main.save_member_language_preference("50", "12", "spanish")
        preferences = main.get_guild_language_preferences("50")
        self.assertEqual(preferences, [("english", 2), ("spanish", 1)])

        class FakeEmoji:
            def __init__(self, name, emoji_id):
                self.name = name
                self.id = emoji_id
                self.animated = True

            def __str__(self):
                return f"<a:{self.name}:{self.id}>"

        created = main.datetime(2023, 4, 18, 2, 43, tzinfo=main.timezone.utc)
        guild = SimpleNamespace(
            id=1101443658953261076,
            name="𝕾𝖊𝖈𝖑𝖚𝖉𝖊",
            description="A quiet place for the strange and unguarded.",
            owner=SimpleNamespace(mention="<@235194449573969920>"),
            owner_id=235194449573969920,
            verification_level=SimpleNamespace(name="medium"),
            created_at=created,
            member_count=50,
            members=[
                SimpleNamespace(status=main.discord.Status.online),
                SimpleNamespace(status=main.discord.Status.offline),
            ],
            text_channels=[object()] * 22,
            voice_channels=[object()] * 4,
            roles=[object()] * 24,
            premium_subscription_count=3,
            preferred_locale="en-US",
            emojis=[FakeEmoji(f"emoji{i}", 151245000000000000 + i) for i in range(65)],
            icon=SimpleNamespace(url="https://cdn.example/icon.png"),
            banner=SimpleNamespace(url="https://cdn.example/banner.png"),
        )
        embed = main.build_server_info_embed(
            guild, language_preferences=preferences, bot_latency=42
        )
        fields = {field.name: field for field in embed.fields}

        self.assertEqual(embed.title, "𖦏 𝕾𝖊𝖈𝖑𝖚𝖉𝖊")
        self.assertNotIn("author", embed.to_dict())
        self.assertEqual(embed.thumbnail.url, "https://cdn.example/icon.png")
        self.assertEqual(embed.image.url, "https://cdn.example/banner.png")
        self.assertTrue(
            embed.description.startswith("A quiet place for the strange and unguarded.")
        )
        self.assertFalse(fields["✧ Server ID"].inline)
        self.assertEqual(fields["✧ Text Channels"].value, "`22`")
        self.assertEqual(fields["✧ Voice Channels"].value, "`4`")
        self.assertEqual(fields["✧ Grim Ping"].value, "`42 ms`")
        self.assertIn("English 🇺🇸 · `2`", fields["✧ Language Signal"].value)
        self.assertIn("Spanish 🇪🇸 · `1`", fields["✧ Language Signal"].value)
        self.assertIn("✧ Emojis · 65", embed.description)
        self.assertNotIn("Server Emblems", embed.description)
        emoji_tokens = [
            token for token in embed.description.split() if token.startswith("<a:")
        ]
        self.assertEqual(len(emoji_tokens), 65)
        self.assertTrue(
            all(
                token.startswith("<a:") and token.endswith(">")
                for token in emoji_tokens
            )
        )
        self.assertIn("PST", fields["✧ Established"].value)
        self.assertIn("server info", embed.footer.text)

    def test_server_dossier_refreshes_a_missing_description(self):
        class FakeBot:
            async def fetch_guild(self, guild_id, with_counts=True):
                return SimpleNamespace(description="Fetched from the server profile.")

        original_bot = main.bot
        main.bot = FakeBot()
        try:
            description = asyncio.run(
                main.resolve_server_description(SimpleNamespace(id=50, description=None))
            )
        finally:
            main.bot = original_bot
        self.assertEqual(description, "Fetched from the server profile.")

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