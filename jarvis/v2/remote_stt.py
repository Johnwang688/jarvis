"""Push to talk from the phone (phone surface design §5 `POST stt`, §6 the
Chats tab, work package P-7).

    POST /m/api/stt     a 16-bit PCM WAV clip in, `{"text": ...}` out

Reached only through `remote_auth.api_route`, **after**
`remote_auth.owner_status` passed (design §4 items 1 to 4: this daemon's
remote listener, the exact Host, a live session of an active passkey, and
for a write the exact Origin, which with the session's SameSite=Strict
cookie is the phone's CSRF rule, P-5's as well). The API and HUD listeners
never serve `/m/` (`daemon._route` refuses it first), so this route is not
theirs; the desk's own dictation stays `POST /stt`, owner-only on the HUD
listener (#227).

**Text in the box, never sent.** The reply is the transcription and nothing
else. This module calls no send, no answer and no approval: what the phone
does with the text is put it in the chat's message box, where Send is still
the owner's tap (P-5). Speech never authorizes (CLAUDE.md, Safety design:
"Speech never authorizes an approval"), and a voice note never answers a
question or a decision (P-7's acceptance): the phone offers the mic only in
a chat's message box, never on an inbox row, a card or a question.

**The desk's path.** The clip goes through `hud_api.dictate`, the function
the desk's `POST /stt` calls (after `hud_api.watch_stt`, as the desk does):
`voice.stt_result(prefer_local=True)` under the owner's switches
(`stt_settings`). **Without speaker verification** (the owner's decision,
2026-10-28): only a passkey-verified phone session can upload, and the
phone's microphone would often refuse the owner's own voice; `verify=False`
reads no voiceprint and records no verdict. The desk keeps it. When the desk is set to local with the cloud fallback
off, the phone's audio never leaves the machine either; when the local
engine cannot take a clip and the fallback is off, the reply says so (the
desk's 409 or 502 and its sentence) and nothing is uploaded. No setting
here, and none on the phone.

**WAV only, built on the phone.** The phone records with Web Audio and
writes the same 16 kHz mono 16-bit PCM WAV the desk's capture writes
(`hud/src/lib/vad.ts` `wavBytes`), not a MediaRecorder file: Safari's
MediaRecorder writes `audio/mp4` (AAC), which the local engine does not take
(`stt_local` decodes PCM WAV only and deliberately runs no media decoder,
such as ffmpeg, on uploaded bytes), and neither does speaker verification.
So the clip must be `Content-Type: audio/wav` (anything else 415), a
RIFF/WAVE PCM file, mono, 16-bit, `RATE_HZ`, with at least one frame and at
most `MAX_S` seconds (anything else 400). The listener reads at most
`BODY_CAP` for this one path, and only once the request already passes the
owner test (`remote_listener._limits`); every other request keeps its
64 KiB cap, so a client without a session can never make it read more.

**Never kept.** The clip lives in memory for the request: nothing here
writes it to disk (the local engine reads it from memory; the cloud chain
posts the bytes), and nothing logs it, its length or its text. An INFO line
names the outcome's status only.

**Limits.** At most `RATE` clips a minute per session (429) and one at a
time per session (429), on top of the local engine's own one-at-a-time
lock. The transcription is cut at `TEXT_CHARS` and passes the phone's
scrub (`remote_api._scrub`) like every string the phone is sent.

SELF_PROTECTED: it decides what the phone's microphone reaches on this
machine. Tests: tests/v2/remote_stt_check.py.
"""
from __future__ import annotations

from collections import deque
import io
import logging
import threading
import time
from urllib.parse import urlsplit
import wave

LOG = logging.getLogger(__name__)

PATH = "/m/api/stt"
MIME = "audio/wav"
# The clip, pinned by tests/v2/remote_stt_check.py: what the phone's
# recorder writes (hud/src/mobile/talk.ts) and nothing else.
RATE_HZ = 16000
MAX_S = 60
HEADER = 44
# The body this one route may carry: a full MAX_S clip, its header and a
# little room. The listener applies it only past the owner test.
BODY_CAP = HEADER + RATE_HZ * 2 * MAX_S + 4096
# The whole body's deadline grows with the size the request declares, so a
# minute's clip (about 1.9 MB) needs about 0.23 Mbit/s, not 0.5: 30 s plus 2 s
# per 100 KB, at most 120 s (`body_deadline`). Only past the owner test.
BODY_DEADLINE_S = 30.0
DEADLINE_PER_100KB_S = 2.0
BODY_DEADLINE_MAX_S = 120.0
# The server's whole budget for one clip (the desk's client sends its own).
BUDGET_S = 30.0
RATE = 12
RATE_S = 60.0
TEXT_CHARS = 20000          # remote_write.MESSAGE_CHARS: the box takes no more
RATE_WORD = "too many clips from this phone; wait a minute"
# Retry-After for a clip refused because another from the session is in flight.
BUSY_RETRY_S = 2
BUSY_WORD = "a clip from this phone is still being transcribed"
TYPE_WORD = "only a 16 kHz mono WAV clip is taken"
BAD_WORD = "the clip is not a 16 kHz mono 16-bit WAV"
EMPTY_WORD = "the clip is empty"
LONG_WORD = f"the clip is longer than {MAX_S} seconds"

