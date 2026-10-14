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

import common  # noqa: F401,I001  (first: pins BLAS threads before numpy loads)
import numpy as np

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
    # Spread over voices AND sentences: voice k % V, sentence (5k + k // V) mod S. A plain "first N
    # per voice" would only ever hit the first few sentences.
    by_voice: dict[str, dict[int, dict]] = {}
    for c in manifest:
        by_voice.setdefault(c["voice"], {})[c["sentence"]] = c
    voices = sorted(by_voice)
    n_sent = len(next(iter(by_voice.values())))
    picked = []
    for k in range(limit):
        v = voices[k % len(voices)]
        picked.append(by_voice[v][(5 * k + k // len(voices)) % n_sent])
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
    mism = [{"ref": r["ref"], "hyp": r["text"]} for r in rows
            if common.norm_words(r["ref"]) != common.norm_words(r["text"])]
    out["load1_at_start"] = os.getloadavg()[0]
    out["mismatch_examples"] = mism[:10]
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
    out["load1_at_end"] = os.getloadavg()[0]
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


def ns_wer(threads: int = 4) -> dict:
    """STT WER on noisy vs RNNoise-denoised audio written by probe_browser.py (vadquality).

    Answers "does noise suppression raise or lower the word error rate" for synthetic
    degradations only. The WAVs are 12 dictation clips joined with 0.8 s gaps; we cut them
    back apart by the known lengths (RNNoise preserves length) and decode clip by clip.
    """
    results = common.WORK / "results"
    src = results / "browser_vadquality.json"
    res = json.loads((src if src.exists() else results / "browser_cpu.json").read_text())["vad_quality"]
    order = res["clip_order_for_stt"]
    by_file = {c["file"]: c for c in common.load_manifest()}
    lengths = [len(common.read_wav(common.FIXTURES / f)) for f in order]
    gap = int(0.8 * common.SR)
    rec, _ = load(threads)
    out = {}
    for cond in res["conditions"]:
        if cond.endswith("_only") or "only" in cond:
            continue
        row = {}
        for variant, fname in (("as_captured", f"{cond}.wav"), ("rnnoise", f"{cond}__rnnoise.wav")):
            x = common.read_wav(common.WORK / "denoised" / fname)
            pos, refs, hyps = 0, [], []
            for f, n in zip(order, lengths, strict=True):
                seg = x[pos: pos + n + gap // 2]
                pos += n + gap
                refs.append(by_file[f]["text"])
                hyps.append(decode(rec, seg)[0])
            row[variant] = common.wer(refs, hyps)
        out[cond] = row
    return out


def contention() -> dict:
    """Kokoro synthesis time per sentence alone vs while Parakeet decodes in the same process.

    Two Kokoro configurations: the daemon's (jarvis.voice: Kokoro(model, voices), ORT default threads
    = all cores) and a pinned 4-thread session. Parakeet runs 4 threads in a background loop.
    """
    import onnxruntime as ort
    from kokoro_onnx import Kokoro

    from jarvis import config

    sentence = "The build finished without errors, but the second test suite is still running."
    manifest = [c for c in common.load_manifest() if c["engine"] == "kokoro"][:6]
    longx = np.concatenate([common.read_wav(common.FIXTURES / c["file"]) for c in manifest])
    rec, _ = load(4)

    def make(pinned: bool):
        if not pinned:
            return Kokoro(str(config.KOKORO_MODEL), str(config.KOKORO_VOICES))
        so = ort.SessionOptions()
        so.intra_op_num_threads, so.inter_op_num_threads = 4, 1
        sess = ort.InferenceSession(str(config.KOKORO_MODEL), sess_options=so, providers=["CPUExecutionProvider"])
        return Kokoro.from_session(sess, str(config.KOKORO_VOICES))

    def times(k, n=8):
        out = []
        for _ in range(n):
            t0 = time.perf_counter()
            samples, sr = k.create(sentence, voice="bm_george", speed=1.2, lang="en-gb")
            out.append(time.perf_counter() - t0)
        return out, len(samples) / sr

    res = {"parakeet_threads": 4, "load1_at_start": os.getloadavg()[0]}
    for label, pinned in (("daemon_default_threads", False), ("pinned_4_threads", True)):
        k = make(pinned)
        k.create(sentence, voice="bm_george", speed=1.2, lang="en-gb")  # warm
        alone, audio_s = times(k)
        stop, busy = threading.Event(), {"decodes": 0}

        def hog():
            while not stop.is_set():
                decode(rec, longx)
                busy["decodes"] += 1

        th = threading.Thread(target=hog)
        th.start()
        time.sleep(1.0)
        during, _ = times(k)
        stop.set()
        th.join()
        res[label] = {"sentence_audio_s": audio_s, "kokoro_alone_s": common.summarize(alone),
                      "kokoro_during_parakeet_s": common.summarize(during),
                      "slowdown_p50": common.pct(during, 50) / common.pct(alone, 50),
                      "parakeet_decodes_during": busy["decodes"]}
    res["load1_at_end"] = os.getloadavg()[0]
    res["note"] = ("Kokoro create() returns a whole sentence, which is how the daemon's /say works, so this latency "
                   "is the time to first audio per chunk. Both models share this process's cores and the machine's load.")
    return res


def main(argv: list[str]) -> int:
    if os.environ.get("JARVIS_VOICE_V0") != "1" and not any(a.startswith("--worker") for a in argv):
        print("skipped: set JARVIS_VOICE_V0=1 to run the V0 STT probe (needs fixtures and models)")
        return 0
    arg = common.parse_args(argv)
    if "--worker" in arg:
        res = worker(int(arg["--worker"]), int(arg.get("--clips", 40)), "--noisy" in arg)
        print("RESULT " + json.dumps(res, default=float))
        return 0
    if "--ns" in arg:
        common.emit("stt_noise_suppression", ns_wer(int(arg.get("--threads", 4))))
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
