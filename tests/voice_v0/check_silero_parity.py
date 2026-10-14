"""Does the browser Silero (onnxruntime-web WASM) agree with native onnxruntime?

    JARVIS_VOICE_V0=1 python tests/voice_v0/check_silero_parity.py

Re-scores the "clean" condition of probe_browser.py's vadquality experiment (same 36 clips, same
energy labels) with native onnxruntime and compares precision, recall and false-accept frames with
the numbers the browser run stored in results/browser_vadquality.json. Run `probe_browser.py --only
vadquality` first. Exit 1 if they differ by more than 0.5 points.
"""

from __future__ import annotations

import json
import sys

import common  # noqa: F401,I001  (first: pins BLAS threads before numpy loads)
import numpy as np
import models


def main() -> int:
    if not common.gated("compare browser and native Silero"):
        return 0
    import onnxruntime as ort

    sess = ort.InferenceSession(str(models.model_file("silero-vad-v5.1.2")), providers=["CPUExecutionProvider"])

    def probs(x: np.ndarray) -> np.ndarray:
        state, ctx, out = np.zeros((2, 1, 128), np.float32), np.zeros(64, np.float32), []
        for i in range(len(x) // 512):
            fr = x[i * 512:(i + 1) * 512]
            p, state = sess.run(None, {"input": np.concatenate([ctx, fr])[None].astype(np.float32), "state": state,
                                       "sr": np.array(common.SR, np.int64)})
            ctx = fr[-64:]
            out.append(float(p[0][0]))
        return np.array(out)

    stored = json.loads((common.WORK / "results" / "browser_vadquality.json").read_text())["vad_quality"]
    n_clips = stored["clips"]
    clips = [c for c in common.load_manifest() if c["engine"] == "kokoro"]
    picked = [clips[(i * 9) % len(clips)] for i in range(n_clips)]     # same selection as vad_quality()
    gap = np.zeros(int(0.8 * common.SR), np.float32)
    clean = np.concatenate([np.concatenate([common.read_wav(common.FIXTURES / c["file"]), gap]) for c in picked])
    n = len(clean) // 512
    en = np.array([common.rms_db(clean[i * 512:(i + 1) * 512]) for i in range(n)])
    truth = np.convolve((en > -50).astype(int), np.ones(7, int), mode="same") > 0
    pred = probs(clean)[:n] > 0.5
    tp, fp, fn = int((pred & truth).sum()), int((pred & ~truth).sum()), int((~pred & truth).sum())
    native = {"precision": tp / max(tp + fp, 1), "recall": tp / max(tp + fn, 1), "false_accept_frames": fp}
    browser = stored["conditions"]["clean"]["silero"]
    print("native :", native)
    print("browser:", {k: browser[k] for k in native})
    ok = abs(native["precision"] - browser["precision"]) < 0.005 and abs(native["recall"] - browser["recall"]) < 0.005
    print("parity:", "ok" if ok else "MISMATCH")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
