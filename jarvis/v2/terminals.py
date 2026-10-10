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
(a script an agent wrote and got run, say) can send any Origin it likes and
fetch a ticket. What limits it: an agent has to get that program run first
(`curl`/`wget` never auto-run, Claude workers pass the PreToolUse permit,
Codex workers have no network in their sandbox); a terminal a window is
showing asks that window before it moves; and `sudo` in a HUD terminal never
caches (the startup file's `alias sudo='sudo -k'`, W-5), so an injected line
cannot ride a `sudo` the owner just typed.

**Lifecycle (W-3, plan decision 12).** The owner's login shell
(`JARVIS_TERMINAL_SHELL`, an absolute path, overrides it), started through
util-linux `setsid --ctty` so it leads its own session with the PTY as its
controlling terminal — never `pty.fork()` in this threaded daemon. A clean
environment, not the daemon's: nothing from `.env`, no `OPENROUTER_API_KEY`,
`HF_HUB_OFFLINE`, `VIRTUAL_ENV` or venv `PATH`, no `ANTHROPIC_*`,
`CLAUDE_*`, `CODEX_*` or `JARVIS_*`. At most six. Each keeps 1 MiB of output
in memory, replayed when a window reattaches. Closing one sends SIGHUP to
every process in its session (found in /proc, so background jobs are
included), then SIGKILL. **Terminals end with the daemon** (`Daemon.stop`
closes them all); there is no tmux.

**Output never leaves memory.** It exists in two places: this ring and the
owner's browser. It never goes to the bus, a log (the daemon log gets
lifecycle lines only: opened, attached, exited, closed, with the folder as a
project name or `~`), a session or thread log, Discord, or disk.

**Shell integration (W-2, item 3).** The startup file (`terminal_rc.sh`)
emits OSC 133 marks carrying a per-terminal nonce; `Marks` turns them into
`CommandSpan`s over the ring's byte offsets, so WP-F's `terminal_read` can
refuse a read covering a secret-printing command's output. `readable` is the
owner's "Jarvis can read" switch, on by default; nothing reads it yet.
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
import tempfile
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
MARK_CAP = 16 * 1024           # an OSC 133 longer than this is not one of ours
SPAN_CAP = 2000
COMMAND_CAP = 4096
RC_PATH = Path(__file__).with_name("terminal_rc.sh")
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

def resolve_shell() -> str:
    """The owner's login shell, or `JARVIS_TERMINAL_SHELL`. An override must
    be an absolute path (a relative one would be resolved in the terminal's
    folder), and nothing under /mnt/ is a Linux shell."""
    override = (config.TERMINAL_SHELL or "").strip()
    if override:
        if not os.path.isabs(override):
            raise TerminalError("JARVIS_TERMINAL_SHELL must be an absolute path")
        path = override
    else:
        try:
            path = pwd.getpwuid(os.getuid()).pw_shell
        except KeyError:
            path = ""
        path = path or "/bin/bash"
    if path.startswith("/mnt/"):
        raise TerminalError("the terminal shell must be a Linux program, not one under /mnt/")
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        raise TerminalError(f"the terminal shell {path} is not an executable file")
    return path


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


def shell_command(shell: str, rcfile: str) -> tuple[list[str], dict]:
    """argv and extra environment. bash takes the startup file as its
    --rcfile (the file then reads the login files itself); any other shell
    starts as a login shell and reads it as $ENV (POSIX)."""
    if os.path.basename(shell) == "bash":
        return [shell, "--rcfile", rcfile, "-i"], {}
    return [shell, "-l"], {"ENV": rcfile}


def session_members(sid: int) -> list[int]:
    """Every live process in session `sid`, background jobs included."""
    members = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return members
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as handle:
                stat = handle.read()
        except OSError:
            continue
        fields = stat[stat.rfind(b")") + 2:].split()
        try:
            if int(fields[3]) == sid and fields[0] != b"Z":
                members.append(int(name))
        except (IndexError, ValueError):
            continue
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

    @property
    def end(self) -> int:
        return self.start + self._size

    def append(self, data: bytes) -> None:
        if not data:
            return
        data = bytes(data)
        if len(data) >= self.cap:
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
            else:
                self._chunks[0] = first[excess:]
                self._size -= excess
                self.start += excess

    def snapshot(self) -> tuple[int, bytes]:
        """(offset, bytes) to replay. Once anything has been dropped, the
        replay starts at the next line, never mid-line or mid-sequence."""
        data = b"".join(self._chunks)
        start = self.start
        if start > 0:
            cut = data.find(b"\n", 0, 64 * 1024)
            if cut >= 0:
                data = data[cut + 1:]
                start += cut + 1
        return start, data


