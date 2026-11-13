import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
import reply_auditor
from reply_auditor import (
    BURST_LINE,
    LOOP_LINE,
    STUCK_BACK_LINE,
    STUCK_FACT_LINE,
    STUCK_FAIL_LINE,
    AuditorConfig,
    ReplyAuditor,
    format_events,
)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def fast_config(**overrides):
    timeouts = dict(reply_auditor.DEFAULT_TIMEOUTS)
    timeouts.update({kind: 0.05 for kind in timeouts})
    return AuditorConfig(timeouts=timeouts, **overrides)


def make_auditor(clock=None, logs=None, **overrides):
    return ReplyAuditor(
        fast_config(**overrides),
        clock=clock or FakeClock(),
        log=(logs.append if logs is not None else lambda line: None),
    )


async def hang():
    await asyncio.sleep(30)
    return "never"


async def stuck_reply(message):
    return await hang()


def make_message(content="hey grim, how's the void", user_id=3, channel_id=2):
    return SimpleNamespace(
        guild=SimpleNamespace(id=1),
        channel=SimpleNamespace(id=channel_id),
        author=SimpleNamespace(id=user_id),
        content=content,
        attachments=[],
        embeds=[],
    )


class TimeoutRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_stalled_reply_is_cancelled_reset_and_retried_once(self):
        auditor = make_auditor()
        resets = []
        auditor.register_reset(resets.append)
        cancelled = asyncio.Event()

        async def stuck():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        retry = AsyncMock(return_value="still here. the void says hi.")
        result = await auditor.guarded_reply("reply", "g:c:u", stuck, retry=retry)

        self.assertTrue(cancelled.is_set())
        self.assertEqual(resets, ["g:c:u"])
        retry.assert_awaited_once()
        self.assertEqual(result.event, "recovered")
        self.assertTrue(result.recovery.startswith(STUCK_BACK_LINE))
        self.assertIn("the void says hi", result.recovery)
        self.assertIsNone(result.value)
        self.assertEqual(
            [event["event"] for event in auditor.recent_events()],
            ["timeout", "recovered"],
        )

    async def test_failed_retry_gives_a_plain_failure_line(self):
        auditor = make_auditor()
        result = await auditor.guarded_reply(
            "reply", "g:c:u", hang, retry=AsyncMock(side_effect=RuntimeError("down"))
        )
        self.assertEqual(result.recovery, STUCK_FAIL_LINE)
        self.assertEqual(result.event, "recovery_failed")

    async def test_stalled_fact_check_never_invents_an_answer(self):
        auditor = make_auditor()
        result = await auditor.guarded_reply("fact_reply", "g:c:u", hang)
        self.assertEqual(result.recovery, STUCK_FACT_LINE)
        self.assertIn("couldn't verify", result.recovery)

    async def test_no_message_when_nobody_is_waiting(self):
        auditor = make_auditor()
        result = await auditor.guarded_reply("chime", "g:c:chime", hang, user_waiting=False)
        self.assertIsNone(result.recovery)
        self.assertEqual(result.event, "timeout")

    async def test_watched_tool_returns_its_timeout_shape(self):
        auditor = make_auditor()

        async def search(system_prompt, user_prompt):
            await asyncio.sleep(30)

        watched = auditor.watch_tool(
            "live_search",
            search,
            material=lambda *a, **k: a[1],
            on_timeout=lambda: {"status": "timeout"},
            on_blocked=lambda: {"status": "loop_blocked"},
        )
        self.assertEqual(await watched("sys", "who won"), {"status": "timeout"})

    async def test_main_hook_recovers_a_stuck_mention(self):
        auditor = make_auditor()
        with patch.object(main, "reply_auditor", auditor), patch.object(
            main, "generate_contextual_reply", new=stuck_reply
        ), patch.object(
            main, "_auditor_retry_reply", AsyncMock(return_value="back. what's up.")
        ):
            result = await main.audited_contextual_reply(make_message())
        self.assertTrue(result.text.startswith(STUCK_BACK_LINE))
        self.assertIn("what's up", result.text)

    async def test_main_hook_does_not_retry_a_fact_ask(self):
        auditor = make_auditor()
        retry = AsyncMock(return_value="made up answer")
        with patch.object(main, "reply_auditor", auditor), patch.object(
            main, "generate_contextual_reply", new=stuck_reply
        ), patch.object(main, "_auditor_retry_reply", retry):
            result = await main.audited_contextual_reply(
                make_message("who won the election in 2026?")
            )
        retry.assert_not_awaited()
        self.assertEqual(result.text, STUCK_FACT_LINE)

    def test_main_tools_are_wrapped(self):
        self.assertTrue(getattr(main.grok_research_query, "__wrapped_by_auditor__", False))
        self.assertTrue(getattr(main._revise_generated_output, "__wrapped_by_auditor__", False))
        self.assertIs(main._evaluate_and_chime, main._audited_chime)


