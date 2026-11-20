import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, PropertyMock, patch

import main


GRIM = SimpleNamespace(id=999, display_name="Grim", name="Grim", global_name="Grim")
OP = SimpleNamespace(id=10, display_name="Kōshin", name="koshin", global_name="Kōshin")
OTHER = SimpleNamespace(id=20, display_name="Noct", name="noct", global_name="Noct")
CHANNEL = SimpleNamespace(id=9)
OTHER_CHANNEL = SimpleNamespace(id=11)
T0 = 1_700_000_000.0
OPENER = "yo grim how we doing my fam"
FOLLOW_UP = "thats wsp. Whats the weather like today?"
GRIM_REPLY_ID = 100
OPENER_ID = 1
FOLLOW_ID = 2


def reply_to(target_id, target_author=GRIM):
    return SimpleNamespace(
        message_id=target_id,
        resolved=SimpleNamespace(id=target_id, author=target_author),
    )


def chat_message(
    content,
    *,
    author=OP,
    channel=CHANNEL,
    mentions=None,
    reference=None,
    mid=OPENER_ID,
    guild_members=None,
):
    members = list(guild_members or [OP, OTHER, GRIM])
    return SimpleNamespace(
        id=mid,
        content=content,
        author=author,
        channel=channel,
        mentions=list(mentions or []),
        reference=reference,
        guild=SimpleNamespace(id=50, members=members),
    )


class ConversationPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.user_patch = patch.object(
            main.discord.Client,
            "user",
            new_callable=PropertyMock,
            return_value=GRIM,
        )
        self.user_patch.start()
        main._open_conversations.clear()
        main._conversation_ladders.clear()
        main._conversation_media_kind.clear()
        main._PENDING_LIVE_ASKS.clear()

    def tearDown(self):
        self.user_patch.stop()
        main._open_conversations.clear()
        main._conversation_ladders.clear()
        main._conversation_media_kind.clear()
        main._PENDING_LIVE_ASKS.clear()

    def start_opener(self, *, now=T0, grim_reply_id=GRIM_REPLY_ID):
        opener = chat_message(OPENER, mid=OPENER_ID)
        self.assertTrue(main.grim_turn_needs_reply(OPENER))
        self.assertTrue(main.grim_should_reply_to_message(opener))
        self.assertTrue(main.grim_chat_turn_action(opener, now=now))
        main.grim_register_ladder_ids(opener, [grim_reply_id])
        return opener

    def test_idle_timeout_is_five_minutes(self):
        self.assertEqual(main.CONVERSATION_IDLE_SECONDS, 300)

    def test_name_call_starts_a_conversation(self):
        opener = self.start_opener()
        self.assertTrue(main.grim_conversation_open(opener, now=T0))
        self.assertIn(str(OPENER_ID), main._conversation_ladders[("9", "10")])
        self.assertIn(str(GRIM_REPLY_ID), main._conversation_ladders[("9", "10")])

    def test_follow_up_without_the_name_still_replies_in_the_reply_ladder(self):
        self.start_opener()
        follow = chat_message(
            FOLLOW_UP,
            mid=FOLLOW_ID,
            reference=reply_to(GRIM_REPLY_ID),
        )
        self.assertFalse(main.grim_turn_needs_reply(FOLLOW_UP))
        self.assertFalse(main.grim_should_reply_to_message(follow))
        self.assertTrue(main._follow_up_in_reply_ladder(follow))
        self.assertTrue(main.grim_chat_turn_action(follow, now=T0 + 10))
        self.assertTrue(main._needs_live_sources(FOLLOW_UP, ""))

    def test_discord_reply_to_the_opener_still_counts(self):
        self.start_opener()
        follow = chat_message(
            FOLLOW_UP,
            mid=FOLLOW_ID,
            reference=reply_to(OPENER_ID, target_author=OP),
        )
        self.assertTrue(main.grim_chat_turn_action(follow, now=T0 + 8))

    def test_same_channel_follow_up_without_a_discord_reply_still_replies(self):
        opener = self.start_opener()
        lines = (
            "how we doing boss man",
            "bro",
            "can you help please?",
            "Grim",
            "you there bruh",
            FOLLOW_UP,
        )
        for idx, text in enumerate(lines, start=FOLLOW_ID):
            with self.subTest(text=text):
                bare = chat_message(text, mid=idx, reference=None)
                if main.grim_name_call_is_bare(text):
                    self.assertTrue(main.grim_should_reply_to_message(bare))
                else:
                    self.assertFalse(main.grim_should_reply_to_message(bare))
                self.assertTrue(main.grim_chat_turn_action(bare, now=T0 + idx))
        self.assertTrue(main.grim_conversation_open(opener, now=T0 + FOLLOW_ID))
        self.assertTrue(main._needs_live_sources(FOLLOW_UP, ""))

    def test_timeout_ends_the_conversation(self):
        self.start_opener()
        follow = chat_message(
            FOLLOW_UP,
            mid=FOLLOW_ID,
            reference=reply_to(GRIM_REPLY_ID),
        )
        self.assertTrue(
            main.grim_conversation_open(follow, now=T0 + main.CONVERSATION_IDLE_SECONDS)
        )
        self.assertFalse(
            main.grim_conversation_open(
                follow, now=T0 + main.CONVERSATION_IDLE_SECONDS + 1
            )
        )
        self.assertFalse(
            main.grim_chat_turn_action(
                follow, now=T0 + main.CONVERSATION_IDLE_SECONDS + 1
            )
        )
        bare = chat_message("you there bruh", mid=3, reference=None)
        self.assertFalse(
            main.grim_chat_turn_action(
                bare, now=T0 + main.CONVERSATION_IDLE_SECONDS + 1
            )
        )

    def test_lone_grim_ping_is_a_bare_name_call_and_still_continues(self):
        ping = chat_message("Grim", mid=FOLLOW_ID, reference=None)
        self.assertTrue(main.grim_name_call_is_bare("Grim"))
        self.assertTrue(main.grim_should_reply_to_message(ping))
        self.assertTrue(main.grim_chat_turn_action(ping, now=T0))
        self.assertTrue(main.grim_conversation_open(ping, now=T0))
        later = chat_message("you there bruh", mid=3, reference=None)
        self.assertTrue(main.grim_chat_turn_action(later, now=T0 + 4))

    def test_saying_this_was_for_someone_else_ends_it_and_stays_quiet(self):
        opener = self.start_opener()
        aside = chat_message(
            "I was replying to Cynibal no need to reply to me now",
            mid=FOLLOW_ID,
            reference=None,
        )
        self.assertTrue(main.grim_says_talking_to_someone_else(aside))
        self.assertTrue(main.grim_asks_not_to_reply(aside))
        self.assertTrue(main.grim_op_releases_conversation(aside))
        self.assertFalse(main.grim_chat_turn_action(aside, now=T0 + 12))
        self.assertFalse(main.grim_conversation_open(opener, now=T0 + 12))
        later = chat_message("you there bruh", mid=3, reference=None)
        self.assertFalse(main.grim_chat_turn_action(later, now=T0 + 13))

    def test_telling_him_not_to_reply_ends_even_with_his_name(self):
        self.start_opener()
        aside = chat_message(
            "grim no need to reply to me now",
            mid=FOLLOW_ID,
            reference=None,
        )
        self.assertTrue(main.grim_asks_not_to_reply(aside))
        self.assertFalse(main.grim_chat_turn_action(aside, now=T0 + 12))
        later = chat_message("how we doing boss man", mid=3, reference=None)
        self.assertFalse(main.grim_chat_turn_action(later, now=T0 + 13))

    def test_i_was_talking_to_another_member_ends_it(self):
        self.start_opener()
        aside = chat_message(
            "i was talking to noct",
            mid=FOLLOW_ID,
            reference=None,
        )
        self.assertTrue(main.grim_says_talking_to_someone_else(aside))
        self.assertFalse(main.grim_chat_turn_action(aside, now=T0 + 12))

    def test_addressing_someone_else_by_mention_ends_it(self):
        opener = self.start_opener()
        aside = chat_message(
            "hey check this",
            mid=FOLLOW_ID,
            mentions=[OTHER],
            reference=None,
        )
        self.assertTrue(main.grim_follow_up_addresses_someone_else(aside))
        self.assertFalse(main.grim_chat_turn_action(aside, now=T0 + 12))
        self.assertFalse(main.grim_conversation_open(opener, now=T0 + 12))
        later = chat_message(
            FOLLOW_UP,
            mid=3,
            reference=None,
        )
        self.assertFalse(main.grim_chat_turn_action(later, now=T0 + 13))

    def test_addressing_someone_else_by_name_ends_it(self):
        self.start_opener()
        aside = chat_message(
            "hey noct you seeing this?",
            mid=FOLLOW_ID,
            reference=None,
        )
        self.assertTrue(main.grim_follow_up_addresses_someone_else(aside))
        self.assertFalse(main.grim_chat_turn_action(aside, now=T0 + 12))

    def test_replying_to_a_third_party_ends_it(self):
        opener = self.start_opener()
        aside = chat_message(
            "hold up",
            mid=FOLLOW_ID,
            reference=reply_to(50, target_author=OTHER),
        )
        self.assertTrue(main.grim_follow_up_addresses_someone_else(aside))
        self.assertFalse(main.grim_chat_turn_action(aside, now=T0 + 12))
        self.assertFalse(main.grim_conversation_open(opener, now=T0 + 12))

    def test_third_party_talk_does_not_keep_grim_in(self):
        opener = self.start_opener()
        side = chat_message(
            "yeah that weather sucks",
            author=OTHER,
            mid=7,
            reference=None,
        )
        self.assertFalse(main.grim_should_reply_to_message(side))
        self.assertFalse(main.grim_chat_turn_action(side, now=T0 + 200))
        self.assertTrue(main.grim_conversation_open(opener, now=T0 + 200))
        expired = chat_message(
            FOLLOW_UP,
            mid=FOLLOW_ID,
            reference=None,
        )
        self.assertFalse(
            main.grim_chat_turn_action(
                expired, now=T0 + main.CONVERSATION_IDLE_SECONDS + 1
            )
        )

    def test_third_party_name_call_is_their_own_conversation(self):
        self.start_opener()
        other_call = chat_message(
            "grim you in?",
            author=OTHER,
            mid=8,
        )
        self.assertTrue(main.grim_chat_turn_action(other_call, now=T0 + 5))
        self.assertTrue(main.grim_conversation_open(other_call, now=T0 + 5))
        op_follow = chat_message(
            FOLLOW_UP,
            mid=FOLLOW_ID,
            reference=reply_to(GRIM_REPLY_ID),
        )
        self.assertTrue(main.grim_chat_turn_action(op_follow, now=T0 + 6))

    def test_at_mention_name_call_also_starts_the_ladder(self):
        ping = chat_message(
            "<@999> how we doing?",
            mentions=[GRIM],
            mid=OPENER_ID,
        )
        self.assertTrue(main.grim_chat_turn_action(ping, now=T0))
        main.grim_register_ladder_ids(ping, [GRIM_REPLY_ID])
        follow = chat_message(
            FOLLOW_UP,
            mid=FOLLOW_ID,
            reference=reply_to(GRIM_REPLY_ID),
        )
        self.assertTrue(main.grim_chat_turn_action(follow, now=T0 + 4))

    def test_follow_up_refresh_extends_the_idle_window(self):
        self.start_opener()
        follow = chat_message(
            FOLLOW_UP,
            mid=FOLLOW_ID,
            reference=None,
        )
        self.assertTrue(main.grim_chat_turn_action(follow, now=T0 + 200))
        later = chat_message(
            "and tonight?",
            mid=3,
            reference=None,
        )
        self.assertTrue(main.grim_chat_turn_action(later, now=T0 + 400))
        self.assertFalse(
            main.grim_chat_turn_action(
                chat_message(
                    "still there?",
                    mid=4,
                    reference=None,
                ),
                now=T0 + 400 + main.CONVERSATION_IDLE_SECONDS + 1,
            )
        )

    def test_other_channel_does_not_continue_the_ladder(self):
        self.start_opener()
        other = chat_message(
            FOLLOW_UP,
            channel=OTHER_CHANNEL,
            mid=FOLLOW_ID,
            reference=None,
        )
        self.assertFalse(main.grim_chat_turn_action(other, now=T0 + 10))
        ladder = chat_message(
            FOLLOW_UP,
            channel=OTHER_CHANNEL,
            mid=3,
            reference=reply_to(GRIM_REPLY_ID),
        )
        self.assertFalse(main.grim_chat_turn_action(ladder, now=T0 + 11))

    def test_on_message_uses_the_conversation_gate(self):
        source = Path(main.__file__).read_text()
        self.assertIn("grim_chat_turn_action(message)", source)
        self.assertIn("grim_should_reply_to_message(message)", source)
        self.assertIn("CONVERSATION_IDLE_SECONDS = 300", source)
        action = source[
            source.index("def grim_chat_turn_action") : source.index(
                "async def generate_haiku"
            )
        ]
        self.assertNotIn("[REDACTED]", action)
        self.assertIn("without a Discord", action)
        miss = 'Live Search has zero info on this'
        self.assertIn(miss, source)
        self.assertNotIn(miss, action)

    def test_different_operators_never_share_hold_memory(self):
        import tempfile
        from pathlib import Path as _Path

        original_db = main.CHAT_DB_FILE
        temp_dir = tempfile.TemporaryDirectory()
        try:
            main.CHAT_DB_FILE = str(_Path(temp_dir.name) / "chat_history.db")
            main.init_chat_db()
            main.save_message_to_db(
                "50", "9", "a1", "Death",
                "the kardashev scale type i sits around planetary watts",
                1.0, False, author_id="99",
            )
            main.save_message_to_db(
                "50", "9", "a2", "Kōshin",
                "right or wrong on this meme",
                2.0, False, author_id="10",
            )
            koshin = main.get_operator_channel_history_from_db(
                "50", "9", author_id="10", author_name="Kōshin", limit=20,
            )
            death = main.get_operator_channel_history_from_db(
                "50", "9", author_id="99", author_name="Death", limit=20,
            )
            self.assertEqual(len(koshin), 1)
            self.assertIn("right or wrong", koshin[0][1])
            self.assertNotIn("kardashev", " ".join(row[1] for row in koshin).casefold())
            self.assertEqual(len(death), 1)
            self.assertIn("kardashev", death[0][1].casefold())
            self.assertEqual(
                main._research_scope_key("50", "9", "10"),
                "50:9:10",
            )
            self.assertNotEqual(
                main._research_scope_key("50", "9", "10"),
                main._research_scope_key("50", "9", "99"),
            )
        finally:
            main.CHAT_DB_FILE = original_db
            temp_dir.cleanup()

    def test_same_user_other_channel_uncorrelated_ask_does_not_dump(self):
        import tempfile
        from pathlib import Path as _Path

        original_db = main.CHAT_DB_FILE
        temp_dir = tempfile.TemporaryDirectory()
        try:
            main.CHAT_DB_FILE = str(_Path(temp_dir.name) / "chat_history.db")
            main.init_chat_db()
            main.save_message_to_db(
                "50", "9", "c1", "Oxy",
                "kardashev type i planetary insolation breakdown",
                1.0, False, author_id="20",
            )
            main.save_message_to_db(
                "50", "9", "c2", "Zed",
                "yeah listening to that kardashev talk",
                2.0, False, author_id="10",
            )
            related = main.correlated_foreign_channel_context(
                "50",
                "11",
                author_id="10",
                author_name="Zed",
                ask_text="yo grim how we doing",
                limit=4,
            )
            self.assertEqual(related, [])
        finally:
            main.CHAT_DB_FILE = original_db
            temp_dir.cleanup()

    def test_same_user_other_channel_correlated_ask_gets_limited_context(self):
        import tempfile
        from pathlib import Path as _Path

        original_db = main.CHAT_DB_FILE
        temp_dir = tempfile.TemporaryDirectory()
        try:
            main.CHAT_DB_FILE = str(_Path(temp_dir.name) / "chat_history.db")
            main.init_chat_db()
            main.save_message_to_db(
                "50", "9", "c1", "Oxy",
                "kardashev type i planetary insolation breakdown",
                1.0, False, author_id="20",
            )
            main.save_message_to_db(
                "50", "9", "c2", "Zed",
                "yeah listening to that kardashev talk",
                2.0, False, author_id="10",
            )
            main.save_message_to_db(
                "50", "9", "c3", "Oxy",
                "unrelated pizza topping debate about pineapple",
                3.0, False, author_id="20",
            )
            related = main.correlated_foreign_channel_context(
                "50",
                "11",
                author_id="10",
                author_name="Zed",
                ask_text="grim remind me what the kardashev breakdown was",
                limit=4,
            )
            self.assertTrue(related)
            blob = " ".join(item["content"] for item in related).casefold()
            self.assertIn("kardashev", blob)
            self.assertIn("related from", blob)
            self.assertNotIn("pineapple", blob)
            self.assertLessEqual(len(related), 4)
        finally:
            main.CHAT_DB_FILE = original_db
            temp_dir.cleanup()

    def test_reply_payload_keeps_ten_newest_turns(self):
        import tempfile
        from pathlib import Path as _Path

        original_db = main.CHAT_DB_FILE
        temp_dir = tempfile.TemporaryDirectory()
        try:
            main.CHAT_DB_FILE = str(_Path(temp_dir.name) / "chat_history.db")
            main.init_chat_db()
            for index in range(1, 16):
                main.save_message_to_db(
                    "50", "9", f"p{index:02d}", "Kōshin",
                    f"prior-{index:02d} still talking",
                    float(index), False, author_id="10",
                )
            main.save_message_to_db(
                "50", "9", "other", "Noct",
                "kardashev secret from someone else",
                0.5, False, author_id="20",
            )
            main.save_message_to_db(
                "50", "9", "cur", "Kōshin",
                "still here with you",
                16.0, False, author_id="10",
            )
            stored = main.get_operator_channel_history_from_db(
                "50", "9", author_id="10", author_name="Kōshin", limit=50,
            )
            self.assertGreater(len(stored), main.MODEL_REPLY_HISTORY_MESSAGES)
            self.assertIn("prior-01", stored[0][1])
            self.assertEqual(main.MODEL_REPLY_HISTORY_MESSAGES, 10)
            self.assertEqual(main.LIVE_SEARCH_MAX_TOKENS, 600)

            captured = {}

            async def complete(
                session, headers, system_prompt, chat_messages,
                text_fallback, has_images,
            ):
                captured["system"] = system_prompt
                captured["messages"] = [dict(item) for item in chat_messages]
                return "the river bit still lands, and i'm still on it with you."

            channel = SimpleNamespace(id=9, name="general")

            async def empty_history(limit=50, before=None):
                if False:
                    yield None

            channel.history = empty_history
            message = SimpleNamespace(
                id="cur",
                content="still here with you",
                author=SimpleNamespace(id=10, display_name="Kōshin", bot=False),
                channel=channel,
                mentions=[],
                reference=None,
                attachments=[],
                embeds=[],
                guild=SimpleNamespace(
                    id=50, name="Seclude", member_count=3, members=[OP, OTHER, GRIM],
                ),
            )

            async def run():
                with patch.dict(main.os.environ, {"XAI_API_KEY": "test-key"}), \
                        patch.object(main, "_complete_contextual_reply", complete), \
                        patch.object(main, "grok_research_query", AsyncMock()) as research:
                    reply = await main.generate_contextual_reply(message)
                    captured["research_called"] = research.await_count
                    return reply

            reply = asyncio.run(run())
            self.assertTrue(reply.text)
            self.assertEqual(captured["research_called"], 0)
            messages = captured["messages"]
            history = messages[:-1]
            self.assertEqual(len(history), 10)
            blob = " ".join(item["content"] for item in messages)
            self.assertNotIn("prior-01", blob)
            self.assertNotIn("prior-05", blob)
            self.assertIn("prior-06", blob)
            self.assertIn("prior-15", blob)
            self.assertNotIn("kardashev", blob.casefold())
            self.assertEqual(blob.count("still here with you"), 1)
            self.assertIn("HOW GRIM SPEAKS", captured["system"])
            self.assertIn("CONVERSATION SCOPE (HARD RULE)", captured["system"])
        finally:
            main.CHAT_DB_FILE = original_db
            temp_dir.cleanup()

    def test_discord_history_fallback_payload_is_capped_at_ten(self):
        import tempfile
        from pathlib import Path as _Path

        original_db = main.CHAT_DB_FILE
        temp_dir = tempfile.TemporaryDirectory()
        try:
            main.CHAT_DB_FILE = str(_Path(temp_dir.name) / "chat_history.db")
            main.init_chat_db()
            newest_first = [
                SimpleNamespace(
                    id=f"d{index:02d}",
                    content=f"discord-{index:02d} line",
                    author=SimpleNamespace(id=10, display_name="Kōshin", bot=False),
                )
                for index in range(12, 0, -1)
            ]

            class HistoryChannel:
                id = 9
                name = "general"

                def history(self, limit=50, before=None):
                    async def _gen():
                        for msg in newest_first[:limit]:
                            yield msg
                    return _gen()

            captured = {}

            async def complete(
                session, headers, system_prompt, chat_messages,
                text_fallback, has_images,
            ):
                captured["messages"] = [dict(item) for item in chat_messages]
                return "the river bit still lands, and i'm still on it with you."

            message = SimpleNamespace(
                id="cur",
                content="still here with you",
                author=SimpleNamespace(id=10, display_name="Kōshin", bot=False),
                channel=HistoryChannel(),
                mentions=[],
                reference=None,
                attachments=[],
                embeds=[],
                guild=SimpleNamespace(
                    id=50, name="Seclude", member_count=3, members=[OP, OTHER, GRIM],
                ),
            )

            async def run():
                with patch.dict(main.os.environ, {"XAI_API_KEY": "test-key"}), \
                        patch.object(main, "_complete_contextual_reply", complete):
                    return await main.generate_contextual_reply(message)

            reply = asyncio.run(run())
            self.assertTrue(reply.text)
            messages = captured["messages"]
            blob = " ".join(item["content"] for item in messages)
            self.assertEqual(len(messages) - 1, 10)
            self.assertNotIn("discord-01", blob)
            self.assertNotIn("discord-02", blob)
            self.assertIn("discord-03", blob)
            self.assertIn("discord-12", blob)
            self.assertEqual(blob.count("still here with you"), 1)
        finally:
            main.CHAT_DB_FILE = original_db
            temp_dir.cleanup()

    def test_changelog_feeds_conversation_patch_notes(self):
        changelog = Path(__file__).resolve().parents[1].joinpath("CHANGELOG.md").read_text()
        self.assertIn("same channel", changelog)
        self.assertIn("without a Discord reply-arrow", changelog)
        self.assertIn("Discord reply-ladder messages still continue", changelog)
        self.assertIn("5 minutes of silence", changelog)
        self.assertIn("without naming him again", changelog)
        self.assertIn("weather or fact questions still live-search", changelog)
        self.assertIn("mention or address someone else", changelog)
        self.assertIn('lone "Grim" ping still counts as "you there"', changelog)
        added = (
            "After a name-call (`Grim` with or without @), Grim stays with that "
            "original poster in the same channel. Follow-ups get a reply even "
            "without naming him again and without a Discord reply-arrow; Discord "
            "reply-ladder messages still continue, and weather or fact questions "
            "still live-search. The conversation ends after 5 minutes of silence "
            "from the original poster, or as soon as they mention or address "
            "someone else. Other people's side talk does not keep him in."
        )
        fixed = (
            "After a name-call, in-channel follow-ups from the original poster "
            "keep the conversation even when they are not Discord replies. A "
            'lone "Grim" ping still counts as "you there" while the conversation '
            "is open."
        )
        patch = f"""@@ -7,6 +7,10 @@
 ### Added
+- {added}
 ### Fixed
+- {fixed}
 context
"""
        notes = main._extract_added_changelog_notes(patch)
        public = main._deploy_card_patch_notes(notes)
        self.assertEqual(notes, public)
        self.assertEqual(len(public), 2)
        self.assertIn("same channel", public[0])
        self.assertIn("not Discord replies", public[1])
        self.assertNotIn("[REDACTED]", main._format_patch_notes(public))


if __name__ == "__main__":
    unittest.main()
