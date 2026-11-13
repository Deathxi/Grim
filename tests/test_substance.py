import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, PropertyMock, patch

import main
import substance


MOOD_CASES = {
    "direct answer": {
        "user": "how do i stop my docker container from restarting?",
        "good": "set restart: no on the service, or run docker update --restart=no on it. check compose too, it overrides the container.",
        "thin": "yeah that's a good question.",
    },
    "teaching": {
        "user": "explain how a hash differs from encryption",
        "good": "a hash only goes one way, so you can't get the input back. encryption is two way with a key. use a hash for passwords and encryption for data you must read again.",
        "thin": "hmm, interesting topic.",
    },
    "empathy and presence": {
        "user": "my dad died last week",
        "good": "i'm sorry. a loss like that rearranges everything. eat something today, and tell me if you want to talk about him.",
        "thin": "damn.",
    },
    "tough love": {
        "user": "i relapsed again and i keep saying i'll stop tomorrow",
        "good": "tomorrow is the lie that keeps it going. pick one thing for tonight, call someone before you use, and tell me who.",
        "thin": "that's valid.",
    },
    "humor and banter": {
        "user": "lol the build failed again because i forgot a semicolon",
        "good": "a semicolon took down your pipeline. dignified way to die. run the linter before pushing so the next one dies faster.",
        "thin": "lmao same.",
    },
    "transparency": {
        "user": "what's the btc price right now?",
        "good": "can't check a live price from here right now, so i won't guess. any exchange page has it, and i can help you read it.",
        "thin": "hmm.",
    },
    "playing along": {
        "user": "never gonna give you up never gonna let you down",
        "good": "never gonna run around and desert you.",
        "thin": "nice.",
    },
    "working it through": {
        "user": "i've been thinking about whether to quit my job, not sure what i actually want",
        "good": "sounds like the job isn't the real question. you keep circling money, so write down the monthly number you'd need and see if the fear shrinks.",
        "thin": "sounds like you've been thinking about whether to quit your job.",
    },
}


class RetryRecorder:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.notes = []

    async def __call__(self, note):
        self.notes.append(note)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class SubstanceCheckTests(unittest.TestCase):
    def test_every_mood_has_a_rule_in_the_prompt(self):
        moods = {rule.mood for rule in substance.MOOD_RULES}
        self.assertEqual(moods, set(MOOD_CASES))
        for rule in substance.MOOD_RULES:
            self.assertIn(f"- {rule.mood}: {rule.rule}.", substance.SUBSTANCE_PROMPT)
        self.assertIn("Mood sets your tone and never removes the content", substance.SUBSTANCE_PROMPT)
        self.assertIn("Do not get longer for this", substance.SUBSTANCE_PROMPT)
        self.assertIn("never the default when you have nothing to say", substance.SUBSTANCE_PROMPT)
        self.assertIn("Do not invent facts", substance.SUBSTANCE_PROMPT)
        self.assertNotIn("\u2014", substance.SUBSTANCE_PROMPT)

    def test_each_mood_passes_with_substance(self):
        for mood, case in MOOD_CASES.items():
            with self.subTest(mood=mood):
                self.assertIsNone(substance.substance_problem(case["good"], case["user"]))

    def test_each_mood_flags_a_thin_reply(self):
        for mood, case in MOOD_CASES.items():
            with self.subTest(mood=mood):
                self.assertIsNotNone(substance.substance_problem(case["thin"], case["user"]))

    def test_each_mood_retries_once_and_keeps_the_better_draft(self):
        async def run():
            for mood, case in MOOD_CASES.items():
                recorder = RetryRecorder(case["good"])
                result = await substance.ensure_substance(case["thin"], case["user"], recorder)
                self.assertEqual(result.outcome, "retried", mood)
                self.assertEqual(result.text, case["good"], mood)
                self.assertEqual(len(recorder.notes), 1, mood)
                self.assertIn("SUBSTANCE RETRY", recorder.notes[0])

        asyncio.run(run())

    def test_each_mood_falls_back_to_an_honest_short_reply_after_two_failures(self):
        async def run():
            for mood, case in MOOD_CASES.items():
                recorder = RetryRecorder(case["thin"])
                result = await substance.ensure_substance(case["thin"], case["user"], recorder)
                self.assertEqual(result.outcome, "fallback", mood)
                self.assertEqual(len(recorder.notes), 1, mood)
                self.assertIsNone(substance.substance_problem(result.text, case["user"]), mood)
                self.assertLess(len(result.text.split()), 25, mood)
                self.assertNotIn("\u2014", result.text)
                self.assertEqual(result.text, result.text.lower())

        asyncio.run(run())

    def test_substantive_reply_is_untouched_and_never_retried(self):
        async def run():
            for mood, case in MOOD_CASES.items():
                recorder = RetryRecorder()
                result = await substance.ensure_substance(case["good"], case["user"], recorder)
                self.assertEqual(result, substance.SubstanceResult(case["good"], "passed"), mood)
                self.assertEqual(recorder.notes, [], mood)

        asyncio.run(run())

    def test_failed_or_empty_retry_ends_in_the_honest_line(self):
        async def run():
            user = "how do i stop my docker container from restarting?"
            for second in (None, "", "   ", RuntimeError("down"), asyncio.TimeoutError()):
                recorder = RetryRecorder(second)
                result = await substance.ensure_substance("", user, recorder)
                self.assertEqual(result.outcome, "fallback")
                self.assertEqual(result.text, substance.honest_short_reply(user))
                self.assertEqual(len(recorder.notes), 1)

        asyncio.run(run())

    def test_casual_small_talk_is_untouched(self):
        async def run():
            pairs = (
                ("gm", "gm."),
                ("lol", "lol same"),
                ("ok", "yeah."),
                ("thanks grim", "anytime."),
                ("what's up", "not much. server's quiet."),
                ("damn that was close", "too close. glad it held."),
                ("u good?", "yeah"),
            )
            for user, reply in pairs:
                self.assertIsNone(substance.substance_problem(reply, user), user)
                recorder = RetryRecorder()
                result = await substance.ensure_substance(reply, user, recorder)
                self.assertEqual(result.text, reply)
                self.assertEqual(result.outcome, "passed")
                self.assertEqual(recorder.notes, [])

        asyncio.run(run())

    def test_small_talk_still_rejects_empty_and_stock_lines(self):
        for reply in ("", "   ", "interesting.", "noted.", "that makes sense.", "i hear you."):
            self.assertIsNotNone(substance.substance_problem(reply, "gm"), reply)

    def test_classification(self):
        self.assertEqual(substance.classify_message(""), "small_talk")
        self.assertEqual(substance.classify_message("gm"), "small_talk")
        self.assertEqual(substance.classify_message("whats up"), "small_talk")
        self.assertEqual(substance.classify_message("is it safe to run this as root"), "question")
        self.assertEqual(substance.classify_message("write me a haiku about servers"), "question")
        self.assertEqual(substance.classify_message("my dad died last week"), "heavy")
        self.assertEqual(
            substance.classify_message("the new build is slower than the last one and i can't tell why"),
            "substantive",
        )


