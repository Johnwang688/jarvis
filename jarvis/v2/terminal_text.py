"""Plain text from a terminal's output bytes (WP-F; decisions W-2).

`terminal_read` reads the same ring the HUD replays, but the daemon has no
xterm: this module is the small, pure part of one it needs. It turns the
bytes a shell wrote into the lines the terminal's **normal buffer** shows,
and nothing else:

* **Escape sequences are stripped, not drawn**: CSI (`ESC [`), OSC (`ESC ]`,
  ended by BEL or ST), DCS/SOS/PM/APC (`ESC P`/`X`/`^`/`_`, ended by ST),
  single-character escapes (`ESC 7`, `ESC ( B` …), and their 8-bit C1 forms
  (U+0080–U+009F as UTF-8). As in xterm, a C0 control inside an escape or a
  CSI is executed and the sequence goes on (DEL is ignored); CAN and SUB
  abort it, and an ESC or a C1 control inside one aborts it and starts the
  next. Any of them may be split across chunks.
* **The alternate screen is never read.** Everything written while
  `CSI ? 1049 h`, `? 1047 h` or `? 47 h` is in force is skipped — vim, less,
  top — including a session that entered it and never left. The state at the
  start of a ring that has dropped bytes comes from `AltTracker`, which the
  terminal feeds every byte the ring drops; a switch the cut split in two is
  handed over whole (`AltTracker.lead`), so its second half is never read as
  the start of the normal screen.
* **The screen has the terminal's width** (2026-10-10 review): `cols` cells a
  row, cursor motion clamped to the margins, and text that reaches the right
  margin wraps onto the next row exactly as xterm's autowrap does — a
  wrapped row continues its logical line, so a long line reads as one line.
  Rows are capped (`MAX_ROWS`, the oldest go first, as scrollback does), so
  nothing a program prints can make the renderer's work or memory grow past
  a small multiple of the ring.
* **Overwritten lines collapse to what the terminal shows.** CR, backspace,
  tab, cursor motion (up, down, left, right, column, position), erase in line
  and display (`clear` included: `CSI 3 J` drops the scrollback, as xterm.js
  does), insert and delete characters and lines, save and restore cursor. A
  progress bar is its last state. Characters are one cell each (a wide or
  combining character is approximate), and scroll regions are ignored.

Two views come back. `lines` is the screen as drawn, as logical lines: what a
read returns. `ink` is the states lines were in when the cursor left them or
an erase (or the row cap) was about to wipe them — so a command line that was
printed and then cleared off the screen is still seen by `terminal_guard`'s
text fallback, which reads both.
Each line carries the stream offsets of the first and last byte that drew it,
which is how a read's window is matched against the shell-integration spans.
"""
from __future__ import annotations

import codecs
from dataclasses import dataclass
import re

ALT_MODES = frozenset({1049, 1047, 47})
MAX_ROWS = 20_000          # rows kept, scrollback-style; the oldest go first
INK_CAP = 8_000_000        # characters of ink; past it `ink_overflow` says where
INK_LINE = 1024            # characters of one ink snapshot
INK_AROUND = 3             # rows of the logical line either side of the one left

# C0 controls that xterm executes inside an escape sequence without ending it
# (CAN, SUB and ESC end it), and DEL, which it ignores there.
_C0 = rb"\x00-\x17\x19\x1c-\x1f\x7f"
_C0_SET = frozenset(range(0x00, 0x18)) | {0x19, 0x1C, 0x1D, 0x1E, 0x1F, 0x7F}

