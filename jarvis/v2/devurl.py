"""Dev servers announced in the owner's HUD terminals (BR-8a; Browser plan
amendment 1 §D.1, decisions B-2).

When `npm run dev` prints `Local: http://localhost:5173/` in a HUD terminal,
the window that shows that terminal is told, so it can open the page in a
Browser pane beside it (BR-8b; amendment §D.2). This module is the pure half:
bytes of terminal output in, cleaned loopback URLs out. `terminals.py` feeds it
from `Terminal._output` (under the lock that function already holds) and sends
what it finds as one `{"type": "dev_url", "url": …}` frame on that terminal's
own attached socket — nothing else ever sees it.

**What is read, and what is kept** (all of it in memory, nothing anywhere else):

* **Only a command's output.** When the shell marks its prompts (OSC 133,
  signed with the terminal's nonce — `Marks` checks the nonce, and the terminal
  hands this module only the letters of signed marks), the prompt and the
  typed command line are skipped: from an `A` or `B` mark until the `C` mark
  that starts the command, so typing `curl http://localhost:3000` opens
  nothing. A shell that marks prompts but never a command (the POSIX startup
  file marks only `A` and `B`), and bash before its first command, skip from
  the mark to the end of that line instead: what the owner typed ends at the
  Enter. A terminal with no marks at all is read whole.
* **Never the alternate screen** (vim, less, top): the rule `terminal_read`
  follows, read by `terminal_text.alt_toggle`. A prompt mark ends it.
* **A partial line of at most `LINE_CAP` bytes**, cleared at every line end
  and dropped whole if it overflows; at most `CARRY_CAP` bytes of an escape
  sequence a chunk cut in two; the last `RECENT` origins sent (de-duplication)
  and the times of the last `RATE_MAX` sends (rate limit). Nothing else: no
  line text is kept past its end, logged, published or written anywhere.

**How a URL is found.** Escape sequences are stripped by `terminal_text`'s
grammar — its own compiled patterns, not a copy: CSI, OSC, DCS/SOS/PM/APC,
single escapes and their 8-bit C1 forms, a C0 control inside a sequence
executed and the sequence going on, any of it split across chunks. Colour
(Vite colours the port apart from the host) draws nothing; a cursor moved
forward is a space; a cursor moved back, a backspace or a deleted character
spoils the line (what is drawn is no longer what was written); any other
cursor move, CR and the line feeds end it. A line that holds no loopback
host spelling or none of the shapes' words is passed over at once (`_HOSTS`,
`_WORDS`: they cannot change an answer, and keep a flood of other URLs at
C speed); the rest is matched against
`SHAPES` (Vite, Next, Create React App, Angular, webpack-dev-server,
`python -m http.server`, Flask, uvicorn, Django, Puma/rails, Hugo, Jekyll,
Streamlit, Gradio, `php -S`, `serve`, Expo, Jupyter, Astro, Nuxt, Storybook
and the like), and the URL it announces is kept only if `clean` accepts it:

* http or https, an explicit port, no user name or password;
* a loopback host: `localhost`, `127.0.0.0/8`, `[::1]`, `0.0.0.0` or `[::]`
  — **always sent as `localhost`** (the window can frame no other loopback
  name); any other name or address (`Network: http://192.168…`, a public
  host, `foo.localhost`) is refused;
* never one of Jarvis's own ports: `config.control_ports()`, the workshop's
  (8403 and its configured port) and whatever the caller adds (the daemon's
  HUD, API and preview listeners as bound);
* the query and fragment dropped (a Jupyter `?token=` never travels), the
  path percent-encoded.

**Sending is the caller's choice; `may_send` says whether it may, and `sent`
records only a frame that went.** One frame per origin per terminal — loopback
hosts folded, so Puma's `127.0.0.1:3000` and `[::1]:3000` are one server —
among the last `RECENT` sent, at most one per `RATE_GAP_S` and `RATE_MAX` per
`RATE_WINDOW_S`. A URL found while no window is attached, or one the socket
refused, is not recorded: the next time the server announces itself (a
restart) a window that is attached by then hears it. A ring replay never
reaches here: only live output is fed.

**The limits, stated.** A server started in the background (`npm run dev &`)
in bash whose banner arrives while the prompt is up is skipped (it lands
between `B` and `C`); run it in the foreground. Input typed ahead while a
command runs is echoed by the tty inside that command's output, and read as
output. A line a program draws with
cursor addressing (a TUI, tmux) is read row by row, as far as its moves end
lines. Text that only *looks* like a banner (`cat` of a log holding `Local:
http://localhost:3000`) is read as one: the window then judges the URL again
(WP-E's Preview judge) and its own rate limit and de-duplication apply. None
of this is a boundary: the result goes to the owner's own window and nowhere
else, and Jarvis never sees it.
"""
from __future__ import annotations

