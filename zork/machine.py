"""A small Z-machine for Zork I, II, and III (version 3 only).

Behavior follows the Z-Machine Standards Document 1.1. This file is Grim's
interpreter. The story files next to it are the MIT-licensed historicalsource
releases; nothing here is copied from another interpreter.
"""

from __future__ import annotations

import base64
from collections import deque


# Z-characters 7..31 in alphabet A2. Z-character 6 is the 10-bit escape.
A2 = "\n0123456789.,!?_#'\"/\\-:()"
if len(A2) != 25:
    raise RuntimeError("A2 alphabet length drifted")


class ZMachineError(RuntimeError):
    """The story stopped because the interpreter hit a bad state."""


class Frame:
    __slots__ = ("pc", "store", "locals", "depth", "argc")

    def __init__(self, pc: int, store: int, locals_: list[int], depth: int, argc: int):
        self.pc = pc
        self.store = store
        self.locals = locals_
        self.depth = depth
        self.argc = argc

    def dump(self) -> dict:
        return {
            "pc": self.pc,
            "store": self.store,
            "locals": list(self.locals),
            "depth": self.depth,
            "argc": self.argc,
        }

    @classmethod
    def load(cls, data: dict) -> Frame:
        return cls(
            int(data["pc"]),
            int(data["store"]),
            [int(value) & 0xFFFF for value in data["locals"]],
            int(data["depth"]),
            int(data["argc"]),
        )


def s16(value: int) -> int:
    value &= 0xFFFF
    return value - 0x10000 if value & 0x8000 else value


def encode_word(text: str) -> bytes:
    """Pack a typed word the way a version-3 dictionary entry is packed."""
    zchars: list[int] = []
    for char in text.lower():
        zchars.extend(_zchars_for(char))
        if len(zchars) >= 6:
            break
    zchars = zchars[:6]
    while len(zchars) < 6:
        zchars.append(5)
    words = []
    for index in range(0, 6, 3):
        words.append((zchars[index] << 10) | (zchars[index + 1] << 5) | zchars[index + 2])
    words[-1] |= 0x8000
    return bytes((words[0] >> 8, words[0] & 0xFF, words[1] >> 8, words[1] & 0xFF))


def _zchars_for(char: str) -> list[int]:
    code = ord(char)
    if 97 <= code <= 122:
        return [code - 97 + 6]
    if 65 <= code <= 90:
        return [4, code - 65 + 6]
    try:
        index = A2.index(char)
    except ValueError:
        return [5, 6, (code >> 5) & 31, code & 31]
    return [5, index + 7]


def _trunc_div(left: int, right: int) -> tuple[int, int]:
    left, right = s16(left), s16(right)
    if right == 0:
        raise ZMachineError("division by zero")
    negative = (left < 0) ^ (right < 0)
    quotient = abs(left) // abs(right)
    if negative:
        quotient = -quotient
    remainder = left - quotient * right
    return quotient & 0xFFFF, remainder & 0xFFFF


_OP0 = (
    "rtrue", "rfalse", "print", "print_ret", "nop", "save", "restore",
    "restart", "ret_popped", "pop", "quit", "new_line", "show_status",
    "verify", "extended", "piracy",
)
_OP1 = (
    "jz", "get_sibling", "get_child", "get_parent", "get_prop_len",
    "inc", "dec", "print_addr", "call_1s", "remove_obj", "print_obj",
    "ret", "jump", "print_paddr", "load", "not_",
)
_OP2 = (
    None, "je", "jl", "jg", "dec_chk", "inc_chk", "jin", "test",
    "or_", "and_", "test_attr", "set_attr", "clear_attr", "store",
    "insert_obj", "loadw", "loadb", "get_prop", "get_prop_addr",
    "get_next_prop", "add", "sub", "mul", "div", "mod",
    "call_2s", "call_2n", "set_colour", "throw", None, None, None,
)
_VAR = (
    "call", "storew", "storeb", "put_prop", "read", "print_char",
    "print_num", "random", "push", "pull", "split_window", "set_window",
    "call_vs2", "erase_window", "erase_line", "set_cursor", "get_cursor",
    "set_text_style", "buffer_mode", "output_stream", "input_stream",
    "sound_effect",
)


