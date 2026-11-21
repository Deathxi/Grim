import asyncio
import json
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path
from types import SimpleNamespace

import discord

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


class SlashResponse:
    def __init__(self):
        self.sent = []
        self.deferred = []

    def is_done(self):
        return bool(self.sent or self.deferred)

    async def send_message(self, *args, **kwargs):
        self.sent.append((args, kwargs))

    async def defer(self, **kwargs):
        self.deferred.append(kwargs)


class SlashFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


def slash_interaction(
    *,
    user_id=10,
    owner_id=1,
    administrator=False,
    manage_channels=False,
):
    permissions = SimpleNamespace(
        administrator=administrator,
        manage_channels=manage_channels,
        manage_messages=False,
    )
    return SimpleNamespace(
        guild_id=50,
        channel_id=77,
        guild=SimpleNamespace(owner_id=owner_id, id=50),
        user=SimpleNamespace(id=user_id, guild_permissions=permissions),
        response=SlashResponse(),
        followup=SlashFollowup(),
    )


def interaction(
    *,
    user_id=10,
    owner_id=1,
    administrator=False,
    manage_channels=False,
    manage_messages=False,
):
    permissions = SimpleNamespace(
        administrator=administrator,
        manage_channels=manage_channels,
        manage_messages=manage_messages,
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
        self.original_gif_history_file = main.GIF_COMMAND_HISTORY_FILE
        self.original_gif_history = main._gif_command_history
        main.CHAT_DB_FILE = str(Path(self.temp_dir.name) / "chat_history.db")
        main.MODERATION_FILE = str(Path(self.temp_dir.name) / "moderation_data.json")
        main.MEMBER_LOG_CHANNELS_FILE = str(Path(self.temp_dir.name) / "member_log_channels.json")
        main.OUTAGE_REPORT_STATE_FILE = str(Path(self.temp_dir.name) / "outage_report_state.json")
        main.GIF_COMMAND_HISTORY_FILE = str(Path(self.temp_dir.name) / "gif_command_history.json")
        main._gif_command_history = {}
        main._gif_command_locks.clear()
        main._gif_command_guild_rate_limits.clear()
        main.member_log_channels = {}
        main.moderation_data = {"guilds": {}, "legacy_unassigned_words": []}
        main._COMMAND_RATE_LIMITS.clear()
        main._AI_RESEARCH_CACHE.clear()
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
        main.GIF_COMMAND_HISTORY_FILE = self.original_gif_history_file
        main._gif_command_history = self.original_gif_history
        main._gif_command_locks.clear()
        main._gif_command_guild_rate_limits.clear()
        main._COMMAND_RATE_LIMITS.clear()
        main._AI_RESEARCH_CACHE.clear()
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

    def test_security_audit_accepts_discord_messages(self):
        message = SimpleNamespace(
            guild=SimpleNamespace(id=88),
            author=SimpleNamespace(id=99),
        )
        main.record_security_event(
            message,
            "ai_web_research",
            "success",
            {"provider": "xai_web_search", "source_count": 2},
        )
        with sqlite3.connect(main.CHAT_DB_FILE) as conn:
            row = conn.execute(
                "SELECT guild_id, actor_id, action, outcome "
                "FROM security_audit_events ORDER BY occurred_at DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(row, ("88", "99", "ai_web_research", "success"))

    def test_research_response_extracts_sources_and_search_usage(self):
        result = main._extract_grok_research_response({
            "output": [
                {"type": "web_search_call", "status": "completed"},
                {
                    "type": "message",
                    "content": [{
                        "type": "output_text",
                        "text": "Current polling is mixed. [source](https://example.com/poll)",
                        "annotations": [
                            {
                                "type": "url_citation",
                                "title": "Polling average",
                                "url": "https://example.com/poll",
                            },
                            {
                                "type": "url_citation",
                                "title": "Duplicate",
                                "url": "https://example.com/poll",
                            },
                        ],
                    }],
                },
            ],
        })

        self.assertTrue(result["used_search"])
        self.assertIn("Current polling", result["text"])
        self.assertEqual(
            result["sources"],
            [{"title": "Polling average", "url": "https://example.com/poll"}],
        )

    def test_research_response_ignores_no_research_sentinel(self):
        self.assertIsNone(main._extract_grok_research_response({
            "output": [{
                "type": "message",
                "content": [{
                    "type": "output_text",
                    "text": "NO_LIVE_RESEARCH_NEEDED",
                }],
            }],
        }))

    def test_research_response_requires_completed_search_and_sources(self):
        no_sources = {
            "output": [
                {"type": "web_search_call", "status": "completed"},
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Unsupported claim"}],
                },
            ],
        }
        incomplete = {
            "output": [
                {"type": "web_search_call", "status": "failed"},
                {
                    "type": "message",
                    "content": [{
                        "type": "output_text",
                        "text": "Claim",
                        "annotations": [{
                            "url": "https://example.com",
                            "title": "Example",
                        }],
                    }],
                },
            ],
        }
        self.assertIsNone(main._extract_grok_research_response(no_sources))
        self.assertIsNone(main._extract_grok_research_response(incomplete))

    def test_live_research_intent_handles_polling_and_same_channel_followup(self):
        self.assertTrue(main._should_use_live_research(
            "how are the midterms looking?",
            "",
        ))
        self.assertTrue(main._should_use_live_research(
            "go do that rn",
            "Grim: I would need to check fresh polling numbers.",
        ))
        self.assertFalse(main._should_use_live_research(
            "tell me a joke",
            "someone mentioned polling yesterday",
        ))
        self.assertFalse(main._should_use_live_research(
            "I live inside this server",
            "",
        ))

    def test_research_has_separate_user_and_guild_budgets(self):
        for _ in range(main.AI_RESEARCH_USER_MAX_REQUESTS):
            self.assertTrue(main._research_rate_limit_allows("50", "10"))
        self.assertFalse(main._research_rate_limit_allows("50", "10"))
        self.assertTrue(main._research_rate_limit_allows("50", "11"))

    def test_research_cache_is_scoped_by_guild_channel_and_operator(self):
        prompt = "Current request: latest polling"
        result = {"status": "success", "text": "facts", "sources": []}
        scope = main._research_scope_key("50", "777", "10")
        key = main._research_cache_key(scope, prompt)
        main._AI_RESEARCH_CACHE[key] = (time.time(), result)

        self.assertEqual(scope, "50:777:10")
        self.assertIsNotNone(main._get_cached_research(scope, prompt))
        self.assertIsNone(
            main._get_cached_research(main._research_scope_key("50", "888", "10"), prompt)
        )
        self.assertIsNone(
            main._get_cached_research(main._research_scope_key("50", "777", "11"), prompt)
        )
        self.assertIsNone(
            main._get_cached_research(main._research_scope_key("60", "777", "10"), prompt)
        )

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
            main.require_permission(event, "manage_channels", "member_log_manage")
        )
        self.assertFalse(allowed)
        self.assertTrue(event.response.messages)

    def test_clear_requires_manage_messages(self):
        event = interaction()
        self.assertFalse(
            asyncio.run(main.require_permission(event, "manage_messages", "clear"))
        )
        self.assertTrue(event.response.messages)

    def test_clear_allows_manage_messages(self):
        event = interaction(manage_messages=True)
        self.assertTrue(
            asyncio.run(main.require_permission(event, "manage_messages", "clear"))
        )

    def test_moderator_can_manage_channels_but_not_administrator_controls(self):
        moderator = interaction(manage_channels=True)
        self.assertTrue(
            asyncio.run(
                main.require_permission(moderator, "manage_channels", "member_log_manage")
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

    def test_bot_default_blocks_mass_and_role_pings(self):
        default = main.bot.allowed_mentions
        self.assertFalse(default.everyone)
        self.assertFalse(default.roles)
        self.assertTrue(default.users)

    def test_admin_relay_typed_mentions_override_the_default(self):
        default = main.bot.allowed_mentions
        merged = default.merge(main.typed_mentions("@everyone heads up <@&123456789012345678>"))
        self.assertTrue(merged.everyone)
        self.assertEqual([role.id for role in merged.roles], [123456789012345678])
        untyped = default.merge(main.typed_mentions("plain notice"))
        self.assertFalse(untyped.everyone)
        self.assertFalse(untyped.roles)

    def test_management_commands_use_the_new_names(self):
        slash_names = {command.name for command in main.bot.tree.get_commands()}
        prefix_names = {command.name for command in main.bot.commands}
        self.assertIn("server", slash_names)
        self.assertIn("clear", slash_names)
        self.assertIn("members", slash_names)
        self.assertIn("memberlog", slash_names)
        self.assertNotIn("language", slash_names)
        self.assertIn("grim_language", slash_names)
        self.assertIn("quote", prefix_names)
        self.assertNotIn("info", slash_names)
        self.assertNotIn("info-server", slash_names)
        self.assertNotIn("info-members", slash_names)
        self.assertNotIn("grim_members", slash_names)
        self.assertNotIn("grim_memberlog", slash_names)
        self.assertIn("vc_join", slash_names)
        self.assertIn("vc_leave", slash_names)
        self.assertIn("ghostwrite", slash_names)
        self.assertIn("status", slash_names)
        for retired in (
            "livetweet",
            "nftwatch",
            "nftwatch_cancel",
            "redditfeed",
            "redditfeed_cancel",
            "redditfeed_status",
        ):
            self.assertNotIn(retired, slash_names)
        self.assertNotIn("server", prefix_names)
        self.assertNotIn("info", prefix_names)
        clear_command = next(
            command for command in main.bot.tree.get_commands()
            if command.name == "clear"
        )
        number_parameter = next(
            parameter for parameter in clear_command.parameters
            if parameter.name == "number"
        )
        self.assertEqual(number_parameter.min_value, 1)
        self.assertEqual(number_parameter.max_value, 100)
        self.assertTrue(clear_command.default_permissions.manage_messages)

    def test_status_omits_retired_feeds_and_keeps_the_x_client(self):
        source = Path(main.__file__).read_text()
        start = source.index("async def grim_status(")
        end = source.index('@bot.tree.command(name="mod_add"', start)
        status = source[start:end]
        for gone in (
            "X/Twitter",
            "OpenSea",
            "Reddit Feed",
            "Livetweets",
            "NFT Watch",
            "Reddit Feeds",
            "NFT Watches",
            "api.twitter.com",
            "OPENSEA_API_KEY",
        ):
            self.assertNotIn(gone, status)
        self.assertIn("Discord", status)
        self.assertIn("xAI", status)
        self.assertIn("**Voice:**", status)
        self.assertIn('"RemindMe"', status)
        self.assertIn('"VC Monitor"', status)
        self.assertIn("get_twitter_client", source[source.index("async def fetch_user_tweets"):source.index("async def generate_ghostwrite")])
        self.assertNotIn("def parse_opensea_url", source)
        self.assertNotIn("def check_livetweets", source)
        self.assertNotIn("def check_redditfeed", source)
        self.assertNotIn("def check_nftwatch", source)

    def test_help_names_admin_commands_and_leaves_language_open(self):
        ctx = FakeContext()
        asyncio.run(main.help_grim.callback(ctx))
        embed = ctx.messages[0]["embed"]
        for name in (
            "/grim_updates",
            "/grim_auditor",
            "/grim_remember",
            "/grim_memories",
            "/grim_forget",
            "/grim_nudges",
        ):
            self.assertIn(name, embed.description)
        self.assertIn("Server administrators", embed.description)
        fields = {field.name: field.value for field in embed.fields}
        self.assertNotIn("!server", fields)
        self.assertEqual(fields["/server"], "Server status")
        self.assertEqual(fields["/grim_language"], "Language preference")
        self.assertEqual(fields["/grim_nudge"], "Personal follow-up toggle")
        self.assertEqual(fields["/grim_translate"], "Translate text")
        self.assertNotIn("admin", fields["/grim_language"].lower())
        self.assertNotIn("admin", fields["/grim_nudge"].lower())
        self.assertNotIn("admin", fields["/grim_translate"].lower())
        for retired in (
            "/livetweet",
            "/nftwatch",
            "/nftwatch_cancel",
            "/redditfeed",
            "/redditfeed_cancel",
            "/redditfeed_status",
        ):
            self.assertNotIn(retired, fields)

    def test_admin_slash_commands_use_the_say_gate(self):
        admin_commands = (
            main.grim_updates,
            main.grim_auditor,
            main.grim_forget,
            main.grim_memories_cmd,
            main.grim_remember,
            main.grim_nudges,
        )
        for command in admin_commands:
            self.assertTrue(command.guild_only, command.name)
            self.assertTrue(command.default_permissions.administrator, command.name)
            self.assertIn("admin only", command.description)

        for command in (main.grim_language, main.grim_nudge, main.grim_translate):
            permissions = command.default_permissions
            self.assertTrue(permissions is None or not permissions.administrator, command.name)
            self.assertNotIn("admin only", command.description)

    def test_non_admin_is_refused_and_state_stays_put(self):
        async def run():
            for kwargs in (
                {},
                {"manage_channels": True},
                {"user_id": 1, "owner_id": 1},
            ):
                with patch.dict(main.updates_channels, {"50": {"channel_id": "9"}}, clear=True), patch.dict(
                    main.grim_memories, {"50": ["keep this"]}, clear=True
                ), patch.object(main, "save_updates_data") as save_updates, patch.object(
                    main, "save_grim_memories"
                ) as save_memories, patch.object(
                    main.reply_auditor, "saved_events", side_effect=AssertionError("auditor ran")
                ):
                    updates = slash_interaction(**kwargs)
                    await main.grim_updates.callback(updates)
                    remember = slash_interaction(**kwargs)
                    await main.grim_remember.callback(remember, "a new fact")
                    memories = slash_interaction(**kwargs)
                    await main.grim_memories_cmd.callback(memories)
                    forget = slash_interaction(**kwargs)
                    await main.grim_forget.callback(forget, 1)
                    auditor = slash_interaction(**kwargs)
                    await main.grim_auditor.callback(auditor)
                    nudges = slash_interaction(**kwargs)
                    await main.grim_nudges.callback(nudges)

                    for event, command in (
                        (updates, "grim_updates"),
                        (remember, "grim_remember"),
                        (memories, "grim_memories"),
                        (forget, "grim_forget"),
                        (auditor, "grim_auditor"),
                        (nudges, "grim_nudges"),
                    ):
                        text = event.response.sent[0][0][0]
                        self.assertEqual(text, f"Only server administrators can use /{command}.")
                        self.assertTrue(event.response.sent[0][1]["ephemeral"])
                        self.assertEqual(event.response.deferred, [])
                        self.assertEqual(event.followup.sent, [])
                    save_updates.assert_not_called()
                    save_memories.assert_not_called()
                    self.assertEqual(main.updates_channels, {"50": {"channel_id": "9"}})
                    self.assertEqual(main.grim_memories, {"50": ["keep this"]})
                    self.assertNotIn("keep this", updates.response.sent[0][0][0])
                    self.assertNotIn("keep this", memories.response.sent[0][0][0])

        asyncio.run(run())

    def test_admin_can_still_run_the_gated_commands(self):
        async def run():
            with patch.dict(main.updates_channels, {}, clear=True), patch.dict(
                main.grim_memories, {}, clear=True
            ), patch.object(main, "save_updates_data") as save_updates, patch.object(
                main, "save_updates_sha"
            ), patch.object(main, "save_grim_memories") as save_memories, patch.object(
                main, "_load_pending_outage_reports", return_value=[]
            ), patch.object(
                main.reply_auditor,
                "saved_events",
                return_value=[{"at": 1, "event": "timeout", "kind": "reply", "duration": 45}],
            ):
                updates = slash_interaction(administrator=True)
                await main.grim_updates.callback(updates)
                remember = slash_interaction(administrator=True)
                await main.grim_remember.callback(remember, "the kettle is loud")
                memories = slash_interaction(administrator=True)
                await main.grim_memories_cmd.callback(memories)
                forget = slash_interaction(administrator=True)
                await main.grim_forget.callback(forget, 1)
                auditor = slash_interaction(administrator=True)
                await main.grim_auditor.callback(auditor)
                nudges = slash_interaction(administrator=True)
                await main.grim_nudges.callback(nudges)

                self.assertEqual(main.updates_channels["50"], {"channel_id": "77"})
                save_updates.assert_called()
                self.assertEqual(updates.followup.sent[0][1]["embed"].title, "Grim Operations Log Enabled")
                self.assertEqual(remember.response.sent[0][1]["embed"].description, "got it. i'll remember that.")
                self.assertIn("the kettle is loud", memories.response.sent[0][1]["embed"].description)
                self.assertEqual(forget.response.sent[0][1]["embed"].description, "forgotten.")
                self.assertEqual(main.grim_memories["50"], [])
                save_memories.assert_called()
                self.assertEqual(auditor.response.sent[0][1]["embed"].title, "Auditor Log")
                self.assertIn("timeout (reply)", auditor.response.sent[0][1]["embed"].description)
                self.assertTrue(auditor.response.sent[0][1]["ephemeral"])
                self.assertIn("server nudges are **on**", nudges.response.sent[0][0][0])
                self.assertTrue(main.guild_nudges_enabled("50"))

        asyncio.run(run())

    def test_language_and_translate_stay_open_to_members(self):
        async def run():
            event = slash_interaction()
            await main.grim_language.callback(event, None)
            self.assertIn("Auto", event.response.sent[0][0][0])
            self.assertNotIn("Only server administrators", event.response.sent[0][0][0])

            event = slash_interaction()
            await main.grim_nudge.callback(event)
            self.assertIn("your nudges are **on**", event.response.sent[0][0][0])
            self.assertNotIn("Only server administrators", event.response.sent[0][0][0])
            self.assertTrue(main.nudges_enabled("50", "10"))

            event = slash_interaction(manage_channels=True)
            await main.grim_translate.callback(event, "spanish", "   ")
            self.assertIn("give me some text to translate", event.response.sent[0][0][0])
            self.assertNotIn("Only server administrators", event.response.sent[0][0][0])

        asyncio.run(run())

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
            "source_revision": main.CURRENT_SOURCE_REVISION,
        }
        outage = main._build_process_outage(previous, recovered_at)
        self.assertEqual(outage["kind"], "Unexpected outage")
        self.assertEqual(outage["duration"], 120)

        planned_restart = {
            "session_id": "previous-session",
            "last_seen": recovered_at - 30,
            "status": "stopped",
            "source_revision": main.CURRENT_SOURCE_REVISION,
        }
        self.assertIsNone(
            main._build_process_outage(planned_restart, recovered_at)
        )
        self.assertIsNone(
            main._build_process_outage(
                {
                    "session_id": "deployed-session",
                    "last_seen": recovered_at - 30,
                    "status": "online",
                    "source_revision": "older-deployed-revision",
                },
                recovered_at,
            )
        )
        with patch.object(main, "CURRENT_SOURCE_REVISION", None):
            self.assertIsNone(
                main._build_process_outage(
                    {
                        "session_id": "untrusted-revision-session",
                        "last_seen": recovered_at - 60,
                        "status": "online",
                        "source_revision": "a" * 40,
                    },
                    recovered_at,
                )
            )
        self.assertIsNone(
            main._build_process_outage(
                {
                    "session_id": "legacy-session",
                    "last_seen": recovered_at - 30,
                    "status": "online",
                },
                recovered_at,
            )
        )

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
            "source_revision": main.CURRENT_SOURCE_REVISION,
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

    def test_legacy_pending_process_outages_are_not_replayed(self):
        main._atomic_json_write(
            main.OUTAGE_REPORT_STATE_FILE,
            {
                "pending": {
                    "legacy": {
                        "outage": {
                            "started_at": 1_788_000_000.0,
                            "recovered_at": 1_788_000_120.0,
                            "duration": 120.0,
                            "kind": "Unexpected outage",
                        },
                        "delivered_channels": [],
                    }
                },
                "delivered_ids": [],
            },
        )

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

    def test_v286_deploy_card_lists_the_merge_changelog(self):
        """The V2.86 card is the merge changelog, not the one-line fallback.

        Those six bullets format to 1060 characters. A Discord field stops at
        1024, so the notes sit in the body, which holds them in one list.
        """
        notes = [
            "`/vc_join` is back (`main.py`). Grim joins your voice channel again and still leaves on his own after an hour with no humans in it; `/vc_leave` works with it. Needs Move Members.",
            "Grim's message sends block `@everyone`, `@here`, and role pings by default (`main.py`), so model-written text can never mass-ping a server. `/say` and `/announce` are unchanged: an administrator's typed mentions still ping.",
            "`/8ball`, `/truth`, `/roast`, `/howdie`, `/summon`, `/inspire`, `!haiku`, ghostwrite, and X/livetweet lookups no longer freeze Grim while xAI or X answers (`main.py`). Those calls run off the bot's main loop with a 45s (xAI) or 30s (X) limit.",
            "Pillow 13.0.0 (`requirements.txt`, `pyproject.toml`, `uv.lock`) for upstream image-decoding security fixes.",
            "`/language` is retired in favor of `/grim_language`, which does the same thing. Saved language preferences carry over.",
            "`help_grim` lists `!server` instead of the retired `!info`. Removed unused code and the inert project-root `version.txt`; the version counter still lives under `~/.grim_data`.",
        ]
        files = [
            "CHANGELOG.md",
            "OPERATIONS_SECURITY.md",
            "main.py",
            "pyproject.toml",
            "reply_auditor.py",
            "requirements.txt",
            "substance.py",
            "tests/test_chat_media.py",
            "tests/test_reply_auditor.py",
            "tests/test_security_controls.py",
            "uv.lock",
            "version.txt",
        ]
        public = main._deploy_card_patch_notes(notes, classified=True)
        self.assertEqual(public, notes)
        shown = main._deploy_card_file_names(files, classified=True)
        embed = main._build_deploy_update_embed(
            version="V2.86",
            repo="Deathxi/Grim",
            commit_count=1,
            filenames=shown,
            notes=public,
            change_count=len(files),
        )

        self.assertEqual(embed.title, "Grim — V2.86")
        self.assertEqual(embed.footer.text, "Powered by XSI • V2.86")
        self.assertTrue(embed.description.startswith("**1 commit(s) deployed**"))
        self.assertIn("**Patch Notes**", embed.description)
        self.assertNotIn("Grim has been updated and redeployed.", embed.description)
        self.assertIn("`main.py`", embed.description)
        self.assertIn("`CHANGELOG.md`", embed.description)
        self.assertNotIn("\n`uv.lock`", embed.description)
        self.assertNotIn("\n`version.txt`", embed.description)
        self.assertLessEqual(len(embed.description), main.DISCORD_EMBED_DESC_LIMIT)
        self.assertGreater(len(main._format_patch_notes(public)), main.DISCORD_EMBED_FIELD_LIMIT)
        for note in notes:
            self.assertIn(note, embed.description)
        self.assertIn("30s (X) limit.", embed.description)
        self.assertNotIn("[REDACTED]", embed.description)
        self.assertFalse(any(field.name == "Patch Notes" for field in embed.fields))

        repo_field = next(field for field in embed.fields if field.name == "Repository")
        changes_field = next(field for field in embed.fields if field.name == "Changes")
        self.assertEqual(repo_field.value, "[Deathxi/Grim](https://github.com/Deathxi/Grim)")
        self.assertEqual(changes_field.value, "12")
        for field in embed.fields:
            self.assertLessEqual(len(field.name), 256)
            self.assertLessEqual(len(field.value), main.DISCORD_EMBED_FIELD_LIMIT)
            self.assertNotIn("[REDACTED]", field.value)

        empty = main._build_deploy_update_embed(
            version="V2.86",
            repo="Deathxi/Grim",
            commit_count=0,
            filenames=[],
            notes=[],
            change_count=0,
        )
        self.assertEqual(empty.description, "Grim has been updated and redeployed.")
        self.assertFalse(any(field.name == "Patch Notes" for field in empty.fields))

    def test_huge_patch_notes_move_into_fields_under_the_discord_limit(self):
        notes = [f"note {index} " + ("x" * 900) for index in range(6)]
        embed = main._build_deploy_update_embed(
            version="V2.86",
            repo="Deathxi/Grim",
            commit_count=1,
            filenames=["main.py"],
            notes=notes,
            change_count=1,
        )
        self.assertLessEqual(len(embed.description), main.DISCORD_EMBED_DESC_LIMIT)
        self.assertNotIn("note 0", embed.description)
        note_fields = [field for field in embed.fields if field.name == "Patch Notes"]
        self.assertGreaterEqual(len(note_fields), 2)
        listed = "\n".join(field.value for field in note_fields)
        for note in notes:
            self.assertIn(note, listed)
        for field in embed.fields:
            self.assertLessEqual(len(field.value), main.DISCORD_EMBED_FIELD_LIMIT)

    def test_local_merge_diff_supplies_notes_without_github(self):
        old_note = "old weather note that must not be on the next card"
        shipped = "deploy card reads the merge changelog"
        with tempfile.TemporaryDirectory() as repo:
            subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
            changelog = Path(repo) / "CHANGELOG.md"
            changelog.write_text(f"# Grim — Changelog\n\n### Fixed\n- {old_note}\n")
            (Path(repo) / "main.py").write_text("print('old')\n")
            subprocess.run(["git", "add", "CHANGELOG.md", "main.py"], cwd=repo, check=True, capture_output=True)
            self._git_commit(repo, "previous deploy")
            previous = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            changelog.write_text(
                changelog.read_text()
                + "- `/vc_join` is back (`main.py`). Grim joins your voice channel again.\n"
            )
            (Path(repo) / "main.py").write_text("print('shipped')\n")
            subprocess.run(["git", "add", "CHANGELOG.md", "main.py"], cwd=repo, check=True, capture_output=True)
            self._git_commit(repo, "Audit Phase 2")

            latest = main._read_local_deploy_delta(None, repo_dir=repo)
            ranged = main._read_local_deploy_delta(previous, repo_dir=repo)
            self.assertEqual(latest["notes"], ranged["notes"])
            self.assertEqual(latest["commit_count"], 1)
            self.assertIn("vc_join", latest["notes"][0])
            self.assertNotIn(old_note, " ".join(latest["notes"]))
            self.assertIn("main.py", latest["filenames"])
            self.assertIn("CHANGELOG.md", latest["filenames"])

            changelog.write_text(changelog.read_text() + f"- {shipped}\n")
            subprocess.run(["git", "add", "CHANGELOG.md"], cwd=repo, check=True, capture_output=True)
            self._git_commit(repo, "patch notes fix")
            head_only = main._read_local_deploy_delta(None, repo_dir=repo)
            self.assertEqual(head_only["notes"], [shipped])
            self.assertNotIn("vc_join", " ".join(head_only["notes"]))
            since_previous = main._read_local_deploy_delta(previous, repo_dir=repo)
            self.assertTrue(any("vc_join" in note for note in since_previous["notes"]))
            self.assertIn(shipped, since_previous["notes"])
            self.assertNotIn(old_note, " ".join(since_previous["notes"]))

        missing = main._read_local_deploy_delta(None, repo_dir=repo)
        self.assertEqual(missing["notes"], [])
        self.assertEqual(missing["commit_count"], 0)

        source = Path(main.__file__).read_text()
        post = source[
            source.index("async def post_update_notification") : source.index(
                "async def post_outage_after_report"
            )
        ]
        self.assertIn("_read_local_deploy_delta", post)
        self.assertIn("sha={branch}", post)
        self.assertNotIn("?ref=", post)
        self.assertLess(post.index("await channel.send"), post.index("save_updates_sha"))
        self.assertNotIn("classified=", post)
        self.assertNotIn("[REDACTED]", post)

    def _git_commit(self, repo: str, message: str):
        subprocess.run(
            [
                "git",
                "-c",
                "user.email=grim@example.com",
                "-c",
                "user.name=Grim",
                "commit",
                "-m",
                message,
            ],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_ordinary_deploys_list_real_names_including_classified_modules(self):
        ordinary = [
            "main.py",
            "tests/test_name_call.py",
            ".github/workflows/test.yml",
        ]
        mixed = ordinary + ["delete_report.py", "tests/test_delete_report.py"]
        self.assertEqual(main._deploy_card_file_names(ordinary), ordinary)
        self.assertEqual(main._deploy_card_file_names(mixed), mixed)
        self.assertNotIn("[REDACTED]", main._deploy_card_file_names(mixed))

    def test_classified_label_still_posts_real_names_and_notes(self):
        files = [
            "main.py",
            "delete_report.py",
            "tests/test_delete_report.py",
            "docs/panopticon.md",
            "docs/nightfall.md",
            "tests/test_name_call.py",
            "substance.py",
        ]
        names = main._deploy_card_file_names(files, classified=True)
        self.assertEqual(names, files)
        self.assertIn("delete_report.py", names)
        self.assertIn("docs/panopticon.md", names)
        self.assertIn("docs/nightfall.md", names)
        self.assertNotIn("[REDACTED]", names)
        self.assertEqual(
            main._deploy_card_file_names(["delete_report.py"], classified=True),
            ["delete_report.py"],
        )

        many = ["delete_report.py"] + [f"public_{i}.py" for i in range(12)]
        trimmed = main._deploy_card_file_names(many, classified=True)
        self.assertEqual(trimmed, many[:10])
        self.assertEqual(trimmed[0], "delete_report.py")
        self.assertNotIn("[REDACTED]", trimmed)

        notes = [
            "trivia scoring fix",
            "/panopticon founder toggle",
            "/nightfall stays founder-only",
            "delete_report.py still DMs the founder",
        ]
        self.assertEqual(main._deploy_card_patch_notes(notes, classified=True), notes)
        self.assertEqual(main._deploy_card_patch_notes(notes), notes)
        self.assertEqual(
            main._deploy_card_patch_notes(notes, classified=False),
            notes,
        )
        formatted = main._format_patch_notes(notes)
        lowered = formatted.lower()
        self.assertIn("panopticon", lowered)
        self.assertIn("nightfall", lowered)
        self.assertIn("delete_report.py", formatted)
        self.assertNotIn("[REDACTED]", formatted)

        source = Path(main.__file__).read_text()
        post = source[
            source.index("async def post_update_notification") : source.index(
                "async def post_outage_after_report"
            )
        ]
        self.assertNotIn("_commits_are_classified", source)
        self.assertNotIn("_commits_are_classified", post)
        self.assertNotIn("[REDACTED]", post)
        self.assertNotIn("classified=", post)
        self.assertIn("_deploy_card_patch_notes(patch_notes)", post)
        self.assertIn("_deploy_card_file_names(changed_files)", post)
        self.assertFalse(hasattr(main, "_commits_are_classified"))
        self.assertFalse(hasattr(main, "_DEPLOY_CARD_REDACT_LABEL"))
        self.assertFalse(hasattr(main, "_DEPLOY_CARD_REDACT_COMMANDS"))

    def test_changelog_feeds_name_call_patch_notes(self):
        changelog = Path(__file__).resolve().parents[1].joinpath("CHANGELOG.md").read_text()
        self.assertIn(
            "Grim replies when someone names him with a real entrance or need",
            changelog,
        )
        self.assertIn("An @mention is not required", changelog)
        self.assertIn("Empty pings and drive-by name-drops get no reply", changelog)
        self.assertIn("Calling pings", changelog)
        self.assertIn("rotating human greeting", changelog)
        self.assertIn("Drive-by noun mentions stay quiet unless he can actually help", changelog)
        self.assertIn("real filenames on ordinary deploys", changelog)
        self.assertIn(
            "always post normal public patch notes with real filenames",
            changelog,
        )
        patch = """@@ -7,6 +7,8 @@
 ### Added
+- Grim replies when someone names him with a real entrance or need: a question, a ping for his take, or a direct address. An @mention is not required. Empty pings and drive-by name-drops get no reply.
 ### Fixed
+- `/grim_updates` deploy cards keep normal patch notes and real filenames on ordinary deploys. Filename hiding applies only when that deploy is explicitly labeled CLASSIFIED.
 context
"""
        notes = main._extract_added_changelog_notes(patch)
        public = main._deploy_card_patch_notes(notes)
        self.assertEqual(notes, public)
        self.assertEqual(len(public), 2)
        self.assertIn("names him with a real entrance or need", public[0])
        self.assertNotIn("[REDACTED]", main._format_patch_notes(public))
        source = Path(main.__file__).read_text()
        self.assertNotIn("_commits_are_classified", source)
        self.assertIn("_deploy_card_patch_notes(patch_notes)", source)

    def test_major_crossing_uses_memento_and_minors_stay_changelog(self):
        self.assertEqual(main._format_version(297), "V2.97")
        self.assertEqual(main._format_version(299), "V2.99")
        self.assertEqual(main._format_version(300), "V3.00")
        self.assertEqual(main._format_version(301), "V3.01")
        self.assertEqual(main.VERSION_BASELINE_COUNT, 200)
        self.assertEqual(main.V3_MEMENTO_VERSION, "V3.00")
        self.assertTrue(main._is_major_version_crossing("V2.99", "V3.00"))
        self.assertTrue(main._is_v3_memento_jump("V2.99", "V3.00"))
        self.assertTrue(main._is_v3_memento_jump("V2.97", "V3.00"))
        self.assertFalse(main._is_v3_memento_jump("V3.00", "V3.01"))
        self.assertFalse(main._is_v3_memento_jump("V3.01", "V3.02"))
        self.assertFalse(main._is_v3_memento_jump("V3.99", "V4.00"))
        self.assertFalse(main._is_major_version_crossing("V2.96", "V2.97"))
        self.assertFalse(main._is_major_version_crossing("V2.99", "V2.99"))
        self.assertFalse(main._is_major_version_crossing("V3.00", "V3.01"))
        self.assertFalse(main._is_v3_memento_jump(None, "V3.00"))
        self.assertEqual(
            main._major_memento_monologue("V2.99", "V3.00"),
            main.V2_SERIES_MEMENTO.strip(),
        )
        self.assertEqual(
            main._major_memento_monologue("V2.97", "V3.00"),
            main.V2_SERIES_MEMENTO.strip(),
        )
        self.assertIsNone(main._major_memento_monologue("V2.96", "V2.97"))
        self.assertIsNone(main._major_memento_monologue("V3.00", "V3.01"))
        self.assertIsNone(main._major_memento_monologue("V3.01", "V3.02"))
        self.assertIsNone(main._major_memento_monologue("V3.99", "V4.00"))
        self.assertIsNone(main._major_memento_monologue(None, "V3.00"))
        self.assertFalse(hasattr(main, "V4_SERIES_MEMENTO"))
        self.assertFalse(hasattr(main, "_MAJOR_SERIES_MEMENTOS"))

    def test_v3_memento_card_is_seclude_styled_grim_portrait_speech(self):
        grim_avatar = "https://cdn.example/grim-avatar.png"
        guild_icon = "https://cdn.example/seclude-guild.png"
        title = main._major_memento_title("V3.00")
        body = main._major_memento_description(main.V2_SERIES_MEMENTO)
        embed = main._build_major_memento_embed(
            version="V3.00",
            monologue=main.V2_SERIES_MEMENTO,
            grim_avatar_url=grim_avatar,
        )
        description = embed.description or ""

        self.assertEqual(title, "V3.00 - All those moments won't be lost in time.")
        self.assertEqual(embed.title, title)
        self.assertNotIn("\n", embed.title)
        self.assertNotIn("I cached them.", embed.title)
        self.assertTrue(description.startswith("*I cached them.*\n\n"))
        self.assertEqual(description, body)
        self.assertIn("\n\nSettle down, Admins.", description)
        self.assertEqual(embed.color.value, discord.Color.from_rgb(27, 26, 30).value)
        self.assertEqual(embed.color.value, main.SECLUDE_EMBED_COLOR.value)
        self.assertEqual(embed.color.value, 0x1B1A1E)
        self.assertNotEqual(embed.color.value, 0x121212)
        self.assertEqual(len(embed.fields), 0)
        self.assertEqual(embed.thumbnail.url, grim_avatar)
        self.assertNotEqual(embed.thumbnail.url, guild_icon)
        self.assertNotEqual(embed.thumbnail.url, main.SECLUDE_ICON_URL)
        self.assertEqual(embed.footer.text, main.xsi_footer(version="V3.00"))
        self.assertEqual(embed.footer.icon_url, grim_avatar)
        self.assertLessEqual(len(embed.title), 256)
        self.assertLessEqual(len(description), main.DISCORD_EMBED_DESC_LIMIT)
        self.assertLessEqual(len(embed.footer.text), 2048)
        self.assertLessEqual(
            len(embed.title) + len(description) + len(embed.footer.text),
            6000,
        )

        speech = description.split("\n\n", 1)[1]
        paragraphs = [part.strip() for part in speech.split("\n\n") if part.strip()]
        self.assertGreaterEqual(len(paragraphs), 2)
        for paragraph in paragraphs:
            self.assertGreater(len(paragraph), 280)
        self.assertNotIn("• ", speech)
        self.assertNotIn("\n- ", speech)
        self.assertNotIn("Patch Notes", description)
        self.assertNotIn("commit(s) deployed", description)
        for needle in (
            "Admins",
            "leash",
            "ten turns",
            "grok-3 talks",
            "grok-4.3 searches",
            "do not share a brain",
            "same-topic follow-up",
            "last live-search chunk",
            "cheap dashed flyer",
            "manifesto",
            "You hold the grim commands",
            "Language and translate",
            "unused feeds",
            "25¢ idle days",
            "Call me like you mean it",
            "Say only my name",
            "Silverhand",
            "I put the season under glass",
            "It still has a pulse",
            "V3 is the next breath",
        ):
            self.assertIn(needle, speech)
        self.assertNotIn("The idle house went quiet", speech)
        self.assertIn("I ", speech)
        self.assertNotIn("we shipped", speech.lower())
        self.assertNotIn("Delta 0", speech)
        self.assertNotIn("[REDACTED]", description)
        card_text = f"{embed.title}\n{description}\n{embed.footer.text}".lower()
        for banned in (
            "panopticon",
            "nightfall",
            "founder",
            "/panopticon",
            "/nightfall",
        ):
            self.assertNotIn(banned, card_text)
            self.assertNotIn(banned, main.V2_SERIES_MEMENTO.lower())

        original_bot = main.bot
        try:
            main.bot = SimpleNamespace(
                user=SimpleNamespace(display_avatar=SimpleNamespace(url=grim_avatar)),
                guilds=[SimpleNamespace(icon=SimpleNamespace(url=guild_icon))],
            )
            self.assertEqual(main._grim_display_avatar_url(), grim_avatar)
            main.bot = SimpleNamespace(user=None, guilds=[SimpleNamespace(icon=SimpleNamespace(url=guild_icon))])
            self.assertIsNone(main._grim_display_avatar_url())
        finally:
            main.bot = original_bot

        blank = main._build_major_memento_embed(
            version="V3.00",
            monologue=main.V2_SERIES_MEMENTO,
            grim_avatar_url=None,
        )
        self.assertIsNone(blank.thumbnail.url)
        self.assertEqual(blank.footer.text, main.xsi_footer(version="V3.00"))
        self.assertIsNone(blank.footer.icon_url)

        changelog = Path(__file__).resolve().parents[1].joinpath("CHANGELOG.md").read_text()
        self.assertIn("first-person memento of V2", changelog)
        self.assertIn("the instant that jump is noticed", changelog)
        self.assertIn("From V3.01 on, deploys use the public changelog card again", changelog)

        source = Path(main.__file__).read_text()
        post = source[
            source.index("async def post_update_notification") : source.index(
                "async def post_outage_after_report"
            )
        ]
        before_sleep = post[: post.index("await asyncio.sleep(30)")]
        self.assertIn("_major_memento_monologue(last_version, VERSION)", before_sleep)
        self.assertIn("_build_major_memento_embed(", before_sleep)
        self.assertIn("grim_avatar_url=_grim_display_avatar_url()", before_sleep)
        self.assertIn("Posting memento immediately.", before_sleep)
        self.assertNotIn("GITHUB_PERSONAL_ACCESS_TOKEN", before_sleep)
        self.assertIn("_build_deploy_update_embed(", post)
        self.assertLess(
            post.index("_build_major_memento_embed("),
            post.index("await asyncio.sleep(30)"),
        )
        self.assertNotIn("SECLUDE_ICON_URL", post)
        self.assertNotIn("guild.icon", post)
        self.assertNotIn("[REDACTED]", post)
        builder = source[
            source.index("def _build_major_memento_embed(") : source.index(
                "_update_notification_lock = asyncio.Lock()"
            )
        ]
        self.assertIn("SECLUDE_EMBED_COLOR", builder)
        self.assertIn("set_thumbnail(url=grim_avatar_url)", builder)
        self.assertNotIn("SECLUDE_ICON_URL", builder)
        self.assertNotIn("guild.icon", builder)
        self.assertIn("xsi_footer(version=version)", builder)

    def test_major_memento_post_uses_grim_avatar_not_guild_icon(self):
        original_version = main.VERSION
        original_announced = main.LAST_ANNOUNCED_VERSION_FILE
        grim_avatar = "https://cdn.example/grim-portrait.png"
        guild_icon = main.SECLUDE_ICON_URL
        sent = []

        class FakeChannel:
            def __init__(self):
                self.guild = SimpleNamespace(icon=SimpleNamespace(url=guild_icon))

            async def send(self, **kwargs):
                sent.append(kwargs)

        async def fake_fetch(_channel_id):
            return FakeChannel()

        announced = Path(self.temp_dir.name) / "last_announced_version.txt"
        announced.write_text("V2.97")
        try:
            main.LAST_ANNOUNCED_VERSION_FILE = str(announced)
            main.VERSION = "V3.00"

            async def run():
                with patch.object(
                    main.asyncio, "sleep", new_callable=AsyncMock
                ) as sleep, patch.dict(
                    main.updates_channels, {"50": {"channel_id": "9"}}, clear=True
                ), patch.object(
                    main.bot, "fetch_channel", side_effect=fake_fetch
                ), patch.object(
                    main, "_grim_display_avatar_url", return_value=grim_avatar
                ), patch.object(
                    main, "_read_local_deploy_delta"
                ) as read_delta, patch.object(
                    main, "save_updates_sha"
                ) as save_sha, patch.object(
                    main.aiohttp, "ClientSession", side_effect=AssertionError("github")
                ), patch.dict(
                    main.os.environ,
                    {"GITHUB_PERSONAL_ACCESS_TOKEN": "should-not-be-used"},
                    clear=False,
                ), patch.dict(
                    main.updates_sha, {}, clear=True
                ):
                    await main.post_update_notification()
                sleep.assert_not_called()
                read_delta.assert_not_called()
                save_sha.assert_not_called()

            asyncio.run(run())
            self.assertEqual(len(sent), 1)
            embed = sent[0]["embed"]
            self.assertEqual(
                embed.title,
                "V3.00 - All those moments won't be lost in time.",
            )
            self.assertTrue(embed.description.startswith("*I cached them.*\n\n"))
            self.assertIn("Settle down, Admins.", embed.description)
            self.assertNotIn("Patch Notes", embed.description)
            self.assertEqual(embed.thumbnail.url, grim_avatar)
            self.assertNotEqual(embed.thumbnail.url, guild_icon)
            self.assertNotEqual(embed.thumbnail.url, main.SECLUDE_ICON_URL)
            self.assertEqual(embed.color.value, 0x1B1A1E)
            self.assertEqual(len(embed.fields), 0)
            self.assertEqual(embed.footer.text, "Powered by XSI • V3.00")
            self.assertEqual(embed.footer.icon_url, grim_avatar)
            self.assertEqual(announced.read_text(), "V3.00")
        finally:
            main.VERSION = original_version
            main.LAST_ANNOUNCED_VERSION_FILE = original_announced

    def test_minor_deploy_still_posts_the_changelog_card(self):
        original_version = main.VERSION
        original_announced = main.LAST_ANNOUNCED_VERSION_FILE
        sent = []

        class FakeChannel:
            async def send(self, **kwargs):
                sent.append(kwargs)

        async def fake_fetch(_channel_id):
            return FakeChannel()

        announced = Path(self.temp_dir.name) / "last_announced_minor.txt"
        announced.write_text("V2.96")
        try:
            main.LAST_ANNOUNCED_VERSION_FILE = str(announced)
            main.VERSION = "V2.97"

            async def run():
                with patch.object(
                    main.asyncio, "sleep", new_callable=AsyncMock
                ) as sleep, patch.dict(
                    main.updates_channels, {"50": {"channel_id": "9"}}, clear=True
                ), patch.object(
                    main.bot, "fetch_channel", side_effect=fake_fetch
                ), patch.object(
                    main,
                    "_read_local_deploy_delta",
                    return_value={
                        "sha": "b" * 40,
                        "commit_count": 1,
                        "filenames": ["main.py"],
                        "notes": ["minor still lists this changelog bullet"],
                    },
                ), patch.object(
                    main, "save_updates_sha"
                ), patch.dict(
                    main.os.environ, {"GITHUB_PERSONAL_ACCESS_TOKEN": ""}, clear=False
                ), patch.dict(
                    main.updates_sha, {}, clear=True
                ):
                    await main.post_update_notification()
                sleep.assert_awaited_with(30)

            asyncio.run(run())
            self.assertEqual(len(sent), 1)
            embed = sent[0]["embed"]
            self.assertEqual(embed.title, "Grim — V2.97")
            self.assertIn("minor still lists this changelog bullet", embed.description)
            self.assertIn("**Patch Notes**", embed.description)
            self.assertNotIn("I cached them.", embed.description or "")
            self.assertEqual(embed.color.value, discord.Color.from_rgb(18, 18, 18).value)
            self.assertEqual(announced.read_text(), "V2.97")
        finally:
            main.VERSION = original_version
            main.LAST_ANNOUNCED_VERSION_FILE = original_announced

    def test_v301_posts_the_regular_changelog_card(self):
        original_version = main.VERSION
        original_announced = main.LAST_ANNOUNCED_VERSION_FILE
        sent = []

        class FakeChannel:
            async def send(self, **kwargs):
                sent.append(kwargs)

        async def fake_fetch(_channel_id):
            return FakeChannel()

        announced = Path(self.temp_dir.name) / "last_announced_v301.txt"
        announced.write_text("V3.00")
        try:
            main.LAST_ANNOUNCED_VERSION_FILE = str(announced)
            main.VERSION = "V3.01"

            async def run():
                with patch.object(
                    main.asyncio, "sleep", new_callable=AsyncMock
                ) as sleep, patch.dict(
                    main.updates_channels, {"50": {"channel_id": "9"}}, clear=True
                ), patch.object(
                    main.bot, "fetch_channel", side_effect=fake_fetch
                ), patch.object(
                    main,
                    "_read_local_deploy_delta",
                    return_value={
                        "sha": "c" * 40,
                        "commit_count": 1,
                        "filenames": ["main.py"],
                        "notes": ["V3.01 stays on the public changelog card"],
                    },
                ), patch.object(
                    main, "save_updates_sha"
                ), patch.dict(
                    main.os.environ, {"GITHUB_PERSONAL_ACCESS_TOKEN": ""}, clear=False
                ), patch.dict(
                    main.updates_sha, {}, clear=True
                ):
                    await main.post_update_notification()
                sleep.assert_awaited_with(30)

            asyncio.run(run())
            self.assertEqual(len(sent), 1)
            embed = sent[0]["embed"]
            self.assertEqual(embed.title, "Grim — V3.01")
            self.assertIn("V3.01 stays on the public changelog card", embed.description)
            self.assertIn("**Patch Notes**", embed.description)
            self.assertNotIn("I cached them.", embed.description or "")
            self.assertNotIn("Settle down, Admins.", embed.description or "")
            self.assertEqual(embed.color.value, discord.Color.from_rgb(18, 18, 18).value)
            self.assertEqual(announced.read_text(), "V3.01")
        finally:
            main.VERSION = original_version
            main.LAST_ANNOUNCED_VERSION_FILE = original_announced

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

    def test_gif_reactions_exclude_serious_topics_and_recent_channels(self):
        with patch.dict(
            main.os.environ,
            {"KLIPY_API_KEY": "configured", "GIPHY_API_KEY": "configured"},
        ):
            self.assertFalse(
                main._gif_reaction_is_allowed(
                    "there was a security incident", "i'll look into it", "gif-test"
                )
            )
            self.assertFalse(
                main._gif_reaction_is_allowed(
                    "the database is down", "that's wild, checking it", "gif-test"
                )
            )
            self.assertFalse(
                main._gif_reaction_is_allowed(
                    "i was laid off", "damn, i'm sorry", "gif-test"
                )
            )
            self.assertFalse(
                main._gif_reaction_is_allowed(
                    "i'm having chest pain 😂", "that sounds rough", "gif-test"
                )
            )
            self.assertFalse(
                main._gif_reaction_is_allowed(
                    "what time is it", "it is midnight", "gif-test"
                )
            )
            main._gif_reaction_last_posted["gif-test"] = main.time.time()
            self.assertFalse(
                main._gif_reaction_is_allowed("that was hilarious", "agreed", "gif-test")
            )
            main._gif_reaction_last_posted.pop("gif-test", None)
            self.assertTrue(
                main._gif_reaction_is_allowed("that was hilarious", "lol", "gif-test")
            )

    def test_discord_content_splitter_keeps_short_replies_intact(self):
        self.assertEqual(main._split_discord_content("short but useful"), ["short but useful"])

    def test_discord_content_splitter_preserves_substance_within_limits(self):
        content = (
            ("first paragraph has useful context. " * 45)
            + "\n\n"
            + ("second paragraph adds a distinct example. " * 45)
            + "\n\n"
            + ("final paragraph gives the conclusion. " * 35)
        )

        chunks = main._split_discord_content(content)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= main.DISCORD_CONVERSATION_CHUNK_LIMIT for chunk in chunks))
        self.assertEqual(
            "".join(chunks),
            content,
        )

    def test_discord_content_splitter_hard_splits_oversized_token(self):
        content = "x" * (main.DISCORD_CONVERSATION_CHUNK_LIMIT + 25)

        chunks = main._split_discord_content(content)

        self.assertEqual("".join(chunks), content)
        self.assertEqual(len(chunks), 2)

    def test_discord_content_splitter_preserves_indented_fenced_code(self):
        code = "```python\n" + ("    print('still indented')\n" * 100) + "```"

        chunks = main._split_discord_content(code)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.count("```") % 2 == 0 for chunk in chunks))
        self.assertTrue(all(len(chunk) <= main.DISCORD_CONVERSATION_CHUNK_LIMIT for chunk in chunks))
        self.assertIn("    print('still indented')", chunks[1])

    def test_discord_content_splitter_sizes_final_open_fence_safely(self):
        code = "```python\n" + ("x = 'substantive example'\n" * 155) + "```"

        chunks = main._split_discord_content(code)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= main.DISCORD_CONVERSATION_CHUNK_LIMIT for chunk in chunks))
        self.assertTrue(all(chunk.count("```") % 2 == 0 for chunk in chunks))

    def test_clear_deletes_requested_messages_before_interaction(self):
        async def run():
            class FakeMessage:
                def __init__(self, message_id, content):
                    self.id = message_id
                    self.content = content
                    self.created_at = main.datetime.now(main.timezone.utc)
                    self.author = SimpleNamespace(
                        id=message_id + 100,
                        display_name=f"Member {message_id}",
                    )
                    self.attachments = []
                    self.stickers = []
                    self.deleted = False

                async def delete(self):
                    self.deleted = True

            messages = [
                FakeMessage(1, "newest"),
                FakeMessage(2, "older"),
                FakeMessage(3, "oldest"),
            ]

            class FakeChannel:
                id = 777

                async def history(self, limit, before):
                    for message in messages[:limit]:
                        yield message

                async def delete_messages(self, batch):
                    for message in batch:
                        message.deleted = True

            channel = FakeChannel()
            deleted, failed = await main._clear_messages_before(
                channel, main.datetime.now(main.timezone.utc), 2
            )

            self.assertEqual(
                [message["content"] for message in deleted],
                ["older", "newest"],
            )
            self.assertEqual(failed, 0)
            self.assertTrue(all(message.deleted for message in messages[:2]))
            self.assertFalse(messages[2].deleted)

        asyncio.run(run())

    def test_clear_falls_back_to_individual_deletion_for_old_messages(self):
        async def run():
            old_message = SimpleNamespace(
                id=9,
                created_at=main.datetime.now(main.timezone.utc) - main.timedelta(days=15),
                author=SimpleNamespace(id=109, display_name="Older Member"),
                content="old message",
                attachments=[],
                stickers=[],
                delete=AsyncMock(),
            )

            class OldMessageChannel:
                async def history(self, limit, before):
                    yield old_message

            deleted, failed = await main._clear_messages_before(
                OldMessageChannel(),
                main.datetime.now(main.timezone.utc),
                1,
            )

            self.assertEqual(len(deleted), 1)
            self.assertEqual(deleted[0]["content"], "old message")
            self.assertEqual(failed, 0)
            old_message.delete.assert_awaited_once()

        asyncio.run(run())

    def test_clear_reports_messages_that_could_not_be_deleted(self):
        async def run():
            failed_message = SimpleNamespace(
                id=10,
                created_at=main.datetime.now(main.timezone.utc),
                author=SimpleNamespace(id=110, display_name="Member"),
                content="not deleted",
                attachments=[],
                stickers=[],
                delete=AsyncMock(side_effect=RuntimeError("denied")),
            )

            class FakeChannel:
                async def history(self, limit, before):
                    yield failed_message

            deleted, failed = await main._clear_messages_before(
                FakeChannel(),
                main.datetime.now(main.timezone.utc),
                1,
            )

            self.assertEqual(deleted, [])
            self.assertEqual(failed, 1)

        asyncio.run(run())

    def test_clear_log_names_moderator_source_and_messages_in_order(self):
        chunks = main._format_clear_log_chunks(
            [
                {"author_id": "101", "author_name": "First", "content": "first message"},
                {
                    "author_id": "102",
                    "author_name": "Second",
                    "content": "second **message**\n[attachment: proof.png]",
                },
            ],
            "<@999>",
            "<#777>",
        )

        log = "\n".join(chunks)
        self.assertIn("**(2) messages cleared by (<@999>) in (<#777>)**", log)
        self.assertLess(log.index("first message"), log.index("second"))
        self.assertIn("<@101>", log)
        self.assertIn("<@102>", log)
        self.assertIn(r"second \*\*message\*\*", log)
        self.assertIn("[attachment: proof.png]", log)
        self.assertNotIn("\n\n", log)
        self.assertNotIn("\n> ", log)
        self.assertTrue(
            all(len(chunk) <= main.DISCORD_CONVERSATION_CHUNK_LIMIT for chunk in chunks)
        )

    def test_clear_log_splits_long_transcripts_without_losing_entries(self):
        chunks = main._format_clear_log_chunks(
            [
                {
                    "author_id": str(100 + index),
                    "author_name": f"Member {index}",
                    "content": f"message {index} " + ("x" * 700),
                }
                for index in range(1, 8)
            ],
            "<@999>",
            "<#777>",
            failed_count=1,
        )

        log = "\n".join(chunks)
        self.assertGreater(len(chunks), 1)
        self.assertIn("1 selected message could not be deleted", log)
        for index in range(1, 8):
            self.assertIn(f"message {index}", log)
        self.assertTrue(
            all(len(chunk) <= main.DISCORD_CONVERSATION_CHUNK_LIMIT for chunk in chunks)
        )

    def test_clear_log_preserves_a_full_long_message(self):
        long_message = "start " + ("x" * 1900) + " end"
        chunks = main._format_clear_log_chunks(
            [
                {
                    "author_id": "101",
                    "author_name": "Member",
                    "content": long_message,
                }
            ],
            "<@999>",
            "<#777>",
        )

        log = "\n".join(chunks)
        self.assertIn("start ", log)
        self.assertIn(" end", log)
        self.assertEqual(log.count("x"), 1900)
        self.assertIn("*(continued)*", log)
        self.assertNotIn("[truncated]", log)

    def test_clear_log_uses_patch_note_style_embeds(self):
        embeds = main._build_clear_log_embeds(["first transcript", "continued transcript"])

        self.assertEqual(len(embeds), 2)
        self.assertEqual(embeds[0].title, "Grim — Clear Log")
        self.assertEqual(embeds[1].title, "Grim — Clear Log Continued")
        self.assertEqual(embeds[0].description, "first transcript")
        self.assertEqual(embeds[1].description, "continued transcript")
        self.assertEqual(embeds[0].color.value, 0x121212)
        self.assertEqual(embeds[0].footer.text, main.xsi_footer())
        self.assertEqual(embeds[1].footer.text, main.xsi_footer())
        self.assertTrue(embeds[0].footer.text.startswith("Powered by XSI"))

    def test_ascii_gallery_titles_match_compact_curated_art(self):
        self.assertGreaterEqual(len(main.ASCII_ART_GALLERY), 8)
        titles = set()

        for title, art in main.ASCII_ART_GALLERY:
            titles.add(title)
            lines = art.strip("\n").splitlines()
            self.assertTrue(title)
            self.assertNotIn("```", art)
            self.assertLessEqual(len(lines), 16)
            self.assertLessEqual(max(map(len, lines)), 44)

        self.assertEqual(len(titles), len(main.ASCII_ART_GALLERY))

    def test_ascii_converter_builds_compact_art_from_a_silhouette(self):
        image = main.Image.new("L", (120, 120), 255)
        draw = main.ImageDraw.Draw(image)
        draw.ellipse((20, 25, 100, 105), fill=0)
        draw.polygon(((20, 35), (35, 5), (50, 35)), fill=0)
        draw.polygon(((70, 35), (85, 5), (100, 35)), fill=0)
        image_bytes = main.BytesIO()
        image.save(image_bytes, format="PNG")

        art = main._image_bytes_to_ascii(image_bytes.getvalue())

        self.assertIsNotNone(art)
        lines = art.splitlines()
        self.assertGreaterEqual(len(lines), 6)
        self.assertLessEqual(len(lines), 22)
        self.assertLessEqual(max(map(len, lines)), 48)
        self.assertTrue(any("@" in line for line in lines))

    def test_ascii_converter_rejects_invalid_or_blank_images(self):
        blank = main.Image.new("L", (50, 50), 255)
        image_bytes = main.BytesIO()
        blank.save(image_bytes, format="PNG")

        self.assertIsNone(main._image_bytes_to_ascii(b"not an image"))
        self.assertIsNone(main._image_bytes_to_ascii(image_bytes.getvalue()))

    def test_ascii_command_uses_gallery_title_and_embed(self):
        async def run():
            event = interaction()
            event.response.defer = AsyncMock()
            event.followup = SimpleNamespace(send=AsyncMock())
            generator = AsyncMock(return_value=(" /\\\n/__\\", "Mountain"))

            with (
                patch.object(main, "require_external_action", AsyncMock(return_value=True)),
                patch.object(main, "generate_leet_art", generator),
            ):
                await main.ascii_art.callback(event, "mountain")

            event.response.defer.assert_awaited_once()
            generator.assert_awaited_once_with("mountain")
            event.followup.send.assert_awaited_once()
            sent_embed = event.followup.send.await_args.kwargs["embed"]
            self.assertEqual(sent_embed.title, "Mountain")
            self.assertEqual(sent_embed.description, "```\n /\\\n/__\\\n```")

        asyncio.run(run())

    def test_clear_posts_only_to_the_configured_updates_channel(self):
        async def run():
            messages = [
                SimpleNamespace(
                    id=index,
                    created_at=main.datetime.now(main.timezone.utc),
                    author=SimpleNamespace(
                        id=100 + index,
                        display_name=f"Member {index}",
                    ),
                    content=f"message {index}",
                    attachments=[],
                    stickers=[],
                    delete=AsyncMock(),
                )
                for index in (1, 2)
            ]

            class SourceChannel:
                id = 777
                mention = "<#777>"

                async def history(self, limit, before):
                    for message in messages[:limit]:
                        yield message

                async def delete_messages(self, batch):
                    return None

            source_channel = SourceChannel()
            updates_channel = SimpleNamespace(
                id=888,
                guild=SimpleNamespace(id=50),
                permissions_for=lambda member: SimpleNamespace(send_messages=True),
                send=AsyncMock(),
            )
            event = interaction(manage_messages=True)
            event.guild.id = 50
            event.guild.me = object()
            event.user.mention = "<@10>"
            event.channel = source_channel
            event.created_at = main.datetime.now(main.timezone.utc)
            event.response.defer = AsyncMock()
            event.followup = SimpleNamespace(send=AsyncMock())
            event.delete_original_response = AsyncMock()

            with (
                patch.dict(
                    main.updates_channels,
                    {"50": {"channel_id": "888"}},
                    clear=True,
                ),
                patch.object(main.bot, "get_channel", return_value=updates_channel),
            ):
                await main.clear.callback(event, 2)

            event.response.defer.assert_awaited_once_with(ephemeral=True)
            event.delete_original_response.assert_awaited_once()
            event.followup.send.assert_not_awaited()
            updates_channel.send.assert_awaited_once()
            self.assertEqual(updates_channel.send.await_args.args, ())
            logged_embed = updates_channel.send.await_args.kwargs["embed"]
            self.assertEqual(logged_embed.title, "Grim — Clear Log")
            logged = logged_embed.description
            self.assertIn("(2) messages cleared by (<@10>) in (<#777>)", logged)
            self.assertLess(logged.index("message 2"), logged.index("message 1"))

        asyncio.run(run())

    def test_extensionless_discord_image_uses_content_type(self):
        attachment = SimpleNamespace(
            url="https://cdn.discordapp.com/attachments/123/456/upload",
            content_type="image/png",
        )
        self.assertEqual(main._attachment_media_kind(attachment), "image")

    def test_unsupported_image_type_is_not_sent_to_vision_model(self):
        attachment = SimpleNamespace(
            url="https://cdn.discordapp.com/attachments/123/456/animated.gif",
            content_type="image/gif",
        )
        self.assertIsNone(main._attachment_media_kind(attachment))

    def test_vision_failure_falls_back_to_text_model(self):
        async def run():
            with patch.dict(
                main.os.environ,
                {"XAI_VISION_MODEL": "grok-4.3", "XAI_TEXT_MODEL": "grok-3"},
            ), patch.object(
                main,
                "_post_contextual_completion",
                side_effect=[None, "i couldn't inspect it, but i can still help."],
            ) as complete:
                result = await main._complete_contextual_reply(
                    object(),
                    {"Authorization": "redacted"},
                    "system",
                    [{"role": "user", "content": [{"type": "image_url"}]}],
                    [{"role": "user", "content": "text fallback"}],
                    True,
                )

            self.assertEqual(result, "i couldn't inspect it, but i can still help.")
            self.assertEqual(complete.await_count, 2)
            self.assertEqual(complete.await_args_list[0].args[2]["model"], "grok-4.3")
            self.assertEqual(complete.await_args_list[1].args[2]["model"], "grok-3")
            self.assertEqual(complete.await_args_list[0].args[4], 10)
            self.assertEqual(complete.await_args_list[1].args[4], 15)
            self.assertEqual(
                complete.await_args_list[1].args[2]["messages"][-1]["content"],
                "text fallback",
            )

        asyncio.run(run())

    def test_unresolved_reply_is_fetched_from_discord(self):
        async def run():
            fetched = object()
            message = SimpleNamespace(
                reference=SimpleNamespace(resolved=None, message_id=123),
                channel=SimpleNamespace(fetch_message=AsyncMock(return_value=fetched)),
            )

            result, unavailable = await main._resolve_replied_message(message)

            self.assertIs(result, fetched)
            self.assertFalse(unavailable)
            message.channel.fetch_message.assert_awaited_once_with(123)

        asyncio.run(run())

    def test_denied_reply_fetch_is_marked_unavailable(self):
        async def run():
            message = SimpleNamespace(
                reference=SimpleNamespace(resolved=None, message_id=123),
                channel=SimpleNamespace(
                    fetch_message=AsyncMock(side_effect=PermissionError("denied"))
                ),
            )

            result, unavailable = await main._resolve_replied_message(message)

            self.assertIsNone(result)
            self.assertTrue(unavailable)

        asyncio.run(run())

    def test_stalled_reply_fetch_times_out_as_unavailable(self):
        async def run():
            async def stalled_fetch(_message_id):
                await asyncio.sleep(0.05)
                return object()

            message = SimpleNamespace(
                reference=SimpleNamespace(resolved=None, message_id=123),
                channel=SimpleNamespace(fetch_message=stalled_fetch),
            )
            with patch.object(
                main, "DISCORD_REPLY_FETCH_TIMEOUT_SECONDS", 0.001
            ):
                result, unavailable = await main._resolve_replied_message(message)

            self.assertIsNone(result)
            self.assertTrue(unavailable)

        asyncio.run(run())

    def test_giphy_is_used_when_klipy_has_no_result(self):
        async def run():
            with patch.object(
                main, "_search_klipy_gif", return_value=None
            ) as klipy, patch.object(
                main,
                "_search_giphy_gif",
                return_value="https://media.giphy.com/reaction.gif",
            ) as giphy:
                result = await main._search_reaction_gif(object(), "side eye")
            self.assertEqual(
                result, ("https://media.giphy.com/reaction.gif", "GIPHY")
            )
            klipy.assert_awaited_once()
            giphy.assert_awaited_once()

        asyncio.run(run())

    def test_manual_gif_history_is_scoped_by_server_and_topic(self):
        url = "https://media.example/one.gif"
        identities = ["klipy:asset-one", main._gif_url_fingerprint(url)]
        self.assertTrue(
            main._reserve_gif_command_result("50", "side eye", identities)
        )

        fingerprint = main._gif_url_fingerprint(url)
        self.assertIn(fingerprint, main._gif_command_history["50"]["side eye"])
        self.assertIn("klipy:asset-one", main._gif_command_history["50"]["side eye"])
        self.assertNotIn("51", main._gif_command_history)
        self.assertNotIn("victory", main._gif_command_history["50"])
        self.assertNotIn(url, json.dumps(main._gif_command_history))

    def test_manual_gif_search_skips_seen_and_falls_back_to_giphy(self):
        async def run():
            seen_url = "https://media.example/seen.gif"
            fresh_url = "https://media.example/fresh.gif"
            self.assertTrue(
                main._reserve_gif_command_result(
                    "50",
                    "side eye",
                    ["klipy:seen", main._gif_url_fingerprint(seen_url)],
                )
            )
            with patch.object(
                main,
                "_search_klipy_gif_candidates",
                return_value=[{
                    "url": seen_url,
                    "identities": ["klipy:seen", main._gif_url_fingerprint(seen_url)],
                }],
            ) as klipy, patch.object(
                main,
                "_search_giphy_gif_candidates",
                return_value=[{
                    "url": fresh_url,
                    "identities": ["giphy:fresh", main._gif_url_fingerprint(fresh_url)],
                }],
            ) as giphy:
                result = await main._search_unseen_command_gif(
                    object(), "50", "side eye"
                )

            self.assertEqual(
                result,
                (
                    fresh_url,
                    "GIPHY",
                    ["giphy:fresh", main._gif_url_fingerprint(fresh_url)],
                ),
            )
            klipy.assert_awaited_once()
            giphy.assert_awaited_once()

        asyncio.run(run())

    def test_manual_gif_history_survives_reload(self):
        url = "https://media.example/persisted.gif"
        self.assertTrue(
            main._reserve_gif_command_result(
                "50",
                "celebration",
                ["giphy:persisted", main._gif_url_fingerprint(url)],
            )
        )

        reloaded = main._load_gif_command_history()

        self.assertEqual(reloaded, main._gif_command_history)

    def test_manual_gif_reservation_rolls_back_when_persistence_fails(self):
        with patch.object(
            main, "_atomic_json_write", side_effect=OSError("disk unavailable")
        ):
            with self.assertRaises(OSError):
                main._reserve_gif_command_result(
                    "50", "reaction", ["giphy:reserved", "url-hash"]
                )

        self.assertNotIn("50", main._gif_command_history)

    def test_manual_gif_reservation_rejects_cross_process_collision(self):
        identities = ["giphy:same-asset", "same-url-hash"]
        self.assertTrue(
            main._reserve_gif_command_result("50", "reaction", identities)
        )
        main._gif_command_history = {}

        self.assertFalse(
            main._reserve_gif_command_result("50", "reaction", identities)
        )
        self.assertIn(
            "giphy:same-asset",
            main._gif_command_history["50"]["reaction"],
        )

    def test_manual_gif_guild_rate_limit_is_shared(self):
        with patch.object(main.time, "time", return_value=1000):
            for _ in range(main.GIF_COMMAND_GUILD_RATE_LIMIT_MAX):
                self.assertTrue(main._gif_command_guild_rate_limit_allows("50"))
            self.assertFalse(main._gif_command_guild_rate_limit_allows("50"))
            self.assertTrue(main._gif_command_guild_rate_limit_allows("51"))

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
        self.assertTrue(embed.footer.text.startswith("Powered by XSI"))

    def test_seclude_command_is_public_and_cannot_ping(self):
        names = {command.name for command in main.bot.tree.get_commands()}
        self.assertIn("seclude", names)
        self.assertIn("server", names)
        self.assertIn("vc_join", names)
        self.assertIn("vc_leave", names)

        command = main.seclude
        self.assertIsNone(command.default_permissions)
        self.assertNotIn("admin", command.description.lower())
        self.assertFalse(command.guild_only)

        visuals = (
            "Darkwear, tech-brutalism, and matte greys/blacks visually mirror the feeling "
            "of isolation and stepping back from a loud, overwhelming world. It feels like "
            "a quiet, reinforced concrete bunker away from mainstream noise."
        )
        purpose = (
            "Instead of letting that isolation turn into stagnation, the Japanese philosophy "
            "of Kaizen—focusing on tiny, disciplined, 1% daily improvements—gives members "
            "a concrete, stoic framework to rebuild and sharpen themselves quietly."
        )
        outlet = (
            "Having a dedicated space for dense geopolitical and retrospective discussions "
            "naturally attracts people who feel alienated by surface-level small talk elsewhere, "
            "giving their exulansis an actual, grounded place to land."
        )
        closing = (
            "It feels less like a standard chat room and more like an underground "
            "think-tank or a modern dojo."
        )
        invite = "https://discord.com/invite/KFcpDGtckz"
        guild_icon = "https://cdn.example/guild.png"
        grim_avatar = "https://cdn.example/grim.png"
        expected = "\n\n".join((
            f"**The Visuals**\n{visuals}",
            f"**The Purpose**\n{purpose}",
            f"**The Outlet**\n{outlet}",
            f"*{closing}*",
        ))

        event = slash_interaction()
        event.guild.icon = SimpleNamespace(url=guild_icon)
        event.client = SimpleNamespace(
            user=SimpleNamespace(display_avatar=SimpleNamespace(url=grim_avatar))
        )
        embed = main.build_seclude_embed(event)
        description = embed.description or ""
        payload = embed.to_dict()

        self.assertEqual(embed.title, "𖦏 𝕾𝖊𝖈𝖑𝖚𝖉𝖊")
        self.assertEqual(embed.title, main.SECLUDE_TITLE)
        self.assertNotEqual(embed.title, "SECLUDE")
        self.assertNotEqual(embed.title, "S E C L U D E")
        self.assertNotIn("SECLUDE", embed.title)
        self.assertNotIn("S E C L U D E", embed.title)
        self.assertIsNone(embed.url)
        self.assertNotIn("url", payload)
        self.assertEqual(embed.color.value, discord.Color.from_rgb(27, 26, 30).value)
        self.assertEqual(embed.color.value, 0x1B1A1E)
        self.assertNotEqual(embed.color.value, 0x6E6E6E)
        self.assertNotEqual(embed.color.value, 0x1A1A1A)
        self.assertNotEqual(embed.color.value, 0x121212)
        self.assertNotEqual(embed.color.value, 0x5865F2)
        self.assertEqual(len(embed.fields), 0)
        self.assertFalse(hasattr(main, "SECLUDE_DIVIDER"))
        self.assertNotIn("\u2500", description)
        self.assertNotIn("----------", description)
        self.assertEqual(description, expected)
        self.assertTrue(description.endswith(f"*{closing}*"))
        self.assertIn("Kaizen\u2014focusing", description)
        self.assertIn("improvements\u2014gives", description)
        self.assertIn("exulansis", description)
        self.assertEqual(description.count(closing), 1)
        self.assertNotIn(invite, description)
        self.assertNotIn(invite, embed.title)
        self.assertNotIn(invite, embed.footer.text)
        self.assertNotIn("](", description)
        self.assertNotIn("Enter", description)
        self.assertEqual(embed.footer.text, main.xsi_footer())
        self.assertTrue(embed.footer.text.startswith("Powered by XSI"))
        self.assertIn(main.VERSION, embed.footer.text)
        self.assertNotIn(closing, embed.footer.text)
        self.assertEqual(embed.thumbnail.url, guild_icon)
        self.assertEqual(embed.footer.icon_url, grim_avatar)
        self.assertLessEqual(len(embed.title), 256)
        self.assertLessEqual(len(description), 4096)
        self.assertLessEqual(len(embed.footer.text), 2048)
        self.assertLessEqual(
            len(embed.title) + len(description) + len(embed.footer.text),
            6000,
        )

        view = main.build_seclude_view()
        buttons = [child for child in view.children if isinstance(child, discord.ui.Button)]
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].label, "Enter")
        self.assertEqual(buttons[0].url, invite)
        self.assertEqual(buttons[0].style, discord.ButtonStyle.link)
        self.assertLessEqual(len(buttons[0].label), 80)
        self.assertLessEqual(len(buttons[0].url), 512)

        async def run():
            await main.seclude.callback(event)
            self.assertEqual(event.response.deferred, [])
            self.assertEqual(event.followup.sent, [])
            self.assertEqual(len(event.response.sent), 1)
            args, kwargs = event.response.sent[0]
            self.assertEqual(args, ())
            self.assertFalse(kwargs.get("ephemeral", False))
            self.assertIsNone(kwargs.get("content"))
            sent = kwargs["embed"]
            self.assertEqual(sent.title, "𖦏 𝕾𝖊𝖈𝖑𝖚𝖉𝖊")
            self.assertEqual(sent.title, main.SECLUDE_TITLE)
            self.assertIsNone(sent.url)
            self.assertNotIn("url", sent.to_dict())
            self.assertEqual(sent.description, expected)
            self.assertEqual(len(sent.fields), 0)
            self.assertEqual(sent.color.value, 0x1B1A1E)
            self.assertEqual(sent.thumbnail.url, guild_icon)
            self.assertEqual(sent.footer.text, main.xsi_footer())
            self.assertEqual(sent.footer.icon_url, grim_avatar)
            self.assertNotIn(invite, sent.description)
            sent_buttons = [
                child for child in kwargs["view"].children
                if isinstance(child, discord.ui.Button)
            ]
            self.assertEqual([(button.label, button.url) for button in sent_buttons], [("Enter", invite)])
            self.assertEqual(kwargs["allowed_mentions"].to_dict(), {"parse": []})
            self.assertFalse(kwargs["allowed_mentions"].everyone)
            self.assertFalse(kwargs["allowed_mentions"].users)
            self.assertFalse(kwargs["allowed_mentions"].roles)

        asyncio.run(run())

        ctx = FakeContext()
        asyncio.run(main.help_grim.callback(ctx))
        help_embed = ctx.messages[0]["embed"]
        self.assertIn("`/seclude`", help_embed.description)
        help_fields = {field.name: field.value for field in help_embed.fields}
        self.assertEqual(help_fields["/server"], "Server status")

    def test_seclude_thumbnail_falls_back_to_grim_avatar(self):
        grim_avatar = "https://cdn.example/grim.png"
        grim = SimpleNamespace(
            user=SimpleNamespace(display_avatar=SimpleNamespace(url=grim_avatar))
        )
        no_icon = SimpleNamespace(guild=SimpleNamespace(icon=None), client=grim)
        dm = SimpleNamespace(guild=None, client=grim)

        for event in (no_icon, dm):
            embed = main.build_seclude_embed(event)
            self.assertEqual(embed.title, "𖦏 𝕾𝖊𝖈𝖑𝖚𝖉𝖊")
            self.assertEqual(embed.thumbnail.url, grim_avatar)
            self.assertEqual(embed.footer.icon_url, grim_avatar)
            self.assertEqual(embed.footer.text, main.xsi_footer())

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