import collections
import ipaddress
import re
import time
from typing import Callable, Iterable
from urllib.parse import quote, urlsplit

from jarvis import config
from . import terminal_text as _tt

LINE_CAP = 512             # a partial line longer than this is dropped whole
CARRY_CAP = 64             # an unfinished escape sequence longer than this is not one
URL_CAP = 512
RECENT = 4                 # origins remembered per terminal, for de-duplication
RATE_GAP_S = 1.0           # at most one frame per terminal this often
RATE_MAX = 4               # … and this many per RATE_WINDOW_S
RATE_WINDOW_S = 60.0
# Lines that pass both cheap tests and are matched against `SHAPES` in one fed
# chunk (or one mark). A flood of lines that are announcements (`yes "Local:
# http://localhost:3000/"`) costs ~30 µs a line, and only one frame a second
# can go anyway: past the budget, the rest of the chunk is not read.
MATCH_BUDGET = 32

# What the sniffer does with the bytes it is fed (the marks move it).
READ = "read"              # a command's output, or a terminal that marks nothing
SKIP_LINE = "skip-line"    # a prompt, then the typed line: skipped to its line feed
SKIP = "skip"              # a prompt and the typed line, skipped to the C mark

# -- the shapes -----------------------------------------------------------------------

# A URL as it can be written: printable ASCII only, so a box-drawing character
# or a colour reset glued to it ends it.
_URL = r"(?P<url>https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+)"
_IN_PARENS = r"\((?P<url>https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'*+,;=%]+)\)"

# (who prints it, the announcement). Case-insensitive; each captures `url`.
SHAPES: tuple[tuple[str, re.Pattern], ...] = tuple((name, re.compile(pattern, re.I)) for name, pattern in (
    # Vite, Nuxt, Astro ("┃ Local    http…"), Storybook, Create React App,
    # Next.js ("- Local:"), Vue CLI, Gatsby, `serve`, Streamlit ("Local URL:").
    # The label opens the line, after nothing but decoration.
    ("local", r"^[^\w]*local(?:\s+url)?\s*:?\s+" + _URL),
    ("next (older)", r"\bstarted server on\b.*?\burl:\s*" + _URL),
    ("angular", r"\bopen your browser on\s+" + _URL),
    ("webpack-dev-server", r"\bloopback:\s*" + _URL),
    # webpack-dev-server 3 ("Project is running at"), Parcel ("Server running
    # at"), Docusaurus ("is running at:").
    ("running at", r"\brunning at:?\s+" + _URL),
    ("python -m http.server", r"\bserving http on\b.*?" + _IN_PARENS),
    # Flask (" * Running on"), uvicorn ("Uvicorn running on"), hypercorn,
    # Gradio ("Running on local URL:").
    ("running on", r"\brunning on(?:\s+local\s+url\s*:)?\s+" + _URL),
    ("django", r"\bdevelopment server at\s+" + _URL),
    # Puma and rails ("* Listening on"), Deno, many Node servers.
    ("listening on", r"\blistening on:?\s+" + _URL),
    ("hugo", r"\bavailable at\s+" + _URL),
    ("jekyll", r"\bserver address:\s*" + _URL),
    ("php -S", r"\bdevelopment server\s+" + _IN_PARENS),
    ("expo", r"\bwaiting on\s+" + _URL),
    # MkDocs, sphinx-autobuild.
    ("serving on", r"\bserving on\s+" + _URL),
    ("eleventy", r"\bserver at\s+" + _URL),
    # Jupyter's own URL line: nothing but its log prefix (and "or") around a
    # URL on Jupyter's paths, or with its token.
    ("jupyter", r"^\s*(?:\[[^\]]*\]\s*)?(?:or\s+)?(?P<url>https?://[A-Za-z0-9\-._~:\[\]%]+"
                r"(?:/(?:lab|tree)\b[A-Za-z0-9\-._~:/?#@!$&'()*+,;=%]*|/\?token=[A-Za-z0-9\-._~%]*))\s*$"),
))

