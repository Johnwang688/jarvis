"""Print markdown tables from ~/.cache/jarvis-voice-v0/results/*.json (what the voice-face note quotes).

    python tests/voice_v0/render_results.py > /tmp/v0-tables.md
"""

from __future__ import annotations

import json
import sys

import common  # noqa: F401,I001

R = common.WORK / "results"


def load(name: str):
    p = R / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else None


def f(x, n=1):
    return "-" if x is None else f"{x:.{n}f}"


def browser() -> None:
    d = load("browser_cpu")
    if not d:
        return
    r = d["runs"]
    print(f"\n#### Browser WASM CPU (Chromium {d['chromium']}, {d['segments']} x {d['segment_seconds']:g} s paced segments)\n")
    print("| module | steady % of one core (median) | above null-worker baseline | first 5 s | worker ms/frame p50 / p95 (budget) | renderer RSS delta MB |")
    print("|---|---|---|---|---|---|")
    for k, label in (("silero5", "Silero VAD v5.1.2 (ort-web 1.30.0)"), ("silero6", "Silero VAD v6.2.3 (ort-web 1.30.0)"),
                     ("rnnoise", "RNNoise 0.2 (Jitsi WASM), 48 kHz")):
        v = r[k]
        fm = v["frame_ms"]
        print(f"| {label} | {f(v['pct_of_core_steady_median'])} | {f(v['pct_of_core_steady_minus_null_worker'])} | {f(v['pct_of_core_first_segment'])} | "
              f"{f(fm['p50'], 1)} / {f(fm['p95'], 1)} ({fm['frame_budget_ms']:g}) | {f(v['renderer_rss_mb_delta_vs_before_prepare'], 0)} |")
    c = r.get("combo_rnnoise_plus_silero_concurrent")
    if c:
        print(f"| RNNoise + Silero together | {f(c['pct_of_core_steady_median'])} | {f(c['steady_minus_two_null_workers'])} | {f(c['segments_pct'][0])} | - | - |")
    print(f"\nNull worker (message passing only): {f(r['baseline_null_worker_16k_frames']['steady_pct_median'])}% at 32 ms frames, "
          f"{f(r['baseline_null_worker_48k_frames']['steady_pct_median'])}% at 10 ms frames.")
    k = d.get("kws_sherpa_wasm_under_node")
    if k and "skipped" in k:
        print(f"\nKWS WASM under Node was skipped: {k['skipped']}")
    elif k:
        print("\n| sherpa-onnx KWS WASM under Node/V8, 1 thread | CPU s per audio s (= fraction of a core) | wall s per audio s | RSS MB |\n|---|---|---|---|")
        for flag in ("int8", "fp32"):
            x = k[flag]
            print(f"| {flag} | {f(100 * x['cpu_per_audio_s'])}% | {f(100 * x['wall_per_audio_s'])}% | {f(x['rss_mb'], 0)} |")


def vad() -> None:
    d = load("browser_vadquality") or load("browser_cpu")
    if not d or "vad_quality" not in d:
        return
    q = d["vad_quality"]
    print(f"\n#### VAD on degraded synthetic speech ({q['clips']} clips, {q['frames']} frames of 32 ms, {q['truth_speech_frames']} speech by clean-energy labels)\n")
    print("| condition | Silero precision / recall / false-accept frames | RNNoise then Silero | RNNoise's own VAD output |\n|---|---|---|---|")
    for name, v in q["conditions"].items():
        if "silero" not in v:
            continue
        cell = lambda x: f"{x['precision']:.3f} / {x['recall']:.3f} / {x['false_accept_frames']}"  # noqa: E731
        print(f"| {name} | {cell(v['silero'])} | {cell(v['rnnoise_then_silero'])} | {cell(v['rnnoise_own_vad_output'])} |")
    for name, v in q["conditions"].items():
        if "silero" in v:
            continue
        print(f"\n{name}: Silero accepts {v['silero_frames_above_0.5_pct']:.1f}% of frames, after RNNoise {v['rnnoise_then_silero_frames_above_0.5_pct']:.1f}%.")


