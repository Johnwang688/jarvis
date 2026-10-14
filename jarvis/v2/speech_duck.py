"""Lowering the Remote Browser's sound while Jarvis speaks (voice plan V15, Q14).

Remote pages play through Chrome's own audio output, which WSLg's PulseAudio
carries to the owner's speakers (browser notes, "Browser sound"). While a
reply is read aloud the HUD holds a short **lease** on this module; for as long
as the lease lives, the sink-inputs of the daemon's own Remote Browser are
held about 15 dB lower, and every one is put back exactly when it ends.

    GET  /speech/duck            -> {"on": bool, "ducked": int, "lease_s": 3.0}
    POST /speech/duck {on: bool, window?: 32 hex} -> the same; on=true grants or renews
                                 that window's lease, on=false ends only that window's

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
import math
import os
import re
import stat
import subprocess
import tempfile
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
#: PA_VOLUME_NORM, 100%. Nothing here ever *writes* a channel above it: a duck lowers,
#: a restore gives back a volume that was at most this (a stream the owner boosted
#: beyond 100% is left alone), and a journal entry above it is forged or corrupt.
NORM = 65536
#: What a listing may report (the owner can boost a stream); only reading is bounded by this.
READ_MAX = 4 * NORM
MAX_RESIDUE = 16
JOURNAL_MAX_ROWS = MAX_RESIDUE + audio.MUTE_CAP     # held ducks and unconfirmed ones
RESIDUE_TTL_S = 30 * 24 * 3600.0
#: A journal entry stamped later than this is forged (or the clock moved a lot).
SKEW_S = 300.0
JOURNAL_MAX_BYTES = 64 * 1024
#: Windows each keep their own lease; this many are remembered at once.
MAX_WINDOWS = 16
#: One continuous duck never lasts longer than this, however it is renewed; then
#: the volumes rest this long, renewals or not, before a request can start a new one.
MAX_LEASE_S = 600.0
CAP_REST_S = 5.0
#: The idle leftover repair backs off to this when no matching stream appears.
IDLE_MAX_S = 60.0
_WINDOW = re.compile(r"[0-9a-f]{32}")

_LIST = ("--format=json", "list", "sink-inputs")


class DuckError(Exception):
    """A pactl call or its output could not be trusted. Never carries output."""


# -- pactl ------------------------------------------------------------------------


def pactl(args: tuple[str, ...], timeout: float = CALL_S) -> bytes:
    """One bounded child, fixed argv, no shell, through the same capped runner as
    the mute (`audio.run_bounded`). Only a listing and a *volume* write of at most
    100% per channel are allowed here: mute, default devices and every other
    command are refused before a process starts."""
    if args != _LIST:
        ok = (len(args) >= 3 and args[0] == "set-sink-input-volume"
              and re.fullmatch(r"[0-9]{1,10}", args[1]) is not None and int(args[1]) < 2**32 - 1
              and len(args) - 2 <= MAX_CHANNELS
              and all(re.fullmatch(r"[0-9]{1,6}", v) is not None and int(v) <= NORM for v in args[2:]))
        if not ok:
            raise DuckError("refused")
    if not 0 < timeout <= audio.COMMAND_S:
        raise DuckError("refused")
    try:
        return audio.run_bounded(args, timeout)
    except audio._UnsettledMute as exc:
        threading.Thread(target=exc.process.wait, name="speech-duck-reap", daemon=True).start()
        raise DuckError("pactl too slow") from None
    except audio.AudioRefused:
        raise DuckError("pactl failed") from None


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
                if type(value) is not int or not 0 <= value <= READ_MAX:
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
            and all(type(v) is int and 0 <= v <= NORM for v in value)):
        return tuple(value)
    return None


def _plausible(prior: tuple[int, ...], applied: tuple[int, ...]) -> bool:
    """A record this module could have written: `applied` is the duck of `prior`
    (give or take the server's rounding) and neither is above 100%. A journal is
    a file a hostile process might forge; it can only ever lower a volume."""
    if len(prior) != len(applied) or prior == applied:
        return False
    return all(abs(a - d) <= 2 for a, d in zip(applied, ducked(prior)))


def _read_journal(path: Path, now: float) -> list[Residue]:
    """The journal, tolerating anything: missing, odd, oversized, non-regular,
    corrupt, implausible or from the future is empty (per entry). Read without
    blocking and never through a symlink."""
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
            if (prior and applied and _plausible(prior, applied)
                    and isinstance(at, (int, float)) and not isinstance(at, bool)
                    and math.isfinite(at) and 0 <= at <= now + SKEW_S):
                out.append(Residue(prior, applied, float(at)))
        return out[:JOURNAL_MAX_ROWS]
    except Exception:       # noqa: BLE001 - OSError, ValueError, UnicodeError, RecursionError ...
        return []


def _clamp(volume: tuple[int, ...]) -> tuple[int, ...]:
    """What may be written: never above 100% in any channel."""
    return tuple(min(v, NORM) for v in volume)


class Ducker:
    """The leases and the ducked streams. One per daemon (`daemon.speech_ducker`).

    `owners()` returns the pinned Remote Chrome roots (`audio.OwnedChrome`) of
    profiles whose sound is permitted and audible right now, or `[]`; it never
    starts the Browser. `run` is the pactl runner (tests inject a fake).

    Every HUD window holds its own lease (a token it chose), so one window's
    `off` cannot end another's; the duck lasts while any lease lives, and never
    longer than `MAX_LEASE_S` in one go."""

    def __init__(self, owners: Callable[[], list], *, run: Callable = pactl, clock=time.monotonic,
                 wall=time.time, journal: Callable[[], Path | None] | Path | None = None,
                 lease_s: float = LEASE_S, scan_s: float = SCAN_S, retry_s: float = RETRY_S,
                 idle_s: float = IDLE_POLL_S, max_lease_s: float = MAX_LEASE_S, threaded: bool = True):
        self._threaded = threaded            # tests drive `tick()` themselves
        self._owners, self._run, self._clock, self._wall = owners, run, clock, wall
        self._journal = journal
        self.lease_s, self.scan_s, self.retry_s, self.idle_s = lease_s, scan_s, retry_s, idle_s
        self.max_lease_s = max_lease_s
        self._idle_now = idle_s
        self._cv = threading.Condition()
        self._work = threading.Lock()        # one scan or restore at a time
        self._leases: dict[str, float] = {}  # window token -> deadline (the injected clock)
        self._since: float | None = None     # when the current unbroken duck began
        self._rest_until = 0.0               # after the cap: no new lease before this
        self._stopped = False
        self._thread: threading.Thread | None = None
        self._held: dict[int, _Held] = {}
        self._free: set[int] = set()         # streams the owner moved: left alone for this lease
        self._residue: list[Residue] = []
        self._loaded = False
        self._failing = False

    # -- the lease ------------------------------------------------------------------

    def grant(self, window: str = "") -> bool:
        """Grant or renew `window`'s lease. False once the daemon is stopping."""
        with self._cv:
            if self._stopped:
                return False
            now = self._clock()
            self._live_locked()                     # drop what expired, and a duck past its cap
            if now < self._rest_until:
                return True                         # resting after the cap: heard, not honoured
            if not self._leases:
                self._since = now
            if window not in self._leases and len(self._leases) >= MAX_WINDOWS:
                del self._leases[min(self._leases, key=self._leases.get)]
            self._leases[window] = now + self.lease_s
            self._idle_now = self.idle_s
            self._start_locked()
            self._cv.notify_all()
        return True

    def release(self, window: str = "") -> None:
        with self._cv:
            self._leases.pop(window, None)
            self._cv.notify_all()

    def status(self) -> dict:
        with self._cv:
            on = self._live_locked()
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
        """Daemon shutdown: end every lease and put streams back, within one overall
        budget (`STOP_S`). Whatever is not back by then stays in the journal."""
        end = time.monotonic() + STOP_S
        with self._cv:
            self._stopped = True
            self._leases.clear()
            self._cv.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, min(STOP_S / 3, end - time.monotonic())))
        if self._work.acquire(timeout=max(0.0, end - time.monotonic())):
            try:
                if self._held:
                    try:
                        self._restore(end)
                    except DuckError:
                        pass
                    if self._held:
                        LOG.warning("speech duck: %d stream(s) could not be restored at shutdown; "
                                    "the journal repairs them at the next start", len(self._held))
            except Exception as exc:        # noqa: BLE001
                LOG.warning("speech duck: restore at shutdown failed (%s)", type(exc).__name__)
            finally:
                self._work.release()
        elif self._held:
            LOG.warning("speech duck: shutdown could not take the worker's turn; the journal repairs at the next start")

    # -- the worker -----------------------------------------------------------------

    def _start_locked(self) -> None:
        if not self._threaded:
            return
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="speech-duck", daemon=True)
            self._thread.start()

    def _live_locked(self) -> bool:
        """Is any lease alive (and under the cap)? Prunes as a side effect."""
        if self._stopped:
            return False
        now = self._clock()
        self._leases = {w: d for w, d in self._leases.items() if now < d}
        if self._leases and self._since is not None and now - self._since >= self.max_lease_s:
            self._leases.clear()                    # a window that renews for ever does not hold the volume for ever
            self._rest_until = now + CAP_REST_S
            LOG.warning("speech duck: one duck lasted %.0f s; restoring for %.0f s", self.max_lease_s, CAP_REST_S)
        return bool(self._leases)

    def _next_wait_locked(self) -> float | None:
        if self._live_locked():
            until = min(max(self._leases.values()), self._since + self.max_lease_s)
            return max(0.01, min(self.scan_s, until - self._clock()))
        if self._held:
            return self.retry_s
        if self._residue:
            return self._idle_now
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

    def _call(self, args: tuple[str, ...], deadline: float | None = None) -> bytes:
        """One pactl call, never longer than `CALL_S` nor past `deadline` (real monotonic time)."""
        timeout = CALL_S
        if deadline is not None:
            timeout = min(CALL_S, deadline - time.monotonic())
            if timeout < 0.05:
                raise DuckError("out of time")
        return self._run(args, timeout)

    def _list(self, deadline: float | None = None) -> list[Stream]:
        return parse(self._call(_LIST, deadline))

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
            if self._stopped:
                return
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
        if any(v > NORM for v in stream.volume):
            return              # boosted past 100% by the owner: giving it back would need a write above the norm
        prior = stream.volume
        # A stream born carrying exactly a duck level was handed it by PulseAudio's
        # memory of an earlier duck (a held one, or a lost one in the journal):
        # the volume to give back is that duck's prior, not this level.
        leftover = next((r for r in [*self._held.values(), *self._residue] if r.applied == stream.volume), None)
        if leftover is not None:
            prior = _clamp(leftover.prior)
        # A duck only ever lowers: never above what the stream has now, never above 100%.
        target = tuple(min(d, c, NORM) for d, c in zip(ducked(prior), stream.volume))
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
                self._call(("set-sink-input-volume", str(stream.index), *map(str, target)))
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

    def _restore(self, deadline: float | None = None) -> None:
        """End of lease: every held stream returns to its prior volume unless the
        owner moved it. The first pactl failure or timeout stops the writing (a
        server that is not answering is not asked again and again); what is left
        stays held, journaled, and is retried. `deadline` bounds the whole thing."""
        by_index = {s.index: s for s in self._list(deadline)}
        failed = False
        for held in list(self._held.values()):
            stream = by_index.get(held.index)
            if stream is None or stream.pid != held.pid:
                self._add_residue(held)
                del self._held[held.index]
            elif stream.volume != held.applied:
                del self._held[held.index]          # the owner's change stands
            elif not failed:
                try:
                    self._call(("set-sink-input-volume", str(held.index), *map(str, _clamp(held.prior))), deadline)
                    del self._held[held.index]
                except DuckError:
                    failed = True
            # after a failure: leave the rest held for the retry
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
        matched = False
        for stream in self._list():
            leftover = next((r for r in self._residue if r.applied == stream.volume), None)
            if leftover is None or stream.muted or self._chain(owners, stream.pid) is None:
                continue
            self._call(("set-sink-input-volume", str(stream.index), *map(str, _clamp(leftover.prior))))
            self._residue.remove(leftover)
            self._persist()
            matched = True
        # Nothing to repair while Chrome plays: look less and less often.
        self._idle_now = self.idle_s if matched else min(self._idle_now * 2, IDLE_MAX_S)

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
            self._residue = _read_journal(path, self._wall())

    def _persist(self) -> None:
        """Held ducks and unconfirmed ones, or no file at all when there are none.
        Held rows are never the ones cut: only the oldest leftovers are."""
        path = self._path()
        if path is None:
            return
        rows = [{"prior": list(h.prior), "applied": list(h.applied), "at": h.at} for h in self._held.values()]
        rows += [{"prior": list(r.prior), "applied": list(r.applied), "at": r.at} for r in self._residue[-MAX_RESIDUE:]]
        tmp = None
        try:
            if not rows:
                path.unlink(missing_ok=True)
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            # A random name made O_EXCL with mode 0600: nothing that already exists
            # (a FIFO, a link) can be opened by this, so it can never block the worker.
            fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
            tmp = name
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"streams": rows}) + "\n")
            os.replace(tmp, path)
            tmp = None
        except OSError as exc:
            LOG.warning("speech duck: journal not written (%s)", type(exc).__name__)
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


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
    body = _object(handler._body(), ("on", "window"), ("on",))
    if type(body["on"]) is not bool:
        fail(400, "on must be a boolean")
    window = body.get("window", "")
    if window != "" and not (isinstance(window, str) and _WINDOW.fullmatch(window)):
        fail(400, "window must be 32 lowercase hex characters")
    if body["on"]:
        if not ducker.grant(window):
            fail(409, "the daemon is stopping")
    else:
        ducker.release(window)
    return 200, ducker.status()