# A control byte, or a C1 control as UTF-8 (0xC2 is never a continuation
# byte, so this is unambiguous at any position).
_CTRL = re.compile(rb"[\x00-\x1f\x7f]|\xc2[\x80-\x9f]")
_CSI_BODY = re.compile(rb"([\x30-\x3f" + _C0 + rb"]*)([\x20-\x2f" + _C0 + rb"]*)([\x40-\x7e])")
_CSI_PARTIAL = re.compile(rb"[\x30-\x3f" + _C0 + rb"]*[\x20-\x2f" + _C0 + rb"]*\Z")
_CSI_PREFIX = re.compile(rb"[\x30-\x3f" + _C0 + rb"]*[\x20-\x2f" + _C0 + rb"]*")
_ESC_LEAD = re.compile(rb"[" + _C0 + rb"]*")
_ESC_BODY = re.compile(rb"([\x20-\x2f" + _C0 + rb"]*)([\x30-\x7e])")
_ESC_PARTIAL = re.compile(rb"[\x20-\x2f" + _C0 + rb"]*\Z")
_ESC_PREFIX = re.compile(rb"[\x20-\x2f" + _C0 + rb"]*")
_STRING_END = re.compile(rb"[\x07\x18\x1a\x1b]|\xc2[\x80-\x9f]")
_ALT_PARAMS = re.compile(rb"\?[0-9;:]*")
_DROP_C0 = re.compile(rb"[" + _C0 + rb"]")
_SPACES = re.compile(r" {4,}")
CARRY_CAP = 4096           # an unfinished CSI or ESC longer than this is not one

GROUND, STRING = 0, 1


