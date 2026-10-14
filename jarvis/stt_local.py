"""Local speech-to-text (voice rework V13, plan 3.5, answered Q6).

Parakeet-TDT-0.6B-v3, int8, through `sherpa-onnx`, inside the daemon: the same
model class the cloud path uses (`nvidia/parakeet-tdt-0.6b-v3`), with no network
in the path. Measured in V0 (docs/notes/voice-face.md): real-time factor 0.042
at 2-4 threads, about 1.1 GB resident once loaded and 1.9 GB peak while loading,
and MORE than 4 threads was 4-18x slower, so the thread count is pinned
(`config.STT_LOCAL_THREADS`, default 2, clamped to 1..4).

What this module guarantees:

- **Optional.** `sherpa_onnx` is imported only when the model is first loaded;
  the daemon runs without it, and `status()` says what is missing.
- **Lazy, and not held forever.** Nothing loads at start. The first decode loads
  the model; after `config.STT_LOCAL_IDLE_MIN` idle minutes a daemon timer drops
  it (`Engine.reap`), because WSL memory is tight.
- **One decode at a time.** Requests queue on one lock, each waiting no longer
  than its own deadline. A caller that gives up marks its job cancelled, so a
  job that never started never runs: abandoned requests cannot stack up.
- **Bounded by the caller's deadline.** `transcribe(timeout=)` returns or raises
  within it (plus thread scheduling). The decode itself cannot be interrupted;
  an abandoned one finishes in the background (a clip is capped at
  `MAX_CLIP_S`, so that is seconds) while holding the lock.
- **Safe errors.** Every `LocalSTTError` message is a fixed short sentence: no
  path, no audio, no library text.
- **WAV only.** The HUD sends 16-bit PCM WAV. Anything else is
  `LocalSTTUnavailable` (the caller decides whether the cloud may take it); no
  media decoder is run on uploaded bytes.

- **Integrity.** `fetch` verifies the download (size cap, then SHA256 against the pin)
  and records each model file's own hash in `.verified`; `verify_model` re-checks them
  when the model loads (once per file identity per process). That guards against
  corruption, not tampering (the marker lives in the same writable directory). A model
  directory the owner supplies themselves (`JARVIS_STT_LOCAL_MODEL`) has no marker and
  is trusted as it is: only the download is verified; a marker that cannot be read
  refuses. The first load after a daemon start hashes about 640 MB, so the first clip
  may miss its local share of the budget and fall back to the cloud.

Deploy (the lead, once): install `sherpa-onnx` into the daemon's venv and run
`python -m jarvis.stt_local fetch` (about 490 MB, SHA256-pinned).
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import shutil
import stat
import sys
import tarfile
import tempfile
import threading
import time
import wave
from pathlib import Path

from . import config

MODEL_NAME = "parakeet-tdt-0.6b-v3 int8 (local)"
#: A clip longer than this is not decoded locally (measured to 60 s in V0;
#: attention memory grows with length). V4's chunking keeps clips under it.
MAX_CLIP_S = 90.0
#: A WAV this large is not ours (90 s of 48 kHz stereo 16-bit is about 17 MB).
MAX_WAV_BYTES = 32 << 20
FILES = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")
#: After a failed load, do not try again for this long (a broken file is not fixed by retrying).
LOAD_RETRY_S = 60.0

# The pinned download. tests/voice_stt_local_check.py asserts these equal V0's
# tests/voice_v0/models.json entry, so the two cannot drift.
MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
             "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2")
MODEL_SHA256 = "5793d0fd397c5778d2cf2126994d58e9d56b1be7c04d13c7a15bb1b4eafb16bf"
_MAX_UNPACKED = 3 << 30
_MAX_MEMBERS = 5000


class LocalSTTError(Exception):
    """A local decode did not produce text. The message is short and safe to show."""


class LocalSTTUnavailable(LocalSTTError):
    """Not installed, not loadable, or this clip is not something it takes."""


class LocalSTTTimeout(LocalSTTError):
    """The caller's deadline passed (waiting for the engine, loading, or decoding)."""