# Two cheap tests before any shape runs (review of PR #70: a flood of lines
# holding `://` ran up to 17 regexes each and throttled the terminal to ~9
# MB/s). Neither can change an answer:
# - `_HOSTS`: every host `clean` accepts is spelled `localhost` (any ASCII
#   case: `_URL` is ASCII-only), `127.…`, `0.0.0.0`, or in brackets starting
#   `[:` or `[0` (`[::]`, `[::1]`, `[0:0:…:1]`: the first group of `::` and
#   `::1` is zero), so a line holding none of them announces nothing `clean`
#   keeps;
# - `_WORDS`: every row of `SHAPES` needs one of these words, written in its
#   pattern as literal text (same flag, so the same case folding), so a line
#   holding none of them matches no row; `local` only before a space or `:`,
#   as the row needs it (so `localhost` alone is not one). A new row adds its
#   word here and to `_WORD_LITERALS`; devurl_check's `Prefilter` holds them
#   to the table.
# `find_url` asks the two regexes; the sniffer asks `_has_host` and
# `_has_word` first, the same tests as plain substring searches (a regex
# alternation under IGNORECASE scans at ~15 MB/s; these at C speed), once over
# a chunk's whole run of lines and then per line.
_HOSTS = re.compile(r"localhost|127\.|0\.0\.0\.0|\[[0:]", re.I)
_WORDS = re.compile(r"local[\s:]|url:|browser on|loopback|running|serving|development server"
                    r"|listening|available at|server address|waiting on|server at|token=|/lab|/tree",
                    re.I)
_WORD_LITERALS = ("url:", "browser on", "loopback", "running", "serving", "development server",
                  "listening", "available at", "server address", "waiting on", "server at",
                  "token=", "/lab", "/tree")
_LOCAL = re.compile(r"local[\s:]")      # on lowered text: no IGNORECASE, so a literal scan
# The characters outside ASCII that IGNORECASE matches to an ASCII letter of
# `_WORDS`: those whose lowercase is one (İ → i, the Kelvin sign → k) and
# those `re` holds equivalent (ı ≈ i, ſ ≈ s). Folded before lowering, so a
# literal search finds every place the regex would.
_FOLD = str.maketrans({"İ": "i", "ı": "i", "ſ": "s", "K": "k"})


def _has_host(data: bytes) -> bool:
    """`_HOSTS` on bytes, as substring searches (ASCII case only, which is
    all `clean`'s ASCII-only hosts have)."""
    return (b"127." in data or b"0.0.0.0" in data or b"[:" in data or b"[0" in data
            or b"localhost" in data.lower())


def _has_word(text: str) -> bool:
    """`_WORDS`, as substring searches on the folded, lowered text: true
    wherever the regex would match (and possibly elsewhere)."""
    low = (text if text.isascii() else text.translate(_FOLD)).lower()
    return any(word in low for word in _WORD_LITERALS) or _LOCAL.search(low) is not None

_TRAILING = ".,;:!?'\")}>*"
_V4_LOOPBACK = ipaddress.ip_network("127.0.0.0/8")
_V6_LOOPBACK = ipaddress.ip_address("::1")
_PATH_SAFE = "/%:@!$&'()*+,;=-._~"


def jarvis_ports() -> frozenset[int]:
    """Jarvis's own listeners, at their defaults and wherever this process's
    config moved them: the control plane (`config.control_ports()`) and the
    workshop, which the v2 daemon binds as its preview listener."""
    return config.control_ports() | {8403, config.WORKSHOP_PORT}


