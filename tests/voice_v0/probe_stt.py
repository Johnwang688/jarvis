"""Local STT probe: Parakeet-TDT-0.6B-v3 int8 through sherpa-onnx on CPU.

    JARVIS_VOICE_V0=1 python tests/voice_v0/probe_stt.py [--threads 1,4,8,24] [--clips N]

Measures, per ONNX Runtime thread count (each in its own subprocess so peak RSS
is clean): model load time, real-time factor (RTF = decode wall / audio
seconds) and CPU-seconds-per-audio-second, over short clips (one Kokoro
sentence, ~5 s) and over concatenated 30 s and 60 s dictation, plus word error
rate against the known source text. WER here is on SYNTHETIC Kokoro speech
(clean, and mixed with synthetic noise / competing Kokoro voices when
`--noisy` is given); it says nothing about the owner's voice or cloud-vs-local.

No cloud STT is called. A cloud/local WER comparison needs paid calls and the
owner's authorisation, so it is deliberately not here.

Also: `--contention` runs a Kokoro synthesis loop in the same process while
Parakeet decodes, to see what a shared CPU does to Kokoro's time to first
audio (the daemon would host both; voice.py serialises Kokoro on one lock).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import numpy as np

import common
import models

PER_COUNT_DEFAULT = "1,4,8,24"


def load(num_threads: int):
    import sherpa_onnx

    d = models.extracted("parakeet-tdt-0.6b-v3-int8")
    t0 = time.perf_counter()
    rec = sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=str(d / "encoder.int8.onnx"), decoder=str(d / "decoder.int8.onnx"),
        joiner=str(d / "joiner.int8.onnx"), tokens=str(d / "tokens.txt"),
        num_threads=num_threads, model_type="nemo_transducer", decoding_method="greedy_search",
    )
    return rec, time.perf_counter() - t0


def decode(rec, x: np.ndarray) -> tuple[str, float, float]:
    """(text, wall_s, cpu_s) for one clip."""
    with common.Cpu() as c:
        s = rec.create_stream()
        s.accept_waveform(common.SR, x)
        rec.decode_stream(s)
        text = s.result.text
    return text, c.wall, c.cpu


def concat(clips: list[np.ndarray], seconds: float) -> np.ndarray:
    gap = np.zeros(int(0.35 * common.SR), np.float32)
    out, n = [], 0
    for x in clips:
        out += [x, gap]
        n += len(x) + len(gap)
        if n >= seconds * common.SR:
            break
    return np.concatenate(out)[: int(seconds * common.SR)] if n >= seconds * common.SR else np.concatenate(out)


def worker(threads: int, limit: int, noisy: bool) -> dict:
    manifest = [c for c in common.load_manifest() if c["engine"] == "kokoro"]
    # one voice-balanced subset: round-robin over voices
    by_voice: dict[str, list[dict]] = {}
    for c in manifest:
        by_voice.setdefault(c["voice"], []).append(c)
    picked = []
    for i in range(max(len(v) for v in by_voice.values())):
        for v in by_voice.values():
            if i < len(v):
                picked.append(v[i])
    picked = picked[:limit]
    audio = {c["file"]: common.read_wav(common.FIXTURES / c["file"]) for c in picked}

    rss0 = common.cur_rss_mb()
    rec, load_s = load(threads)
    rss_loaded = common.cur_rss_mb()
    decode(rec, next(iter(audio.values())))  # warm-up (allocations, first-run kernels)

    out: dict = {"threads": threads, "load_s": load_s, "rss_before_mb": rss0, "rss_loaded_mb": rss_loaded}
    rows = []
    for c in picked:
        text, wall, cpu = decode(rec, audio[c["file"]])
        dur = len(audio[c["file"]]) / common.SR
        rows.append({"text": text, "ref": c["text"], "dur": dur, "wall": wall, "cpu": cpu})
    out["short"] = {
        "n": len(rows), "audio_s": sum(r["dur"] for r in rows),
        "rtf": common.summarize([r["wall"] / r["dur"] for r in rows]),
        "cpu_per_audio_s": common.summarize([r["cpu"] / r["dur"] for r in rows]),
        "wer_clean": common.wer([r["ref"] for r in rows], [r["text"] for r in rows]),
    }
    # long dictation: concatenate clips with 350 ms gaps (what V4 would chunk)
    xs = [audio[c["file"]] for c in picked]
    for secs in (30, 60):
        long_rows = []
        for rep in range(3):
            x = concat(xs[rep * 7:] + xs[: rep * 7], secs)
            _, wall, cpu = decode(rec, x)
            long_rows.append({"dur": len(x) / common.SR, "wall": wall, "cpu": cpu})
        out[f"long_{secs}s"] = {
            "rtf": common.summarize([r["wall"] / r["dur"] for r in long_rows]),
            "cpu_per_audio_s": common.summarize([r["cpu"] / r["dur"] for r in long_rows]),
            "audio_s": long_rows[0]["dur"],
        }
    if noisy:
        out["noisy"] = noisy_wer(rec, picked, audio)
    out["peak_rss_mb"] = common.peak_rss_mb()
    return out


def noisy_wer(rec, picked, audio) -> dict:
    """WER on clean vs degraded copies. Degradations are synthetic; see make_noisy."""
    import make_noisy

    res = {}
    for cond, fn in make_noisy.conditions().items():
        refs, hyps = [], []
        for i, c in enumerate(picked[:40]):
            x = fn(audio[c["file"]], i, picked, audio)
            text, _, _ = decode(rec, x)
            refs.append(c["text"])
            hyps.append(text)
        res[cond] = common.wer(refs, hyps)
    return res


def contention() -> dict:
    """Kokoro time-to-first-audio alone vs while Parakeet decodes in the same process."""
    from kokoro_onnx import Kokoro

    from jarvis import config

    k = Kokoro(str(config.KOKORO_MODEL), str(config.KOKORO_VOICES))
    sentence = "The build finished without errors, but the second test suite is still running."
    k.create(sentence, voice="bm_george", speed=1.2, lang="en-gb")  # warm
    alone = []
    for _ in range(6):
        t0 = time.perf_counter()
        k.create(sentence, voice="bm_george", speed=1.2, lang="en-gb")
        alone.append(time.perf_counter() - t0)
    rec, _ = load(4)
    manifest = [c for c in common.load_manifest() if c["engine"] == "kokoro"][:6]
    longx = np.concatenate([common.read_wav(common.FIXTURES / c["file"]) for c in manifest])
    stop = threading.Event()
    busy = {"decodes": 0}

    def hog():
        while not stop.is_set():
            decode(rec, longx)
            busy["decodes"] += 1

    th = threading.Thread(target=hog)
    th.start()
    time.sleep(1.0)
    during = []
    for _ in range(6):
        t0 = time.perf_counter()
        k.create(sentence, voice="bm_george", speed=1.2, lang="en-gb")
        during.append(time.perf_counter() - t0)
    stop.set()
    th.join()
    return {"kokoro_alone_s": common.summarize(alone), "kokoro_during_parakeet_s": common.summarize(during),
            "parakeet_threads": 4, "parakeet_decodes_during": busy["decodes"],
            "note": "Kokoro create() returns the whole sentence; first-audio is that latency here "
                    "(the daemon sends whole chunks), so this is time-to-first-audio per chunk."}


def main(argv: list[str]) -> int:
    if os.environ.get("JARVIS_VOICE_V0") != "1" and "--worker" not in argv:
        print("skipped: set JARVIS_VOICE_V0=1 to run the V0 STT probe (needs fixtures and models)")
        return 0
    arg = {a.split("=")[0]: (a.split("=", 1)[1] if "=" in a else True) for a in argv if a.startswith("--")}
    if "--worker" in arg:
        res = worker(int(arg["--worker"]), int(arg.get("--clips", 40)), "--noisy" in arg)
        print("RESULT " + json.dumps(res, default=float))
        return 0
    if "--contention" in arg:
        common.emit("stt_contention", contention())
        return 0
    results = []
    for t in str(arg.get("--threads", PER_COUNT_DEFAULT)).split(","):
        cmd = [sys.executable, __file__, f"--worker={t}", f"--clips={arg.get('--clips', 40)}"]
        if "--noisy" in arg and t == str(arg.get("--noisy-threads", 4)):
            cmd.append("--noisy")
        env = dict(os.environ)
        env.pop("OMP_NUM_THREADS", None)
        p = subprocess.run(cmd, capture_output=True, text=True, env=env)
        line = [ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")]
        if not line:
            print(p.stdout[-2000:], p.stderr[-2000:])
            return 1
        results.append(json.loads(line[0][7:]))
    common.emit("stt_rtf", {"model": "parakeet-tdt-0.6b-v3 int8 (sherpa-onnx %s)" % __import__("sherpa_onnx").__version__,
                            "runs": results})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
