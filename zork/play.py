"""Zork sessions, the one save slot per player per game, and the panel text.

Saves live under the runtime data directory the caller passes in. Nothing in
this module writes into the git checkout.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
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
    ui: str = HUD
    error: str | None = None


class Session:
    def __init__(
        self,
        game_id: str,
        machine: ZMachine,
        transcript: str,
        latest: str | None = None,
        ui: str = HUD,
    ):
        self.game_id = game_id
        self.machine = machine
        self.transcript = transcript.strip("\n")
        self.latest = (self.transcript if latest is None else latest).strip("\n")
        self.ui = _ui_mode(ui)
        self.room = machine.room
        self.score = machine.score
        self.moves = machine.moves

    @classmethod
    def start(cls, game_id: str) -> Session:
        machine = ZMachine(load_story(game_id))
        opening = machine.run()
        session = cls(game_id, machine, opening)
        session.pull_status()
        return session

    def pull_status(self) -> None:
        self.room = self.machine.room
        self.score = self.machine.score
        self.moves = self.machine.moves

    def play(self, command: str) -> Turn:
        before = self.score
        output = self.machine.accept(command)
        self.pull_status()
        self._append(command, output)
        died = "you have died" in output.casefold()
        aside = choose_aside(
            game_id=self.game_id,
            died=died,
            delta=self.score - before,
            max_score=GAMES[self.game_id].max_score,
            key=f"{self.moves}:{output[-80:]}",
        )
        return self.panel(text=output, aside=aside, finished=self.machine.halted)

    def panel(
        self,
        text: str = "",
        aside: str | None = None,
        finished: bool = False,
        ok: bool = True,
        error: str | None = None,
    ) -> Turn:
        info = GAMES[self.game_id]
        shown = self.transcript if self.ui == TERMINAL else (self.latest or self.transcript)
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
    _write_slot(root, user_id, session)
    return session.panel(text=session.transcript)


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
    ui = _saved_ui(root, user_id, game_id)
    _delete_slot(root, user_id, game_id)
    _LIVE.pop(key, None)
    session = Session.start(game_id)
    session.ui = ui
    _LIVE[key] = session
    _write_slot(root, user_id, session)
    return session.panel(text=session.transcript)


def toggle_ui(root: str | Path, user_id: str, game_id: str) -> Turn:
    """Switch this player's panel for this game between HUD and TERMINAL."""
    session = _ensure(root, user_id, game_id)
    session.ui = HUD if session.ui == TERMINAL else TERMINAL
    _write_slot(root, user_id, session)
    return session.panel(text=session.latest)


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
        session = Session(
            game_id,
            machine,
            transcript,
            latest,
            _ui_mode(payload.get("ui")),
        )
        session.pull_status()
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
        "ui": session.ui,
    }
    _atomic_json(_slot_path(root, user_id, session.game_id), payload)


def _ui_mode(value) -> str:
    return TERMINAL if str(value or "").casefold() == TERMINAL else HUD


def _saved_ui(root: str | Path, user_id: str, game_id: str) -> str:
    path = _slot_path(root, user_id, game_id)
    if not path.is_file():
        return HUD
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return HUD
    if not isinstance(payload, dict):
        return HUD
    return _ui_mode(payload.get("ui"))


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
                index = {"threads": loaded["threads"]}
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
