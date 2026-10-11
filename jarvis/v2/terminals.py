"""The owner's terminals in the HUD (WP-C; HUD plan §2.4, decisions W-2, W-3, W-5).

Each terminal is the owner's login shell on a real PTY, running as the owner,
unsandboxed and ungated by permissions: the owner's terminal, exactly as
Windows Terminal is. CLAUDE.md keeps terminals out of `DESKTOP_APPS` because
keystrokes from the *agent* into a shell route around the approval gate. This
is the other direction, so the same reasoning becomes the rule here: **the
agent never gets a lever on these terminals.** No tool, MCP tool, fast-path
tool or Discord verb names this module or its routes, and
`tests/v2/terminal_check.py` asserts it the way `archive_check.py` does.

**Who can connect** (the browser does not apply the same-origin policy to a
WebSocket, so the Origin check carries the weight):

- Every route is `projects.owner_only`: it answers only on the HUD's own
  listener (`FACE_PORT`), never the API listener every tool's HTTP client
  uses (8405) and never the preview origin (8403, which serves only
  `/p/...`), and only to a request whose `Origin` is present and equals the
  HUD's own (`http://127.0.0.1:<port>` or `http://localhost:<port>`, matching
  `Host`). A missing Origin, `null` (the sandboxed Preview frame), the
  workshop's 8403 and every other site are refused.
- `Host` must be literally `127.0.0.1:<port>` or `localhost:<port>`
  (`hud_api.check_origin`, and again here), so DNS rebinding cannot reach it.
- The socket also needs a **ticket**: single use, 30 s, valid for one
  terminal, and minted only by an owner-only JSON `POST`, which a page on
  another origin cannot send without a CORS preflight the daemon never
  answers. No cookie is involved (cookies ignore ports, so any page on
  127.0.0.1 could plant one).
- **Taking over** a terminal another window is showing asks in that window
  first ("Let it / Keep it"), and is refused after 20 s with no answer.

**Stricter than `/approvals`, on purpose.** `POST /approvals/{id}` refuses
only a *wrong* Origin, so a local client that sends none is accepted there.
A terminal needs the HUD listener, an Origin that must be present, a ticket,
and the takeover question. For a browser-based attack two independent
checks would have to fail at once.

**Not a boundary against a program running as the owner.** Such a program
(a script an agent wrote and got run, say) can send any Origin it likes,
fetch a ticket, and attach to a terminal no window is showing. What limits
it: an agent has to get that program run first (`curl`/`wget` never
auto-run, Claude workers pass the PreToolUse permit, Codex workers have no
network in their sandbox); a terminal a window is showing asks that window
before it moves; **every attach publishes `terminal_attached`** on the bus
(the terminal's id and the time, nothing else), so the HUD can say when a
terminal was attached by something other than itself; and `sudo` in a HUD
terminal never caches (`alias sudo='sudo -k'`, W-5), so an injected line
cannot ride a `sudo` the owner just typed.

**Lifecycle (W-3, plan decision 12).** The owner's login shell
(`JARVIS_TERMINAL_SHELL`, an absolute path, overrides it; the realpath is
what is checked and launched, under the name it was given as argv[0], so
`rbash` stays restricted), started through util-linux `setsid --ctty`
so it leads its own session with the PTY as its controlling terminal —
never `pty.fork()` in this threaded daemon — with HUP, INT, QUIT, TERM and
TSTP back at their defaults (`env --default-signal`), whatever the daemon
inherited. A clean environment, not the daemon's: nothing from `.env`, no
`OPENROUTER_API_KEY`, `HF_HUB_OFFLINE`, `VIRTUAL_ENV` or venv `PATH`, no
`ANTHROPIC_*`, `CLAUDE_*`, `CODEX_*` or `JARVIS_*`. At most six. Each keeps
1 MiB of output in memory, replayed when a window reattaches. Input goes
through a bounded queue and a writer thread of its own, so a program that
does not read its input never stalls the window's socket; once a frame is
dropped for want of room, the socket refuses all input (a lone Ctrl-C
excepted) until the queue drains and the window sends `input_resume`, so a
paste reaches the program as a prefix, never spliced. Closing one sends
SIGHUP to every process in its session (found in /proc, so background jobs
are included), then SIGKILL; the session's leader is matched on its start
time, so a session whose pid was reused is never signalled. **Terminals end
with the daemon** (`Daemon.stop` closes them all); there is no tmux.

**Output never leaves memory.** It exists in two places: this ring and the
owner's browser. It never goes to the bus (the only record there is
`terminal_attached`: ids and a time), a log (the daemon log gets lifecycle
lines only: opened, attached, exited, closed, with the folder as a project
name or `~`), a session or thread log, Discord, or disk.

**Shell integration (W-2, item 3).** bash reads `terminal_rc.bash` as its
--rcfile; a POSIX `sh`-family shell (sh, dash, ash, ksh, mksh, …) reads
`terminal_rc.sh` as `$ENV`; any other shell (zsh, fish, …) runs as a plain
login shell with **no** startup file — no `sudo -k` and no marks — and the
listing says so (`integration`). The startup file sets `sudo -k` and emits
OSC 133 marks signed with a per-terminal nonce; `Marks` turns them into
`CommandSpan`s over the ring's byte offsets for WP-F's `terminal_read`.
The startup file **never touches disk**: it is written whole into a pipe
whose write end is closed before the shell starts, and the shell reads it as
/dev/fd/<n>, draining it before it runs anything (the POSIX file unsets
`$ENV` too). After that the nonce is only in an unexported shell variable
that PS1 names rather than holds (an exported PS1 carries no nonce), so a
program started in the terminal has no ordinary way to learn it. A program
running as the owner *outside* the terminal could still race the shell to
read that pipe — it would get the nonce and leave the shell with no startup
file at all — which is the same-uid limit stated above, not a new one.
**Spans are advisory, never a
boundary**: a program in the terminal still writes whatever bytes it likes
between the marks; a nested shell, `sudo -i`, `ssh` or `python` puts
everything under the outer command's span; a line kept out of history
records only its first command. WP-F's text-pattern refusals must always
apply, marks or no marks. `readable` is the owner's "Jarvis can read"
switch, on by default; nothing reads it yet.
"""
from __future__ import annotations

import collections
from dataclasses import dataclass, replace
import fcntl
import hmac
import json
import logging
import os
from pathlib import Path
import pwd
import re
import secrets
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from urllib.parse import unquote_to_bytes

from jarvis import config
from .model import utcnow
from . import ws

LOG = logging.getLogger(__name__)

