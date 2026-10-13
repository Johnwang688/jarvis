"""Smart Turn (pipecat smart-turn-v3.2-cpu ONNX): latency and behaviour.

    JARVIS_VOICE_V0=1 python tests/voice_v0/probe_smart_turn.py [--threads 1,4]

Input protocol follows pipecat's inference.py: the last 8 s of 16 kHz audio,
Whisper log-mel features (WhisperFeatureExtractor, chunk_length=8), one
`input_features` tensor, sigmoid probability of "turn complete".

Behaviour test (SYNTHETIC): complete Kokoro sentences vs. Kokoro fragments cut
mid-sentence (function word or after a comma), each followed by 0.5 s / 1.0 s
of silence, which is the situation at a pause. Kokoro's prosody on a synthesized
fragment is not a person trailing off, so accuracy here is indicative only; the
plan's adoption test (beats the plain 2.5 s timer on the owner's own dictation)
stays a manual gate.
"""

from __future__ import annotations

import json
import os
import sys

import numpy as np

import common
import models


def session(threads: int):
    import onnxruntime as ort

    so = ort.SessionOptions()
    so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    so.inter_op_num_threads = 1
    so.intra_op_num_threads = threads
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    return ort.InferenceSession(str(models.model_file("smart-turn-v3.2-cpu")), sess_options=so,
                                providers=["CPUExecutionProvider"])


def make_predict(sess):
    from transformers import WhisperFeatureExtractor

    fe = WhisperFeatureExtractor(chunk_length=8)

    def features(audio: np.ndarray) -> np.ndarray:
        audio = audio[-8 * common.SR:]
        f = fe(audio, sampling_rate=common.SR, return_tensors="np", padding="max_length",
               max_length=8 * common.SR, truncation=True, do_normalize=True)
        return np.expand_dims(f.input_features.squeeze(0).astype(np.float32), 0)

    def predict(audio: np.ndarray) -> float:
        return float(sess.run(None, {"input_features": features(audio)})[0].reshape(-1)[0])

    return features, predict


def main(argv: list[str]) -> int:
    if os.environ.get("JARVIS_VOICE_V0") != "1":
        print("skipped: set JARVIS_VOICE_V0=1 to run the V0 Smart Turn probe (needs fixtures and models)")
        return 0
    arg = {a.split("=")[0]: (a.split("=", 1)[1] if "=" in a else True) for a in argv if a.startswith("--")}
    items = json.loads((common.FIXTURES / "special" / "special.json").read_text())
    turn = [i for i in items if i["group"].startswith("turn_")]
    audio = {i["file"]: common.read_wav(common.FIXTURES / "special" / i["file"]) for i in turn}
    sess = session(1)
    features, predict = make_predict(sess)
    res = {"model": "smart-turn-v3.2-cpu", "input_names": [i.name for i in sess.get_inputs()],
           "note": "SYNTHETIC Kokoro sentences vs. fragments; see module docstring", "behaviour": {}, "latency": []}
    for silence in (0.2, 0.5, 1.0):
        pad = np.zeros(int(silence * common.SR), np.float32)
        rows = {"turn_complete": [], "turn_incomplete": []}
        for i in turn:
            rows[i["group"]].append(predict(np.concatenate([audio[i["file"]], pad])))
        comp, inc = np.array(rows["turn_complete"]), np.array(rows["turn_incomplete"])
        acc = float((np.sum(comp > 0.5) + np.sum(inc <= 0.5)) / (len(comp) + len(inc)))
        res["behaviour"][f"{silence}s_silence"] = {
            "complete_p": common.summarize(list(comp)), "incomplete_p": common.summarize(list(inc)),
            "complete_recall@0.5": float(np.mean(comp > 0.5)), "incomplete_correct@0.5": float(np.mean(inc <= 0.5)),
            "accuracy@0.5": acc, "n_complete": len(comp), "n_incomplete": len(inc),
        }
    # latency, split into feature extraction vs model run, for a typical 6 s window
    x = np.concatenate([audio[turn[0]["file"]], np.zeros(int(0.5 * common.SR), np.float32)])
    for t in [int(v) for v in str(arg.get("--threads", "1,4")).split(",")]:
        s = session(t)
        feat, pred = make_predict(s)
        for _ in range(3):
            pred(x)
        fe_ms, run_ms, tot_ms, cpu_ms = [], [], [], []
        import time

        for _ in range(60):
            with common.Cpu() as c:
                t0 = time.perf_counter()
                f = feat(x)
                t1 = time.perf_counter()
                s.run(None, {"input_features": f})
                t2 = time.perf_counter()
            fe_ms.append((t1 - t0) * 1000)
            run_ms.append((t2 - t1) * 1000)
            tot_ms.append((t2 - t0) * 1000)
            cpu_ms.append(c.cpu * 1000)
        res["latency"].append({"threads": t, "features_ms": common.summarize(fe_ms), "model_ms": common.summarize(run_ms),
                               "total_ms": common.summarize(tot_ms), "cpu_ms": common.summarize(cpu_ms)})
    res["peak_rss_mb"] = common.peak_rss_mb()
    common.emit("smart_turn", res)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
