"""Client for T3 threads and exact project lookup. Returned text is untrusted data."""

from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

RUNNING = frozenset({"queued", "preparing", "starting", "running"})
SETTLED = frozenset({
    "idle", "waiting", "completed", "interrupted", "failed", "cancelled", "rolled_back", "exported",
})
_STDOUT_CAP = 8 * 1024 * 1024
_IO_CHUNK = 64 * 1024
_ENV_KEEP = ("PATH", "HOME", "T3CODE_HOME", "LANG")
_PROJECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")


class T3Error(Exception):
    _MESSAGES = {
        "missing": "T3 CLI is missing or changed before launch.",
        "arguments": "Invalid T3 CLI arguments.",
        "failed": "T3 CLI command failed.",
        "shape": "T3 CLI returned an unsupported JSON response. Check the installed T3 version.",
        "project_state": "T3 project state is unavailable or unsupported.",
        "timeout": "T3 CLI timed out.",
    }

    def __init__(self, kind: str, *, output_limit: bool = False):
        self.kind = kind
        super().__init__("T3 CLI exceeded its output limit." if output_limit else self._MESSAGES[kind])


@dataclass(frozen=True)
class Run:
    thread_id: str
    run_id: str
    status: str


@dataclass(frozen=True)
class State:
    thread_id: str
    status: str
    waiting_on_owner: bool
    pending_requests: int
    title: str = field(repr=False)
    provider: str
    model: str
    messages: list[tuple[str, str]] = field(repr=False)
    settled_at: float | None = None
    run_count: int | None = None


def _executable(path: Path) -> os.stat_result:
    info = path.stat(follow_symlinks=False)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or not os.access(path, os.X_OK)):
        raise T3Error("missing")
    return info


def _resolve(home, env) -> tuple[Path, os.stat_result]:
    try:
        if "JARVIS_T3_BIN" in env:
            override = env["JARVIS_T3_BIN"]
            if not override:
                raise T3Error("missing")
            path = Path(override).absolute()
            return path, _executable(path)

        root = (Path.home() if home is None else Path(home).absolute()) / ".t3" / "wsl-runtime"
        newest = None
        for path in root.glob("*/t3"):
            try:
                info = _executable(path)
            except (OSError, ValueError, T3Error):
                continue
            if newest is None or (info.st_mtime_ns, path) > (newest[1].st_mtime_ns, newest[0]):
                newest = path, info
        if newest is not None:
            return newest
    except (OSError, TypeError, ValueError, RuntimeError):
        raise T3Error("missing") from None
    raise T3Error("missing")


def resolve(*, home=None, env=None) -> Path:
    """Find the override or newest user-owned regular executable in the WSL runtime."""
    return _resolve(home, os.environ if env is None else env)[0]


def _arg(value, limit=256, *, allow_dash=False) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or (not allow_dash and value.startswith("-"))
            or any(unicodedata.category(c) in {"Cc", "Cs", "Zl", "Zp"} for c in value)
            or len(value.encode("utf-8")) > limit):
        raise T3Error("arguments")
    return value


def _message(value: str) -> bytes:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise T3Error("arguments")
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError:
        pass
    # The encoding error retains the prompt. Raise after leaving its handler.
    raise T3Error("arguments")


def _project(value) -> str:
    try:
        return _arg(os.fspath(value), limit=4096)
    except TypeError:
        raise T3Error("arguments") from None


def _seconds(value) -> float:
    if type(value) not in (int, float) or not -sys.float_info.max <= value <= sys.float_info.max:
        raise T3Error("arguments")
    return float(value)


