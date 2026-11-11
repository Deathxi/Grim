import tempfile
import unittest
from pathlib import Path

from zork.machine import encode_word
from zork.play import (
    DEATH_LINES,
    GAMES,
    choose_aside,
    command_session,
    load_story,
    open_session,
    render_panel,
    reset_runtime_cache,
    restart_session,
    story_hash,
)


class ZorkInterpreterTests(unittest.TestCase):
    def setUp(self):
        reset_runtime_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.player = "235194449573969920"

    def tearDown(self):
        reset_runtime_cache()
        self.tmp.cleanup()

    def test_dictionary_encoding_and_official_story_files(self):
        self.assertEqual(encode_word("i").hex(), "38a594a5")
        license_text = Path(__file__).resolve().parents[1].joinpath("zork/stories/LICENSE").read_text()
        self.assertIn("MIT License", license_text)
        self.assertIn("Copyright (c) 2025 Microsoft", license_text)
        expected = {
            "zork1": (119, b"880429"),
            "zork2": (63, b"860811"),
            "zork3": (25, b"860811"),
        }
        for game_id, (release, serial) in expected.items():
            story = load_story(game_id)
            self.assertEqual(story[0], 3)
            self.assertEqual(int.from_bytes(story[2:4], "big"), release)
            self.assertEqual(story[0x12:0x18], serial)
            self.assertEqual(len(story_hash(game_id)), 64)

    def test_panel_keeps_game_text_and_puts_the_aside_outside_it(self):
        title, status, description = render_panel(
            "ZORK I",
            "West of House",
            0,
            12,
            350,
            "You are standing in an open field.",
            "The house wins this one.",
        )
        self.assertEqual(title, "WEST OF HOUSE")
        self.assertEqual(status, "ZORK I // WEST OF HOUSE // SCORE 0/350 // MOVES 12")
        self.assertIn(status, description)
        fenced = description.split("```")[1]
        self.assertIn("You are standing in an open field.", fenced)
        self.assertNotIn("The house wins this one.", fenced)
        self.assertTrue(description.rstrip().endswith("*The house wins this one.*"))
        self.assertIsNone(
            choose_aside(game_id="zork1", died=False, delta=9, max_score=350, key="small")
        )
        self.assertIsNotNone(
            choose_aside(game_id="zork1", died=False, delta=10, max_score=350, key="large")
        )
        death = choose_aside(game_id="zork1", died=True, delta=40, max_score=350, key="dead")
        self.assertIn(death, DEATH_LINES)

    def test_zork1_command_round_trip(self):
        opening = open_session(self.root, self.player, "zork1")
        self.assertTrue(opening.ok, opening.error)
        self.assertIn("white house", opening.transcript.lower())
        self.assertEqual(opening.status, "ZORK I // WEST OF HOUSE // SCORE 0/350 // MOVES 0")
        opened = command_session(self.root, self.player, "zork1", "open mailbox")
        self.assertTrue(opened.ok, opened.error)
        self.assertIn("leaflet", opened.text.lower())
        self.assertIn("SCORE 0/350", opened.status)
        self.assertGreaterEqual(opened.moves, 1)

    def test_one_autosave_per_player_per_game_restores(self):
        command_session(self.root, self.player, "zork1", "open mailbox")
        slot = self.root / "zork" / "saves" / self.player / "zork1.json"
        self.assertTrue(slot.is_file())
        self.assertTrue(str(slot.resolve()).startswith(str(self.root.resolve())))
        self.assertEqual(slot.stat().st_mode & 0o777, 0o600)
        before = slot.read_bytes()

        reset_runtime_cache()
        restored = command_session(self.root, self.player, "zork1", "take leaflet")
        self.assertIn("taken", restored.text.lower(), restored.text)
        self.assertIn("leaflet", command_session(self.root, self.player, "zork1", "inventory").text.lower())

        other = command_session(self.root, "999", "zork1", "take leaflet")
        self.assertNotIn("taken", other.text.lower(), other.text)

        zork2 = open_session(self.root, self.player, "zork2")
        self.assertIn("barrow", zork2.transcript.lower())
        self.assertTrue((self.root / "zork" / "saves" / self.player / "zork2.json").is_file())
        self.assertNotEqual(before, (self.root / "zork" / "saves" / self.player / "zork2.json").read_bytes())

        reset_runtime_cache()
        restart_session(self.root, self.player, "zork1")
        reset_runtime_cache()
        cleared = command_session(self.root, self.player, "zork1", "take leaflet")
        self.assertNotIn("taken", cleared.text.lower(), cleared.text)

    def test_zork2_and_zork3_accept_a_command(self):
        for game_id in ("zork2", "zork3"):
            opening = open_session(self.root, self.player, game_id)
            self.assertTrue(opening.ok, opening.error)
            self.assertTrue(opening.transcript.strip())
            self.assertIn(GAMES[game_id].label, opening.status)
            looked = command_session(self.root, self.player, game_id, "look")
            self.assertTrue(looked.ok, looked.error)
            self.assertTrue(looked.text.strip(), looked.text)
            self.assertNotEqual(looked.room, "")


class ZorkCommandTests(unittest.TestCase):
    def test_slash_command_offers_all_three_and_uses_the_xsi_footer(self):
        import main
        from zork import ui
        from zork.play import Turn

        names = {command.name for command in main.bot.tree.get_commands()}
        self.assertIn("zork", names)
        command = next(command for command in main.bot.tree.get_commands() if command.name == "zork")
        self.assertEqual(len(command.parameters), 1)
        parameter = command.parameters[0]
        self.assertEqual(parameter.name, "game")
        self.assertTrue(parameter.required)
        self.assertEqual({choice.value for choice in parameter.choices}, {"zork1", "zork2", "zork3"})

        title, status, description = render_panel(
            "ZORK I", "West of House", 0, 12, 350, "You are standing.", None
        )
        turn = Turn(
            ok=True,
            game_id="zork1",
            title=title,
            description=description,
            text="",
            transcript="",
            status=status,
            room="West of House",
            score=0,
            moves=12,
            aside=None,
            finished=False,
        )
        embed = ui.embed_for(turn)
        self.assertEqual(embed.title, "WEST OF HOUSE")
        self.assertIn("ZORK I // WEST OF HOUSE // SCORE 0/350 // MOVES 12", embed.description)
        self.assertEqual(embed.footer.text, main.xsi_footer())
        self.assertEqual([child.label for child in ui.ZorkView().children], ["Look", "Inventory", "Restart"])
