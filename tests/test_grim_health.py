import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

import grim_health
import main
from grim_health import (
    CHECKS,
    HealthBoard,
    build_health_embed,
    classify_security_rows,
    score_health,
)

NOW = 100_000.0


def ev(event, kind="reply", ago=60, **extra):
    return {"at": NOW - ago, "event": event, "kind": kind, **extra}


def score(events=(), security=None, **kwargs):
    return score_health(list(events), security, now=NOW, window_seconds=6 * 3600, **kwargs)


class ScoringTests(unittest.TestCase):
    def test_quiet_system_is_defcon_5_and_everything_healthy(self):
        health = score()
        self.assertEqual(health.level, 5)
        self.assertEqual(health.degraded, [])
        self.assertEqual(len(health.healthy), len(CHECKS))
        self.assertIsNone(health.last_event)

    def test_level_is_the_worst_check(self):
        health = score([ev("timeout")] * 2 + [ev("recovery_failed")] * 2)
        self.assertEqual(health.level, 2)
        self.assertEqual([c.key for c in health.degraded], ["recovery", "stuck"])

    def test_threshold_ladders(self):
        cases = [
            ([ev("timeout")] * 1, 4), ([ev("timeout")] * 4, 3), ([ev("timeout")] * 10, 2),
            ([ev("tool_loop")] * 3, 3), ([ev("reply_chain")] * 6, 2),
            ([ev("recovery_failed")], 3), ([ev("recovery_failed")] * 5, 1),
            ([ev("auditor_error", "hook")], 3), ([ev("auditor_error", "hook")] * 5, 1),
            ([ev("intervention_suppressed", "cooldown")], 4),
        ]
        for events, level in cases:
            self.assertEqual(score(events).level, level, (events[0]["event"], len(events)))

    def test_security_signals_lower_the_level(self):
        self.assertEqual(score(security={"strip_failed": (1, NOW)}).level, 2)
        self.assertEqual(score(security={"strip_failed": (3, NOW)}).level, 1)
        self.assertEqual(score(security={"search_failed": (4, NOW)}).level, 3)
        self.assertEqual(score(security={"denied": (4, NOW)}).level, 4)
        self.assertEqual(score(security={"denied": (30, NOW)}).level, 2)
        self.assertEqual(score(security={"rate_limited": (15, NOW)}).level, 4)
        self.assertEqual(score(security={"command_failed": (10, NOW)}).level, 2)

    def test_style_and_recoveries_never_lower_the_level(self):
        health = score([ev("style", "dash")] * 80 + [ev("recovered")] * 40 + [ev("repeat_subject")] * 40)
        self.assertEqual(health.level, 5)
        self.assertEqual(dict(health.info), {"recovered": 40, "repeat subject": 40, "voice slips": 80})

    def test_old_events_age_out_of_the_window(self):
        health = score([ev("recovery_failed", ago=7 * 3600)] * 6)
        self.assertEqual(health.level, 5)

    def test_last_event_is_the_newest_across_sources(self):
        health = score([ev("timeout", ago=600)], {"denied": (2, NOW - 30)})
        self.assertEqual(health.last_event, ("permission denial", NOW - 30))
        health = score([ev("tool_loop", "live_search", ago=5)], {"denied": (2, NOW - 30)})
        self.assertEqual(health.last_event[0], "tool loop blocked · live_search")

    def test_connectivity_is_not_scored(self):
        keys = {spec.key for spec in CHECKS}
        self.assertFalse(keys & {"gateway", "latency", "connectivity", "outage"})
        self.assertEqual(score([ev("gateway_down", "x") for _ in range(20)]).level, 5)

    def test_window_env(self):
        with patch.dict(os.environ, {"GRIM_HEALTH_WINDOW_HOURS": "12"}):
            self.assertEqual(grim_health.health_window_seconds(), 12 * 3600)
        for bad in ("zero", "0", "500", "nan"):
            with patch.dict(os.environ, {"GRIM_HEALTH_WINDOW_HOURS": bad}):
                self.assertEqual(grim_health.health_window_seconds(), 6 * 3600)


