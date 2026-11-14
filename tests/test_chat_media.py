import asyncio
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, PropertyMock, patch

import discord

import main
from tests.test_image_clean import dirty_gif, dirty_png, metadata_left


GIF_ASK = (
    "Grim show me a gif of the current events going on in the world today "
    "in this date."
)
PICTURE_ASK = "grim can you send pictures?"
MIKU_PICTURE = "send a picture of miku"
COPYPASTA_ASK = "send that copypasta as an image"
ASCII_ASK = "send ascii art of miku"
REFUSAL = "no way to show gifs here"
TEXT_ONLY = "no, just text"


class FakeChannel:
    id = 9
    name = "general"

    def history(self, limit, before=None):
        async def _empty():
            if False:
                yield None

        return _empty()


def mention(text, channel=None, author_id=10, name="Kōshin"):
    return SimpleNamespace(
        guild=SimpleNamespace(id=50, name="Seclude", member_count=3),
        channel=channel or FakeChannel(),
        author=SimpleNamespace(id=author_id, display_name=name),
        content=f"<@999> {text}",
        attachments=[],
        embeds=[],
        reference=None,
        id=1,
        mentions=[],
    )


def hold_follow(text, channel=None, author_id=10, name="Kōshin", mid=2):
    """Nameless same-channel follow-up after a name-call hold."""
    return SimpleNamespace(
        guild=SimpleNamespace(id=50, name="Seclude", member_count=3),
        channel=channel or FakeChannel(),
        author=SimpleNamespace(id=author_id, display_name=name),
        content=text,
        attachments=[],
        embeds=[],
        reference=None,
        id=mid,
        mentions=[],
    )


CORRECTIONS = (
    "no how you really feel in that situation",
    "no not that",
    "no I meant how you feel",
    "that's not it",
    "thats not it",
)
WEATHER_FORECAST = {
    "status": "success",
    "used_search": True,
    "text": "tomorrow in the bay area: mostly cloudy, highs around 64, light rain late.",
    "sources": [
        {"title": "NWS", "url": "https://forecast.weather.gov/MapClick.php?lat=37.77&lon=-122.42"},
    ],
}


class FakeMediaResponse:
    def __init__(self, status, data=b"", json_data=None):
        self.status = status
        self._data = data
        self._json = json_data or {}
        self.headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def read(self):
        return self._data

    async def json(self):
        return self._json


class FakeGetSession:
    def __init__(self, response):
        self._response = response
        self.urls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, headers=None, timeout=None, params=None):
        self.urls.append(url)
        return self._response


class ChatMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db = main.CHAT_DB_FILE
        main.CHAT_DB_FILE = str(Path(self.temp_dir.name) / "chat_history.db")
        main.init_chat_db()
        main._COMMAND_RATE_LIMITS.clear()
        main._AI_RESEARCH_CACHE.clear()
        main._LIVE_RESEARCH_FAILURES.clear()
        main._WEATHER_LIVE_FAILURES.clear()
        main._open_conversations.clear()
        main._conversation_ladders.clear()
        main._conversation_media_kind.clear()

    def tearDown(self):
        main.CHAT_DB_FILE = self.original_db
        main._COMMAND_RATE_LIMITS.clear()
        main._AI_RESEARCH_CACHE.clear()
        main._LIVE_RESEARCH_FAILURES.clear()
        main._WEATHER_LIVE_FAILURES.clear()
        main._open_conversations.clear()
        main._conversation_ladders.clear()
        main._conversation_media_kind.clear()
        self.temp_dir.cleanup()

    def reply(self, text, research=None, candidates=None, downloaded=None):
        model = AsyncMock(return_value="should not speak")
        search = AsyncMock(
            return_value=candidates
            if candidates is not None
            else [{
                "url": "https://media.example/found.gif",
                "identities": ["test:1"],
            }]
        )
        if downloaded is None:
            downloaded = (b"GIF89a-clean", "grim.gif")
        download = AsyncMock(return_value=downloaded)
        if research is None:
            research = AsyncMock(return_value={"status": "empty"})

        async def run():
            with patch.dict(main.os.environ, {"XAI_API_KEY": "test-key"}), \
                    patch.object(
                        main.discord.Client,
                        "user",
                        new_callable=PropertyMock,
                        return_value=SimpleNamespace(id=999),
                    ), \
                    patch.object(
                        main.bot,
                        "get_channel",
                        return_value=SimpleNamespace(name="general"),
                    ), \
                    patch.object(main, "grok_research_query", research), \
                    patch.object(main, "_complete_contextual_reply", model), \
                    patch.object(main, "_search_chat_media_candidates", search), \
                    patch.object(main, "_download_cleaned_chat_media", download):
                return await main.generate_contextual_reply(mention(text))

        result = asyncio.run(run())
        return result, model, search

    def test_parse_gif_current_events_and_pictures(self):
        gif = main.parse_chat_media_request(GIF_ASK)
        self.assertEqual(gif["kind"], "gif")
        self.assertTrue(gif["current_events"])
        pictures = main.parse_chat_media_request(PICTURE_ASK)
        self.assertEqual(pictures["kind"], "image")
        self.assertTrue(pictures["capability"])
        miku = main.parse_chat_media_request(MIKU_PICTURE)
        self.assertEqual(miku["kind"], "image")
        self.assertIn("miku", miku["topic"])
        pasta = main.parse_chat_media_request(COPYPASTA_ASK)
        self.assertEqual(pasta["kind"], "image")
        self.assertIsNone(main.parse_chat_media_request(ASCII_ASK))
        self.assertIsNone(main.parse_chat_media_request("how we doing boss man"))
        of_gif = main.parse_chat_media_request("gif of cats")
        self.assertEqual(of_gif["kind"], "gif")
        self.assertIn("cats", of_gif["topic"])
        of_pic = main.parse_chat_media_request("pic of miku")
        self.assertEqual(of_pic["kind"], "image")
        meme = main.parse_chat_media_request("meme of stunned")
        self.assertEqual(meme["kind"], "image")

    def test_reactions_and_followups_are_not_media_asks(self):
        message = mention("LMAO wtf")
        main._conversation_media_kind[("9", "10")] = "gif"
        for line in (
            "LMAO wtf",
            "huh",
            "lmao",
            "damn",
            "wtf",
            "yeah",
            "ok",
            "miku",
            "that's wild",
            "wait what",
            *CORRECTIONS,
        ):
            self.assertIsNone(
                main.parse_chat_media_request(line, message),
                msg=line,
            )

    def test_corrections_are_not_media_asks_even_with_remembered_kind(self):
        message = hold_follow("no how you really feel in that situation")
        main._conversation_media_kind[("9", "10")] = "gif"
        for line in CORRECTIONS:
            folded = main._output_match_text(main._conversation_text(line))
            self.assertTrue(
                main._chat_media_correction_followup(folded),
                msg=line,
            )
            self.assertFalse(main._explicit_chat_media_ask(folded), msg=line)
            self.assertIsNone(
                main.parse_chat_media_request(line, message),
                msg=line,
            )
        of_gif = main.parse_chat_media_request(
            "show me a gif of no way home", message
        )
        self.assertEqual(of_gif["kind"], "gif")
        self.assertIn("home", of_gif["topic"])

    def test_reaction_followups_do_not_attach_media(self):
        main._conversation_media_kind[("9", "10")] = "gif"
        for line in ("LMAO wtf", "huh"):
            result, model, search = self.reply(line)
            search.assert_not_awaited()
            model.assert_awaited()
            self.assertIsNone(result.media)
            self.assertIsNone(result.media_url)
            self.assertNotEqual(result.text, main.CHAT_MEDIA_HERE["gif"])

    def test_correction_followups_do_not_attach_media(self):
        main._conversation_media_kind[("9", "10")] = "gif"
        for line in CORRECTIONS:
            result, model, search = self.reply(line)
            search.assert_not_awaited()
            model.assert_awaited()
            self.assertIsNone(result.media, msg=line)
            self.assertIsNone(result.media_url, msg=line)
            self.assertNotEqual(result.text, main.CHAT_MEDIA_HERE["gif"], msg=line)
            self.assertNotIn(("9", "10"), main._conversation_media_kind)

    def hold_sequence(self, steps, research=None, model_reply="that's the weather in my bones."):
        model = AsyncMock(return_value=model_reply)
        search = AsyncMock(return_value=[{
            "url": "https://media.example/found.gif",
            "identities": ["test:1"],
        }])
        download = AsyncMock(return_value=(b"GIF89a-clean", "grim.gif"))
        if research is None:
            research = AsyncMock(return_value=WEATHER_FORECAST)
        results = []
        channel = FakeChannel()

        async def run():
            with patch.object(
                main.discord.Client,
                "user",
                new_callable=PropertyMock,
                return_value=SimpleNamespace(id=999),
            ):
                opener = mention("yo grim how we doing my fam", channel=channel)
                self.assertTrue(
                    main.grim_chat_turn_action(opener, now=1_700_000_000.0)
                )
            with patch.dict(main.os.environ, {"XAI_API_KEY": "test-key"}), \
                    patch.object(
                        main.discord.Client,
                        "user",
                        new_callable=PropertyMock,
                        return_value=SimpleNamespace(id=999),
                    ), \
                    patch.object(
                        main.bot,
                        "get_channel",
                        return_value=SimpleNamespace(name="STAFF"),
                    ), \
                    patch.object(main, "grok_research_query", research), \
                    patch.object(main, "_complete_contextual_reply", model), \
                    patch.object(main, "_search_chat_media_candidates", search), \
                    patch.object(main, "_download_cleaned_chat_media", download):
                for idx, text in enumerate(steps, start=2):
                    follow = hold_follow(text, channel=channel, mid=idx)
                    self.assertTrue(
                        main.grim_chat_turn_action(
                            follow, now=1_700_000_000.0 + idx
                        ),
                        msg=text,
                    )
                    results.append(
                        await main.generate_contextual_reply(follow)
                    )
            return results

        replies = asyncio.run(run())
        return replies, model, search

    def test_screenshot_gif_correction_sequence(self):
        replies, model, search = self.hold_sequence((
            "whats the weather like tomorrow in the Bay Area?",
            "show me a gif of how we're gonna feel tomorrow",
            "no how you really feel in that situation",
            "send another gif",
            "gif of rain",
        ))
        weather, gif_ask, correction, another, gif_of = replies
        self.assertTrue(weather.text.startswith(WEATHER_FORECAST["text"]))
        self.assertIn("-# sources:", weather.text)
        self.assertIsNone(weather.media)
        self.assertEqual(gif_ask.media, b"GIF89a-clean")
        self.assertEqual(gif_ask.media_filename, "grim.gif")
        self.assertEqual(gif_ask.text, main.CHAT_MEDIA_HERE["gif"])
        search.assert_awaited()
        model.assert_awaited()
        self.assertIsNone(correction.media)
        self.assertIsNone(correction.media_url)
        self.assertNotEqual(correction.text, main.CHAT_MEDIA_HERE["gif"])
        self.assertEqual(correction.text, "that's the weather in my bones.")
        self.assertEqual(another.media, b"GIF89a-clean")
        self.assertEqual(another.media_filename, "grim.gif")
        self.assertEqual(gif_of.media, b"GIF89a-clean")
        self.assertGreaterEqual(search.await_count, 3)

    def test_gif_reaction_skips_correction_followups(self):
        folded = main._output_match_text(
            main._conversation_text("no how you really feel in that situation")
        )
        self.assertTrue(main._chat_media_correction_followup(folded))
        with patch.dict(
            main.os.environ,
            {"KLIPY_API_KEY": "k", "GIPHY_API_KEY": "g"},
        ):
            self.assertFalse(
                main._gif_reaction_is_allowed(
                    "no how you really feel in that situation",
                    "here.",
                    "9",
                )
            )

    def test_explicit_asks_still_send_media(self):
        gif_result, gif_model, gif_search = self.reply(
            "show me a gif of cats",
            downloaded=(b"GIF89a-clean", "grim.gif"),
        )
        gif_model.assert_not_awaited()
        gif_search.assert_awaited()
        self.assertEqual(gif_result.media, b"GIF89a-clean")
        self.assertEqual(gif_result.media_filename, "grim.gif")
        pic_result, pic_model, pic_search = self.reply(
            "send a picture of miku",
            downloaded=(b"PNG-miku", "grim.png"),
            candidates=[{
                "url": "https://media.example/miku.png",
                "identities": ["test:miku"],
            }],
        )
        pic_model.assert_not_awaited()
        pic_search.assert_awaited()
        self.assertEqual(pic_result.media, b"PNG-miku")
        self.assertEqual(pic_result.media_filename, "grim.png")

    def test_gif_ask_sends_a_mocked_gif(self):
        payload = b"GIF89a-clean"
        result, model, search = self.reply(
            "show me a gif of cats",
            downloaded=(payload, "grim.gif"),
        )
        model.assert_not_awaited()
        search.assert_awaited()
        self.assertEqual(result.media, payload)
        self.assertEqual(result.media_filename, "grim.gif")
        self.assertNotIn(REFUSAL, (result.text or "").casefold())
        self.assertFalse(result.media_filename.startswith("koshin"))

    def test_current_events_gif_does_not_use_the_old_refusal(self):
        research = AsyncMock(return_value={
            "status": "success",
            "used_search": True,
            "text": "as of nov 14, a ceasefire deal is the lead story.",
            "sources": [{"title": "Reuters", "url": "https://www.reuters.com/world/a"}],
        })
        result, model, search = self.reply(GIF_ASK, research=research)
        model.assert_not_awaited()
        self.assertIsNotNone(result.media)
        folded = (result.text or "").casefold()
        self.assertNotIn(REFUSAL, folded)
        self.assertNotIn("just text", folded)
        self.assertNotIn("ascii", folded)
        search.assert_awaited()
        query = search.await_args.args[2]
        self.assertTrue(query)
        self.assertNotEqual(query, "current events going on in the world today")

    def test_current_events_empty_search_uses_miss_line_not_refusal(self):
        result, model, search = self.reply(
            GIF_ASK,
            research=AsyncMock(return_value={"status": "empty"}),
            candidates=[],
        )
        model.assert_not_awaited()
        search.assert_not_awaited()
        self.assertIsNone(result.media)
        self.assertIsNone(result.media_url)
        first, sep, rest = (result.text or "").partition("\n")
        self.assertEqual(first, main.LIVE_SEARCH_ZERO_INFO)
        self.assertEqual(sep, "\n")
        folded = (result.text or "").casefold()
        self.assertNotIn(REFUSAL, folded)
        self.assertNotIn("just text", folded)
        self.assertNotIn("no way to show", folded)

    def test_empty_provider_does_not_claim_gifs_cannot_be_shown(self):
        result, model, search = self.reply(
            "show me a gif of cats",
            candidates=[],
            downloaded=None,
        )
        self.assertIsNone(result.media)
        folded = (result.text or "").casefold()
        self.assertEqual(result.text, main.CHAT_MEDIA_MISS["gif"])
        self.assertNotIn(REFUSAL, folded)
        self.assertNotIn("no way to show gifs", folded)
        self.assertNotIn("just text", folded)
        model.assert_not_awaited()
        search.assert_awaited()

    def test_picture_capability_sends_an_image_not_text_only(self):
        payload = b"PNG-clean"
        result, model, search = self.reply(
            PICTURE_ASK,
            downloaded=(payload, "grim.png"),
            candidates=[{
                "url": "https://media.example/found.png",
                "identities": ["test:img"],
            }],
        )
        model.assert_not_awaited()
        self.assertEqual(result.media, payload)
        self.assertEqual(result.media_filename, "grim.png")
        folded = (result.text or "").casefold()
        self.assertNotIn(TEXT_ONLY, folded)
        self.assertNotIn("just text", folded)
        self.assertNotIn("text art", folded)
        self.assertEqual(result.text, "yeah.")

    def test_miku_picture_is_a_file_not_ascii(self):
        payload = b"PNG-miku"
        result, model, _search = self.reply(
            MIKU_PICTURE,
            downloaded=(payload, "grim.png"),
            candidates=[{
                "url": "https://media.example/miku.png",
                "identities": ["test:miku"],
            }],
        )
        model.assert_not_awaited()
        self.assertEqual(result.media, payload)
        self.assertNotIn("ascii", (result.text or "").casefold())
        self.assertNotIn("@", result.text or "")
        self.assertTrue((result.media_filename or "").endswith(".png"))

    def test_copypasta_image_ask_is_not_ascii(self):
        parsed = main.parse_chat_media_request(COPYPASTA_ASK)
        self.assertEqual(parsed["kind"], "image")
        result, model, _search = self.reply(
            COPYPASTA_ASK,
            downloaded=(b"PNG-meme", "grim.png"),
            candidates=[{
                "url": "https://media.example/meme.png",
                "identities": ["test:meme"],
            }],
        )
        model.assert_not_awaited()
        self.assertEqual(result.media, b"PNG-meme")
        self.assertNotIn("ascii", (result.text or "").casefold())

    def test_download_strips_metadata_and_uses_neutral_filename(self):
        dirty = dirty_gif()
        session = FakeGetSession(FakeMediaResponse(200, dirty))

        async def run():
            return await main._download_cleaned_chat_media(
                session, "https://media.example/secret.gif"
            )

        cleaned, filename = asyncio.run(run())
        self.assertEqual(filename, "grim.gif")
        self.assertFalse(metadata_left(cleaned))
        self.assertNotIn(b"secret-comment", cleaned)
        self.assertNotIn("koshin", filename.casefold())

    def test_png_download_strips_and_stays_anonymous(self):
        dirty = dirty_png()
        session = FakeGetSession(FakeMediaResponse(200, dirty))
        cleaned, filename = asyncio.run(
            main._download_cleaned_chat_media(session, "https://media.example/x.png")
        )
        self.assertEqual(filename, "grim.png")
        self.assertFalse(metadata_left(cleaned))

    def test_send_attaches_the_gif_without_naming_the_invoker(self):
        payload = dirty_gif()
        cleaned, _ext = main.clean_image(payload)
        sent = SimpleNamespace(
            id=1,
            created_at=SimpleNamespace(timestamp=lambda: 0.0),
        )
        reply_fn = AsyncMock(return_value=sent)
        message = SimpleNamespace(
            content="<@999> show me a gif of cats",
            channel=SimpleNamespace(id=9, send=AsyncMock(return_value=sent)),
            guild=SimpleNamespace(id=50),
            author=SimpleNamespace(id=10, display_name="Kōshin"),
            reply=reply_fn,
        )
        with patch.object(main, "save_message_to_db"):
            asyncio.run(main._send_mention_reply(
                message,
                "here.",
                media=cleaned,
                media_filename="grim.gif",
            ))
        kwargs = reply_fn.await_args.kwargs
        attached = kwargs["file"]
        self.assertIsInstance(attached, discord.File)
        self.assertEqual(attached.filename, "grim.gif")
        self.assertEqual(kwargs["content"], "here.")
        self.assertEqual(kwargs["mention_author"], False)
        mentions = kwargs["allowed_mentions"]
        self.assertFalse(mentions.everyone)
        self.assertFalse(mentions.users)
        self.assertNotIn("koshin", str(kwargs).casefold())
        self.assertNotIn("Kōshin", attached.filename)

    def test_fail_send_path_never_uses_the_old_refusal_string(self):
        result, _model, _search = self.reply(
            "show me a gif of cats",
            candidates=[],
            downloaded=None,
        )
        sent = SimpleNamespace(
            id=1,
            created_at=SimpleNamespace(timestamp=lambda: 0.0),
        )
        reply_fn = AsyncMock(return_value=sent)
        message = SimpleNamespace(
            content="<@999> show me a gif of cats",
            channel=SimpleNamespace(id=9, send=AsyncMock(return_value=sent)),
            guild=SimpleNamespace(id=50),
            reply=reply_fn,
        )
        with patch.object(main, "_maybe_build_gif_reaction", AsyncMock(return_value=None)), \
                patch.object(main, "save_message_to_db"):
            asyncio.run(main._send_mention_reply(message, result.text))
        posted = reply_fn.await_args.args[0]
        folded = posted.casefold()
        self.assertNotIn(REFUSAL, folded)
        self.assertNotIn("no way to show gifs", folded)
        self.assertEqual(posted, main.CHAT_MEDIA_MISS["gif"])

    def test_prompt_forbids_text_only_and_ascii_standins(self):
        source = Path(main.__file__).read_text()
        self.assertIn("Never claim Discord cannot show GIFs or pictures", source)
        self.assertIn("never say you are text-only", source)
        self.assertIn("Never substitute ASCII", source)
        self.assertIn("correct a GIF or picture you just sent", source)
        self.assertNotIn(REFUSAL, source)

    def test_changelog_names_the_media_and_hold_fixes(self):
        changelog = Path(__file__).resolve().parents[1].joinpath("CHANGELOG.md").read_text()
        self.assertIn("GIF, picture, meme image, or copypasta image", changelog)
        self.assertIn("never substitutes ASCII", changelog)
        self.assertIn("tells him not to reply", changelog)
        self.assertNotIn("[REDACTED]", changelog.split("When someone asks Grim for a GIF")[1][:400])

    def test_changelog_names_the_explicit_ask_gate(self):
        changelog = Path(__file__).resolve().parents[1].joinpath("CHANGELOG.md").read_text()
        self.assertIn("only fires on an explicit ask", changelog)
        self.assertIn("`wtf`", changelog)
        self.assertIn("`huh`", changelog)
        self.assertIn("not a file", changelog)
        self.assertNotIn("[REDACTED]", changelog.split("Chat GIF and picture sending")[1][:400])

    def test_changelog_names_the_gif_correction_gate(self):
        changelog = Path(__file__).resolve().parents[1].joinpath("CHANGELOG.md").read_text()
        self.assertIn("no how you really feel in that situation", changelog)
        self.assertIn("no not that", changelog)
        self.assertIn("send another gif", changelog)
        self.assertIn("not another file", changelog)
        self.assertNotIn("[REDACTED]", changelog.split("After Grim sends a GIF")[1][:500])


if __name__ == "__main__":
    unittest.main()