TERMINAL_CAP = 6
RING_BYTES = 1 << 20
TICKET_TTL_S = 30.0
TAKEOVER_TIMEOUT_S = 20.0
CLOSE_GRACE_S = 3.0
STOP_GRACE_S = 1.0
REPLAY_CHUNK = 32 * 1024
INPUT_CAP = 256 * 1024         # queued input a program is not reading, before a paste is refused
MARK_CAP = 16 * 1024           # an OSC 133 longer than this is not one of ours
RC_PIPE_MAX = 8 * 1024         # two pages: what a pipe holds even under pipe-user-pages-soft
SPAN_CAP = 2000
COMMAND_CAP = 4096
CTRL_C = b"\x03"
# The startup files: bash's --rcfile, and $ENV for a POSIX sh-family shell.
RC_PATHS = {"bash": Path(__file__).with_name("terminal_rc.bash"),
            "posix": Path(__file__).with_name("terminal_rc.sh")}
# Shells that read $ENV when interactive. busybox is what /bin/sh resolves to
# on some systems.
POSIX_SHELLS = frozenset({"sh", "dash", "ash", "ksh", "ksh93", "mksh", "posh", "yash", "busybox"})
_RESET_SIGNALS = "HUP,INT,QUIT,TERM,TSTP"
_ID = re.compile(r"[0-9a-f]{8}")

# The environment a terminal starts from: names that pass, and names that
# never do even if they match (belt and braces over the allowlist).
_PASS = frozenset({
    "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "TZ",
    # WSL interop and display, so `explorer.exe .` and GUI apps work.
    "WSL_DISTRO_NAME", "WSL_INTEROP", "WSLENV", "WSL2_GUI_APPS_ENABLED",
    "DISPLAY", "WAYLAND_DISPLAY", "PULSE_SERVER",
    "XDG_RUNTIME_DIR", "XDG_DATA_DIRS", "XDG_CONFIG_DIRS",
})
_PASS_PREFIXES = ("LC_",)
_NEVER = frozenset({"OPENROUTER_API_KEY", "HF_HUB_OFFLINE", "VIRTUAL_ENV", "PYTHONPATH",
                    "PYTHONHOME", "ENV", "BASH_ENV", "PROMPT_COMMAND"})
_NEVER_PREFIXES = ("JARVIS_", "ANTHROPIC_", "CLAUDE_", "CODEX_", "OPENAI_")
_DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class TerminalError(Exception):
    """A request the terminals cannot honour, in a sentence (HTTP 409)."""


def _fail(status, text):
    from .daemon import APIError
    raise APIError(status, text)


# -- the shell and its environment --------------------------------------------------

@dataclass(frozen=True)
class Shell:
    """The shell a terminal runs: `path` is the realpath that is checked and
    executed, `name` what the owner named it, which the shell gets as its
    argv[0] — `/usr/bin/rbash` is a link to bash and must still run
    restricted."""
    path: str
    name: str

    @property
    def kind(self) -> str:
        return shell_kind(self.path, self.name)


def resolve_shell() -> Shell:
    """The owner's login shell, or `JARVIS_TERMINAL_SHELL`. Its realpath is
    what is checked and launched (the Codex resolver's rule); the name it was
    given stays its argv[0]. An override must be absolute (a relative one
    would be resolved in the terminal's folder), and nothing under /mnt/ is
    a Linux shell."""
    override = (config.TERMINAL_SHELL or "").strip()
    if override:
        if not os.path.isabs(override):
            raise TerminalError("JARVIS_TERMINAL_SHELL must be an absolute path")
        given = override
    else:
        try:
            given = pwd.getpwuid(os.getuid()).pw_shell
        except KeyError:
            given = ""
        given = given or "/bin/bash"
    path = os.path.realpath(given)
    if given.startswith("/mnt/") or path.startswith("/mnt/"):
        raise TerminalError("the terminal shell must be a Linux program, not one under /mnt/")
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        raise TerminalError(f"the terminal shell {given} is not an executable file")
    return Shell(path, given)


def shell_kind(path: str, name: str | None = None) -> str:
    """"bash", "posix" (reads $ENV when interactive) or "none" (zsh, fish,
    anything else: no startup file, so no `sudo -k` and no marks). Judged on
    the program and on the name it is called by: bash called `sh` is a POSIX
    shell and ignores --rcfile."""
    real, called = os.path.basename(path), os.path.basename(name or path)
    if real == "bash":
        return "posix" if called == "sh" else "bash"
    return "posix" if real in POSIX_SHELLS or called in POSIX_SHELLS else "none"


def shell_command(shell: str, kind: str, rcfile: str | None) -> tuple[list[str], dict]:
    """argv and extra environment. bash takes `terminal_rc.bash` as its
    --rcfile and a POSIX shell starts interactive with `terminal_rc.sh` as
    $ENV; each file reads the login files itself. Any other shell starts as
    a plain login shell."""
    if kind == "bash":
        return [shell, "--rcfile", rcfile, "-i"], {}
    if kind == "posix":
        return [shell, "-i"], {"ENV": rcfile}
    return [shell, "-l"], {}


_env_support: tuple | None = None


def _env_probe() -> tuple[str | None, bool, bool]:
    """(env, --default-signal works, --argv0 works) for this system's env
    (GNU coreutils, uutils). Probed once."""
    global _env_support
    if _env_support is None:
        env = shutil.which("env", path="/usr/bin:/bin")

        def works(*options):
            try:
                return subprocess.run([env, *options, "--", "true"], capture_output=True,
                                      timeout=5).returncode == 0
            except (OSError, subprocess.SubprocessError):
                return False
        signals = bool(env) and works(f"--default-signal={_RESET_SIGNALS}")
        argv0 = bool(env) and works("--argv0=jarvis-probe")
        if not signals:
            LOG.warning("env --default-signal is unavailable; terminal shells inherit the "
                        "daemon's ignored signals")
        _env_support = (env, signals, argv0)
    return _env_support


def launcher(shell: Shell) -> tuple[list[str], str]:
    """(prefix, program): `env` resetting HUP, INT, QUIT, TERM and TSTP to
    their defaults — a signal the daemon inherited as ignored would stay
    ignored in the shell and every job, and a SIGHUP close would not land —
    and giving the shell its own name as argv[0] while the checked realpath
    is what runs.

    **What runs is always the checked realpath.** An env without --argv0
    cannot give it another name, so there it runs as itself — but only when
    that changes nothing: a name whose basename differs from the program's
    (`rbash` → bash, `sh` → bash or dash, a busybox applet) decides how the
    program behaves, and running it under its own name would run something
    the owner did not name (an unrestricted shell for `rbash`), so that is
    refused. Executing the given name instead, as this used to, ran whatever
    that path resolved to *at exec time*, not the file that was checked."""
    env, signals, argv0 = _env_probe()
    if not argv0 and os.path.basename(shell.name) != os.path.basename(shell.path):
        raise TerminalError(
            f"this system's env cannot set argv[0], and {shell.name} would not run as itself; "
            f"set JARVIS_TERMINAL_SHELL to {shell.path}")
    prefix = []
    if env and (signals or argv0):
        prefix = [env]
        if signals:
            prefix.append(f"--default-signal={_RESET_SIGNALS}")
        if argv0:
            prefix.append(f"--argv0={shell.name}")
        prefix.append("--")
    return prefix, shell.path