class LocalSTTFailed(LocalSTTError):
    """The decode itself failed."""


# ---- what is installed -------------------------------------------------------

def dependency_present() -> bool:
    try:
        return importlib.util.find_spec("sherpa_onnx") is not None
    except (ImportError, ValueError):
        return False


def model_dir() -> Path:
    return Path(config.STT_LOCAL_MODEL_DIR).expanduser()


def model_present(directory: Path | None = None) -> bool:
    d = directory or model_dir()
    try:
        return all((d / name).is_file() for name in FILES)
    except OSError:
        return False


_verified: dict[tuple, bool] = {}


MARKER_CAP = 64 << 10


def _open_regular(path: Path) -> tuple[int, os.stat_result]:
    """An fd for a regular file that is not a symlink, and its fstat; LocalSTTUnavailable otherwise.

    `O_NOFOLLOW` (a symlink to /dev/zero is refused, not read), `O_NONBLOCK` (opening a FIFO
    does not wait for a writer) and an `S_ISREG` check on the open fd (a FIFO or device is
    refused before a byte is read). The model directory is writable by anything running as the
    owner, so nothing in it may make the engine block or exhaust memory while it holds its lock.
    """
    again = "run `python -m jarvis.stt_local fetch` again"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        raise
    except OSError:
        raise LocalSTTUnavailable(f"a speech model file is a link or cannot be opened: {again}") from None
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        raise LocalSTTUnavailable(f"a speech model file cannot be read: {again}") from None
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise LocalSTTUnavailable(f"a speech model file is not a regular file: {again}")
    return fd, st


def _read_marker(path: Path) -> bytes | None:
    """The marker's bytes (at most MARKER_CAP), None if there is no marker; otherwise a refusal."""
    again = "run `python -m jarvis.stt_local fetch` again"
    try:
        fd, st = _open_regular(path)
    except FileNotFoundError:
        return None
    try:
        if st.st_size > MARKER_CAP:
            raise LocalSTTUnavailable(f"the speech model's record is too large: {again}")
        data = os.read(fd, MARKER_CAP + 1)
    except OSError:
        raise LocalSTTUnavailable(f"the speech model's record is unreadable: {again}") from None
    finally:
        os.close(fd)
    if len(data) > MARKER_CAP:
        raise LocalSTTUnavailable(f"the speech model's record is too large: {again}")
    return data


