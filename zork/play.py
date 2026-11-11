"""Zork sessions, the one save slot per player per game, and the panel text.

Saves live under the runtime data directory the caller passes in. Nothing in
this module writes into the git checkout.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import zlib
from dataclasses import dataclass
from pathlib import Path

from zork.machine import ZMachine, ZMachineError


@dataclass(frozen=True)
class GameInfo:
    game_id: str
    title: str
    label: str
    filename: str
    max_score: int


GAMES = {
    "zork1": GameInfo("zork1", "Zork I", "ZORK I", "zork1.z3", 350),
    "zork2": GameInfo("zork2", "Zork II", "ZORK II", "zork2.z3", 400),
    "zork3": GameInfo("zork3", "Zork III", "ZORK III", "zork3.z3", 7),
}

DEATH_LINES = (
    "The house wins this one.",
    "You can leave. The score stays.",
    "That was the dark, collecting.",
    "Nothing down there was going to negotiate.",
    "West of the house was the kind part.",
)
SCORE_LINES = (
    "The dungeon gave something back.",
    "Hold that. It cost someone else more.",
    "A real gain. They are rare down here.",
    "The house noticed you taking that.",
)

_STORIES: dict[str, bytes] = {}
_HASHES: dict[str, str] = {}
_LIVE: dict[tuple[str, str, str], "Session"] = {}
_INDEXES: dict[str, dict] = {}
SAVE_VERSION = 1
TRANSCRIPT_LIMIT = 3400
HUD = "hud"
TERMINAL = "terminal"


@dataclass
class Turn:
    ok: bool
    game_id: str
    title: str
    description: str
    text: str
    transcript: str
    status: str
    room: str
    score: int
    moves: int
    aside: str | None
    finished: bool
    ui: str = TERMINAL
    place: str = ""
    error: str | None = None


class Session:
    def __init__(
        self,
        game_id: str,
        machine: ZMachine,
        transcript: str,
        latest: str | None = None,
        ui: str = TERMINAL,
        place: str | None = None,
        ui_chosen: bool = False,
    ):
        self.game_id = game_id
        self.machine = machine
        self.transcript = transcript.strip("\n")
        self.latest = (self.transcript if latest is None else latest).strip("\n")
        self.ui = _ui_mode(ui) if ui_chosen else TERMINAL
        self.ui_chosen = bool(ui_chosen)
        self.place = _game_text(place or "")
        self.place_for = ""
        self.map: dict[str, dict] = {}
        self.room = machine.room
        self.score = machine.score
        self.moves = machine.moves

    @classmethod
    def start(cls, game_id: str) -> Session:
        machine = ZMachine(load_story(game_id))
        opening = machine.run()
        session = cls(game_id, machine, opening)
        session.pull_status()
        session.place = _place_from_output(opening, session.room)
        session.place_for = session.room
        return session

    def pull_status(self) -> None:
        self.room = self.machine.room
        self.score = self.machine.score
        self.moves = self.machine.moves

    def play(self, command: str) -> Turn:
        before = self.score
        previous_room = self.room
        previous_place = self.place
        previous_id = self.machine.location()
        output = self.machine.accept(command)
        self.pull_status()
        self._append(command, output)
        self._note_place(previous_room, output)
        self._remember_walk(previous_id, previous_room, command, self.machine.location(), self.room)
        died = "you have died" in output.casefold()
        aside = choose_aside(
            game_id=self.game_id,
            died=died,
            delta=self.score - before,
            max_score=GAMES[self.game_id].max_score,
            key=f"{self.moves}:{output[-80:]}",
        )
        return self.panel(
            text=output,
            aside=aside,
            finished=self.machine.halted,
            previous_place=previous_place,
        )

    def panel(
        self,
        text: str = "",
        aside: str | None = None,
        finished: bool = False,
        ok: bool = True,
        error: str | None = None,
        previous_place: str = "",
    ) -> Turn:
        info = GAMES[self.game_id]
        self._ensure_place()
        if self.ui == HUD:
            shown = _hud_body(self.place, self.latest, previous_place=previous_place)
        else:
            shown = self.transcript
        title, status, description = render_panel(
            info.label,
            self.room,
            self.score,
            self.moves,
            info.max_score,
            shown,
            aside,
        )
        return Turn(
            ok=ok,
            game_id=self.game_id,
            title=title,
            description=description,
            text=text,
            transcript=self.transcript,
            status=status,
            room=self.room,
            score=self.score,
            moves=self.moves,
            aside=aside,
            finished=finished or self.machine.halted,
            ui=self.ui,
            place=self.place,
            error=error,
        )

    def _append(self, command: str, output: str) -> None:
        chunk = output.strip("\n")
        block = f"> {command}\n{chunk}" if command else chunk
        if not block:
            return
        self.latest = block
        if self.transcript:
            self.transcript = f"{self.transcript.rstrip()}\n\n{block}"
        else:
            self.transcript = block
        if len(self.transcript) > TRANSCRIPT_LIMIT:
            trimmed = self.transcript[-TRANSCRIPT_LIMIT:]
            newline = trimmed.find("\n")
            self.transcript = trimmed[newline + 1 :] if newline != -1 else trimmed

    def _note_place(self, previous_room: str, output: str) -> None:
        text = _game_text(output)
        if not text:
            return
        entered = self.room.strip().casefold() != (previous_room or "").strip().casefold()
        if entered or _is_location_text(text, self.room):
            self.place = _place_from_output(text, self.room)
            if _is_location_text(self.place, self.room):
                self.place_for = self.room
            else:
                self.place_for = ""

    def _remember_walk(self, src_id: int, src_name: str, command: str, dst_id: int, dst_name: str) -> None:
        direction = _compass(command)
        if not direction or not src_id or not dst_id or src_id == dst_id:
            return
        if not src_name.strip() or not dst_name.strip():
            return
        self._set_exit(src_id, src_name, direction, dst_id, dst_name)
        back = _OPPOSITE[direction]
        existing = str(((self.map.get(str(dst_id)) or {}).get("exits") or {}).get(back) or "")
        if existing in ("", str(src_id)):
            self._set_exit(dst_id, dst_name, back, src_id, src_name)

    def _set_exit(self, src_id: int, src_name: str, direction: str, dst_id: int, dst_name: str) -> None:
        src = self.map.setdefault(str(src_id), {"name": src_name.strip(), "exits": {}})
        if src_name.strip():
            src["name"] = src_name.strip()
        src.setdefault("exits", {})[direction] = str(dst_id)
        dst = self.map.setdefault(str(dst_id), {"name": dst_name.strip(), "exits": {}})
        if dst_name.strip():
            dst["name"] = dst_name.strip()

    def sketch(self) -> str:
        here_id = str(self.machine.location() or "")
        node = self.map.get(here_id) or {}
        exits = node.get("exits") or {}
        names = {}
        for direction, dest in exits.items():
            if direction not in _OPPOSITE:
                continue
            other = self.map.get(str(dest)) or {}
            name = str(other.get("name") or "").strip()
            if name and str(dest) != here_id:
                names[direction] = name
        return render_sketch(str(node.get("name") or self.room or "Here"), names)

    def _ensure_place(self) -> None:
        """Fill a missing room description from LOOK, then restore the game."""
        if self.place and self.place_for.casefold() == (self.room or "").casefold():
            return
        if self.machine.halted or not self.machine.waiting:
            return
        snap = self.machine.snapshot()
        try:
            output = self.machine.accept("look")
        except ZMachineError:
            self.machine.load_snapshot(snap)
            return
        self.machine.load_snapshot(snap)
        self.pull_status()
        text = _place_from_output(output, self.room)
        if text:
            self.place = text
        self.place_for = self.room


def choose_aside(*, game_id: str, died: bool, delta: int, max_score: int, key: str) -> str | None:
    del game_id
    if died:
        return _pick(DEATH_LINES, key)
    threshold = 10 if max_score >= 100 else 1
    if delta >= threshold:
        return _pick(SCORE_LINES, key)
    return None


def render_panel(
    label: str,
    room: str,
    score: int,
    moves: int,
    max_score: int,
    transcript: str,
    aside: str | None,
) -> tuple[str, str, str]:
    place = " ".join(room.split()).upper() if room and room.strip() else label
    title = place[:256]
    status = f"{label} // {place} // SCORE {score}/{max_score} // MOVES {moves}"
    body = transcript.strip("\n")
    aside_line = ""
    if aside:
        clean = " ".join(aside.replace("*", "").split())
        if clean:
            aside_line = f"\n*{clean}*"
    budget = 3900 - len(status) - len(aside_line)
    if budget < 200:
        budget = 200
    if len(body) > budget:
        body = body[-budget:]
        newline = body.find("\n")
        if 0 <= newline < 180:
            body = body[newline + 1 :]
        body = "…\n" + body
    description = f"{status}\n{_fence(body)}{aside_line}"
    if len(description) > 4096:
        description = description[:4093] + "…"
    return title, status, description


def load_story(game_id: str) -> bytes:
    info = GAMES.get(game_id)
    if info is None:
        raise ValueError(f"unknown game {game_id}")
    if game_id not in _STORIES:
        path = Path(__file__).resolve().parent / "stories" / info.filename
        data = path.read_bytes()
        _STORIES[game_id] = data
        _HASHES[game_id] = hashlib.sha256(data).hexdigest()
    return _STORIES[game_id]


def story_hash(game_id: str) -> str:
    load_story(game_id)
    return _HASHES[game_id]


def reset_runtime_cache() -> None:
    """Drop in-process sessions so the next open reads the save files."""
    _LIVE.clear()
    _INDEXES.clear()


def open_session(root: str | Path, user_id: str, game_id: str) -> Turn:
    session = _ensure(root, user_id, game_id)
    turn = session.panel(text=session.transcript)
    _write_slot(root, user_id, session)
    return turn


def command_session(root: str | Path, user_id: str, game_id: str, command: str) -> Turn:
    key = _cache_key(root, user_id, game_id)
    try:
        session = _ensure(root, user_id, game_id)
        if session.machine.halted:
            return session.panel(aside="The game has stopped. Restart to go back in.", finished=True)
        turn = session.play(command.strip())
    except Exception as exc:
        print(f"[Zork] {game_id} stalled: {type(exc).__name__}: {exc}")
        _LIVE.pop(key, None)
        try:
            session = _ensure(root, user_id, game_id)
        except Exception as reload_error:
            return _failed(game_id, str(reload_error))
        return session.panel(aside="The machine stalled. You're still where you were.")
    _write_slot(root, user_id, session)
    return turn


def restart_session(root: str | Path, user_id: str, game_id: str) -> Turn:
    """Clear that player's slot for this game, then boot a new one."""
    key = _cache_key(root, user_id, game_id)
    ui, chosen = _saved_choice(root, user_id, game_id)
    _delete_slot(root, user_id, game_id)
    _LIVE.pop(key, None)
    session = Session.start(game_id)
    session.ui = ui
    session.ui_chosen = chosen
    _LIVE[key] = session
    turn = session.panel(text=session.transcript)
    _write_slot(root, user_id, session)
    return turn


