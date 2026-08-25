import asyncio
import json
import sqlite3
import tempfile
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
        main.CHAT_DB_FILE = str(Path(self.temp_dir.name) / "chat_history.db")
        main.MODERATION_FILE = str(Path(self.temp_dir.name) / "moderation_data.json")
        main.moderation_data = {"guilds": {}, "legacy_unassigned_words": []}
        main._COMMAND_RATE_LIMITS.clear()
        main.init_chat_db()

    def tearDown(self):
        main.CHAT_DB_FILE = self.original_db
        main.MODERATION_FILE = self.original_moderation_file
        main.moderation_data = self.original_moderation_data
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


if __name__ == "__main__":
    unittest.main()