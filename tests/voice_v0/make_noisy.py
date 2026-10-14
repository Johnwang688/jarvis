"""Synthetic degradation conditions shared by the STT and browser probes.

Everything here is made from synthetic Kokoro audio and generated noise. The
"tv" condition is two other Kokoro voices speaking at once, a stand-in for a
television: it is NOT a recording of one.
"""

from __future__ import annotations

import common  # noqa: F401,I001  (first: pins BLAS threads before numpy loads)
import numpy as np



def _rng(seed: int) -> np.random.Generator:
    return np.random.default_rng(seed)


def pink(n: int, seed: int = 1) -> np.ndarray:
    """1/f noise (fan / room-tone stand-in), unit RMS."""
    white = _rng(seed).standard_normal(n)
    f = np.fft.rfft(white)
    f /= np.sqrt(np.maximum(np.arange(len(f)), 1))
    x = np.fft.irfft(f, n)
    return (x / np.sqrt(np.mean(x**2))).astype(np.float32)


def white(n: int, seed: int = 2) -> np.ndarray:
    x = _rng(seed).standard_normal(n).astype(np.float32)
    return x / np.sqrt(np.mean(x**2))


def babble(picked, audio, i: int, n: int, voices: int = 2) -> np.ndarray:
    """Competing speech: `voices` other clips (different text) laid end to end."""
    out = np.zeros(n, np.float32)
    for k in range(voices):
        j = (i + 7 + 5 * k) % len(picked)
        pos = 0
        while pos < n:
            x = audio[picked[j]["file"]]
            seg = x[: n - pos]
            out[pos: pos + len(seg)] += seg
            pos += len(x) + int(0.2 * common.SR)
            j = (j + 3) % len(picked)
    return out


def conditions():
    """name -> fn(clean, index, picked, audio) producing the degraded clip."""

    def mk(kind: str, snr: float):
        def fn(x, i, picked, audio):
            if kind == "pink":
                nz = pink(len(x), seed=i + 10)
            elif kind == "white":
                nz = white(len(x), seed=i + 20)
            else:
                nz = babble(picked, audio, i, len(x))
            return common.mix_at_snr(x, nz, snr)
        return fn

    conds = {"clean": lambda x, i, p, a: x}
    for kind, snrs in (("pink", (10, 0)), ("white", (10,)), ("tv", (10, 5))):
        for s in snrs:
            conds[f"{kind}_{s}dB"] = mk(kind, s)
    return conds


if __name__ == "__main__":
    # A library for the probes; run directly it only reports the gate and lists the conditions.
    if common.gated("list the synthetic degradation conditions"):
        print("\n".join(conditions()))
