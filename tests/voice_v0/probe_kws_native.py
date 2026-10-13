"""Native sherpa-onnx open-vocabulary KWS probe (zipformer gigaspeech 3.3M).

    JARVIS_VOICE_V0=1 python tests/voice_v0/probe_kws_native.py

What it measures, on SYNTHETIC speech only:
  * CPU per audio second (process CPU time / audio seconds, 100 ms chunks fed
    as fast as possible) for the fp32 and int8 model files at 1 and 2 threads.
    This is the native cost; the browser (WASM) cost is probe_browser.py.
  * Detection of the plan's keywords (jarvis, over, cancel, pause, resume, skip,
    stop) in isolated clips, carrier phrases and trailing dictation words, and
    false accepts on similar-sounding words and on ~30 minutes of ordinary
    dictation, over a (boost score, threshold) grid.
  * Detection delay: audio time between the end of the last spoken word and the
    detection callback (excludes compute).
Distance (1 m / 3 m), room reverberation, a real TV and a real person are NOT
measured here; those stay in the manual gate.
"""

from __future__ import annotations

import json
import os
import sys

import numpy as np

import common
import make_noisy
import models

KEYWORDS = ["JARVIS", "OVER", "CANCEL", "PAUSE", "RESUME", "SKIP", "STOP"]
CHUNK = 1600  # 100 ms


def kws_dir():
    return models.extracted("kws-zipformer-gigaspeech-3.3M")


def write_keywords(path: str, score: float, threshold: float) -> None:
    import sentencepiece as spm

    sp = spm.SentencePieceProcessor(model_file=str(kws_dir() / "bpe.model"))
    with open(path, "w") as fh:
        for w in KEYWORDS:
            pieces = sp.encode(w, out_type=str)
            fh.write(f"{' '.join(pieces)} :{score} #{threshold} @{w}\n")


def spotter(int8: bool, threads: int, score: float, threshold: float, tmp: str):
    import sherpa_onnx

    d = kws_dir()
    suffix = ".int8.onnx" if int8 else ".onnx"
    kw = f"{tmp}/kw_{score}_{threshold}.txt"
    write_keywords(kw, score, threshold)
    return sherpa_onnx.KeywordSpotter(
        tokens=str(d / "tokens.txt"),
        encoder=str(d / f"encoder-epoch-12-avg-2-chunk-16-left-64{suffix}"),
        decoder=str(d / f"decoder-epoch-12-avg-2-chunk-16-left-64{suffix}"),
        joiner=str(d / f"joiner-epoch-12-avg-2-chunk-16-left-64{suffix}"),
        keywords_file=kw, num_threads=threads, keywords_score=score, keywords_threshold=threshold,
        max_active_paths=4, num_trailing_blanks=1)


def run_clip(sp, x: np.ndarray, tail_s: float = 0.8) -> list[tuple[str, float]]:
    """[(keyword, audio_time_s)] for one clip, fed in 100 ms chunks plus a silent tail."""
    x = np.concatenate([x, np.zeros(int(tail_s * common.SR), np.float32)])
    s = sp.create_stream()
    hits = []
    for pos in range(0, len(x), CHUNK):
        s.accept_waveform(common.SR, x[pos: pos + CHUNK])
        while sp.is_ready(s):
            sp.decode_stream(s)
            r = sp.get_result(s)
            word = r if isinstance(r, str) else getattr(r, "keyword", "")
            if word:
                hits.append((word.strip().upper(), (pos + CHUNK) / common.SR))
                sp.reset_stream(s)
    return hits


def cpu_cost(int8: bool, threads: int, tmp: str, clips: list[np.ndarray]) -> dict:
    sp = spotter(int8, threads, 1.0, 0.25, tmp)
    run_clip(sp, clips[0])
    audio_s = 0.0
    with common.Cpu() as c:
        for x in clips:
            run_clip(sp, x, tail_s=0.0)
            audio_s += len(x) / common.SR
    return {"int8": int8, "threads": threads, "audio_s": audio_s, "wall_per_audio_s": c.wall / audio_s,
            "cpu_per_audio_s": c.cpu / audio_s, "cpu_pct_of_one_core_realtime": 100 * c.cpu / audio_s,
            "rss_mb": common.cur_rss_mb()}


