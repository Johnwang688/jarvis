"""Lowering the Remote Browser's sound while Jarvis speaks (voice plan V15, Q14).

Remote pages play through Chrome's own audio output, which WSLg's PulseAudio
carries to the owner's speakers (browser notes, "Browser sound"). While a
reply is read aloud the HUD holds a short **lease** on this module; for as long
as the lease lives, the sink-inputs of the daemon's own Remote Browser are
held about 15 dB lower, and every one is put back exactly when it ends.

    GET  /speech/duck            -> {"on": bool, "ducked": int, "lease_s": 3.0}
    POST /speech/duck {on: bool} -> the same; on=true grants or renews the lease

Owner-only (the HUD listener, like the other pickers). The lease is the whole
safety story: nothing here holds a volume for longer than `LEASE_S` after the
last renewal, so a closed window, a lost connection or a crashed HUD restores
within seconds without anyone saying so. Daemon shutdown restores too.

Rules (each one has a test in `tests/v2/speech_duck_check.py`):

- **Whose streams.** Only a sink-input whose PID descends from a Remote Chrome
  that `BrowserSound.duck_owners()` names: pinned to its profile folder, the
  executable, the uid and the process start time, the same ancestry check the
  mute uses (`audio.OwnedChrome.chain`). Never by application name (another
  agent's Playwright Chrome has the same name). Every other stream is never
  read for anything but its PID and never written.
- **Only audible, only permitted.** A profile is a target only when the owner's
  Remote sound permission is on for it *now* and its Chrome launched unmuted.
  Sound off, structurally muted or never started: no pactl write at all.
- **Never the mute.** Nothing here sends `set-sink-input-mute`, and a stream
  that is muted is left alone entirely.
- **Exact restore.** The prior per-channel volumes are recorded; restore writes
  them back raw. **The owner wins:** if a stream's volume is no longer the value
  this module wrote (the owner moved the slider in the Windows mixer), it is
  not touched again, now or at restore, and is not ducked again in that lease.
- **A stream that appears mid-duck** is ducked on the next scan (`SCAN_S`) from
  its own prior volume and restored to it. It may be audible at full volume for
  up to one scan interval.
- **PulseAudio remembers.** `module-stream-restore` saves every volume set by
  `pactl` under the application's name ("Google Chrome") and gives it to the
  next stream of that name (measured, voice notes V15). So a ducked volume that
  is never restored (daemon killed, or the stream died mid-duck) would make the
  next Remote stream start quiet. Each duck is therefore written to a small
  journal first; a later start recognises a new stream carrying exactly the
  ducked value and puts its prior value back (`Residue`).
- **Bounded, non-fatal pactl.** A fixed argv, no shell, a deadline per call; a
  failure or timeout is logged once per outage and retried at the next scan.
"""
from __future__ import annotations

import json
import logging
import os
import re
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from jarvis import config

from .browser import audio

LOG = logging.getLogger("jarvis.v2.speech_duck")

#: The lease the HUD renews about once a second while speech is active.
LEASE_S = 3.0
#: How often a lease is rescanned for streams that appeared or that the owner moved.
SCAN_S = 0.5
#: After a failed restore, how soon it is tried again.
RETRY_S = 1.0
#: While only a crash's leftover is known, how often an idle daemon looks for a stream to repair.
IDLE_POLL_S = 3.0
#: "About -15 dB". PulseAudio's raw volume is cubic in amplitude, so a true
#: amplitude change of DUCK_DB is raw * 10**(DUCK_DB/60) (pactl's "-15dB").
DUCK_DB = -15.0
FACTOR = 10 ** (DUCK_DB / 60.0)
#: One pactl call, and the whole of a shutdown restore.
CALL_S = 2.0
STOP_S = 6.0
MAX_STREAMS = audio.MUTE_CAP
MAX_CHANNELS = 8
#: No honest stream is louder than a few times the norm (65536); this only bounds what we will write.
VOLUME_MAX = 4 * 65536
MAX_RESIDUE = 16
RESIDUE_TTL_S = 30 * 24 * 3600.0
JOURNAL_MAX_BYTES = 64 * 1024

