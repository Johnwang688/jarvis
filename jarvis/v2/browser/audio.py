"""BR10: curated WSLg launch audio and bounded, owned-stream mute only.

No preference file, global mixer setting, unmute command or permission decision
lives here. The host owns instance/activity lifetimes. Pulse index attribution is
best effort: re-enumeration and process identities narrow, but cannot eliminate,
the server's index-reuse race between the final check and its mute command.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import threading
import time
from typing import Callable

PULSE_SOCKET = Path("/mnt/wslg/PulseServer")
PULSE_SERVER = "unix:/mnt/wslg/PulseServer"
PACTL = Path("/usr/bin/pactl")
CHROME = "/opt/google/chrome/chrome"
OUTPUT_CAP = 256 * 1024
STREAM_CAP = 256
MUTE_CAP = 16
COMMAND_S = 4.0
BATCH_S = 12.0
UNAVAILABLE = "sound is not available on this machine"
MUTE_UNAVAILABLE = "immediate mute is unavailable; sound is waiting for restart"
MUTE_UNCERTAIN = "owned audio mute could not be confirmed; sound is waiting for restart"
SUPERSEDED = "the sound request changed"


@dataclass(frozen=True)
class Availability:
    routing: bool
    immediate_mute: bool


def availability() -> Availability:
    try:
        routing = stat.S_ISSOCK(PULSE_SOCKET.lstat().st_mode)
    except OSError:
        routing = False
    try:
        utility = stat.S_ISREG(PACTL.stat().st_mode) and os.access(PACTL, os.X_OK)
    except OSError:
        utility = False
    return Availability(routing, routing and utility)


@dataclass(frozen=True)
class LaunchAudio:
    args: list[str]
    env: dict[str, str]
    applied: bool
    reason: str | None


def launch_audio(args: list[str], env: dict[str, str], desired: bool,
                 available: Availability | None = None) -> LaunchAudio:
    """Compose only after the host has built its curated/font environment."""
    if type(desired) is not bool:
        raise ValueError("sound must be a boolean")
    supported = available if available is not None else availability()
    applied = desired and supported.routing
    result_args = [arg for arg in args if arg != "--mute-audio"] if applied else list(args)
    if not applied and "--mute-audio" not in result_args:
        result_args.append("--mute-audio")
    result_env = {key: value for key, value in env.items() if key != "PULSE_SERVER"}
    if applied:
        result_env["PULSE_SERVER"] = PULSE_SERVER
    return LaunchAudio(result_args, result_env, applied,
                       UNAVAILABLE if desired and not applied else None)


class InstanceAudio:
    """One launch's immutable mode and conservative mute-attempt taint.

    An already issued OFF may finish after ON. Never clear its taint on a
    settings change or an error; only a new instance is provably unmuted.
    """
    def __init__(self, applied: bool):
        self._applied = applied
        self._lock = threading.Lock()
        self._epoch = 0
        self._tainted = False
        self._pending: set[int] = set()
        self._settled = None

    @property
    def applied(self) -> bool:
        return self._applied

    def begin_mute(self) -> int:
        with self._lock:
            self._epoch += 1
            self._tainted = True
            self._pending.add(self._epoch)
            return self._epoch

    def on_settled(self, callback) -> None:
        with self._lock:
            self._settled = callback

    def finish_mute(self, epoch: int) -> None:
        with self._lock:
            self._pending.discard(epoch)
            callback = self._settled
        if callback is not None:
            callback()

    def snapshot(self) -> tuple[int, bool, int]:
        with self._lock:
            return self._epoch, self._tainted, len(self._pending)

    def synchronized(self, desired: bool) -> bool:
        with self._lock:
            return (not self._pending and self.applied == desired
                    and not (desired and self._tainted))


class AudioRefused(Exception):
    """Fixed safe reason, never utility output or process arguments."""


class _UnsettledMute(AudioRefused):
    """A killed utility has not exited yet; its instance must stay reserved."""
    def __init__(self, process):
        super().__init__(MUTE_UNCERTAIN)
        self.process = process


def _retire_mute(process, state: InstanceAudio, epoch: int) -> None:
    def reap():
        try:
            process.wait()
        except Exception:
            # Unknown cleanup stays pending; no fresh instance may overlap it.
            return
        state.finish_mute(epoch)
    threading.Thread(target=reap, name="browser-mute-cleanup", daemon=True).start()


def _read_small(path: Path, cap: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        result = bytearray()
        while len(result) <= cap:
            chunk = os.read(fd, min(8192, cap + 1 - len(result)))
            if not chunk:
                return bytes(result)
            result.extend(chunk)
        raise ValueError("process record too large")
    finally:
        os.close(fd)


@dataclass(frozen=True)
class Process:
    pid: int
    parent: int
    started: int
    uid: int
    executable: str


class Processes:
    """Read minimal process identities; command line is read only for pinned Chrome."""
    def __init__(self, root: Path = Path("/proc"), *, uid: int | None = None):
        self.root = root
        self.uid = os.getuid() if uid is None else uid

    def identity(self, pid: int) -> Process | None:
        if type(pid) is not int or not 1 < pid < 2**31:
            return None
        folder = self.root / str(pid)
        try:
            before = _read_small(folder / "stat", 4096)
            # comm may contain spaces and parentheses; fields follow its last ')'.
            fields = before.rsplit(b")", 1)[1].split()
            parent, started = int(fields[1]), int(fields[19])
            uid = folder.stat().st_uid
            exe = os.readlink(folder / "exe")
            after = _read_small(folder / "stat", 4096)
            later = after.rsplit(b")", 1)[1].split()
            if (parent, started) != (int(later[1]), int(later[19])) or started <= 0:
                return None
            return Process(pid, parent, started, uid, exe)
        except (OSError, ValueError, IndexError):
            return None

    def profile(self, process: Process, folder: Path) -> bool:
        if process.uid != self.uid or process.executable != CHROME:
            return False
        try:
            args = _read_small(self.root / str(process.pid) / "cmdline", 64 * 1024).split(b"\0")
            wanted = os.fsencode(str(folder))
            found = []
            for index, arg in enumerate(args):
                if arg.startswith(b"--user-data-dir="):
                    found.append(arg.partition(b"=")[2])
                elif arg == b"--user-data-dir":
                    found.append(args[index + 1] if index + 1 < len(args) else b"")
            return found == [wanted] and self.identity(process.pid) == process
        except (OSError, ValueError):
            return False


@dataclass(frozen=True)
class OwnedChrome:
    process: Process
    folder: Path = field(repr=False)
    reader: Processes = field(repr=False, compare=False)

    @classmethod
    def pin(cls, pid: int, folder: Path, reader: Processes | None = None):
        reader = reader or Processes()
        process = reader.identity(pid)
        if process is None or not reader.profile(process, folder):
            raise AudioRefused(MUTE_UNCERTAIN)
        return cls(process, folder, reader)

    def current(self) -> bool:
        return (self.reader.identity(self.process.pid) == self.process
                and self.reader.profile(self.process, self.folder))

    def chain(self, pid: int) -> tuple[Process, ...] | None:
        """Require unchanged owner/start/ancestry for the whole bounded Chrome chain."""
        if not self.current():
            return None
        rows = []
        seen = set()
        for _ in range(32):
            if pid in seen:
                return None
            seen.add(pid)
            row = self.reader.identity(pid)
            if row is None or row.uid != self.process.uid or row.executable != CHROME:
                return None
            rows.append(row)
            if row == self.process:
                return tuple(rows) if (self.current() and all(
                    self.reader.identity(item.pid) == item for item in rows)) else None
            pid = row.parent
        return None


@dataclass(frozen=True)
class Sink:
    index: int
    pid: int
    muted: bool


def sink_inputs(raw: bytes) -> tuple[Sink, ...]:
    """Discard all labels/media fields. Malformed/duplicate indexes fail closed."""
    if not isinstance(raw, bytes) or len(raw) > OUTPUT_CAP:
        raise AudioRefused(MUTE_UNCERTAIN)
    try:
        rows = json.loads(raw)
        if not isinstance(rows, list) or len(rows) > STREAM_CAP:
            raise ValueError
        result = []
        indexes = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError
            index, muted = row.get("index"), row.get("mute")
            props = row.get("properties")
            if not isinstance(props, dict):
                raise ValueError
            pid = props.get("application.process.id")
            if (type(index) is not int or not 0 <= index < 2**32 - 1 or index in indexes
                    or type(muted) is not bool):
                raise ValueError
            indexes.add(index)
            # A server may own streams with no Unix process. They are never ours.
            if pid is None:
                continue
            if not isinstance(pid, str) or re.fullmatch(r"[1-9][0-9]{0,9}", pid) is None:
                raise ValueError
            value = int(pid)
            if not 1 < value < 2**31:
                raise ValueError
            result.append(Sink(index, value, muted))
        return tuple(result)
    except (ValueError, TypeError, RecursionError):
        raise AudioRefused(MUTE_UNCERTAIN) from None


def _pactl(args: tuple[str, ...], timeout: float) -> bytes:
    """One bounded child; no shell, inherited env, stderr text or global commands."""
    allowed = (args == ("--format=json", "list", "sink-inputs") or
               (len(args) == 3 and args[0] == "set-sink-input-mute" and args[2] == "1"
                and re.fullmatch(r"[0-9]{1,10}", args[1]) is not None
                and int(args[1]) < 2**32 - 1))
    if not allowed or not 0 < timeout <= COMMAND_S:
        raise AudioRefused(MUTE_UNCERTAIN)
    return _run_bounded(args, timeout)


def _run_bounded(args: tuple[str, ...], timeout: float) -> bytes:
    """The one bounded pactl child. Private on purpose: it applies NO allowlist, so
    its only callers are `_pactl` above (listing and mute) and
    `speech_duck.pactl` (listing and volume), and each must judge `args` against its
    own allowlist before calling. Nothing else may import it. Output is read in
    capped chunks (never buffered whole), the child is killed at the deadline, and
    one that will not die is reported as `_UnsettledMute` for the caller to reap."""
    process = None
    try:
        process = subprocess.Popen([str(PACTL), *args], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PULSE_SERVER": PULSE_SERVER})
        data = bytearray()
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise AudioRefused(MUTE_UNCERTAIN)
                chunk = os.read(process.stdout.fileno(), min(8192, OUTPUT_CAP + 1 - len(data)))
                if not chunk:
                    selector.unregister(process.stdout)
                else:
                    data.extend(chunk)
                    if len(data) > OUTPUT_CAP:
                        raise AudioRefused(MUTE_UNCERTAIN)
        if process.wait(timeout=max(.001, deadline - time.monotonic())) != 0:
            raise AudioRefused(MUTE_UNCERTAIN)
        return bytes(data)
    except (OSError, subprocess.TimeoutExpired):
        raise AudioRefused(MUTE_UNCERTAIN) from None
    finally:
        if process is not None:
            try:
                if process.poll() is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    # A bounded waiter does not imply actual native completion.
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        raise _UnsettledMute(process) from None
            finally:
                if process.stdout is not None:
                    process.stdout.close()


@dataclass(frozen=True)
class MuteResult:
    confirmed: int
    complete: bool
    reason: str | None = None


class Pulse:
    def __init__(self, *, run: Callable = _pactl, clock=time.monotonic,
                 available: Callable = availability):
        self.run, self.clock, self.available = run, clock, available

    def mute_owned(self, owner: OwnedChrome, state: InstanceAudio,
                   current: Callable[[], bool]) -> MuteResult:
        """Only the host may provide the current instance/desired-generation predicate."""
        if not self.available().immediate_mute:
            return MuteResult(0, False, MUTE_UNAVAILABLE)
        if state.snapshot()[2]:
            return MuteResult(0, False, MUTE_UNCERTAIN)
        deadline = self.clock() + BATCH_S
        confirmed = 0
        def call(args):
            remaining = deadline - self.clock()
            if remaining <= 0 or not current() or not owner.current():
                raise AudioRefused(SUPERSEDED if remaining > 0 else MUTE_UNCERTAIN)
            return self.run(args, min(COMMAND_S, remaining))
        def listing():
            return sink_inputs(call(("--format=json", "list", "sink-inputs")))
        try:
            rows = listing()
            targets = [(row, owner.chain(row.pid)) for row in rows]
            targets = [(row, chain) for row, chain in targets if chain is not None]
            if len(targets) > MUTE_CAP:
                raise AudioRefused(MUTE_UNCERTAIN)
            for row, chain in targets:
                if row.muted:
                    continue
                fresh = next((item for item in listing() if item.index == row.index), None)
                if fresh is None or fresh.pid != row.pid or owner.chain(row.pid) != chain:
                    raise AudioRefused(MUTE_UNCERTAIN)
                if fresh.muted:
                    continue
                epoch = state.begin_mute()
                try:
                    call(("set-sink-input-mute", str(row.index), "1"))
                except _UnsettledMute as exc:
                    _retire_mute(exc.process, state, epoch)
                    epoch = None
                    raise AudioRefused(MUTE_UNCERTAIN) from None
                finally:
                    if epoch is not None:
                        state.finish_mute(epoch)
                after = next((item for item in listing() if item.index == row.index), None)
                if (after is None or after.pid != row.pid or not after.muted
                        or owner.chain(row.pid) != chain):
                    raise AudioRefused(MUTE_UNCERTAIN)
                confirmed += 1
            return MuteResult(confirmed, True)
        except _UnsettledMute as exc:
            # Even an unsettled listing child is reaped and blocks another batch.
            epoch = state.begin_mute()
            _retire_mute(exc.process, state, epoch)
            return MuteResult(confirmed, False, MUTE_UNCERTAIN)
        except AudioRefused as exc:
            return MuteResult(confirmed, False, str(exc))
