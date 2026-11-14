import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, PropertyMock, patch

import main


class FakeChannel:
    id = 9
    name = "general"

    def history(self, limit, before=None):
        async def _empty():
            if False:
                yield None
        return _empty()


def mention(text, channel=None):
    return SimpleNamespace(
        guild=SimpleNamespace(id=50, name="Seclude", member_count=3),
        channel=channel or FakeChannel(),
        author=SimpleNamespace(id=10, display_name="Noct"),
        content=f"<@999> {text}",
        attachments=[],
        embeds=[],
        reference=None,
    )


SUCCESS = {
    "status": "success",
    "used_search": True,
    "cached": False,
    "text": "as of nov 12, reuters reports he is still ceo.",
    "sources": [
        {"title": "Reuters", "url": "https://www.reuters.com/tech/a"},
        {"title": "Reuters again", "url": "https://www.reuters.com/tech/b"},
        {"title": "Post", "url": "https://x.com/someone/status/1"},
    ],
}

TRAINING_CLAIMS = ("stable", "no reports", "available info", "still ceo", "as far as i know")
MISS_WRAP = "can't verify that from here, and i'm not going to invent it."


class FakeResponse:
    def __init__(self, status, data):
        self.status = status
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._data


class FakeSession:
    def __init__(self, response, calls):
        self._responses = response if isinstance(response, list) else [response]
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, headers=None, json=None):
        self._calls.append((url, json))
        if self._responses:
            return self._responses.pop(0)
        return FakeResponse(500, {})


class LiveSourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db = main.CHAT_DB_FILE
        main.CHAT_DB_FILE = str(Path(self.temp_dir.name) / "chat_history.db")
        main.init_chat_db()
        main._COMMAND_RATE_LIMITS.clear()
        main._AI_RESEARCH_CACHE.clear()
        main._LIVE_RESEARCH_FAILURES.clear()

    def tearDown(self):
        main.CHAT_DB_FILE = self.original_db
        main._COMMAND_RATE_LIMITS.clear()
        main._AI_RESEARCH_CACHE.clear()
        main._LIVE_RESEARCH_FAILURES.clear()
        self.temp_dir.cleanup()

    def converse(self, texts, research, model_reply="sure."):
        """Run mentions through the real handler with search and chat stubbed."""
        if isinstance(model_reply, (list, tuple)):
            model = AsyncMock(side_effect=list(model_reply))
        else:
            model = AsyncMock(return_value=model_reply)
        results = []

        async def run():
            channel = FakeChannel()
            with patch.dict(main.os.environ, {"XAI_API_KEY": "test-key"}), \
                    patch.object(
                        main.discord.Client,
                        "user",
                        new_callable=PropertyMock,
                        return_value=SimpleNamespace(id=999),
                    ), \
                    patch.object(main.bot, "get_channel", return_value=SimpleNamespace(name="general")), \
                    patch.object(main, "grok_research_query", research), \
                    patch.object(main, "_complete_contextual_reply", model):
                for text in texts:
                    results.append(await main.generate_contextual_reply(mention(text, channel)))

        asyncio.run(run())
        return results, model

    def assert_zero_info_miss(self, reply, wrap=MISS_WRAP):
        first, sep, rest = (reply.text or "").partition("\n")
        self.assertEqual(first, "Live Search has zero info on this")
        self.assertEqual(sep, "\n")
        self.assertEqual(rest, wrap)
        self.assertEqual(reply.text, f"{main.LIVE_SEARCH_ZERO_INFO}\n{wrap}")
        self.assertNotIn("\n", rest)
        self.assertNotIn("where it stands", reply.text)
        self.assertNotIn("-# sources:", rest)
        folded = rest.casefold()
        for claim in TRAINING_CLAIMS:
            self.assertNotIn(claim, folded)

    def assert_plain_failure(self, reply, line_key, wrap=MISS_WRAP):
        if line_key == "empty":
            self.assert_zero_info_miss(reply, wrap)
            return
        self.assertEqual(reply.text, main.LIVE_SOURCE_FAILURE_LINES[line_key])
        folded = reply.text.casefold()
        for claim in TRAINING_CLAIMS:
            self.assertNotIn(claim, folded)
        self.assertTrue("memory" in folded or "won't guess" in folded)

    def test_facts_information_and_method_require_live_sources(self):
        for text in (
            "what's jensen huang's position?",
            "who is the ceo of nvidia",
            "the board removed him",
            "is this true",
            "latest news on the election",
            "how do I update nvidia drivers",
            "how to check election results",
            "walk me through the process of filing that form",
            "What causes apathy?",
            "do you think excessive smoking and staying up late at night can cause potential apathy?",
            "can smoking cause apathy",
        ):
            self.assertTrue(main._needs_live_sources(text, ""), text)

    def test_casual_creative_and_personal_talk_stays_on_chat_path(self):
        for text in (
            "tell me a joke",
            "lol",
            "how are you",
            "how is it going pal",
            "you okay?",
            "huh bruh",
            "damn accurate",
            "write me a poem about the void?",
            "i got fired and i feel awful",
            "they fired me",
            "what do you think of my build?",
            "why is the sky blue?",
            "what's the vibe",
            "> exactly they’ve got their priorities jacked up\nhuh bruh",
        ):
            self.assertFalse(main._needs_live_sources(text, ""), text)
            self.assertFalse(
                main._needs_live_sources(text, "", recent_failure=True),
                text,
            )

    def test_capability_question_is_not_a_news_search(self):
        for text in (
            "let me try here but do you have any live search capability?",
            "do you have live search",
            "can you search the web",
            "is live search working",
            "Do you have access to the Internet?",
            "it doesn't need to be from memory. Do you have access to the Internet?",
            "do you have internet access",
        ):
            self.assertTrue(main._is_self_capability_question(text), text)
            self.assertFalse(main._needs_live_sources(text, ""), text)
            self.assertFalse(
                main._needs_live_sources(text, "", recent_failure=True),
                text,
            )
        for text in (
            "what's jensen huang's position?",
            "can you search the web for nvidia's ceo",
            "latest news on the election",
            "do you think excessive smoking and staying up late at night can cause potential apathy?",
        ):
            self.assertFalse(main._is_self_capability_question(text), text)
            self.assertTrue(main._needs_live_sources(text, ""), text)

    def test_fail_memory_does_not_force_search_or_block_chat(self):
        self.assertFalse(main._needs_live_sources("nvidia though", "", recent_failure=True))
        self.assertFalse(main._needs_live_sources("nvidia though", "", recent_failure=False))
        self.assertFalse(main._needs_live_sources("lol ok", "", recent_failure=True))
        self.assertFalse(
            main._needs_live_sources("how is it going pal", "", recent_failure=True)
        )
        self.assertFalse(
            main._needs_live_sources("what's jensen huang's position?", "", recent_failure=True)
        )
        self.assertFalse(
            main._needs_live_sources(
                "do you think excessive smoking and staying up late at night "
                "can cause potential apathy?",
                "",
                recent_failure=True,
            )
        )
        self.assertTrue(
            main._needs_live_sources("check again", "", recent_failure=True)
        )

    def test_no_current_sources_sentinel_is_not_an_answer(self):
        self.assertIsNone(main._extract_grok_research_response({
            "output": [
                {"type": "web_search_call", "status": "completed"},
                {
                    "type": "message",
                    "content": [{
                        "type": "output_text",
                        "text": main.NO_CURRENT_SOURCES_SENTINEL,
                        "annotations": [{"url": "https://example.com", "title": "x"}],
                    }],
                },
            ],
        }))

    def test_research_request_defaults_to_web_only_fast_payload(self):
        calls = []
        empty = {"output": [{"type": "web_search_call", "status": "completed"}]}
        session = FakeSession(FakeResponse(200, empty), calls)
        with patch.dict(main.os.environ, {"XAI_API_KEY": "test-key"}), \
                patch.object(main.aiohttp, "ClientSession", return_value=session):
            result = asyncio.run(main.grok_research_query("sys", "who runs nvidia"))
        self.assertEqual(result, {"status": "empty"})
        url, payload = calls[0]
        self.assertEqual(url, "https://api.x.ai/v1/responses")
        self.assertEqual(payload["model"], main.XAI_SEARCH_MODEL_DEFAULT)
        self.assertEqual(payload["reasoning"], {"effort": "none"})
        self.assertEqual(payload["max_output_tokens"], main.LIVE_SEARCH_MAX_TOKENS)
        self.assertEqual(main.LIVE_SEARCH_HTTP_TIMEOUT, 12)
        self.assertEqual(main.LIVE_SEARCH_WAIT_TIMEOUT, 15)
        self.assertEqual(
            [tool["type"] for tool in payload["tools"]],
            ["web_search"],
        )
        self.assertEqual(len(calls), 1)

    def test_x_search_only_when_the_ask_needs_x(self):
        self.assertFalse(main._needs_x_search("who is nvidia's ceo"))
        self.assertTrue(main._needs_x_search("what did they tweet about nvidia"))
        self.assertEqual(main._search_tools_for("who is nvidia's ceo"), main.XAI_WEB_SEARCH_TOOLS)
        self.assertEqual(
            main._search_tools_for("what did they tweet about nvidia"),
            main.XAI_SEARCH_TOOLS,
        )
        calls = []
        empty = {"output": [{"type": "web_search_call", "status": "completed"}]}
        session = FakeSession(FakeResponse(200, empty), calls)
        with patch.dict(main.os.environ, {"XAI_API_KEY": "test-key"}), \
                patch.object(main.aiohttp, "ClientSession", return_value=session):
            result = asyncio.run(
                main.grok_research_query(
                    "sys",
                    "what did they tweet about nvidia",
                    tools=main.XAI_SEARCH_TOOLS,
                )
            )
        self.assertEqual(result, {"status": "empty"})
        self.assertEqual(
            [tool["type"] for tool in calls[0][1]["tools"]],
            ["web_search", "x_search"],
        )

    def test_research_request_reports_provider_error(self):
        session = FakeSession(FakeResponse(500, {}), [])
        with patch.dict(main.os.environ, {"XAI_API_KEY": "test-key"}), \
                patch.object(main.aiohttp, "ClientSession", return_value=session):
            result = asyncio.run(main.grok_research_query("sys", "who runs nvidia"))
        self.assertEqual(result["status"], "provider_error")

    def test_successful_search_is_used_with_dated_answer_and_short_sources(self):
        research = AsyncMock(return_value=SUCCESS)
        (reply,), model = self.converse(["what's jensen huang's position?"], research)
        self.assertEqual(research.await_count, 1)
        model.assert_not_awaited()
        self.assertTrue(reply.text.startswith(SUCCESS["text"]))
        self.assertIn(
            "-# sources: [reuters.com](<https://www.reuters.com/tech/a>)"
            " · [x.com](<https://x.com/someone/status/1>)",
            reply.text,
        )
        system_prompt = research.await_args.args[0]
        self.assertIn("never from training", system_prompt)
        self.assertIn("newest", system_prompt)
        self.assertIn("causal", system_prompt)
        self.assertIn(main.format_grim_current_time()[:10], system_prompt)

    def test_empty_search_says_so_without_training_knowledge(self):
        research = AsyncMock(return_value={"status": "empty"})
        (reply,), model = self.converse(
            ["what's jensen huang's position?"],
            research,
            model_reply=MISS_WRAP,
        )
        model.assert_awaited()
        self.assert_zero_info_miss(reply, MISS_WRAP)
        prompt = model.await_args.args[2]
        self.assertIn("casual wrap", prompt)
        self.assertNotIn("<LIVE_RESEARCH_STATUS>", prompt)

    def test_search_error_timeout_and_exception_say_so_plainly(self):
        for outcome, key in (
            ({"status": "provider_error", "http_status": 500}, "error"),
            ({"status": "parse_error"}, "error"),
            ({"status": "timeout"}, "timeout"),
        ):
            main._LIVE_RESEARCH_FAILURES.clear()
            (reply,), model = self.converse(["who is nvidia's ceo?"], AsyncMock(return_value=outcome))
            model.assert_not_awaited()
            self.assert_plain_failure(reply, key)
        main._LIVE_RESEARCH_FAILURES.clear()
        crash = AsyncMock(side_effect=RuntimeError("boom"))
        (reply,), model = self.converse(["who is nvidia's ceo?"], crash)
        model.assert_not_awaited()
        self.assert_plain_failure(reply, "error")

    def test_rate_limited_search_does_not_guess(self):
        research = AsyncMock(return_value=SUCCESS)
        with patch.object(main, "_research_rate_limit_allows", return_value=False):
            (reply,), model = self.converse(["who is nvidia's ceo?"], research)
        research.assert_not_awaited()
        model.assert_not_awaited()
        self.assertEqual(reply.text, main.LIVE_SOURCE_FAILURE_LINES["rate_limited"])

    def test_followups_after_one_miss_return_to_chat_demeanor(self):
        research = AsyncMock(return_value={"status": "empty"})
        wrap = "still here. not filling that in."
        replies, model = self.converse(
            [
                "what's jensen huang's position?",
                "the board removed him",
                "so is he still in charge",
            ],
            research,
            model_reply=wrap,
        )
        self.assertEqual(research.await_count, 1)
        self.assert_zero_info_miss(replies[0], wrap)
        self.assertEqual(replies[1].text, wrap)
        self.assertEqual(replies[2].text, wrap)
        for reply in replies[1:]:
            self.assertNotIn("Live Search has zero info on this", reply.text)
            self.assertNotIn("where it stands", reply.text)
        self.assertEqual(model.await_count, 3)

    def test_casual_followup_after_failure_is_not_a_news_miss(self):
        research = AsyncMock(return_value={"status": "empty"})
        wrap = "the void keeps its secrets."
        replies, model = self.converse(
            ["what's jensen huang's position?", "lol ok"],
            research,
            model_reply=wrap,
        )
        self.assertEqual(research.await_count, 1)
        self.assert_zero_info_miss(replies[0], wrap)
        self.assertEqual(replies[1].text, wrap)
        self.assertNotIn("Live Search has zero info on this", replies[1].text)
        self.assertNotIn("where it stands", replies[1].text)
        prompt = model.await_args.args[2]
        self.assertNotIn("<LIVE_RESEARCH_STATUS>", prompt)
        self.assertNotIn("casual wrap", prompt)

    def test_success_clears_failure_memory(self):
        research = AsyncMock(side_effect=[{"status": "empty"}, SUCCESS])
        replies, model = self.converse(
            ["what's jensen huang's position?", "check again rn", "lol nice"],
            research,
            model_reply=[MISS_WRAP, "told you."],
        )
        self.assert_zero_info_miss(replies[0], MISS_WRAP)
        self.assertIn("sources:", replies[1].text)
        self.assertNotIn("Live Search has zero info on this", replies[1].text)
        self.assertEqual(replies[2].text, "told you.")
        prompt = model.await_args.args[2]
        self.assertNotIn("<LIVE_RESEARCH_STATUS>", prompt)

    def test_casual_chat_never_searches(self):
        research = AsyncMock(return_value=SUCCESS)
        (reply,), model = self.converse(["tell me a joke"], research, model_reply="death walks into a bar.")
        research.assert_not_awaited()
        self.assertEqual(reply.text, "death walks into a bar.")

    def test_greeting_and_quote_do_not_take_the_news_miss_path(self):
        research = AsyncMock(return_value={"status": "empty"})
        main._remember_live_failure("50:9")
        (hello,), model = self.converse(
            ["you okay?"],
            research,
            model_reply="yeah, holding steady.",
        )
        research.assert_not_awaited()
        model.assert_awaited()
        self.assertEqual(hello.text, "yeah, holding steady.")
        self.assertNotIn("where it stands", hello.text)
        self.assertNotEqual(hello.text, main.LIVE_SOURCE_FAILURE_LINES["empty"])

        main._remember_live_failure("50:9")
        (hello,), model = self.converse(
            ["how is it going pal"],
            research,
            model_reply="still here. the void's quiet.",
        )
        research.assert_not_awaited()
        model.assert_awaited()
        self.assertEqual(hello.text, "still here. the void's quiet.")
        self.assertNotIn("where it stands", hello.text)
        self.assertNotEqual(hello.text, main.LIVE_SOURCE_FAILURE_LINES["empty"])
        self.assertNotIn("<LIVE_RESEARCH_STATUS>", model.await_args.args[2])

        main._remember_live_failure("50:9")
        quoted = (
            "> exactly they've got their priorities jacked up\n"
            "huh bruh"
        )
        (quote_reply,), model = self.converse(
            [quoted],
            research,
            model_reply="yeah. priorities are a mess.",
        )
        research.assert_not_awaited()
        self.assertEqual(quote_reply.text, "yeah. priorities are a mess.")
        self.assertNotIn("where it stands", quote_reply.text)

    def test_capability_question_uses_a_successful_search(self):
        research = AsyncMock(return_value=SUCCESS)
        ping = "let me try here but do you have any live search capability?"
        (reply,), model = self.converse([ping], research)
        self.assertEqual(research.await_count, 1)
        model.assert_not_awaited()
        self.assertNotEqual(reply.text, main.LIVE_SOURCE_FAILURE_LINES["empty"])
        self.assertNotIn("where it stands", reply.text)
        self.assertTrue(reply.text.startswith(main.CAPABILITY_PROOF_PREFIX))
        self.assertIn(SUCCESS["text"], reply.text)
        self.assertIn(
            "-# sources: [reuters.com](<https://www.reuters.com/tech/a>)",
            reply.text,
        )
        self.assertEqual(research.await_args.args[1], main.LIVE_SEARCH_PROOF_QUERY)
        self.assertNotIn("capability", research.await_args.args[1].casefold())
        self.assertEqual(
            tuple(research.await_args.kwargs.get("tools") or ()),
            main.XAI_WEB_SEARCH_TOOLS,
        )

    def test_capability_empty_or_fail_stays_honest_not_a_news_miss(self):
        ping = "do you have any live search capability?"
        research = AsyncMock(return_value={"status": "empty"})
        (reply,), model = self.converse([ping], research)
        model.assert_not_awaited()
        self.assertEqual(reply.text, main.LIVE_SOURCE_CAPABILITY_LINES["empty"])
        self.assertNotEqual(reply.text, main.LIVE_SOURCE_FAILURE_LINES["empty"])
        folded = reply.text.casefold()
        self.assertIn("wired", folded)
        self.assertTrue("memory" in folded or "won't guess" in folded)
        for claim in TRAINING_CLAIMS:
            self.assertNotIn(claim, folded)

        main._LIVE_RESEARCH_FAILURES.clear()
        (reply,), model = self.converse(
            [ping], AsyncMock(return_value={"status": "provider_error"})
        )
        model.assert_not_awaited()
        self.assertEqual(reply.text, main.LIVE_SOURCE_CAPABILITY_LINES["error"])
        self.assertNotIn("where it stands", reply.text)

    def test_capability_question_ignores_channel_failure_memory(self):
        research = AsyncMock(return_value=SUCCESS)
        main._remember_live_failure("50:9")
        (reply,), model = self.converse(
            ["let me try here but do you have any live search capability?"],
            research,
        )
        model.assert_not_awaited()
        self.assertTrue(reply.text.startswith(main.CAPABILITY_PROOF_PREFIX))
        self.assertIn("sources:", reply.text)
        self.assertNotIn("where it stands", reply.text)

    def test_web_only_search_does_not_retry_a_second_round_trip(self):
        calls = []
        session = FakeSession([FakeResponse(400, {}), FakeResponse(200, {})], calls)
        with patch.dict(main.os.environ, {"XAI_API_KEY": "test-key"}), \
                patch.object(main.aiohttp, "ClientSession", return_value=session):
            result = asyncio.run(main.grok_research_query("sys", "who runs nvidia"))
        self.assertEqual(result["status"], "provider_error")
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            [tool["type"] for tool in calls[0][1]["tools"]],
            ["web_search"],
        )

    def test_research_retries_web_search_only_after_x_tool_rejected(self):
        calls = []
        success_body = {
            "output": [
                {"type": "web_search_call", "status": "completed"},
                {
                    "type": "message",
                    "content": [{
                        "type": "output_text",
                        "text": SUCCESS["text"],
                        "annotations": [{
                            "url": "https://www.reuters.com/tech/a",
                            "title": "Reuters",
                        }],
                    }],
                },
            ],
        }
        session = FakeSession(
            [FakeResponse(400, {}), FakeResponse(200, success_body)],
            calls,
        )
        with patch.dict(main.os.environ, {"XAI_API_KEY": "test-key"}), \
                patch.object(main.aiohttp, "ClientSession", return_value=session):
            result = asyncio.run(
                main.grok_research_query(
                    "sys",
                    "what did they tweet about nvidia",
                    tools=main.XAI_SEARCH_TOOLS,
                )
            )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["text"], SUCCESS["text"])
        self.assertEqual(
            [tool["type"] for tool in calls[0][1]["tools"]],
            ["web_search", "x_search"],
        )
        self.assertEqual(
            [tool["type"] for tool in calls[1][1]["tools"]],
            ["web_search"],
        )

    def test_fact_search_uses_a_short_web_only_prompt(self):
        research = AsyncMock(return_value=SUCCESS)
        (reply,), model = self.converse(["what's jensen huang's position?"], research)
        model.assert_not_awaited()
        self.assertTrue(reply.text.startswith(SUCCESS["text"]))
        self.assertEqual(
            tuple(research.await_args.kwargs.get("tools") or ()),
            main.XAI_WEB_SEARCH_TOOLS,
        )
        self.assertEqual(
            research.await_args.args[1],
            "what's jensen huang's position?",
        )
        self.assertLessEqual(len(research.await_args.args[1]), 1200)

    def test_causal_health_ask_uses_live_search(self):
        research = AsyncMock(return_value=SUCCESS)
        ask = (
            "do you think excessive smoking and staying up late at night "
            "can cause potential apathy?"
        )
        (reply,), model = self.converse([ask], research)
        self.assertEqual(research.await_count, 1)
        model.assert_not_awaited()
        self.assertTrue(reply.text.startswith(SUCCESS["text"]))
        self.assertIn("sources:", reply.text)
        self.assertEqual(research.await_args.args[1], ask)
        system_prompt = research.await_args.args[0]
        self.assertIn("never from training", system_prompt)
        self.assertIn("causal", system_prompt)

    def test_internet_access_ask_is_capability_not_a_news_miss(self):
        research = AsyncMock(return_value=SUCCESS)
        ping = "it doesn't need to be from memory. Do you have access to the Internet?"
        (reply,), model = self.converse([ping], research)
        self.assertEqual(research.await_count, 1)
        model.assert_not_awaited()
        self.assertTrue(reply.text.startswith(main.CAPABILITY_PROOF_PREFIX))
        self.assertNotIn("where it stands", reply.text)
        self.assertNotEqual(reply.text, main.LIVE_SOURCE_FAILURE_LINES["empty"])
        self.assertEqual(research.await_args.args[1], main.LIVE_SEARCH_PROOF_QUERY)

    def test_miss_reply_formatter_is_two_lines(self):
        self.assertEqual(
            main.format_live_search_miss_reply("yeah, holding steady."),
            "Live Search has zero info on this\nyeah, holding steady.",
        )
        self.assertEqual(
            main.format_live_search_miss_reply(
                "Live Search has zero info on this\ncan't verify that from here, "
                "and i'm not going to invent it."
            ),
            "Live Search has zero info on this\n"
            "can't verify that from here, and i'm not going to invent it.",
        )
        self.assertEqual(
            main.format_live_search_miss_reply(""),
            f"{main.LIVE_SEARCH_ZERO_INFO}\n{main.LIVE_SEARCH_MISS_WRAP_FALLBACK}",
        )

    def test_empty_miss_is_two_line_body(self):
        research = AsyncMock(return_value={"status": "empty"})
        (reply,), model = self.converse(
            ["What's jensen huang's position?"],
            research,
            model_reply=MISS_WRAP,
        )
        self.assertEqual(research.await_count, 1)
        model.assert_awaited()
        self.assertEqual(
            reply.text,
            "Live Search has zero info on this\n" + MISS_WRAP,
        )
        first, sep, rest = reply.text.partition("\n")
        self.assertEqual(first, "Live Search has zero info on this")
        self.assertEqual(sep, "\n")
        self.assertEqual(rest, MISS_WRAP)
        self.assertNotIn("sources:", rest)
        self.assertNotIn("where it stands", reply.text)

    def test_miss_then_followups_stay_on_chat_including_causal_retry(self):
        research = AsyncMock(return_value={"status": "empty"})
        smoking = (
            "do you think excessive smoking and staying up late at night "
            "can cause potential apathy?"
        )
        greeting = "yeah, holding steady."
        replies, model = self.converse(
            [
                smoking,
                "you okay?",
                smoking,
                "what is it about the question that you can't pull fresh details?",
            ],
            research,
            model_reply=[MISS_WRAP, greeting, greeting, greeting],
        )
        self.assertEqual(research.await_count, 1)
        self.assert_zero_info_miss(replies[0], MISS_WRAP)
        self.assertEqual(replies[1].text, greeting)
        self.assertEqual(replies[2].text, greeting)
        self.assertEqual(replies[3].text, greeting)
        for reply in replies[1:]:
            self.assertNotIn("Live Search has zero info on this", reply.text)
            self.assertNotIn("where it stands", reply.text)
        self.assertEqual(model.await_count, 4)

        main._LIVE_RESEARCH_FAILURES.clear()
        research = AsyncMock(return_value={"status": "empty"})
        replies, model = self.converse(
            [smoking, "Do you have access to the Internet?"],
            research,
            model_reply=MISS_WRAP,
        )
        self.assertEqual(research.await_count, 2)
        self.assert_zero_info_miss(replies[0], MISS_WRAP)
        self.assertEqual(replies[1].text, main.LIVE_SOURCE_CAPABILITY_LINES["empty"])
        self.assertNotEqual(replies[1].text, main.LIVE_SOURCE_FAILURE_LINES["empty"])
        self.assertNotIn("Live Search has zero info on this", replies[1].text)
        self.assertNotIn("where it stands", replies[1].text)
        self.assertEqual(research.await_args.args[1], main.LIVE_SEARCH_PROOF_QUERY)

    def test_leftover_topic_after_failure_does_not_block_chat(self):
        research = AsyncMock(return_value={"status": "empty"})
        wrap = "yeah nvidia's a whole saga."
        replies, model = self.converse(
            ["what's jensen huang's position?", "nvidia though"],
            research,
            model_reply=wrap,
        )
        self.assertEqual(research.await_count, 1)
        self.assert_zero_info_miss(replies[0], wrap)
        self.assertEqual(replies[1].text, wrap)
        self.assertNotIn("Live Search has zero info on this", replies[1].text)
        self.assertNotIn("<LIVE_RESEARCH_STATUS>", model.await_args.args[2])

    def test_sourced_reply_keeps_blue_links_and_suppresses_unfurls(self):
        sourced = main.format_sourced_reply(SUCCESS["text"], SUCCESS["sources"])
        proof = main.format_capability_proof_reply(SUCCESS["text"], SUCCESS["sources"])
        self.assertIn(
            "-# sources: [reuters.com](<https://www.reuters.com/tech/a>)"
            " · [x.com](<https://x.com/someone/status/1>)",
            sourced,
        )
        self.assertIn("https://www.reuters.com/tech/a", sourced)
        self.assertIn("https://x.com/someone/status/1", sourced)
        self.assertTrue(main._reply_has_source_links(sourced))
        self.assertTrue(main._reply_has_source_links(proof))
        self.assertEqual(
            main._source_unfurl_kwargs(sourced),
            {"suppress_embeds": True},
        )
        self.assertEqual(
            main._source_unfurl_kwargs(proof),
            {"suppress_embeds": True},
        )

    def test_unsourced_replies_do_not_suppress_embeds(self):
        for text in (
            "yeah, holding steady.",
            main.format_live_search_miss_reply(MISS_WRAP),
            main.LIVE_SOURCE_CAPABILITY_LINES["empty"],
            main.LIVE_SOURCE_FAILURE_LINES["timeout"],
            "saw this https://example.com/post and it's wild",
            main.format_sourced_reply("no citations this time.", []),
        ):
            self.assertFalse(main._reply_has_source_links(text), text)
            self.assertEqual(main._source_unfurl_kwargs(text), {}, text)

    def test_mention_send_suppresses_unfurls_only_on_sourced_replies(self):
        sourced = main.format_sourced_reply(SUCCESS["text"], SUCCESS["sources"])
        casual = "yeah, holding steady."
        long_body = ("as of nov 12, still ceo. " * 80) + "done."
        long_sourced = main.format_sourced_reply(long_body, SUCCESS["sources"])
        self.assertGreater(len(long_sourced), main.DISCORD_CONVERSATION_CHUNK_LIMIT)

        def send(text):
            sent = SimpleNamespace(
                id=1,
                created_at=SimpleNamespace(timestamp=lambda: 0.0),
            )
            reply_fn = AsyncMock(return_value=sent)
            channel_send = AsyncMock(return_value=sent)
            message = SimpleNamespace(
                content="<@999> what's jensen huang's position?",
                channel=SimpleNamespace(id=9, send=channel_send),
                guild=SimpleNamespace(id=50),
                reply=reply_fn,
            )
            with patch.object(
                main, "_maybe_build_gif_reaction", AsyncMock(return_value=None)
            ), patch.object(main, "save_message_to_db"):
                asyncio.run(main._send_mention_reply(message, text))
            return reply_fn, channel_send

        reply_fn, channel_send = send(sourced)
        self.assertEqual(reply_fn.await_args.args, (sourced,))
        self.assertEqual(reply_fn.await_args.kwargs["mention_author"], False)
        self.assertTrue(reply_fn.await_args.kwargs["suppress_embeds"])
        channel_send.assert_not_awaited()

        reply_fn, channel_send = send(casual)
        self.assertEqual(reply_fn.await_args.args, (casual,))
        self.assertEqual(reply_fn.await_args.kwargs["mention_author"], False)
        self.assertNotIn("suppress_embeds", reply_fn.await_args.kwargs)
        channel_send.assert_not_awaited()

        reply_fn, channel_send = send(long_sourced)
        self.assertTrue(reply_fn.await_args.kwargs["suppress_embeds"])
        self.assertTrue(channel_send.await_args.kwargs["suppress_embeds"])
        self.assertIn("-# sources:", channel_send.await_args.args[0])

        long_casual = ("yeah, holding steady. " * 100) + "done."
        self.assertGreater(len(long_casual), main.DISCORD_CONVERSATION_CHUNK_LIMIT)
        reply_fn, channel_send = send(long_casual)
        self.assertNotIn("suppress_embeds", reply_fn.await_args.kwargs)
        self.assertNotIn("suppress_embeds", channel_send.await_args.kwargs)

    def test_gif_mention_send_still_suppresses_source_unfurls(self):
        sourced = main.format_sourced_reply(SUCCESS["text"], SUCCESS["sources"])
        gif_embed = object()
        sent = SimpleNamespace(
            id=1,
            created_at=SimpleNamespace(timestamp=lambda: 0.0),
        )
        reply_fn = AsyncMock(return_value=sent)
        message = SimpleNamespace(
            content="<@999> what's jensen huang's position?",
            channel=SimpleNamespace(id=9, send=AsyncMock(return_value=sent)),
            guild=SimpleNamespace(id=50),
            reply=reply_fn,
        )
        with patch.object(
            main,
            "_maybe_build_gif_reaction",
            AsyncMock(return_value=(gif_embed, "with_text")),
        ), patch.object(main, "save_message_to_db"):
            asyncio.run(main._send_mention_reply(message, sourced))
        self.assertIs(reply_fn.await_args.kwargs["embed"], gif_embed)
        self.assertEqual(reply_fn.await_args.kwargs["content"], sourced)
        self.assertTrue(reply_fn.await_args.kwargs["suppress_embeds"])
        self.assertEqual(reply_fn.await_args.kwargs["mention_author"], False)

        casual = "yeah, holding steady."
        reply_fn = AsyncMock(return_value=sent)
        message.reply = reply_fn
        with patch.object(
            main,
            "_maybe_build_gif_reaction",
            AsyncMock(return_value=(gif_embed, "with_text")),
        ), patch.object(main, "save_message_to_db"):
            asyncio.run(main._send_mention_reply(message, casual))
        self.assertEqual(reply_fn.await_args.kwargs["content"], casual)
        self.assertNotIn("suppress_embeds", reply_fn.await_args.kwargs)

    def test_research_response_accepts_custom_tool_call_and_markdown_urls(self):
        result = main._extract_grok_research_response({
            "output": [
                {
                    "type": "custom_tool_call",
                    "name": "x_keyword_search",
                    "status": "completed",
                },
                {
                    "type": "message",
                    "content": [{
                        "type": "output_text",
                        "text": (
                            "as of nov 13, reuters reports a ceasefire. "
                            "https://www.reuters.com/world/a"
                        ),
                    }],
                },
            ],
        })
        self.assertIsNotNone(result)
        self.assertTrue(result["used_search"])
        self.assertEqual(
            result["sources"][0]["url"],
            "https://www.reuters.com/world/a",
        )


if __name__ == "__main__":
    unittest.main()
