import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

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

    def tearDown(self):
        self.user_patch.stop()
        main._open_conversations.clear()
        main._conversation_ladders.clear()
        main._conversation_media_kind.clear()

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

    def test_lone_grim_ping_continues_only_while_the_conversation_is_open(self):
        ping = chat_message("Grim", mid=FOLLOW_ID, reference=None)
        self.assertFalse(main.grim_should_reply_to_message(ping))
        self.assertFalse(main.grim_chat_turn_action(ping, now=T0))
        self.start_opener()
        self.assertTrue(main.grim_chat_turn_action(ping, now=T0 + 4))

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
