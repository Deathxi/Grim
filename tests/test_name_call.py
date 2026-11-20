import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, PropertyMock, patch

import main


GRIM = SimpleNamespace(id=999)


def chat_message(content, *, mentions=None):
    return SimpleNamespace(
        content=content,
        mentions=list(mentions or []),
    )


def _reply_message(content, *, mentions=None, mid=1):
    class Channel:
        id = 9
        name = "STAFF"

        def history(self, limit=50, before=None):
            async def _gen():
                if False:
                    yield None
            return _gen()

    return SimpleNamespace(
        id=mid,
        content=content,
        mentions=list(mentions or []),
        author=SimpleNamespace(id=10, display_name="Kōshin", bot=False),
        channel=Channel(),
        attachments=[],
        embeds=[],
        reference=None,
        guild=SimpleNamespace(
            id=50, name="Seclude", member_count=3, members=[],
        ),
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
        main._bare_ping_greet_at = 0

    def tearDown(self):
        self.user_patch.stop()

    def test_incident_name_calls_need_a_reply(self):
        name_calls = (
            "Grim guess who it is",
            "hello Grim wake up",
            "Grim, arise.",
            "Grim, answer the question above.",
        )
        for text in name_calls:
            with self.subTest(text=text):
                self.assertTrue(main.grim_turn_needs_reply(text), text)
                self.assertTrue(
                    main.grim_should_reply_to_message(chat_message(text)),
                    text,
                )
                self.assertFalse(main._is_weather_ask(text), text)
                self.assertFalse(main._needs_live_sources(text, ""), text)
                self.assertFalse(
                    main._needs_live_sources(
                        text,
                        "Kōshin: what's the weather tomorrow in the Bay Area?",
                    ),
                    text,
                )

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

    def test_empty_ping_is_a_bare_greet(self):
        empty = (
            "Grim",
            "grim",
            "grim!",
            "GRIM.",
            "@grim",
            "<@999>",
            "<@!999>",
            "  <@999>  ",
            "what's up Grim",
            "whats up grim",
            "hey grim",
            "yo grim",
        )
        for text in empty:
            with self.subTest(text=text):
                mentioned = "<@" in text
                self.assertEqual(
                    main.grim_name_call_kind(text, mentioned=mentioned),
                    main.NAME_CALL_BARE,
                    text,
                )
                self.assertTrue(main.grim_turn_needs_reply(text, mentioned=mentioned), text)
                self.assertTrue(main.grim_name_call_is_bare(text, mentioned=mentioned), text)
                mentions = [GRIM] if mentioned else []
                self.assertTrue(
                    main.grim_should_reply_to_message(
                        chat_message(text, mentions=mentions)
                    ),
                    text,
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
            self.assertTrue(main.grim_should_reply_to_message(empty))
            self.assertEqual(
                main.grim_name_call_kind("<@999>", mentioned=True),
                main.NAME_CALL_BARE,
            )
            self.assertFalse(main.grim_should_reply_to_message(passing))
            self.assertTrue(main.grim_should_reply_to_message(named))

    def test_calling_ping_answers_deftones_and_baseball_bat(self):
        calling = (
            "they're the Bay Area's Deftones @Grim fax or na",
            "they're the Bay Area's Deftones <@999> fax or na",
            "Hey I got a new baseball bat and I want to paint it, Grim do you have any thoughts?",
            "grim do you have any thoughts?",
        )
        for text in calling:
            with self.subTest(text=text):
                mentioned = "<@" in text or "@Grim" in text or "@grim" in text
                self.assertEqual(
                    main.grim_name_call_kind(text, mentioned="<@" in text),
                    main.NAME_CALL_CALLING,
                    text,
                )
                self.assertTrue(
                    main.grim_turn_needs_reply(text, mentioned="<@" in text),
                    text,
                )
                self.assertFalse(
                    main.grim_name_call_is_bare(text, mentioned="<@" in text),
                    text,
                )
                self.assertFalse(main._needs_live_sources(text, ""), text)
                mentions = [GRIM] if "<@" in text else []
                self.assertTrue(
                    main.grim_should_reply_to_message(
                        chat_message(text, mentions=mentions)
                    ),
                    text,
                )

    def test_drive_by_noun_stays_quiet_unless_he_can_help(self):
        silent = (
            "... yeah I was looking at the files and saw Grim did something there. I'll fix it later bro",
            "I was talking to grim yesterday about the deploy",
            "grim is down again",
            "did you see what grim said in #ops",
            "credit grim for the cleanup",
        )
        for text in silent:
            with self.subTest(text=text):
                self.assertIsNone(main.grim_name_call_kind(text), text)
                self.assertFalse(main.grim_turn_needs_reply(text), text)
        helpful = (
            "can grim look at this deploy?",
            "why did grim delete those files?",
            "need grim to check the logs",
        )
        for text in helpful:
            with self.subTest(text=text):
                self.assertEqual(
                    main.grim_name_call_kind(text),
                    main.NAME_CALL_CALLING,
                    text,
                )
                self.assertTrue(main.grim_turn_needs_reply(text), text)
                self.assertFalse(main.grim_name_call_is_bare(text), text)

    def test_bare_ping_greets_rotate_and_skip_the_model(self):
        main._bare_ping_greet_at = 0
        greets = [main.grim_bare_ping_greet() for _ in range(len(main.BARE_PING_GREETS) + 1)]
        self.assertEqual(greets[0], "What's up?")
        self.assertEqual(greets[1], "How's it going?")
        self.assertEqual(greets[2], "You called?")
        self.assertEqual(greets[3], "What's up?")
        self.assertEqual(
            set(main.BARE_PING_GREETS),
            {"What's up?", "How's it going?", "You called?"},
        )

        captured = {}

        async def complete(*args, **kwargs):
            captured["called"] = True
            return "model should not run on a bare ping"

        message = _reply_message("<@999>", mentions=[GRIM])
        main._bare_ping_greet_at = 0
        reply = asyncio.run(self._run_reply(message, complete))
        self.assertEqual(reply.text, "What's up?")
        self.assertNotIn("called", captured)

    def test_calling_ping_uses_the_model_not_a_greet(self):
        captured = {}

        async def complete(
            session, headers, system_prompt, chat_messages, text_fallback, has_images
        ):
            captured["messages"] = [dict(item) for item in chat_messages]
            return "nah, that's a stretch, but the bay fog fits."

        text = "they're the Bay Area's Deftones <@999> fax or na"
        message = _reply_message(text, mentions=[GRIM], mid=7)
        main._bare_ping_greet_at = 0
        reply = asyncio.run(self._run_reply(message, complete))
        self.assertTrue(reply.text)
        self.assertNotIn(reply.text, main.BARE_PING_GREETS)
        self.assertIn("fax or na", captured["messages"][-1]["content"])
        self.assertFalse(main._needs_live_sources(text, ""))

    async def _run_reply(self, message, complete, research=None):
        if research is None:
            research = AsyncMock()
        with patch.dict(main.os.environ, {"XAI_API_KEY": "test-key"}), \
                patch.object(main, "_complete_contextual_reply", complete), \
                patch.object(main, "get_operator_channel_history_from_db", return_value=[]), \
                patch.object(main, "correlated_foreign_channel_context", return_value=[]), \
                patch.object(main, "grok_research_query", research):
            return await main.generate_contextual_reply(message)


if __name__ == "__main__":
    unittest.main()
