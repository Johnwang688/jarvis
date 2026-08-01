"""Synthetic checks for the Discord bot tools. Free — no network, no API.

What must hold:
  1. channels/read/send/dm speak the API correctly (fake transport), with
     the Bot auth header and oldest-first message ordering
  2. gating: discord_send is dangerous and a denial never touches the
     network; discord_dm_owner is safe (recipient pinned to the owner) and
     included in the workflow toolset without breaking its no-dangerous rule
  3. discord_token.json is covered by every secrets layer
  4. a missing bundle explains `jarvis auth discord`

Run:  .venv/bin/python tests/discord_check.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from jarvis import config, tools, workflows
from jarvis.tools import secrets

BOT_TOKEN = "fake-bot-token-abc123456-not-for-the-model"


def _response(status: int, payload) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        status_code=status, json=lambda: payload, text=json.dumps(payload)
    )


calls: list[dict] = []


def fake_request(method, url, headers=None, timeout=None, **kwargs):
    calls.append({"method": method, "url": url, "headers": headers, **kwargs})
    assert headers["Authorization"] == f"Bot {BOT_TOKEN}"
    path = url.replace(config.DISCORD_API, "")
    if path == "/users/@me/guilds":
        return _response(200, [{"id": "g1", "name": "Jarvis HQ"}])
    if path == "/guilds/g1/channels":
        return _response(200, [
            {"id": "c1", "name": "general", "type": 0},
            {"id": "v1", "name": "lounge", "type": 2},  # voice: excluded
        ])
    if path == "/channels/c1/messages" and method == "GET":
        return _response(200, [  # Discord returns newest first
            {"timestamp": "2026-07-31T16:02:00", "author": {"username": "johnw"},
             "content": "second", "attachments": [], "embeds": []},
            {"timestamp": "2026-07-31T16:01:00", "author": {"username": "johnw"},
             "content": "first", "attachments": [{"a": 1}], "embeds": []},
        ])
    if path == "/channels/c1/messages" and method == "POST":
        return _response(200, {"id": "m9"})
    if path == "/users/@me/channels":
        assert kwargs["json"]["recipient_id"] == "42"
        return _response(200, {"id": "dmc"})
    if path == "/channels/dmc/messages":
        return _response(200, {"id": "m10"})
    raise AssertionError(f"unexpected call: {method} {url}")


def tool_checks() -> None:
    out = tools.dispatch("discord_channels", "{}")
    assert "Jarvis HQ (server g1)" in out.text and "#general — c1" in out.text, out.text
    assert "lounge" not in out.text, "voice channels must be excluded"

    out = tools.dispatch("discord_read", json.dumps({"channel_id": "c1"}))
    first_pos, second_pos = out.text.find("first"), out.text.find("second")
    assert 0 <= first_pos < second_pos, f"not oldest-first: {out.text}"
    assert "johnw" in out.text and "+1 attachment(s)" in out.text, out.text

    out = tools.dispatch(
        "discord_send",
        json.dumps({"channel_id": "c1", "message": "build finished"}),
        approve=lambda t, a: True,
    )
    assert "Posted" in out.text, out.text
    posted = [c for c in calls if c["url"].endswith("/channels/c1/messages") and c["method"] == "POST"]
    assert posted[-1]["json"]["content"] == "build finished"

    out = tools.dispatch("discord_dm_owner", json.dumps({"message": "workflow done"}))
    assert "DM sent" in out.text, out.text
    print("ok  tools: channels/read/send/dm speak the API, oldest-first, Bot auth")


def gating_checks() -> None:
    assert tools.REGISTRY["discord_send"].dangerous is True
    assert tools.REGISTRY["discord_dm_owner"].dangerous is False
    assert tools.REGISTRY["discord_read"].dangerous is False

    assert "discord_dm_owner" in workflows.SAFE_TOOLS
    dangerous = {name for name, t in tools.REGISTRY.items() if t.dangerous}
    assert not dangerous & set(workflows.SAFE_TOOLS), "dangerous tool in SAFE_TOOLS"

    real = httpx.request
    httpx.request = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("a denied send touched the network")
    )
    try:
        out = tools.dispatch(
            "discord_send",
            json.dumps({"channel_id": "c1", "message": "x"}),
            approve=lambda t, a: False,
        )
        assert "declined" in out.text, out.text
    finally:
        httpx.request = real
    print("ok  gating: send needs approval; dm_owner is safe and workflow-usable")


def secrets_checks() -> None:
    assert secrets.is_protected(config.DISCORD_TOKEN_PATH)
    assert secrets.protected_in_command(f"cat {config.DISCORD_TOKEN_PATH}")
    assert secrets.protected_in_command("less ~/.config/jarvis/discord_token.*")
    out = tools.dispatch("read_file", json.dumps({"path": str(config.DISCORD_TOKEN_PATH)}))
    assert "protected" in out.text, out.text
    scrubbed = secrets.scrub(f"log line with {BOT_TOKEN} in it")
    assert BOT_TOKEN not in scrubbed and secrets.REDACTED in scrubbed, scrubbed
    print("ok  secrets: bundle refused by name/glob/read_file, token scrubbed")


def missing_checks() -> None:
    config.DISCORD_TOKEN_PATH = Path(tempfile.mkdtemp()) / "nope" / "discord_token.json"
    out = tools.dispatch("discord_dm_owner", json.dumps({"message": "x"}))
    assert "jarvis auth discord" in out.text, out.text
    print("ok  missing: absent bundle explains the setup command")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        config.DISCORD_TOKEN_PATH = Path(tmp) / "discord_token.json"
        config.DISCORD_TOKEN_PATH.write_text(
            json.dumps({"bot_token": BOT_TOKEN, "owner_id": "42"}), encoding="utf-8"
        )
        real = httpx.request
        httpx.request = fake_request
        try:
            tool_checks()
        finally:
            httpx.request = real
        gating_checks()
        secrets_checks()
    missing_checks()
    print("\nall discord checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
