"""Checks for the Discord Gateway listener.

Synthetic (free): the response rule — ONLY the owner, only via mention or
DM, never bots, never empty — plus mention stripping and the answer path
against a fake REST layer and a fake agent. Voice notes: what qualifies as
one (flag or waveform metadata, never a dragged-in mp3), the transcribe →
turn → spoken-reply pipeline with STT/TTS/download stubbed, every failure
becoming a text reply, and — via the real face server — that a transcription
can never resolve a pending authorization.

Live (needs the real token bundle; skipped without it): connects to the real
Gateway, completes HELLO -> IDENTIFY -> READY, and confirms the bot
identity. No messages are sent.

Run:  .venv/bin/python tests/discord_gateway_check.py
"""

from __future__ import annotations

import json
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis import config, discord_gateway
from jarvis.discord_gateway import (
    GatewayListener,
    should_respond,
    strip_mention,
    voice_attachment,
)

BOT, OWNER = "111", "999"


def _boom(exc: Exception):
    def raiser(*args, **kwargs):
        raise exc

    return raiser


def _voice_message(
    author: str = OWNER,
    guild: str | None = None,
    flags: int = discord_gateway.VOICE_MESSAGE_FLAG,
    content_type: str = "audio/ogg",
    waveform: bool = True,
    size: int = 4000,
    content: str = "",
) -> dict:
    attachment = {
        "id": "a1",
        "content_type": content_type,
        "url": "https://cdn.example.invalid/voice.ogg",
        "size": size,
    }
    if waveform:
        attachment["waveform"] = "AAAA"
        attachment["duration_secs"] = 2.1
    message = {
        "author": {"id": author, "bot": False},
        "mentions": [],
        "content": content,
        "channel_id": "c1",
        "flags": flags,
        "attachments": [attachment],
    }
    if guild:
        message["guild_id"] = guild
    return message


def rule_checks() -> None:
    def msg(author="999", bot=False, mentions=(BOT,), guild="g1", content="do the thing"):
        m = {
            "author": {"id": author, "bot": bot},
            "mentions": [{"id": i} for i in mentions],
            "content": content,
            "channel_id": "c1",
        }
        if guild:
            m["guild_id"] = guild
        return m

    assert should_respond(msg(), BOT, OWNER) is True
    assert should_respond(msg(author="555"), BOT, OWNER) is False, "stranger mention must be ignored"
    assert should_respond(msg(bot=True), BOT, OWNER) is False, "bots never trigger"
    assert should_respond(msg(mentions=()), BOT, OWNER) is False, "no mention, no trigger"
    assert should_respond(msg(mentions=(), guild=None), BOT, OWNER) is True, "owner DM needs no mention"
    assert should_respond(msg(guild=None, author="555", mentions=()), BOT, OWNER) is False, "stranger DM ignored"
    assert should_respond(msg(content=f"<@{BOT}>"), BOT, OWNER) is False, "bare mention is empty"

    assert strip_mention(f"<@{BOT}> status report", BOT) == "status report"
    assert strip_mention(f"<@!{BOT}> hey", BOT) == "hey"
    print("ok  rules: owner-only, mention-or-DM, no bots, no empties; mention stripped")


def voice_rule_checks() -> None:
    assert should_respond(_voice_message(), BOT, OWNER) is True, "owner DM voice note triggers"
    assert should_respond(_voice_message(author="555"), BOT, OWNER) is False, "stranger voice note ignored"
    assert should_respond(_voice_message(guild="g1"), BOT, OWNER) is False, \
        "a guild voice note cannot carry a mention, so it never triggers"
    assert should_respond(_voice_message(flags=0), BOT, OWNER) is True, "waveform metadata alone qualifies"
    assert should_respond(_voice_message(flags=0, waveform=False), BOT, OWNER) is False, \
        "a plain audio file with no text is not a voice note"
    assert should_respond(_voice_message(content_type="video/mp4"), BOT, OWNER) is False, \
        "the flag without an audio attachment is nothing"
    assert voice_attachment(_voice_message())["id"] == "a1"
    assert voice_attachment(_voice_message(flags=0, waveform=False)) is None
    print("ok  voice rules: flag-or-waveform, owner DM only, dragged-in audio ignored")


