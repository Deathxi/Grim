import json
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
    HUD,
    TERMINAL,
    map_sketch,
    render_sketch,
    toggle_ui,
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

    def test_failed_move_keeps_the_prior_room_above_the_newest_line(self):
        open_session(self.root, self.player, "zork1")
        self.assertEqual(toggle_ui(self.root, self.player, "zork1").ui, HUD)
        for command in ("south", "east", "open window", "west"):
            command_session(self.root, self.player, "zork1", command)
        failed = command_session(self.root, self.player, "zork1", "north")
        self.assertEqual(failed.ui, HUD)
        self.assertEqual(failed.room, "Kitchen")
        self.assertIn("can't go that way", failed.text.lower())
        body = failed.description
        folded = body.lower()
        self.assertIn("you are in the kitchen of the white house", folded)
        self.assertIn("bottle", folded)
        self.assertIn("sack", folded)
        self.assertLess(folded.index("you are in the kitchen"), folded.index("can't go that way"))
        self.assertLess(body.index("SCORE"), folded.index("you are in the kitchen"))
        self.assertLess(body.index("MOVES"), folded.index("you are in the kitchen"))
        self.assertIn(failed.room.upper(), body)
        self.assertIn(f"SCORE {failed.score}/{GAMES['zork1'].max_score}", body)
        self.assertIn(f"MOVES {failed.moves}", body)

        arrived = command_session(self.root, self.player, "zork1", "west")
        self.assertEqual(arrived.room, "Living Room")
        arrived_body = arrived.description.lower()
        self.assertIn("living room", arrived_body)
        self.assertNotIn("you are in the kitchen", arrived_body)
        self.assertNotIn("can't go that way", arrived_body)
        self.assertNotIn("bottle", arrived_body)

    def test_hud_shows_latest_and_zork_ui_toggles_terminal(self):
        command_session(self.root, self.player, "zork1", "open mailbox")
        latest = command_session(self.root, self.player, "zork1", "north")
        self.assertEqual(latest.ui, TERMINAL)
        self.assertIn("> open mailbox", latest.description)

        hud = toggle_ui(self.root, self.player, "zork1")
        self.assertEqual(hud.ui, HUD)
        self.assertIn("boarded", hud.description.lower())
        self.assertNotIn("leaflet", hud.description.lower())
        self.assertNotIn("> open mailbox", hud.description)
        self.assertEqual(hud.title, hud.room.upper())
        self.assertIn(hud.room.upper(), hud.description)
        self.assertIn(f"SCORE {hud.score}/{GAMES['zork1'].max_score}", hud.description)
        self.assertIn(f"MOVES {hud.moves}", hud.description)

        terminal = toggle_ui(self.root, self.player, "zork1")
        self.assertEqual(terminal.ui, TERMINAL)
        self.assertIn("> open mailbox", terminal.description)
        self.assertIn("leaflet", terminal.description.lower())
        self.assertIn("boarded", terminal.description.lower())

        reset_runtime_cache()
        restored = open_session(self.root, self.player, "zork1")
        self.assertEqual(restored.ui, TERMINAL)
        self.assertIn("> open mailbox", restored.description)

        other = command_session(self.root, "999", "zork1", "open mailbox")
        other = command_session(self.root, "999", "zork1", "north")
        self.assertEqual(other.ui, TERMINAL)
        self.assertIn("> open mailbox", other.description)

        same_player_other_game = command_session(self.root, self.player, "zork2", "inventory")
        self.assertEqual(same_player_other_game.ui, TERMINAL)
        self.assertNotIn("> open mailbox", same_player_other_game.description)

    def test_old_save_and_fresh_game_render_through_the_bot_panel(self):
        from zork.ui import embed_for

        open_session(self.root, self.player, "zork1")
        self.assertEqual(toggle_ui(self.root, self.player, "zork1").ui, HUD)
        for command in ("south", "east", "open window", "west", "north"):
            played = command_session(self.root, self.player, "zork1", command)
        moves = played.moves
        score = played.score
        slot = self.root / "zork" / "saves" / self.player / "zork1.json"
        older = json.loads(slot.read_text())
        older.pop("place", None)
        older.pop("place_for", None)
        older.pop("ui_chosen", None)
        older["ui"] = "hud"
        slot.write_text(json.dumps(older))

        reset_runtime_cache()
        untouched = open_session(self.root, self.player, "zork1")
        self.assertEqual(untouched.ui, TERMINAL)
        self.assertEqual(untouched.moves, moves)
        self.assertEqual(untouched.score, score)

        chosen = dict(older)
        chosen["ui_chosen"] = True
        slot.write_text(json.dumps(chosen))
        reset_runtime_cache()
        restored = open_session(self.root, self.player, "zork1")
        self.assertEqual(restored.ui, HUD)
        self.assertEqual(restored.moves, moves)
        self.assertEqual(restored.score, score)
        panel = embed_for(restored).description
        folded = panel.lower()
        self.assertLess(panel.index("SCORE"), folded.index("you are in the kitchen of the white house"))
        self.assertLess(panel.index("MOVES"), folded.index("you are in the kitchen of the white house"))
        self.assertIn("bottle", folded)
        self.assertIn("sack", folded)
        self.assertLess(folded.index("you are in the kitchen of the white house"), folded.index("can't go that way"))
        self.assertEqual(embed_for(restored).title, "KITCHEN")

        taken = command_session(self.root, self.player, "zork1", "take bottle")
        self.assertEqual(taken.moves, moves + 1)
        self.assertIn("taken", taken.text.lower())
        taken_panel = embed_for(taken).description.lower()
        self.assertLess(taken_panel.index("you are in the kitchen of the white house"), taken_panel.index("taken"))

        fresh = open_session(self.root, "42", "zork1")
        self.assertEqual(fresh.ui, TERMINAL)
        self.assertEqual(fresh.moves, 0)
        chosen_hud = toggle_ui(self.root, "42", "zork1")
        self.assertEqual(chosen_hud.ui, HUD)
        self.assertEqual(chosen_hud.moves, 0)
        opened = command_session(self.root, "42", "zork1", "open mailbox")
        self.assertEqual(opened.moves, 1)
        fresh_panel = embed_for(opened).description
        fresh_folded = fresh_panel.lower()
        self.assertIn("small mailbox", fresh_folded)
        self.assertIn("leaflet", fresh_folded)
        self.assertLess(fresh_panel.index("SCORE"), fresh_folded.index("small mailbox"))
        self.assertLess(fresh_folded.index("small mailbox"), fresh_folded.index("leaflet"))
        self.assertEqual(embed_for(opened).title, opened.room.upper())

    def test_sketch_draws_visited_up_and_leaves_unvisited_blank(self):
        sketch = render_sketch(
            "Kitchen",
            {"s": "Living Room", "u": "Attic"},
        )
        self.assertIn("[Kitchen]", sketch)
        self.assertIn("Living Room", sketch)
        self.assertIn("Up: Attic", sketch)
        self.assertNotIn("Down:", sketch)
        self.assertNotIn(" -- ", sketch.split("\n")[0])
        self.assertLess(sketch.index("[Kitchen]"), sketch.index("Living Room"))
        self.assertLess(sketch.index("Living Room"), sketch.index("Up: Attic"))

    def test_map_shows_only_walked_neighbors_in_all_three_stories(self):
        stories = (
            ("zork1", "south", ("south", "east")),
            ("zork2", "south", ("south", "south")),
            ("zork3", "south", ("take lantern", "light lamp", "south", "west")),
        )
        sketches = {}
        for game_id, step, further in stories:
            open_session(self.root, self.player, game_id)
            if game_id == "zork1":
                stayed = command_session(self.root, self.player, game_id, "east")
                self.assertEqual(stayed.room, "West of House")
            walked = command_session(self.root, self.player, game_id, step)
            moves = walked.moves
            score = walked.score
            sketch = map_sketch(self.root, self.player, game_id)
            sketches[game_id] = sketch
            self.assertIn(f"[{walked.room}]", sketch)
            origin = open_session(self.root, f"origin-{game_id}", game_id).room
            self.assertIn(origin, sketch)
            self.assertNotEqual(origin, walked.room)
            other = open_session(self.root, f"other-{game_id}", game_id)
            for command in further:
                other = command_session(self.root, f"other-{game_id}", game_id, command)
            self.assertNotIn(other.room, sketch)
            self.assertNotIn(other.room, map_sketch(self.root, self.player, game_id))
            again = open_session(self.root, self.player, game_id)
            self.assertEqual(again.moves, moves)
            self.assertEqual(again.score, score)
            self.assertEqual(again.room, walked.room)
            reset_runtime_cache()
            self.assertEqual(map_sketch(self.root, self.player, game_id), sketch)
            if game_id == "zork1":
                toggle_ui(self.root, self.player, game_id)
                self.assertEqual(map_sketch(self.root, self.player, game_id), sketch)
        self.assertIn("[South of House]", sketches["zork1"])
        self.assertIn("West of House", sketches["zork1"])
        self.assertNotIn("North of House", sketches["zork1"])
        self.assertIn("[Narrow Tunnel]", sketches["zork2"])
        self.assertIn("Inside the Barrow", sketches["zork2"])
        self.assertIn("[Junction]", sketches["zork3"])
        self.assertIn("Endless Stair", sketches["zork3"])

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

    def test_panel_lists_only_objects_named_in_the_room(self):
        import asyncio

        import discord

        from zork import ui

        house = open_session(self.root, self.player, "zork1")
        self.assertEqual(map_sketch(self.root, self.player, "zork1").strip(), "[West of House]")
        house_view = ui.panel_view(self.player, "zork1", house)
        labels = [child.label for child in house_view.children if isinstance(child, discord.ui.Button)]
        self.assertEqual(
            labels,
            ["N", "S", "E", "W", "U", "D", "Look", "Inventory", "Type", "Take", "Open", "Examine", "Restart", "Dismiss"],
        )
        house_menu = next(child for child in house_view.children if isinstance(child, discord.ui.Select))
        house_names = [option.value for option in house_menu.options]
        self.assertTrue(any("mailbox" in name.casefold() for name in house_names))
        self.assertFalse(any("bottle" in name.casefold() for name in house_names))
        self.assertFalse(any("leaflet" in name.casefold() for name in house_names))
        for name in house_names:
            self.assertIn(name.casefold(), house.place.casefold())

        mailbox = next(name for name in house_names if "mailbox" in name.casefold())
        house_menu._values = [mailbox]
        saved = dict(ui._config)
        ui._config["root"] = self.root
        ui._config["allow_move"] = lambda *_args, **_kwargs: True
        ui._PICKED.clear()

        class Response:
            def __init__(self):
                self.done = False
                self.sent = None

            def is_done(self):
                return self.done

            async def send_message(self, *args, **kwargs):
                self.done = True
                self.sent = kwargs.get("content", args[0] if args else "")

            async def defer(self, **kwargs):
                self.done = True

            async def edit_message(self, **kwargs):
                self.done = True
                self.edited = kwargs

        class Followup:
            def __init__(self):
                self.sent = []

            async def send(self, *args, **kwargs):
                self.sent.append(kwargs)

        class Interaction:
            def __init__(self):
                self.user = type("User", (), {"id": self_player})()
                self.guild_id = 1
                self.response = Response()
                self.followup = Followup()
                self.edited = None

            async def edit_original_response(self, **kwargs):
                self.edited = kwargs

        self_player = self.player
        try:
            missing = Interaction()
            take = next(child for child in house_view.children if child.label == "Take")
            asyncio.run(take.callback(missing))
            self.assertIn("Pick an object", missing.response.sent)

            picked = Interaction()
            asyncio.run(house_menu.callback(picked))
            self.assertEqual(ui._PICKED[(str(self.player), "zork1")], mailbox)
            examined = Interaction()
            examine = next(child for child in house_view.children if child.label == "Examine")
            asyncio.run(examine.callback(examined))
            self.assertIn("mailbox", examined.edited["embed"].description.casefold())
            opened = Interaction()
            open_button = next(child for child in house_view.children if child.label == "Open")
            asyncio.run(open_button.callback(opened))
            self.assertIn("leaflet", opened.edited["embed"].description.casefold())
            refreshed = next(
                child for child in opened.edited["view"].children if isinstance(child, discord.ui.Select)
            )
            self.assertFalse(any("leaflet" in option.value.casefold() for option in refreshed.options))
            taken = Interaction()
            asyncio.run(take.callback(taken))
            self.assertIn("anchored", taken.edited["embed"].description.casefold())
        finally:
            ui._config.clear()
            ui._config.update(saved)
            ui._PICKED.clear()

        cook = "424242"
        kitchen = open_session(self.root, cook, "zork1")
        for command in ("south", "east", "open window", "west"):
            kitchen = command_session(self.root, cook, "zork1", command)
        self.assertEqual(kitchen.room, "Kitchen")
        kitchen_view = ui.panel_view(cook, "zork1", kitchen)
        kitchen_labels = [child.label for child in kitchen_view.children if isinstance(child, discord.ui.Button)]
        self.assertEqual(kitchen_labels, labels)
        barrow = open_session(self.root, cook, "zork2")
        barrow_labels = [
            child.label
            for child in ui.panel_view(cook, "zork2", barrow).children
            if isinstance(child, discord.ui.Button)
        ]
        self.assertEqual(barrow_labels, labels)
        stair = open_session(self.root, cook, "zork3")
        stair_labels = [
            child.label
            for child in ui.panel_view(cook, "zork3", stair).children
            if isinstance(child, discord.ui.Button)
        ]
        self.assertEqual(stair_labels, labels)
        menu = next(child for child in kitchen_view.children if isinstance(child, discord.ui.Select))
        names = [option.value for option in menu.options]
        self.assertIn("glass bottle", names)
        self.assertIn("brown sack", names)
        self.assertNotIn("leaflet", [name.casefold() for name in names])
        self.assertLessEqual(len(names), 8)
        for name in names:
            self.assertIn(name.casefold(), kitchen.place.casefold())
        bottle = next(child for child in kitchen_view.children if isinstance(child, discord.ui.Select))
        bottle._values = ["glass bottle"]
        ui._config["root"] = self.root
        ui._config["allow_move"] = lambda *_args, **_kwargs: True
        try:
            choose = Interaction()
            asyncio.run(bottle.callback(choose))
            grabbed = Interaction()
            kitchen_take = next(child for child in kitchen_view.children if child.label == "Take")
            asyncio.run(kitchen_take.callback(grabbed))
            self.assertIn("taken", grabbed.edited["embed"].description.casefold())
            held = command_session(self.root, cook, "zork1", "inventory")
            self.assertIn("glass bottle", held.text.casefold())
        finally:
            ui._config.clear()
            ui._config.update(saved)
            ui._PICKED.clear()

    def test_zork_opens_a_private_panel_and_types_through_a_modal(self):
        import asyncio
        from types import SimpleNamespace

        import discord

        from zork import ui

        saved = dict(ui._config)
        ui._config["root"] = self.root
        ui._config["footer"] = lambda: "Powered by XSI • test"
        ui._config["allow_open"] = None
        ui._config["allow_move"] = lambda *_args, **_kwargs: True
        ui._PICKED.clear()
        threaded = {"called": False}

        class Channel:
            id = 42
            type = discord.ChannelType.text

            async def create_thread(self, **kwargs):
                threaded["called"] = True
                raise AssertionError("a private panel does not open a thread")

        class Response:
            def __init__(self):
                self.done = False
                self.sent = None
                self.modal = None

            def is_done(self):
                return self.done

            async def send_message(self, *args, **kwargs):
                self.done = True
                self.sent = kwargs.get("content", args[0] if args else "")
                self.kwargs = kwargs

            async def defer(self, **kwargs):
                self.done = True
                self.kwargs = kwargs

            async def send_modal(self, modal):
                self.done = True
                self.modal = modal

            async def edit_message(self, **kwargs):
                self.done = True
                self.edited = kwargs

        class Followup:
            def __init__(self):
                self.sent = []

            async def send(self, *args, **kwargs):
                self.sent.append((args, kwargs))

        class Interaction:
            def __init__(self):
                self.user = SimpleNamespace(id=self_player, name="death")
                self.guild = SimpleNamespace(id=7)
                self.guild_id = 7
                self.channel = Channel()
                self.response = Response()
                self.followup = Followup()
                self.edited = None

            async def edit_original_response(self, **kwargs):
                self.edited = kwargs

        self_player = self.player
        try:
            opening = Interaction()
            asyncio.run(ui._command(opening, "zork1"))
            self.assertFalse(threaded["called"])
            self.assertTrue(opening.response.kwargs.get("ephemeral"))
            panel = opening.followup.sent[-1][1]
            self.assertTrue(panel.get("ephemeral"))
            self.assertIn(
                "N",
                [child.label for child in panel["view"].children if isinstance(child, discord.ui.Button)],
            )
            from zork.play import active_game

            self.assertEqual(active_game(self.root, 7, self.player), "zork1")

            typed = Interaction()
            type_button = next(child for child in panel["view"].children if child.label == "Type")
            asyncio.run(type_button.callback(typed))
            modal = typed.response.modal
            self.assertEqual(modal.command.max_length, 180)
            modal.command._value = "east"
            submitted = Interaction()
            asyncio.run(modal.on_submit(submitted))
            self.assertIn("boarded", submitted.edited["embed"].description.casefold())
            self.assertEqual(map_sketch(self.root, self.player, "zork1").strip(), "[West of House]")

            command_session(self.root, self.player, "zork1", "south")
            sketch = Interaction()
            asyncio.run(ui._map(sketch))
            self.assertTrue(sketch.response.kwargs.get("ephemeral"))
            self.assertIn("[South of House]", sketch.response.sent)
            self.assertNotIn("Kitchen", sketch.response.sent)

            switched = Interaction()
            asyncio.run(ui._ui(switched))
            self.assertEqual(switched.followup.sent[-1][0][0], "HUD")
            saves = list((self.root / "zork" / "saves").rglob("*.json"))
            self.assertEqual(len(saves), 1)

            dismissed = Interaction()
            dismiss = next(child for child in panel["view"].children if child.label == "Dismiss")
            asyncio.run(dismiss.callback(dismissed))
            self.assertEqual(dismissed.response.edited["content"], "Dismissed.")
            self.assertIsNone(dismissed.response.edited["view"])
        finally:
            ui._config.clear()
            ui._config.update(saved)
            ui._PICKED.clear()


class ZorkCommandTests(unittest.TestCase):
    def test_slash_command_offers_all_three_and_uses_the_xsi_footer(self):
        import main
        from zork import ui
        from zork.play import Turn

        names = {command.name for command in main.bot.tree.get_commands()}
        self.assertIn("zork", names)
        self.assertIn("zork-ui", names)
        self.assertIn("zork-map", names)
        self.assertNotIn("zork-move-history", names)
        mapped = next(command for command in main.bot.tree.get_commands() if command.name == "zork-map")
        self.assertEqual(len(mapped.parameters), 0)
        ui_command = next(command for command in main.bot.tree.get_commands() if command.name == "zork-ui")
        self.assertEqual(len(ui_command.parameters), 0)
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
