"""Speaker enrollment and verification (voice rework V12, plan 3.4, V-2, V-12, V-14).

Whose voice is it? The daemon answers that BEFORE any speech-to-text call, so a
voice that is not the owner's never reaches a cloud model. NeMo TitaNet-small
through `sherpa-onnx` (V0: EER 0.0 to 1.9 % on synthetic voices, 34 ms for 3 s of
audio on one thread, about 80 MB resident) turns the first 3 s of speech into a
192-number vector; the owner's enrollment is the mean of such vectors from three
prompted readings; the cosine similarity of the two is compared with a threshold.

What this module guarantees:

- **Local only.** Nothing here imports a network client except `fetch()`, which
  only the owner runs by hand. The store is never uploaded, and no function here
  returns audio or an embedding to a caller (`status()` carries counts, a
  threshold and the last score, nothing else).
- **Off until enrolled (V-2), and off for a Guest (V-14).** With no voiceprint
  there is nothing to verify against and `verify()` says so. The Guest switch is
  one small file beside the voiceprint, so it survives a daemon restart and a HUD
  reload until the owner turns it off. It skips verification and nothing else:
  this module has no knowledge of the microphone, the VAD, noise suppression,
  self-voice gating or an approval card, so there is nothing for it to relax.
- **Fail closed.** Enrolled, and the voiceprint is damaged, was made by another
  model, the engine is not installed, or the clip cannot be decoded: the answer is
  an error (and `/stt` uploads nothing), never "accepted". The way out is the
  owner's: Delete, re-enroll, or Guest voice. An unreadable Guest file is
  "Guest off", the safe direction.
- **Optional dependency.** `sherpa_onnx` is imported only when the model is first
  loaded. The daemon runs without it, and `status()` says what is missing.
- **Lazy, and not held forever.** The first verification loads the model; after
  `config.SPEAKER_IDLE_MIN` idle minutes a daemon timer drops it. Threads are
  pinned (`config.SPEAKER_THREADS`).
- **The store** is `config.SPEAKER_DIR`, mode 0700, owned by the owner. Every
  file is opened relative to a directory descriptor with O_NOFOLLOW, read only
  if it is a small regular file (a FIFO, a device or a symlink is refused, never
  waited on), and written atomically. A hostile or corrupt file is "damaged",
  never an exception that escapes and never an accept.
- **Bounded.** At most `READINGS_MAX` readings of at most `MAX_READING_S` each.

Deploy (the lead, once): install `sherpa-onnx` into the daemon's venv and run
`python -m jarvis.speaker fetch` (40 MB, SHA256-pinned).
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import math
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import config

MODEL_NAME = "nemo_en_titanet_small"
# The pinned download. tests/voice_speaker_check.py asserts these equal V0's
# tests/voice_v0/models.json entry, so the two cannot drift.
MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
             "nemo_en_titanet_small.onnx")
MODEL_SHA256 = "ad4a1802485d8b34c722d2a9d04249662f2ece5d28a7a039063ca22f515a789e"

#: How much of a clip is embedded (plan 3.4: "the first <= 3 s").
WINDOW_S = 3.0
#: Less voiced audio than this in the window is too little to say whose it is (the HUD's own `too_short`, 350 ms).
MIN_VOICED_S = 0.35
#: A clip is decoded only this far in (a 25 s chunk is verified by its first seconds).
VERIFY_READ_S = 12.0
MAX_VERIFY_WAV_BYTES = 16 << 20
#: Waiting for the engine (a decode runs at a time), then giving up, fail closed.
VERIFY_TIMEOUT_S = 10.0

READINGS_MIN = 3
READINGS_MAX = 6
MIN_READING_S = 6.0
MAX_READING_S = 30.0
MAX_READING_BYTES = 4 << 20
#: What the HUD and the daemon both hold a prompted reading to (the HUD's live
#: meter reads these from `status()["quality"]`; the daemon is the judge).
QUALITY = {
    "min_s": MIN_READING_S, "max_s": MAX_READING_S,
    "min_level_db": -40.0, "max_level_db": -6.0,     # RMS of the spoken frames, dBFS
    "max_clip": 0.005,                                # share of samples at full scale
    "min_speech": 0.45,                               # share of 20 ms frames that are speech
}
#: The prompt the HUD shows for a reading (about 10 s read aloud; no wake word in it).
PROMPTS = (
    "The quick brown fox jumps over the lazy dog while the morning light spreads across the quiet kitchen table.",
    "Please read this second passage at your usual pace and volume, sitting where you normally sit when you talk to the computer.",
    "For the last reading, say anything you like for about ten seconds, such as what you plan to work on this afternoon.",
)
#: The sentence every built-in voice speaks to be scored as a free impostor.
IMPOSTOR_TEXT = "Hello, this is a short test of the voice that is speaking right now, and nothing more."
IMPOSTOR_BUDGET_S = 150.0
IMPOSTOR_MAX = 64

_FILE_CAP = 256 * 1024          # voiceprint.json / guest.json / impostors.json are far smaller
_IMPOSTOR_CAP = 1 << 20
_MAX_DIM = 4096
_GATE_FLOOR = 10 ** (-55 / 20)  # a frame quieter than -55 dBFS is never speech
_FRAME_S = 0.02
_READING_NAME = re.compile(r"r-(\d{3})\.wav")


class SpeakerError(Exception):
    """Nothing here carries a path, audio, a vector or library text: every message is a sentence."""


class NotOwner(SpeakerError):
    """The voice is not the enrolled one (HTTP 422). Nothing was transcribed or uploaded."""


class VerifyUnavailable(SpeakerError):
    """Enrolled, but this clip cannot be judged (HTTP 503). Fail closed."""


class VerifyUnsupported(SpeakerError):
    """Enrolled, and the audio is not something the verifier decodes (HTTP 415)."""


class StoreError(SpeakerError):
    """The speaker folder is not safe to use."""


class EnrollError(SpeakerError):
    """An enrollment step cannot be done now (HTTP 409)."""


# ---- what is installed -------------------------------------------------------

def dependency_present() -> bool:
    try:
        return importlib.util.find_spec("sherpa_onnx") is not None and importlib.util.find_spec("numpy") is not None
    except (ImportError, ValueError):
        return False


def model_path() -> Path:
    return Path(config.SPEAKER_MODEL_PATH).expanduser()


def model_present(path: Path | None = None) -> bool:
    try:
        return (path or model_path()).is_file()
    except OSError:
        return False


def installed() -> tuple[bool, str]:
    """(usable, reason when not). Cheap: no import of the library, no model read."""
    if not dependency_present():
        return False, "the sherpa-onnx package is not installed"
    if not model_present():
        return False, "the speaker model is not downloaded"
    return True, ""


def _np():
    try:
        import numpy as np
    except ImportError:
        raise VerifyUnavailable("speaker verification needs numpy") from None
    return np


# ---- audio -------------------------------------------------------------------

def decode_wav(audio: bytes, mime: str = "audio/wav", *, max_seconds: float, max_bytes: int = MAX_VERIFY_WAV_BYTES):
    """(float32 mono samples, rate, whole clip's seconds) from a PCM WAV.

    Only the first `max_seconds` are decoded. Raises ValueError with a sentence
    for anything else: no media decoder ever runs on uploaded bytes.
    """
    np = _np()
    kind = (mime or "").split(";", 1)[0].strip().lower()
    if kind not in ("audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave") or audio[:4] != b"RIFF":
        raise ValueError("speaker checks take WAV audio only")
    if len(audio) > max_bytes:
        raise ValueError("clip too large")
    try:
        with wave.open(io.BytesIO(audio), "rb") as w:
            channels, width, rate, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            if width != 2 or not 1 <= channels <= 2 or not 8000 <= rate <= 48000:
                raise ValueError("speaker checks take 16-bit PCM WAV, 8 to 48 kHz")
            raw = w.readframes(min(frames, int(rate * max_seconds)))
    except ValueError:
        raise
    except (wave.Error, EOFError, OSError):
        raise ValueError("speaker checks take 16-bit PCM WAV") from None
    pcm = np.frombuffer(raw[: len(raw) - len(raw) % (2 * channels)], dtype="<i2")
    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1)
    return (pcm.astype(np.float32) / 32768.0), rate, frames / rate


def _frames(x, rate):
    """(per-20ms-frame RMS, frame length)."""
    np = _np()
    n = max(1, int(rate * _FRAME_S))
    k = len(x) // n
    if k == 0:
        return np.zeros(0), n
    f = x[: k * n].astype(np.float64).reshape(k, n)
    return np.sqrt((f * f).mean(axis=1)), n


def _voiced(rms):
    """Boolean per frame: louder than -55 dBFS and within 14 dB of the clip's loud frames."""
    np = _np()
    if rms.size == 0:
        return rms > 1
    return rms >= max(_GATE_FLOOR, 0.2 * float(np.percentile(rms, 95)))


def _first_speech(voiced) -> int | None:
    """The first frame that starts a run of speech (3 of 5 frames), or None."""
    np = _np()
    for i in np.flatnonzero(voiced):
        if int(voiced[i: i + 5].sum()) >= 3:
            return int(i)
    return None


def voiced_window(x, rate, seconds: float = WINDOW_S):
    """The first `seconds` of speech, starting just before it, as (samples, voiced seconds); None if too little."""
    rms, n = _frames(x, rate)
    voiced = _voiced(rms)
    start = _first_speech(voiced)
    if start is None:
        return None
    first = max(0, start - 5)
    window = x[first * n: first * n + int(seconds * rate)]
    spoken = float(voiced[first: first + int(seconds / _FRAME_S)].sum()) * _FRAME_S
    return (window, spoken) if spoken >= MIN_VOICED_S else None


def reading_windows(x, rate):
    """Non-overlapping 3 s windows over a prepared reading, each at least half speech and 2 s long."""
    rms, n = _frames(x, rate)
    voiced = _voiced(rms)
    start = _first_speech(voiced)
    if start is None:
        return []
    out, size = [], int(WINDOW_S * rate)
    at = max(0, start - 5) * n
    while at + int(2.0 * rate) <= len(x):
        piece = x[at: at + size]
        share = float(voiced[at // n: (at + len(piece)) // n].mean()) if len(piece) >= n else 0.0
        if share >= 0.5:
            out.append(piece)
        at += size
    return out


def quality(x, rate) -> tuple[dict, list[str]]:
    """Metrics for a prepared reading and the problems with it (empty when it is good)."""
    np = _np()
    seconds = len(x) / rate if rate else 0.0
    rms, _ = _frames(x, rate)
    voiced = _voiced(rms)
    share = float(voiced.mean()) if rms.size else 0.0
    spoken = rms[voiced]
    level = 20 * math.log10(max(float(np.sqrt((spoken ** 2).mean())) if spoken.size else 0.0, 1e-9))
    clip = float((np.abs(x) >= 0.99).mean()) if len(x) else 0.0
    metrics = {"seconds": round(seconds, 2), "level_db": round(level, 1), "clipping": round(clip, 4), "speech": round(share, 2)}
    q, problems = QUALITY, []
    if seconds < q["min_s"]:
        problems.append("too short: read the whole passage")
    if seconds > q["max_s"]:
        problems.append("too long: stop after the passage")
    if share < q["min_speech"] or not spoken.size:
        problems.append("too much silence: speak right away and keep going")
    elif level < q["min_level_db"]:
        problems.append("too quiet: speak closer to the microphone")
    elif level > q["max_level_db"]:
        problems.append("too loud: move back from the microphone")
    if clip > q["max_clip"]:
        problems.append("the sound is clipping: speak more softly")
    return metrics, problems


def encode_wav(x, rate: int) -> bytes:
    np = _np()
    pcm = (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buffer.getvalue()


# ---- the embedding engine ----------------------------------------------------

#: The model is 40 MB; nothing near 64 MB is it.
MAX_MODEL_BYTES = 64 << 20


def _expected_sha() -> str:
    given = (os.environ.get("JARVIS_SPEAKER_MODEL_SHA256") or "").strip().lower()
    return given if re.fullmatch(r"[0-9a-f]{64}", given) else MODEL_SHA256


@contextlib.contextmanager
def verified_model(path: Path, sha256: str | None = None):
    """Yield a path to exactly the bytes that were hashed (L4).

    The file is opened `O_NOFOLLOW` (a symlink, a FIFO or a device is refused, never waited on), must be a regular
    file of at most `MAX_MODEL_BYTES`, and its SHA-256 must be the pinned one. The bytes that were hashed are
    copied into an anonymous in-memory file (`memfd`, Linux) and that is what the loader is pointed at
    (`/proc/self/fd/N`), so neither a swap nor an in-place rewrite of the file after the check changes what is
    loaded. Called once per model load, not per clip. Where `memfd_create` does not exist the verified path is
    handed out as is.
    """
    want = sha256 or _expected_sha()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        raise VerifyUnavailable("the speaker model file cannot be opened safely") from None
    held = -1
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or not 0 < st.st_size <= MAX_MODEL_BYTES:
            raise VerifyUnavailable("the speaker model file is not a regular file of the expected size")
        try:
            held = os.memfd_create("speaker-model", os.MFD_CLOEXEC)
        except (AttributeError, OSError):
            held = -1
        digest, size = hashlib.sha256(), 0
        while True:
            block = os.pread(fd, 1 << 20, size)
            if not block:
                break
            size += len(block)
            if size > MAX_MODEL_BYTES:
                raise VerifyUnavailable("the speaker model file is not a regular file of the expected size")
            digest.update(block)
            if held >= 0:
                os.write(held, block)
        if digest.hexdigest() != want:
            raise VerifyUnavailable("the speaker model file is not the pinned one: run `python -m jarvis.speaker fetch`")
        proc = f"/proc/self/fd/{held}"
        yield proc if held >= 0 and os.path.exists(proc) else str(path)
    finally:
        os.close(fd)
        if held >= 0:
            os.close(held)


class _Sherpa:
    """The real backend. `embed(samples, rate) -> floats`; sherpa resamples to 16 kHz itself."""

    def __init__(self, path: Path, threads: int):
        import sherpa_onnx

        with verified_model(path) as checked:
            cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=checked, num_threads=threads, debug=False)
            self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)

    def embed(self, samples, rate: int):
        stream = self.extractor.create_stream()
        stream.accept_waveform(sample_rate=rate, waveform=samples)
        stream.input_finished()
        return self.extractor.compute(stream)

    def close(self) -> None:
        self.extractor = None


def _default_factory(threads: int):
    ok, why = installed()
    if not ok:
        raise VerifyUnavailable(f"speaker verification is not set up: {why}")
    return _Sherpa(model_path(), threads)


#: After a failed load, do not try again for this long (a broken file is not fixed by retrying).
LOAD_RETRY_S = 30.0


class Engine:
    """One lazily loaded embedder, one embedding at a time, unloaded when idle.

    `factory(threads)` builds a backend with `embed(samples, rate) -> floats` (and
    optionally `close()`); tests pass a fake. `clock` is a monotonic clock in
    seconds. `schedule=False` leaves the idle timer off so a test calls `reap()`.
    """

    def __init__(self, factory=None, *, threads: int | None = None, idle_s: float | None = None,
                 clock=time.monotonic, schedule: bool = True, model: str = MODEL_NAME):
        self._factory = factory or _default_factory
        self._threads = threads if threads is not None else config.SPEAKER_THREADS
        self._idle_s = idle_s if idle_s is not None else config.SPEAKER_IDLE_MIN * 60
        self._clock = clock
        self._schedule = schedule
        self.model = model
        self._lock = threading.Lock()         # one embedding (or load, or unload) at a time
        self._state = threading.Lock()        # guards the fields below
        self._backend = None
        self._last_used = 0.0
        self._timer: threading.Timer | None = None
        self._load_failed_at: float | None = None
        self.loads = 0
        self.embeds = 0

    @property
    def loaded(self) -> bool:
        with self._state:
            return self._backend is not None

    @property
    def threads(self) -> int:
        return self._threads

    @property
    def idle_s(self) -> float:
        return self._idle_s

    def embed(self, samples, rate: int, *, timeout: float = VERIFY_TIMEOUT_S):
        """A unit-length float64 vector for `samples`, or VerifyUnavailable."""
        np = _np()
        if not self._lock.acquire(timeout=max(0.0, timeout)):
            raise VerifyUnavailable("speaker verification is busy")
        try:
            backend = self._load()
            try:
                raw = backend.embed(samples, rate)
            except SpeakerError:
                raise
            except Exception as exc:  # noqa: BLE001 - library text never reaches a caller
                raise VerifyUnavailable(f"speaker verification failed ({type(exc).__name__})") from None
            with self._state:
                self.embeds += 1
        finally:
            with self._state:
                self._last_used = self._clock()
            self._lock.release()
            if self.loaded:
                self._arm()
        return _unit(raw, np)

    def _load(self):
        with self._state:
            backend, failed_at = self._backend, self._load_failed_at
        if backend is not None:
            return backend
        if failed_at is not None and self._clock() - failed_at < LOAD_RETRY_S:
            raise VerifyUnavailable("speaker verification failed to load")
        try:
            backend = self._factory(self._threads)
        except SpeakerError:
            with self._state:
                self._load_failed_at = self._clock()
            raise
        except Exception as exc:  # noqa: BLE001
            with self._state:
                self._load_failed_at = self._clock()
            raise VerifyUnavailable(f"speaker verification failed to load ({type(exc).__name__})") from None
        with self._state:
            self._backend, self._load_failed_at = backend, None
            self.loads += 1
        return backend

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
        """Unload when idle for `idle_s` and nothing is running; re-arm otherwise. True if unloaded."""
        if not self._lock.acquire(blocking=False):
            self._arm()
            return False
        try:
            with self._state:
                if self._backend is None:
                    return False
                if self._clock() - self._last_used < self._idle_s:
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


def _unit(raw, np):
    """A finite, nonzero vector of a sane length, scaled to length 1."""
    try:
        v = np.asarray(raw, dtype=np.float64).reshape(-1)
    except (TypeError, ValueError):
        raise VerifyUnavailable("speaker verification failed (bad embedding)") from None
    if not 2 <= v.size <= _MAX_DIM or not np.isfinite(v).all():
        raise VerifyUnavailable("speaker verification failed (bad embedding)")
    norm = float(np.linalg.norm(v))
    if norm < 1e-9:
        raise VerifyUnavailable("speaker verification failed (bad embedding)")
    return v / norm


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


def available() -> tuple[bool, str]:
    """Whether an embedding can be attempted at all. A test engine installed with `set_engine` counts."""
    with _engine_lock:
        custom = _engine is not None and _engine._factory is not _default_factory
    return (True, "") if custom else installed()


def _model_name() -> str:
    with _engine_lock:
        return _engine.model if _engine is not None else MODEL_NAME


# ---- the store ---------------------------------------------------------------

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class Store:
    """The speaker folder: one directory, mode 0700, files opened relative to its descriptor."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _root_fd(self, create: bool = False) -> int:
        """An open descriptor for the folder. FileNotFoundError if absent (and not `create`); StoreError if unsafe."""
        try:
            st = os.lstat(self.root)
        except FileNotFoundError:
            if not create:
                raise
            try:
                self.root.parent.mkdir(parents=True, exist_ok=True)
                os.mkdir(self.root, 0o700)
            except FileExistsError:
                pass
            except OSError:
                raise StoreError("the speaker folder cannot be created") from None
            st = os.lstat(self.root)
        except OSError:
            raise StoreError("the speaker folder cannot be read") from None
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise StoreError("the speaker folder is not a plain folder")
        if st.st_uid != os.geteuid():
            raise StoreError("the speaker folder is not yours")
        try:
            fd = os.open(self.root, _DIR_FLAGS)
        except OSError:
            raise StoreError("the speaker folder cannot be opened") from None
        try:
            fst = os.fstat(fd)
            if (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
                raise StoreError("the speaker folder changed while it was opened")
            if fst.st_mode & 0o077:
                os.fchmod(fd, 0o700)
        except BaseException:
            os.close(fd)
            raise
        return fd

    @staticmethod
    def _sub(rfd: int, sub: str | None, create: bool) -> int:
        """`sub` opened under `rfd` (or `rfd` itself, duplicated). A symlink or a file there is StoreError."""
        if sub is None:
            return os.dup(rfd)
        if create:
            try:
                os.mkdir(sub, 0o700, dir_fd=rfd)
            except FileExistsError:
                pass
            except OSError:
                raise StoreError("the speaker folder cannot be written") from None
        try:
            fd = os.open(sub, _DIR_FLAGS, dir_fd=rfd)
        except FileNotFoundError:
            raise
        except OSError:
            raise StoreError("the speaker folder holds something unexpected") from None
        try:
            if os.fstat(fd).st_mode & 0o077:
                os.fchmod(fd, 0o700)
        except OSError:
            os.close(fd)
            raise StoreError("the speaker folder cannot be written") from None
        return fd

    def probe(self) -> bool:
        """True if the folder exists and is safe to use, False if absent; StoreError if it is unsafe."""
        try:
            fd = self._root_fd()
        except FileNotFoundError:
            return False
        os.close(fd)
        return True

    def read(self, name: str, cap: int, sub: str | None = None) -> bytes:
        """The bytes of a small regular file. FileNotFoundError if absent; StoreError if it is anything else or too big."""
        rfd = self._root_fd()
        try:
            dfd = self._sub(rfd, sub, False)
            try:
                try:
                    fd = os.open(name, _FILE_FLAGS, dir_fd=dfd)
                except FileNotFoundError:
                    raise
                except OSError:
                    raise StoreError("a speaker file cannot be opened") from None   # a symlink (ELOOP), a permission
                try:
                    st = os.fstat(fd)
                    if not stat.S_ISREG(st.st_mode) or st.st_size > cap:
                        raise StoreError("a speaker file is not a small regular file")
                    chunks, size = [], 0
                    while True:
                        block = os.read(fd, min(1 << 16, cap + 1 - size))
                        if not block:
                            break
                        chunks.append(block)
                        size += len(block)
                        if size > cap:
                            raise StoreError("a speaker file is not a small regular file")
                    return b"".join(chunks)
                except OSError:
                    raise StoreError("a speaker file cannot be read") from None
                finally:
                    os.close(fd)
            finally:
                os.close(dfd)
        finally:
            os.close(rfd)

    def write(self, name: str, data: bytes, sub: str | None = None) -> None:
        """Atomically replace `name` (mode 0600). StoreError on any failure; nothing half-written is left."""
        rfd = self._root_fd(create=True)
        try:
            dfd = self._sub(rfd, sub, True)
            tmp = f".{name}.{secrets.token_hex(4)}.tmp"
            try:
                try:
                    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
                except OSError:
                    try:
                        os.unlink(tmp, dir_fd=dfd)
                    except OSError:
                        pass
                    raise StoreError("the speaker folder cannot be written") from None
                try:
                    os.fsync(dfd)
                except OSError:
                    pass
            finally:
                os.close(dfd)
        finally:
            os.close(rfd)

    def names(self, sub: str, pattern: re.Pattern, limit: int = 64) -> list[str]:
        """Regular files in `sub` whose names match, sorted. Missing folder is none; a non-folder is StoreError."""
        try:
            rfd = self._root_fd()
        except FileNotFoundError:
            return []
        try:
            try:
                dfd = self._sub(rfd, sub, False)
            except FileNotFoundError:
                return []
            try:
                found = []
                for name in sorted(os.listdir(dfd)):
                    if not pattern.fullmatch(name):
                        continue
                    try:
                        if stat.S_ISREG(os.stat(name, dir_fd=dfd, follow_symlinks=False).st_mode):
                            found.append(name)
                    except OSError:
                        continue
                return found[:limit]
            finally:
                os.close(dfd)
        finally:
            os.close(rfd)

    def remove(self, name: str, sub: str | None = None) -> None:
        try:
            rfd = self._root_fd()
        except FileNotFoundError:
            return
        try:
            try:
                dfd = self._sub(rfd, sub, False)
            except FileNotFoundError:
                return
            try:
                os.unlink(name, dir_fd=dfd)
            except FileNotFoundError:
                pass
            except OSError:
                raise StoreError("the speaker folder cannot be written") from None
            finally:
                os.close(dfd)
        finally:
            os.close(rfd)

    def delete_all(self) -> bool:
        """Remove the folder and everything in it (symlinks inside are removed, never followed). True if it existed."""
        try:
            st = os.lstat(self.root)
        except FileNotFoundError:
            return False
        except OSError:
            raise StoreError("the speaker folder cannot be read") from None
        try:
            if stat.S_ISDIR(st.st_mode):
                shutil.rmtree(self.root)
            else:
                os.unlink(self.root)         # a symlink or a stray file: the name goes, a target is never followed
        except OSError:
            raise StoreError("the speaker folder could not be removed") from None
        if os.path.lexists(self.root):
            raise StoreError("the speaker folder could not be removed")
        return True


def store() -> Store:
    return Store(Path(config.SPEAKER_DIR).expanduser())


def _json(data: bytes):
    """Parsed JSON, or None for anything unparseable (deep nesting, huge numbers, bad UTF-8)."""
    try:
        return json.loads(data.decode("utf-8"))
    except Exception:  # noqa: BLE001 - ValueError, UnicodeError, RecursionError, OverflowError ...
        return None


def _number(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except (OverflowError, ValueError):
        return None
    return f if math.isfinite(f) else None


@dataclass
class Voiceprint:
    model: str
    dim: int
    centroid: list[float]
    threshold: float
    readings: int
    windows: int
    created: str
    #: The highest score any built-in voice got against this voiceprint when it was accepted (L1):
    #: a short command's relaxed threshold never goes below it plus 0.02. None: not known.
    impostor_max: float | None = None


def _parse_voiceprint(raw) -> Voiceprint | None:
    if not isinstance(raw, dict) or raw.get("version") != 1:
        return None
    model, dim, cen = raw.get("model"), raw.get("dim"), raw.get("centroid")
    if not isinstance(model, str) or not 0 < len(model) <= 64 or isinstance(dim, bool) or not isinstance(dim, int):
        return None
    if not 2 <= dim <= _MAX_DIM or not isinstance(cen, list) or len(cen) != dim:
        return None
    values = [_number(v) for v in cen]
    if any(v is None for v in values) or math.sqrt(sum(v * v for v in values)) < 1e-9:
        return None
    t = _number(raw.get("threshold"))
    if t is None or not config.SPEAKER_THRESHOLD_MIN <= t <= config.SPEAKER_THRESHOLD_MAX:
        return None
    ints = []
    for key in ("readings", "windows"):
        v = raw.get(key)
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 10_000:
            return None
        ints.append(v)
    created = raw.get("created")
    imax = None
    if raw.get("impostor_max") is not None:
        imax = _number(raw.get("impostor_max"))
        if imax is None or not -1.0 <= imax <= 1.5:
            return None
    return Voiceprint(model, dim, values, t, ints[0], ints[1], created if isinstance(created, str) else "", imax)


@dataclass
class State:
    kind: str          # "none" | "enrolled" | "damaged" | "unsafe"
    voiceprint: Voiceprint | None
    guest: bool
    readings: int
    error: str = ""


def read_state() -> State:
    """What is stored, judged without trusting it. Never raises."""
    s = store()
    try:
        if not s.probe():
            return State("none", None, False, 0)
    except StoreError as exc:
        return State("unsafe", None, False, 0, str(exc))
    return _read_state(s)


def _read_state(s: Store) -> State:
    kind, vp, error = "none", None, ""
    try:
        data = s.read("voiceprint.json", _FILE_CAP)
    except FileNotFoundError:
        pass
    except StoreError as exc:
        kind, error = "damaged", str(exc)
    else:
        vp = _parse_voiceprint(_json(data))
        if vp is None:
            kind, error = "damaged", "the saved voiceprint is damaged"
        else:
            kind = "enrolled"
    guest = False
    try:
        raw = _json(s.read("guest.json", 4096))
        guest = isinstance(raw, dict) and raw.get("guest") is True
    except (FileNotFoundError, StoreError):
        guest = False                        # unreadable is "Guest off": verification stays on
    try:
        readings = len(s.names("readings", _READING_NAME))
    except StoreError:
        readings = 0
    return State(kind, vp, guest, readings, error)


# ---- verification ------------------------------------------------------------

_last: dict = {}
_last_lock = threading.Lock()


def _remember(result: str, score: float | None = None) -> None:
    with _last_lock:
        _last.clear()
        _last.update({"result": result, "score": None if score is None else round(float(score), 3), "at": int(time.time())})


def effective_threshold(vp: Voiceprint, seconds: float) -> float:
    """The threshold for a clip of `seconds`: a command (under `SPEAKER_SHORT_CLIP_S`) gets it relaxed by
    `SPEAKER_SHORT_RELAX`, but never below the closest built-in voice plus 0.02 (L1), and never above the full one."""
    if seconds >= config.SPEAKER_SHORT_CLIP_S:
        return vp.threshold
    relaxed = vp.threshold - config.SPEAKER_SHORT_RELAX
    if vp.impostor_max is not None:
        relaxed = max(relaxed, min(vp.threshold, vp.impostor_max + 0.02))
    return relaxed


def verify(audio: bytes, mime: str = "audio/wav") -> str:
    """Whose voice is `audio`? "matched", "skipped_guest", "skipped_unenrolled" or "no_speech" (too little speech to
    judge: the caller answers an empty text and transcribes nothing); NotOwner, VerifyUnavailable or VerifyUnsupported otherwise.

    The caller transcribes only when this returns. Order matters and is pinned by
    tests: the Guest switch first (skips verification and only that), then "is
    anyone enrolled", then everything that can fail closed.
    """
    state = read_state()
    if state.guest:
        _remember("skipped_guest")
        return "skipped_guest"
    if state.kind == "none":
        _remember("skipped_unenrolled")
        return "skipped_unenrolled"
    if state.kind != "enrolled" or state.voiceprint is None:
        raise VerifyUnavailable("the saved voiceprint cannot be used: delete it and enroll again, or turn on Guest voice")
    vp = state.voiceprint
    if vp.model != _model_name():
        raise VerifyUnavailable("the saved voiceprint was made by another speaker model: rebuild it in Settings, or turn on Guest voice")
    ok, why = available()
    if not ok:
        raise VerifyUnavailable(f"speaker verification is not set up: {why}")
    try:
        x, rate, seconds = decode_wav(audio, mime, max_seconds=VERIFY_READ_S)
    except ValueError as exc:
        raise VerifyUnsupported(str(exc)) from None
    window = voiced_window(x, rate)
    if window is None:
        # Too little speech to say whose it is (L2): not a refusal and not a transcription. The HUD
        # answers it like any silent clip ("didn't catch that"), and nothing is uploaded.
        _remember("no_speech")
        return "no_speech"
    emb = engine().embed(window[0], rate)
    if emb.size != vp.dim:
        raise VerifyUnavailable("the saved voiceprint does not fit the speaker model: rebuild it in Settings, or turn on Guest voice")
    np = _np()
    cen = np.asarray(vp.centroid, dtype=np.float64)
    score = float(emb @ cen / np.linalg.norm(cen))
    if score < effective_threshold(vp, seconds):
        _remember("rejected", score)
        raise NotOwner("not the enrolled speaker")
    _remember("matched", score)
    return "matched"


# ---- enrollment --------------------------------------------------------------

_mutex = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def status() -> dict:
    """What Settings shows. Counts, a threshold and the last score; never audio, never a vector. Never loads the model."""
    state = read_state()
    ok, why = available()
    vp = state.voiceprint
    stale = bool(vp and vp.model != _model_name())
    if state.kind == "enrolled" and vp is not None:
        kind = "stale" if stale else "enrolled"
    elif state.kind in ("damaged", "unsafe"):
        kind = state.kind
    else:
        kind = "enrolling" if state.readings else "not_enrolled"
    with _engine_lock:
        eng = _engine
    if state.guest:
        mode = "guest"
    elif state.kind == "none":
        mode = "off"
    else:                      # enrolled: on if it can verify; damaged, unsafe, stale or no engine: it cannot, so nothing is transcribed
        mode = "on" if kind == "enrolled" and ok else "blocked"
    with _last_lock:
        last = dict(_last)
    return {
        "state": kind,
        "enrolled": state.kind == "enrolled" and not stale,
        "readings": state.readings,
        "readings_min": READINGS_MIN,
        "readings_max": READINGS_MAX,
        "prompts": list(PROMPTS),
        "quality": dict(QUALITY),
        "guest": state.guest,
        # What /stt?verify=1 does right now: "off" (nobody enrolled), "guest", "on", or "blocked" (enrolled but it cannot verify, so nothing is transcribed).
        "mode": mode,
        "threshold": None if vp is None else round(vp.threshold, 3),
        "threshold_default": config.SPEAKER_THRESHOLD_DEFAULT,
        "threshold_min": config.SPEAKER_THRESHOLD_MIN,
        "proposal_floor": config.SPEAKER_PROPOSAL_FLOOR,
        "threshold_warning": ("" if vp is None or vp.threshold >= config.SPEAKER_PROPOSAL_FLOOR else
                              f"the threshold ({vp.threshold:.2f}) is below {config.SPEAKER_PROPOSAL_FLOOR:.2f}: "
                              "other voices may be accepted"),
        "threshold_max": config.SPEAKER_THRESHOLD_MAX,
        "short_clip_s": config.SPEAKER_SHORT_CLIP_S,
        "short_relax": config.SPEAKER_SHORT_RELAX,
        "voiceprint_readings": None if vp is None else vp.readings,
        "voiceprint_model": None if vp is None else vp.model,
        "created": None if vp is None else vp.created,
        "installed": ok,
        "reason": why,
        "loaded": bool(eng and eng.loaded),
        "threads": eng.threads if eng else config.SPEAKER_THREADS,
        "idle_unload_min": round((eng.idle_s if eng else config.SPEAKER_IDLE_MIN * 60) / 60, 2),
        "model": _model_name(),
        "fetch": "python -m jarvis.speaker fetch",
        "error": state.error,
        "last": last or None,
    }


def add_reading(audio: bytes, mime: str = "audio/wav") -> dict:
    """Judge and keep one prompted reading. {accepted, problems, metrics, readings}.

    A reading that fails the quality check is not stored. What is stored is our
    own re-encoding (16-bit mono PCM), never the uploaded bytes.
    """
    try:
        x, rate, seconds = decode_wav(audio, mime, max_seconds=MAX_READING_S + 1, max_bytes=MAX_READING_BYTES)
    except ValueError as exc:
        raise EnrollError(str(exc)) from None
    metrics, problems = quality(x, rate)
    with _mutex:
        s = store()
        s.probe()
        count = len(s.names("readings", _READING_NAME))
        if count >= READINGS_MAX:
            raise EnrollError(f"enough readings ({READINGS_MAX}): accept the voiceprint, or delete and start again")
        if problems:
            return {"accepted": False, "problems": problems, "metrics": metrics, "readings": count}
        taken = {int(_READING_NAME.fullmatch(n).group(1)) for n in s.names("readings", _READING_NAME)}
        number = next(i for i in range(1000) if i not in taken)
        s.write(f"r-{number:03d}.wav", encode_wav(x[: int(MAX_READING_S * rate)], rate), sub="readings")
        count = len(s.names("readings", _READING_NAME))
    return {"accepted": True, "problems": [], "metrics": metrics, "readings": count}


def _load_readings() -> list[list]:
    """For each stored reading, the embedding of each of its windows (needs the engine)."""
    s = store()
    out = []
    eng = engine()
    for name in s.names("readings", _READING_NAME):
        try:
            x, rate, _ = decode_wav(s.read(name, MAX_READING_BYTES, sub="readings"), "audio/wav",
                                    max_seconds=MAX_READING_S + 1, max_bytes=MAX_READING_BYTES)
        except (ValueError, FileNotFoundError, StoreError):
            continue                              # a damaged reading is left out, not fatal
        vectors = [eng.embed(w, rate) for w in reading_windows(x, rate)]
        if vectors:
            out.append(vectors)
    return out


def _centroid(vectors):
    np = _np()
    mean = np.mean(np.stack(vectors), axis=0)
    norm = float(np.linalg.norm(mean))
    if norm < 1e-9:
        raise EnrollError("the readings cancel out: record them again")
    return mean / norm


# The built-in voices, scored as free impostors against the owner's voiceprint.
def impostor_voices() -> list[str]:
    """Kokoro `a*`/`b*` voices and Pocket builtins (the pool read-aloud hands out), plus the current voice."""
    from . import pocket, voice

    names = [n for n in sorted(voice.available_voices()) if n[:1] in ("a", "b")]
    if pocket.available():
        names += [f"pocket:{n}" for n in pocket.BUILTINS]
    try:
        current = voice.selected_voice()
        if current and current not in names:
            names.append(current)
    except Exception:  # noqa: BLE001
        pass
    return names[:IMPOSTOR_MAX]


def synth_voice(name: str) -> bytes:
    """One local sentence as WAV bytes. Never the cloud: `voice._local_tts` and `pocket.synthesize` only."""
    from . import pocket, voice

    if name.startswith("pocket:"):
        return pocket.synthesize(IMPOSTOR_TEXT, name.removeprefix("pocket:"))
    return voice._local_tts(IMPOSTOR_TEXT, name, 1.0)


def impostor_vectors(budget_s: float = IMPOSTOR_BUDGET_S, names: list[str] | None = None) -> dict:
    """voice name -> unit embedding of that voice speaking one sentence, cached per model in the store.

    `names` limits it to those voices (L3: one newly used voice); default: every built-in voice read-aloud hands out.
    """
    s = store()
    np = _np()
    model = _model_name()
    cache: dict[str, list[float]] = {}
    try:
        raw = _json(s.read("impostors.json", _IMPOSTOR_CAP))
        if isinstance(raw, dict) and raw.get("version") == 1 and raw.get("model") == model and isinstance(raw.get("voices"), dict):
            for name, vec in raw["voices"].items():
                nums = [_number(v) for v in vec] if isinstance(vec, list) and 2 <= len(vec) <= _MAX_DIM else None
                if isinstance(name, str) and nums and all(v is not None for v in nums):
                    cache[name] = nums
    except (FileNotFoundError, StoreError):
        pass
    deadline = time.monotonic() + budget_s
    changed = False
    out = {}
    eng = engine()
    for name in (impostor_voices() if names is None else names):
        if name in cache:
            try:
                out[name] = _unit(cache[name], np)
                continue
            except VerifyUnavailable:
                del cache[name]               # a damaged cache entry is recomputed
        if time.monotonic() > deadline:
            break
        try:
            x, rate, _ = decode_wav(synth_voice(name), "audio/wav", max_seconds=12.0, max_bytes=MAX_VERIFY_WAV_BYTES)
            window = voiced_window(x, rate)
            if window is None:
                continue
            vec = eng.embed(window[0], rate)
        except VerifyUnavailable:
            raise
        except Exception:  # noqa: BLE001 - one voice that will not speak is skipped
            continue
        out[name] = vec
        cache[name] = [float(v) for v in vec]
        changed = True
    if changed:
        try:
            s.write("impostors.json", json.dumps({"version": 1, "model": model, "voices": cache}).encode())
        except StoreError:
            pass
    return out


#: How close to the owner's threshold a built-in voice may score before the owner is told (L3).
VOICE_WARN_MARGIN = 0.05


def voice_warning(name: str) -> str | None:
    """A sentence when Jarvis speaking as `name` could pass for the owner, else None (L3).

    Called when the speaking voice or a chat's voice (V7) changes. The voice is scored against the enrolled
    centroid (its one-sentence embedding is cached beside the voiceprint, so only the first use speaks anything).
    None also when nothing is enrolled, Guest voice is on, or the voice cannot be judged: a failure here must
    never get in the way of choosing a voice.
    """
    try:
        state = read_state()
        vp = state.voiceprint
        if state.kind != "enrolled" or state.guest or vp is None or vp.model != _model_name() or not available()[0]:
            return None
        got = impostor_vectors(budget_s=30.0, names=[name]).get(name)
        if got is None or got.size != vp.dim:
            return None
        np = _np()
        cen = np.asarray(vp.centroid, dtype=np.float64)
        score = float(got @ cen / np.linalg.norm(cen))
        # Judged at the short-clip threshold too: a command is the weakest check.
        limit = min(effective_threshold(vp, 0.0), vp.threshold) - VOICE_WARN_MARGIN
        if score >= limit:
            return (f"{name} sounds like you (it scores {score:.2f} against your voiceprint, and the check refuses below "
                    f"{vp.threshold:.2f}): Jarvis speaking as this voice may be taken for you. Choose another voice, or raise "
                    "the threshold in Settings > Voice.")
    except Exception:  # noqa: BLE001
        return None
    return None


BIN_LO, BIN_WIDTH, BINS = -0.2, 0.05, 24


def _hist(scores) -> list[int]:
    counts = [0] * BINS
    for v in scores:
        counts[min(BINS - 1, max(0, int((v - BIN_LO) / BIN_WIDTH)))] += 1
    return counts


def _pct(sorted_scores, q: float) -> float:
    if not sorted_scores:
        return 0.0
    i = q * (len(sorted_scores) - 1)
    lo, hi = int(math.floor(i)), int(math.ceil(i))
    return sorted_scores[lo] + (sorted_scores[hi] - sorted_scores[lo]) * (i - lo)


def choose_threshold(g10: float, top: float | None) -> dict:
    """The proposed threshold from the owner's weakest 10 % of window scores (`g10`) and the closest built-in
    voice (`top`, None when none could be scored). Pure, so the rules are pinned by a table of cases.

    - Separated (`g10 > top`): halfway between them, but never nearer the owner than 0.10 below `g10` (a TV, a
      cold or another room scores the owner lower than a quiet enrollment did), and never inside the impostors
      (`top + 0.02` at least).
    - Overlapping (`g10 <= top`): `top + 0.02`, which may refuse the owner's weakest readings.
    - Nothing scored: `min(SPEAKER_THRESHOLD_DEFAULT, g10 - 0.05)`.
    The result is clamped to [SPEAKER_PROPOSAL_FLOOR, SPEAKER_THRESHOLD_MAX]. `confirm` is True (the owner must
    "accept anyway", and is told to record the readings again) when the unclamped value fell below the floor, the
    owner's weakest readings score below `SPEAKER_GENUINE_FLOOR` against each other, or they overlap a built-in voice.
    """
    floor, ceiling = config.SPEAKER_PROPOSAL_FLOOR, config.SPEAKER_THRESHOLD_MAX
    warnings: list[str] = []
    overlap = top is not None and g10 <= top
    if top is not None:
        if g10 > top:
            raw = max(top + 0.02, min((g10 + top) / 2, g10 - 0.10))
        else:
            raw = top + 0.02
            warnings.append("your voice and one of Jarvis's built-in voices score alike")
    else:
        raw = min(config.SPEAKER_THRESHOLD_DEFAULT, g10 - 0.05)
        warnings.append("no built-in voice could be scored, so the threshold comes from your readings alone")
    if g10 < config.SPEAKER_GENUINE_FLOOR:
        warnings.append(f"your readings match each other weakly (their weakest scores are {g10:.2f}, below {config.SPEAKER_GENUINE_FLOOR:.2f})")
    if raw < floor:
        warnings.append(f"the threshold the readings support ({raw:.2f}) is below {floor:.2f}, so it was raised to {floor:.2f} and may refuse you")
    confirm = overlap or g10 < config.SPEAKER_GENUINE_FLOOR or raw < floor
    if confirm:
        warnings.append("record the readings again, closer to the microphone in a quiet room")
    return {"threshold": max(floor, min(ceiling, raw)), "raw": raw, "confirm": confirm, "overlap": overlap,
            "warning": "; ".join(warnings)}


def propose() -> dict:
    """Score the readings against each other and against every built-in voice, and propose a threshold.

    Nothing is stored. Genuine scores are leave-one-reading-out (each reading's
    windows against the voiceprint of the OTHER readings); impostor scores are
    each built-in voice against the voiceprint of all of them.
    """
    ok, why = available()
    if not ok:
        raise EnrollError(f"speaker verification is not set up: {why}")
    with _mutex:
        np = _np()
        per = _load_readings()
        if len(per) < READINGS_MIN:
            raise EnrollError(f"record {READINGS_MIN} readings first")
        genuine = []
        for i, vectors in enumerate(per):
            rest = [v for j, group in enumerate(per) if j != i for v in group]
            cen = _centroid(rest)
            genuine += [float(v @ cen) for v in vectors]
        everything = _centroid([v for group in per for v in group])
        imps = {name: float(v @ everything) for name, v in impostor_vectors().items()}
    g, imp = sorted(genuine), sorted(imps.values())
    g10 = _pct(g, 0.10)
    chosen = choose_threshold(g10, imp[-1] if imp else None)
    threshold, warning = chosen["threshold"], chosen["warning"]
    return {
        "threshold": round(threshold, 3),
        "bins": {"lo": BIN_LO, "width": BIN_WIDTH, "n": BINS},
        "genuine": {"n": len(g), "min": round(g[0], 3), "p10": round(g10, 3), "median": round(_pct(g, 0.5), 3),
                    "hist": _hist(g)},
        "impostors": {"n": len(imp), "max": round(imp[-1], 3) if imp else None,
                      "median": round(_pct(imp, 0.5), 3) if imp else None, "hist": _hist(imp),
                      "closest": [{"voice": n, "score": round(v, 3)} for n, v in sorted(imps.items(), key=lambda kv: -kv[1])[:3]]},
        "false_reject": round(sum(1 for v in g if v < threshold) / len(g), 3),
        "false_accept": round(sum(1 for v in imp if v >= threshold) / len(imp), 3) if imp else None,
        "overlap": chosen["overlap"],
        "confirm": chosen["confirm"],
        "warning": warning,
        "readings": len(per),
    }


def _check_threshold(value) -> float:
    t = _number(value)
    if t is None or not config.SPEAKER_THRESHOLD_MIN <= t <= config.SPEAKER_THRESHOLD_MAX:
        raise ValueError(f"threshold must be a number from {config.SPEAKER_THRESHOLD_MIN} to {config.SPEAKER_THRESHOLD_MAX}")
    return t


def _write_voiceprint(vp: Voiceprint) -> None:
    store().write("voiceprint.json", json.dumps({
        "version": 1, "model": vp.model, "dim": vp.dim, "centroid": vp.centroid, "threshold": vp.threshold,
        "readings": vp.readings, "windows": vp.windows, "created": vp.created, "impostor_max": vp.impostor_max,
    }).encode())


def accept(threshold=None, anyway: bool = False) -> dict:
    """Build the voiceprint from the stored readings and switch verification on.

    `threshold` defaults to `propose()`'s; one the owner gives may be as low as `SPEAKER_THRESHOLD_MIN` (the status
    then carries a warning). When the analysis says the readings do not separate the owner's voice clearly
    (`propose()["confirm"]`) this refuses with "record the readings again" unless `anyway` is True.
    """
    t = None if threshold is None else _check_threshold(threshold)
    ok, why = available()
    if not ok:
        raise EnrollError(f"speaker verification is not set up: {why}")
    with _mutex:
        per = _load_readings()
        if len(per) < READINGS_MIN:
            raise EnrollError(f"record {READINGS_MIN} readings first")
        proposal = propose()
        if proposal["confirm"] and anyway is not True:
            raise EnrollError(f"the readings do not tell your voice from the built-in voices clearly: {proposal['warning']}; "
                              "or accept anyway")
        if t is None:
            t = proposal["threshold"]
        vectors = [v for group in per for v in group]
        cen = _centroid(vectors)
        imax = proposal["impostors"]["max"]
        _write_voiceprint(Voiceprint(_model_name(), int(cen.size), [float(v) for v in cen], t, len(per), len(vectors), _now(),
                                     None if imax is None else float(imax)))
    return status()


def set_threshold(value) -> dict:
    t = _check_threshold(value)
    with _mutex:
        state = read_state()
        if state.kind != "enrolled" or state.voiceprint is None:
            raise EnrollError("there is no voiceprint to tune")
        vp = state.voiceprint
        vp.threshold = t
        _write_voiceprint(vp)
    return status()


def set_guest(flag: bool) -> dict:
    """Persist the Guest voice switch beside the voiceprint (until the owner turns it off)."""
    if not isinstance(flag, bool):
        raise ValueError("guest must be a boolean")
    with _mutex:
        store().write("guest.json", json.dumps({"guest": flag}).encode())
    return status()


def delete() -> dict:
    """Remove the whole store: readings, voiceprint, cache and the Guest switch (Q12: Delete removes all of it)."""
    with _mutex:
        store().delete_all()
    with _last_lock:
        _last.clear()
    return status()


# ---- download (explicit, owner-run) ------------------------------------------

def fetch(dest: Path | None = None, *, url: str = MODEL_URL, sha256: str = MODEL_SHA256) -> Path:
    """Download and verify the pinned model into `dest` (default `model_path()`).

    The file is hashed before it is moved into place, so a wrong or truncated
    download never becomes the model. Raises SystemExit with a sentence on failure.
    """
    import urllib.request

    dest = Path(dest) if dest else model_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".speaker-fetch-", dir=dest.parent))
    try:
        part = work / "model.onnx"
        print(f"downloading {url} ...", file=sys.stderr)
        req = urllib.request.Request(url, headers={"User-Agent": "jarvis-speaker/1"})
        with urllib.request.urlopen(req, timeout=120) as resp, part.open("wb") as out:
            total = 0
            while True:
                block = resp.read(1 << 20)
                if not block:
                    break
                total += len(block)
                if total > MAX_MODEL_BYTES:
                    raise SystemExit(f"the download is larger than {MAX_MODEL_BYTES >> 20} MB: not the model")
                out.write(block)
        digest = hashlib.sha256()
        with part.open("rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                digest.update(block)
        if digest.hexdigest() != sha256:
            raise SystemExit(f"sha256 mismatch: got {digest.hexdigest()}, pinned {sha256}")
        os.replace(part, dest)
        return dest
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str]) -> int:
    if argv[:1] == ["fetch"]:
        print(f"ok: {fetch()}")
        return 0
    if argv[:1] == ["status"]:
        for key, value in status().items():
            if key not in ("prompts", "quality"):
                print(f"{key}: {value}")
        return 0
    print("usage: python -m jarvis.speaker fetch | status", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