def named_in_text(game_id: str, room: str, text: str) -> list[str]:
    """Object names that already appear in this room's text. Nothing hidden."""
    if game_id not in GAMES:
        return []
    folded = text.casefold()
    room_name = " ".join(room.casefold().split())
    found: list[str] = []
    for name in _object_names(game_id):
        clean = " ".join(name.split())
        key = clean.casefold()
        if len(clean) < 3 or key in _OBJECT_SKIP or key == room_name:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", folded):
            found.append(clean)
    found.sort(key=len, reverse=True)
    kept: list[str] = []
    for name in found:
        key = name.casefold()
        if any(key in chosen.casefold() for chosen in kept):
            continue
        kept.append(name)
        if len(kept) == 8:
            break
    kept.sort(key=str.lower)
    return kept


def remember_active(root: str | Path, guild_id: str, user_id: str, game_id: str) -> None:
    if game_id not in GAMES:
        raise ValueError(game_id)
    index = _index(root)
    index.setdefault("active", {})[f"{guild_id}:{_user_key(user_id)}"] = game_id
    _write_index(root, index)


def active_game(root: str | Path, guild_id: str, user_id: str) -> str | None:
    try:
        found = _index(root).get("active", {}).get(f"{guild_id}:{_user_key(user_id)}")
    except (OSError, ValueError):
        return None
    return found if found in GAMES else None