def answer_checks() -> None:
    calls, spoken_flags = [], []

    def fake_api(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return types.SimpleNamespace(status_code=200, json=lambda: {})

    import jarvis.tools.discord as discord_tools

    real = discord_tools._api
    discord_tools._api = fake_api
    try:
        def turn(text, ch, spoken):
            spoken_flags.append(spoken)
            return f"echo: {text}"

        listener = GatewayListener(run_turn=turn)
        listener.bot_id = BOT
        listener._answer(
            {"channel_id": "c1", "content": f"<@{BOT}> status report", "author": {"id": OWNER}}
        )
    finally:
        discord_tools._api = real

    assert ("POST", "/channels/c1/typing", {}) == calls[0], calls[0]
    method, path, kwargs = calls[1]
    assert path == "/channels/c1/messages" and kwargs["json"]["content"] == "echo: status report"
    assert spoken_flags == [False], "typed turns must not claim to be spoken"
    print("ok  answer: typing indicator, mention-stripped turn, reply posted")


def voice_answer_checks() -> None:
    import jarvis.tools.discord as discord_tools
    from jarvis import voice as voice_mod

    calls, turns = [], []

    def fake_api(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return types.SimpleNamespace(status_code=200, json=lambda: {})

    real = (discord_tools._api, discord_gateway._download, voice_mod.stt, voice_mod.tts)
    discord_tools._api = fake_api
    discord_gateway._download = lambda url: b"OggS-fake-opus"
    voice_mod.stt = lambda audio, mime="audio/webm": "what time is it"
    voice_mod.tts = lambda text, **kwargs: b"RIFF" + b"\x00" * 64
    try:
        def turn(text, ch, spoken):
            turns.append((text, ch, spoken))
            return "half past nine"

        listener = GatewayListener(run_turn=turn, announce=lambda note: None)
        listener.bot_id = BOT

        # happy path: transcript in, text + WAV voice reply out, one message
        listener._answer(_voice_message())
        assert turns == [("what time is it", "c1", True)], turns
        method, path, kwargs = calls[1]
        assert path == "/channels/c1/messages"
        assert json.loads(kwargs["data"]["payload_json"])["content"] == "half past nine"
        name, audio, mime = kwargs["files"]["files[0]"]
        assert name == "jarvis-reply.wav" and mime == "audio/wav" and audio[:4] == b"RIFF"

        # cloud TTS emits mp3 — the attachment is named by sniffing, not label
        voice_mod.tts = lambda text, **kwargs: b"\xff\xfbmp3-ish"
        calls.clear()
        listener._answer(_voice_message())
        name, _, mime = calls[1][2]["files"]["files[0]"]
        assert name == "jarvis-reply.mp3" and mime == "audio/mpeg"

        # STT failure becomes a text reply and the agent never runs
        voice_mod.stt = _boom(ValueError("garbled"))
        turns.clear()
        calls.clear()
        listener._answer(_voice_message())
        assert turns == [], "a failed transcription must not reach the agent"
        assert "couldn't make out" in calls[1][2]["json"]["content"]

        # an absurdly large attachment is refused before any download
        voice_mod.stt = lambda audio, mime="audio/webm": "unreachable"
        discord_gateway._download = _boom(AssertionError("downloaded an oversize attachment"))
        calls.clear()
        listener._answer(_voice_message(size=64 * 1024 * 1024))
        assert "too large" in calls[1][2]["json"]["content"]

        # TTS failure degrades to a text-only reply, never a silent drop
        discord_gateway._download = lambda url: b"OggS-fake-opus"
        voice_mod.stt = lambda audio, mime="audio/webm": "what time is it"
        voice_mod.tts = _boom(RuntimeError("no model"))
        calls.clear()
        listener._answer(_voice_message())
        method, path, kwargs = calls[1]
        assert kwargs["json"]["content"] == "half past nine" and "files" not in kwargs
    finally:
        discord_tools._api, discord_gateway._download, voice_mod.stt, voice_mod.tts = real

    print("ok  voice answer: stt->turn->tts pipeline, sniffed filename, failures degrade to text")


def approval_isolation_check() -> None:
    """The property that makes voice safe: only typed replies can authorize."""
    from jarvis.face import server

    hits = []
    fake_agent = types.SimpleNamespace(
        run_turn=lambda text: types.SimpleNamespace(text=f"agent saw: {text}")
    )
    real_agent = server._get_discord_agent
    server.DISCORD_APPROVALS.handle_reply = (
        lambda ch, text: hits.append((ch, text)) or "authorized"
    )
    server._get_discord_agent = lambda: fake_agent
    try:
        assert server._discord_turn("yes", "c9") == "authorized"
        assert hits == [("c9", "yes")]
        spoken = server._discord_turn("yes", "c9", spoken=True)
        assert len(hits) == 1, "a transcription must never reach the approval parser"
        assert spoken == "agent saw: [voice note] yes", spoken
    finally:
        del server.DISCORD_APPROVALS.handle_reply  # un-shadow the real method
        server._get_discord_agent = real_agent
    print("ok  approvals: typed replies resolve them, voice notes never do")


def live_handshake_check() -> None:
    if not config.DISCORD_TOKEN_PATH.exists():
        print("skip live handshake: no token bundle")
        return
    hits = []
    listener = GatewayListener(run_turn=lambda t, c, s: "", announce=hits.append)
    listener.start()
    deadline = time.monotonic() + 15
    while (
        time.monotonic() < deadline
        and not listener.bot_id
        and not any("Message Content Intent" in h for h in hits)
    ):
        time.sleep(0.3)
    listener.stop()
    if any("Message Content Intent" in h for h in hits):
        # Environment prerequisite, not a code failure — surfaced clearly.
        print("skip live handshake: the portal's Message Content Intent is off "
              "(the listener reported it and stopped, as designed)")
        return
    assert listener.bot_id, f"no READY within 15s ({hits})"
    assert any("listening as" in h for h in hits), hits
    print(f"ok  live: gateway handshake complete, bot id {listener.bot_id}")


def main() -> int:
    rule_checks()
    voice_rule_checks()
    answer_checks()
    voice_answer_checks()
    approval_isolation_check()
    live_handshake_check()
    print("\nall gateway checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