_LIST = ("--format=json", "list", "sink-inputs")


class DuckError(Exception):
    """A pactl call or its output could not be trusted. Never carries output."""


# -- pactl ------------------------------------------------------------------------


def pactl(args: tuple[str, ...], timeout: float = CALL_S) -> bytes:
    """One bounded child, fixed argv, no shell. Only a listing and a *volume*
    write are allowed here: mute, default devices and every other command are
    refused before a process starts."""
    if args != _LIST:
        ok = (len(args) >= 3 and args[0] == "set-sink-input-volume"
              and re.fullmatch(r"[0-9]{1,10}", args[1]) is not None and int(args[1]) < 2**32 - 1
              and len(args) - 2 <= MAX_CHANNELS
              and all(re.fullmatch(r"[0-9]{1,7}", v) is not None and int(v) <= VOLUME_MAX for v in args[2:]))
        if not ok:
            raise DuckError("refused")
    if not 0 < timeout <= 10:
        raise DuckError("refused")
    process = None
    try:
        process = subprocess.Popen([str(audio.PACTL), *args], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True,
                                   env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                                        "PULSE_SERVER": audio.PULSE_SERVER})
        out, _ = process.communicate(timeout=timeout)
        if process.returncode != 0 or len(out) > audio.OUTPUT_CAP:
            raise DuckError("pactl failed")
        return out
    except (OSError, subprocess.TimeoutExpired):
        raise DuckError("pactl unavailable or too slow") from None
    finally:
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.communicate(timeout=1)
            except Exception:       # noqa: BLE001 - reaped in the background instead
                threading.Thread(target=process.wait, name="speech-duck-reap", daemon=True).start()


@dataclass(frozen=True)
class Stream:
    index: int
    pid: int
    muted: bool
    volume: tuple[int, ...]


def parse(raw: bytes) -> list[Stream]:
    """The sink-inputs that belong to a Unix process, reduced to what ducking
    reads. A stream with no PID is never ours and is dropped; anything malformed
    is an error (nothing is then written)."""
    try:
        rows = json.loads(raw)
        if not isinstance(rows, list) or len(rows) > audio.STREAM_CAP:
            raise ValueError
        out, seen = [], set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError
            index, muted, props = row.get("index"), row.get("mute"), row.get("properties")
            if (type(index) is not int or not 0 <= index < 2**32 - 1 or index in seen
                    or type(muted) is not bool or not isinstance(props, dict)):
                raise ValueError
            seen.add(index)
            pid = props.get("application.process.id")
            if pid is None:
                continue
            if not isinstance(pid, str) or re.fullmatch(r"[1-9][0-9]{0,9}", pid) is None or not 1 < int(pid) < 2**31:
                raise ValueError
            channels = row.get("volume")
            if not isinstance(channels, dict) or not 1 <= len(channels) <= MAX_CHANNELS:
                raise ValueError
            volume = []
            for entry in channels.values():
                value = entry.get("value") if isinstance(entry, dict) else None
                if type(value) is not int or not 0 <= value <= VOLUME_MAX:
                    raise ValueError
                volume.append(value)
            out.append(Stream(index, int(pid), muted, tuple(volume)))
        return out
    except (ValueError, TypeError, RecursionError):
        raise DuckError("unreadable listing") from None


def ducked(volume: tuple[int, ...]) -> tuple[int, ...]:
    """The duck level of a stream's per-channel volume (balance is kept)."""
    return tuple(max(1, round(v * FACTOR)) if v > 0 else 0 for v in volume)


# -- state ------------------------------------------------------------------------


@dataclass
class _Held:
    index: int
    pid: int
    prior: tuple[int, ...]
    applied: tuple[int, ...]
    at: float


@dataclass
class Residue:
    """A duck that was never confirmed restored: PulseAudio may hand `applied`
    to the next stream of the same application. `prior` is what it should be."""
    prior: tuple[int, ...]
    applied: tuple[int, ...]
    at: float


