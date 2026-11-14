import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

import main


GRIM = SimpleNamespace(id=999)


def chat_message(content, *, mentions=None):
    return SimpleNamespace(
        content=content,
        mentions=list(mentions or []),
    )


class NameCallReplyTests(unittest.TestCase):
    def setUp(self):
        self.user_patch = patch.object(
            main.discord.Client,
            "user",
            new_callable=PropertyMock,
            return_value=GRIM,
        )
        self.user_patch.start()

    def tearDown(self):
        self.user_patch.stop()

    def test_direct_question_to_grim_without_mention_replies(self):
        questions = (
            "I think the patch is solid. what about you grim?",
            "grim what do you think of this take?",
            "yo grim, you seeing this?",
            "that's my read — thoughts grimmy?",
            "grim is this true?",
        )
        for text in questions:
            with self.subTest(text=text):
                self.assertTrue(main.grim_turn_needs_reply(text))
                self.assertTrue(
                    main.grim_should_reply_to_message(chat_message(text))
                )

    def test_mention_with_an_entrance_replies(self):
        mentioned = (
            "<@999> thoughts on this?",
            "<@999> what do you think?",
            "hey <@999> you good?",
            "<@!999> grim is this working?",
        )
        for text in mentioned:
            with self.subTest(text=text):
                self.assertTrue(main.grim_turn_needs_reply(text, mentioned=True))
                self.assertTrue(
                    main.grim_should_reply_to_message(
                        chat_message(text, mentions=[GRIM])
                    )
                )

    def test_passing_third_person_name_drop_with_no_ask_skips(self):
        drive_bys = (
            "I was talking to grim yesterday about the deploy",
            "grim is down again",
            "did you see what grim said in #ops",
            "credit grim for the cleanup",
            "unlike grim, this bot never replies",
            "I was talking to <@999> yesterday about the deploy",
        )
        for text in drive_bys:
            with self.subTest(text=text):
                mentioned = "<@" in text
                self.assertFalse(
                    main.grim_turn_needs_reply(text, mentioned=mentioned)
                )

    def test_empty_ping_skips(self):
        empty = (
            "Grim",
            "grim",
            "grim!",
            "GRIM.",
            "@grim",
            "<@999>",
            "<@!999>",
            "  <@999>  ",
        )
        for text in empty:
            with self.subTest(text=text):
                mentioned = "<@" in text
                self.assertFalse(
                    main.grim_turn_needs_reply(text, mentioned=mentioned)
                )
                mentions = [GRIM] if mentioned else []
                self.assertFalse(
                    main.grim_should_reply_to_message(
                        chat_message(text, mentions=mentions)
                    )
                )

    def test_nicknames_and_direct_address_reply_without_hardcoded_line(self):
        name_calls = (
            "grim bro you in?",
            "what about you grim brooooo",
            "grim broobroooo, your take?",
            "yo grimbo — agree or nah?",
            "hey grim that's wild",
            "ask grim before we ship it",
            "does grim think this holds?",
        )
        for text in name_calls:
            with self.subTest(text=text):
                self.assertTrue(main.grim_turn_needs_reply(text))
        self.assertNotIn(
            "... what about you Grim broobroooo?",
            Path(main.__file__).read_text(),
        )

    def test_prefix_commands_and_unrelated_chat_skip(self):
        self.assertFalse(
            main.grim_should_reply_to_message(chat_message("!ping grim you there?"))
        )
        self.assertFalse(main.grim_turn_needs_reply("anyone seen the logs"))
        self.assertFalse(main.grim_turn_needs_reply(""))

    def test_quoted_name_drop_is_not_an_entrance(self):
        quoted = "> grim what do you think\nyeah that tracks"
        self.assertFalse(main.grim_turn_needs_reply(quoted))

    def test_on_message_uses_the_name_call_gate_not_mentions_alone(self):
        source = Path(main.__file__).read_text()
        self.assertIn("grim_should_reply_to_message(message)", source)
        self.assertIn("def grim_turn_needs_reply(", source)
        mention_gate = "if bot.user in message.mentions and not is_prefix_command:"
        self.assertNotIn(mention_gate, source)

    def test_should_reply_reads_discord_mentions_from_the_message(self):
        with patch.object(
            main.discord.Client,
            "user",
            new_callable=PropertyMock,
            return_value=GRIM,
        ):
            ping = chat_message("<@999> you good?", mentions=[GRIM])
            empty = chat_message("<@999>", mentions=[GRIM])
            passing = chat_message(
                "I was talking to <@999> yesterday", mentions=[GRIM]
            )
            named = chat_message("grim what do you think?")
            self.assertTrue(main.grim_should_reply_to_message(ping))
            self.assertFalse(main.grim_should_reply_to_message(empty))
            self.assertFalse(main.grim_should_reply_to_message(passing))
            self.assertTrue(main.grim_should_reply_to_message(named))


if __name__ == "__main__":
    unittest.main()