def alt_toggle(params: bytes, intermediates: bytes, final: int) -> bool | None:
    """True/False when a CSI enters/leaves the alternate screen, else None.
    Only the strict DECSET/DECRST shape counts — `?` then digits, `;`, `:`,
    with any C0 controls inside it removed first — the shape `AltTracker`
    looks for."""
    if final not in (0x68, 0x6C) or b"?" not in params:
        return None
    params, intermediates = _DROP_C0.sub(b"", params), _DROP_C0.sub(b"", intermediates)
    if intermediates or not _ALT_PARAMS.fullmatch(params):
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
    alternate-screen switches exactly as `Renderer` does: an `ESC [ ?` (any
    C0 controls interleaved, which xterm executes and goes on) or C1 CSI `?`,
    digits, `;` or `:`, then `h` or `l`. In the escape grammar that text
    cannot sit inside another sequence (an ESC or C1 control ends every
    string), so a regex over the raw bytes is the parser's answer.

    A switch whose bytes straddle the last feed is held as `pending`; `lead`
    hands those bytes over, so whoever renders what follows parses the switch
    whole instead of reading its second half as text.
    """

    _SWITCH = re.compile(rb"(?:\x1b[" + _C0 + rb"]*\[|\xc2\x9b)([" + _C0 + rb"]*\?[0-9;:" + _C0
                         + rb"]*)([hl])")
    _TAIL = re.compile(rb"(?:\x1b[" + _C0 + rb"]*(?:\[[" + _C0 + rb"]*(?:\?[0-9;:" + _C0
                       + rb"]*)?)?|\xc2(?:\x9b[" + _C0 + rb"]*(?:\?[0-9;:" + _C0 + rb"]*)?)?)\Z")

    def __init__(self, alt: bool = False):
        self.alt = alt
        self._carry = b""

    @property
    def pending(self) -> bytes:
        """The start of a switch-shaped sequence the last feed ended inside."""
        return self._carry

    def feed(self, data: bytes) -> None:
        if not data:
            return
        buf = self._carry + bytes(data)
        self._carry = b""
        for match in self._SWITCH.finditer(buf):
            state = alt_toggle(match.group(1), b"", match.group(2)[0])
            if state is not None:
                self.alt = state
        tail = self._TAIL.search(buf, max(0, len(buf) - CARRY_CAP))
        if tail is not None and tail.start() < len(buf):
            self._carry = buf[tail.start():]

    def lead(self, data: bytes) -> tuple[bool, bytes]:
        """(the state after `data`, the bytes of a switch still unfinished
        there), without feeding anything: render from `len(lead)` bytes before
        the end of `data`, in that state, and the switch is parsed whole."""
        probe = AltTracker(self.alt)
        probe._carry = self._carry
        probe.feed(data)
        return probe.alt, probe._carry

    def at(self, data: bytes) -> bool:
        """The state after `data` too, without feeding it."""
        return self.lead(data)[0]


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
    # The offset from which ink stopped being kept (INK_CAP), or None.
    ink_overflow: int | None = None


class _Screen:
    """The normal buffer: rows of `cols` cells, a cursor, xterm's autowrap,
    and a record of the states lines were in before they changed."""

    def __init__(self, cols: int, rows: int, offset: int):
        self.cols = max(2, min(int(cols), 1000))
        self.rows = max(1, min(int(rows), 500))
        self.lines: list[str] = [""]
        self.first: list[int] = [offset]
        self.last: list[int] = [offset]
        self.wrapped: list[bool] = [False]       # this row continues the row above
        self.row = 0
        self.col = 0
        self.pending = False                     # at the right margin: the next glyph wraps
        self.dirty: set[int] = set()
        self.ink: list[Line] = []
        self.ink_size = 0
        self.ink_overflow: int | None = None
        self.saved: tuple[int, int] | None = None
        self.dropped = 0                         # rows let go so far (indices shift by this)
        # The cursor has come to this row and not changed it yet: the first
        # change records what an earlier visit left there (lazy ink — a row
        # nobody comes back to change is still on screen, and read there).
        self.fresh = True

    # -- bookkeeping ------------------------------------------------------------------

    def top(self) -> int:
        return max(0, len(self.lines) - self.rows)

    def ensure(self, row: int, offset: int) -> None:
        while len(self.lines) <= row:
            self.lines.append("")
            self.first.append(offset)
            self.last.append(offset)
            self.wrapped.append(False)
        if len(self.lines) > MAX_ROWS + 1000:
            self.drop(len(self.lines) - MAX_ROWS)

    def drop(self, k: int) -> None:
        """The oldest `k` rows leave, as scrollback does; the ink keeps them."""
        k = min(k, max(0, len(self.lines) - 1))
        if k <= 0:
            return
        for row in sorted(r for r in self.dirty if r < k):
            self.snap(row)
        del self.lines[:k], self.first[:k], self.last[:k], self.wrapped[:k]
        self.dropped += k
        self.wrapped[0] = False
        self.dirty = {r - k for r in self.dirty if r >= k}
        self.row = max(0, self.row - k)
        if self.saved is not None:
            self.saved = (max(0, self.saved[0] - k), self.saved[1])

    def _logical(self, row: int, around: int) -> tuple[int, int]:
        lo = row
        while lo > 0 and self.wrapped[lo] and row - lo < around:
            lo -= 1
        hi = row
        while hi + 1 < len(self.lines) and self.wrapped[hi + 1] and hi - row < around:
            hi += 1
        return lo, hi

    def _joined(self, lo: int, hi: int) -> str:
        if lo == hi:
            return self.lines[lo]
        parts = [self.lines[r].ljust(self.cols) if r < hi else self.lines[r] for r in range(lo, hi + 1)]
        return "".join(parts)

    def snap(self, row: int) -> None:
        if row not in self.dirty:
            return
        self.dirty.discard(row)
        if not 0 <= row < len(self.lines) or self.ink_overflow is not None:
            return
        lo, hi = self._logical(row, INK_AROUND)
        text = _SPACES.sub("   ", self._joined(lo, hi).strip())[:INK_LINE]
        if not text:
            return
        self.ink.append(Line(text, min(self.first[lo:hi + 1]), max(self.last[lo:hi + 1])))
        self.ink_size += len(text)
        if self.ink_size > INK_CAP:
            self.ink_overflow = self.last[row]

    def snap_all(self) -> None:
        for row in sorted(self.dirty):
            self.snap(row)

    def changing(self) -> None:
        """The current row is about to change: keep what an earlier visit
        left on it."""
        if self.fresh:
            self.snap(self.row)
            self.fresh = False

    def touch(self, row: int, a: int, b: int) -> None:
        if a < self.first[row]:
            self.first[row] = a
        if b > self.last[row]:
            self.last[row] = b
        self.dirty.add(row)

    def move(self, row: int, col: int, offset: int) -> None:
        """Put the cursor at (row, col), clamped to the margins."""
        row = max(0, row)
        if row != self.row:
            self.fresh = True
        before = self.dropped
        self.ensure(row, offset)
        row -= self.dropped - before
        self.row = max(0, min(row, len(self.lines) - 1))
        self.col = max(0, min(col, self.cols - 1))
        self.pending = False

    def linefeed(self, offset: int, col: int | None = None) -> None:
        self.move(self.row + 1, self.col if col is None else col, offset)

    # -- drawing -----------------------------------------------------------------------

    def _wrap(self, offset: int) -> None:
        """xterm's autowrap: the next row continues this logical line."""
        self.ensure(self.row + 1, offset)       # may let old rows go: self.row follows
        nxt = min(self.row + 1, len(self.lines) - 1)
        self.wrapped[nxt] = True
        self.row, self.col, self.pending = nxt, 0, False
        self.fresh = True

    def write(self, text: str, a: int, b: int) -> None:
        cols = self.cols
        while text:
            if self.pending:
                self._wrap(a)
            if self.fresh:
                self.changing()
            row, col = self.row, self.col
            piece, text = text[:cols - col], text[cols - col:]
            line = self.lines[row]
            if col == len(line):
                line += piece
            elif col > len(line):
                line += " " * (col - len(line)) + piece
            else:
                line = line[:col] + piece + line[col + len(piece):]
            self.lines[row] = line
            self.touch(row, a, b)
            col += len(piece)
            if col >= cols:
                self.col, self.pending = cols - 1, True
            else:
                self.col = col

    def erase_line(self, mode: int, offset: int) -> None:
        row, col = self.row, self.col
        self.snap(row)
        self.fresh = False
        line = self.lines[row]
        if mode == 0:
            line = line[:col]
        elif mode == 1:
            line = (" " * min(col + 1, len(line)) + line[col + 1:]).rstrip(" ")
        else:
            line = ""
        self.lines[row] = line
        self.pending = False
        self.touch(row, offset, offset)

    def erase_display(self, mode: int, offset: int) -> None:
        top = self.top()
        if mode == 3:
            # The scrollback goes (xterm.js); the screen stays.
            self.drop(top)
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
        self.fresh = False
        n = max(1, min(n, self.cols - col))
        line = self.lines[row]
        if col > len(line):
            line += " " * (col - len(line))
        if kind == "X":          # erase n characters
            line = line[:col] + " " * min(n, max(0, len(line) - col)) + line[col + n:]
        elif kind == "P":        # delete n characters
            line = line[:col] + line[col + n:]
        else:                    # "@": insert n blanks; the row keeps its width
            line = (line[:col] + " " * n + line[col:])[:self.cols]
        self.lines[row] = line
        self.pending = False
        self.touch(row, offset, offset)

    def lines_op(self, kind: str, n: int, offset: int) -> None:
        self.snap_all()
        n = max(1, min(n, self.rows))
        row = self.row
        if kind == "L":          # insert n blank lines at the cursor row
            for _ in range(n):
                self.lines.insert(row, "")
                self.first.insert(row, offset)
                self.last.insert(row, offset)
                self.wrapped.insert(row, False)
        else:                    # "M": delete n lines at the cursor row
            end = min(len(self.lines), row + n)
            del self.lines[row:end], self.first[row:end], self.last[row:end], self.wrapped[row:end]
            if not self.lines:
                self.lines, self.first, self.last, self.wrapped = [""], [offset], [offset], [False]
            self.ensure(row, offset)
            self.row = min(self.row, len(self.lines) - 1)
        self.wrapped[0] = False
        self.dirty = set()
        self.pending = False
        if len(self.lines) > MAX_ROWS + 1000:
            self.drop(len(self.lines) - MAX_ROWS)

    def reset(self, offset: int) -> None:
        self.snap_all()
        self.lines, self.first, self.last, self.wrapped = [""], [offset], [offset], [False]
        self.row = self.col = 0
        self.pending = False
        self.dirty = set()
        self.saved = None

    def result(self) -> tuple[list[Line], list[Line]]:
        # No snapshot of what is still on screen: `lines` is that state, and a
        # reader of ink reads `lines` too.
        self.dirty = set()
        lines: list[Line] = []
        r, n = 0, len(self.lines)
        while r < n:
            hi = r
            while hi + 1 < n and self.wrapped[hi + 1]:
                hi += 1
            text = self._joined(r, hi).rstrip(" ")
            lines.append(Line(text, min(self.first[r:hi + 1]), max(self.last[r:hi + 1])))
            r = hi + 1
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
    and `cols`/`rows` the terminal's size (cursor positioning is relative to
    the top of the screen; text wraps at the right margin)."""

    def __init__(self, start: int = 0, *, alt: bool = False, rows: int = 24, cols: int = 80):
        self.offset = start
        self.alt = alt
        self._screen = _Screen(cols, rows, start)
        self._saved_alt: tuple[int, int] | None = None
        self._state = GROUND
        self._bel_ends = False           # the open string is an OSC (BEL ends it)
        self._carry = b""
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._decoding = False           # the decoder may hold part of a character

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
        return Rendered(tuple(lines), tuple(ink), self.offset, self._screen.ink_overflow)

    # -- the parser --------------------------------------------------------------------

    def _execute(self, controls: bytes, offset: int) -> None:
        """C0 controls met inside a sequence: xterm executes them in place."""
        for byte in controls:
            if byte in _C0_SET and byte != 0x7F:
                self._control(byte, offset)

    def _escape(self, buf: bytes, i: int, base: int) -> int:
        """At an ESC: the index after what it introduced, or -1 when the
        sequence is not finished in `buf`."""
        n = len(buf)
        j = _ESC_LEAD.match(buf, i + 1).end()          # C0s executed in the escape state
        if j >= n:
            return -1
        lead, c = buf[i + 1:j], buf[j]
        if c == 0x5B:                                   # [  CSI
            k = self._csi(buf, j + 1, base, i, lead)
            return k
        if c == 0x5D:                                   # ]  OSC
            self._execute(lead, base + i)
            self._state, self._bel_ends = STRING, True
            return j + 1
        if c in (0x50, 0x58, 0x5E, 0x5F):               # P X ^ _  DCS SOS PM APC
            self._execute(lead, base + i)
            self._state, self._bel_ends = STRING, False
            return j + 1
        if 0x20 <= c <= 0x7E:
            match = _ESC_BODY.match(buf, j)
            if match is not None:
                self._execute(lead + match.group(1), base + i)
                self._esc(_DROP_C0.sub(b"", match.group(1)), match.group(2)[0], base + i)
                return match.end()
            if _ESC_PARTIAL.match(buf, j):
                return -1
            k = _ESC_PREFIX.match(buf, j).end()         # aborted at the byte that broke it
            self._execute(lead + buf[j:k], base + i)
            return k
        self._execute(lead, base + i)
        return j                                        # ESC ESC, CAN, SUB, ≥0x80: abort, go on

    def _c1(self, buf: bytes, i: int, base: int) -> int:
        c = buf[i + 1]
        if c == 0x9B:
            return self._csi(buf, i + 2, base, i, b"")
        if c == 0x9D:
            self._state, self._bel_ends = STRING, True
        elif c in (0x90, 0x98, 0x9E, 0x9F):
            self._state, self._bel_ends = STRING, False
        elif not self.alt:
            s = self._screen
            if c == 0x85:                                # NEL
                s.linefeed(base + i, col=0)
            elif c == 0x84:                              # IND
                s.linefeed(base + i)
            elif c == 0x8D:                              # RI
                self._up(1, base + i)
        return i + 2

    def _csi(self, buf: bytes, j: int, base: int, i: int, lead: bytes) -> int:
        match = _CSI_BODY.match(buf, j)
        if match is not None:
            params, intermediates = match.group(1), match.group(2)
            if lead or _DROP_C0.search(buf, j, match.end()) is not None:
                self._execute(lead + params + intermediates, base + i)
                params, intermediates = _DROP_C0.sub(b"", params), _DROP_C0.sub(b"", intermediates)
            self._dispatch(params, intermediates, match.group(3)[0], base + i)
            return match.end()
        if _CSI_PARTIAL.match(buf, j):
            return -1
        k = _CSI_PREFIX.match(buf, j).end()             # aborted: the breaking byte goes on
        self._execute(lead + buf[j:k], base + i)
        return k

    def _string(self, buf: bytes, i: int, base: int) -> int:
        """Inside an OSC/DCS/SOS/PM/APC: skip to its end."""
        n = len(buf)
        pos = i
        while True:
            match = _STRING_END.search(buf, pos)
            if match is None:
                if buf.endswith(b"\xc2"):
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
        if not self._decoding and raw.isascii():
            text = raw.decode("ascii")
        else:
            text = self._decoder.decode(raw)
            self._decoding = True
        if text and not self.alt:
            self._screen.write(text, a, b)

    def _flush(self, offset: int) -> None:
        if not self._decoding:
            return
        self._decoding = False
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
            s.linefeed(offset)
        elif byte == 0x08:
            s.move(s.row, s.col - (0 if s.pending else 1), offset)
        elif byte == 0x09:
            s.col = min(s.cols - 1, (s.col // 8 + 1) * 8)
            s.pending = False

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
            s.linefeed(offset)
        elif final == 0x45:                              # E  next line
            s.linefeed(offset, col=0)
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
                    s = self._screen
                    s.row = min(row, len(s.lines) - 1)
                    s.col = min(col, s.cols - 1)
                    s.pending = False
                    self._saved_alt = None
            return
        if self.alt or intermediates or final == 0x6D or params[:1] in (b"?", b">", b"<", b"="):
            return                                       # m (colours) and the private modes draw nothing
        s = self._screen
        n = _num(params, 0, 1)
        if final == 0x41:                                # A  up
            self._up(n, offset)
        elif final in (0x42, 0x65):                      # B e  down
            s.move(min(len(s.lines) - 1, s.row + n), s.col, offset)
        elif final in (0x43, 0x61):                      # C a  forward
            s.move(s.row, s.col + min(n, s.cols), offset)
        elif final == 0x44:                              # D  back
            s.move(s.row, s.col - min(n, s.cols), offset)
        elif final == 0x45:                              # E  next line
            s.move(min(len(s.lines) - 1, s.row + n), 0, offset)
        elif final == 0x46:                              # F  previous line
            s.move(max(s.top(), s.row - n), 0, offset)
        elif final in (0x47, 0x60):                      # G `  column
            s.move(s.row, min(n, s.cols) - 1, offset)
        elif final == 0x64:                              # d  row
            s.move(s.top() + min(n, s.rows) - 1, s.col, offset)
        elif final in (0x48, 0x66):                      # H f  position
            row = min(_num(params, 0, 1), s.rows)
            s.move(s.top() + row - 1, min(_num(params, 1, 1), s.cols) - 1, offset)
        elif final == 0x4B:                              # K  erase in line
            s.erase_line(_num(params, 0, 0) if params else 0, offset)
        elif final == 0x4A:                              # J  erase in display
            s.erase_display(_num(params, 0, 0) if params else 0, offset)
        elif final in (0x58, 0x50, 0x40):                # X P @
            s.chars(chr(final), n, offset)
        elif final in (0x4C, 0x4D):                      # L M  insert/delete lines
            s.lines_op(chr(final), n, offset)
        elif final == 0x53:                              # S  scroll up: new lines below
            s.ensure(len(s.lines) - 1 + min(n, s.rows), offset)
        elif final == 0x73:                              # s  save cursor
            s.saved = (s.row, s.col)
        elif final == 0x75:                              # u  restore cursor
            if s.saved is not None:
                s.move(min(s.saved[0], len(s.lines) - 1), s.saved[1], offset)


def render(data: bytes, start: int = 0, *, alt: bool = False, rows: int = 24,
           cols: int = 80) -> Rendered:
    """`data` (the stream from offset `start`) as the normal buffer draws it."""
    renderer = Renderer(start, alt=alt, rows=rows, cols=cols)
    renderer.feed(data)
    return renderer.finish()