@dataclass
class CommandSpan:
    """One command line and the output it produced, as absolute offsets into
    the terminal's output stream: `start` is the first byte after its C mark,
    `end` the first byte of its D mark (None while it runs)."""
    command: str
    start: int
    end: int | None = None
    exit_code: int | None = None
    prompt: int | None = None


class Marks:
    """Parses OSC 133 shell-integration marks out of the output stream, across
    chunk boundaries, and keeps the per-command spans. A mark without this
    terminal's nonce is ignored, so a program's output cannot forge one."""

    PREFIX = b"\x1b]133;"

    def __init__(self, nonce: str):
        self._nonce = nonce.encode()
        self._carry = b""
        self._offset = 0
        self._prompt: int | None = None
        self.spans: collections.deque[CommandSpan] = collections.deque(maxlen=SPAN_CAP)
        self.integrated = False

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
        self.integrated = True
        open_span = self._open()
        if kind == b"A":
            if open_span is not None:           # a prompt with no D: it ended there
                open_span.end = start
            self._prompt = start
        elif kind == b"C":
            if open_span is not None:
                open_span.end = start
            command = unquote_to_bytes(values.get(b"cmdline_url", b"")).decode("utf-8", "replace")
            self.spans.append(CommandSpan(command[:COMMAND_CAP], start=end, prompt=self._prompt))
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
    """What WP-F's `terminal_read` will read: the ring's bytes from `start`,
    the command spans overlapping them, and whether this terminal's shell
    emits marks at all (`integrated`; without them a read falls back to
    matching command lines in the text, W-2)."""
    start: int
    data: bytes
    spans: tuple
    integrated: bool
    readable: bool


# -- one terminal --------------------------------------------------------------------

class _Takeover:
    def __init__(self, holder):
        self.id = secrets.token_hex(4)
        self.holder = holder
        self.allow: bool | None = None
        self.event = threading.Event()


class Terminal:
    def __init__(self, tid: str, *, shell: str, folder: str, project_id: str | None,
                 label: str, cols: int, rows: int, nonce: str, rcfile: Path):
        self.id = tid
        self.shell = shell
        self.folder = folder
        self.project_id = project_id
        self.label = label
        self.title = f"{os.path.basename(shell)} · {label}"
        self.created = utcnow()
        self.cols, self.rows = cols, rows
        self.readable = True
        self.exited = False
        self.exit_code: int | None = None
        self._rcfile = rcfile
        self._ring = Ring()
        self._marks = Marks(nonce)
        self._lock = threading.Lock()           # ring, marks, attachment, state
        self._io_lock = threading.RLock()       # the master fd's life
        self._attachment: ws.WebSocket | None = None
        self._takeover: _Takeover | None = None
        self._closing = False
        self._master: int | None = None
        self._proc: subprocess.Popen | None = None
        self._wake_r, self._wake_w = os.pipe()
        self._reader: threading.Thread | None = None

    # -- spawning ---------------------------------------------------------------------

    def spawn(self, argv: list[str], env: dict) -> None:
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
            # setsid --ctty: a new session led by the shell, with this PTY as
            # its controlling terminal. The child is never a process-group
            # leader here, so setsid does not fork and the pid is the shell's.
            self._proc = subprocess.Popen([setsid, "--ctty", "--", *argv], stdin=slave,
                                          stdout=slave, stderr=slave, cwd=self.folder,
                                          env=env, close_fds=True)
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        self._master = master
        self._reader = threading.Thread(target=self._read_loop, name=f"jarvis-terminal-{self.id}",
                                        daemon=True)
        self._reader.start()

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
            self._marks.feed(data)
            self._marks.prune(self._ring.start)
            attached = self._attachment
            if attached is not None and not attached.send_binary(data):
                # Too far behind: dropped. The window reattaches and replays.
                self._attachment = None

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

    def write_input(self, data: bytes, alive) -> None:
        """Owner keystrokes and pastes into the PTY. Never blocks for good on a
        program that is not reading: it waits in slices while `alive()`."""
        view = memoryview(data)
        while view and alive():
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
                "busy": self.busy(), "integrated": self._marks.integrated}

    def history(self) -> History:
        """The ring and its command spans, for WP-F. No route reaches this."""
        with self._lock:
            start, data = self._ring.snapshot()
            spans = tuple(replace(s) for s in self._marks.spans
                          if s.end is None or s.end > start)
            return History(start, data, spans, self._marks.integrated, self.readable)

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
        proc = self._proc
        if proc is None:
            return []
        members = set(session_members(proc.pid))
        if proc.poll() is None:
            members.add(proc.pid)
        return sorted(members)

    def alive(self) -> bool:
        return bool(self._members())

    def finish(self) -> None:
        """SIGKILL whatever outlived the hangup, reap the shell, stop the
        reader and release the PTY."""
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
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(1.0)
        with self._io_lock:
            if self._master is not None:
                os.close(self._master)
                self._master = None
        for fd in (self._wake_r, self._wake_w):
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            self._rcfile.unlink()
        except OSError:
            pass
        if not self.exited and self._proc is not None and self._proc.returncode is not None:
            code = self._proc.returncode
            self.exited, self.exit_code = True, code if code >= 0 else 128 - code


