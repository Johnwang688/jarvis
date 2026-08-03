"""Discord Gateway listener: real-time replies when the owner @mentions the bot.

A single thread holds a websocket to Discord's Gateway and turns
MESSAGE_CREATE events into agent turns. The response rule is deliberately
strict — see should_respond(): only the OWNER triggers a turn, and only by
mentioning the bot (or DMing it). Anyone else in a server can type
"@jarvis do X" and nothing happens: Discord text is untrusted content, and
handing strangers an agent trigger would hand them the agent.

The reply itself posts without the approval gate, by the same rationale as
discord_dm_owner: it answers the owner, in the place the owner asked. Any
*tools* the turn uses still carry their own gates — a dangerous tool while
the owner is away from the HUD simply gets denied, and Jarvis says so in
the reply.

Voice notes (2026-08-03): a Discord voice message from the owner — the
IS_VOICE_MESSAGE flag plus one audio/ogg attachment — is transcribed with
voice.stt() (parakeet takes ogg/opus as-is, probed at 1.000 similarity) and
the reply comes back as text *plus* synthesized speech attached to the same
message. The transcript is handed to run_turn with spoken=True so the server
keeps it away from the approval parser: a transcription is one mishearing
away from "yes", so only typed replies can resolve an authorization. Every
failure on the voice path (too large, download, STT) becomes a text reply,
and a TTS failure degrades to text-only — never a silent drop.

Protocol notes (v10): HELLO (op 10) -> IDENTIFY (op 2) -> READY; heartbeat
(op 1) every heartbeat_interval ms and immediately when the server asks;
reconnect with a fresh IDENTIFY on any drop (resume is more protocol than
this needs at one-owner scale).
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
# guild messages + message content + DMs
INTENTS = (1 << 9) | (1 << 15) | (1 << 12)
MAX_DISCORD_CHARS = 2000
VOICE_MESSAGE_FLAG = 1 << 13  # IS_VOICE_MESSAGE on the message
MAX_VOICE_BYTES = 8 * 1024 * 1024  # refuse absurd attachments before download


class GatewayClosed(RuntimeError):
    """Discord closed the socket; code explains why (4014 = intents off)."""

    def __init__(self, code: int, reason: str):
        self.code = code
        super().__init__(f"gateway closed: {code} {reason or '(no reason)'}")


def _recv_json(ws) -> dict[str, Any]:
    """One frame as JSON; a close frame becomes a GatewayClosed with its code."""
    import struct

    opcode, data = ws.recv_data()
    if opcode == 8:  # close frame: 2-byte code + utf-8 reason
        code = struct.unpack(">H", data[:2])[0] if len(data) >= 2 else 0
        raise GatewayClosed(code, data[2:].decode("utf-8", errors="replace"))
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    return json.loads(data)


def voice_attachment(message: dict[str, Any]) -> dict[str, Any] | None:
    """The voice-note attachment on a message, or None.

    A voice message is the IS_VOICE_MESSAGE flag plus one audio attachment
    carrying waveform metadata; either signal qualifies an audio attachment.
    A music file dragged into the chat has neither, so it is never fed to
    STT as if someone had spoken it.
    """
    flagged = bool(int(message.get("flags") or 0) & VOICE_MESSAGE_FLAG)
    for attachment in message.get("attachments") or []:
        mime = (attachment.get("content_type") or "").split(";")[0]
        if mime.startswith("audio/") and (flagged or "waveform" in attachment):
            return attachment
    return None


def should_respond(message: dict[str, Any], bot_id: str, owner_id: str) -> bool:
    """Only the owner, only by mention or DM, never bots, never empty.

    A voice note counts as content. In practice that means DMs only: a
    voice message cannot carry an @mention, so a guild voice note never
    qualifies — which keeps the trigger rule exactly as strict as text.
    """
    author = message.get("author") or {}
    if author.get("bot") or author.get("id") != owner_id:
        return False
    is_dm = "guild_id" not in message
    mentioned = any(m.get("id") == bot_id for m in message.get("mentions") or [])
    if not (is_dm or mentioned):
        return False
    if voice_attachment(message) is not None:
        return True
    return bool(strip_mention(message.get("content") or "", bot_id).strip())


def strip_mention(content: str, bot_id: str) -> str:
    """Remove the bot's own mention so the agent sees the actual request."""
    for form in (f"<@{bot_id}>", f"<@!{bot_id}>"):
        content = content.replace(form, "")
    return content.strip()


def _download(url: str) -> bytes:
    """Fetch an attachment from Discord's CDN. Module-level so tests stub it."""
    import httpx

    response = httpx.get(url, timeout=30, follow_redirects=True)
    response.raise_for_status()
    return response.content


def _audio_filename(audio: bytes) -> tuple[str, str]:
    """Name reply audio by what it is — local TTS emits WAV, cloud MP3, and
    voice.py's contract is sniff, never trust a label."""
    if audio[:4] == b"RIFF":
        return "jarvis-reply.wav", "audio/wav"
    return "jarvis-reply.mp3", "audio/mpeg"