def _dotenv_names(path: Path) -> set[str]:
    """The names `.env` defines (config loads them into the daemon's own
    environment). Names only: no value is kept."""
    names = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return names
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            names.add(line.partition("=")[0].strip())
    return names


def _venv_dirs(source) -> list[Path]:
    dirs = []
    if source.get("VIRTUAL_ENV"):
        dirs.append(Path(source["VIRTUAL_ENV"]))
    if sys.prefix != sys.base_prefix:
        dirs.append(Path(sys.prefix))
    return [d.resolve() for d in dirs]


def _clean_path(source) -> str:
    """The daemon's PATH without any Python virtualenv in it, so `python` in
    a terminal is the owner's, never Jarvis's venv."""
    venvs = _venv_dirs(source)
    kept = []
    for entry in (source.get("PATH") or "").split(":"):
        if not entry or entry in kept:
            continue
        try:
            resolved = Path(entry).resolve()
            in_venv = (any(resolved.is_relative_to(v) for v in venvs)
                       or (resolved.parent / "pyvenv.cfg").exists())
        except (OSError, RuntimeError, ValueError):
            continue
        if not in_venv:
            kept.append(entry)
    return ":".join(kept) or _DEFAULT_PATH


def clean_environment(shell: str, *, source=None, env_file: Path | None = None) -> dict:
    """A fresh login environment for `shell`, built from an allowlist of the
    daemon's variables: never anything `.env` defines, no credential or
    Jarvis variable, and a PATH with Jarvis's venv taken out."""
    source = os.environ if source is None else source
    dotenv = _dotenv_names(env_file or config.REPO_ROOT / ".env")
    env = {}
    for name, value in source.items():
        if name in dotenv or name in _NEVER or name.startswith(_NEVER_PREFIXES):
            continue
        if name in _PASS or name.startswith(_PASS_PREFIXES):
            env[name] = value
    try:
        entry = pwd.getpwuid(os.getuid())
        env.setdefault("HOME", entry.pw_dir)
        env.setdefault("USER", entry.pw_name)
        env.setdefault("LOGNAME", entry.pw_name)
    except KeyError:
        env.setdefault("HOME", str(Path.home()))
    env.setdefault("LANG", "C.UTF-8")
    env["SHELL"] = shell
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    env["PATH"] = _clean_path(source)
    return env


# -- sessions in /proc ---------------------------------------------------------------

