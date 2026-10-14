"""Speaker-embedding probe: EER on SYNTHETIC voices and verification latency.

    JARVIS_VOICE_V0=1 python tests/voice_v0/probe_speaker.py [--threads 1,4]

IMPORTANT: this is NOT owner-vs-TV EER. The "speakers" are Kokoro (and, when
fixtures exist, Pocket) voices. A synthetic voice is a fixed style vector, so
two clips of one voice differ only by text and speed, which makes genuine
trials unrealistically easy; real people vary with mood, distance, mic and
cold. Treat the numbers as a floor on model separation, a latency/RSS receipt
and a way to compare models, never as the expected false-accept rate.

Protocol (mirrors plan 3.4): enrol = centroid of the L2-normalised embeddings
of 3 clips; test = cosine against the centroid; genuine = held-out clips of
the enrolled voice, impostor = clips of every other voice. Conditions:
  clean_full   whole clips
  clean_3s     first 3 s only (the length /stt?verify would embed)
  degraded_3s  first 3 s through Opus 24 kbit/s (the codec the HUD uploads; Ogg container here) plus
               pink noise at 15 dB SNR
  overlap_3s   first 3 s with a second voice mixed in at 10 dB (a TV talking)
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

import common  # noqa: F401,I001  (first: pins BLAS threads before numpy loads)
import numpy as np

import make_noisy
import models

MODELS = {
    "wespeaker_en_voxceleb_resnet34": "spk-wespeaker-resnet34",
    "wespeaker_en_voxceleb_CAM++": "spk-wespeaker-campp",
    "3dspeaker_campplus_en_voxceleb": "spk-3dspeaker-campplus",
    "nemo_titanet_small": "spk-nemo-titanet-small",
}


def opus_roundtrip(x: np.ndarray, kbps: int = 24) -> np.ndarray:
    with tempfile.TemporaryDirectory() as td:
        src, enc, dec = f"{td}/a.wav", f"{td}/a.ogg", f"{td}/b.wav"
        common.write_wav(src, x)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-c:a", "libopus", "-b:a", f"{kbps}k", enc], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", enc, "-ar", "16000", "-ac", "1", dec], check=True)
        return common.read_wav(dec)


def extractor(name: str, threads: int):
    import sherpa_onnx

    cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(models.model_file(MODELS[name])), num_threads=threads, debug=False)
    return sherpa_onnx.SpeakerEmbeddingExtractor(cfg)


def embed(ex, x: np.ndarray) -> np.ndarray:
    s = ex.create_stream()
    s.accept_waveform(sample_rate=common.SR, waveform=x)
    s.input_finished()
    e = np.asarray(ex.compute(s), dtype=np.float64)
    return e / (np.linalg.norm(e) + 1e-12)


def prep(x: np.ndarray, seed: int) -> np.ndarray:
    """Trim Kokoro's digital-zero padding and lay a faint pink noise floor (-60 dBFS) under the clip.

    Exact-zero frames are unlike any microphone, and some embedders (CAM++ here) score same-voice pairs
    very differently with and without them, which would make the model comparison about padding.
    """
    idx = np.where(np.abs(x) > 0.003)[0]
    x = x[idx[0]: idx[-1] + 1] if len(idx) else x
    return (x + make_noisy.pink(len(x), seed=seed) * 10 ** (-60 / 20)).astype(np.float32)


def conditions(clips, audio):
    """name -> function(clip_index) -> waveform."""
    three = 3 * common.SR

    def degraded(i):
        x = audio[clips[i]["file"]][:three]
        x = common.mix_at_snr(x, make_noisy.pink(len(x), seed=100 + i), 15)
        return opus_roundtrip(x)

    def overlap(i):
        x = audio[clips[i]["file"]][:three]
        j = (i * 7 + 13) % len(clips)
        while clips[j]["voice"] == clips[i]["voice"]:
            j = (j + 1) % len(clips)
        other = audio[clips[j]["file"]][:three]
        other = np.pad(other, (0, max(0, len(x) - len(other))))[: len(x)]
        return common.mix_at_snr(x, other, 10)

    return {
        "clean_full": lambda i: audio[clips[i]["file"]],
        "clean_3s": lambda i: audio[clips[i]["file"]][:three],
        "degraded_3s": degraded,
        "overlap_3s": overlap,
    }


def score_condition(embs: np.ndarray, clips, enroll_idx_per_voice, voices, jarvis_voice="bm_george"):
    centroids = {}
    for v in voices:
        c = embs[enroll_idx_per_voice[v]].mean(axis=0)
        centroids[v] = c / np.linalg.norm(c)
    gen, imp = [], []
    pair_imp: dict[tuple[str, str], list[float]] = {}
    jarvis_imp = []
    enrolled_idx = {i for v in voices for i in enroll_idx_per_voice[v]}
    for i, c in enumerate(clips):
        if i in enrolled_idx:
            continue
        for v in voices:
            s = float(embs[i] @ centroids[v])
            if c["voice"] == v:
                gen.append(s)
            else:
                imp.append(s)
                pair_imp.setdefault((v, c["voice"]), []).append(s)
                if c["voice"] == jarvis_voice:
                    jarvis_imp.append(s)
    gen, imp = np.array(gen), np.array(imp)
    e, thr = common.eer(gen, imp)
    # worst pair: the enrolled voice with the highest mean impostor score from one other voice
    worst = max(pair_imp.items(), key=lambda kv: float(np.mean(kv[1])))
    far_at = {}
    for frr_target in (0.01, 0.05):
        t = float(np.quantile(gen, frr_target))  # accept if score >= t -> FRR = target
        far_at[f"FAR@FRR={frr_target:.0%}"] = float(np.mean(imp >= t))
    return {
        "eer": e, "eer_threshold": thr, "genuine": common.summarize(list(gen)), "impostor": common.summarize(list(imp)),
        "n_genuine": len(gen), "n_impostor": len(imp), **far_at,
        "worst_pair": {"enrolled": worst[0][0], "impostor_voice": worst[0][1], "mean_score": float(np.mean(worst[1])),
                       "max_score": float(np.max(worst[1]))},
        "jarvis_voice_as_impostor_max": float(np.max(jarvis_imp)) if jarvis_imp else None,
    }


def run_model(name: str, threads: int, clips, audio, cond_audio, enroll_idx, voices):
    ex = extractor(name, threads)
    out = {"model": name, "file": MODELS[name]}
    embed(ex, audio[clips[0]["file"]][: 3 * common.SR])  # warm
    for cname, waves in cond_audio.items():
        embs = np.stack([embed(ex, w) for w in waves])
        out[cname] = score_condition(embs, clips, enroll_idx, voices)
    return out


def latency(name: str, threads: int, audio, clips) -> dict:
    rss0 = common.cur_rss_mb()
    ex = extractor(name, threads)
    # exactly 3 s of audio per call: sentences are 3-5 s, so join neighbours of one voice and cut
    xs = [np.concatenate([audio[clips[(i * 11 + k) % len(clips)]["file"]] for k in range(3)])[: 3 * common.SR]
          for i in range(50)]
    embed(ex, xs[0])
    times, cpus = [], []
    for x in xs:
        with common.Cpu() as c:
            embed(ex, x)
        times.append(c.wall * 1000)
        cpus.append(c.cpu * 1000)
    return {"model": name, "threads": threads, "audio_s": 3.0, "n": len(times),
            "wall_ms": common.summarize(times), "cpu_ms": common.summarize(cpus),
            "rss_growth_mb_from_loading_this_extractor": common.cur_rss_mb() - rss0, "process_rss_mb": common.cur_rss_mb()}


def main(argv: list[str]) -> int:
    if os.environ.get("JARVIS_VOICE_V0") != "1":
        print("skipped: set JARVIS_VOICE_V0=1 to run the V0 speaker probe (needs fixtures and models)")
        return 0
    arg = common.parse_args(argv)
    threads = [int(t) for t in str(arg.get("--threads", "1,4")).split(",")]
    only = arg.get("--models")
    names = [m for m in MODELS if not only or m in str(only).split(",")]
    all_clips = common.load_manifest()
    clips = [c for c in all_clips if c["engine"] == "kokoro"]
    pocket = [c for c in all_clips if c["engine"] == "pocket"]
    clips += pocket  # Pocket voices join as extra speakers (enrolled with sentences 0-2 too)
    audio = {c["file"]: prep(common.read_wav(common.FIXTURES / c["file"]), i) for i, c in enumerate(clips)}
    voices = sorted({c["voice"] for c in clips})
    enroll_idx = {v: [i for i, c in enumerate(clips) if c["voice"] == v and c["sentence"] in (0, 1, 2)] for v in voices}
    conds = conditions(clips, audio)
    # waveforms are built once (Opus round trips are slow) and shared by every model
    cond_audio = {cname: [fn(i) for i in range(len(clips))] for cname, fn in conds.items()}
    res = {"note": "SYNTHETIC speakers (Kokoro/Pocket voices); not owner-vs-TV EER",
           "speakers": len(voices), "kokoro_voices": len({c["voice"] for c in clips if c["engine"] == "kokoro"}),
           "pocket_voices": len({c["voice"] for c in clips if c["engine"] == "pocket"}), "clips": len(clips),
           "eer": [], "latency": []}
    for n in names:
        t0 = time.time()
        res["eer"].append(run_model(n, 1, clips, audio, cond_audio, enroll_idx, voices))
        print(f"{n}: eer done in {time.time() - t0:.0f}s", file=sys.stderr)
        for t in threads:
            res["latency"].append(latency(n, t, audio, clips))
    res["peak_rss_mb"] = common.peak_rss_mb()
    common.emit("speaker_eer", res)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