class ClassifyTests(unittest.TestCase):
    def test_rows_map_to_security_categories(self):
        rows = [
            ("announce", "denied", 3, 10.0),
            ("grim_updates_manage", "denied", 2, 20.0),
            ("ai_web_research", "success", 9, 30.0),
            ("ai_web_research", "timeout", 2, 40.0),
            ("ai_web_research", "failed", 1, 41.0),
            ("ai_web_research", "rate_limited", 5, 42.0),
            ("external_ai", "rate_limited", 4, 43.0),
            ("image_strip", "failed", 2, 50.0),
            ("newsfeed_create", "failed", 1, 60.0),
            ("say", "posted", 7, 70.0),
            ("say", "previewed", 7, 71.0),
            ("bad row",),
        ]
        self.assertEqual(
            classify_security_rows(rows),
            {
                "denied": (5, 20.0),
                "search_failed": (3, 41.0),
                "rate_limited": (9, 43.0),
                "strip_failed": (2, 50.0),
                "command_failed": (1, 60.0),
            },
        )


class EmbedTests(unittest.TestCase):
    def fields(self, embed):
        return {field.name: field.value for field in embed.fields}

    def test_healthy_board(self):
        embed = build_health_embed(score(), "Powered by XSI • v")
        self.assertEqual(embed.title, "Grim · Health · DEFCON 5 · FADE OUT")
        self.assertIn("All checks healthy", embed.description)
        fields = self.fields(embed)
        self.assertEqual(
            list(fields),
            ["Healthy", "Degraded", "Counts · last 6h", "Last Event", "Quick Fix"],
        )
        self.assertEqual(fields["Degraded"], "none")
        self.assertEqual(fields["Counts · last 6h"], "none")
        self.assertEqual(fields["Quick Fix"], "None needed.")
        self.assertEqual(embed.footer.text, "Powered by XSI • v")
        self.assertEqual(embed.color, discord.Color.from_rgb(18, 18, 18))

    def test_degraded_board_has_overview_fields(self):
        health = score([ev("tool_loop", "live_search", ago=90)] * 3 + [ev("style", "dash")], {"denied": (5, NOW - 10)})
        embed = build_health_embed(health, "f")
        fields = self.fields(embed)
        self.assertEqual(embed.title, "Grim · Health · DEFCON 3 · ROUND HOUSE")
        self.assertIn("2 of 10 checks degraded. Worst: Loops.", embed.description)
        self.assertIn("`D3` Loops · `x3` <t:", fields["Degraded"])
        self.assertIn("`D4` Permission denials · `x5`", fields["Degraded"])
        self.assertNotIn("Loops", fields["Healthy"])
        self.assertIn("Stuck calls", fields["Healthy"])
        self.assertIn("loops 3", fields["Counts · last 6h"])
        self.assertIn("voice slips 1", fields["Counts · last 6h"])
        self.assertIn("same search or check keeps firing", fields["Quick Fix"])
        self.assertIn("<t:", fields["Last Event"])

    def test_board_text_comes_only_from_fixed_labels_and_counts(self):
        secret = {"at": NOW, "event": "timeout", "kind": "reply", "user": "alice", "text": "hunter2"}
        embed = build_health_embed(score([secret]), "f")
        dumped = repr(embed.to_dict())
        for leaked in ("alice", "hunter2"):
            self.assertNotIn(leaked, dumped)