class LoopDetectionTests(unittest.IsolatedAsyncioTestCase):
    def test_third_near_identical_reply_is_replaced_and_state_reset(self):
        auditor = make_auditor()
        resets = []
        auditor.register_reset(resets.append)
        first = auditor.review_reply("g:c:u", "the void is patient. so am i.")
        second = auditor.review_reply("g:c:u", "the void is patient, so am i!")
        third = auditor.review_reply("g:c:u", "The void is patient. So am I.")
        self.assertIsNone(first.event)
        self.assertIsNone(second.event)
        self.assertEqual(third.text, LOOP_LINE)
        self.assertEqual(third.event, "reply_loop")
        self.assertEqual(resets, ["g:c:u"])
        self.assertIsNone(auditor.review_reply("g:c:u", "the void is patient. so am i.").event)

    def test_repeats_outside_the_window_are_not_a_loop(self):
        clock = FakeClock()
        auditor = make_auditor(clock)
        for _ in range(3):
            review = auditor.review_reply("g:c:u", "same old line here")
            clock.now += 700
        self.assertIsNone(review.event)

    async def test_same_tool_call_over_and_over_is_blocked(self):
        auditor = make_auditor()
        calls = []

        async def search(system_prompt, user_prompt):
            calls.append(user_prompt)
            return {"status": "success"}

        watched = auditor.watch_tool(
            "live_search",
            search,
            material=lambda *a, **k: a[1],
            on_timeout=lambda: {"status": "timeout"},
            on_blocked=lambda: {"status": "loop_blocked"},
        )

        async def conversation():
            return [await watched("sys", "btc price") for _ in range(5)]

        outcome = await auditor.run("reply", conversation, conversation="g:c:u", timeout=5)
        statuses = [result["status"] for result in outcome.value]
        self.assertEqual(statuses[:3], ["success"] * 3)
        self.assertEqual(statuses[3:], ["loop_blocked"] * 2)
        self.assertEqual(len(calls), 3)

    async def test_tool_calls_without_a_conversation_are_not_counted(self):
        auditor = make_auditor()
        self.assertTrue(all(auditor.allow_tool_call("live_search", "x") for _ in range(10)))

    async def test_reply_chain_is_paused_with_one_notice(self):
        auditor = make_auditor(burst_limit=3)
        work = AsyncMock(return_value="ok")
        results = [await auditor.guarded_reply("reply", "g:c:u", work) for _ in range(5)]
        self.assertEqual([r.value for r in results[:3]], ["ok"] * 3)
        self.assertEqual(results[3].recovery, BURST_LINE)
        self.assertIsNone(results[4].recovery)
        self.assertEqual(results[4].event, "reply_chain")
        self.assertEqual(work.await_count, 3)

    def test_main_loop_hook_drops_the_cached_answer(self):
        auditor = make_auditor()
        text = "btc is at 100k right now"
        message = make_message("btc price?")
        with patch.object(main, "reply_auditor", auditor), patch.dict(
            main._AI_RESEARCH_CACHE, {"k": (0.0, {"text": text})}, clear=True
        ):
            main.audit_outgoing_reply(message, text)
            main.audit_outgoing_reply(message, text)
            third = main.audit_outgoing_reply(message, text)
            self.assertEqual(third, LOOP_LINE)
            self.assertNotIn("k", main._AI_RESEARCH_CACHE)


class NormalReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_work_passes_through_untouched(self):
        logs = []
        auditor = make_auditor(logs=logs)
        sentinel = object()
        result = await auditor.guarded_reply("reply", "g:c:u", AsyncMock(return_value=sentinel))
        self.assertIs(result.value, sentinel)
        self.assertIsNone(result.recovery)
        self.assertEqual(auditor.recent_events(), [])
        self.assertEqual(logs, [])

    def test_varied_clean_replies_are_unchanged(self):
        auditor = make_auditor()
        replies = [
            "rough week. sit with it, you don't have to fix it tonight.",
            "lol no. that cat has a better retirement plan than you.",
            "entropy wins eventually, but not before lunch.",
            "fair. go sleep, the void keeps.",
        ]
        for reply in replies:
            review = auditor.review_reply("g:c:u", reply)
            self.assertEqual(review.text, reply)
            self.assertIsNone(review.event)
        self.assertEqual(auditor.recent_events(), [])

    async def test_main_hook_keeps_a_normal_reply(self):
        auditor = make_auditor()
        reply = main.ContextualReply(text="just a reply")
        with patch.object(main, "reply_auditor", auditor), patch.object(
            main, "generate_contextual_reply", AsyncMock(return_value=reply)
        ):
            result = await main.audited_contextual_reply(make_message())
            sent = main.audit_outgoing_reply(make_message(), result.text)
        self.assertIs(result, reply)
        self.assertEqual(sent, "just a reply")

    async def test_work_errors_surface_unchanged(self):
        auditor = make_auditor()
        with self.assertRaises(ValueError):
            await auditor.guarded_reply("reply", "g:c:u", AsyncMock(side_effect=ValueError("x")))

    async def test_disabled_auditor_only_runs_the_work(self):
        auditor = ReplyAuditor(AuditorConfig(enabled=False), log=lambda line: None)
        work = AsyncMock(return_value="plain")
        result = await auditor.guarded_reply("reply", "g:c:u", work)
        self.assertEqual(result.value, "plain")
        work.assert_awaited_once()


class StyleTests(unittest.TestCase):
    def test_dash_and_opener_are_nudged_not_blocked(self):
        auditor = make_auditor()
        review = auditor.review_reply("g:c:u", "Ah, the void \u2014 patient as ever.")
        self.assertEqual(review.text, "the void, patient as ever.")
        self.assertEqual(review.event, "style")
        self.assertEqual(set(review.flags), {"dash", "opener"})

    def test_code_blocks_are_left_alone(self):
        text = "```\na \u2014 b\n```"
        self.assertEqual(reply_auditor.nudge_style(text), (text, ()))

    def test_stock_line_across_conversations_is_flagged(self):
        auditor = make_auditor()
        line = "the void remembers everything you forgot. anyway."
        flags = [auditor.review_reply(f"g:c:{user}", line).flags for user in range(4)]
        self.assertNotIn("stock_line", flags[2])
        self.assertIn("stock_line", flags[3])

    def test_question_streak_is_flagged(self):
        auditor = make_auditor()
        flags = [
            auditor.review_reply("g:c:u", text).flags
            for text in ("you good?", "what happened then?", "and after that, who called?")
        ]
        self.assertIn("question_streak", flags[2])

    def test_events_hold_no_message_content(self):
        auditor = make_auditor()
        secret = "my password is hunter2 \u2014 do not tell"
        auditor.review_reply("g:c:u", secret)
        dumped = repr(auditor.recent_events()) + format_events(auditor)
        self.assertNotIn("hunter2", dumped)
        self.assertNotIn("g:c:u", dumped)


class AuditorFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_bookkeeping_errors_fail_open(self):
        auditor = make_auditor()
        auditor.note_turn = lambda conversation: 1 / 0
        result = await auditor.guarded_reply("reply", "g:c:u", AsyncMock(return_value="fine"))
        self.assertEqual(result.value, "fine")

    async def test_recovery_errors_fail_open_to_the_old_timeout(self):
        auditor = make_auditor()
        auditor.reset_conversation = lambda conversation: 1 / 0
        result = await auditor.guarded_reply("reply", "g:c:u", hang)
        self.assertIsNone(result.recovery)
        self.assertEqual(result.event, "timeout")

    def test_review_errors_return_the_original_text(self):
        auditor = make_auditor()
        with patch.object(reply_auditor, "_normalize", side_effect=RuntimeError("boom")):
            review = auditor.review_reply("g:c:u", "original \u2014 text")
        self.assertEqual(review.text, "original \u2014 text")

    def test_broken_reset_hook_does_not_stop_the_reset(self):
        auditor = make_auditor()
        auditor.register_reset(lambda conversation: 1 / 0)
        auditor.review_reply("g:c:u", "a line")
        auditor.reset_conversation("g:c:u")
        self.assertNotIn("g:c:u", auditor._replies)

    async def test_main_hook_survives_a_broken_auditor(self):
        broken = SimpleNamespace(
            guarded_reply=AsyncMock(side_effect=RuntimeError("auditor down")),
            review_reply=lambda *a, **k: 1 / 0,
        )
        reply = main.ContextualReply(text="grim still talks")
        generate = AsyncMock(return_value=reply)
        with patch.object(main, "reply_auditor", broken), patch.object(
            main, "generate_contextual_reply", generate
        ):
            result = await main.audited_contextual_reply(make_message())
            sent = main.audit_outgoing_reply(make_message(), result.text)
        self.assertIs(result, reply)
        self.assertEqual(sent, "grim still talks")
        generate.assert_awaited_once()

    async def test_watched_tool_survives_a_broken_material_hook(self):
        auditor = make_auditor()
        watched = auditor.watch_tool(
            "fact_check",
            AsyncMock(return_value="revised"),
            material=lambda *a, **k: 1 / 0,
            on_timeout=lambda: None,
            on_blocked=lambda: None,
        )
        self.assertEqual(await watched("draft", "problem"), "revised")

    def test_bad_env_values_fall_back_to_defaults(self):
        with patch.dict(
            "os.environ",
            {"GRIM_AUDITOR_TIMEOUT_REPLY": "nope", "GRIM_AUDITOR_LOOP_REPEATS": "-4"},
        ):
            config = AuditorConfig.from_env()
        self.assertEqual(config.timeout_for("reply"), 45.0)
        self.assertEqual(config.loop_repeat_limit, 3)

    def test_env_overrides_apply(self):
        with patch.dict(
            "os.environ",
            {"GRIM_AUDITOR_TIMEOUT_LIVE_SEARCH": "12", "GRIM_AUDITOR_ENABLED": "off"},
        ):
            config = AuditorConfig.from_env()
        self.assertEqual(config.timeout_for("live_search"), 12.0)
        self.assertFalse(config.enabled)


class RateLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_stall_in_the_cooldown_gets_no_intervention(self):
        auditor = make_auditor()
        retry = AsyncMock(return_value="back")
        first = await auditor.guarded_reply("reply", "g:c:u", hang, retry=retry)
        second = await auditor.guarded_reply("reply", "g:c:u", hang, retry=retry)
        self.assertEqual(first.event, "recovered")
        self.assertIsNone(second.recovery)
        self.assertEqual(second.event, "timeout")
        retry.assert_awaited_once()
        self.assertEqual(auditor.counts().get("intervention_suppressed"), 1)

    async def test_cooldown_expires(self):
        clock = FakeClock()
        auditor = make_auditor(clock)
        self.assertTrue(auditor.allow_intervention("g:c:u"))
        self.assertFalse(auditor.allow_intervention("g:c:u"))
        clock.now += 301
        self.assertTrue(auditor.allow_intervention("g:c:u"))

    def test_global_cap_limits_interventions_across_conversations(self):
        auditor = make_auditor(global_intervention_limit=2)
        allowed = [auditor.allow_intervention(f"g:c:{user}") for user in range(4)]
        self.assertEqual(allowed, [True, True, False, False])

    def test_loop_breaker_cannot_loop_itself(self):
        auditor = make_auditor()
        texts = [auditor.review_reply("g:c:u", "same thing again and again").text for _ in range(9)]
        self.assertEqual(texts.count(LOOP_LINE), 1)
        self.assertEqual(auditor.review_reply("g:c:u", LOOP_LINE).text, LOOP_LINE)


class AdminCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_auditor_command_is_admin_only(self):
        response = SimpleNamespace(send_message=AsyncMock())
        interaction = SimpleNamespace(response=response)
        with patch.object(main, "require_permission", AsyncMock(return_value=False)):
            await main.grim_auditor.callback(interaction)
        response.send_message.assert_not_awaited()

    async def test_auditor_command_shows_events_privately(self):
        auditor = make_auditor()
        auditor.record("timeout", "reply", duration=45)
        response = SimpleNamespace(send_message=AsyncMock())
        interaction = SimpleNamespace(response=response)
        with patch.object(main, "reply_auditor", auditor), patch.object(
            main, "require_permission", AsyncMock(return_value=True)
        ):
            await main.grim_auditor.callback(interaction)
        args, kwargs = response.send_message.await_args
        self.assertIn("timeout (reply) 45.0s", args[0])
        self.assertTrue(kwargs["ephemeral"])


if __name__ == "__main__":
    unittest.main()