clock = time.monotonic


def body_deadline(declared: int | None) -> float:
    """The body deadline for a clip of `declared` bytes (None: chunked, no size
    declared, so the most)."""
    if declared is None:
        return BODY_DEADLINE_MAX_S
    return min(BODY_DEADLINE_MAX_S, BODY_DEADLINE_S + DEADLINE_PER_100KB_S * max(0, declared) / 100_000)


class _Refused(Exception):
    def __init__(self, status: int, word: str):
        super().__init__(word)
        self.status = status
        self.word = word


def check_wav(data: bytes) -> None:
    """The clip is a mono 16-bit PCM WAV at RATE_HZ, with 1 to MAX_S
    seconds of frames, and the file holds them all; `_Refused` otherwise."""
    if not data:
        raise _Refused(400, EMPTY_WORD)
    if len(data) > BODY_CAP:
        raise _Refused(413, "request body too large")
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise _Refused(400, BAD_WORD)
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            shape = (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getcomptype())
            frames = w.getnframes()
            raw = w.readframes(frames)
    except Exception:  # noqa: BLE001 — a malformed header is the client's, never a 500
        raise _Refused(400, BAD_WORD) from None
    if shape != (1, 2, RATE_HZ, "NONE"):
        raise _Refused(400, BAD_WORD)
    if frames == 0:
        raise _Refused(400, EMPTY_WORD)
    if frames > RATE_HZ * MAX_S:
        raise _Refused(400, LONG_WORD)
    if len(raw) != frames * 2:
        raise _Refused(400, BAD_WORD)


class Limits:
    """Each session's clips in the last `RATE_S` (monotonic, at most `RATE`)
    and the sessions with a clip in flight (at most one each)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._seen: dict[str, deque] = {}
        self._busy: set[str] = set()

    def take(self, digest: str) -> str | None:
        """None when this session may send a clip now (and it is marked
        busy until `done`), else the refusal's word."""
        now = clock()
        with self._lock:
            for key in [k for k, q in self._seen.items() if not q or now - q[-1] >= RATE_S]:
                del self._seen[key]
            if digest in self._busy:
                return BUSY_WORD
            seen = self._seen.setdefault(digest, deque())
            while seen and now - seen[0] >= RATE_S:
                seen.popleft()
            if len(seen) >= RATE:
                return RATE_WORD
            seen.append(now)
            self._busy.add(digest)
            return None

    def done(self, digest: str) -> None:
        with self._lock:
            self._busy.discard(digest)


_LIMITS_LOCK = threading.Lock()


def limits_of(server) -> Limits:
    with _LIMITS_LOCK:
        found = getattr(server, "phone_stt_limits", None)
        if not isinstance(found, Limits):
            found = Limits()
            server.phone_stt_limits = found
        return found


def _header(handler, name: str) -> str:
    get_all = getattr(getattr(handler, "headers", None), "get_all", None)
    values = get_all(name) if callable(get_all) else None
    return values[0] if values and len(values) == 1 else ""


def transcribe(daemon, data: bytes) -> str:
    """The desk's dictation (`hud_api.dictate`) for one checked clip: its
    text, or `_Refused` with the desk's status and sentence."""
    from .daemon import APIError
    from .hud_api import dictate, watch_stt
    from .remote_api import _scrub
    watch_stt(daemon)
    try:
        _kind, got = dictate(daemon, data, MIME, budget=BUDGET_S, verify=False)
    except APIError as exc:
        raise _Refused(exc.status if 400 <= exc.status < 600 else 502, _scrub(str(exc))[:300]) from None
    if got is None:                       # too little speech to judge: nothing was transcribed
        return ""
    return _scrub(str(got.text or ""))[:TEXT_CHARS]


def route(handler, path: str, session: dict, body: bytes):
    """(status, payload, extra headers) for `POST /m/api/stt`, or None for
    any other path."""
    if path != PATH:
        return None
    if handler.command != "POST":
        return 405, {"error": "method not allowed"}, [("Allow", "POST")]
    daemon = getattr(handler, "daemon", None)
    if daemon is None:
        return 404, {"error": "not found"}, []
    if urlsplit(getattr(handler, "path", "") or "").query:
        return 400, {"error": "bad request"}, []
    if _header(handler, "Content-Type").split(";", 1)[0].strip().lower() != MIME:
        return 415, {"error": TYPE_WORD}, []
    digest = session.get("token_sha256") or ""
    limits = limits_of(handler.server)
    refused = limits.take(digest)
    if refused is not None:
        wait = BUSY_RETRY_S if refused == BUSY_WORD else int(RATE_S)
        return 429, {"error": refused}, [("Retry-After", str(wait))]
    try:
        check_wav(body)
        text = transcribe(daemon, body)
    except _Refused as exc:
        LOG.info("phone: a dictation clip refused (%d)", exc.status)
        return exc.status, {"error": exc.word}, []
    finally:
        limits.done(digest)
    LOG.info("phone: a dictation clip transcribed")
    return 200, {"text": text}, []
