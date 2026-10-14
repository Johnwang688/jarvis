"""Owner-run measurements that need a real microphone (V0 manual gate).

Everything here runs LOCALLY, keeps audio only in memory, writes nothing to disk
and makes no network call. Run it yourself, at the PC, in the room and seat you
really use. It reads the mic through PulseAudio (`parec`, WSLg's RDPSource by
default), so it measures the WSL/Linux path, not Windows Chrome's; for the
browser's own echo cancellation use aec_probe.html instead.

    python tests/voice_v0/manual/owner_manual.py kws     [--seconds 30] [--label "1 m"]
    python tests/voice_v0/manual/owner_manual.py vad     [--minutes 5]
    python tests/voice_v0/manual/owner_manual.py wer     [--n 20]
    python tests/voice_v0/manual/owner_manual.py speaker [--tv-minutes 3]

Add `--source stdin` to read raw 16 kHz mono s16le PCM from stdin instead (used to
smoke-test these scripts with synthetic audio). `--device NAME` picks a PulseAudio
source (`pactl list short sources`).

kws      Say the keywords while it listens; prints a hit tally. Run it once at 1 m
         and once at 3 m (`--label`), and once with the TV on and you silent (false
         accepts per hour = hits / minutes * 60).
vad      You stay silent with the TV or video playing; reports the share of frames
         Silero calls speech. This is the "false accepts with the TV on" number.
wer      Reads you 20 dictation sentences, transcribes each with local Parakeet and
         reports WER against the prompt. Cloud WER needs paid calls and your say-so,
         so it is not here; compare yourself with the HUD's STT on the same sentences.
speaker  Enrol from 3 readings, score 20 of your own 3 s clips against it, then
         score windows of the TV (you silent) and report owner-vs-TV EER and the
         false-accept rate at 1% / 5% false-reject.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import common  # noqa: E402,F401,I001
import numpy as np  # noqa: E402

import models  # noqa: E402
import probe_kws_native as kn  # noqa: E402
import probe_speaker as ps  # noqa: E402
import probe_stt as pstt  # noqa: E402
from make_fixtures import SENTENCES  # noqa: E402

SR = common.SR
FRAME = 512


class Mic:
    def __init__(self, source: str = "pulse", device: str | None = None):
        self.source = source
        if source == "stdin":
            self.fh = sys.stdin.buffer
            self.proc = None
        else:
            cmd = ["parec", "--raw", "--format=s16le", f"--rate={SR}", "--channels=1", "--latency-msec=40"]
            if device:
                cmd += ["-d", device]
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.fh = self.proc.stdout

    def read(self, n: int) -> np.ndarray | None:
        raw = self.fh.read(n * 2)
        if not raw or len(raw) < 2:
            return None
        return np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0

    def seconds(self, s: float) -> np.ndarray:
        out, need = [], int(s * SR)
        while need > 0:
            x = self.read(min(need, FRAME * 4))
            if x is None:
                break
            out.append(x)
            need -= len(x)
        return np.concatenate(out) if out else np.zeros(0, np.float32)

    def utterance(self, max_s: float = 20.0, hang_s: float = 1.2, thr_db: float = -42.0) -> np.ndarray:
        """Record from first speech until `hang_s` of quiet (energy gate; fine for a prompted reading)."""
        buf, started, quiet = [], False, 0.0
        t = 0.0
        while t < max_s:
            x = self.read(FRAME)
            if x is None:
                break
            t += len(x) / SR
            loud = common.rms_db(x) > thr_db
            if loud:
                started, quiet = True, 0.0
            elif started:
                quiet += len(x) / SR
            if started:
                buf.append(x)
            if started and quiet >= hang_s:
                break
        return np.concatenate(buf) if buf else np.zeros(0, np.float32)

    def close(self):
        if self.proc:
            self.proc.terminate()


def silero_session():
    import onnxruntime as ort

    return ort.InferenceSession(str(models.model_file("silero-vad-v5.1.2")), providers=["CPUExecutionProvider"])


def silero_probs(sess, x: np.ndarray) -> np.ndarray:
    state = np.zeros((2, 1, 128), np.float32)
    ctx = np.zeros(64, np.float32)
    out = []
    for i in range(len(x) // FRAME):
        fr = x[i * FRAME:(i + 1) * FRAME]
        inp = np.concatenate([ctx, fr])[None].astype(np.float32)
        p, state = sess.run(None, {"input": inp, "state": state, "sr": np.array(SR, np.int64)})
        ctx = fr[-64:]
        out.append(float(p[0][0]))
    return np.array(out)


def cmd_kws(mic: Mic, a: dict) -> None:
    import tempfile as tf

    secs = float(a.get("--seconds", 30))
    label = a.get("--label", "")
    with tf.TemporaryDirectory() as tmp:
        sp = kn.spotter(False, 1, 1.0, 0.25, tmp)
        print(f"Listening for {secs:.0f} s {label}. Say: {', '.join(w.lower() for w in kn.KEYWORDS)} ...")
        x = mic.seconds(secs)
        hits = kn.run_clip(sp, x, tail_s=0.5)
    tally: dict[str, int] = {}
    for w, t in hits:
        tally[w] = tally.get(w, 0) + 1
        print(f"  {t:6.1f}s  {w.lower()}")
    mins = len(x) / SR / 60
    print(f"\n{label or 'run'}: {len(hits)} hits in {mins:.2f} min -> {len(hits) / max(mins, 1e-9) * 60:.1f} per hour")
    for w in kn.KEYWORDS:
        print(f"  {w.lower():8s} {tally.get(w, 0)}")


def cmd_vad(mic: Mic, a: dict) -> None:
    minutes = float(a.get("--minutes", 5))
    print(f"Stay silent with the TV/video on for {minutes:g} min ...")
    x = mic.seconds(minutes * 60)
    p = silero_probs(silero_session(), x)
    thr = 0.5
    runs = int(np.sum((p[1:] > thr) & (p[:-1] <= thr)))
    print(f"frames above {thr}: {100 * np.mean(p > thr):.1f}%  | speech onsets: {runs} "
          f"({runs / max(len(x) / SR / 60, 1e-9):.1f} per minute) | mic level {common.rms_db(x):.1f} dBFS")


def cmd_wer(mic: Mic, a: dict) -> None:
    n = int(a.get("--n", 20))
    rec, _ = pstt.load(4)
    refs, hyps = [], []
    for i in range(n):
        ref = SENTENCES[i % len(SENTENCES)]
        print(f"\n[{i + 1}/{n}] Read aloud:  {ref}")
        x = mic.utterance()
        if len(x) < SR // 2:
            print("  (nothing heard, skipped)")
            continue
        hyp = pstt.decode(rec, x)[0]
        print(f"  heard:  {hyp}")
        refs.append(ref)
        hyps.append(hyp)
    r = common.wer(refs, hyps)
    print(f"\nlocal Parakeet WER on {len(refs)} sentences: {100 * r['wer']:.1f}%  (sub {r['sub']}, del {r['del']}, ins {r['ins']} of {r['ref_words']} words)")
    print("Audio was never written to disk and has been dropped.")


def cmd_speaker(mic: Mic, a: dict) -> None:
    name = a.get("--model", "nemo_titanet_small")
    ex = ps.extractor(name, 2)
    emb = lambda x: ps.embed(ex, x)  # noqa: E731
    cen = []
    for i in range(3):
        print(f"\nEnrol {i + 1}/3: read ~10 s of anything, in your normal voice.")
        cen.append(emb(mic.utterance(max_s=20, hang_s=1.5)))
    c = np.mean(cen, axis=0)
    c /= np.linalg.norm(c)
    own = []
    for i in range(20):
        print(f"[{i + 1}/20] Say a short sentence (any).")
        x = mic.utterance(max_s=8)[: 3 * SR]
        if len(x) >= SR:
            own.append(float(emb(x) @ c))
    mins = float(a.get("--tv-minutes", 3))
    print(f"\nNow stay silent and play the TV/video for {mins:g} min ...")
    x = mic.seconds(mins * 60)
    tv = []
    for i in range(0, len(x) - 3 * SR, 3 * SR):
        w = x[i:i + 3 * SR]
        if common.rms_db(w) > -50:
            tv.append(float(emb(w) @ c))
    if not own or not tv:
        print("not enough audio of one kind to score")
        return
    own, tv = np.array(own), np.array(tv)
    e, thr = common.eer(own, tv)
    print(f"\nmodel {name}: owner clips {len(own)}, TV windows {len(tv)}")
    print(f"owner score p50 {np.median(own):.3f}, TV score p95 {np.percentile(tv, 95):.3f}, TV max {tv.max():.3f}")
    print(f"owner-vs-TV EER {100 * e:.1f}% at threshold {thr:.3f}")
    for frr in (0.01, 0.05):
        t = float(np.quantile(own, frr))
        print(f"  at {frr:.0%} false-reject (threshold {t:.3f}): TV false-accept {100 * np.mean(tv >= t):.1f}%")
    print("Embeddings and audio stayed in memory and are dropped now.")


def main(argv: list[str]) -> int:
    if os.environ.get("JARVIS_VOICE_V0") != "1":
        print("skipped: set JARVIS_VOICE_V0=1 (owner-run, real microphone; see the module docstring)")
        return 0
    a = common.parse_args(argv[1:])
    if not argv or argv[0] not in ("kws", "vad", "wer", "speaker"):
        print(__doc__)
        return 2
    mic = Mic(str(a.get("--source", "pulse")), a.get("--device"))
    try:
        {"kws": cmd_kws, "vad": cmd_vad, "wer": cmd_wer, "speaker": cmd_speaker}[argv[0]](mic, a)
    finally:
        mic.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