def map_sketch(root: str | Path, user_id: str, game_id: str) -> str:
    """Monospace sketch of walked connections. Does not run the game."""
    return _ensure(root, user_id, game_id).sketch()


def toggle_ui(root: str | Path, user_id: str, game_id: str) -> Turn:
    """Switch this player's panel for this game between HUD and TERMINAL."""
    session = _ensure(root, user_id, game_id)
    session.ui = HUD if session.ui == TERMINAL else TERMINAL
    session.ui_chosen = True
    turn = session.panel(text=session.latest)
    _write_slot(root, user_id, session)
    return turn


def bind_thread(
    root: str | Path,
    thread_id: int | str,
    user_id: str,
    game_id: str,
    message_id: int | str,
    guild_id: int | str,
) -> None:
    if game_id not in GAMES:
        raise ValueError(game_id)
    index = _index(root)
    index["threads"][str(thread_id)] = {
        "user_id": _user_key(user_id),
        "game": game_id,
        "message_id": str(message_id),
        "guild_id": str(guild_id or ""),
    }
    _write_index(root, index)


def lookup_thread(root: str | Path | None, thread_id: int | str) -> dict | None:
    if not root:
        return None
    try:
        found = _index(root)["threads"].get(str(thread_id))
    except (OSError, ValueError):
        return None
    return dict(found) if isinstance(found, dict) else None


