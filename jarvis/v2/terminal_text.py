"""Plain text from a terminal's output bytes (WP-F; decisions W-2).

`terminal_read` reads the same ring the HUD replays, but the daemon has no
xterm: this module is the small, pure part of one it needs. It turns the
bytes a shell wrote into the lines the terminal's **normal buffer** shows,
and nothing else:

* **Escape sequences are stripped, not drawn**: CSI (`ESC [`), OSC (`ESC ]`,
  ended by BEL or ST), DCS/SOS/PM/APC (`ESC P`/`X`/`^`/`_`, ended by ST),
  single-character escapes (`ESC 7`, `ESC ( B` …), and their 8-bit C1 forms
  (U+0080–U+009F as UTF-8). CAN and SUB abort a sequence, an ESC or a C1
  control inside one aborts it and starts the next, and a C0 control inside a
  CSI aborts it too (xterm would execute it and go on; here the sequence is
  dropped, so the alternate-screen rule below reads exactly as
  `AltTracker` reads it). Any of them may be split across chunks.
* **The alternate screen is never read.** Everything written while
  `CSI ? 1049 h`, `? 1047 h` or `? 47 h` is in force is skipped — vim, less,
  top — including a session that entered it and never left. The state at the
  start of a ring that has dropped bytes comes from `AltTracker`, which the
  terminal feeds every byte the ring drops.
* **Overwritten lines collapse to what the terminal shows.** CR, backspace,
  tab, cursor motion (up, down, left, right, column, position), erase in line
  and display (`clear` included: `CSI 3 J` drops the scrollback, as xterm.js
  does), insert and delete characters and lines, save and restore cursor. A
  progress bar is its last state. Characters are one cell each (a wide or
  combining character is approximate), and scroll regions are ignored.

Two views come back. `lines` is the screen as drawn: what a read returns.
`ink` is every state a line was in when it was overwritten, erased or left —
a superset, so a command line that was printed and then cleared off the
screen is still seen by `terminal_guard`'s text fallback. Each line carries
the stream offsets of the first and last byte that drew it, which is how a
read's window is matched against the shell-integration spans.
"""
from __future__ import annotations

import codecs
from dataclasses import dataclass
import re

ALT_MODES = frozenset({1049, 1047, 47})

# A control byte, or a C1 control as UTF-8 (0xC2 is never a continuation
# byte, so this is unambiguous at any position).
_CTRL = re.compile(rb"[\x00-\x1f\x7f]|\xc2[\x80-\x9f]")
_CSI_BODY = re.compile(rb"([\x30-\x3f]*)([\x20-\x2f]*)([\x40-\x7e])")
_CSI_PARTIAL = re.compile(rb"[\x30-\x3f]*[\x20-\x2f]*\Z")
_CSI_PREFIX = re.compile(rb"[\x30-\x3f]*[\x20-\x2f]*")
_ESC_BODY = re.compile(rb"([\x20-\x2f]*)([\x30-\x7e])")
_ESC_PARTIAL = re.compile(rb"[\x20-\x2f]*\Z")
_ESC_PREFIX = re.compile(rb"[\x20-\x2f]*")
_STRING_END = re.compile(rb"[\x07\x18\x1a\x1b]|\xc2[\x80-\x9f]")
_ALT_PARAMS = re.compile(rb"\?[0-9;:]*")
CARRY_CAP = 256          # an unfinished CSI or ESC longer than this is not one

GROUND, STRING = 0, 1


def alt_toggle(params: bytes, intermediates: bytes, final: int) -> bool | None:
    """True/False when a CSI enters/leaves the alternate screen, else None.
    Only the strict DECSET/DECRST shape counts — `?` then digits, `;`, `:` —
    the same shape `AltTracker` looks for."""
    if intermediates or final not in (0x68, 0x6C) or not _ALT_PARAMS.fullmatch(params):
        return None
    for part in params[1:].split(b";"):
        head = part.split(b":", 1)[0]
        if head.isdigit() and int(head) in ALT_MODES:
            return final == 0x68
    return None


