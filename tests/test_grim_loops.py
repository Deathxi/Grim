import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, PropertyMock, patch

import grim_loops
import main


GRIM = SimpleNamespace(id=999, display_name="Grim", name="Grim")
OP = SimpleNamespace(id=10, display_name="Kōshin", name="koshin", bot=False)


def chat_message(content, *, channel_id=9, author=OP, mid="cur"):
    channel = SimpleNamespace(id=channel_id, name="general")

    async def empty_history(limit=50, before=None):
        if False:
            yield None

    channel.history = empty_history
    return SimpleNamespace(
        id=mid,
        content=content,
        author=author,
        channel=channel,
        mentions=[],
        reference=None,
        attachments=[],
        embeds=[],
        guild=SimpleNamespace(id=50, name="Seclude", member_count=3, members=[OP, GRIM]),
    )


class GrimLoopUnitTests(unittest.TestCase):
    def setUp(self):
        grim_loops.clear_loop_state()

    def tearDown(self):
        grim_loops.clear_loop_state()

    def test_repair_skips_casual_and_catches_stakes_and_ambiguity(self):
        self.assertFalse(grim_loops.needs_repair_confirm("yo grim"))
        self.assertFalse(grim_loops.needs_repair_confirm("grim what's up"))
        self.assertFalse(grim_loops.needs_repair_confirm("right or wrong on this meme"))
        self.assertTrue(grim_loops.needs_repair_confirm("grim send $50 to my friend"))
        self.assertTrue(grim_loops.needs_repair_confirm("venmo them 20 after you confirm"))
        self.assertTrue(grim_loops.needs_repair_confirm("grim do that"))
        self.assertTrue(grim_loops.needs_repair_confirm("dm them the plan"))

    def test_nudges_off_instruction_has_no_idle_poll(self):
        notes = grim_loops.reply_loop_instructions(repair=False, nudges_on=False)
        self.assertIn("NUDGES ARE OFF", notes)
        self.assertIn("LIGHT AGENTIC", notes)
        self.assertNotIn("NUDGES ARE ON", notes)
        self.assertNotIn("STICKY CONTEXT", notes)

    def test_repair_and_nudge_share_one_question_budget(self):
        notes = grim_loops.reply_loop_instructions(repair=True, nudges_on=True)
        self.assertIn("read-back", notes)
        self.assertIn("At most one question", notes)
        self.assertNotIn("NUDGES ARE ON", notes)

    def test_sticky_omits_empty_and_clips_correlated_bits(self):
        self.assertIsNone(grim_loops.format_sticky_block())
        block = grim_loops.format_sticky_block(
            topics=["kardashev type i watts"],
            prefs=["language:spanish"],
            vibe="late-night science chat",
        )
        self.assertIn(grim_loops.STICKY_HEADER, block)
        self.assertIn("kardashev", block)
        self.assertIn("spanish", block)
        self.assertIn("late-night science chat", block)

    def test_thumbs_up_is_not_a_miss_prompt(self):
        self.assertEqual(grim_loops.is_feedback_emoji("👍"), "up")
        self.assertEqual(grim_loops.is_feedback_emoji("👎"), "down")
        self.assertIsNone(grim_loops.is_feedback_emoji("🔥"))
        self.assertFalse(grim_loops.already_prompted_miss("1"))
        grim_loops.set_pending_miss("9", "10", "100")
        self.assertTrue(grim_loops.already_prompted_miss("100"))
        self.assertEqual(grim_loops.take_pending_miss("9", "10"), "100")
        self.assertIsNone(grim_loops.take_pending_miss("9", "10"))

    def test_agentic_yes_is_local_and_text_only(self):
        self.assertTrue(grim_loops.is_agentic_yes("yes, outline it"))
        self.assertTrue(grim_loops.is_agentic_yes("go ahead"))
        self.assertFalse(grim_loops.is_agentic_yes("maybe later"))
        source = Path(grim_loops.__file__).read_text()
        self.assertIn("no extra tools", source)
        self.assertNotIn("web_search", source)