def _ensure(root: str | Path, user_id: str, game_id: str) -> Session:
    if game_id not in GAMES:
        raise ValueError(game_id)
    key = _cache_key(root, user_id, game_id)
    cached = _LIVE.get(key)
    if cached is not None:
        return cached
    loaded = _read_slot(root, user_id, game_id)
    session = loaded if loaded is not None else Session.start(game_id)
    _LIVE[key] = session
    return session


def _cache_key(root: str | Path, user_id: str, game_id: str) -> tuple[str, str, str]:
    return (str(_root(root)), _user_key(user_id), game_id)


def _root(root: str | Path) -> Path:
    return Path(root).expanduser().resolve()


def _user_key(user_id: str) -> str:
    digits = "".join(character for character in str(user_id) if character.isdigit())
    if not digits:
        raise ValueError("user id")
    return digits


def _slot_path(root: str | Path, user_id: str, game_id: str) -> Path:
    if game_id not in GAMES:
        raise ValueError(game_id)
    return _root(root) / "zork" / "saves" / _user_key(user_id) / f"{game_id}.json"


def _read_slot(root: str | Path, user_id: str, game_id: str) -> Session | None:
    path = _slot_path(root, user_id, game_id)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text())
        if payload.get("version") != SAVE_VERSION or payload.get("game") != game_id:
            return None
        if payload.get("story") != story_hash(game_id):
            return None
        machine = ZMachine(load_story(game_id))
        machine.load_snapshot(_decode_snap(payload["machine"]))
        checkpoint = payload.get("checkpoint")
        if checkpoint:
            machine.checkpoint = _decode_snap(checkpoint)
        transcript = str(payload.get("transcript") or "")
        latest = payload.get("latest")
        if not isinstance(latest, str):
            latest = _latest_block(transcript)
        ui, chosen = _resolve_ui(payload)
        session = Session(game_id, machine, transcript, latest, ui, ui_chosen=chosen)
        session.pull_status()
        session.map = _clean_map(payload.get("map"))
        stored_place = payload.get("place")
        stored_for = str(payload.get("place_for") or "")
        if isinstance(stored_place, str) and _is_location_text(_game_text(stored_place), session.room):
            session.place = _game_text(stored_place)
            session.place_for = session.room
        elif isinstance(stored_place, str) and stored_for.casefold() == session.room.casefold():
            session.place = _game_text(stored_place)
            session.place_for = session.room
        return session
    except (OSError, ValueError, KeyError, TypeError, ZMachineError) as exc:
        print(f"[Zork] Ignoring unreadable save {path.name}: {type(exc).__name__}")
        return None


def _write_slot(root: str | Path, user_id: str, session: Session) -> None:
    payload = {
        "version": SAVE_VERSION,
        "game": session.game_id,
        "story": story_hash(session.game_id),
        "machine": _encode_snap(session.machine.snapshot()),
        "checkpoint": _encode_snap(session.machine.checkpoint) if session.machine.checkpoint else None,
        "transcript": session.transcript,
        "latest": session.latest,
        "place": session.place,
        "place_for": session.place_for,
        "ui": session.ui,
        "ui_chosen": session.ui_chosen,
        "map": session.map,
    }
    _atomic_json(_slot_path(root, user_id, session.game_id), payload)


_COMPASS = {
    "n": "n",
    "north": "n",
    "s": "s",
    "south": "s",
    "e": "e",
    "east": "e",
    "w": "w",
    "west": "w",
    "u": "u",
    "up": "u",
    "d": "d",
    "down": "d",
}
_OPPOSITE = {"n": "s", "s": "n", "e": "w", "w": "e", "u": "d", "d": "u"}
_OBJECT_NAMES: dict[str, list[str]] = {}
_OBJECT_SKIP = {
    "you",
    "me",
    "it",
    "them",
    "all",
    "here",
    "floor",
    "ground",
    "sky",
    "wall",
    "walls",
}


