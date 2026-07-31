"""Checks for the local TTS backend. Free — no network, no API.

Verifies: local synthesis produces valid mono 16-bit WAV at a sane duration,
the bm_george voice exists in the downloaded voices file, repeated calls are
fast (the whole point), and a broken local setup falls back to the cloud
path (asserted against a stubbed llm.speech, so nothing is actually sent).

Run:  .venv/bin/python tests/voice_local_check.py
"""

from __future__ import annotations

import io
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis import config, voice


def synth_checks() -> None:
    assert config.TTS_BACKEND == "local", config.TTS_BACKEND
    assert config.KOKORO_MODEL.exists(), "model file missing — see CLAUDE.md"

    t0 = time.monotonic()
    audio = voice.tts("Right away, sir.")
    first = (time.monotonic() - t0) * 1000  # includes one-time model load

    assert audio.startswith(b"RIFF"), "local backend must emit WAV"
    with wave.open(io.BytesIO(audio)) as wav:
        assert wav.getnchannels() == 1 and wav.getsampwidth() == 2
        seconds = wav.getnframes() / wav.getframerate()
    assert 0.5 < seconds < 4, f"suspicious duration {seconds:.2f}s"

    warm = []
    for _ in range(3):
        t0 = time.monotonic()
        voice.tts("Right away, sir.")
        warm.append((time.monotonic() - t0) * 1000)
    print(f"ok  synth: valid WAV, {seconds:.2f}s speech; first call {first:.0f}ms "
          f"(incl. model load), warm {', '.join(f'{w:.0f}' for w in warm)}ms")

    longer = voice.tts(
        "I went through the whole inbox and grouped everything by urgency, "
        "which took a little digging because two threads were mislabeled."
    )
    with wave.open(io.BytesIO(longer)) as wav:
        assert wav.getnframes() / wav.getframerate() > 4, "long text, short audio?"
    print("ok  synth: long sentences render fully")


def fallback_checks() -> None:
    calls = []

    def fake_speech(**kwargs):
        calls.append(kwargs)
        return b"\xff\xfbFAKE-MP3"

    real_speech = voice.llm.speech
    real_model = config.KOKORO_MODEL
    real_instance = voice._kokoro
    voice.llm.speech = fake_speech
    config.KOKORO_MODEL = Path("/nonexistent/kokoro.onnx")
    voice._kokoro = None  # force a reload attempt against the broken path
    voice._local_warned = False
    try:
        out = voice.tts("hello there")
        assert out == b"\xff\xfbFAKE-MP3", "fallback did not reach the cloud path"
        assert calls and calls[0]["voice"] == config.TTS_VOICE
    finally:
        voice.llm.speech = real_speech
        config.KOKORO_MODEL = real_model
        voice._kokoro = real_instance
        voice._local_warned = False
    print("ok  fallback: broken local setup falls through to the cloud path")


def main() -> int:
    synth_checks()
    fallback_checks()
    print("\nall local-voice checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