class GrimLoopIntegrationTests(unittest.TestCase):
    def setUp(self):
        grim_loops.clear_loop_state()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db = main.CHAT_DB_FILE
        main.CHAT_DB_FILE = str(Path(self.temp_dir.name) / "chat_history.db")
        main.init_chat_db()
        main._open_conversations.clear()
        main._conversation_ladders.clear()
        self.user_patch = patch.object(
            main.discord.Client,
            "user",
            new_callable=PropertyMock,
            return_value=GRIM,
        )
        self.user_patch.start()

    def tearDown(self):
        self.user_patch.stop()
        main.CHAT_DB_FILE = self.original_db
        grim_loops.clear_loop_state()
        self.temp_dir.cleanup()

    def test_nudges_default_off_and_user_override_wins(self):
        self.assertFalse(main.nudges_enabled("50", "10"))
        self.assertFalse(main.guild_nudges_enabled("50"))
        self.assertTrue(main.toggle_user_nudges("50", "10"))
        self.assertTrue(main.nudges_enabled("50", "10"))
        self.assertTrue(main.toggle_guild_nudges("50"))
        self.assertFalse(main.toggle_user_nudges("50", "10"))
        self.assertFalse(main.nudges_enabled("50", "10"))
        self.assertTrue(main.guild_nudges_enabled("50"))
        self.assertTrue(main.nudges_enabled("50", "11"))

    def test_sticky_skips_uncorrelated_and_survives_a_new_thread(self):
        main.save_message_to_db(
            "50", "9", "c1", "Kōshin",
            "kardashev type i planetary insolation breakdown",
            1.0, False, author_id="10",
        )
        main.save_message_to_db(
            "50", "9", "c2", "Kōshin",
            "also I like pineapple on pizza",
            2.0, False, author_id="10",
        )
        empty = main.collect_sticky_context(
            "50", "99",
            author_id="10",
            author_name="Kōshin",
            ask_text="yo grim how we doing",
        )
        self.assertIsNone(empty)
        sticky = main.collect_sticky_context(
            "50", "99",
            author_id="10",
            author_name="Kōshin",
            ask_text="grim remind me what the kardashev breakdown was",
        )
        self.assertIsNotNone(sticky)
        self.assertIn("kardashev", sticky.casefold())
        self.assertNotIn("pineapple", sticky.casefold())
        self.assertIn(grim_loops.STICKY_HEADER, sticky)

    def test_sticky_does_not_resend_the_ten_turn_window(self):
        for index in range(1, 16):
            main.save_message_to_db(
                "50", "9", f"p{index:02d}", "Kōshin",
                f"kardashev prior-{index:02d} still talking",
                float(index), False, author_id="10",
            )
        sticky = main.collect_sticky_context(
            "50", "9",
            author_id="10",
            author_name="Kōshin",
            ask_text="grim remind me the kardashev prior notes",
            history_window=10,
        )
        self.assertIsNotNone(sticky)
        self.assertIn("prior-05", sticky)
        self.assertNotIn("prior-15", sticky)
        self.assertNotIn("prior-06", sticky)
        self.assertLessEqual(sticky.count("kardashev prior-"), grim_loops.MAX_STICKY_TOPICS)
        self.assertLess(len(sticky), 800)

    def test_memory_note_is_stored_without_a_model(self):
        main.append_member_memory_note("50", "10", "missed: skipped the savings plan")
        notes = main.get_member_memory_notes("50", "10")
        self.assertEqual(notes[0], "missed: skipped the savings plan")
        sticky = main.collect_sticky_context(
            "50", "9",
            author_id="10",
            author_name="Kōshin",
            ask_text="grim what about that savings plan",
        )
        self.assertIn("savings plan", sticky)

    def test_uncorrelated_reply_does_not_inject_sticky_or_search(self):
        main.save_message_to_db(
            "50", "9", "c1", "Kōshin",
            "kardashev type i planetary insolation breakdown",
            1.0, False, author_id="10",
        )
        captured = {}

        async def complete(session, headers, system_prompt, chat_messages, text_fallback, has_images):
            captured["system"] = system_prompt
            captured["calls"] = captured.get("calls", 0) + 1
            return "still here. breathing."

        async def run():
            with patch.dict(main.os.environ, {"XAI_API_KEY": "test-key"}), \
                    patch.object(main, "_complete_contextual_reply", complete), \
                    patch.object(main, "_needs_live_sources", return_value=False), \
                    patch.object(main, "grok_research_query", AsyncMock()) as research:
                reply = await main.generate_contextual_reply(
                    chat_message("still here with you")
                )
                captured["research"] = research.await_count
                return reply

        reply = asyncio.run(run())
        self.assertTrue(reply.text)
        self.assertEqual(captured["calls"], 1)
        self.assertEqual(captured["research"], 0)
        self.assertNotIn("STICKY CONTEXT", captured["system"])
        self.assertIn("NUDGES ARE OFF", captured["system"])
        self.assertNotIn("This ask is ambiguous or high-stakes", captured["system"])

    def test_high_stakes_ask_adds_repair_in_the_same_completion(self):
        captured = {}

        async def complete(session, headers, system_prompt, chat_messages, text_fallback, has_images):
            captured["system"] = system_prompt
            captured["calls"] = captured.get("calls", 0) + 1
            return "you want me to send $50 to your friend. say yes if that's the ask."

        async def run():
            with patch.dict(main.os.environ, {"XAI_API_KEY": "test-key"}), \
                    patch.object(main, "_complete_contextual_reply", complete), \
                    patch.object(main, "_needs_live_sources", return_value=False), \
                    patch.object(main, "grok_research_query", AsyncMock()) as research:
                reply = await main.generate_contextual_reply(
                    chat_message("grim send $50 to my friend")
                )
                captured["research"] = research.await_count
                return reply

        reply = asyncio.run(run())
        self.assertIn("say yes", reply.text)
        self.assertEqual(captured["calls"], 1)
        self.assertEqual(captured["research"], 0)
        self.assertIn("high-stakes", captured["system"])
        self.assertIn("Do not make a second call", captured["system"])

    def test_nudges_on_adds_same_completion_instruction(self):
        main.toggle_user_nudges("50", "10")
        captured = {}

        async def complete(session, headers, system_prompt, chat_messages, text_fallback, has_images):
            captured["system"] = system_prompt
            captured["calls"] = captured.get("calls", 0) + 1
            return "black friday is a trap. want me to outline a savings plan?"

        async def run():
            with patch.dict(main.os.environ, {"XAI_API_KEY": "test-key"}), \
                    patch.object(main, "_complete_contextual_reply", complete), \
                    patch.object(main, "_needs_live_sources", return_value=False):
                return await main.generate_contextual_reply(
                    chat_message("grim how do I not blow money on black friday")
                )

        reply = asyncio.run(run())
        self.assertIn("savings plan", reply.text)
        self.assertEqual(captured["calls"], 1)
        self.assertIn("NUDGES ARE ON", captured["system"])
        self.assertIn("LIGHT AGENTIC", captured["system"])

    def test_thumbs_down_prompts_without_a_model_call(self):
        main.save_message_to_db(
            "50", "9", "100", "Grim", "some reply", 1.0, True, author_id="999"
        )
        sent = SimpleNamespace(id=200, created_at=SimpleNamespace(timestamp=lambda: 2.0))
        channel = SimpleNamespace(id=9, send=AsyncMock(return_value=sent))
        payload = SimpleNamespace(
            user_id=10,
            message_id=100,
            guild_id=50,
            channel_id=9,
            emoji=SimpleNamespace(name="👎"),
        )

        async def run():
            with patch.object(main.bot, "get_channel", return_value=channel), \
                    patch.object(main, "grok_research_query", AsyncMock()) as research, \
                    patch.object(main, "_complete_contextual_reply", AsyncMock()) as complete:
                await main.handle_grim_reply_feedback(payload)
                return research.await_count, complete.await_count

        research_calls, complete_calls = asyncio.run(run())
        self.assertEqual(research_calls, 0)
        self.assertEqual(complete_calls, 0)
        channel.send.assert_awaited_once_with(grim_loops.MISS_PROMPT)
        notes = main.get_member_memory_notes("50", "10")
        self.assertTrue(any("thumbs-down" in note for note in notes))
        self.assertIn(("9", "10"), main._open_conversations)
        self.assertEqual(grim_loops.take_pending_miss("9", "10"), "100")
        grim_loops.set_pending_miss("9", "10", "100")
        main.append_member_memory_note(
            "50",
            "10",
            grim_loops.miss_note_from_explanation("you skipped the savings plan"),
        )
        self.assertIn(
            "missed: you skipped the savings plan",
            main.get_member_memory_notes("50", "10"),
        )

    def test_thumbs_up_does_not_call_the_model_or_store_a_miss(self):
        main.save_message_to_db(
            "50", "9", "100", "Grim", "some reply", 1.0, True, author_id="999"
        )
        channel = SimpleNamespace(id=9, send=AsyncMock())
        payload = SimpleNamespace(
            user_id=10,
            message_id=100,
            guild_id=50,
            channel_id=9,
            emoji=SimpleNamespace(name="👍"),
        )

        async def run():
            with patch.object(main.bot, "get_channel", return_value=channel), \
                    patch.object(main, "_complete_contextual_reply", AsyncMock()) as complete:
                await main.handle_grim_reply_feedback(payload)
                return complete.await_count

        self.assertEqual(asyncio.run(run()), 0)
        channel.send.assert_not_called()
        self.assertEqual(main.get_member_memory_notes("50", "10"), [])

    def test_constraints_and_no_idle_background_loop(self):
        source = Path(main.__file__).read_text() + Path(grim_loops.__file__).read_text()
        self.assertEqual(main.MODEL_REPLY_HISTORY_MESSAGES, 10)
        self.assertIn("def _needs_live_sources", source)
        self.assertIn("allowed_mentions=discord.AllowedMentions(everyone=False", source)
        self.assertIn("async def vc_join", source)
        self.assertIn("async def seclude", source)
        self.assertNotIn("@tasks.loop", Path(grim_loops.__file__).read_text())
        self.assertNotIn("newsfeed", Path(grim_loops.__file__).read_text())
        self.assertIn("on_raw_reaction_add", Path(main.__file__).read_text())
        self.assertIn("_add_feedback_reactions", Path(main.__file__).read_text())


if __name__ == "__main__":
    unittest.main()
