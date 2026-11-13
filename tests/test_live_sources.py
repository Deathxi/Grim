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

    def assert_plain_failure(self, reply, line_key):
        self.assertEqual(reply.text, main.LIVE_SOURCE_FAILURE_LINES[line_key])
        folded = reply.text.casefold()
        for claim in TRAINING_CLAIMS:
            self.assertNotIn(claim, folded)
        self.assertTrue("memory" in folded or "won't guess" in folded)

    def test_factual_and_current_asks_require_live_sources(self):
        for text in (
            "what's jensen huang's position?",
            "who is the ceo of nvidia",
            "the board removed him",
            "is this true",
            "latest news on the election",
            "why is the sky blue?",
        ):
            self.assertTrue(main._needs_live_sources(text, ""), text)

    def test_casual_creative_and_personal_talk_stays_on_chat_path(self):
        for text in (
            "tell me a joke",
            "lol",
            "how are you",
            "damn accurate",
            "write me a poem about the void?",
            "i got fired and i feel awful",
            "they fired me",
            "what do you think of my build?",
        ):
            self.assertFalse(main._needs_live_sources(text, ""), text)

    def test_capability_question_is_not_a_news_search(self):
        for text in (
            "let me try here but do you have any live search capability?",
            "do you have live search",
            "can you search the web",
            "is live search working",
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
        ):
            self.assertFalse(main._is_self_capability_question(text), text)
            self.assertTrue(main._needs_live_sources(text, ""), text)

    def test_followup_after_failed_search_searches_again(self):
        self.assertTrue(main._needs_live_sources("nvidia though", "", recent_failure=True))
        self.assertFalse(main._needs_live_sources("nvidia though", "", recent_failure=False))
        self.assertFalse(main._needs_live_sources("lol ok", "", recent_failure=True))

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

    def test_research_request_uses_web_and_x_search_and_reports_empty(self):
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
        self.assertEqual(
            [tool["type"] for tool in payload["tools"]],
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
        self.assertIn(main.format_grim_current_time()[:10], system_prompt)

    def test_empty_search_says_so_without_training_knowledge(self):
        research = AsyncMock(return_value={"status": "empty"})
        (reply,), model = self.converse(["what's jensen huang's position?"], research)
        model.assert_not_awaited()
        self.assert_plain_failure(reply, "empty")

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

    def test_followups_do_not_turn_a_failed_search_into_a_claim(self):
        research = AsyncMock(return_value={"status": "empty"})
        replies, model = self.converse(
            [
                "what's jensen huang's position?",
                "the board removed him",
                "so is he still in charge",
            ],
            research,
            model_reply="his position reads stable from available info.",
        )
        self.assertEqual(research.await_count, 3)
        model.assert_not_awaited()
        for reply in replies:
            self.assert_plain_failure(reply, "empty")

    def test_casual_followup_after_failure_carries_the_failure_forward(self):
        research = AsyncMock(return_value={"status": "empty"})
        replies, model = self.converse(
            ["what's jensen huang's position?", "lol ok"],
            research,
            model_reply="the void keeps its secrets.",
        )
        self.assertEqual(research.await_count, 1)
        self.assertEqual(replies[1].text, "the void keeps its secrets.")
        prompt = model.await_args.args[2]
        self.assertIn("<LIVE_RESEARCH_STATUS>", prompt)
        self.assertIn("never describe anything as stable", prompt)

    def test_success_clears_failure_memory(self):
        research = AsyncMock(side_effect=[{"status": "empty"}, SUCCESS])
        replies, model = self.converse(
            ["what's jensen huang's position?", "check again rn", "lol nice"],
            research,
            model_reply="told you.",
        )
        self.assertIn("sources:", replies[1].text)
        prompt = model.await_args.args[2]
        self.assertNotIn("<LIVE_RESEARCH_STATUS>", prompt)

    def test_casual_chat_never_searches(self):
        research = AsyncMock(return_value=SUCCESS)
        (reply,), model = self.converse(["tell me a joke"], research, model_reply="death walks into a bar.")
        research.assert_not_awaited()
        self.assertEqual(reply.text, "death walks into a bar.")

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

    def test_research_retries_web_search_only_after_tool_rejected(self):
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
            result = asyncio.run(main.grok_research_query("sys", "who runs nvidia"))
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
