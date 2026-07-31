"""Jarvis's voice: speech behind swappable contracts.

`tts(text) -> audio bytes` and `stt(audio) -> text` are the whole interface —
the same pattern as `memory_search`: callers depend on the contract, so the
backend can change without touching anything else. That paid off 2026-07-31:
after a day of intermittent multi-second stalls somewhere in the cloud TTS
path, the default backend became the same Kokoro model running locally on
CPU (kokoro-onnx) — same bm_george voice, no network in the speech path.
Local output is WAV, cloud is MP3; every consumer decodes by sniffing, not
by trusting a label.
"""

from __future__ import annotations

import io
import threading
import wave

from . import config, llm

_kokoro = None
_kokoro_lock = threading.Lock()
_local_warned = False


def _local_tts(text: str, voice: str, speed: float) -> bytes:
    """Kokoro on this machine. Raises if deps/models are missing."""
    global _kokoro
    with _kokoro_lock:  # one model instance; create() is not thread-safe
        if _kokoro is None:
            from kokoro_onnx import Kokoro

            _kokoro = Kokoro(str(config.KOKORO_MODEL), str(config.KOKORO_VOICES))
        lang = "en-gb" if voice.startswith("b") else "en-us"
        samples, sample_rate = _kokoro.create(text, voice=voice, speed=speed, lang=lang)

    pcm = (samples.clip(-1.0, 1.0) * 32767).astype("<i2").tobytes()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


def tts(
    text: str,
    voice: str | None = None,
    instructions: str | None = None,
    speed: float | None = None,
) -> bytes:
    """Synthesize speech for `text`. Returns audio bytes (WAV local, MP3 cloud)."""
    global _local_warned
    if not text.strip():
        raise ValueError("nothing to say")
    # The cloud provider silently truncates audio above ~1.3x (verified
    # 2026-07-30); local Kokoro is fine with it, but one consistent pace
    # beats a backend-dependent one.
    speed = min(max(speed or config.TTS_SPEED, 0.5), 1.3)
    voice = voice or config.TTS_VOICE

    if config.TTS_BACKEND == "local":
        try:
            return _local_tts(text, voice, speed)
        except Exception as exc:
            if not _local_warned:
                _local_warned = True
                print(f"[voice] local tts unavailable ({exc}); using OpenRouter")

    return llm.speech(
        text=text,
        model=config.TTS_MODEL,
        voice=voice,
        instructions=config.TTS_INSTRUCTIONS if instructions is None else instructions,
        speed=speed,
        provider=config.TTS_PROVIDER or None,
    )


def stt(audio: bytes, mime: str = "audio/webm") -> str:
    """Transcribe spoken audio to text."""
    if not audio:
        raise ValueError("no audio")
    ext = mime.rsplit("/", 1)[-1].split(";")[0] or "webm"
    return llm.transcribe(
        audio, model=config.STT_MODEL, filename=f"audio.{ext}", mime=mime
    )
