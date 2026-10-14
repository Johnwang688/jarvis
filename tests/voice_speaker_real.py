"""The real TitaNet-small through `jarvis.speaker` (voice rework V12), gated.

Not part of the free sweep. It needs V0's probe venv (sherpa-onnx), the pinned
model and V0's synthetic fixtures, so it exits 0 without doing anything unless
`JARVIS_VOICE_V12_REAL=1`:

    JARVIS_VOICE_V12_REAL=1 PYTHONPATH=. ~/.cache/jarvis-voice-v0/venv/bin/python tests/voice_speaker_real.py

Fixtures are SYNTHETIC Kokoro and Pocket voices (`~/.cache/jarvis-voice-v0/fixtures`,
made by `tests/voice_v0/make_fixtures.py`), never a real person's voice. The
"owner" is one synthetic voice: three prompted "readings" are made of its clips
joined, the other voices stand in for the built-in voices and for other people.

This exercises the real path end to end -- decode, voiced window, the real
embedder, enrollment, the proposed threshold, accept, verify -- and prints the
numbers the threshold is chosen from. As V0 says, a synthetic voice is easier
than a person (two clips differ only in text and speed) and the impostors are
TTS voices, not a TV or a family member: this is NOT an owner-vs-TV false-accept
rate. The owner-at-the-PC run is the manual gate in docs/notes/voice-face.md.
"""
from __future__ import annotations

import hashlib
import os
import resource
import statistics
import sys
import tempfile
import time
import wave
from pathlib import Path

if os.environ.get("JARVIS_VOICE_V12_REAL") != "1":
    print("skipped: set JARVIS_VOICE_V12_REAL=1 (needs the V0 venv, the pinned model and V0's fixtures)")
    sys.exit(0)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from jarvis import config, speaker  # noqa: E402

FIXTURES = Path(os.environ.get("JARVIS_VOICE_V0_FIXTURES", Path.home() / ".cache" / "jarvis-voice-v0" / "fixtures"))
MODEL = Path(os.environ.get("JARVIS_VOICE_MODELS", Path.home() / ".cache" / "jarvis-voice-models")) / "spk-nemo-titanet-small" / "nemo_en_titanet_small.onnx"
OWNER = "kokoro_af_bella"
JARVIS = "kokoro_bm_george"          # the default Jarvis voice: V0 saw it score closest to an enrolled voice

FAILURES: list[str] = []
CHECKS = 0


def ok(condition, message):
    global CHECKS
    CHECKS += 1
    print(("ok   " if condition else "FAIL ") + message)
    if not condition:
        FAILURES.append(message)