def clean(raw: str, refused: Iterable[int] = ()) -> str | None:
    """The URL a dev server announced, as the window may open it, or None.
    Loopback http(s) with an explicit port, never a refused one, no user
    name or password; no query or fragment. **The host sent is always
    `localhost`** (review of PR #70): `0.0.0.0` and `[::]` mean "every
    address", and `[::1]` or `127.0.0.2` are hosts the window's frame policy
    cannot name, so sending them would only burn the server's one frame."""
    url = raw
    while url and url[-1] in _TRAILING:
        url = url[:-1]
    if not url or len(url) > URL_CAP:
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or "@" in parts.netloc:
        return None
    host = parts.hostname
    if not host or port is None or not 0 < port < 65536:
        return None
    if host not in ("localhost", "0.0.0.0", "::"):
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return None
        if not ((address.version == 4 and address in _V4_LOOPBACK) or address == _V6_LOOPBACK):
            return None
    if port in refused:
        return None
    path = quote(parts.path or "/", safe=_PATH_SAFE)
    return f"{parts.scheme}://localhost:{port}{path}"


def find_url(line: str, refused: Iterable[int] = ()) -> str | None:
    """The first URL a line announces that `clean` accepts, or None."""
    if _HOSTS.search(line) is None or _WORDS.search(line) is None:
        return None
    for _, shape in SHAPES:
        match = shape.search(line)
        if match is not None:
            url = clean(match.group("url"), refused)
            if url is not None:
                return url
    return None


def origin_key(url: str) -> tuple[str, int]:
    """What de-duplication compares: the scheme and the port. Every host
    `clean` lets through is this machine's loopback and is sent as
    `localhost`, so `localhost:3000`, `127.0.0.1:3000` and `[::1]:3000` are
    one server (Puma prints two of them)."""
    parts = urlsplit(url)
    return parts.scheme, parts.port or 0


# -- the sniffer ----------------------------------------------------------------------

# A run of plain text and whole colour sequences, which draw nothing: it stops
# at a backspace, any other escape, a C1 control, or a `\xc2` the chunk ends on
# (maybe the start of one). One C-level match per run however much colour it
# holds; `_SGR` then drops the colour (review of PR #70: a colour flood ran at
# ~21 MB/s through the terminal, one Python step per sequence).
_PLAIN = re.compile(rb"(?:[^\x08\x1b\xc2]+|\xc2(?=[^\x80-\x9f])|\x1b\[[0-9;:]*m)+")
# C0 controls inside a run that draw nothing here (not TAB, not a line end).
_SILENT = bytes(b for b in range(0x20) if b not in (0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x1B)) + b"\x7f"
_BREAK = re.compile(rb"[\n\x0b\x0c\r]")
_LINEFEED = re.compile(rb"[\n\x0b\x0c]")
# A whole colour sequence, the bulk of most escapes: it draws nothing, so the
# text either side of it is one run (exactly what `terminal_text`'s grammar
# makes of it: a CSI with digit parameters and `m`, no C0 inside).
_SGR = re.compile(rb"\x1b\[[0-9;:]*m")
# CSI finals: what each does to the line being read. SGR, the modes and the
# reports draw nothing (and are not listed).
_FORWARD = frozenset(b"Ca")             # cursor forward: a gap
_SPOILS = frozenset(b"DPX@")            # back, delete, erase or insert characters
_ENDS = frozenset(b"ABEFG`HfduLMST")    # any other move: the line ends there


def _first_number(params: bytes) -> int:
    head = params.split(b";", 1)[0].split(b":", 1)[0]
    return int(head) if head.isdigit() and len(head) < 6 else 0