def stt() -> None:
    d = load("stt_rtf")
    if not d:
        return
    print(f"\n#### Local STT: {d['model']}\n")
    print("| ORT threads | load s | RSS after load / peak MB | RTF 5 s clips p50 / p95 | RTF 30 s | RTF 60 s | CPU s per audio s (5 s clips) | machine load1 start / end |\n|---|---|---|---|---|---|---|---|")
    for r in d["runs"]:
        s = r["short"]
        print(f"| {r['threads']} | {f(r['load_s'])} | {f(r['rss_loaded_mb'], 0)} / {f(r['peak_rss_mb'], 0)} | {s['rtf']['p50']:.3f} / {s['rtf']['p95']:.3f} | "
              f"{r['long_30s']['rtf']['p50']:.3f} | {r['long_60s']['rtf']['p50']:.3f} | {s['cpu_per_audio_s']['p50']:.2f} | {f(r['load1_at_start'])} / {f(r['load1_at_end'])} |")
    w = d["runs"][0]["short"]
    print(f"\nWER, clean synthetic: {100 * w['wer_clean']['wer']:.1f}% ({w['wer_clean']['sub']} sub, {w['wer_clean']['del']} del, "
          f"{w['wer_clean']['ins']} ins over {w['wer_clean']['ref_words']} words, {w['n']} clips, {w['audio_s']:.0f} s of audio).")
    for r in d["runs"]:
        if "noisy" in r:
            print("\n| degradation (synthetic) | WER |\n|---|---|")
            for c, x in r["noisy"].items():
                print(f"| {c} | {100 * x['wer']:.1f}% ({x['ref_words']} words) |")
    ns = load("stt_noise_suppression")
    if ns:
        print("\n| degradation | WER as captured | WER after RNNoise |\n|---|---|---|")
        for c, v in ns.items():
            if c != "_env":
                print(f"| {c} | {100 * v['as_captured']['wer']:.1f}% | {100 * v['rnnoise']['wer']:.1f}% ({v['as_captured']['ref_words']} words) |")
    c = load("stt_contention")
    if c:
        print("\n| Kokoro (one 15-word sentence) | alone p50 / p95 s | during Parakeet p50 / p95 s | slowdown |\n|---|---|---|---|")
        for k in ("daemon_default_threads", "pinned_4_threads"):
            if k in c:
                x = c[k]
                print(f"| {k} | {x['kokoro_alone_s']['p50']:.2f} / {x['kokoro_alone_s']['p95']:.2f} | {x['kokoro_during_parakeet_s']['p50']:.2f} / {x['kokoro_during_parakeet_s']['p95']:.2f} | {x['slowdown_p50']:.2f}x |")
        print(f"(load1 {c['load1_at_start']:.1f} -> {c['load1_at_end']:.1f})")


def speaker() -> None:
    d = load("speaker_eer")
    if not d:
        return
    print(f"\n#### Speaker embedding on SYNTHETIC voices ({d['speakers']} speakers: {d['kokoro_voices']} Kokoro + {d['pocket_voices']} Pocket, {d['clips']} clips)\n")
    print("| model | EER clean full clip | EER 3 s | EER 3 s, noise + Opus | EER 3 s + second voice at 10 dB | FAR at 1% FRR (overlap) | EER threshold (3 s) |\n|---|---|---|---|---|---|---|")
    for m in d["eer"]:
        e = lambda c: f"{100 * m[c]['eer']:.2f}%"  # noqa: E731
        print(f"| {m['model']} | {e('clean_full')} | {e('clean_3s')} | {e('degraded_3s')} | {e('overlap_3s')} | {100 * m['overlap_3s']['FAR@FRR=1%']:.1f}% | {m['clean_3s']['eer_threshold']:.3f} |")
    n = d["eer"][0]["clean_3s"]
    print(f"\nTrials per condition: {n['n_genuine']} genuine, {n['n_impostor']} impostor.\n")
    print("| model | 3 s embedding p50 / p95 ms, 1 thread | 4 threads | RSS growth MB | \n|---|---|---|---|")
    by = {}
    for l in d["latency"]:
        by.setdefault(l["model"], {})[l["threads"]] = l
    for m, v in by.items():
        print(f"| {m} | {v[1]['wall_ms']['p50']:.1f} / {v[1]['wall_ms']['p95']:.1f} | {v[4]['wall_ms']['p50']:.1f} / {v[4]['wall_ms']['p95']:.1f} | {v[1]['rss_growth_mb_from_loading_this_extractor']:.0f} |")