def _proc_pids() -> list[int]:
    try:
        return [int(name) for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        return []


def _proc_stat(pid: int):
    """(state, session id, start time) from /proc/<pid>/stat, or None."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            stat = handle.read()
    except OSError:
        return None
    fields = stat[stat.rfind(b")") + 2:].split()
    try:
        return fields[0], int(fields[3]), int(fields[19])
    except (IndexError, ValueError):
        return None


def session_members(sid: int, leader_start: int | None) -> list[int]:
    """Every live process in session `sid`, background jobs included — but
    nothing at all once the number `sid` belongs to another process (its
    start time is not the one the shell had): a session id is only kept from
    reuse while some member of the session lives, so after the last one is
    gone a new, unrelated session could carry the same number."""
    leader = _proc_stat(sid)
    if leader is not None and leader_start is not None and leader[2] != leader_start:
        return []
    members = []
    for pid in _proc_pids():
        stat = _proc_stat(pid)
        if stat is not None and stat[1] == sid and stat[0] != b"Z":
            members.append(pid)
    return members


def _winsize(cols: int, rows: int) -> bytes:
    return struct.pack("HHHH", rows, cols, 0, 0)


# -- the ring and the marks -------------------------------------------------------

class Ring:
    """The last `cap` bytes of a terminal's output, in order, with the
    absolute stream offset of the first byte still held (`start`)."""

    def __init__(self, cap: int = RING_BYTES):
        self.cap = cap
        self._chunks: collections.deque[bytes] = collections.deque()
        self._size = 0
        self.start = 0
        self._before = b""                     # the last byte dropped, if any

    @property
    def end(self) -> int:
        return self.start + self._size

    def append(self, data: bytes) -> None:
        if not data:
            return
        data = bytes(data)
        if len(data) >= self.cap:
            if len(data) > self.cap:
                self._before = data[-self.cap - 1:-self.cap]
            elif self._chunks:
                self._before = self._chunks[-1][-1:]
            self.start = self.end + len(data) - self.cap
            self._chunks.clear()
            self._chunks.append(data[-self.cap:])
            self._size = self.cap
            return
        if self._chunks and len(self._chunks[-1]) + len(data) <= 16 * 1024:
            self._chunks[-1] += data           # keep keystroke echoes from making a chunk each
        else:
            self._chunks.append(data)
        self._size += len(data)
        while self._size > self.cap:
            first = self._chunks[0]
            excess = self._size - self.cap
            if len(first) <= excess:
                self._chunks.popleft()
                self._size -= len(first)
                self.start += len(first)
                self._before = first[-1:]
            else:
                self._chunks[0] = first[excess:]
                self._size -= excess
                self.start += excess
                self._before = first[excess - 1:excess]

    def snapshot(self) -> tuple[int, bytes]:
        """(offset, bytes) to replay. Once anything has been dropped and the
        cut did not fall just after a newline, the replay starts just after
        the first newline in its first 64 KiB, so it does not begin
        mid-line; with no newline there it starts at the cut. An escape
        sequence that spans the cut, or that newline, can still be split."""
        data = b"".join(self._chunks)
        start = self.start
        if start > 0 and self._before != b"\n":
            cut = data.find(b"\n", 0, 64 * 1024)
            if cut >= 0:
                data = data[cut + 1:]
                start += cut + 1
        return start, data


@dataclass
class CommandSpan:
    """One command line and the output it produced, as absolute offsets into
    the terminal's output stream: `start` is the first byte after its C mark,
    `end` the first byte of its D mark (None while it runs). Advisory: see
    the module note."""
    command: str
    start: int
    end: int | None = None
    exit_code: int | None = None
    prompt: int | None = None


class Marks:
    """Parses OSC 133 shell-integration marks out of the output stream, across
    chunk boundaries, and keeps the per-command spans. A mark without this
    terminal's nonce is ignored. The nonce is kept from programs started in
    the terminal (the module note says how), but a span is still only
    advisory: a program can write any bytes between real marks."""

    PREFIX = b"\x1b]133;"

    def __init__(self, nonce: str):
        self._nonce = nonce.encode()
        self._carry = b""
        self._offset = 0
        self.prompt: int | None = None            # where the last signed prompt (A) began
        self.spans: collections.deque[CommandSpan] = collections.deque()
        # Bytes before this offset lost their spans to SPAN_CAP: read them as
        # unattributed, whoever printed them.
        self.spans_from = 0
        self.integrated = False
        # The startup file is *running*: a signed prompt mark (A) has been
        # seen. `integration` says what was configured; this says it took — a
        # profile that `exec`s another shell configures "bash" and marks
        # nothing, so no `sudo -k` either.
        self.marked = False

    def feed(self, data: bytes) -> None:
        buf = self._carry + data
        base = self._offset - len(self._carry)
        self._offset += len(data)
        self._carry = b""
        pos = 0
        prefix = self.PREFIX
        while True:
            i = buf.find(prefix, pos)
            if i < 0:
                for k in range(min(len(prefix) - 1, len(buf) - pos), 0, -1):
                    if buf.endswith(prefix[:k]):
                        self._carry = buf[-k:]
                        break
                return
            j = i + len(prefix)
            limit = j + MARK_CAP + 2            # never scan further than a mark can be long
            stops = [x for x in (buf.find(b"\x07", j, limit), buf.find(b"\x1b", j, limit),
                                 buf.find(b"\x18", j, limit), buf.find(b"\x1a", j, limit)) if x >= 0]
            first = min(stops) if stops else -1
            if first < 0 or (buf[first] == 0x1b and first + 1 == len(buf)):
                if len(buf) - j > MARK_CAP:
                    pos = i + 1                 # too long to be ours
                    continue
                self._carry = buf[i:]           # finish it with the next chunk
                return
            if first - j > MARK_CAP:
                pos = i + 1
                continue
            if buf[first] == 0x07:
                end = first + 1
            elif buf[first] == 0x1b and buf[first + 1] == 0x5C:
                end = first + 2
            else:
                pos = first if buf[first] == 0x1b else first + 1   # aborted
                continue
            self._mark(buf[j:first], base + i, base + end)
            pos = end

    def _open(self) -> CommandSpan | None:
        if self.spans and self.spans[-1].end is None:
            return self.spans[-1]
        return None

    def _mark(self, params: bytes, start: int, end: int) -> None:
        fields = params.split(b";")
        kind, values, extras = fields[0], {}, []
        for field in fields[1:]:
            if b"=" in field:
                key, _, value = field.partition(b"=")
                values[key] = value
            else:
                extras.append(field)
        nonce = values.get(b"jarvis")
        if nonce is None or not hmac.compare_digest(nonce, self._nonce):
            return
        open_span = self._open()
        if kind == b"A":
            if open_span is not None:           # a prompt with no D: it ended there
                open_span.end = start
            self.prompt = start
            self.marked = True
        elif kind == b"C":
            if open_span is not None:
                open_span.end = start
            command = unquote_to_bytes(values.get(b"cmdline_url", b"")).decode("utf-8", "replace")
            while len(self.spans) >= SPAN_CAP:
                evicted = self.spans.popleft()
                self.spans_from = max(self.spans_from,
                                      evicted.end if evicted.end is not None else start)
            self.spans.append(CommandSpan(command[:COMMAND_CAP], start=end, prompt=self.prompt))
            self.integrated = True              # output is attributed to commands from here on
        elif kind == b"D" and open_span is not None:
            open_span.end = start
            if extras and re.fullmatch(rb"-?\d{1,6}", extras[0]):
                open_span.exit_code = int(extras[0])

    def prune(self, before: int) -> None:
        """Forget spans whose output has entirely left the ring."""
        while self.spans and self.spans[0].end is not None and self.spans[0].end <= before:
            self.spans.popleft()


@dataclass(frozen=True)
class History:
    """What WP-F's `terminal_read` will read: the ring's bytes from `start`
    and the command spans overlapping them. Bytes before `spans_from` lost
    their spans to the span cap and are unattributed. `integrated`: this
    terminal has marked a command; without it a reader has text patterns
    only. Spans are advisory either way (module note): the text-pattern
    refusals apply to every read. `prompt` is where the last signed prompt
    mark began, None if there was none."""
    start: int
    data: bytes
    spans: tuple
    spans_from: int
    integrated: bool
    readable: bool
    prompt: int | None = None


# -- one terminal --------------------------------------------------------------------

class _Takeover:
    def __init__(self, holder):
        self.id = secrets.token_hex(4)
        self.holder = holder
        self.allow: bool | None = None
        self.event = threading.Event()


class Terminal:
    def __init__(self, tid: str, *, shell: str, folder: str, project_id: str | None,
                 label: str, cols: int, rows: int, nonce: str, integration: str = "none"):
        self.id = tid
        self.shell = shell
        self.folder = folder
        self.project_id = project_id
        self.label = label
        self.integration = integration
        self.title = f"{os.path.basename(shell)} · {label}"
        self.created = utcnow()
        self.cols, self.rows = cols, rows
        self.readable = True
        self.exited = False
        self.exit_code: int | None = None
        self._ring = Ring()
        self._marks = Marks(nonce)
        self._lock = threading.Lock()           # ring, marks, attachment, state
        self._io_lock = threading.RLock()       # the master fd's life
        self._attachment: ws.WebSocket | None = None
        self._takeover: _Takeover | None = None
        self._closing = False
        self._finished = False
        self._master: int | None = None
        self._proc: subprocess.Popen | None = None
        self._start_time: int | None = None     # the shell's, from /proc: guards pid reuse
        self._session_gone = False              # seen empty once: never signalled again
        self._wake_r, self._wake_w = os.pipe()
        self._reader: threading.Thread | None = None
        # Input: queued by the window's socket, written by a thread of its own.
        self._inbox: collections.deque[bytes] = collections.deque()
        self._in_bytes = 0                      # queued plus in flight
        self._in_flight = 0
        self._in_flight_ctrl_c = False
        self._in_gen = 0
        self._in_cond = threading.Condition()
        self._writer: threading.Thread | None = None

    # -- spawning ---------------------------------------------------------------------

    def spawn(self, argv: list[str], env: dict, rc_fd: int | None = None) -> None:
        """Start `argv` (a launcher prefix, the program, its arguments) on a
        new PTY. `rc_fd` is the read end of the pipe holding the startup file:
        the shell inherits it as /dev/fd/<rc_fd>, and this process closes its
        copy at once, so the file exists nowhere but in that pipe."""
        try:
            setsid = shutil.which("setsid", path="/usr/bin:/bin")
            if setsid is None:
                raise TerminalError("util-linux setsid is missing, so no terminal can start")
            master, slave = os.openpty()
            try:
                fcntl.ioctl(master, termios.TIOCSWINSZ, _winsize(self.cols, self.rows))
                if hasattr(termios, "IUTF8"):
                    attrs = termios.tcgetattr(slave)
                    attrs[0] |= termios.IUTF8
                    termios.tcsetattr(slave, termios.TCSANOW, attrs)
                # setsid --ctty: a new session led by the shell, with this PTY
                # as its controlling terminal. The child is never a
                # process-group leader here, so setsid does not fork, and `env`
                # execs in place: the pid is the shell's.
                self._proc = subprocess.Popen([setsid, "--ctty", "--", *argv],
                                              stdin=slave, stdout=slave, stderr=slave,
                                              cwd=self.folder, env=env, close_fds=True,
                                              pass_fds=(rc_fd,) if rc_fd is not None else ())
            except BaseException:
                os.close(master)
                raise
            finally:
                os.close(slave)
        finally:
            if rc_fd is not None:
                os.close(rc_fd)
        stat = _proc_stat(self._proc.pid)       # unreaped, so still ours to read
        self._start_time = stat[2] if stat is not None else None
        os.set_blocking(master, False)
        self._master = master
        self._reader = threading.Thread(target=self._read_loop, name=f"jarvis-terminal-{self.id}",
                                        daemon=True)
        self._writer = threading.Thread(target=self._input_loop,
                                        name=f"jarvis-terminal-in-{self.id}", daemon=True)
        self._reader.start()
        self._writer.start()

    # -- output ---------------------------------------------------------------------

    def _read_loop(self) -> None:
        master = self._master
        while True:
            try:
                ready, _, _ = select.select([master, self._wake_r], [], [], 0.5)
            except (OSError, ValueError):
                break
            if self._wake_r in ready:
                break
            if master in ready:
                try:
                    data = os.read(master, 65536)
                except BlockingIOError:
                    data = None
                except OSError:
                    data = b""                  # EIO: nothing holds the PTY open any more
                if data:
                    self._output(data)
                elif data == b"":
                    self._check_exit(wait=True)
                    break
            self._check_exit()

    def _output(self, data: bytes) -> None:
        with self._lock:
            self._ring.append(data)
            was_marked = self._marks.marked
            self._marks.feed(data)
            self._marks.prune(self._ring.start)
            attached = self._attachment
            if attached is not None and not attached.send_binary(data):
                # Too far behind: dropped. The window reattaches and replays.
                self._attachment = None
                attached = None
            if attached is not None and self._marks.marked and not was_marked:
                # The startup file is running: the window drops its "no
                # integration" note. Sent once, after the bytes that carried it.
                attached.send_text(json.dumps({"type": "marked"}))

    def _check_exit(self, wait: bool = False) -> None:
        proc = self._proc
        if proc is None or self.exited:
            return
        if wait:
            try:
                code = proc.wait(timeout=None if not self._closing else 1.0)
            except subprocess.TimeoutExpired:
                return
        else:
            code = proc.poll()
        if code is None:
            return
        code = code if code >= 0 else 128 - code      # killed by signal n: the shell's 128+n
        with self._lock:
            if self.exited:
                return
            self.exited, self.exit_code = True, code
            attached = self._attachment
            if attached is not None:
                attached.send_text(json.dumps({"type": "exit", "code": code}))
        LOG.info("terminal %s exited (code %d)", self.id, code)

    # -- input and size ------------------------------------------------------------

    def queue_input(self, data: bytes) -> bool:
        """Owner keystrokes and pastes, for the writer thread. Never blocks:
        False when more than INPUT_CAP is already waiting on a program that
        is not reading (the socket then refuses all further input until the
        window resumes it: `Terminals.serve`). A lone Ctrl-C always goes in,
        whatever the cap: it throws away the input still queued — keeping one
        Ctrl-C already waiting, so two quick ones never collapse into one —
        and the paste chunk being written, unless that is a Ctrl-C too."""
        with self._in_cond:
            if self._closing:
                return False
            if data == CTRL_C:
                waiting = CTRL_C in self._inbox
                self._inbox.clear()
                if waiting:
                    self._inbox.append(CTRL_C)
                if self._in_flight and not self._in_flight_ctrl_c:
                    self._in_gen += 1          # the writer abandons the paste chunk in hand
                self._in_bytes = self._in_flight + len(self._inbox)
            elif self._in_bytes + len(data) > INPUT_CAP:
                return False
            self._inbox.append(data)
            self._in_bytes += len(data)
            self._in_cond.notify()
        return True

    def input_drained(self) -> bool:
        """Nothing queued and nothing being written."""
        with self._in_cond:
            return self._in_bytes == 0

    def _input_loop(self) -> None:
        while True:
            with self._in_cond:
                while not self._inbox and not self._closing:
                    self._in_cond.wait()
                if self._closing:
                    return
                data = self._inbox.popleft()
                generation = self._in_gen
                self._in_flight = len(data)
                self._in_flight_ctrl_c = data == CTRL_C
            try:
                self._write(data, lambda: self._in_gen == generation and not self._closing)
            finally:
                with self._in_cond:
                    if self._in_gen == generation:
                        self._in_bytes -= len(data)
                    else:
                        self._in_bytes = max(0, self._in_bytes - self._in_flight)
                    self._in_flight = 0
                    self._in_flight_ctrl_c = False

    def _write(self, data: bytes, keep) -> None:
        view = memoryview(data)
        while view and keep():
            with self._io_lock:
                if self._master is None or self._closing:
                    return
                try:
                    view = view[os.write(self._master, view):]
                except BlockingIOError:
                    pass
                except OSError:
                    return
                master = self._master
            if view:
                try:
                    select.select([], [master], [], 0.25)
                except (OSError, ValueError):
                    return

    def resize(self, cols: int, rows: int) -> None:
        with self._io_lock:
            if self._master is None or self._closing:
                return
            fcntl.ioctl(self._master, termios.TIOCSWINSZ, _winsize(cols, rows))
            self.cols, self.rows = cols, rows

    def busy(self) -> bool:
        """Something other than the shell is in the foreground (the HUD asks
        before closing then)."""
        with self._io_lock:
            if self._master is None or self._proc is None or self.exited:
                return False
            try:
                return os.tcgetpgrp(self._master) != self._proc.pid
            except OSError:
                return False

    def row(self) -> dict:
        return {"id": self.id, "title": self.title, "folder": self.folder,
                "project_id": self.project_id, "created": self.created,
                "cols": self.cols, "rows": self.rows,
                "shown": self._attachment is not None, "exited": self.exited,
                "exit_code": self.exit_code, "readable": self.readable,
                "busy": self.busy(), "integration": self.integration,
                "integrated": self._marks.integrated, "marked": self._marks.marked}

    def history(self) -> History:
        """The ring and its command spans, for WP-F. No route reaches this."""
        with self._lock:
            start, data = self._ring.snapshot()
            spans = tuple(replace(s) for s in self._marks.spans
                          if s.end is None or s.end > start)
            return History(start, data, spans, self._marks.spans_from, self._marks.integrated,
                           self.readable, self._marks.prompt)

    # -- windows ---------------------------------------------------------------------

    def holds(self, sock) -> bool:
        return self._attachment is sock

    def _install(self, sock) -> None:
        """Attach `sock` and replay the ring into it, atomically with output
        (the caller holds `_lock`, which `_output` takes too)."""
        self._attachment = sock
        start, data = self._ring.snapshot()
        sock.send_text(json.dumps({"type": "attached", "terminal": self.row(),
                                   "replay": len(data)}))
        for i in range(0, len(data), REPLAY_CHUNK):
            sock.send_binary(data[i:i + REPLAY_CHUNK])
        sock.send_text(json.dumps({"type": "replayed"}))
        if self.exited:
            sock.send_text(json.dumps({"type": "exit", "code": self.exit_code}))

    def attach(self, sock, timeout: float) -> str | None:
        """Attach a window's socket. None when attached; otherwise why not.
        A terminal another window shows asks that window first."""
        with self._lock:
            if self._closing:
                return "this terminal is closing"
            holder = self._attachment
            if holder is None or holder.closed.is_set():
                self._install(sock)
                LOG.info("terminal %s attached", self.id)
                return None
            if self._takeover is not None:
                return "another window is already asking for this terminal"
            request = self._takeover = _Takeover(holder)
        holder.send_text(json.dumps({"type": "takeover_request", "id": request.id,
                                     "timeout_s": timeout}))
        sock.send_text(json.dumps({"type": "waiting", "timeout_s": timeout}))
        request.event.wait(timeout)
        with self._lock:
            if self._takeover is request:
                self._takeover = None
            if self._closing:
                return "this terminal is closing"
            current = self._attachment
            gone = current is not holder or holder.closed.is_set()
            if request.allow is True or gone:
                if current is holder:
                    self._attachment = None
                    holder.send_text(json.dumps({"type": "taken"}))
                    holder.close(ws.NORMAL, "taken by another window")
                if self._attachment is not None:
                    return "another window has this terminal"
                self._install(sock)
                LOG.info("terminal %s taken over by another window", self.id)
                return None
        LOG.info("terminal %s: takeover refused", self.id)
        if request.allow is False:
            return "the window showing this terminal kept it"
        return f"the window showing this terminal did not answer in {int(timeout)} s"

    def detach(self, sock) -> None:
        with self._lock:
            if self._attachment is sock:
                self._attachment = None
            request = self._takeover
            if request is not None and request.holder is sock:
                request.event.set()             # nobody left to ask

    def control(self, sock, raw: bytes) -> None:
        """A JSON control message from the attached window."""
        try:
            message = json.loads(raw)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        kind = message.get("type")
        if kind == "resize":
            cols, rows = message.get("cols"), message.get("rows")
            if _size_ok(cols, rows):
                self.resize(cols, rows)
        elif kind == "takeover":
            with self._lock:
                request = self._takeover
                if (request is not None and request.holder is sock
                        and message.get("id") == request.id
                        and isinstance(message.get("allow"), bool)):
                    request.allow = message["allow"]
                    request.event.set()

    # -- closing ---------------------------------------------------------------------

    def hangup(self, reason: str) -> None:
        """Tell the window, then SIGHUP every process in the session."""
        with self._lock:
            if self._closing:
                return
            self._closing = True
            attached, self._attachment = self._attachment, None
            request = self._takeover
        with self._in_cond:
            self._in_cond.notify_all()
        if request is not None:
            request.event.set()
        if attached is not None:
            attached.send_text(json.dumps({"type": "exit", "code": self.exit_code,
                                           "reason": reason}))
            attached.close(ws.GOING_AWAY if reason == "ended" else ws.NORMAL, reason)
        for pid in self._members():
            for sig in (signal.SIGHUP, signal.SIGCONT):
                try:
                    os.kill(pid, sig)
                except OSError:
                    pass

    def _members(self) -> list[int]:
        """The session's live processes. Once it has been seen empty it stays
        empty: nothing is signalled after that, whoever has the number now."""
        proc = self._proc
        if proc is None or self._session_gone:
            return []
        members = set(session_members(proc.pid, self._start_time))
        if proc.poll() is None:
            members.add(proc.pid)
        if not members:
            self._session_gone = True
        return sorted(members)

    def alive(self) -> bool:
        return bool(self._members())

    def finish(self) -> None:
        """SIGKILL whatever outlived the hangup, reap the shell, stop the
        threads and release the PTY. Runs once."""
        with self._lock:
            if self._finished:
                return
            self._finished = True
            self._closing = True
        with self._in_cond:
            self._in_cond.notify_all()
        for pid in self._members():
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        if self._proc is not None:
            try:
                self._proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                LOG.warning("terminal %s: the shell did not exit after SIGKILL", self.id)
        try:
            os.write(self._wake_w, b"x")
        except OSError:
            pass
        for thread in (self._reader, self._writer):
            if thread is not None and thread is not threading.current_thread():
                thread.join(1.0)
        with self._io_lock:
            if self._master is not None:
                os.close(self._master)
                self._master = None
        for fd in (self._wake_r, self._wake_w):
            try:
                os.close(fd)
            except OSError:
                pass
        if not self.exited and self._proc is not None and self._proc.returncode is not None:
            code = self._proc.returncode
            self.exited, self.exit_code = True, code if code >= 0 else 128 - code


def _size_ok(cols, rows) -> bool:
    return (type(cols) is int and type(rows) is int and 2 <= cols <= 1000 and 1 <= rows <= 500)


_NOT_READING = ("the program in this terminal is not reading its input; this input was "
                "dropped, and so will all input be until the window sends input_resume")
_LATCHED = "input is paused after a dropped paste; send input_resume to type again"


def _send(sock, kind: str, **fields) -> None:
    sock.send_text(json.dumps({"type": kind, **fields}))


def _control_type(raw: bytes):
    try:
        message = json.loads(raw)
    except ValueError:
        return None
    return message.get("type") if isinstance(message, dict) else None


# -- every terminal ------------------------------------------------------------------

class Terminals:
    """The daemon's terminals, their tickets, and the attach sessions.

    `publish` is the daemon bus's: the only record it is ever handed is
    `terminal_attached` with the terminal's id and the time."""

    def __init__(self, *, clock=time.monotonic, publish=None):
        self._lock = threading.Lock()
        self._terminals: dict[str, Terminal] = {}
        self._tickets: dict[str, tuple[str, float]] = {}
        self._clock = clock
        self._publish = publish
        self._rc_text: dict[str, bytes] = {}
        self._stopped = False

    def _rc_pipe(self, nonce: str, kind: str) -> int:
        """The read end of a pipe holding this terminal's startup file, with
        its nonce: the shell reads it as /dev/fd/<n>, so it never touches
        disk. It is written whole and the write end closed before the shell
        starts, so the shell's first read drains it and anything that opens
        it later reads nothing. Each shipped file is read once per daemon,
        like the code beside it."""
        if kind not in self._rc_text:
            self._rc_text[kind] = RC_PATHS[kind].read_bytes()
        content = f"__jarvis_nonce='{nonce}'\n".encode() + self._rc_text[kind]
        # A pipe holds 64 KiB normally but only two pages once the owner is
        # over `pipe-user-pages-soft`, and nothing reads this one until the
        # shell starts: a blocking write past that would wedge `create()` —
        # and, under `Terminals._lock`, every terminal route — for good. So
        # the file must fit the guaranteed 8 KiB, the write never blocks, and
        # a short write is a refusal, never a truncated startup file.
        if len(content) > RC_PIPE_MAX:
            raise TerminalError("the terminal startup file is too large")
        read, write = os.pipe()
        try:
            os.set_blocking(write, False)
            try:
                written = os.write(write, content)
            except BlockingIOError:
                written = 0
            if written != len(content):
                raise TerminalError("the terminal startup file did not fit in its pipe")
        except BaseException:
            os.close(read)
            raise
        finally:
            os.close(write)
        return read

    def create(self, folder: str, *, project_id: str | None, label: str,
               cols: int = 80, rows: int = 24) -> Terminal:
        if not _size_ok(cols, rows):
            raise ValueError("cols must be 2-1000 and rows 1-500")
        shell = resolve_shell()
        kind = shell.kind
        prefix, program = launcher(shell)
        with self._lock:
            if self._stopped:
                raise TerminalError("Jarvis is stopping")
            if len(self._terminals) >= TERMINAL_CAP:
                raise TerminalError(f"{TERMINAL_CAP} terminals are open; close one first")
            tid = secrets.token_hex(4)
            while tid in self._terminals:
                tid = secrets.token_hex(4)
            nonce = secrets.token_hex(16)
            # The startup file first: a refusal (too large, a short write)
            # then leaves nothing behind but the pipe it closes itself.
            rc_fd = self._rc_pipe(nonce, kind) if kind != "none" else None
            try:
                terminal = Terminal(tid, shell=shell.name, folder=folder, project_id=project_id,
                                    label=label, cols=cols, rows=rows, nonce=nonce, integration=kind)
            except BaseException:
                if rc_fd is not None:
                    os.close(rc_fd)
                raise
            try:
                argv, extra = shell_command(program, kind,
                                            f"/dev/fd/{rc_fd}" if rc_fd is not None else None)
                env = clean_environment(shell.name)
                env.update(extra)
            except BaseException:
                if rc_fd is not None:
                    os.close(rc_fd)
                terminal.finish()               # the wake pipe
                raise
            try:
                terminal.spawn([*prefix, *argv], env, rc_fd)   # closes rc_fd, whatever happens
            except BaseException:
                terminal.finish()               # the wake pipe
                raise
            self._terminals[tid] = terminal
        LOG.info("terminal %s opened in %s", tid, label)
        return terminal

    def get(self, tid: str) -> Terminal:
        if not isinstance(tid, str) or not _ID.fullmatch(tid):
            _fail(400, "terminal id must be eight lowercase hex characters")
        with self._lock:
            terminal = self._terminals.get(tid)
        if terminal is None:
            _fail(404, f"terminal {tid} not found")
        return terminal

    def listing(self) -> list[dict]:
        with self._lock:
            terminals = list(self._terminals.values())
        return [t.row() for t in terminals]

    # -- tickets ---------------------------------------------------------------------

    def _expire(self, now: float) -> None:
        for ticket in [k for k, (_, until) in self._tickets.items() if until <= now]:
            del self._tickets[ticket]

    def ticket(self, tid: str) -> str:
        self.get(tid)
        ticket = secrets.token_urlsafe(32)
        with self._lock:
            now = self._clock()
            self._expire(now)
            self._tickets[ticket] = (tid, now + TICKET_TTL_S)
        return ticket

    def redeem(self, ticket, tid: str) -> bool:
        """Single use: a ticket is spent by being presented, whatever the
        answer. True only for an unexpired ticket minted for `tid`."""
        if not isinstance(ticket, str) or not ticket:
            return False
        with self._lock:
            now = self._clock()
            entry = self._tickets.pop(ticket, None)
            self._expire(now)
        return entry is not None and entry[1] > now and hmac.compare_digest(entry[0], tid)

    # -- an attach session (the HTTP handler's thread) -----------------------------

    def _attached(self, terminal: Terminal) -> None:
        """`terminal_attached`, so the HUD can tell an attach that was not its
        own. The id and the time: never output, a command or a ticket."""
        if self._publish is None:
            return
        try:
            self._publish({"kind": "terminal_attached",
                           "data": {"terminal_id": terminal.id, "at": utcnow()}})
        except Exception as exc:  # noqa: BLE001 — a bus problem never ends a session
            LOG.warning("terminal %s: attach not published (%s)", terminal.id, type(exc).__name__)

    def serve(self, terminal: Terminal, sock) -> None:
        try:
            refused = terminal.attach(sock, TAKEOVER_TIMEOUT_S)
            if refused is not None:
                sock.send_text(json.dumps({"type": "refused", "reason": refused}))
                sock.close(ws.POLICY, "refused")
                return
            self._attached(terminal)
            # Latched once any input frame is dropped: every later one is
            # refused too (a lone Ctrl-C excepted), until the queue has
            # drained *and* the window says `input_resume`. Accepting the next
            # frame once room frees would splice a paste — the program would
            # get a prefix, a hole, then a later chunk mid-line.
            latched = False
            while True:
                message = sock.receive()
                if message is None:
                    break
                kind, data = message
                if not terminal.holds(sock):
                    break
                if kind == ws.BINARY:
                    if latched and data != CTRL_C:
                        _send(sock, "input_dropped", bytes=len(data), latched=True,
                              reason=_LATCHED)
                    elif not terminal.queue_input(data):
                        latched = True
                        _send(sock, "input_dropped", bytes=len(data), latched=True,
                              reason=_NOT_READING)
                elif _control_type(data) == "input_resume":
                    if latched and not terminal.input_drained():
                        _send(sock, "input_resume_refused",
                              reason="earlier input is still being written; "
                                     "send input_resume again once it has drained")
                    else:
                        latched = False
                        _send(sock, "input_resumed")
                else:
                    terminal.control(sock, data)
        except Exception as exc:
            LOG.warning("terminal %s session failed (%s)", terminal.id, type(exc).__name__)
        finally:
            terminal.detach(sock)
            sock.close()
            sock.wait_closed(ws.CLOSE_WAIT_S + 0.5)
            sock.abort()

    # -- closing ---------------------------------------------------------------------

    def close(self, tid: str, grace: float | None = None) -> dict:
        self.get(tid)                           # 400 / 404 in words
        with self._lock:
            terminal = self._terminals.pop(tid, None)
        if terminal is None:                    # another DELETE got here first
            _fail(404, f"terminal {tid} not found")
        terminal.hangup("closed")
        until = time.monotonic() + (CLOSE_GRACE_S if grace is None else grace)
        while terminal.alive() and time.monotonic() < until:
            time.sleep(0.05)
        terminal.finish()
        LOG.info("terminal %s closed", tid)
        return {"ok": True, "id": tid, "exit_code": terminal.exit_code}

    def hangup_all(self) -> None:
        """Daemon stop, first half: no new terminal, and every session HUP'd."""
        with self._lock:
            self._stopped = True
            terminals = list(self._terminals.values())
        for terminal in terminals:
            terminal.hangup("ended")

    def finish_all(self, deadline: float) -> None:
        """Daemon stop, second half: wait for the sessions until `deadline`
        (at most STOP_GRACE_S), SIGKILL what is left, release everything."""
        with self._lock:
            self._stopped = True
            terminals = list(self._terminals.values())
            self._terminals.clear()
            self._tickets.clear()
        for terminal in terminals:
            terminal.hangup("ended")
        until = min(deadline, time.monotonic() + STOP_GRACE_S)
        while any(t.alive() for t in terminals) and time.monotonic() < until:
            time.sleep(0.05)
        for terminal in terminals:
            terminal.finish()
            LOG.info("terminal %s ended with Jarvis", terminal.id)


# -- routes (owner-only, the HUD listener) -----------------------------------------------

def _strict_host(handler, daemon) -> None:
    host = handler.headers.get("Host", "")
    port = daemon.face_port
    if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
        _fail(403, "untrusted Host")


def resolve_folder(daemon, spec) -> tuple[str, str | None, str]:
    """(folder, project id, label) from what the pane shows: a thread's own
    folder, a task's worktree (else its root), a project's root, or home.
    Always from ids, never from a path string."""
    stores = daemon.stores
    if spec == "home":
        return str(Path(os.environ.get("HOME") or Path.home())), None, "~"
    if not isinstance(spec, dict) or len(spec) != 1 or next(iter(spec)) not in ("thread", "project", "task"):
        _fail(400, 'in must be "home" or one of {"thread": id}, {"project": id}, {"task": id}')
    kind, value = next(iter(spec.items()))
    if kind == "thread":
        thread = daemon.require(stores.threads, value)
        project = daemon.require(stores.projects, thread.project_id)
        folder = daemon.thread_json(thread).get("cwd") or project.root
    elif kind == "task":
        task = daemon.require(stores.tasks, value)
        project = daemon.require(stores.projects, task.project_id)
        if task.worktree and Path(task.worktree).is_dir():
            folder = task.worktree
        else:
            folder = task.root or project.root
    else:
        project = daemon.require(stores.projects, value)
        folder = project.root
    return folder, project.id, project.name


def route(handler, daemon, parts, query):
    """`/terminals…`, or None. Every route is owner-only (module note)."""
    if not parts or parts[0] != "terminals":
        return None
    from .daemon import _object
    from .projects import owner_only
    owner_only(handler, daemon)
    _strict_host(handler, daemon)
    terminals = daemon.terminals
    method = handler.command
    if parts == ["terminals"]:
        _object(query, ())
        if method == "GET":
            return 200, terminals.listing()
        if method == "POST":
            body = _object(handler._body(), ("in", "cols", "rows"), ("in",))
            cols, rows = body.get("cols", 80), body.get("rows", 24)
            if not _size_ok(cols, rows):
                _fail(400, "cols must be an integer 2-1000 and rows 1-500")
            folder, project_id, label = resolve_folder(daemon, body["in"])
            if not os.path.isabs(folder) or not Path(folder).is_dir():
                _fail(409, f"the folder {folder} does not exist any more")
            try:
                terminal = terminals.create(folder, project_id=project_id, label=label,
                                            cols=cols, rows=rows)
            except TerminalError as exc:
                _fail(409, str(exc))
            return 201, terminal.row()
    if len(parts) in (2, 3):
        terminal = terminals.get(parts[1])
        if len(parts) == 2 and method == "DELETE":
            _object(query, ())
            return 200, terminals.close(terminal.id)
        if len(parts) == 2 and method == "PATCH":
            # The owner's "Jarvis can read" switch (W-2). Exactly {readable}.
            _object(query, ())
            body = _object(handler._body(), ("readable",), ("readable",))
            if not isinstance(body["readable"], bool):
                _fail(400, "readable must be a boolean")
            terminal.readable = body["readable"]
            return 200, terminal.row()
        if parts[2:] == ["ticket"] and method == "POST":
            _object(query, ())
            _object(handler._body(), ())
            return 200, {"ticket": terminals.ticket(terminal.id), "expires_in": TICKET_TTL_S}
        if parts[2:] == ["attach"] and method == "GET":
            _object(query, ("ticket",))
            try:
                key = ws.handshake_key(handler.headers)
            except ws.HandshakeError as exc:
                _fail(400, str(exc))
            if not terminals.redeem(query.get("ticket"), terminal.id):
                _fail(403, "a fresh ticket for this terminal is required")
            # The 101 is written by hand, past `end_headers`: it carries the
            # listener's frame headers explicitly (WP-E, every response).
            from .hud_api import frame_headers
            sock = ws.upgrade(handler, key, headers=frame_headers(handler))
            terminals.serve(terminal, sock)
            return 200, None
    _fail(404, "route not found")
