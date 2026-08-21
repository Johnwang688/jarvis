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
_voice_override: str | None = None

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


def selected_voice() -> str:
    """Return the voice selected in the HUD, or the normal resolved voice."""
    return _voice_override or voice_for()


def set_voice(name: str) -> str:
    """Select an installed voice for subsequent speech; "" clears the override.

    Accepts any name in `catalog()` — a Kokoro bundle voice or a `pocket:`
    builtin/clone/hybrid. Clearing returns to the avatar/config resolution,
    which is the picker's "follow the avatar" affordance.
    """
    global _voice_override
    name = name.strip()
    if not name:
        _voice_override = None
        return selected_voice()
    if not _known(name):
        raise LookupError(f"voice {name!r} is not installed")
    _voice_override = name
    return name


def voice_for(av: "avatars.Avatar | None" = None) -> str:
    """The voice to speak in: the active avatar's, or the configured default.

    An avatar's voice is honored only when it is a name this machine can
    actually synthesize. A typo in an avatar.json has to leave him *audible*
    — the same degradation rule that keeps a broken wake regex from leaving
    him unsummonable. It also means the cloud fallback is safe: a Kokoro
    name was validated against the bundle for the model both paths run, and
    a `pocket:` name is routed locally by tts() and never sent to the cloud.
    """
    av = av or avatars.active()
    wanted = (av.voice or "").strip()
    if not wanted:
        return config.TTS_VOICE
    if _known(wanted):
        return wanted
    _warn_once(
        f"[voice] avatar {av.slug!r} asks for voice {wanted!r}, which is not "
        f"installed; speaking as {config.TTS_VOICE}"
    )
    return config.TTS_VOICE


def catalog() -> list[dict]:
    """Every voice this machine can speak: [{name, backend, kind}].

    The Kokoro bundle, plus — when pocket-tts is importable — the Pocket TTS
    builtins and the custom clone/hybrid store (`config.VOICES_DIR`, built by
    `jarvis voice clone|mix`). A name in here is a name that can at least be
    attempted locally; the routing in `tts()` guarantees the cloud fallback
    never sees a `pocket:` name.
    """
    entries = [
        {"name": name, "backend": "kokoro", "kind": "builtin"}
        for name in sorted(available_voices())
    ]
    from . import pocket

    if pocket.available():
        entries += [
            {"name": f"pocket:{name}", "backend": "pocket", "kind": "builtin"}
            for name in pocket.BUILTINS
        ]
        entries += [
            {"name": f"pocket:{v['name']}", "backend": "pocket", "kind": v["kind"]}
            for v in pocket.custom_voices()
        ]
    return entries


def _known(name: str) -> bool:
    """Can this machine at least attempt to speak as `name`?"""
    if name.startswith("pocket:"):
        from . import pocket

        return pocket.available() and pocket.known(name.removeprefix("pocket:"))
    return name in available_voices()


# ---- chunk boundaries --------------------------------------------------------
# A streamed reply is several chunks scheduled back-to-back on the browser's
# audio clock, so whatever silence Kokoro pads each chunk with *becomes* the
# pause the owner hears between them. Measured 2026-08-18 on a four-chunk
# reply: ~30ms of lead-in and ~85-105ms of tail per chunk, half a second in
# total, and — the part that actually sounds wrong — the same gap whether the
# boundary falls between two sentences or in the middle of one.
#
# `_sentences()` splits a long opener at a clause, or failing that at a word,
# so a mid-phrase boundary is routine. Padding it like a full stop is heard as
# a stumble. So the pad is trimmed off and put back deliberately, sized by what
# the chunk actually ends on: a full stop gets a real pause, a comma gets half
# of one, and a bare word cut gets almost nothing because the sentence is still
# running.

_TAIL_MS = {"sentence": 130, "clause": 70, "mid": 15}
_LEAD_MS = 10


def _boundary(text: str) -> str:
    """What kind of pause the end of `text` has earned."""
    end = text.rstrip().rstrip('")]\'')[-1:]
    if end in ".!?…":
        return "sentence"
    if end in ",;:—-":
        return "clause"
    return "mid"


def _repad(samples, sample_rate: int, kind: str):
    """Trim Kokoro's own padding off both ends, then pad the tail on purpose.

    A margin is kept at the front so a plosive's attack is not clipped, and a
    chunk that never rises above the floor comes back untouched — a quiet
    chunk is recoverable, an empty one is a dropped sentence.
    """
    import numpy as np

    loud = np.flatnonzero(np.abs(samples) > 0.01)
    if loud.size == 0:
        return samples
    lead = int(sample_rate * _LEAD_MS / 1000)
    start = max(0, int(loud[0]) - lead)
    end = min(len(samples), int(loud[-1]) + 1 + lead)
    tail = np.zeros(int(sample_rate * _TAIL_MS[kind] / 1000), dtype=samples.dtype)
    return np.concatenate([samples[start:end], tail])


def _encode_wav(samples, sample_rate: int) -> bytes:
    """Float samples in [-1, 1] -> 16-bit mono WAV. Both local backends end here."""
    pcm = (samples.clip(-1.0, 1.0) * 32767).astype("<i2").tobytes()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


def _local_tts(text: str, voice: str, speed: float) -> bytes:
    """Kokoro on this machine. Raises if deps/models are missing."""
    global _kokoro
    with _kokoro_lock:  # one model instance; create() is not thread-safe
        if _kokoro is None:
            from kokoro_onnx import Kokoro

            _kokoro = Kokoro(str(config.KOKORO_MODEL), str(config.KOKORO_VOICES))
        lang = _LANGS.get(voice[:1], "en-us")
        samples, sample_rate = _kokoro.create(text, voice=voice, speed=speed, lang=lang)

    samples = _repad(samples, sample_rate, _boundary(text))
    return _encode_wav(samples, sample_rate)


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
    voice = voice or _voice_override or voice_for(av)

    if voice.startswith("pocket:"):
        # The second local backend (Kyutai Pocket TTS, jarvis/pocket.py):
        # its builtins and the owner's clones/hybrids live under the
        # `pocket:` prefix so bare names stay Kokoro's. Pocket has no speed
        # control, so the clamp's answer simply is not sent — said once,
        # because the pace change from the configured Kokoro speed is audible.
        if speed != 1.0:
            _warn_once(f"[voice] pocket voices have no speed control; ignoring {speed}x")
        try:
            from . import pocket

            return pocket.synthesize(text, voice.removeprefix("pocket:"))
        except Exception as exc:
            # Degrade to a Kokoro name and fall into the existing local->cloud
            # chain below: every failure leaves him audible, and the cloud
            # never receives a name only this backend understands. The guard
            # keeps a JARVIS_TTS_VOICE=pocket:x from looping straight back in.
            fallback = config.TTS_VOICE
            if fallback.startswith("pocket:"):
                fallback = "bm_george"
            _warn_once(
                f"[voice] pocket voice {voice!r} unavailable ({exc}); "
                f"speaking as {fallback}"
            )
            voice = fallback

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