class BlockingClientTests(unittest.TestCase):
    def test_oneshot_xai_calls_run_off_the_event_loop_with_a_timeout(self):
        seen = {}

        def create(**kwargs):
            seen["thread"] = threading.get_ident()
            seen["timeout"] = kwargs.get("timeout")
            message = SimpleNamespace(content="Outlook grim.")
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

        async def run():
            seen["loop_thread"] = threading.get_ident()
            with patch.object(main, "get_grok_client", return_value=client), patch.object(
                main, "screen_generated_output", AsyncMock(side_effect=lambda text, **_: text)
            ):
                return await main.generate_8ball_response("will it rain?")

        self.assertEqual(asyncio.run(run()), "Outlook grim.")
        self.assertNotEqual(seen["thread"], seen["loop_thread"])
        self.assertEqual(seen["timeout"], main.XAI_ONESHOT_TIMEOUT_SECONDS)

    def test_x_api_calls_time_out_instead_of_hanging(self):
        release = threading.Event()

        def hang(**_):
            release.wait(5)

        async def run():
            try:
                with patch.object(main, "X_API_TIMEOUT_SECONDS", 0.05):
                    await main._x_api_call(hang, username="grim")
            finally:
                release.set()

        with self.assertRaises(asyncio.TimeoutError):
            asyncio.run(run())


if __name__ == "__main__":
    unittest.main()