def _left(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise T3Error("timeout")
    return remaining


def _launch(path: Path, expected: os.stat_result, argv: list[str], env: dict[str, str],
            message: bytes | None, deadline: float) -> subprocess.Popen:
    # On WSL/Linux, pin the selected executable through exec even if an update
    # swaps the name. O_PATH also avoids blocking on a replacement FIFO.
    try:
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            actual = os.fstat(fd)
            executable = f"/proc/self/fd/{fd}"
            if (not stat.S_ISREG(actual.st_mode) or actual.st_uid != os.geteuid()
                    or not os.path.samestat(expected, actual)
                    or not os.access(executable, os.X_OK)):
                raise T3Error("missing")
            _left(deadline)
            return subprocess.Popen(
                [os.fspath(path), *argv], executable=executable, pass_fds=(fd,),
                env=env, stdin=subprocess.PIPE if message is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True,
            )
        finally:
            os.close(fd)
    except (FileNotFoundError, PermissionError, NotADirectoryError):
        raise T3Error("missing") from None


def _capture(proc: subprocess.Popen, message: bytes | None, deadline: float) -> tuple[int, bytearray]:
    output = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            os.set_blocking(proc.stdout.fileno(), False)
            selector.register(proc.stdout, selectors.EVENT_READ)
            if proc.stdin is not None:
                remaining = memoryview(message)
                os.set_blocking(proc.stdin.fileno(), False)
                selector.register(proc.stdin, selectors.EVENT_WRITE)

            while selector.get_map():
                for key, event in selector.select(min(_left(deadline), 60)):
                    _left(deadline)
                    if event == selectors.EVENT_READ:
                        chunk = os.read(key.fd, min(_IO_CHUNK, _STDOUT_CAP - len(output) + 1))
                        if len(output) + len(chunk) > _STDOUT_CAP:
                            raise T3Error("shape", output_limit=True)
                        output.extend(chunk)
                        more = bool(chunk)
                    else:
                        try:
                            remaining = remaining[os.write(key.fd, remaining[:_IO_CHUNK]):]
                            more = bool(remaining)
                        except BrokenPipeError:
                            more = False
                    if not more:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
        # The child can close stdout before exiting. Keep the same deadline.
        while True:
            try:
                return proc.wait(timeout=min(_left(deadline), 60)), output
            except subprocess.TimeoutExpired:
                _left(deadline)
    finally:
        if proc.returncode is None:
            # The unreaped leader still identifies the process group.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                stream.close()
        if proc.returncode is None:
            try:
                proc.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                # Never let a delayed reap hold up the caller.
                pass


def _field(obj, key, expected):
    if not isinstance(obj, dict) or type(obj.get(key)) is not expected:
        raise T3Error("shape")
    return obj[key]


def _run(data, waiting: bool) -> Run:
    thread = _field(data, "threadId", str)
    run = _field(data, "runId", str)
    # A start that does not wait answers before the run has a status: it is queued.
    status = _field(data, "status", str) if "status" in data else "queued"
    return Run(thread, run, "waiting" if waiting else status)


def _settled_at(row):
    value = row.get("settledAt")
    if isinstance(value, str):
        try:
            instant = datetime.fromisoformat(value)
            if instant.tzinfo is not None:
                return instant.timestamp()
        except (ValueError, OverflowError, OSError):
            pass
    return None


def _state(row, messages, *, waiting=False, listed=False) -> State:
    status = _field(row, "status", str)
    # The list shape does not include the pending request count.
    pending = 0 if listed and "pendingRequestCount" not in row else _field(row, "pendingRequestCount", int)
    if pending < 0:
        raise T3Error("shape")
    return State(
        _field(row, "threadId", str), status, waiting or status == "waiting" or pending > 0,
        pending, _field(row, "title", str), _field(row, "providerInstanceId", str),
        _field(row, "model", str), messages, _settled_at(row),
        _field(row, "runCount", int) if "runCount" in row else None,
    )


class T3:
    def __init__(self, binary=None, *, timeout=60):
        self._binary = binary
        self._timeout = _seconds(timeout)

    def _invoke(self, argv, *, message=None, deadline=None):
        command_deadline = time.monotonic() + self._timeout
        deadline = command_deadline if deadline is None else min(deadline, command_deadline)
        _left(deadline)
        try:
            if self._binary is None:
                path, info = _resolve(None, os.environ)
            else:
                path = Path(self._binary).absolute()
                info = _executable(path)
        except (OSError, TypeError, ValueError):
            raise T3Error("missing") from None
        env = {key: os.environ[key] for key in _ENV_KEEP if key in os.environ}
        try:
            proc = _launch(path, info, argv, env, message, deadline)
            return _capture(proc, message, deadline)
        except (OSError, ValueError):
            raise T3Error("failed") from None

    def _call(self, verb, *args, message=None, deadline=None):
        code, output = self._invoke(["thread", verb, "--json", *args], message=message, deadline=deadline)
        if code not in (0, 2):
            raise T3Error("failed")
        try:
            return json.loads(output.decode("utf-8")), code == 2
        except (ValueError, RecursionError):
            pass
        # Decode errors can retain the raw response in their payload.
        raise T3Error("shape")

    def project_add(self, path, *, title=None) -> None:
        args = ["project", "add"]
        if title is not None:
            args.extend(("--title", _arg(title)))
        code, _ = self._invoke([*args, _project(path)])
        if code != 0:
            raise T3Error("failed")

    def project_at(self, path) -> str | None:
        """Read the exact active project id from T3's local project projection.

        The thread CLI resolves enclosing projects and even other git checkouts.
        Its list output omits the workspace root, including for empty projects.
        """
        root = _project(path)
        base = (os.environ.get("T3CODE_HOME") or "").strip() or "~/.t3"
        import sqlite3  # at first use: the daemon must start without loading it
        try:
            database = Path(base).expanduser().absolute() / "userdata" / "statev2.sqlite"
            if not database.is_file():
                return None
            uri = database.as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=min(self._timeout, 1))) as db:
                db.execute("PRAGMA trusted_schema=OFF")
                rows = db.execute(
                    "SELECT project_id FROM projection_projects "
                    "WHERE workspace_root = ? AND deleted_at IS NULL AND archived_at IS NULL LIMIT 2",
                    (root,),
                ).fetchall()
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            raise T3Error("project_state") from None
        if not rows:
            return None
        if len(rows) != 1 or not isinstance(rows[0][0], str) or not _PROJECT_ID.fullmatch(rows[0][0]):
            raise T3Error("project_state")
        return rows[0][0]

    def start(self, project, message, *, provider, model, title=None, worktree=None, branch=None) -> Run:
        """Start a thread. The title is visible in the local process list."""
        args = ["--provider", _arg(provider), "--model", _arg(model)]
        if title is not None:
            title = _arg(title, limit=320, allow_dash=True)
            if title.startswith("-"):
                # Keep an initial dash in a subject from being parsed as a flag.
                args.append("--title=" + title)
            else:
                args.extend(("--title", title))
        for flag, value in (("--worktree", worktree), ("--branch", branch)):
            if value is not None:
                args.extend((flag, _arg(value)))
        return _run(*self._call("start", *args, _project(project), "-", message=_message(message)))

    def send(self, thread_id, message) -> Run:
        return _run(*self._call("send", _arg(thread_id), "-", message=_message(message)))

    def _show(self, thread_id, messages, deadline=None) -> State:
        data, waiting = self._call("show", "--messages", str(messages), thread_id, deadline=deadline)
        recent = [(_field(row, "role", str), _field(row, "text", str))
                  for row in _field(data, "messages", list)]
        return _state(_field(data, "thread", dict), recent, waiting=waiting)

    def show(self, thread_id, *, messages=4) -> State:
        if type(messages) is not int or not 0 <= messages <= 2**31 - 1:
            raise T3Error("arguments")
        return self._show(_arg(thread_id), messages)

    def list(self, project) -> list[State]:
        rows, _ = self._call("list", "--project", _project(project))
        if not isinstance(rows, list):
            raise T3Error("shape")
        return [_state(row, [], listed=True) for row in rows]

    def wait(self, thread_id, *, timeout, poll_s=2.0, sleep=time.sleep, clock=time.monotonic) -> State:
        thread_id = _arg(thread_id)
        timeout = _seconds(timeout)
        poll_s = _seconds(poll_s)
        if poll_s <= 0:
            raise T3Error("arguments")
        deadline = time.monotonic() + timeout
        poll_deadline = clock() + timeout
        # An injected clock can shorten the budget. The real deadline still
        # bounds the subprocess and sleeps if the injected clock goes backward.
        while True:
            left = min(poll_deadline - clock(), _left(deadline))
            if left <= 0:
                raise T3Error("timeout")
            state = self._show(thread_id, 4, min(deadline, time.monotonic() + left))
            left = min(poll_deadline - clock(), _left(deadline))
            if left <= 0:
                raise T3Error("timeout")
            if state.status in SETTLED:
                return state
            if state.status not in RUNNING:
                raise T3Error("shape")
            sleep(min(left, poll_s))