class AltTracker:
    """Whether the alternate screen is in force after the bytes fed so far.

    Fed every byte a terminal's ring drops, so a read of a ring that has
    lost its beginning still knows whether it starts inside vim. Regular
    expressions only — it runs on the daemon's output path — and it reads
    alternate-screen switches exactly as `Renderer` does: an `ESC [ ?` or
    C1 CSI `?`, digits, `;` or `:`, then `h` or `l`. In the escape grammar
    that text cannot sit inside another sequence (an ESC or C1 control ends
    every string), so a regex over the raw bytes is the parser's answer.
    """

    _SWITCH = re.compile(rb"(?:\x1b\[|\xc2\x9b)(\?[0-9;:]*)([hl])")
    _TAIL = re.compile(rb"(?:\x1b(?:\[(?:\?[0-9;:]*)?)?|\xc2(?:\x9b(?:\?[0-9;:]*)?)?)\Z")

    def __init__(self, alt: bool = False):
        self.alt = alt
        self._carry = b""

    def feed(self, data: bytes) -> None:
        if not data:
            return
        buf = self._carry + bytes(data)
        self._carry = b""
        for match in self._SWITCH.finditer(buf):
            state = alt_toggle(match.group(1), b"", match.group(2)[0])
            if state is not None:
                self.alt = state
        tail = self._TAIL.search(buf, max(0, len(buf) - 64))
        if tail is not None and tail.start() < len(buf):
            self._carry = buf[tail.start():]

    def at(self, data: bytes) -> bool:
        """The state after `data` too, without feeding it."""
        probe = AltTracker(self.alt)
        probe._carry = self._carry
        probe.feed(data)
        return probe.alt


@dataclass(frozen=True)
class Line:
    """One line of text and the stream offsets of the bytes that drew it."""
    text: str
    first: int
    last: int


@dataclass(frozen=True)
class Rendered:
    lines: tuple[Line, ...]
    ink: tuple[Line, ...]
    end: int


class _Screen:
    """The normal buffer: lines without a width, a cursor, and a memory of
    every state a line was in before it changed."""

    def __init__(self, rows: int, offset: int):
        self.rows = max(1, int(rows))
        self.lines: list[str] = [""]
        self.first: list[int] = [offset]
        self.last: list[int] = [offset]
        self.row = 0
        self.col = 0
        self.dirty: set[int] = set()
        self.ink: list[Line] = []
        self.saved: tuple[int, int] | None = None

    # -- bookkeeping ------------------------------------------------------------------

    def top(self) -> int:
        return max(0, len(self.lines) - self.rows)

    def ensure(self, row: int, offset: int) -> None:
        while len(self.lines) <= row:
            self.lines.append("")
            self.first.append(offset)
            self.last.append(offset)

    def snap(self, row: int) -> None:
        if row in self.dirty:
            self.dirty.discard(row)
            if 0 <= row < len(self.lines):
                self.ink.append(Line(self.lines[row], self.first[row], self.last[row]))

    def snap_all(self) -> None:
        for row in sorted(self.dirty):
            self.snap(row)

    def touch(self, row: int, a: int, b: int) -> None:
        if a < self.first[row]:
            self.first[row] = a
        if b > self.last[row]:
            self.last[row] = b
        self.dirty.add(row)

    def move(self, row: int, col: int, offset: int) -> None:
        """Put the cursor at (row, col), keeping the record of the line it
        leaves or may overwrite."""
        row = max(0, row)
        if row != self.row or col < self.col:
            self.snap(self.row)
        self.ensure(row, offset)
        self.row, self.col = row, max(0, col)

    # -- drawing -----------------------------------------------------------------------

    def write(self, text: str, a: int, b: int) -> None:
        if not text:
            return
        row, col = self.row, self.col
        line = self.lines[row]
        if col == len(line):
            line += text
        elif col > len(line):
            line += " " * (col - len(line)) + text
        else:
            line = line[:col] + text + line[col + len(text):]
        self.lines[row] = line
        self.col = col + len(text)
        self.touch(row, a, b)

    def erase_line(self, mode: int, offset: int) -> None:
        row, col = self.row, self.col
        self.snap(row)
        line = self.lines[row]
        if mode == 0:
            line = line[:col]
        elif mode == 1:
            line = " " * min(col + 1, len(line)) + line[col + 1:]
        else:
            line = ""
        self.lines[row] = line.rstrip(" ") if mode != 0 else line
        self.touch(row, offset, offset)

    def erase_display(self, mode: int, offset: int) -> None:
        top = self.top()
        if mode == 3:
            # The scrollback goes (xterm.js); the screen stays.
            self.snap_all()
            if top:
                del self.lines[:top], self.first[:top], self.last[:top]
                self.row = max(0, self.row - top)
                self.dirty = set()
                if self.saved is not None:
                    self.saved = (max(0, self.saved[0] - top), self.saved[1])
            return
        if mode == 0:
            rows = range(self.row + 1, len(self.lines))
            self.erase_line(0, offset)
        elif mode == 1:
            rows = range(top, self.row)
            self.erase_line(1, offset)
        else:
            rows = range(top, len(self.lines))
        for row in rows:
            self.snap(row)
            if self.lines[row]:
                self.lines[row] = ""
                self.touch(row, offset, offset)
                self.dirty.discard(row)

    def chars(self, kind: str, n: int, offset: int) -> None:
        row, col = self.row, self.col
        self.snap(row)
        line = self.lines[row]
        if col > len(line):
            line += " " * (col - len(line))
        if kind == "X":          # erase n characters
            line = line[:col] + " " * min(n, max(0, len(line) - col)) + line[col + n:]
        elif kind == "P":        # delete n characters
            line = line[:col] + line[col + n:]
        else:                    # "@": insert n blanks
            line = line[:col] + " " * n + line[col:]
        self.lines[row] = line
        self.touch(row, offset, offset)

    def lines_op(self, kind: str, n: int, offset: int) -> None:
        self.snap_all()
        n = min(n, 1000)
        row = self.row
        if kind == "L":          # insert n blank lines at the cursor row
            for _ in range(n):
                self.lines.insert(row, "")
                self.first.insert(row, offset)
                self.last.insert(row, offset)
        else:                    # "M": delete n lines at the cursor row
            end = min(len(self.lines), row + n)
            del self.lines[row:end], self.first[row:end], self.last[row:end]
            self.ensure(row, offset)
        self.dirty = set()

    def reset(self, offset: int) -> None:
        self.snap_all()
        self.lines, self.first, self.last = [""], [offset], [offset]
        self.row = self.col = 0
        self.dirty = set()
        self.saved = None

    def result(self) -> tuple[list[Line], list[Line]]:
        self.snap_all()
        lines = [Line(t.rstrip(" "), f, l) for t, f, l in zip(self.lines, self.first, self.last)]
        while lines and not lines[-1].text.strip():
            lines.pop()
        return lines, list(self.ink)


