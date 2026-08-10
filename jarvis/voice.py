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
import re
import threading
import wave

from . import avatars, config, llm

_kokoro = None
_kokoro_lock = threading.Lock()
_local_warned = False
_warned: set[str] = set()


def _warn_once(message: str) -> None:
    """A misconfigured avatar should say so once, not once per sentence —
    the face synthesizes a chunk at a time."""
    if message not in _warned:
        _warned.add(message)
        print(message)


# ---- markdown -> speech ------------------------------------------------------
# The model writes markdown because the HUD renders it. TTS reads the source,
# so "**done**" came out as "asterisk asterisk done asterisk asterisk". These
# strip the syntax and drop what has no spoken form at all (code blocks,
# horizontal rules, table rules). Deliberately lossy in one direction only:
# never invent words, only remove punctuation the owner was never meant to
# hear. `speakable()` is idempotent — plain prose passes through unchanged.

_MD_FENCE = re.compile(r"^\s{0,3}(?:```|~~~)")
_MD_RULE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,}|={3,})\s*$")
_MD_TABLE_RULE = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
_MD_QUOTE = re.compile(r"^\s{0,3}>\s?")
_MD_BULLET = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+")
_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_AUTOLINK = re.compile(r"<(https?://[^>\s]+)>")
_MD_CODE = re.compile(r"`+([^`]+)`+")
_MD_STRONG = re.compile(r"(\*\*|__|~~)(\S(?:.*?\S)?)\1", re.S)
# Single-delimiter emphasis, with the guard that keeps snake_case and
# 2 * 3 * 4 intact: a `_` flanked by word characters is not emphasis.
_MD_EM = re.compile(r"(?<![*\w])\*(\S(?:[^*]*?\S)?)\*(?!\*)|(?<![_\w])_(\S(?:[^_]*?\S)?)_(?!\w)")
_MD_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!~>|])")


def speakable(text: str) -> str:
    """Strip markdown so TTS reads the words, not the syntax.

    Returns "" when nothing is left worth saying (a reply that was only a
    code block) — callers skip synthesis rather than speak punctuation.
    """
    lines: list[str] = []
    in_fence = False
    for line in (text or "").splitlines():
        if _MD_FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or _MD_RULE.match(line):
            continue
        if "|" in line and _MD_TABLE_RULE.match(line):
            continue  # the |---|---| under a table header
        line = _MD_HEADING.sub("", line)
        line = _MD_QUOTE.sub("", line)
        line = _MD_BULLET.sub(r"\1", line)
        if line.strip().startswith("|") or line.count("|") >= 2:
            # A table row reads as a list: "name, 12, ready".
            line = re.sub(r"\s*\|\s*", ", ", line.strip().strip("|")).strip(", ")
        line = _MD_IMAGE.sub(r"\1", line)
        line = _MD_LINK.sub(r"\1", line)
        line = _MD_AUTOLINK.sub(r"\1", line)
        line = _MD_CODE.sub(r"\1", line)
        line = _MD_STRONG.sub(r"\2", line)
        line = _MD_EM.sub(lambda m: m.group(1) or m.group(2), line)
        line = _MD_ESCAPE.sub(r"\1", line)
        lines.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# ---- which voice -------------------------------------------------------------
# An avatar changes the name, the wake phrase and the face; the voice is the
# fourth thing, and the one the owner notices first — an avatar that renames
# the window and then answers in the previous voice is the same kind of
# mismatch as the window and the server disagreeing about who he is.
#
# Resolution lives here rather than at the call sites for exactly the reason
# speakable() does: three surfaces synthesize speech today (the face, /say,
# Discord voice notes) and a fourth will, and none of them should have to
# remember. It is read per call, so switching avatars mid-conversation moves
# the voice on the next sentence with no restart.

_voices: set[str] | None = None

# Kokoro tags each voice with the language it was trained on, in the first
# letter of its name. The old rule — "b" is British, everything else is
# American — was right for the two English families and silently wrong for
# the other thirty voices, which would have been synthesized as US English.
_LANGS = {
    "a": "en-us", "b": "en-gb", "e": "es", "f": "fr-fr", "h": "hi",
    "i": "it", "j": "ja", "p": "pt-br", "z": "cmn",
}


def available_voices() -> set[str]:
    """Every voice in the local Kokoro bundle; empty if it is not installed.

    Cached — this is consulted on every synthesized chunk.
    """
    global _voices
    if _voices is None:
        try:
            import numpy as np

            with np.load(str(config.KOKORO_VOICES)) as bundle:
                _voices = set(bundle.files)
        except Exception:
            _voices = set()
    return _voices


def voice_for(av: "avatars.Avatar | None" = None) -> str:
    """The voice to speak in: the active avatar's, or the configured default.

    An avatar's voice is honored only when it is a name this machine can
    actually synthesize. A typo in an avatar.json has to leave him *audible*
    — the same degradation rule that keeps a broken wake regex from leaving
    him unsummonable. It also means the cloud fallback is safe: the name was
    validated against the bundle for the model both paths run.
    """
    av = av or avatars.active()
    wanted = (av.voice or "").strip()
    if not wanted:
        return config.TTS_VOICE
    if wanted in available_voices():
        return wanted
    _warn_once(
        f"[voice] avatar {av.slug!r} asks for voice {wanted!r}, which is not "
        f"installed; speaking as {config.TTS_VOICE}"
    )
    return config.TTS_VOICE


def _local_tts(text: str, voice: str, speed: float) -> bytes:
    """Kokoro on this machine. Raises if deps/models are missing."""
    global _kokoro
    with _kokoro_lock:  # one model instance; create() is not thread-safe
        if _kokoro is None:
            from kokoro_onnx import Kokoro

            _kokoro = Kokoro(str(config.KOKORO_MODEL), str(config.KOKORO_VOICES))
        lang = _LANGS.get(voice[:1], "en-us")
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
    # Applied here, not only at the call sites, so no speech path can forget
    # it. Idempotent, so a caller that already stripped pays nothing.
    text = speakable(text)
    if not text.strip():
        raise ValueError("nothing to say")
    # Who is speaking, unless the caller named a voice explicitly (a probe or
    # a bench pinning one). The avatar is read fresh per call — a switch lands
    # on the next sentence.
    av = avatars.active()
    # The cloud provider silently truncates audio above ~1.3x (verified
    # 2026-07-30); local Kokoro is fine with it, but one consistent pace
    # beats a backend-dependent one. The clamp is the last word, so an avatar
    # cannot ask for a pace that gets its own speech cut off.
    speed = min(max(speed or av.speed or config.TTS_SPEED, 0.5), 1.3)
    voice = voice or voice_for(av)

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