def _hash_file(path: Path, expected_size) -> tuple[tuple, str]:
    """(identity, sha256) of a model file, reading no more than its size. A file whose size is not
    the recorded one is not hashed at all."""
    fd, st = _open_regular(path)
    try:
        key = (str(path), st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        if isinstance(expected_size, int) and not isinstance(expected_size, bool) and expected_size != st.st_size:
            return key, ""
        if key in _verified:
            return key, ""                 # the caller reads the cached verdict
        digest, left = hashlib.sha256(), st.st_size
        while left > 0:
            block = os.read(fd, min(1 << 20, left))
            if not block:
                break
            digest.update(block)
            left -= len(block)
        return key, digest.hexdigest() if left == 0 else ""
    except OSError:
        raise LocalSTTUnavailable("a speech model file cannot be read: run `python -m jarvis.stt_local fetch` again") from None
    finally:
        os.close(fd)


def verify_model(directory: Path | None = None) -> None:
    """Check the model files against the hashes `fetch` recorded in `.verified`.

    This guards against CORRUPTION (a truncated copy, a bad disk, a half-finished
    download), not tampering: the marker sits in the same writable directory as the
    files, so anyone who can change a model file can change its recorded hash. Only a
    directory that `fetch` made has a marker. A directory without one (the owner's own,
    via `JARVIS_STT_LOCAL_MODEL`) is trusted as it is. A marker that is there but
    unreadable, not JSON, or without a `files` table is NOT trusted: it refuses, since
    "something was recorded and cannot be read" is the corrupt case. Hashing about 640 MB
    takes seconds, so each file identity (path, inode, size, mtime, ctime) is hashed once
    per process. Raises LocalSTTUnavailable ("run the fetch again") on any mismatch.

    Hostile entries cannot wedge the engine (this runs under its lock): the marker and
    every model file are opened `O_NOFOLLOW | O_NONBLOCK` and must be regular files, the
    marker is read to at most 64 KB, and each model file is read to at most its size (which
    must equal the size `fetch` recorded). A symlink, FIFO or device refuses at once.
    """
    d = directory or model_dir()
    again = "run `python -m jarvis.stt_local fetch` again"
    text = _read_marker(d / ".verified")
    if text is None:
        return
    try:
        record = json.loads(text)
        recorded = record.get("files") if isinstance(record, dict) else None
        sizes = record.get("sizes") if isinstance(record, dict) and isinstance(record.get("sizes"), dict) else {}
    except (ValueError, RecursionError, UnicodeError):
        recorded, sizes = None, {}
    if not isinstance(recorded, dict):
        raise LocalSTTUnavailable(f"the speech model's record is corrupt: {again}")
    for name in FILES:
        path = d / name
        try:
            key, digest = _hash_file(path, sizes.get(name))
        except FileNotFoundError:
            raise LocalSTTUnavailable(f"the speech model files are incomplete: {again}") from None
        if key not in _verified:
            _verified[key] = digest != "" and digest == recorded.get(name)
        if not _verified[key]:
            raise LocalSTTUnavailable(f"the speech model files do not match what was downloaded: {again}")


def installed() -> tuple[bool, str]:
    """(usable, reason when not). Cheap: no import of the library, no model read."""
    if not dependency_present():
        return False, "the sherpa-onnx package is not installed"
    if not model_present():
        return False, "the speech model is not downloaded"
    return True, ""


# ---- audio -------------------------------------------------------------------

def wav_samples(audio: bytes, mime: str = "audio/wav"):
    """(float32 mono samples, sample rate) from a PCM WAV; LocalSTTUnavailable for anything else.

    Whatever the bytes are (a fuzzed header, a lying frame count), nothing but
    LocalSTTUnavailable comes out: the HUD route would otherwise answer a 500.
    """
    try:
        return _wav_samples(audio, mime)
    except LocalSTTError:
        raise
    except Exception:  # noqa: BLE001 - wave/numpy errors on hostile bytes
        raise LocalSTTUnavailable("local speech-to-text takes 16-bit PCM WAV") from None


def _wav_samples(audio: bytes, mime: str):
    import numpy as np

    kind = (mime or "").split(";", 1)[0].strip().lower()
    if kind not in ("audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave") or audio[:4] != b"RIFF":
        raise LocalSTTUnavailable("local speech-to-text takes WAV audio only")
    if len(audio) > MAX_WAV_BYTES:
        raise LocalSTTUnavailable("clip too large for local speech-to-text")
    with wave.open(io.BytesIO(audio), "rb") as w:
        channels, width, rate, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        if width != 2 or channels < 1 or rate < 8000 or rate > 192000:
            raise LocalSTTUnavailable("local speech-to-text takes 16-bit PCM WAV")
        if frames / rate > MAX_CLIP_S:
            raise LocalSTTUnavailable("clip too long for local speech-to-text")
        raw = w.readframes(frames)
    pcm = np.frombuffer(raw[: len(raw) - len(raw) % (2 * channels)], dtype="<i2")
    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1)
    return (pcm.astype(np.float32) / 32768.0), rate


# ---- the recognizer ----------------------------------------------------------

