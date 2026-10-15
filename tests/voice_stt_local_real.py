"""One real-model smoke test of local STT (V13). Gated: skipped unless JARVIS_STT_LOCAL_REAL=1.

Runs the product engine (`jarvis/stt_local.py`) with the real Parakeet model on ONE synthetic Kokoro
clip from V0's fixtures: the text matches, a second decode is warm and fast, a 48 kHz stereo copy still
decodes, and unloading hands the memory back. Needs the V0 probe venv (sherpa-onnx) and the pinned
model; the normal sweep, which has neither, skips it.

    systemd-run --user --scope -q -p MemoryMax=4G -p MemorySwapMax=0 -- \\
      env JARVIS_STT_LOCAL_REAL=1 PYTHONPATH=<worktree> \\
      ~/.cache/jarvis-voice-v0/venv/bin/python tests/voice_stt_local_real.py

(4G, not 3G: the model's peak while loading is 1.9 GB.) Fixtures come from
JARVIS_VOICE_V0_DIR (default ~/.cache/jarvis-voice-v0); the model from JARVIS_STT_LOCAL_MODEL, else
the product default, else V0's pinned copy. It never downloads, calls the network or reads `.env`.
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
import wave
from pathlib import Path

if os.environ.get("JARVIS_STT_LOCAL_REAL") != "1":
    print("skipped: set JARVIS_STT_LOCAL_REAL=1 to run the real-model local STT smoke test")
    raise SystemExit(0)

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import stt_compare_probe as probe  # noqa: E402  (reuses its stub-config loader: nothing reads .env)

common = probe.common


def rss_mb() -> float:
    return int(open("/proc/self/statm").read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6


def main() -> int:
    failures = []

    def check(name, ok, detail=""):
        print(("ok   " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))
        if not ok:
            failures.append(name)

    model_dir = probe.find_model_dir(None)
    if model_dir is None:
        print("FAIL no local model (python -m jarvis.stt_local fetch, or JARVIS_STT_LOCAL_MODEL)")
        return 1
    stt_local = probe.load_stt_local(model_dir)
    manifest = json.loads((common.FIXTURES / "manifest.json").read_text())
    clip = next(c for c in manifest if c["engine"] == "kokoro")
    wav = (common.FIXTURES / clip["file"]).read_bytes()
    seconds = float(clip["seconds"])

    engine = stt_local.Engine(threads=2, schedule=False)
    base = rss_mb()
    t0 = time.perf_counter()
    text = engine.transcribe(wav, "audio/wav", timeout=60)
    cold = time.perf_counter() - t0
    loaded = rss_mb()
    result = common.wer([clip["text"]], [text])
    check("transcribes the clip", result["wer"] <= 0.1, f"wer {result['wer']:.3f}: {text!r}")
    check("the model is loaded lazily, with 2 threads", engine.loaded and engine.threads == 2 and engine.loads == 1)
    check("loading costs about a gigabyte", 500 < loaded - base < 2200, f"{loaded - base:.0f} MB")

    t0 = time.perf_counter()
    again = engine.transcribe(wav, "audio/wav", timeout=60)
    warm = time.perf_counter() - t0
    check("a warm decode is fast (RTF well under 0.3)", warm / seconds < 0.3, f"RTF {warm / seconds:.3f}, cold {cold:.1f} s")
    check("and identical", again == text)

    # 48 kHz stereo: the engine mixes to mono and hands sherpa the native rate to resample.
    with wave.open(io.BytesIO(wav)) as w:
        import numpy as np

        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    up = np.repeat(pcm, 3)
    stereo = np.stack([up, up], axis=1).reshape(-1)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2), w.setsampwidth(2), w.setframerate(48000)
        w.writeframes(stereo.astype("<i2").tobytes())
    other = engine.transcribe(buf.getvalue(), "audio/wav", timeout=60)
    check("a 48 kHz stereo copy still decodes", common.wer([clip["text"]], [other])["wer"] <= 0.25, repr(other))

    try:
        engine.transcribe(wav, "audio/wav", timeout=0.0)
        check("zero budget raises", False)
    except stt_local.LocalSTTTimeout:
        check("zero budget raises LocalSTTTimeout", True)

    unloaded = engine.unload()
    time.sleep(0.2)
    after = rss_mb()
    check("unload releases the model", unloaded and not engine.loaded)
    check("and hands the memory back", loaded - after > 500, f"{loaded:.0f} -> {after:.0f} MB")
    engine.transcribe(wav, "audio/wav", timeout=60)
    check("it reloads on demand", engine.loads == 2)
    print(f"cold {cold:.2f} s, warm {warm:.3f} s for {seconds:.1f} s of audio; RSS {base:.0f} -> {loaded:.0f} -> {after:.0f} MB")
    engine.unload()
    print(f"{'FAILED' if failures else 'all passed'}: {len(failures)} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