class RestatementTests(unittest.TestCase):
    def test_polished_echo_is_not_a_reply(self):
        cases = (
            (
                "i think the new build is worse than the last one honestly",
                "you think the new build is worse than the last one.",
            ),
            (
                "the update broke my build and i'm frustrated about it",
                "so you're saying the update broke your build and you're frustrated about it.",
            ),
            (
                "how do i stop my docker container from restarting?",
                "stop my docker container from restarting.",
            ),
            (
                "my manager keeps changing the plan and i'm exhausted by it",
                "what you're saying is your manager keeps changing the plan and you're exhausted.",
            ),
        )
        for user, reply in cases:
            with self.subTest(reply=reply):
                self.assertIn(
                    substance.substance_problem(reply, user),
                    {"repeats the message", "restates instead of adding"},
                )

    def test_restating_is_allowed_only_as_introspection_with_new_content(self):
        thinking = "i've been thinking about whether to quit my job, not sure what i actually want"
        mirrored = (
            "sounds like the job isn't the real question. you keep circling money, "
            "so write down the monthly number you'd need and see if the fear shrinks."
        )
        self.assertIsNone(substance.substance_problem(mirrored, thinking))
        pure_echo = "sounds like you've been thinking about whether to quit your job."
        self.assertIsNotNone(substance.substance_problem(pure_echo, thinking))

        not_thinking = "my manager keeps changing the plan every single week now"
        framed_with_little = "sounds like your manager keeps changing the plan, and that's rough."
        self.assertEqual(
            substance.substance_problem(framed_with_little, not_thinking),
            "restates instead of adding",
        )
        framed_with_substance = (
            "sounds like the plan has no owner. ask him for one fixed review day each "
            "week and put every change in writing, so the churn has a cost."
        )
        self.assertIsNone(substance.substance_problem(framed_with_substance, not_thinking))

    def test_dodging_a_direct_question_fails(self):
        user = "should i use postgres or sqlite for a small discord bot?"
        for reply in ("what do you think?", "depends.", "hard to say, who knows.", "good question."):
            self.assertIsNotNone(substance.substance_problem(reply, user), reply)
        answer = "sqlite. one file, no server to run, and a small bot will never outgrow it."
        self.assertIsNone(substance.substance_problem(answer, user))

    def test_a_question_back_is_fine_while_someone_works_something_through(self):
        thinking = "i keep wondering whether i should leave and i can't decide what matters most"
        reply = "what would you regret more in a year, staying or leaving?"
        self.assertIsNone(substance.substance_problem(reply, thinking))

    def test_retry_note_keeps_the_live_source_rule_and_no_user_text(self):
        user = "what's the btc price right now? secret-marker-123"
        note = substance.retry_instruction("only reaction words", user)
        self.assertNotIn("secret-marker-123", note)
        self.assertIn("Do not invent facts", note)
        self.assertIn("current events", note)
        self.assertIn("say so plainly", note)
        self.assertIn("longer", note)
        self.assertNotIn("\u2014", note)

    def test_honest_lines_never_invent_facts_or_use_filler_openers(self):
        for user in ("gm", "how do i fix this?", "my dad died last week", ""):
            line = substance.honest_short_reply(user)
            self.assertEqual(line, line.lower())
            self.assertNotIn("\u2014", line)
            self.assertFalse(line.startswith(("ah", "great", "sure", "well")))
            self.assertNotRegex(line, r"\d")