def evaluate(sp, special: list[dict], audio: dict, dictation: list[tuple[dict, np.ndarray]]) -> dict:
    tp = fn = fp = 0
    per_kw = {k: [0, 0] for k in KEYWORDS}  # [detected, expected]
    delays, false_hits = [], []
    for it in special:
        hits = run_clip(sp, audio[it["file"]])
        got = [h[0] for h in hits]
        for e in it["expect"]:
            per_kw[e.upper()][1] += 1
            if e.upper() in got:
                tp += 1
                per_kw[e.upper()][0] += 1
                if it["group"] == "kws_pos":
                    delays.append(next(t for w, t in hits if w == e.upper()) - len(audio[it["file"]]) / common.SR
                                  if it["expect"][-1] == e else np.nan)
            else:
                fn += 1
        for w in got:
            if w.lower() not in it["expect"]:
                fp += 1
                false_hits.append({"clip": it["text"], "voice": it["voice"], "kw": w})
    dict_fp, dict_audio = 0, 0.0
    for c, x in dictation:
        hits = run_clip(sp, x)
        expected = set(common.norm_words(c["text"]))
        dict_audio += len(x) / common.SR
        for w, _ in hits:
            if w.lower() not in expected:
                dict_fp += 1
    d = [v for v in delays if not np.isnan(v)]
    return {"recall": tp / max(tp + fn, 1), "tp": tp, "fn": fn, "special_false_accepts": fp,
            "per_keyword_recall": {k: (v[0] / v[1] if v[1] else None) for k, v in per_kw.items()},
            "dictation_audio_min": dict_audio / 60, "dictation_false_accepts": dict_fp,
            "dictation_false_accepts_per_hour": dict_fp / (dict_audio / 3600) if dict_audio else None,
            "delay_after_last_word_s": common.summarize(d) if d else None, "false_hit_examples": false_hits[:8]}


def main(argv: list[str]) -> int:
    if os.environ.get("JARVIS_VOICE_V0") != "1":
        print("skipped: set JARVIS_VOICE_V0=1 to run the V0 native KWS probe (needs fixtures and models)")
        return 0
    import tempfile

    special = json.loads((common.FIXTURES / "special" / "special.json").read_text())
    special = [i for i in special if i["group"].startswith("kws_")]
    audio = {i["file"]: common.read_wav(common.FIXTURES / "special" / i["file"]) for i in special}
    main_clips = [c for c in common.load_manifest() if c["engine"] == "kokoro"]
    main_audio = [(c, common.read_wav(common.FIXTURES / c["file"])) for c in main_clips[::2]]  # ~every other clip
    res = {"model": "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01", "note": "SYNTHETIC speech only",
           "cpu": [], "grid": []}
    with tempfile.TemporaryDirectory() as tmp:
        sample = [x for _, x in main_audio[:30]]
        for int8 in (False, True):
            for th in (1, 2):
                res["cpu"].append(cpu_cost(int8, th, tmp, sample))
                print("cpu", res["cpu"][-1], file=sys.stderr)
        for score in (1.0, 2.0):
            for thr in (0.15, 0.25, 0.4):
                sp = spotter(False, 1, score, thr, tmp)
                ev = evaluate(sp, special, audio, main_audio)
                ev.update({"score": score, "threshold": thr, "int8": False})
                res["grid"].append(ev)
                print("grid", score, thr, ev["recall"], ev["dictation_false_accepts_per_hour"], file=sys.stderr)
        # int8 at the default operating point, and under degradation at the default
        sp = spotter(True, 1, 1.0, 0.25, tmp)
        ev = evaluate(sp, special, audio, main_audio)
        ev.update({"score": 1.0, "threshold": 0.25, "int8": True})
        res["int8_default"] = ev
        sp = spotter(False, 1, 1.0, 0.25, tmp)
        pos = [i for i in special if i["group"] == "kws_pos"]
        res["degraded"] = {}
        for name, mk in (("pink_10dB", lambda x, i: common.mix_at_snr(x, make_noisy.pink(len(x), seed=i), 10)),
                         ("tv_10dB", lambda x, i: common.mix_at_snr(
                             x, make_noisy.babble(main_audio_list := [c for c, _ in main_audio],
                                                  {c["file"]: a for c, a in main_audio}, i, len(x)), 10))):
            tp = n = 0
            for i, it in enumerate(pos):
                x = mk(audio[it["file"]], i)
                got = [h[0] for h in run_clip(sp, x)]
                for e in it["expect"]:
                    n += 1
                    tp += e.upper() in got
            res["degraded"][name] = {"recall": tp / n, "n": n}
    res["peak_rss_mb"] = common.peak_rss_mb()
    common.emit("kws_native", res)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