class DevUrlSniffer:
    """One terminal's: fed its live output (`feed`), told its signed marks
    (`mark`), it returns the loopback URLs announced; `may_send` decides
    which may be sent and `sent` records one that was. Memory: `LINE_CAP` + `CARRY_CAP` bytes, `RECENT` origins and
    `RATE_MAX` times, whatever it is fed. Not thread-safe: the terminal calls
    it under its own lock."""

    def __init__(self, refused: Callable[[], Iterable[int]] = jarvis_ports, *,
                 clock: Callable[[], float] = time.monotonic):
        self._refused = refused
        self._clock = clock
        self._mode = READ
        self._seen_command = False      # a C mark has come: B is skipped to the next C
        self._line = bytearray()
        self._spoiled = False           # skip to the next line end
        self._alt = False
        self._string = False            # inside an OSC/DCS/SOS/PM/APC
        self._bel_ends = False
        self._carry = b""
        self._paused = False
        self._budget = MATCH_BUDGET     # full matches left in this chunk
        self._recent: collections.deque = collections.deque(maxlen=RECENT)
        self._sent: collections.deque = collections.deque(maxlen=RATE_MAX)

    # -- what is held ---------------------------------------------------------------

    def held(self) -> int:
        """Bytes of output held right now (the partial line and the carry)."""
        return len(self._line) + len(self._carry)

    @property
    def mode(self) -> str:
        return self._mode

    # -- the marks ----------------------------------------------------------------------

    def mark(self, kind: bytes) -> list[str]:
        """A signed OSC 133 mark (`A`, `B`, `C` or `D`) at this point of the
        stream. Ends the line being read (output with no newline before the
        prompt still counts) and moves the mode."""
        found: list[str] = []
        self._budget = MATCH_BUDGET
        self._end_line(found)
        if kind == b"C":
            self._seen_command = True
            self._mode = READ
        elif kind == b"D":
            self._mode = READ
        elif kind in (b"A", b"B"):
            self._mode = SKIP if self._seen_command else SKIP_LINE
            if kind == b"A":
                self._alt = False       # a shell prompt is drawn on the normal screen
        return found

    # -- switched off and on ----------------------------------------------------------

    def pause(self) -> None:
        """Output is going by unread (auto-open is off): when feeding starts
        again, nothing half-seen counts."""
        self._paused = True

    def _resume(self) -> None:
        self._paused = False
        self._line.clear()
        self._carry = b""
        self._string = False
        self._alt = False
        self._spoiled = True            # the line it comes back in was not seen whole

    # -- feeding -------------------------------------------------------------------

    def feed(self, data: bytes) -> list[str]:
        """Live output, in order, in chunks of any size. Returns the URLs
        the lines it finished announce (cleaned; not yet admitted)."""
        found: list[str] = []
        if not data:
            return found
        if self._paused:
            self._resume()
        self._budget = MATCH_BUDGET
        buf = self._carry + bytes(data) if self._carry else bytes(data)
        self._carry = b""
        i, n = 0, len(buf)
        while i < n:
            if self._string:
                i = self._skip_string(buf, i)
                continue
            plain = _PLAIN.match(buf, i)
            if plain is not None:
                run = buf[i:plain.end()]
                if b"\x1b" in run:
                    run = _SGR.sub(b"", run)    # every escape in the run starts a whole colour
                if run:
                    self._run(run, found)
                i = plain.end()
                if i >= n:
                    return found
            byte = buf[i]
            if byte == 0xC2 and i == n - 1:
                self._carry = buf[i:]           # maybe a C1 control: decide next time
                return found
            if byte == 0x1B:
                j = self._escape(buf, i, found)
            elif byte == 0x08:
                self._spoil()
                j = i + 1
            else:
                j = self._c1(buf, i, found)
            if j < 0:
                if n - i > CARRY_CAP:
                    j = i + 1                   # too long to be a sequence: drop the introducer
                else:
                    self._carry = buf[i:]
                    return found
            i = j
        return found

    # -- text and lines ---------------------------------------------------------------

    def _run(self, run: bytes, found: list[str]) -> None:
        """Plain text, TABs and line ends: handled in bulk (a flood of short
        lines costs a few C-level searches a chunk, not a step a line)."""
        if self._mode == SKIP or self._alt:
            return
        if self._mode == SKIP_LINE:
            feed = _LINEFEED.search(run)
            if feed is None:
                return
            self._to_read()
            run = run[feed.end():]
            if not run:
                return
        run = run.translate(None, _SILENT)
        last = max(run.rfind(b"\n"), run.rfind(b"\r"), run.rfind(b"\x0b"), run.rfind(b"\x0c"))
        if last < 0:
            self._append(run)
            return
        first = _BREAK.search(run).start()
        self._append(run[:first])
        self._end_line(found)
        if first < last:
            middle = run[first + 1:last]
            # C-level passes over the whole middle first: most floods (logs,
            # other hosts' URLs, a loopback access log) stop here.
            if (b"://" in middle and _has_host(middle)
                    and _has_word(middle.decode("utf-8", "replace"))):
                for line in _BREAK.split(middle):
                    if self._budget <= 0:
                        break
                    if len(line) <= LINE_CAP and b"://" in line:
                        self._match(line, found)
        self._append(run[last + 1:])

    def _append(self, text: bytes) -> None:
        if self._spoiled or not text or self._mode != READ or self._alt:
            return
        if len(self._line) + len(text) > LINE_CAP:
            self._line.clear()
            self._spoiled = True                # dropped whole
            return
        self._line += text.replace(b"\t", b" ")

    def _end_line(self, found: list[str]) -> None:
        if (self._mode == READ and not self._spoiled and not self._alt
                and b"://" in self._line):
            self._match(bytes(self._line), found)
        self._line.clear()
        self._spoiled = False

    def _spoil(self) -> None:
        self._line.clear()
        self._spoiled = True

    def _to_read(self) -> None:
        self._mode = READ
        self._line.clear()
        self._spoiled = False

    def _match(self, line: bytes, found: list[str]) -> None:
        if self._budget <= 0 or not _has_host(line):   # `find_url`'s own tests, cheaply first
            return
        text = line.decode("utf-8", "replace")
        if not _has_word(text):
            return
        self._budget -= 1
        url = find_url(text, set(self._refused()))
        if url is not None and url not in found:
            found.append(url)

    def _control(self, byte: int, found: list[str]) -> None:
        """A C0 control met inside an escape sequence: executed, as xterm does."""
        if byte in (0x0A, 0x0B, 0x0C):
            if self._mode == SKIP_LINE:
                self._to_read()
            else:
                self._end_line(found)
        elif byte == 0x0D:
            self._end_line(found)
        elif byte == 0x08:
            self._spoil()
        elif byte == 0x09:
            self._append(b" ")

    def _execute(self, controls: bytes, found: list[str]) -> None:
        for byte in controls:
            if 0x08 <= byte <= 0x0D:
                self._control(byte, found)

    # -- escape sequences (terminal_text's grammar) -------------------------------------

    def _escape(self, buf: bytes, i: int, found: list[str]) -> int:
        n = len(buf)
        j = _tt._ESC_LEAD.match(buf, i + 1).end()
        if j >= n:
            return -1
        lead, c = buf[i + 1:j], buf[j]
        if c == 0x5B:                                   # [  CSI
            return self._csi(buf, j + 1, lead, found)
        if c in (0x5D, 0x50, 0x58, 0x5E, 0x5F):         # ] P X ^ _  OSC DCS SOS PM APC
            self._execute(lead, found)
            self._string, self._bel_ends = True, c == 0x5D
            return j + 1
        if 0x20 <= c <= 0x7E:
            match = _tt._ESC_BODY.match(buf, j)
            if match is not None:
                self._execute(lead + match.group(1), found)
                self._esc(_tt._DROP_C0.sub(b"", match.group(1)), match.group(2)[0], found)
                return match.end()
            if _tt._ESC_PARTIAL.match(buf, j):
                return -1
            k = _tt._ESC_PREFIX.match(buf, j).end()
            self._execute(lead + buf[j:k], found)
            return k
        self._execute(lead, found)
        return j                                        # ESC ESC, CAN, SUB, ≥0x80: abort, go on

    def _c1(self, buf: bytes, i: int, found: list[str]) -> int:
        c = buf[i + 1]
        if c == 0x9B:                                   # CSI
            return self._csi(buf, i + 2, b"", found)
        if c in (0x9D, 0x90, 0x98, 0x9E, 0x9F):         # OSC DCS SOS PM APC
            self._string, self._bel_ends = True, c == 0x9D
        elif c in (0x84, 0x85):                         # IND, NEL: a line feed
            self._control(0x0A, found)
        elif c == 0x8D:                                 # RI
            self._end_line(found)
        return i + 2

    def _csi(self, buf: bytes, j: int, lead: bytes, found: list[str]) -> int:
        match = _tt._CSI_BODY.match(buf, j)
        if match is not None:
            params, intermediates = match.group(1), match.group(2)
            if lead or _tt._DROP_C0.search(buf, j, match.end()) is not None:
                self._execute(lead + params + intermediates, found)
                params = _tt._DROP_C0.sub(b"", params)
                intermediates = _tt._DROP_C0.sub(b"", intermediates)
            self._dispatch(params, intermediates, match.group(3)[0], found)
            return match.end()
        if _tt._CSI_PARTIAL.match(buf, j):
            return -1
        k = _tt._CSI_PREFIX.match(buf, j).end()         # aborted: the breaking byte goes on
        self._execute(lead + buf[j:k], found)
        return k

    def _skip_string(self, buf: bytes, i: int) -> int:
        """Inside an OSC/DCS/SOS/PM/APC: skip to its end (terminal_text's
        rule). Nothing of it is kept."""
        n = len(buf)
        pos = i
        while True:
            match = _tt._STRING_END.search(buf, pos)
            if match is None:
                if buf.endswith(b"\xc2"):
                    self._carry = buf[-1:]
                return n
            k = match.start()
            byte = buf[k]
            if byte == 0x07:
                if self._bel_ends:
                    self._string = False
                    return k + 1
                pos = k + 1                              # BEL inside a DCS: ignored
                continue
            if byte in (0x18, 0x1A):                     # CAN, SUB: aborted
                self._string = False
                return k + 1
            if byte == 0x1B:
                if k + 1 >= n:
                    self._carry = b"\x1b"
                    return n
                self._string = False
                return k + 2 if buf[k + 1] == 0x5C else k   # ST, or a new escape
            self._string = False
            return k + 2 if buf[k + 1] == 0x9C else k

    def _dispatch(self, params: bytes, intermediates: bytes, final: int, found: list[str]) -> None:
        toggle = _tt.alt_toggle(params, intermediates, final)
        if toggle is not None:
            if toggle and not self._alt:
                self._end_line(found)
                self._alt = True
            elif not toggle and self._alt:
                self._alt = False
                self._line.clear()
                self._spoiled = False
            return
        if intermediates or params[:1] in (b"?", b">", b"<", b"="):
            return                                       # private modes and reports draw nothing
        if final in _FORWARD:
            self._append(b" ")
        elif final == 0x4B:                              # K  erase in line
            if _first_number(params) in (1, 2):
                self._line.clear()                       # what was drawn on it is gone
        elif final == 0x4A:                              # J  erase in display
            if _first_number(params) != 0:
                self._line.clear()
        elif final in _SPOILS:
            self._spoil()
        elif final in _ENDS:
            self._end_line(found)

    def _esc(self, intermediates: bytes, final: int, found: list[str]) -> None:
        if intermediates:
            return                                       # a character set: draws nothing
        if final in (0x44, 0x45):                        # D index, E next line
            self._control(0x0A, found)
        elif final in (0x38, 0x4D):                      # 8 restore cursor, M reverse index
            self._end_line(found)
        elif final == 0x63:                              # c  full reset
            self._end_line(found)
            self._alt = False

    # -- what may be sent -------------------------------------------------------------

    def may_send(self, url: str) -> bool:
        """May `url` go to the attached window now? Asks only: nothing is
        recorded until `sent` says the frame went (review of PR #70: a URL
        recorded before a send that failed was never offered again)."""
        if origin_key(url) in self._recent:
            return False
        now = self._clock()
        if self._sent and now - self._sent[-1] < RATE_GAP_S:
            return False
        if len(self._sent) >= RATE_MAX and now - self._sent[0] < RATE_WINDOW_S:
            return False
        return True

    def sent(self, url: str) -> None:
        """`url` reached the window: its origin joins the last `RECENT`, and
        the send counts against the rate. A URL that could not be sent (no
        window, a socket that refused it) is never recorded, so the server's
        next announcement still can be."""
        self._recent.append(origin_key(url))
        self._sent.append(self._clock())
