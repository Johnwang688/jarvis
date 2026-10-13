"""Offline self-check for the V0 scoring helpers (WER, EER, SNR mixing).

Needs numpy only; does not download anything. The probes themselves live in
tests/voice_v0/ and are gated behind JARVIS_VOICE_V0=1 (see its README text in
docs/notes/voice-face.md).
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent / "voice_v0"))
import common  # noqa: E402


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")


def main() -> int:
    r = common.wer(["the quick brown fox"], ["the quick brown fox"])
    check(r["wer"] == 0, "identical text is 0 WER")
    r = common.wer(["the quick brown fox"], ["the slow brown"])
    check((r["sub"], r["del"], r["ins"]) == (1, 1, 0) and abs(r["wer"] - 0.5) < 1e-9, f"sub+del: {r}")
    r = common.wer(["a b"], ["a x b y"])
    check((r["sub"], r["del"], r["ins"]) == (0, 0, 2), f"insertions: {r}")
    check(common.norm_words("Jarvis, don't -- stop!") == ["jarvis", "don't", "stop"], "normalisation")
    rng = np.random.default_rng(0)
    e, _ = common.eer(rng.normal(2, 1, 4000), rng.normal(0, 1, 4000))
    check(0.12 < e < 0.20, f"EER of two unit gaussians 2 apart is ~0.159, got {e}")
    e, _ = common.eer(np.array([0.9, 0.8]), np.array([0.1, 0.2]))
    check(e == 0.0, "separable scores give 0 EER")
    s = rng.normal(0, 0.1, 16000).astype(np.float32)
    mixed = common.mix_at_snr(s, rng.normal(0, 1, 4000).astype(np.float32), 10.0)
    snr = common.rms_db(s) - common.rms_db(mixed - s)
    check(abs(snr - 10) < 0.5, f"mix_at_snr hits target SNR, got {snr:.2f}")
    print("voice_v0 helpers: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