def turn() -> None:
    d = load("smart_turn")
    if not d:
        return
    print("\n#### Smart Turn v3.2 (CPU, int8) on SYNTHETIC sentences vs fragments\n")
    print("| trailing audio | p(complete) median: complete / incomplete | complete recall @0.5 | incomplete correct @0.5 | AUC | best-threshold accuracy |\n|---|---|---|---|---|---|")
    for k, v in d["behaviour"].items():
        print(f"| {k} | {v['complete_p']['p50']:.3f} / {v['incomplete_p']['p50']:.3f} | {100 * v['complete_recall@0.5']:.0f}% | {100 * v['incomplete_correct@0.5']:.0f}% | {v['auc']:.2f} | {100 * v['best_threshold_accuracy']:.0f}% @ {v['best_threshold']:.2f} |")
    print("\n| threads | features ms p50 / p95 | model ms p50 / p95 | total ms p50 / p95 | CPU ms p50 |\n|---|---|---|---|---|")
    for l in d["latency"]:
        print(f"| {l['threads']} | {l['features_ms']['p50']:.1f} / {l['features_ms']['p95']:.1f} | {l['model_ms']['p50']:.1f} / {l['model_ms']['p95']:.1f} | {l['total_ms']['p50']:.1f} / {l['total_ms']['p95']:.1f} | {l['cpu_ms']['p50']:.1f} |")


def kws() -> None:
    d = load("kws_native")
    if not d:
        return
    print("\n#### sherpa-onnx KWS, native, synthetic speech\n")
    print("| model file | threads | CPU % of a core, real time | RSS MB |\n|---|---|---|---|")
    for c in d["cpu"]:
        print(f"| {'int8' if c['int8'] else 'fp32'} | {c['threads']} | {c['cpu_pct_of_one_core_realtime']:.2f} | {c['rss_mb']:.0f} |")
    # Denominators: expected occurrences per keyword in the special fixtures. Recall below pools the
    # positive phrases with the few "hard negative" phrases that legitimately contain a keyword.
    sp = common.FIXTURES / "special" / "special.json"
    if sp.exists():
        pos, inc = {}, {}
        for it in json.loads(sp.read_text()):
            if it["group"].startswith("kws_"):
                tgt = pos if it["group"] == "kws_pos" else inc
                for w in it.get("expect", []):
                    tgt[w.upper()] = tgt.get(w.upper(), 0) + 1
        print("\nn per keyword (positive phrases + negative-group phrases that really contain the word; recall pools both):")
        print(", ".join(f"{k} {pos.get(k, 0)}+{inc.get(k, 0)}" for k in ("JARVIS", "OVER", "CANCEL", "PAUSE", "RESUME", "SKIP", "STOP")))
    print("\n| config (boost, threshold) | recall | JARVIS | OVER | CANCEL | PAUSE | RESUME | SKIP | STOP | stem-variant FA | unrelated FA | dictation FA/h |\n|---|---|---|---|---|---|---|---|---|---|---|---|")
    rows = list(d["grid"]) + [d["int8_default"], d["tuned_short_words"]]
    for g in rows:
        pk = g["per_keyword_recall"]
        label = f"{g['score']} / {g.get('threshold', '-')}" + (" int8" if g["int8"] else "")
        print(f"| {label} | {100 * g['recall']:.0f}% | " + " | ".join(f"{100 * pk[k]:.0f}%" for k in ("JARVIS", "OVER", "CANCEL", "PAUSE", "RESUME", "SKIP", "STOP"))
              + f" | {g['special_false_accepts_stem_variants']} | {g['special_false_accepts_unrelated']} | {g['dictation_false_accepts_per_hour']:.1f} ({g['dictation_false_accepts']} in {g['dictation_audio_min']:.1f} min) |")
    print(f"\nWith 10 dB pink noise recall is {100 * d['degraded']['pink_10dB']['recall']:.0f}%, with 10 dB competing speech {100 * d['degraded']['tv_10dB']['recall']:.0f}% (default config).")


if __name__ == "__main__":
    for fn in (browser, vad, stt, speaker, turn, kws):
        fn()
    sys.exit(0)