class GatewayListener:
    """Owns the websocket; hands qualifying messages to `run_turn`.

    run_turn(text, channel_id, spoken) -> reply text. spoken is True when
    the text came out of STT rather than the owner's keyboard — the server
    uses it to keep transcriptions away from the approval parser. It is
    called on a worker thread so a long agent turn never stalls heartbeats.
    """

    def __init__(
        self,
        run_turn: Callable[[str, str, bool], str],
        announce: Callable[[str], None] = print,
    ):
        self.run_turn = run_turn
        self.announce = announce
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.bot_id = ""
        self.owner_id = ""

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        from .tools.discord import _load_bundle

        bundle = _load_bundle()  # raises if not connected — caller guards
        self.owner_id = str(bundle.get("owner_id", ""))
        self._token = bundle["bot_token"]
        self._thread = threading.Thread(
            target=self._serve, name="discord-gateway", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- the socket loop -----------------------------------------------------

    def _serve(self) -> None:
        import websocket

        while not self._stop.is_set():
            try:
                ws = websocket.create_connection(GATEWAY_URL, timeout=20)
                self._session(ws)
            except GatewayClosed as exc:
                if exc.code == 4014:
                    # Config problem, not a blip — retrying forever helps nobody.
                    self.announce(
                        "[discord] Discord refused the connection: enable "
                        "'Message Content Intent' for the bot (Developer "
                        "Portal → Bot → Privileged Gateway Intents), then "
                        "restart the face. Listener stopped."
                    )
                    return
                if not self._stop.is_set():
                    self.announce(f"[discord] {exc}; reconnecting")
                    time.sleep(5)
            except Exception as exc:
                if not self._stop.is_set():
                    self.announce(f"[discord] gateway dropped ({type(exc).__name__}); reconnecting")
                    time.sleep(5)

    def _session(self, ws) -> None:
        import websocket

        hello = _recv_json(ws)
        beat_every = hello["d"]["heartbeat_interval"] / 1000.0
        seq: int | None = None
        ws.send(json.dumps({
            "op": 2,
            "d": {
                "token": self._token,
                "intents": INTENTS,
                "properties": {"os": "linux", "browser": "jarvis", "device": "jarvis"},
            },
        }))

        ws.settimeout(1.0)
        next_beat = time.monotonic() + beat_every
        while not self._stop.is_set():
            if time.monotonic() >= next_beat:
                ws.send(json.dumps({"op": 1, "d": seq}))
                next_beat = time.monotonic() + beat_every
            try:
                frame = _recv_json(ws)
            except websocket.WebSocketTimeoutException:
                continue
            if frame.get("s") is not None:
                seq = frame["s"]
            op, kind, data = frame.get("op"), frame.get("t"), frame.get("d")
            if op == 1:  # server asked for an immediate heartbeat
                ws.send(json.dumps({"op": 1, "d": seq}))
            elif op == 7 or op == 9:  # reconnect / invalid session
                ws.close()
                return
            elif kind == "READY":
                self.bot_id = str(data["user"]["id"])
                self.announce(
                    f"[discord] listening as {data['user'].get('username')} "
                    f"(replies only to the owner)"
                )
            elif kind == "MESSAGE_CREATE":
                if should_respond(data, self.bot_id, self.owner_id):
                    threading.Thread(
                        target=self._answer, args=(data,), daemon=True
                    ).start()

    # -- turning a message into a reply ---------------------------------------

    def _answer(self, message: dict[str, Any]) -> None:
        from .tools.discord import _api

        channel_id = str(message["channel_id"])
        text = strip_mention(message.get("content") or "", self.bot_id)
        voice_note = voice_attachment(message)
        try:
            _api("POST", f"/channels/{channel_id}/typing")
            if voice_note is not None:
                try:
                    text = self._hear(voice_note)
                except Exception as exc:
                    self._post(channel_id, f"I couldn't make out that voice message ({exc}).")
                    return
            reply = (self.run_turn(text, channel_id, voice_note is not None) or "").strip()
            if reply:
                audio = self._speak(reply) if voice_note is not None else None
                self._post(channel_id, reply[:MAX_DISCORD_CHARS], audio)
        except Exception as exc:
            self.announce(f"[discord] turn failed: {type(exc).__name__}: {exc}")

    def _hear(self, attachment: dict[str, Any]) -> str:
        """Voice note -> transcript. Raises with a human-readable reason."""
        from . import voice

        size = int(attachment.get("size") or 0)
        if size > MAX_VOICE_BYTES:
            raise ValueError(f"it is too large — {size // (1024 * 1024)}MB")
        audio = _download(attachment["url"])
        mime = (attachment.get("content_type") or "audio/ogg").split(";")[0]
        text = voice.stt(audio, mime=mime).strip()
        if not text:
            raise ValueError("the transcription came back empty")
        return text

    def _speak(self, reply: str) -> bytes | None:
        """Synthesize a voice-note reply; a TTS failure degrades to text-only."""
        from . import voice

        try:
            return voice.tts(reply)
        except Exception as exc:
            self.announce(f"[discord] tts failed ({type(exc).__name__}: {exc}); text-only reply")
            return None

    def _post(self, channel_id: str, content: str, audio: bytes | None = None) -> None:
        from .tools.discord import _api

        path = f"/channels/{channel_id}/messages"
        if audio is None:
            _api("POST", path, json={"content": content})
            return
        name, mime = _audio_filename(audio)
        _api(
            "POST",
            path,
            data={"payload_json": json.dumps({"content": content})},
            files={"files[0]": (name, audio, mime)},
        )