class ZMachine:
    """Version-3 Z-machine. Runs until a read, a quit, or a hard error."""

    def __init__(self, story: bytes, seed: int = 1):
        if not story or story[0] != 3:
            raise ZMachineError("only Z-machine version 3 story files are supported")
        self.original = bytes(story)
        self.mem = bytearray(story)
        self.static_base = self.u16(0x0E)
        self.initial_pc = self.u16(0x06)
        if self.static_base <= 0 or self.static_base > len(self.mem):
            raise ZMachineError("story header has a bad static memory base")
        if self.initial_pc >= len(self.mem):
            raise ZMachineError("story header has a bad start address")
        self.pc = self.initial_pc
        self.stack: list[int] = []
        self.frames: list[Frame] = []
        self.screen: list[str] = []
        self.halted = False
        self.waiting = False
        self.read_text = 0
        self.read_parse = 0
        self.stream1 = True
        self.stream2 = False
        self.stream3: list[list[int]] = []
        self.rng = seed & 0x7FFFFFFF or 1
        self.checkpoint: dict | None = None
        self.room = ""
        self.score = 0
        self.moves = 0
        self.timed = False
        self.recent: deque[tuple[int, int]] = deque(maxlen=12)
        self.steps = 0

    def u16(self, addr: int) -> int:
        return (self.mem[addr] << 8) | self.mem[addr + 1]

    def set_u16(self, addr: int, value: int) -> None:
        value &= 0xFFFF
        self.mem[addr] = value >> 8
        self.mem[addr + 1] = value & 0xFF

    def run(self, limit: int = 2_000_000) -> str:
        self.screen.clear()
        steps = 0
        while not self.halted and not self.waiting:
            self.step()
            steps += 1
            if steps >= limit:
                self._fail(f"step limit {limit}")
        self.steps = steps
        try:
            self.refresh_status()
        except ZMachineError:
            pass
        return "".join(self.screen)

    def accept(self, line: str) -> str:
        if self.halted or not self.waiting:
            raise ZMachineError("the game is not waiting for a command")
        self._perform_read(line)
        self.waiting = False
        return self.run()

    def step(self) -> None:
        pc = self.pc
        opcode = self.mem[pc]
        self.recent.append((pc, opcode))
        try:
            self._decode()
        except ZMachineError:
            raise
        except Exception as exc:
            self._fail(f"{type(exc).__name__}: {exc} at ${pc:04x} opcode ${opcode:02x}")

    def snapshot(self) -> dict:
        return {
            "dynamic": bytes(self.mem[: self.static_base]),
            "pc": self.pc,
            "stack": list(self.stack),
            "frames": [frame.dump() for frame in self.frames],
            "waiting": self.waiting,
            "read_text": self.read_text,
            "read_parse": self.read_parse,
            "stream1": self.stream1,
            "stream2": self.stream2,
            "stream3": [list(entry) for entry in self.stream3],
            "rng": self.rng,
            "halted": self.halted,
            "room": self.room,
            "score": self.score,
            "moves": self.moves,
            "timed": self.timed,
        }

    def load_snapshot(self, snap: dict) -> None:
        dynamic = snap["dynamic"]
        if isinstance(dynamic, str):
            dynamic = base64.b64decode(dynamic)
        if len(dynamic) != self.static_base:
            raise ZMachineError("save does not match this story")
        self.mem[: self.static_base] = dynamic
        self.pc = int(snap["pc"])
        self.stack = [int(value) & 0xFFFF for value in snap["stack"]]
        self.frames = [Frame.load(frame) for frame in snap["frames"]]
        self.waiting = bool(snap["waiting"])
        self.read_text = int(snap["read_text"])
        self.read_parse = int(snap["read_parse"])
        self.stream1 = bool(snap["stream1"])
        self.stream2 = bool(snap["stream2"])
        self.stream3 = [[int(item) for item in entry] for entry in snap["stream3"]]
        self.rng = int(snap["rng"]) & 0x7FFFFFFF or 1
        self.halted = bool(snap["halted"])
        self.room = str(snap.get("room") or "")
        self.score = int(snap.get("score") or 0)
        self.moves = int(snap.get("moves") or 0)
        self.timed = bool(snap.get("timed"))

    def refresh_status(self) -> None:
        self.timed = bool(self.mem[0x01] & 0x02)
        location = self.read_var(16)
        self.score = s16(self.read_var(17))
        self.moves = self.read_var(18)
        self.room = self.short_name(location).strip() if location else ""

    def restart_machine(self) -> None:
        kept = self.u16(0x10) & 0x0003
        self.mem[:] = self.original
        self.set_u16(0x10, (self.u16(0x10) & ~0x0003) | kept)
        self.pc = self.initial_pc
        self.stack.clear()
        self.frames.clear()
        self.waiting = False
        self.halted = False
        self.stream1 = True
        self.stream2 = bool(kept & 0x0001)
        self.stream3.clear()
        self.read_text = 0
        self.read_parse = 0

    # --- decode -----------------------------------------------------------

    def _fail(self, message: str) -> None:
        trail = " ".join(f"{pc:04x}:{opcode:02x}" for pc, opcode in self.recent)
        raise ZMachineError(f"{message} [{trail}]")

    def _decode(self) -> None:
        opcode = self.mem[self.pc]
        self.pc += 1
        if opcode < 0x80:
            kinds = (2 if opcode & 0x40 else 1, 2 if opcode & 0x20 else 1)
            operands = [self._operand(kind) for kind in kinds]
            self._dispatch(_OP2, "op2", opcode & 0x1F, operands)
            return
        if opcode < 0xB0:
            operands = [self._operand((opcode >> 4) & 0x03)]
            self._dispatch(_OP1, "op1", opcode & 0x0F, operands)
            return
        if opcode < 0xC0:
            self._dispatch(_OP0, "op0", opcode & 0x0F, None)
            return
        two_operand = opcode < 0xE0
        type_byte = self.mem[self.pc]
        self.pc += 1
        operands = []
        for shift in (6, 4, 2, 0):
            kind = (type_byte >> shift) & 0x03
            if kind == 3:
                break
            operands.append(self._operand(kind))
        if two_operand:
            self._dispatch(_OP2, "op2", opcode & 0x1F, operands)
        else:
            self._dispatch(_VAR, "opv", opcode & 0x1F, operands)

    def _dispatch(self, table: tuple, prefix: str, number: int, operands) -> None:
        if number >= len(table) or not table[number]:
            self._fail(f"unimplemented {prefix}:{number}")
        handler = getattr(self, f"{prefix}_{table[number]}", None)
        if handler is None:
            self._fail(f"unimplemented {prefix}:{number} {table[number]}")
        if operands is None:
            handler()
        else:
            handler(operands)

    def _operand(self, kind: int) -> int:
        if kind == 0:
            value = self.u16(self.pc)
            self.pc += 2
            return value
        if kind == 1:
            value = self.mem[self.pc]
            self.pc += 1
            return value
        if kind == 2:
            number = self.mem[self.pc]
            self.pc += 1
            return self.read_var(number, indirect=False)
        self._fail("bad operand type")
        return 0

    def read_var(self, number: int, indirect: bool = False) -> int:
        number &= 0xFF
        if number == 0:
            if not self.stack:
                self._fail("stack underflow")
            if indirect:
                return self.stack[-1]
            return self.stack.pop()
        if number < 16:
            if not self.frames or number - 1 >= len(self.frames[-1].locals):
                self._fail(f"missing local {number}")
            return self.frames[-1].locals[number - 1]
        return self.u16(self.u16(0x0C) + 2 * (number - 16))

    def write_var(self, number: int, value: int, indirect: bool = False) -> None:
        number &= 0xFF
        value &= 0xFFFF
        if number == 0:
            if indirect:
                if not self.stack:
                    self._fail("stack underflow")
                self.stack[-1] = value
            else:
                self.stack.append(value)
            return
        if number < 16:
            if not self.frames or number - 1 >= len(self.frames[-1].locals):
                self._fail(f"missing local {number}")
            self.frames[-1].locals[number - 1] = value
            return
        self.set_u16(self.u16(0x0C) + 2 * (number - 16), value)

    def store_result(self, value: int) -> None:
        variable = self.mem[self.pc]
        self.pc += 1
        self.write_var(variable, value, indirect=False)

    def read_branch(self) -> tuple[bool, int]:
        first = self.mem[self.pc]
        self.pc += 1
        if first & 0x40:
            offset = first & 0x3F
        else:
            offset = ((first & 0x3F) << 8) | self.mem[self.pc]
            self.pc += 1
            if offset & 0x2000:
                offset -= 0x4000
        return bool(first & 0x80), offset

    def apply_branch(self, polarity: bool, offset: int, condition: bool) -> None:
        if bool(condition) != polarity:
            return
        if offset == 0:
            self.do_return(0)
        elif offset == 1:
            self.do_return(1)
        else:
            self.pc += offset - 2

    def branch(self, condition: bool) -> None:
        polarity, offset = self.read_branch()
        self.apply_branch(polarity, offset, condition)

    # --- calls ------------------------------------------------------------

    def do_call(self, operands: list[int], store: int | None) -> None:
        packed = operands[0] if operands else 0
        if packed == 0:
            if store is not None:
                self.write_var(store, 0)
            return
        address = (packed & 0xFFFF) * 2
        local_count = self.mem[address]
        if local_count > 15:
            self._fail(f"routine at ${address:04x} has {local_count} locals")
        cursor = address + 1
        locals_ = []
        for _ in range(local_count):
            locals_.append(self.u16(cursor))
            cursor += 2
        arguments = operands[1:4]
        for index, argument in enumerate(arguments[:local_count]):
            locals_[index] = argument & 0xFFFF
        self.frames.append(
            Frame(self.pc, 0 if store is None else store, locals_, len(self.stack), len(arguments))
        )
        self.pc = cursor

    def do_return(self, value: int) -> None:
        if not self.frames:
            self.halted = True
            return
        frame = self.frames.pop()
        del self.stack[frame.depth :]
        self.pc = frame.pc
        self.write_var(frame.store, value)

    # --- text -------------------------------------------------------------

    def emit_zscii(self, code: int) -> None:
        if code == 0 or len(self.screen) > 250_000:
            return
        if self.stream3:
            table, count = self.stream3[-1]
            slot = table + 2 + count
            if 0 <= slot < self.static_base:
                self.mem[slot] = code & 0xFF
            count += 1
            self.stream3[-1][1] = count
            if table + 1 < self.static_base:
                self.set_u16(table, count & 0xFFFF)
            return
        if not self.stream1:
            return
        if code == 13:
            self.screen.append("\n")
        elif 32 <= code <= 126:
            self.screen.append(chr(code))

    def emit_text(self, text: str) -> None:
        for char in text:
            if char == "\n":
                self.emit_zscii(13)
            else:
                code = ord(char)
                self.emit_zscii(code if code <= 126 else 63)

    def read_zstring(self, addr: int, in_abbrev: bool = False, depth: int = 0) -> tuple[str, int]:
        if depth > 3:
            return "", addr
        pieces: list[int] = []
        words = 0
        while words < 4096:
            word = self.u16(addr)
            addr += 2
            pieces.append((word >> 10) & 31)
            pieces.append((word >> 5) & 31)
            pieces.append(word & 31)
            words += 1
            if word & 0x8000:
                break
        chars: list[str] = []
        alphabet = 0
        index = 0
        while index < len(pieces):
            zchar = pieces[index]
            index += 1
            if zchar == 0:
                chars.append(" ")
                alphabet = 0
                continue
            if zchar == 4:
                alphabet = 1
                continue
            if zchar == 5:
                alphabet = 2
                continue
            if zchar in (1, 2, 3):
                if in_abbrev or index >= len(pieces):
                    alphabet = 0
                    continue
                which = pieces[index]
                index += 1
                chars.append(self._abbrev(32 * (zchar - 1) + which, depth + 1))
                alphabet = 0
                continue
            if alphabet == 2 and zchar == 6:
                if index + 1 >= len(pieces):
                    break
                code = (pieces[index] << 5) | pieces[index + 1]
                index += 2
                chars.append(self._zscii_char(code))
                alphabet = 0
                continue
            if zchar < 6:
                alphabet = 0
                continue
            chars.append(self._alphabet_char(alphabet, zchar))
            alphabet = 0
        return "".join(chars), addr

    def _abbrev(self, number: int, depth: int) -> str:
        if not 0 <= number <= 95:
            return ""
        table = self.u16(0x18)
        packed = self.u16(table + 2 * number)
        if packed == 0:
            return ""
        text, _ = self.read_zstring(packed * 2, in_abbrev=True, depth=depth)
        return text

    def _alphabet_char(self, alphabet: int, zchar: int) -> str:
        if alphabet == 0:
            return chr(ord("a") + zchar - 6)
        if alphabet == 1:
            return chr(ord("A") + zchar - 6)
        index = zchar - 7
        if 0 <= index < len(A2):
            return A2[index]
        return ""

    def _zscii_char(self, code: int) -> str:
        if code == 13:
            return "\n"
        if 32 <= code <= 126:
            return chr(code)
        return "?"

    def short_name(self, obj: int) -> str:
        obj = self._obj(obj)
        if not obj:
            return ""
        header = self.u16(self._obj_addr(obj) + 7)
        if self.mem[header] == 0:
            return ""
        text, _ = self.read_zstring(header + 1)
        return text

    # --- objects ----------------------------------------------------------

    def _obj(self, value: int) -> int:
        value &= 0xFFFF
        return value if 1 <= value <= 255 else 0

    def _obj_addr(self, obj: int) -> int:
        return self.u16(0x0A) + 62 + (obj - 1) * 9

    def _relation(self, obj: int, offset: int) -> int:
        obj = self._obj(obj)
        if not obj:
            return 0
        return self.mem[self._obj_addr(obj) + offset]

    def _set_relation(self, obj: int, offset: int, value: int) -> None:
        obj = self._obj(obj)
        if obj:
            self.mem[self._obj_addr(obj) + offset] = value & 0xFF

    def _attr(self, obj: int, attr: int, set_to: bool | None = None) -> bool:
        obj = self._obj(obj)
        if not obj or not 0 <= attr <= 31:
            return False
        address = self._obj_addr(obj) + (attr // 8)
        bit = 1 << (7 - (attr % 8))
        if set_to is None:
            return bool(self.mem[address] & bit)
        if set_to:
            self.mem[address] |= bit
        else:
            self.mem[address] &= ~bit & 0xFF
        return set_to

    def _prop_start(self, obj: int) -> int:
        header = self.u16(self._obj_addr(obj) + 7)
        return header + 1 + self.mem[header] * 2

    def _walk_props(self, obj: int):
        obj = self._obj(obj)
        if not obj:
            return
        address = self._prop_start(obj)
        for _ in range(64):
            size = self.mem[address]
            if size == 0:
                return
            number = size & 31
            length = (size >> 5) + 1
            data = address + 1
            yield number, length, data
            address = data + length

    def _get_prop(self, obj: int, prop: int) -> int:
        prop &= 0xFFFF
        for number, length, data in self._walk_props(obj):
            if number == prop:
                if length == 1:
                    return self.mem[data]
                return self.u16(data)
        if 1 <= prop <= 31:
            return self.u16(self.u16(0x0A) + 2 * (prop - 1))
        return 0

    def _put_prop(self, obj: int, prop: int, value: int) -> None:
        prop &= 0xFFFF
        for number, length, data in self._walk_props(obj):
            if number == prop:
                if length == 1:
                    self.mem[data] = value & 0xFF
                else:
                    self.set_u16(data, value)
                return
        self._fail(f"object {obj} has no property {prop}")

    def _remove(self, obj: int) -> None:
        obj = self._obj(obj)
        if not obj:
            return
        parent = self._relation(obj, 4)
        if parent:
            child = self._relation(parent, 6)
            if child == obj:
                self._set_relation(parent, 6, self._relation(obj, 5))
            else:
                previous = child
                guard = 0
                while previous and self._relation(previous, 5) != obj and guard < 255:
                    previous = self._relation(previous, 5)
                    guard += 1
                if previous and self._relation(previous, 5) == obj:
                    self._set_relation(previous, 5, self._relation(obj, 5))
        self._set_relation(obj, 4, 0)
        self._set_relation(obj, 5, 0)

    def _insert(self, obj: int, dest: int) -> None:
        obj = self._obj(obj)
        dest = self._obj(dest)
        if not obj or obj == dest:
            return
        self._remove(obj)
        if not dest:
            return
        self._set_relation(obj, 5, self._relation(dest, 6))
        self._set_relation(obj, 4, dest)
        self._set_relation(dest, 6, obj)

    # --- dictionary -------------------------------------------------------

    def lookup_word(self, encoded: bytes, dictionary: int = 0) -> int:
        if not dictionary:
            dictionary = self.u16(0x08)
        separators = self.mem[dictionary]
        entry_len = self.mem[dictionary + 1 + separators]
        count = s16(self.u16(dictionary + 2 + separators))
        start = dictionary + 4 + separators
        if entry_len < 4 or count == 0:
            return 0
        if count < 0:
            for index in range(-count):
                address = start + index * entry_len
                if bytes(self.mem[address : address + 4]) == encoded:
                    return address
            return 0
        low = 0
        high = count - 1
        while low <= high:
            mid = (low + high) // 2
            address = start + mid * entry_len
            word = bytes(self.mem[address : address + 4])
            if word == encoded:
                return address
            if word < encoded:
                low = mid + 1
            else:
                high = mid - 1
        return 0

    def _perform_read(self, line: str) -> None:
        text_addr = self.read_text
        parse_addr = self.read_parse
        if text_addr <= 0:
            self._fail("read without a text buffer")
        max_chars = self.mem[text_addr]
        codes: list[int] = []
        for char in line.replace("\t", " "):
            code = ord(char)
            if 65 <= code <= 90:
                code += 32
            if code == 13 or code == 10:
                break
            if 32 <= code <= 126:
                codes.append(code)
            if len(codes) >= max_chars:
                break
        for index, code in enumerate(codes):
            self.mem[text_addr + 1 + index] = code
        terminator = text_addr + 1 + len(codes)
        if terminator < len(self.mem):
            self.mem[terminator] = 0
        if parse_addr:
            self._tokenize(text_addr, parse_addr, "".join(chr(code) for code in codes))

    def _tokenize(self, text_addr: int, parse_addr: int, text: str) -> None:
        dictionary = self.u16(0x08)
        separator_count = self.mem[dictionary]
        separators = set(self.mem[dictionary + 1 : dictionary + 1 + separator_count])
        max_words = self.mem[parse_addr]
        count = 0
        index = 0
        length = len(text)
        while index < length and count < max_words:
            code = ord(text[index])
            if code == 32:
                index += 1
                continue
            if code in separators:
                self._write_token(parse_addr, count, text[index], index + 1)
                count += 1
                index += 1
                continue
            start = index
            while index < length and ord(text[index]) not in separators and text[index] != " ":
                index += 1
            self._write_token(parse_addr, count, text[start:index], start + 1)
            count += 1
        self.mem[parse_addr + 1] = count

    def _write_token(self, parse_addr: int, count: int, word: str, position: int) -> None:
        found = self.lookup_word(encode_word(word))
        slot = parse_addr + 2 + count * 4
        self.set_u16(slot, found)
        self.mem[slot + 2] = len(word) & 0xFF
        self.mem[slot + 3] = position & 0xFF

    # --- 0OP --------------------------------------------------------------

    def op0_rtrue(self) -> None:
        self.do_return(1)

    def op0_rfalse(self) -> None:
        self.do_return(0)

    def op0_print(self) -> None:
        text, nxt = self.read_zstring(self.pc)
        self.pc = nxt
        self.emit_text(text)

    def op0_print_ret(self) -> None:
        text, nxt = self.read_zstring(self.pc)
        self.pc = nxt
        self.emit_text(text)
        self.emit_zscii(13)
        self.do_return(1)

    def op0_nop(self) -> None:
        return

    def op0_save(self) -> None:
        # Saved PC is the fall-through. Success takes the branch now; a later
        # restore resumes at the fall-through, which the game treats as "restored".
        polarity, offset = self.read_branch()
        self.checkpoint = self.snapshot()
        self.apply_branch(polarity, offset, True)

    def op0_restore(self) -> None:
        self.read_branch()
        if not self.checkpoint:
            return
        kept = self.u16(0x10) & 0x0003
        checkpoint = self.checkpoint
        self.load_snapshot(checkpoint)
        self.checkpoint = checkpoint
        self.set_u16(0x10, (self.u16(0x10) & ~0x0003) | kept)

    def op0_restart(self) -> None:
        self.restart_machine()

    def op0_ret_popped(self) -> None:
        if not self.stack:
            self._fail("stack underflow")
        self.do_return(self.stack.pop())

    def op0_pop(self) -> None:
        if not self.stack:
            self._fail("stack underflow")
        self.stack.pop()

    def op0_quit(self) -> None:
        self.halted = True

    def op0_new_line(self) -> None:
        self.emit_zscii(13)

    def op0_show_status(self) -> None:
        self.refresh_status()

    def op0_verify(self) -> None:
        expected = self.u16(0x1C)
        file_length = self.u16(0x1A) * 2
        total = 0
        end = min(file_length, len(self.original))
        for index in range(0x40, end):
            total = (total + self.original[index]) & 0xFFFF
        self.branch(total == expected)

    def op0_extended(self) -> None:
        self._fail("extended opcodes are not used in version 3")

    def op0_piracy(self) -> None:
        self.branch(True)

    # --- 1OP --------------------------------------------------------------

    def op1_jz(self, ops: list[int]) -> None:
        self.branch((ops[0] & 0xFFFF) == 0)

    def op1_get_sibling(self, ops: list[int]) -> None:
        sibling = self._relation(ops[0], 5)
        self.store_result(sibling)
        self.branch(sibling != 0)

    def op1_get_child(self, ops: list[int]) -> None:
        child = self._relation(ops[0], 6)
        self.store_result(child)
        self.branch(child != 0)

    def op1_get_parent(self, ops: list[int]) -> None:
        self.store_result(self._relation(ops[0], 4))

    def op1_get_prop_len(self, ops: list[int]) -> None:
        address = ops[0] & 0xFFFF
        if address == 0:
            self.store_result(0)
            return
        size = self.mem[address - 1]
        self.store_result((size >> 5) + 1)

    def op1_inc(self, ops: list[int]) -> None:
        self._inc_dec(ops[0], 1)

    def op1_dec(self, ops: list[int]) -> None:
        self._inc_dec(ops[0], -1)

    def _inc_dec(self, variable: int, delta: int) -> int:
        variable &= 0xFF
        value = s16(self.read_var(variable, indirect=True)) + delta
        stored = value & 0xFFFF
        self.write_var(variable, stored, indirect=True)
        return s16(stored)

    def op1_print_addr(self, ops: list[int]) -> None:
        text, _ = self.read_zstring(ops[0] & 0xFFFF)
        self.emit_text(text)

    def op1_call_1s(self, ops: list[int]) -> None:
        self._fail("call_1s is not used in version 3")

    def op1_remove_obj(self, ops: list[int]) -> None:
        self._remove(ops[0])

    def op1_print_obj(self, ops: list[int]) -> None:
        self.emit_text(self.short_name(ops[0]))

    def op1_ret(self, ops: list[int]) -> None:
        self.do_return(ops[0])

    def op1_jump(self, ops: list[int]) -> None:
        self.pc += s16(ops[0]) - 2

    def op1_print_paddr(self, ops: list[int]) -> None:
        text, _ = self.read_zstring((ops[0] & 0xFFFF) * 2)
        self.emit_text(text)

    def op1_load(self, ops: list[int]) -> None:
        self.store_result(self.read_var(ops[0], indirect=True))

    def op1_not_(self, ops: list[int]) -> None:
        self.store_result(~ops[0])

    # --- 2OP --------------------------------------------------------------

    def op2_je(self, ops: list[int]) -> None:
        first = ops[0] & 0xFFFF if ops else 0
        self.branch(any((value & 0xFFFF) == first for value in ops[1:]))

    def op2_jl(self, ops: list[int]) -> None:
        self.branch(s16(ops[0]) < s16(ops[1]))

    def op2_jg(self, ops: list[int]) -> None:
        self.branch(s16(ops[0]) > s16(ops[1]))

    def op2_dec_chk(self, ops: list[int]) -> None:
        value = self._inc_dec(ops[0], -1)
        self.branch(value < s16(ops[1]))

    def op2_inc_chk(self, ops: list[int]) -> None:
        value = self._inc_dec(ops[0], 1)
        self.branch(value > s16(ops[1]))

    def op2_jin(self, ops: list[int]) -> None:
        self.branch(self._relation(ops[0], 4) == self._obj(ops[1]))

    def op2_test(self, ops: list[int]) -> None:
        bitmap = ops[0] & 0xFFFF
        flags = ops[1] & 0xFFFF
        self.branch((bitmap & flags) == flags)

    def op2_or_(self, ops: list[int]) -> None:
        self.store_result(ops[0] | ops[1])

    def op2_and_(self, ops: list[int]) -> None:
        self.store_result(ops[0] & ops[1])

    def op2_test_attr(self, ops: list[int]) -> None:
        self.branch(self._attr(ops[0], ops[1] & 0xFF))

    def op2_set_attr(self, ops: list[int]) -> None:
        self._attr(ops[0], ops[1] & 0xFF, True)

    def op2_clear_attr(self, ops: list[int]) -> None:
        self._attr(ops[0], ops[1] & 0xFF, False)

    def op2_store(self, ops: list[int]) -> None:
        self.write_var(ops[0], ops[1], indirect=True)

    def op2_insert_obj(self, ops: list[int]) -> None:
        self._insert(ops[0], ops[1])

    def op2_loadw(self, ops: list[int]) -> None:
        self.store_result(self.u16((ops[0] + 2 * ops[1]) & 0xFFFF))

    def op2_loadb(self, ops: list[int]) -> None:
        self.store_result(self.mem[(ops[0] + ops[1]) & 0xFFFF])

    def op2_get_prop(self, ops: list[int]) -> None:
        self.store_result(self._get_prop(ops[0], ops[1]))

    def op2_get_prop_addr(self, ops: list[int]) -> None:
        prop = ops[1] & 0xFFFF
        for number, _length, data in self._walk_props(ops[0]):
            if number == prop:
                self.store_result(data)
                return
        self.store_result(0)

    def op2_get_next_prop(self, ops: list[int]) -> None:
        prop = ops[1] & 0xFFFF
        found = prop == 0
        for number, _length, _data in self._walk_props(ops[0]):
            if found:
                self.store_result(number)
                return
            if number == prop:
                found = True
        self.store_result(0)

    def op2_add(self, ops: list[int]) -> None:
        self.store_result(ops[0] + ops[1])

    def op2_sub(self, ops: list[int]) -> None:
        self.store_result(ops[0] - ops[1])

    def op2_mul(self, ops: list[int]) -> None:
        self.store_result(s16(ops[0]) * s16(ops[1]))

    def op2_div(self, ops: list[int]) -> None:
        quotient, _remainder = _trunc_div(ops[0], ops[1])
        self.store_result(quotient)

    def op2_mod(self, ops: list[int]) -> None:
        _quotient, remainder = _trunc_div(ops[0], ops[1])
        self.store_result(remainder)

    def op2_call_2s(self, ops: list[int]) -> None:
        self._fail("call_2s is not used in version 3")

    def op2_call_2n(self, ops: list[int]) -> None:
        self._fail("call_2n is not used in version 3")

    def op2_set_colour(self, ops: list[int]) -> None:
        return

    def op2_throw(self, ops: list[int]) -> None:
        self._fail("throw is not used in version 3")

    # --- VAR --------------------------------------------------------------

    def opv_call(self, ops: list[int]) -> None:
        store = self.mem[self.pc]
        self.pc += 1
        self.do_call(ops, store)

    def opv_storew(self, ops: list[int]) -> None:
        address = (ops[0] + 2 * ops[1]) & 0xFFFF
        if address + 1 < self.static_base:
            self.set_u16(address, ops[2] if len(ops) > 2 else 0)

    def opv_storeb(self, ops: list[int]) -> None:
        address = (ops[0] + ops[1]) & 0xFFFF
        if address < self.static_base:
            self.mem[address] = (ops[2] if len(ops) > 2 else 0) & 0xFF

    def opv_put_prop(self, ops: list[int]) -> None:
        self._put_prop(ops[0], ops[1], ops[2] if len(ops) > 2 else 0)

    def opv_read(self, ops: list[int]) -> None:
        self.refresh_status()
        self.read_text = ops[0] if ops else 0
        self.read_parse = ops[1] if len(ops) > 1 else 0
        self.waiting = True

    def opv_print_char(self, ops: list[int]) -> None:
        self.emit_zscii(ops[0] & 0xFFFF)

    def opv_print_num(self, ops: list[int]) -> None:
        self.emit_text(str(s16(ops[0])))

    def opv_random(self, ops: list[int]) -> None:
        span = s16(ops[0]) if ops else 0
        if span > 0:
            self.rng = (1103515245 * self.rng + 12345) & 0x7FFFFFFF
            value = (self.rng % span) + 1
        elif span < 0:
            self.rng = (-span) & 0x7FFFFFFF or 1
            value = 0
        else:
            self.rng = 1
            value = 0
        self.store_result(value)

    def opv_push(self, ops: list[int]) -> None:
        self.stack.append((ops[0] if ops else 0) & 0xFFFF)

    def opv_pull(self, ops: list[int]) -> None:
        if not self.stack:
            self._fail("stack underflow")
        value = self.stack.pop()
        self.write_var(ops[0] if ops else 0, value, indirect=True)

    def opv_split_window(self, ops: list[int]) -> None:
        return

    def opv_set_window(self, ops: list[int]) -> None:
        return

    def opv_call_vs2(self, ops: list[int]) -> None:
        self._fail("call_vs2 is not used in version 3")

    def opv_erase_window(self, ops: list[int]) -> None:
        return

    def opv_erase_line(self, ops: list[int]) -> None:
        return

    def opv_set_cursor(self, ops: list[int]) -> None:
        return

    def opv_get_cursor(self, ops: list[int]) -> None:
        return

    def opv_set_text_style(self, ops: list[int]) -> None:
        return

    def opv_buffer_mode(self, ops: list[int]) -> None:
        return

    def opv_output_stream(self, ops: list[int]) -> None:
        if not ops:
            return
        number = s16(ops[0])
        if number == 0:
            return
        stream = abs(number)
        enabled = number > 0
        if stream == 1:
            self.stream1 = enabled
        elif stream == 2:
            self.stream2 = enabled
            flags = self.u16(0x10)
            self.set_u16(0x10, flags | 0x0001 if enabled else flags & ~0x0001)
        elif stream == 3:
            if enabled and len(ops) > 1:
                table = ops[1] & 0xFFFF
                if table + 1 < self.static_base:
                    self.set_u16(table, 0)
                self.stream3.append([table, 0])
            elif not enabled and self.stream3:
                table, count = self.stream3.pop()
                if table + 1 < self.static_base:
                    self.set_u16(table, count & 0xFFFF)
        elif stream == 4:
            return

    def opv_input_stream(self, ops: list[int]) -> None:
        return

    def opv_sound_effect(self, ops: list[int]) -> None:
        return
