"""Checks for the Discord Gateway listener.

Synthetic (free): the response rule — ONLY the owner, only via mention or
DM, never bots, never empty — plus mention stripping and the answer path
against a fake REST layer and a fake agent.

Live (needs the real token bundle; skipped without it): connects to the real
Gateway, completes HELLO -> IDENTIFY -> READY, and confirms the bot
identity. No messages are sent.

Run:  .venv/bin/python tests/discord_gateway_check.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis import config, discord_gateway
from jarvis.discord_gateway import GatewayListener, should_respond, strip_mention

BOT, OWNER = "111", "999"


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


def answer_checks() -> None:
    calls = []

    def fake_api(method, path, **kwargs):
        calls.append((method, path, kwargs))
        import types

        return types.SimpleNamespace(status_code=200, json=lambda: {})

    import jarvis.tools.discord as discord_tools

    real = discord_tools._api
    discord_tools._api = fake_api
    try:
        listener = GatewayListener(run_turn=lambda text, ch: f"echo: {text}")
        listener.bot_id = BOT
        listener._answer(
            {"channel_id": "c1", "content": f"<@{BOT}> status report", "author": {"id": OWNER}}
        )
    finally:
        discord_tools._api = real

    assert ("POST", "/channels/c1/typing", {}) == calls[0], calls[0]
    method, path, kwargs = calls[1]
    assert path == "/channels/c1/messages" and kwargs["json"]["content"] == "echo: status report"
    print("ok  answer: typing indicator, mention-stripped turn, reply posted")


def live_handshake_check() -> None:
    if not config.DISCORD_TOKEN_PATH.exists():
        print("skip live handshake: no token bundle")
        return
    hits = []
    listener = GatewayListener(run_turn=lambda t, c: "", announce=hits.append)
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
    answer_checks()
    live_handshake_check()
    print("\nall gateway checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