def _volumes(value) -> tuple[int, ...] | None:
    if (isinstance(value, list) and 1 <= len(value) <= MAX_CHANNELS
            and all(type(v) is int and 0 <= v <= VOLUME_MAX for v in value)):
        return tuple(value)
    return None


def _read_journal(path: Path) -> list[Residue]:
    """The journal, tolerating anything: missing, odd, oversized, non-regular or
    corrupt is empty. Read without blocking and never through a symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return []
            with os.fdopen(fd, "rb", closefd=False) as handle:
                data = handle.read(JOURNAL_MAX_BYTES + 1)
        finally:
            os.close(fd)
        if len(data) > JOURNAL_MAX_BYTES:
            return []
        raw = json.loads(data.decode("utf-8"))
        rows = raw.get("streams") if isinstance(raw, dict) else None
        out = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            prior, applied, at = _volumes(row.get("prior")), _volumes(row.get("applied")), row.get("at")
            if prior and applied and len(prior) == len(applied) and prior != applied \
                    and isinstance(at, (int, float)) and not isinstance(at, bool):
                out.append(Residue(prior, applied, float(at)))
        return out[:MAX_RESIDUE]
    except Exception:       # noqa: BLE001 - OSError, ValueError, UnicodeError, RecursionError ...
        return []


class Ducker:
    """The lease and the ducked streams. One per daemon (`daemon.speech_ducker`).

    `owners()` returns the pinned Remote Chrome roots (`audio.OwnedChrome`) of
    profiles whose sound is permitted and audible right now, or `[]`; it never
    starts the Browser. `run` is the pactl runner (tests inject a fake)."""

    def __init__(self, owners: Callable[[], list], *, run: Callable = pactl, clock=time.monotonic,
                 wall=time.time, journal: Callable[[], Path | None] | Path | None = None,
                 lease_s: float = LEASE_S, scan_s: float = SCAN_S, retry_s: float = RETRY_S,
                 idle_s: float = IDLE_POLL_S, threaded: bool = True):
        self._threaded = threaded            # tests drive `tick()` themselves
        self._owners, self._run, self._clock, self._wall = owners, run, clock, wall
        self._journal = journal
        self.lease_s, self.scan_s, self.retry_s, self.idle_s = lease_s, scan_s, retry_s, idle_s
        self._cv = threading.Condition()
        self._work = threading.Lock()        # one scan or restore at a time
        self._deadline: float | None = None
        self._stopped = False
        self._thread: threading.Thread | None = None
        self._held: dict[int, _Held] = {}
        self._free: set[int] = set()         # streams the owner moved: left alone for this lease
        self._residue: list[Residue] = []
        self._loaded = False
        self._failing = False

    # -- the lease ------------------------------------------------------------------

    def grant(self) -> bool:
        """Grant or renew the lease. False once the daemon is stopping."""
        with self._cv:
            if self._stopped:
                return False
            self._deadline = self._clock() + self.lease_s
            self._start_locked()
            self._cv.notify_all()
        return True

    def release(self) -> None:
        with self._cv:
            self._deadline = None
            self._cv.notify_all()

    def status(self) -> dict:
        with self._cv:
            on = self._deadline is not None and self._clock() < self._deadline and not self._stopped
        return {"on": on, "ducked": len(self._held), "lease_s": self.lease_s}

    def recover(self) -> None:
        """Daemon start: read the journal of a previous run. A leftover duck is
        repaired when a Remote stream next appears carrying its value."""
        with self._cv:
            if self._stopped:
                return
            self._load()
            if self._residue:
                self._start_locked()
                self._cv.notify_all()

    def stop(self) -> None:
        """Daemon shutdown: end the lease and put every stream back, bounded."""
        with self._cv:
            self._stopped = True
            self._deadline = None
            self._cv.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(STOP_S)
        if self._work.acquire(timeout=STOP_S):
            try:
                if self._held:
                    self._restore()
                    if self._held:
                        LOG.warning("speech duck: %d stream(s) could not be restored at shutdown; "
                                    "the journal repairs them at the next start", len(self._held))
            except Exception as exc:        # noqa: BLE001
                LOG.warning("speech duck: restore at shutdown failed (%s)", type(exc).__name__)
            finally:
                self._work.release()

    # -- the worker -----------------------------------------------------------------

    def _start_locked(self) -> None:
        if not self._threaded:
            return
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="speech-duck", daemon=True)
            self._thread.start()

    def _live_locked(self) -> bool:
        return self._deadline is not None and self._clock() < self._deadline and not self._stopped

    def _next_wait_locked(self) -> float | None:
        if self._live_locked():
            return max(0.01, min(self.scan_s, self._deadline - self._clock()))
        if self._held:
            return self.retry_s
        if self._residue:
            return self.idle_s
        return None

    def _loop(self) -> None:
        while True:
            with self._cv:
                if self._stopped:
                    return
            self.tick()
            with self._cv:
                if self._stopped:
                    return
                wait = self._next_wait_locked()
                self._cv.wait(wait)

    def tick(self) -> None:
        """One scan (lease live) or one restore (lease over). The worker calls it;
        tests call it directly with a fake clock."""
        with self._work:
            try:
                with self._cv:
                    live = self._live_locked()
                    self._load()
                if live:
                    self._duck()
                elif self._held:
                    self._restore()
                else:
                    self._free.clear()
                    if self._residue:
                        self._sweep()
                self._note(True)
            except DuckError:
                self._note(False)
            except Exception as exc:        # noqa: BLE001 - nothing here may kill the worker
                LOG.warning("speech duck: unexpected %s", type(exc).__name__)

    def _note(self, ok: bool) -> None:
        if ok and self._failing:
            LOG.info("speech duck: pactl is answering again")
        elif not ok and not self._failing:
            LOG.warning("speech duck: pactl failed or timed out; will retry (volumes are restored when it answers)")
        self._failing = not ok

    # -- scanning -------------------------------------------------------------------

    def _list(self) -> list[Stream]:
        return parse(self._run(_LIST, CALL_S))

    @staticmethod
    def _chain(owners, pid: int):
        for owner in owners:
            chain = owner.chain(pid)
            if chain is not None:
                return chain
        return None

    def _duck(self) -> None:
        owners = self._owners() or []
        if not owners and not self._held:
            return              # nothing audible: no pactl call at all
        by_index = {s.index: s for s in self._list()}
        self._reconcile(by_index)
        if not owners:
            return
        for stream in sorted(by_index.values(), key=lambda s: s.index):
            if stream.index in self._held or stream.index in self._free or stream.muted:
                continue
            if len(self._held) >= MAX_STREAMS:
                break
            chain = self._chain(owners, stream.pid)
            if chain is not None:
                self._duck_one(stream, chain, owners)

    def _reconcile(self, by_index: dict[int, Stream]) -> None:
        """Held streams that vanished become residue; ones the owner moved are theirs."""
        changed = False
        for held in list(self._held.values()):
            stream = by_index.get(held.index)
            if stream is None or stream.pid != held.pid:
                self._add_residue(held)
                del self._held[held.index]
                changed = True
            elif stream.volume != held.applied:
                del self._held[held.index]
                self._free.add(held.index)
                changed = True
        if changed:
            self._persist()

    def _duck_one(self, stream: Stream, chain, owners) -> None:
        prior = stream.volume
        leftover = next((r for r in self._residue if r.applied == stream.volume), None)
        if leftover is not None:
            prior = leftover.prior      # born ducked by PulseAudio's memory of a lost duck
        target = ducked(prior)
        # Index reuse is the one race pactl cannot close: look again, on the same
        # PID and volume, immediately before writing.
        fresh = {s.index: s for s in self._list()}.get(stream.index)
        if fresh is None or fresh.pid != stream.pid or fresh.volume != stream.volume or fresh.muted:
            return
        if self._chain(owners, stream.pid) != chain:
            return
        held = _Held(stream.index, stream.pid, prior, target, self._wall())
        self._held[stream.index] = held
        self._persist()                 # before the write: a crash leaves a record, never a silent duck
        if stream.volume != target:
            try:
                self._run(("set-sink-input-volume", str(stream.index), *map(str, target)), CALL_S)
            except DuckError:
                del self._held[stream.index]
                self._persist()
                raise
            actual = {s.index: s for s in self._list()}.get(stream.index)
            if actual is not None and actual.pid == stream.pid and actual.volume != target:
                held.applied = actual.volume    # what the server really holds is what "unchanged" means
        if leftover is not None and leftover in self._residue:
            self._residue.remove(leftover)      # only now: a failed write above keeps the record
        self._persist()

    # -- restoring ------------------------------------------------------------------

    def _restore(self) -> None:
        """End of lease: every held stream returns to its prior volume unless the
        owner moved it. A stream that cannot be written stays held and is retried."""
        by_index = {s.index: s for s in self._list()}
        failed = False
        for held in list(self._held.values()):
            stream = by_index.get(held.index)
            if stream is None or stream.pid != held.pid:
                self._add_residue(held)
                del self._held[held.index]
            elif stream.volume != held.applied:
                del self._held[held.index]          # the owner's change stands
            else:
                try:
                    self._run(("set-sink-input-volume", str(held.index), *map(str, held.prior)), CALL_S)
                    del self._held[held.index]
                except DuckError:
                    failed = True
        if not self._held:
            self._free.clear()
        self._persist()
        if failed:
            raise DuckError("restore incomplete")

    def _sweep(self) -> None:
        """No lease: repair a stream that was born carrying a lost duck."""
        now = self._wall()
        kept = [r for r in self._residue if now - r.at < RESIDUE_TTL_S]
        if len(kept) != len(self._residue):
            self._residue = kept
            self._persist()
        if not self._residue:
            return
        owners = self._owners() or []
        if not owners:
            return
        for stream in self._list():
            leftover = next((r for r in self._residue if r.applied == stream.volume), None)
            if leftover is None or stream.muted or self._chain(owners, stream.pid) is None:
                continue
            self._run(("set-sink-input-volume", str(stream.index), *map(str, leftover.prior)), CALL_S)
            self._residue.remove(leftover)
            self._persist()

    # -- the journal ----------------------------------------------------------------

    def _add_residue(self, held: _Held) -> None:
        self._residue = [r for r in self._residue if r.applied != held.applied]
        self._residue.append(Residue(held.prior, held.applied, held.at))
        del self._residue[:-MAX_RESIDUE]

    def _path(self) -> Path | None:
        journal = self._journal() if callable(self._journal) else self._journal
        return Path(journal) if journal is not None else None

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        path = self._path()
        if path is not None:
            self._residue = _read_journal(path)

    def _persist(self) -> None:
        """Held ducks and unconfirmed ones, or no file at all when there are none."""
        path = self._path()
        if path is None:
            return
        rows = [{"prior": list(h.prior), "applied": list(h.applied), "at": h.at} for h in self._held.values()]
        rows += [{"prior": list(r.prior), "applied": list(r.applied), "at": r.at} for r in self._residue]
        try:
            if not rows:
                path.unlink(missing_ok=True)
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"streams": rows[-MAX_RESIDUE:]}) + "\n")
            os.replace(tmp, path)
        except OSError as exc:
            LOG.warning("speech duck: journal not written (%s)", type(exc).__name__)


# -- the route --------------------------------------------------------------------


def route(handler, daemon, parts, query):
    """`/speech/duck`, or None."""
    if parts != ["speech", "duck"]:
        return None
    from .daemon import _object
    from .hud_api import fail
    from .projects import owner_only

    owner_only(handler, daemon)
    _object(query, ())
    ducker = daemon.speech_ducker
    if handler.command == "GET":
        return 200, ducker.status()
    if handler.command != "POST":
        return None
    body = _object(handler._body(), ("on",), ("on",))
    if type(body["on"]) is not bool:
        fail(400, "on must be a boolean")
    if body["on"]:
        if not ducker.grant():
            fail(409, "the daemon is stopping")
    else:
        ducker.release()
    return 200, ducker.status()