def _num(params: bytes, index: int, default: int) -> int:
    parts = params.split(b";")
    if index >= len(parts):
        return default
    head = parts[index].split(b":", 1)[0]
    if not head.isdigit():
        return default
    value = int(head[:9])
    return value if value > 0 else default


class Renderer:
    """Bytes in, `Rendered` out. Feed it in chunks of any size: a sequence
    or a UTF-8 character split across chunks is finished by the next one.

    `start` is the stream offset of the first byte fed (line offsets are in
    the stream's terms), `alt` whether the alternate screen is in force there,
    and `rows` the terminal's height (cursor positioning is relative to the
    top of the screen)."""

    def __init__(self, start: int = 0, *, alt: bool = False, rows: int = 24):
        self.offset = start
        self.alt = alt
        self._screen = _Screen(rows, start)
        self._saved_alt: tuple[int, int] | None = None
        self._state = GROUND
        self._bel_ends = False           # the open string is an OSC (BEL ends it)
        self._carry = b""
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    # -- feeding -----------------------------------------------------------------------

    def feed(self, data: bytes) -> None:
        if not data:
            return
        buf = self._carry + bytes(data)
        base = self.offset - len(self._carry)
        self.offset += len(data)
        self._carry = b""
        i, n = 0, len(buf)
        while i < n:
            if self._state == STRING:
                i = self._string(buf, i, base)
                continue
            match = _CTRL.search(buf, i)
            stop = match.start() if match is not None else n
            if match is None and buf.endswith(b"\xc2"):
                stop = n - 1             # maybe the start of a C1 control: decide next time
            if stop > i:
                self._text(buf[i:stop], base + i, base + stop)
            if match is None:
                if stop < n:
                    self._carry = buf[stop:]
                return
            i = stop
            self._flush(base + i)
            byte = buf[i]
            if byte == 0x1B:
                j = self._escape(buf, i, base)
            elif byte == 0xC2:
                j = self._c1(buf, i, base)
            else:
                self._control(byte, base + i)
                j = i + 1
            if j < 0:
                if n - i > CARRY_CAP:
                    j = i + 1            # too long to be a sequence: drop the introducer
                else:
                    self._carry = buf[i:]
                    return
            i = j

    def finish(self) -> Rendered:
        self._flush(self.offset)
        lines, ink = self._screen.result()
        return Rendered(tuple(lines), tuple(ink), self.offset)

    # -- the parser --------------------------------------------------------------------

    def _escape(self, buf: bytes, i: int, base: int) -> int:
        """At an ESC: the index after what it introduced, or -1 when the
        sequence is not finished in `buf`."""
        n = len(buf)
        if i + 1 >= n:
            return -1
        c = buf[i + 1]
        if c == 0x5B:                                   # [  CSI
            return self._csi(buf, i + 2, base, i)
        if c == 0x5D:                                   # ]  OSC
            self._state, self._bel_ends = STRING, True
            return i + 2
        if c in (0x50, 0x58, 0x5E, 0x5F):               # P X ^ _  DCS SOS PM APC
            self._state, self._bel_ends = STRING, False
            return i + 2
        if 0x20 <= c <= 0x7E:
            match = _ESC_BODY.match(buf, i + 1)
            if match is not None:
                self._esc(match.group(1), match.group(2)[0], base + i)
                return match.end()
            if _ESC_PARTIAL.match(buf, i + 1):
                return -1
            return _ESC_PREFIX.match(buf, i + 1).end()  # aborted at the byte that broke it
        return i + 1                                    # ESC ESC, ESC + control: abort, go on

    def _c1(self, buf: bytes, i: int, base: int) -> int:
        c = buf[i + 1]
        if c == 0x9B:
            return self._csi(buf, i + 2, base, i)
        if c == 0x9D:
            self._state, self._bel_ends = STRING, True
        elif c in (0x90, 0x98, 0x9E, 0x9F):
            self._state, self._bel_ends = STRING, False
        elif not self.alt:
            if c == 0x85:                                # NEL
                self._screen.move(self._screen.row + 1, 0, base + i)
            elif c == 0x84:                              # IND
                self._screen.move(self._screen.row + 1, self._screen.col, base + i)
            elif c == 0x8D:                              # RI
                self._up(1, base + i)
        return i + 2

    def _csi(self, buf: bytes, j: int, base: int, i: int) -> int:
        match = _CSI_BODY.match(buf, j)
        if match is not None:
            self._dispatch(match.group(1), match.group(2), match.group(3)[0], base + i)
            return match.end()
        if _CSI_PARTIAL.match(buf, j):
            return -1
        return _CSI_PREFIX.match(buf, j).end()          # aborted: the breaking byte goes on

    def _string(self, buf: bytes, i: int, base: int) -> int:
        """Inside an OSC/DCS/SOS/PM/APC: skip to its end."""
        n = len(buf)
        pos = i
        while True:
            match = _STRING_END.search(buf, pos)
            if match is None:
                if buf.endswith(b"\xc2") or buf.endswith(b"\x1b"):
                    self._carry = buf[-1:]
                return n
            k = match.start()
            byte = buf[k]
            if byte == 0x07:
                if self._bel_ends:
                    self._state = GROUND
                    return k + 1
                pos = k + 1                              # BEL inside a DCS: ignored
                continue
            if byte in (0x18, 0x1A):                     # CAN, SUB: aborted
                self._state = GROUND
                return k + 1
            if byte == 0x1B:
                if k + 1 >= n:
                    self._carry = b"\x1b"
                    return n
                self._state = GROUND
                return k + 2 if buf[k + 1] == 0x5C else k   # ST, or a new escape
            # A C1 control: ST ends the string; any other aborts it and goes on.
            self._state = GROUND
            return k + 2 if buf[k + 1] == 0x9C else k

    # -- what the sequences do -----------------------------------------------------------

    def _text(self, raw: bytes, a: int, b: int) -> None:
        text = self._decoder.decode(raw)
        if text and not self.alt:
            self._screen.write(text, a, b)

    def _flush(self, offset: int) -> None:
        rest = self._decoder.decode(b"", True)
        self._decoder.reset()
        if rest and not self.alt:
            self._screen.write(rest, offset, offset)

    def _control(self, byte: int, offset: int) -> None:
        if self.alt:
            return
        s = self._screen
        if byte == 0x0D:
            s.move(s.row, 0, offset)
        elif byte in (0x0A, 0x0B, 0x0C):
            s.move(s.row + 1, s.col, offset)
        elif byte == 0x08:
            s.move(s.row, s.col - 1, offset)
        elif byte == 0x09:
            s.col = (s.col // 8 + 1) * 8

    def _up(self, n: int, offset: int) -> None:
        s = self._screen
        s.move(max(s.top(), s.row - n), s.col, offset)

    def _esc(self, intermediates: bytes, final: int, offset: int) -> None:
        if self.alt or intermediates:
            return
        s = self._screen
        if final == 0x37:                                # 7  save cursor
            s.saved = (s.row, s.col)
        elif final == 0x38:                              # 8  restore cursor
            if s.saved is not None:
                s.move(min(s.saved[0], len(s.lines) - 1), s.saved[1], offset)
        elif final == 0x4D:                              # M  reverse index
            self._up(1, offset)
        elif final == 0x44:                              # D  index
            s.move(s.row + 1, s.col, offset)
        elif final == 0x45:                              # E  next line
            s.move(s.row + 1, 0, offset)
        elif final == 0x63:                              # c  full reset
            s.reset(offset)

    def _dispatch(self, params: bytes, intermediates: bytes, final: int, offset: int) -> None:
        toggle = alt_toggle(params, intermediates, final)
        if toggle is not None:
            if toggle and not self.alt:
                s = self._screen
                self._saved_alt = (s.row, s.col)
                s.snap(s.row)
                self.alt = True
            elif not toggle and self.alt:
                self.alt = False
                if self._saved_alt is not None:
                    row, col = self._saved_alt
                    self._screen.row = min(row, len(self._screen.lines) - 1)
                    self._screen.col = col
                    self._saved_alt = None
            return
        if self.alt or intermediates or params[:1] in (b"?", b">", b"<", b"="):
            return
        s = self._screen
        n = _num(params, 0, 1)
        if final == 0x41:                                # A  up
            self._up(n, offset)
        elif final in (0x42, 0x65):                      # B e  down
            s.move(min(len(s.lines) - 1, s.row + n), s.col, offset)
        elif final in (0x43, 0x61):                      # C a  forward
            s.move(s.row, s.col + n, offset)
        elif final == 0x44:                              # D  back
            s.move(s.row, s.col - n, offset)
        elif final == 0x45:                              # E  next line
            s.move(min(len(s.lines) - 1, s.row + n), 0, offset)
        elif final == 0x46:                              # F  previous line
            s.move(max(s.top(), s.row - n), 0, offset)
        elif final in (0x47, 0x60):                      # G `  column
            s.move(s.row, n - 1, offset)
        elif final == 0x64:                              # d  row
            s.move(s.top() + min(n, s.rows) - 1, s.col, offset)
        elif final in (0x48, 0x66):                      # H f  position
            row = min(_num(params, 0, 1), s.rows)
            s.move(s.top() + row - 1, _num(params, 1, 1) - 1, offset)
        elif final == 0x4B:                              # K  erase in line
            s.erase_line(_num(params, 0, 0) if params else 0, offset)
        elif final == 0x4A:                              # J  erase in display
            s.erase_display(_num(params, 0, 0) if params else 0, offset)
        elif final in (0x58, 0x50, 0x40):                # X P @
            s.chars(chr(final), n, offset)
        elif final in (0x4C, 0x4D):                      # L M  insert/delete lines
            s.lines_op(chr(final), n, offset)
        elif final == 0x53:                              # S  scroll up: new lines below
            s.ensure(len(s.lines) - 1 + min(n, 1000), offset)
        elif final == 0x73:                              # s  save cursor
            s.saved = (s.row, s.col)
        elif final == 0x75:                              # u  restore cursor
            if s.saved is not None:
                s.move(min(s.saved[0], len(s.lines) - 1), s.saved[1], offset)


def render(data: bytes, start: int = 0, *, alt: bool = False, rows: int = 24) -> Rendered:
    """`data` (the stream from offset `start`) as the normal buffer draws it."""
    renderer = Renderer(start, alt=alt, rows=rows)
    renderer.feed(data)
    return renderer.finish()
