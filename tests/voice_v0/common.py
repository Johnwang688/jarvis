"""Shared helpers for the V0 probes: audio IO, WER, EER, timing, memory.

Pure numpy/stdlib on purpose so `tests/voice_v0_helpers_check.py` can exercise
the scoring maths without any model download.
"""

from __future__ import annotations

import json
import os

# BLAS threads would hide behind process-CPU numbers; probes pin them before numpy loads.
for _v in ("OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")
import re
import resource
import time
import wave
from pathlib import Path

import numpy as np

SR = 16000
GATE = "JARVIS_VOICE_V0"
WORK = Path(os.environ.get("JARVIS_VOICE_V0_DIR", Path.home() / ".cache" / "jarvis-voice-v0"))
FIXTURES = WORK / "fixtures"


# ---- audio ------------------------------------------------------------------

def read_wav(path: str | Path) -> np.ndarray:
    """16-bit PCM mono WAV -> float32 [-1, 1]. Requires 16 kHz (fixtures are)."""
    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2, "mono 16-bit only"
        assert w.getframerate() == SR, f"expected {SR} Hz, got {w.getframerate()}"
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def write_wav(path: str | Path, x: np.ndarray, sr: int = SR) -> None:
    pcm = (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def resample_to_16k(x: np.ndarray, sr: int) -> np.ndarray:
    if sr == SR:
        return x.astype(np.float32)
    from math import gcd

    from scipy.signal import resample_poly

    g = gcd(SR, sr)
    return resample_poly(x, SR // g, sr // g).astype(np.float32)


def rms_db(x: np.ndarray) -> float:
    return 20 * np.log10(float(np.sqrt(np.mean(np.square(x))) + 1e-9))


def mix_at_snr(speech: np.ndarray, noise: np.ndarray, snr_db: float) -> np.ndarray:
    """Add `noise` (looped/cut to length) to `speech` at the given speech-to-noise ratio."""
    reps = int(np.ceil(len(speech) / len(noise)))
    n = np.tile(noise, reps)[: len(speech)]
    gain = 10 ** ((rms_db(speech) - snr_db - rms_db(n)) / 20)
    return (speech + gain * n).astype(np.float32)


def load_manifest() -> list[dict]:
    return json.loads((FIXTURES / "manifest.json").read_text())


# ---- scoring ----------------------------------------------------------------

def norm_words(text: str) -> list[str]:
    text = text.lower().replace("-", " ")
    return re.sub(r"[^a-z0-9' ]", " ", text).split()


def edit_counts(ref: list[str], hyp: list[str]) -> tuple[int, int, int, int]:
    """(substitutions, deletions, insertions, ref_len) from a word-level Levenshtein."""
    n, m = len(ref), len(hyp)
    d = np.zeros((n + 1, m + 1), dtype=np.int32)
    d[:, 0] = np.arange(n + 1)
    d[0, :] = np.arange(m + 1)
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            d[i, j] = min(d[i - 1, j] + 1, d[i, j - 1] + 1, d[i - 1, j - 1] + (ref[i - 1] != hyp[j - 1]))
    i, j, s, dl, ins = n, m, 0, 0, 0
    while i > 0 or j > 0:
        if i > 0 and j > 0 and d[i, j] == d[i - 1, j - 1] + (ref[i - 1] != hyp[j - 1]):
            s += int(ref[i - 1] != hyp[j - 1])
            i, j = i - 1, j - 1
        elif i > 0 and d[i, j] == d[i - 1, j] + 1:
            dl += 1
            i -= 1
        else:
            ins += 1
            j -= 1
    return s, dl, ins, n


def wer(refs: list[str], hyps: list[str]) -> dict:
    """Corpus WER (sum of edits / sum of reference words) after normalisation."""
    s = d = i = n = 0
    for r, h in zip(refs, hyps, strict=True):
        a, b, c, m = edit_counts(norm_words(r), norm_words(h))
        s, d, i, n = s + a, d + b, i + c, n + m
    return {"wer": (s + d + i) / max(n, 1), "sub": s, "del": d, "ins": i, "ref_words": n}


def eer(genuine: np.ndarray, impostor: np.ndarray) -> tuple[float, float]:
    """Equal error rate and its threshold for similarity scores (higher = same)."""
    scores = np.concatenate([genuine, impostor])
    labels = np.concatenate([np.ones(len(genuine)), np.zeros(len(impostor))])
    order = np.argsort(-scores, kind="stable")
    labels = labels[order]
    tp = np.cumsum(labels)
    fp = np.cumsum(1 - labels)
    frr = 1 - tp / len(genuine)
    far = fp / len(impostor)
    k = int(np.argmin(np.abs(frr - far)))
    return float((frr[k] + far[k]) / 2), float(scores[order][k])


def pct(values: list[float], q: float) -> float:
    return float(np.percentile(np.asarray(values, dtype=np.float64), q))


def summarize(values: list[float]) -> dict:
    return {"n": len(values), "p50": pct(values, 50), "p95": pct(values, 95), "mean": float(np.mean(values)),
            "max": float(np.max(values))}


# ---- process ----------------------------------------------------------------

def peak_rss_mb() -> float:
    """Peak resident set of this process (Linux ru_maxrss is KiB)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def cur_rss_mb() -> float:
    with open("/proc/self/statm") as fh:
        return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6


class Cpu:
    """Wall + process-CPU stopwatch (user+sys of this process, all threads)."""

    def __enter__(self):
        self.w0, self.c0 = time.perf_counter(), time.process_time()
        return self

    def __exit__(self, *exc):
        self.wall = time.perf_counter() - self.w0
        self.cpu = time.process_time() - self.c0


def emit(name: str, payload: dict) -> None:
    """Print a machine-readable result line and drop it in WORK/results/."""
    out = WORK / "results"
    out.mkdir(parents=True, exist_ok=True)
    payload = {**payload, "_env": {"loadavg_at_finish": os.getloadavg(), "cpus": os.cpu_count(),
                                   "date": time.strftime("%Y-%m-%d %H:%M:%S")}}
    (out / f"{name}.json").write_text(json.dumps(payload, indent=2, default=float))
    print(json.dumps(payload, indent=2, default=float))


def parse_args(argv: list[str]) -> dict:
    """Tiny flag parser: `--k=v`, `--k v` and bare `--flag` (value True)."""
    out: dict = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            if "=" in a:
                k, v = a.split("=", 1)
                out[k] = v
            elif i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                out[a] = argv[i + 1]
                i += 1
            else:
                out[a] = True
        i += 1
    return out


def gated(what: str) -> bool:
    """True when JARVIS_VOICE_V0=1. Otherwise print a one-line skip so the caller can exit 0.

    Every script in tests/voice_v0/ that downloads, generates, serves or measures calls this first.
    """
    if os.environ.get(GATE) == "1":
        return True
    print(f"skipped: set {GATE}=1 to {what}")
    return False


def kokoro_paths() -> tuple[Path, Path]:
    """Kokoro model and voices, resolved here and NOT through jarvis.config.

    Importing jarvis.config runs its _load_dotenv(), which reads the credential .env; a measurement
    probe has no business doing that. The defaults and override variables match config.py.
    """
    home = Path.home() / ".cache" / "jarvis-tts"
    return (Path(os.environ.get("JARVIS_KOKORO_MODEL", home / "kokoro-v1.0.onnx")),
            Path(os.environ.get("JARVIS_KOKORO_VOICES", home / "voices-v1.0.bin")))