def _object_names(game_id: str) -> list[str]:
    cached = _OBJECT_NAMES.get(game_id)
    if cached is not None:
        return cached
    machine = ZMachine(load_story(game_id))
    names = []
    for number in range(1, 256):
        try:
            name = machine.short_name(number).strip()
        except (ZMachineError, IndexError, ValueError):
            continue
        if name:
            names.append(name)
    _OBJECT_NAMES[game_id] = names
    return names


def _compass(command: str) -> str | None:
    words = [word.strip(".,!?").lower() for word in command.split() if word.strip(".,!?")]
    if not words:
        return None
    if words[0] in {"go", "walk", "move"} and len(words) == 2:
        words = words[1:]
    if len(words) != 1:
        return None
    return _COMPASS.get(words[0])


def _clean_map(value) -> dict:
    if not isinstance(value, dict):
        return {}
    cleaned = {}
    for ident, node in value.items():
        if not str(ident).isdigit() or not isinstance(node, dict):
            continue
        name = " ".join(str(node.get("name") or "").split())
        raw = node.get("exits")
        exits = {}
        if isinstance(raw, dict):
            for direction, dest in raw.items():
                if direction in _OPPOSITE and str(dest).isdigit():
                    exits[direction] = str(dest)
        if name or exits:
            cleaned[str(ident)] = {"name": name, "exits": exits}
    return cleaned