class BoardTests(unittest.IsolatedAsyncioTestCase):
    def make(self, levels, **kwargs):
        """levels: a list the test mutates; compute scores whatever is at the end."""
        delivered = []

        async def compute():
            return levels[-1]

        async def deliver(health, messages, repost):
            delivered.append((health.level, dict(messages), repost))
            return {"55": f"m{len(delivered)}"}

        clock = SimpleNamespace(now=0.0)
        board = HealthBoard(
            compute, deliver, gaps={"urgent": 60, "elevated": 300, "routine": 900},
            debounce=0, clock=lambda: clock.now, log=lambda line: None, **kwargs,
        )
        return board, delivered, clock

    @staticmethod
    def health(level, count=1):
        events = {
            5: [], 4: [ev("timeout")] * count, 3: [ev("recovery_failed")], 2: [ev("recovery_failed")] * 2,
            1: [ev("recovery_failed")] * 5,
        }[level]
        return score(events)

    async def test_first_post_then_edit_in_place(self):
        levels = [self.health(4)]
        board, delivered, clock = self.make(levels)
        self.assertTrue(await board.refresh())
        levels.append(self.health(5))
        clock.now += 1000
        self.assertTrue(await board.refresh())
        self.assertEqual([(d[0], d[1], d[2]) for d in delivered], [(4, {}, False), (5, {"55": "m1"}, False)])

    async def test_unchanged_board_is_not_touched(self):
        levels = [self.health(4)]
        board, delivered, clock = self.make(levels)
        await board.refresh()
        clock.now += 5000
        self.assertFalse(await board.refresh())
        self.assertEqual(len(delivered), 1)

    async def test_changes_inside_the_gap_wait_instead_of_spamming(self):
        levels = [self.health(4, 1)]
        board, delivered, clock = self.make(levels)
        await board.refresh()
        levels.append(self.health(4, 2))
        clock.now += 30
        self.assertFalse(await board.refresh())
        self.assertEqual(len(delivered), 1)
        clock.now += 1000
        self.assertTrue(await board.refresh())
        self.assertEqual(len(delivered), 2)

    async def test_deferred_update_arrives_on_its_own(self):
        levels = [self.health(4, 1)]
        board, delivered, clock = self.make(levels)
        await board.refresh()
        levels.append(self.health(4, 2))
        clock.now += 899.99
        await board.refresh()
        clock.now += 1
        await asyncio.sleep(0.05)
        self.assertEqual(len(delivered), 2)

    async def test_drop_to_defcon_2_reposts_and_replaces_the_old_board(self):
        levels = [self.health(4)]
        board, delivered, clock = self.make(levels)
        await board.refresh()
        levels.append(self.health(2))
        clock.now += 100
        await board.refresh()
        self.assertEqual(delivered[-1], (2, {"55": "m1"}, True))

    async def test_repost_has_a_cooldown_so_flapping_edits_instead(self):
        levels = [self.health(4)]
        board, delivered, clock = self.make(levels)
        await board.refresh()
        levels.append(self.health(2))
        clock.now += 100
        await board.refresh()
        levels.append(self.health(4))
        clock.now += 1000
        await board.refresh()
        levels.append(self.health(1))
        clock.now += 200
        await board.refresh()
        self.assertEqual([d[2] for d in delivered], [False, True, False, False])

    async def test_state_survives_a_restart_without_a_duplicate_post(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "health_dashboard.json")

            def save(state):
                with open(path, "w") as handle:
                    json.dump(state, handle)

            def load():
                with open(path) as handle:
                    return json.load(handle)

            levels = [self.health(3)]
            board, delivered, _ = self.make(levels, save=save)
            await board.refresh()
            self.assertEqual(set(load()), {"signature", "level", "messages"})
            again, delivered_again, _ = self.make(levels, load=load)
            self.assertEqual(again.state["messages"], {"55": "m1"})
            self.assertFalse(await again.refresh())
            self.assertEqual(delivered_again, [])

    async def test_failures_never_raise_and_retry_later(self):
        levels = [self.health(3)]
        calls = []

        async def deliver(health, messages, repost):
            calls.append(1)
            return None

        board = HealthBoard(
            AsyncMock(side_effect=lambda: levels[-1]), deliver, debounce=0, log=lambda line: None
        )
        self.assertFalse(await board.refresh())
        self.assertEqual(board.state["signature"], "")
        self.assertFalse(await board.refresh())
        self.assertEqual(len(calls), 2)

        broken = HealthBoard(AsyncMock(side_effect=RuntimeError("db down")), deliver, log=lambda line: None)
        self.assertFalse(await broken.refresh())

        bad_save = HealthBoard(
            AsyncMock(side_effect=lambda: levels[-1]),
            AsyncMock(return_value={"55": "m"}),
            save=lambda state: 1 / 0,
            log=lambda line: None,
        )
        self.assertTrue(await bad_save.refresh())

    def test_request_refresh_without_a_loop_is_harmless(self):
        board = HealthBoard(AsyncMock(), AsyncMock(), log=lambda line: None)
        board.request_refresh()


class MainWiringTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.embed_health = score([ev("timeout")])

    def channel(self, edit=None):
        partial = SimpleNamespace(edit=edit or AsyncMock(), delete=AsyncMock())
        return SimpleNamespace(
            get_partial_message=lambda message_id: partial,
            send=AsyncMock(return_value=SimpleNamespace(id=900)),
            partial=partial,
        )

    async def deliver(self, channel, messages, repost=False):
        with patch.dict(main.updates_channels, {"1": {"channel_id": "55"}}, clear=True), patch.object(
            main.bot, "fetch_channel", AsyncMock(return_value=channel)
        ):
            return await main.deliver_health_board(self.embed_health, messages, repost)

    async def test_first_delivery_posts_a_plain_embed(self):
        channel = self.channel()
        self.assertEqual(await self.deliver(channel, {}), {"55": "900"})
        kwargs = channel.send.await_args.kwargs
        self.assertEqual(set(kwargs), {"embed"})
        self.assertTrue(kwargs["embed"].title.startswith("Grim · Health · DEFCON"))

    async def test_later_delivery_edits_the_same_message(self):
        channel = self.channel()
        self.assertEqual(await self.deliver(channel, {"55": "777"}), {"55": "777"})
        channel.partial.edit.assert_awaited_once()
        channel.send.assert_not_awaited()

    async def test_deleted_board_is_replaced(self):
        gone = AsyncMock(side_effect=discord.NotFound(SimpleNamespace(status=404, reason="x"), "gone"))
        channel = self.channel(edit=gone)
        self.assertEqual(await self.deliver(channel, {"55": "777"}), {"55": "900"})
        channel.send.assert_awaited_once()
        channel.partial.delete.assert_not_awaited()

    async def test_repost_deletes_the_old_board_after_posting(self):
        channel = self.channel()
        self.assertEqual(await self.deliver(channel, {"55": "777"}, repost=True), {"55": "900"})
        channel.send.assert_awaited_once()
        channel.partial.delete.assert_awaited_once()
        channel.partial.edit.assert_not_awaited()

    async def test_no_updates_channel_means_no_delivery(self):
        with patch.dict(main.updates_channels, {}, clear=True):
            self.assertIsNone(await main.deliver_health_board(self.embed_health, {}, False))

    async def test_unreachable_channel_keeps_the_old_message_id(self):
        with patch.dict(main.updates_channels, {"1": {"channel_id": "55"}}, clear=True), patch.object(
            main.bot, "fetch_channel", AsyncMock(side_effect=RuntimeError("no access"))
        ):
            result = await main.deliver_health_board(self.embed_health, {"55": "777"}, False)
        self.assertEqual(result, {"55": "777"})

    async def test_compute_health_reads_security_events_and_auditor_events(self):
        auditor = main.ReplyAuditor(main.AuditorConfig(), log=lambda line: None)
        auditor.record("recovery_failed", "reply")
        with patch.object(main, "reply_auditor", auditor), patch.object(
            main, "_health_security_rows", return_value=[("say", "denied", 12, 5.0)]
        ):
            health = await main.compute_health()
        self.assertEqual(health.level, 3)
        self.assertEqual({c.key for c in health.degraded}, {"recovery", "denials"})

    async def test_compute_health_survives_an_unreadable_database(self):
        auditor = main.ReplyAuditor(main.AuditorConfig(), log=lambda line: None)
        with patch.object(main, "reply_auditor", auditor), patch.object(
            main, "_health_security_rows", side_effect=sqlite3.OperationalError("locked")
        ):
            self.assertEqual((await main.compute_health()).level, 5)

    def test_security_query_matches_the_audit_table(self):
        with tempfile.TemporaryDirectory() as folder:
            database = os.path.join(folder, "chat.db")
            with patch.object(main, "CHAT_DB_FILE", database):
                main.init_chat_db()
                main.record_security_event(None, "image_strip", "failed")
                main.record_security_event(None, "announce", "denied", {"reason": "x"})
                rows = main._health_security_rows(0)
        self.assertEqual(
            classify_security_rows(rows)["strip_failed"][0], 1
        )
        self.assertEqual(classify_security_rows(rows)["denied"][0], 1)

    def test_state_and_events_live_under_grim_data(self):
        self.assertEqual(main.HEALTH_BOARD_FILE, os.path.join(main.DATA_DIR, "health_dashboard.json"))
        self.assertEqual(main.AUDITOR_EVENTS_FILE, os.path.join(main.DATA_DIR, "auditor_events.json"))

    def test_refresh_task_is_wired_into_the_health_monitor(self):
        import inspect

        self.assertIn("health_board_refresh", inspect.getsource(main.health_monitor.coro))
        self.assertIn("health_board_refresh", inspect.getsource(main.on_ready))

    async def test_failed_image_strip_is_recorded_as_a_security_event(self):
        attachment = SimpleNamespace(size=10, read=AsyncMock(return_value=b"x"))
        with patch.object(main, "_sniff_relay_image", return_value="PNG"), patch.object(
            main, "clean_image_report", side_effect=main.ImageCleanError("boom")
        ), patch.object(main, "record_security_event") as record:
            with self.assertRaises(main.RelayImageError):
                await main._read_relay_image(attachment, "say", 1000)
        record.assert_called_once_with(None, "image_strip", "failed")


if __name__ == "__main__":
    unittest.main()
