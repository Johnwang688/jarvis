"""Synthetic voices and a fake embedder for the speaker checks (voice rework V12).

Nothing here is a recording of anyone: a "voice" is a stack of harmonics on a
fundamental, chopped into syllable-length bursts with pauses, so the energy
gates see speech and silence. `FakeBackend` embeds a window as the mean-removed
log energy in 24 log-spaced bands, which is enough that two clips of the same
fundamental score near 1 and clips of far fundamentals score well apart. It is
a stand-in for TitaNet in the free checks; the real model is
`tests/voice_speaker_real.py`.
"""
from __future__ import annotations

import io
import wave

import numpy as np

RATE = 16000
OWNER, FRIEND, STRANGER = 110.0, 190.0, 310.0


def voice(f0: float, seconds: float, *, rate: int = RATE, level: float = 0.2, seed: int = 0,
          pauses: bool = True, noise: float = 0.002) -> np.ndarray:
    """A float32 clip: harmonics of `f0` in 0.35 s bursts separated by 0.1 s of room tone."""
    rng = np.random.default_rng(seed)
    n = int(seconds * rate)
    t = np.arange(n) / rate
    jitter = 1.0 + 0.01 * rng.standard_normal()
    x = np.zeros(n)
    for k in range(1, 12):
        x += np.sin(2 * np.pi * f0 * jitter * k * t + rng.uniform(0, 6.28)) / k
    x *= level / max(1e-9, np.abs(x).max())
    if pauses:
        gate = ((t % 0.45) < 0.35).astype(float)
        x *= gate
    x += noise * rng.standard_normal(n)
    return x.astype(np.float32)


def wav(x: np.ndarray, rate: int = RATE) -> bytes:
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


def clip(f0: float, seconds: float, **kw) -> bytes:
    return wav(voice(f0, seconds, **kw))


class FakeBackend:
    """`embed(samples, rate) -> 24 numbers`; counts its calls."""
    calls = 0

    def __init__(self, threads: int = 1):
        self.threads = threads

    def embed(self, samples, rate):
        type(self).calls += 1
        spec = np.abs(np.fft.rfft(np.asarray(samples, dtype=np.float64) * np.hanning(len(samples))))
        freqs = np.fft.rfftfreq(len(samples), 1 / rate)
        edges = np.geomspace(70, 4000, 25)
        feats = np.array([spec[(freqs >= a) & (freqs < b)].sum() for a, b in zip(edges[:-1], edges[1:])])
        v = np.log1p(feats)
        return (v - v.mean()).tolist()

    def close(self):
        pass


def fake_engine(**kw):
    from jarvis import speaker

    FakeBackend.calls = 0
    return speaker.Engine(lambda threads: FakeBackend(threads), schedule=False, **kw)