def render_sketch(here: str, exits: dict[str, str]) -> str:
    """Current room in the middle. A side is drawn only when that neighbor was walked."""
    label = f"[{' '.join((here or 'Here').split())}]"
    north = " ".join(str(exits.get("n") or "").split())
    south = " ".join(str(exits.get("s") or "").split())
    east = " ".join(str(exits.get("e") or "").split())
    west = " ".join(str(exits.get("w") or "").split())
    up = " ".join(str(exits.get("u") or "").split())
    down = " ".join(str(exits.get("d") or "").split())
    left = f"{west} -- " if west else ""
    right = f" -- {east}" if east else ""
    middle = f"{left}{label}{right}"
    hub = len(left) + (len(label) // 2)
    rows: list[tuple[int, str]] = []
    if north:
        rows.append((hub - (len(north) // 2), north))
        rows.append((hub, "|"))
    rows.append((0, middle))
    if south:
        rows.append((hub, "|"))
        rows.append((hub - (len(south) // 2), south))
    shift = max(0, -min(start for start, _text in rows))
    lines = [" " * (start + shift) + text for start, text in rows]
    vertical = []
    if up:
        vertical.append(f"Up: {up}")
    if down:
        vertical.append(f"Down: {down}")
    if vertical:
        lines.append("")
        lines.append("   ".join(vertical))
    return "\n".join(lines)


def _ui_mode(value) -> str:
    return HUD if str(value or "").casefold() == HUD else TERMINAL


def _resolve_ui(payload: dict) -> tuple[str, bool]:
    """TERMINAL until /zork-ui records a choice. An older terminal value counts as one."""
    if payload.get("ui_chosen") is True:
        return _ui_mode(payload.get("ui")), True
    if str(payload.get("ui") or "").casefold() == TERMINAL:
        return TERMINAL, True
    return TERMINAL, False


def _saved_choice(root: str | Path, user_id: str, game_id: str) -> tuple[str, bool]:
    path = _slot_path(root, user_id, game_id)
    if not path.is_file():
        return TERMINAL, False
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return TERMINAL, False
    if not isinstance(payload, dict):
        return TERMINAL, False
    return _resolve_ui(payload)


def _game_text(output: str) -> str:
    text = (output or "").strip()
    if not text:
        return ""
    head, sep, tail = text.rpartition("\n")
    if sep and tail.strip() == ">":
        text = head.strip()
    elif text == ">":
        return ""
    return text.strip("\n")


def _split_latest(latest: str) -> tuple[str, str]:
    text = (latest or "").strip("\n")
    if text.startswith("> "):
        echo, _, rest = text.partition("\n")
        return echo, _game_text(rest)
    return "", _game_text(text)


def _is_location_text(text: str, room: str) -> bool:
    if not text or not room:
        return False
    first = text.split("\n", 1)[0].strip()
    return first.casefold() == room.strip().casefold()


def _place_from_output(text: str, room: str) -> str:
    cleaned = _game_text(text)
    if not cleaned or not room:
        return cleaned
    target = room.strip().casefold()
    lines = cleaned.split("\n")
    for index, line in enumerate(lines):
        if line.strip().casefold() == target:
            return "\n".join(lines[index:]).strip("\n")
    return cleaned


def _squash(text: str) -> str:
    lines = text.strip("\n").split("\n")
    cleaned: list[str] = []
    blank = False
    for line in lines:
        if not line.strip():
            if cleaned and not blank:
                cleaned.append("")
            blank = True
            continue
        blank = False
        cleaned.append(line.rstrip())
    return "\n".join(cleaned).strip("\n")


def _excise(text: str, block: str) -> str:
    """Drop every copy of a room block. The caller puts the current room back once."""
    text = _game_text(text)
    block = _game_text(block)
    if not text or not block:
        return text
    while block in text:
        text = text.replace(block, "", 1)
    return _squash(text)


def _join_parts(first: str, second: str) -> str:
    first = _game_text(first)
    second = _game_text(second)
    if not first:
        return second
    if not second or second == first or second in first:
        return first
    return f"{first}\n\n{second}"


def _hud_body(place: str, latest: str, previous_place: str = "") -> str:
    """One current room, then the newest outcome. A death stays in story order.

    The command echo stays in the transcript. It is not drawn on the panel.
    """
    pinned = _game_text(place)
    _echo, reply = _split_latest(latest)
    reply = _game_text(reply)
    prior = _game_text(previous_place)
    if prior and prior != pinned:
        reply = _excise(reply, prior)
    if not reply or reply == pinned:
        return pinned or reply
    if pinned and pinned in reply:
        outcome = _excise(reply, pinned)
        if "you have died" in reply.casefold():
            return _join_parts(outcome, pinned)
        return _join_parts(pinned, outcome)
    return _join_parts(pinned, reply)


def _latest_block(transcript: str) -> str:
    text = transcript.strip("\n")
    marker = "\n\n> "
    index = text.rfind(marker)
    if index == -1:
        return text
    return text[index + 2 :]


def _delete_slot(root: str | Path, user_id: str, game_id: str) -> None:
    path = _slot_path(root, user_id, game_id)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        print(f"[Zork] Could not clear save: {type(exc).__name__}")


def _encode_snap(snap: dict) -> dict:
    encoded = dict(snap)
    dynamic = snap["dynamic"]
    if isinstance(dynamic, (bytes, bytearray)):
        encoded["dynamic"] = base64.b64encode(bytes(dynamic)).decode("ascii")
    return encoded


def _decode_snap(data: dict) -> dict:
    decoded = dict(data)
    dynamic = data["dynamic"]
    if isinstance(dynamic, str):
        decoded["dynamic"] = base64.b64decode(dynamic)
    return decoded


def _index_path(root: str | Path) -> Path:
    return _root(root) / "zork" / "threads.json"


def _index(root: str | Path) -> dict:
    key = str(_root(root))
    cached = _INDEXES.get(key)
    if cached is not None:
        return cached
    path = _index_path(root)
    index = {"threads": {}}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict) and isinstance(loaded.get("threads"), dict):
                active = loaded.get("active")
                index = {
                    "threads": loaded["threads"],
                    "active": active if isinstance(active, dict) else {},
                }
        except (OSError, ValueError):
            index = {"threads": {}}
    _INDEXES[key] = index
    return index


def _write_index(root: str | Path, index: dict) -> None:
    _INDEXES[str(_root(root))] = index
    _atomic_json(_index_path(root), index)


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")))
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)


def _fence(text: str) -> str:
    safe = (text or " ").replace("```", "``\u200b`")
    return f"```\n{safe}\n```"


def _pick(lines: tuple[str, ...], key: str) -> str:
    return lines[zlib.crc32(key.encode()) % len(lines)]


def _failed(game_id: str, message: str) -> Turn:
    info = GAMES.get(game_id) or GAMES["zork1"]
    title, status, description = render_panel(
        info.label, "", 0, 0, info.max_score, message or "The machine stalled.", None
    )
    return Turn(
        ok=False,
        game_id=info.game_id,
        title=title,
        description=description,
        text=message,
        transcript=message,
        status=status,
        room="",
        score=0,
        moves=0,
        aside=None,
        finished=True,
        error=message,
    )