class ContextualReplyIntegrationTests(unittest.TestCase):
    def setUp(self):
        main._COMMAND_RATE_LIMITS.clear()

    def run_reply(self, text, model):
        class Channel:
            id = 9
            name = "general"

            def history(self, limit, before=None):
                async def _empty():
                    if False:
                        yield None
                return _empty()

        message = SimpleNamespace(
            guild=SimpleNamespace(id=50, name="Seclude", member_count=3),
            channel=Channel(),
            author=SimpleNamespace(id=10, display_name="Death"),
            content=f"<@999> {text}",
            attachments=[],
            embeds=[],
            reference=None,
        )
        with patch.dict(main.os.environ, {"XAI_API_KEY": "test-key"}), patch.object(
            main.discord.Client,
            "user",
            new_callable=PropertyMock,
            return_value=SimpleNamespace(id=999),
        ), patch.object(
            main.bot, "get_channel", return_value=SimpleNamespace(name="general")
        ), patch.object(
            main, "_needs_live_sources", return_value=False
        ), patch.object(main, "_complete_contextual_reply", model):
            return asyncio.run(main.generate_contextual_reply(message))

    def test_mention_prompt_carries_the_substance_rules_and_introspection_restate(self):
        model = AsyncMock(return_value="leave the switch where you can reach it.")
        self.run_reply("a killswitch if the build goes wrong", model)
        prompt = model.await_args.args[2]
        self.assertIn(substance.SUBSTANCE_PROMPT, prompt)
        self.assertIn("Restate only as an introspection tool", prompt)
        self.assertNotIn("SUBSTANCE RETRY", prompt)

    def test_thin_reply_retries_once_with_a_stronger_instruction(self):
        question = "how do i stop my docker container from restarting?"
        good = MOOD_CASES["direct answer"]["good"]
        model = AsyncMock(side_effect=["good question.", good])
        result = self.run_reply(question, model)
        self.assertEqual(result.text, good)
        self.assertEqual(model.await_count, 2)
        first_prompt = model.await_args_list[0].args[2]
        retry_prompt = model.await_args_list[1].args[2]
        self.assertNotIn("SUBSTANCE RETRY", first_prompt)
        self.assertTrue(retry_prompt.startswith(first_prompt))
        self.assertIn("SUBSTANCE RETRY", retry_prompt)
        self.assertIn("Answer it in the first sentence", retry_prompt)

    def test_double_failure_returns_the_honest_short_reply(self):
        question = "how do i stop my docker container from restarting?"
        model = AsyncMock(side_effect=["good question.", "hmm."])
        result = self.run_reply(question, model)
        self.assertEqual(model.await_count, 2)
        self.assertEqual(result.text, substance.honest_short_reply(question))
        self.assertIsNone(result.failure)

    def test_a_failing_retry_call_returns_the_honest_short_reply(self):
        question = "how do i stop my docker container from restarting?"
        model = AsyncMock(side_effect=["good question.", asyncio.TimeoutError()])
        result = self.run_reply(question, model)
        self.assertEqual(result.text, substance.honest_short_reply(question))

    def test_small_talk_goes_through_with_one_model_call(self):
        model = AsyncMock(return_value="lol same.")
        result = self.run_reply("lol", model)
        self.assertEqual(result.text, "lol same.")
        self.assertEqual(model.await_count, 1)

    def test_substantive_reply_goes_through_with_one_model_call(self):
        good = MOOD_CASES["empathy and presence"]["good"]
        model = AsyncMock(return_value=good)
        result = self.run_reply("my dad died last week", model)
        self.assertEqual(result.text, good)
        self.assertEqual(model.await_count, 1)

    def test_provider_silence_keeps_the_existing_failure_path(self):
        model = AsyncMock(return_value=None)
        result = self.run_reply("how do i stop my docker container from restarting?", model)
        self.assertIsNone(result.text)
        self.assertEqual(model.await_count, 1)


if __name__ == "__main__":
    unittest.main()