def _size_ok(cols, rows) -> bool:
    return (type(cols) is int and type(rows) is int and 2 <= cols <= 1000 and 1 <= rows <= 500)


# -- every terminal ------------------------------------------------------------------

class Terminals:
    """The daemon's terminals, their tickets, and the attach sessions."""

    def __init__(self, *, clock=time.monotonic):
        self._lock = threading.Lock()
        self._terminals: dict[str, Terminal] = {}
        self._tickets: dict[str, tuple[str, float]] = {}
        self._clock = clock
        self._rc_text: str | None = None
        self._rc_dir: Path | None = None
        self._stopped = False

    def _rcfile(self, tid: str, nonce: str) -> Path:
        """This terminal's private copy of the startup file, with its nonce.
        The shipped file is read once per daemon, like the code beside it."""
        if self._rc_text is None:
            self._rc_text = RC_PATH.read_text(encoding="utf-8")
        if self._rc_dir is None:
            self._rc_dir = Path(tempfile.mkdtemp(prefix="jarvis-terminals-"))   # mode 700
        path = self._rc_dir / f"rc-{tid}.sh"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"__jarvis_nonce='{nonce}'\n{self._rc_text}")
        return path

    def create(self, folder: str, *, project_id: str | None, label: str,
               cols: int = 80, rows: int = 24) -> Terminal:
        if not _size_ok(cols, rows):
            raise ValueError("cols must be 2-1000 and rows 1-500")
        shell = resolve_shell()
        with self._lock:
            if self._stopped:
                raise TerminalError("Jarvis is stopping")
            if len(self._terminals) >= TERMINAL_CAP:
                raise TerminalError(f"{TERMINAL_CAP} terminals are open; close one first")
            tid = secrets.token_hex(4)
            while tid in self._terminals:
                tid = secrets.token_hex(4)
            nonce = secrets.token_hex(16)
            rcfile = self._rcfile(tid, nonce)
            terminal = Terminal(tid, shell=shell, folder=folder, project_id=project_id,
                                label=label, cols=cols, rows=rows, nonce=nonce, rcfile=rcfile)
            argv, extra = shell_command(shell, str(rcfile))
            env = clean_environment(shell)
            env.update(extra)
            try:
                terminal.spawn(argv, env)
            except BaseException:
                terminal.finish()               # the wake pipe and the startup file
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

    def serve(self, terminal: Terminal, sock) -> None:
        try:
            refused = terminal.attach(sock, TAKEOVER_TIMEOUT_S)
            if refused is not None:
                sock.send_text(json.dumps({"type": "refused", "reason": refused}))
                sock.close(ws.POLICY, "refused")
                return
            alive = lambda: not sock.closed.is_set() and terminal.holds(sock)  # noqa: E731
            while True:
                message = sock.receive()
                if message is None:
                    break
                kind, data = message
                if not terminal.holds(sock):
                    break
                if kind == ws.BINARY:
                    terminal.write_input(data, alive)
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
        terminal = self.get(tid)
        with self._lock:
            self._terminals.pop(tid, None)
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
        if self._rc_dir is not None:
            shutil.rmtree(self._rc_dir, ignore_errors=True)
            self._rc_dir = None


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
            sock = ws.upgrade(handler, key)
            terminals.serve(terminal, sock)
            return 200, None
    _fail(404, "route not found")
