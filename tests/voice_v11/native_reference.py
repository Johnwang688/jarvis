"""V11 reference run: what sherpa-onnx's own KeywordSpotter says about "Jarvis" on V0's synthetic fixtures.

    JARVIS_VOICE_V0=1 ~/.cache/jarvis-voice-v0/venv/bin/python -I tests/voice_v11/native_reference.py

The browser engine (hud/src/lib/wake/) is a port of that spotter, and this file is what it is held to:
it writes `reference.json` (per clip, per threshold: the keywords that fired and when, in audio time),
and `hud/src/lib/wake/parity.test.ts` / `tests/face/hud_v2_wake_check.py` compare the port against it.
Synthetic Kokoro speech only; no recording of a person. Skipped (exit 0) without JARVIS_VOICE_V0=1.
It reuses V0's fixtures and pinned models and imports nothing from `jarvis`.

Keyword sets: `jarvis` (the wake word) and `commands` ("JARVIS PAUSE" etc. as whole phrases, the
under-a-card recognizer's candidate, Q10); thresholds 0.15 / 0.25 / 0.4 at boost 1.0.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "voice_v0"))

import common  # noqa: E402,I001  (pins BLAS threads before numpy loads)
import models  # noqa: E402
import numpy as np  # noqa: E402

CHUNK = 1600  # 100 ms
SETS = {
    "jarvis": ["JARVIS"],
    "commands": ["JARVIS PAUSE", "JARVIS RESUME", "JARVIS SKIP", "JARVIS STOP"],
}
THRESHOLDS = (0.15, 0.25, 0.4)


def kws_dir() -> Path:
    return models.extracted("kws-zipformer-gigaspeech-3.3M")


def spotter(words: list[str], threshold: float, tmp: str, boost: float = 1.0):
    import sentencepiece as spm
    import sherpa_onnx

    d = kws_dir()
    sp = spm.SentencePieceProcessor(model_file=str(d / "bpe.model"))
    kw = f"{tmp}/kw_{abs(hash((tuple(words), threshold)))}.txt"
    with open(kw, "w") as fh:
        for w in words:
            fh.write(f"{' '.join(sp.encode(w, out_type=str))} :{boost} #{threshold} @{w.replace(' ', '_')}\n")
    s = "chunk-16-left-64.int8.onnx"
    return sherpa_onnx.KeywordSpotter(
        tokens=str(d / "tokens.txt"),
        encoder=str(d / f"encoder-epoch-12-avg-2-{s}"), decoder=str(d / f"decoder-epoch-12-avg-2-{s}"),
        joiner=str(d / f"joiner-epoch-12-avg-2-{s}"),
        keywords_file=kw, num_threads=1, keywords_score=boost, keywords_threshold=threshold,
        max_active_paths=4, num_trailing_blanks=1)


def run_clip(sp, x: np.ndarray, tail_s: float = 0.8) -> list[tuple[str, float]]:
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
                hits.append((word.strip().upper().replace("_", " "), round((pos + CHUNK) / common.SR, 3)))
                sp.reset_stream(s)
    return hits


def main() -> int:
    if os.environ.get("JARVIS_VOICE_V0") != "1":
        print("skipped: set JARVIS_VOICE_V0=1 to run the V11 native reference (needs V0 fixtures and models)")
        return 0
    special = json.loads((common.FIXTURES / "special" / "special.json").read_text())
    special = [i for i in special if i["group"].startswith("kws_")]
    clips = [{"file": f"special/{i['file']}", "text": i["text"], "expect": i["expect"], "group": i["group"], "voice": i["voice"]}
             for i in special]
    main_clips = [c for c in common.load_manifest() if c["engine"] == "kokoro"][::3]
    clips += [{"file": c["file"], "text": c["text"], "expect": [], "group": "dictation", "voice": c.get("voice", "")}
              for c in main_clips]
    audio = {c["file"]: common.read_wav(common.FIXTURES / c["file"]) for c in clips}
    out = {"model": "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01 int8", "boost": 1.0, "clips": clips, "runs": {}}
    with tempfile.TemporaryDirectory() as tmp:
        for name, words in SETS.items():
            for thr in THRESHOLDS:
                sp = spotter(words, thr, tmp)
                key = f"{name}@{thr}"
                out["runs"][key] = {c["file"]: run_clip(sp, audio[c["file"]]) for c in clips}
                print(key, sum(len(v) for v in out["runs"][key].values()), "hits", file=sys.stderr)
    dest = common.WORK / "v11"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "reference.json").write_text(json.dumps(out))
    print(f"wrote {dest / 'reference.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