class _Sherpa:
    """The real backend. `decode(samples, rate) -> text`; sherpa resamples to 16 kHz itself."""

    def __init__(self, directory: Path, threads: int):
        import sherpa_onnx

        self.recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(directory / FILES[0]), decoder=str(directory / FILES[1]),
            joiner=str(directory / FILES[2]), tokens=str(directory / FILES[3]),
            num_threads=threads, model_type="nemo_transducer", decoding_method="greedy_search",
        )

    def decode(self, samples, rate: int) -> str:
        stream = self.recognizer.create_stream()
        stream.accept_waveform(rate, samples)
        self.recognizer.decode_stream(stream)
        return stream.result.text

    def close(self) -> None:
        self.recognizer = None


def _default_factory(threads: int):
    ok, why = installed()
    if not ok:
        raise LocalSTTUnavailable(f"local speech-to-text is not set up: {why}")
    verify_model()
    return _Sherpa(model_dir(), threads)


class _Job:
    __slots__ = ("samples", "rate", "deadline", "done", "text", "error", "cancelled")

    def __init__(self, samples, rate, deadline):
        self.samples, self.rate, self.deadline = samples, rate, deadline
        self.done = threading.Event()
        self.text = ""
        self.error: Exception | None = None
        self.cancelled = False


class Engine:
    """One lazily loaded recognizer, one decode at a time, unloaded when idle.

    `factory(threads)` builds a backend with `decode(samples, rate) -> str` (and
    optionally `close()`); tests pass a fake. `clock` is a monotonic clock in
    seconds. `schedule=False` leaves the idle timer off so a test calls
    `reap()` itself.
    """

    def __init__(self, factory=None, *, threads: int | None = None, idle_s: float | None = None,
                 clock=time.monotonic, schedule: bool = True):
        self._factory = factory or _default_factory
        self._threads = threads if threads is not None else config.STT_LOCAL_THREADS
        self._idle_s = idle_s if idle_s is not None else config.STT_LOCAL_IDLE_MIN * 60
        self._clock = clock
        self._schedule = schedule
        self._lock = threading.Lock()          # one decode (or load, or unload) at a time
        self._state = threading.Lock()         # guards the fields below
        self._backend = None
        self._last_used = 0.0
        self._timer: threading.Timer | None = None
        self._load_failed_at: float | None = None
        self._waiting = 0
        self.loads = 0
        self.decodes = 0

    # -- state --------------------------------------------------------------
    @property
    def loaded(self) -> bool:
        with self._state:
            return self._backend is not None

    @property
    def waiting(self) -> int:
        with self._state:
            return self._waiting

    @property
    def threads(self) -> int:
        return self._threads

    @property
    def idle_s(self) -> float:
        return self._idle_s

    # -- the one entry point --------------------------------------------------
    def transcribe(self, audio: bytes, mime: str = "audio/wav", *, timeout: float) -> str:
        """Text for one WAV clip, within `timeout` seconds; a LocalSTTError otherwise."""
        samples, rate = wav_samples(audio, mime)
        if len(samples) == 0:
            return ""
        job = _Job(samples, rate, self._clock() + max(0.0, timeout))
        with self._state:
            self._waiting += 1
        threading.Thread(target=self._run, args=(job,), name="stt-local", daemon=True).start()
        if not job.done.wait(max(0.0, timeout)):
            job.cancelled = True
            raise LocalSTTTimeout("local speech-to-text ran out of time")
        if job.error is not None:
            raise job.error
        return job.text

    def _run(self, job: _Job) -> None:
        try:
            if not self._acquire(job):
                job.error = LocalSTTTimeout("local speech-to-text ran out of time")
                return
            try:
                if job.cancelled:
                    return                      # the caller left while we queued: do not decode
                backend = self._load()
                if job.cancelled:
                    return
                job.text = (backend.decode(job.samples, job.rate) or "").strip()
                with self._state:
                    self.decodes += 1
            finally:
                with self._state:
                    self._last_used = self._clock()
                self._lock.release()
                if self.loaded:
                    self._arm()
        except LocalSTTError as exc:
            job.error = exc
        except Exception as exc:  # noqa: BLE001 - library text never reaches a caller
            job.error = LocalSTTFailed(f"local decode failed ({type(exc).__name__})")
        finally:
            with self._state:
                self._waiting -= 1
            job.done.set()

    def _acquire(self, job: _Job) -> bool:
        """Wait for the engine in short slices, so a caller that has left (or a deadline that
        has passed) frees this thread at once instead of at the end of someone else's decode."""
        while True:
            if job.cancelled or job.deadline - self._clock() <= 0:
                return False
            if self._lock.acquire(timeout=min(0.05, max(0.0, job.deadline - self._clock()))):
                return True

    def _load(self):
        with self._state:
            backend = self._backend
            failed_at = self._load_failed_at
        if backend is not None:
            return backend
        if failed_at is not None and self._clock() - failed_at < LOAD_RETRY_S:
            raise LocalSTTUnavailable("local speech-to-text failed to load")
        try:
            backend = self._factory(self._threads)
        except LocalSTTError:
            with self._state:
                self._load_failed_at = self._clock()
            raise
        except Exception as exc:  # noqa: BLE001
            with self._state:
                self._load_failed_at = self._clock()
            raise LocalSTTUnavailable(f"local speech-to-text failed to load ({type(exc).__name__})") from None
        with self._state:
            self._backend, self._load_failed_at = backend, None
            self.loads += 1
        return backend

    # -- idle unload ------------------------------------------------------------
    def _arm(self) -> None:
        if not self._schedule:
            return
        with self._state:
            if self._timer is not None:
                self._timer.cancel()
            timer = threading.Timer(self._idle_s, self.reap)
            timer.daemon = True
            self._timer = timer
        timer.start()

    def reap(self) -> bool:
        """Unload when idle for `idle_s` and no decode is running; re-arm otherwise. True if unloaded."""
        if not self._lock.acquire(blocking=False):
            self._arm()                          # busy: look again later
            return False
        try:
            with self._state:
                idle = self._clock() - self._last_used
                if self._backend is None:
                    return False
                if idle < self._idle_s:
                    rearm = True
                else:
                    rearm = False
                    backend, self._backend = self._backend, None
            if rearm:
                self._arm()
                return False
            self._close(backend)
            return True
        finally:
            self._lock.release()

    def unload(self) -> bool:
        """Drop the model now (blocks behind a running decode). True if one was loaded."""
        with self._lock:
            with self._state:
                backend, self._backend = self._backend, None
                timer, self._timer = self._timer, None
            if timer is not None:
                timer.cancel()
            if backend is None:
                return False
            self._close(backend)
            return True

    @staticmethod
    def _close(backend) -> None:
        close = getattr(backend, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass
        import gc

        gc.collect()
        try:        # hand freed pages back to the OS (best effort, glibc only)
            import ctypes

            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:  # noqa: BLE001
            pass


# ---- the daemon's engine -------------------------------------------------------

_engine: Engine | None = None
_engine_lock = threading.Lock()


def engine() -> Engine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = Engine()
        return _engine


def set_engine(replacement: Engine | None) -> Engine | None:
    """Swap the process engine (tests); returns the previous one."""
    global _engine
    with _engine_lock:
        previous, _engine = _engine, replacement
    return previous


def available() -> bool:
    """Whether a local decode can be attempted at all (dependency and model present).

    A test engine installed with `set_engine` counts as available: the check is
    about the process's engine, not the files.
    """
    with _engine_lock:
        custom = _engine is not None and _engine._factory is not _default_factory
    return True if custom else installed()[0]


def status() -> dict:
    """What Settings shows. Never loads the model."""
    ok, why = installed()
    with _engine_lock:
        eng = _engine
    return {
        "installed": ok,
        "reason": why,
        "loaded": bool(eng and eng.loaded),
        "threads": eng.threads if eng else config.STT_LOCAL_THREADS,
        "idle_unload_min": round((eng.idle_s if eng else config.STT_LOCAL_IDLE_MIN * 60) / 60, 2),
        "model": MODEL_NAME,
        "fetch": "python -m jarvis.stt_local fetch",
    }


# ---- download (explicit, owner-run) --------------------------------------------

MAX_DOWNLOAD = 700 << 20        # the pinned archive is 487 MB; anything much bigger is not it


def fetch(dest: Path | None = None, *, url: str = MODEL_URL, sha256: str = MODEL_SHA256,
          max_bytes: int = MAX_DOWNLOAD) -> Path:
    """Download, verify and unpack the pinned model into `dest` (default `model_dir()`).

    The download is cut off at `max_bytes` before anything is hashed; the archive is
    hashed before anything is extracted, unpacked with tarfile's `data` filter under
    member and size caps into a fresh directory, and only the four model files are
    moved into place, with their own hashes recorded in `.verified` (checked again at
    load by `verify_model`). An existing `dest` is replaced only if it carries that
    marker, i.e. is an earlier download of this model: a directory the owner made is
    never deleted. Raises SystemExit with a sentence on any failure; nothing is left
    half-written at `dest`.
    """
    import urllib.request

    dest = Path(dest) if dest else model_dir()
    if dest.exists() and not (dest / ".verified").is_file():
        raise SystemExit(f"{dest} exists and is not an earlier download of this model "
                         "(no .verified marker); remove it yourself or choose another folder")
    dest.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".stt-fetch-", dir=dest.parent))
    try:
        archive = work / "model.tar.bz2"
        print(f"downloading {url} ...", file=sys.stderr)
        req = urllib.request.Request(url, headers={"User-Agent": "jarvis-stt/1"})
        digest, size = hashlib.sha256(), 0
        with urllib.request.urlopen(req, timeout=120) as resp, archive.open("wb") as out:
            for block in iter(lambda: resp.read(1 << 20), b""):
                size += len(block)
                if size > max_bytes:
                    raise SystemExit(f"download is larger than {max_bytes >> 20} MB: not the pinned model, stopped")
                digest.update(block)
                out.write(block)
        if digest.hexdigest() != sha256:
            raise SystemExit(f"sha256 mismatch: got {digest.hexdigest()}, pinned {sha256}")
        unpacked = work / "x"
        unpacked.mkdir()
        with tarfile.open(archive) as tar:
            members = tar.getmembers()
            if len(members) > _MAX_MEMBERS or sum(m.size for m in members) > _MAX_UNPACKED:
                raise SystemExit("archive too large to unpack")
            tar.extractall(unpacked, filter="data")
        roots = [p for p in unpacked.iterdir()]
        root = roots[0] if len(roots) == 1 and roots[0].is_dir() else unpacked
        if not model_present(root):
            raise SystemExit("the archive does not hold the expected model files")
        staged = work / "model"
        staged.mkdir()
        hashes, sizes = {}, {}
        for name in FILES:
            shutil.move(str(root / name), str(staged / name))
            h = hashlib.sha256()
            with (staged / name).open("rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    h.update(block)
            hashes[name] = h.hexdigest()
            sizes[name] = (staged / name).stat().st_size
        (staged / ".verified").write_text(json.dumps({"archive_sha256": sha256, "files": hashes, "sizes": sizes}, indent=1) + "\n")
        if dest.exists():
            shutil.rmtree(dest)
        os.replace(staged, dest)
        return dest
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str]) -> int:
    if argv[:1] == ["fetch"]:
        path = fetch()
        print(f"ok: {path}")
        return 0
    if argv[:1] == ["status"]:
        for key, value in status().items():
            print(f"{key}: {value}")
        return 0
    print("usage: python -m jarvis.stt_local fetch | status", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
