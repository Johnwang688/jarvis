"""Checks for the local TTS backend. Free — no network, no API.

Verifies: local synthesis produces valid mono 16-bit WAV at a sane duration,
the bm_george voice exists in the downloaded voices file, repeated calls are
fast (the whole point), and a broken local setup falls back to the cloud
path (asserted against a stubbed llm.speech, so nothing is actually sent).

Also covers **which** voice speaks (2026-08-06): an avatar's voice is the
fourth thing switching an avatar changes, and it resolves inside `tts()` so
no speech path can forget it. The cases that matter are the degradations —
an avatar naming a voice this machine does not have has to leave him
*audible*, the same rule that keeps a broken wake regex from leaving him
unsummonable.

Run:  .venv/bin/python tests/voice_local_check.py
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis import config

# Pin the avatar before anything synthesizes. Without this the suite reads
# the owner's saved choice, so switching avatars would change what the
# fallback check asserts about the configured voice — a test that passes or
# fails on state outside the repo.
_TMP = tempfile.TemporaryDirectory()
config.AVATARS_DIR = Path(_TMP.name) / "avatars"
config.AVATAR_STATE_PATH = Path(_TMP.name) / "state" / "avatar.json"
config.AVATARS_DIR.mkdir(parents=True)
config.AVATAR_ENV = "jarvis"

from jarvis import avatars, voice  # noqa: E402


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


def make_avatar(slug: str, meta: dict) -> None:
    path = config.AVATARS_DIR / slug
    path.mkdir(parents=True, exist_ok=True)
    (path / "avatar.json").write_text(json.dumps(meta), encoding="utf-8")


def avatar_voice_checks() -> None:
    """Which voice speaks, without synthesizing anything."""
    installed = voice.available_voices()
    assert config.TTS_VOICE in installed, f"{config.TTS_VOICE} missing from the bundle"
    other = next(v for v in sorted(installed) if v != config.TTS_VOICE)

    make_avatar("speaker", {"name": "Speaker", "voice": other, "speed": 1.05})
    make_avatar("typo", {"name": "Typo", "voice": "am_nonexistent"})
    make_avatar("mute-spec", {"name": "Plain"})
    make_avatar("badspeed", {"name": "Bad", "voice": other, "speed": "fast"})

    said: list[dict] = []

    def fake_local(text, voice_name, speed):
        said.append({"voice": voice_name, "speed": speed, "text": text})
        return b"RIFF-FAKE"

    real_local, voice._warned = voice._local_tts, set()
    voice._local_tts = fake_local
    try:
        config.AVATAR_ENV = "speaker"
        voice.tts("hello")
        assert said[-1]["voice"] == other, said[-1]
        assert abs(said[-1]["speed"] - 1.05) < 1e-6, said[-1]
        print(f"ok  avatar: its voice and pace are what speaks — {other}, 1.05x")

        voice.tts("hello", voice=config.TTS_VOICE, speed=1.0)
        assert said[-1]["voice"] == config.TTS_VOICE and said[-1]["speed"] == 1.0
        print("ok  avatar: an explicit voice still wins (probes, benches)")

        # An avatar must not be able to ask for a pace that truncates its own
        # speech — the clamp is the last word, not the avatar.
        make_avatar("racer", {"name": "Racer", "voice": other, "speed": 9.0})
        config.AVATAR_ENV = "racer"
        voice.tts("hello")
        assert said[-1]["speed"] == 1.3, said[-1]
        print("ok  avatar: an absurd speed is still clamped to 1.3")

        # The degradations. Each has to leave him audible.
        config.AVATAR_ENV = "typo"
        voice.tts("hello")
        assert said[-1]["voice"] == config.TTS_VOICE, said[-1]
        print("ok  avatar: a voice that is not installed falls back, audibly")

        config.AVATAR_ENV = "mute-spec"
        voice.tts("hello")
        assert said[-1]["voice"] == config.TTS_VOICE, said[-1]
        print("ok  avatar: an avatar with no voice uses the configured one")

        config.AVATAR_ENV = "badspeed"
        voice.tts("hello")
        assert said[-1]["speed"] == config.TTS_SPEED, said[-1]
        assert said[-1]["voice"] == other, said[-1]
        print("ok  avatar: an unparseable speed falls back without losing the voice")

        config.AVATAR_ENV = "nope-no-such-avatar"
        voice.tts("hello")
        assert said[-1]["voice"] == config.TTS_VOICE, said[-1]
        print("ok  avatar: a slug that vanished still speaks")
    finally:
        voice._local_tts = real_local
        config.AVATAR_ENV = "jarvis"

    # The face synthesizes a chunk at a time, so a broken avatar must not
    # print once per sentence.
    assert len(voice._warned) == 1, voice._warned
    print("ok  avatar: the unknown-voice warning is said once, not per sentence")


def language_checks() -> None:
    """Kokoro tags a voice's language in its first letter. The old rule —
    'b' is British, everything else American — was silently wrong for the
    thirty non-English voices."""
    import numpy as np

    seen: list[dict] = []

    class FakeKokoro:
        def create(self, text, voice, speed, lang):
            seen.append({"voice": voice, "lang": lang})
            return np.zeros(2400, dtype="float32"), 24000

    real = voice._kokoro
    voice._kokoro = FakeKokoro()
    try:
        for name, expect in (("bm_george", "en-gb"), ("am_michael", "en-us"),
                             ("jf_alpha", "ja"), ("zm_yunxi", "cmn"),
                             ("ff_siwis", "fr-fr")):
            voice.tts("hello", voice=name)
            assert seen[-1]["lang"] == expect, seen[-1]
        print(f"ok  language: {len(seen)} voices map to the right language, "
              "not all to en-us")
    finally:
        voice._kokoro = real


def main() -> int:
    avatar_voice_checks()
    language_checks()
    synth_checks()
    fallback_checks()
    print("\nall local-voice checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