def read(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    return pcm.astype(np.float32) / 32768.0


def prep(x: np.ndarray, seed: int, level_db: float = -20.0) -> np.ndarray:
    """Trim Kokoro's digital zeros, lay a -60 dBFS noise floor, scale to a healthy speech level (as V0 did)."""
    idx = np.where(np.abs(x) > 0.003)[0]
    x = x[idx[0]: idx[-1] + 1] if len(idx) else x
    rms = float(np.sqrt((x ** 2).mean())) or 1.0
    x = x * (10 ** (level_db / 20) / rms)
    x = np.clip(x, -0.98, 0.98)
    rng = np.random.default_rng(seed)
    return (x + rng.standard_normal(len(x)).astype(np.float32) * 10 ** (-60 / 20)).astype(np.float32)


def wav(x: np.ndarray) -> bytes:
    return speaker.encode_wav(x, 16000)


def clips(voice: str) -> list[Path]:
    return sorted(FIXTURES.glob(f"{voice}_[0-9]*.wav"))


def main() -> int:
    for need in (MODEL, FIXTURES):
        if not need.exists():
            print(f"missing {need}")
            return 2
    ok(hashlib.sha256(MODEL.read_bytes()).hexdigest() == speaker.MODEL_SHA256, "the model file is the pinned one (sha256)")
    tmp = Path(tempfile.mkdtemp())
    config.SPEAKER_DIR = tmp / "speaker"
    config.SPEAKER_MODEL_PATH = MODEL
    installed, why = speaker.installed()
    ok(installed, f"the real engine is installed ({why or 'sherpa-onnx and the model'})")
    if not installed:
        return 1
    eng = speaker.Engine(schedule=False)
    speaker.set_engine(eng)
    ok(not eng.loaded and speaker.status()["loaded"] is False, "nothing is loaded until the first use")

    owner = clips(OWNER)
    ok(len(owner) >= 20, f"{len(owner)} owner fixtures")
    # Three readings of three joined clips each (about 12 s), the rest held out.
    readings = [owner[i * 3: i * 3 + 3] for i in range(3)]
    held_out = owner[9:]
    for n, group in enumerate(readings):
        parts = []
        for k, path in enumerate(group):
            parts += [prep(read(path), 100 * n + k), np.zeros(int(0.25 * 16000), dtype=np.float32)]
        got = speaker.add_reading(wav(np.concatenate(parts)))
        ok(got["accepted"], f"reading {n + 1} passes the quality check ({got['metrics']})")

    others = sorted({p.name.rsplit("_", 1)[0] for p in FIXTURES.glob("*_[0-9]*.wav")} - {OWNER})
    stand_in = {name: clips(name)[0] for name in others if clips(name)}
    speaker.impostor_voices = lambda: list(stand_in)                       # the built-in voices, from fixtures
    speaker.synth_voice = lambda name: wav(prep(read(stand_in[name]), 7))  # (no TTS runs here)

    t0 = time.monotonic()
    proposal = speaker.propose()
    t_propose = time.monotonic() - t0
    g, i = proposal["genuine"], proposal["impostors"]
    print(f"proposal: threshold {proposal['threshold']}  genuine n={g['n']} min={g['min']} p10={g['p10']} median={g['median']}"
          f"  impostors n={i['n']} max={i['max']} median={i['median']} closest={i['closest']}  "
          f"({t_propose:.1f} s for {i['n']} built-in voices)")
    ok(g["n"] >= 6 and i["n"] >= 30, f"{g['n']} genuine windows scored against {i['n']} built-in voices")
    ok(g["p10"] > i["max"], f"the owner's weakest readings ({g['p10']}) score above every built-in voice ({i['max']})")
    ok(not proposal["overlap"] and proposal["false_reject"] == 0 and proposal["false_accept"] == 0, "no overlap at the proposed threshold")
    ok(i["max"] < proposal["threshold"] < g["p10"], f"the threshold ({proposal['threshold']}) sits between them")

    st = speaker.accept(proposal["threshold"])
    ok(st["mode"] == "on" and st["enrolled"], "accept turns verification on")
    threshold = st["threshold"]

    # Held-out owner clips (3 s windows, as /stt would embed) are accepted.
    times, scores, accepted = [], [], 0
    for k, path in enumerate(held_out):
        audio = wav(prep(read(path), 500 + k))
        t = time.monotonic()
        try:
            speaker.verify(audio)
            accepted += 1
        except speaker.NotOwner:
            pass
        times.append((time.monotonic() - t) * 1000)
        scores.append(speaker.status()["last"]["score"])
    print(f"held-out owner: accepted {accepted}/{len(held_out)}  scores min={min(scores)} median={statistics.median(scores)}  "
          f"verify ms p50={statistics.median(times):.0f} max={max(times):.0f}")
    ok(accepted >= int(0.95 * len(held_out)), f"{accepted}/{len(held_out)} held-out owner clips accepted")

    # Every other voice is refused, before any cloud call would be made.
    refused = total = 0
    worst = (0.0, "")
    for name in others:
        for k, path in enumerate(clips(name)[:4]):
            total += 1
            try:
                speaker.verify(wav(prep(read(path), 900 + k)))
            except speaker.NotOwner:
                refused += 1
            else:
                pass
            s = speaker.status()["last"]["score"]
            if s is not None and s > worst[0]:
                worst = (s, name)
    print(f"other voices: refused {refused}/{total}  highest score {worst[0]} ({worst[1]}) against threshold {threshold}")
    ok(refused >= int(0.99 * total), f"{refused}/{total} clips of {len(others)} other voices refused")

    # Jarvis's own default voice.
    own = clips(JARVIS)
    if own:
        verdicts = []
        for k, path in enumerate(own[:8]):
            try:
                speaker.verify(wav(prep(read(path), 1200 + k)))
                verdicts.append(True)
            except speaker.NotOwner:
                verdicts.append(False)
        ok(not any(verdicts), f"Jarvis's own voice ({JARVIS}) is refused ({sum(verdicts)}/{len(verdicts)} accepted)")

    # A one-second command gets the relaxed threshold, and is still not another voice's.
    short = 16000
    hits = 0
    for k, path in enumerate(held_out[:8]):
        x = prep(read(path), 700 + k)
        try:
            speaker.verify(wav(x[:short]))
            hits += 1
        except speaker.NotOwner:
            pass
    print(f"1 s owner commands accepted: {hits}/8 (threshold {threshold} less {config.SPEAKER_SHORT_RELAX})")
    ok(hits >= 5, f"most of the owner's 1 s commands are accepted ({hits}/8)")

    ok(eng.loads == 1, "the model loaded once for all of it")
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(f"peak RSS {rss:.0f} MB; engine threads {eng.threads}")
    ok(eng.unload() and not eng.loaded, "the model unloads on request")
    speaker.delete()
    ok(not os.path.lexists(config.SPEAKER_DIR), "Delete removes the store")
    print(f"\n{CHECKS} checks, {len(FAILURES)} failed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
