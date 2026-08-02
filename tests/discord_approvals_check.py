"""Synthetic checks for approving a dangerous tool from a Discord DM. Free.

No network: the DM sender is injected, so every message the owner would see
is captured in a list. What matters here is that the *only* way through is a
clear yes from the owner, in the channel the question was asked in, against
the specific request it was asked about.

Run:  .venv/bin/python tests/discord_approvals_check.py
"""

from __future__ import annotations

import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis import discord_approvals as da
from jarvis.discord_gateway import should_respond
from jarvis.face.approvals import ApprovalBroker

DM_CHANNEL = "dm-777"


class FakeDiscord:
    """Stands in for the owner's DM channel."""

    def __init__(self, broken: bool = False):
        self.sent: list[str] = []
        self.broken = broken

    def send(self, text: str) -> str:
        if self.broken:
            raise RuntimeError("Discord is not connected")
        self.sent.append(text)
        return DM_CHANNEL

    def code(self, index: int = -1) -> str:
        return re.search(r"code `([a-z0-9]{4})`", self.sent[index]).group(1)


class NoWindow:
    """A face with no HUD open — the away-from-desk case."""

    def __init__(self, viewers: int = 0):
        self.events: list[tuple[str, dict]] = []
        self._viewers = viewers

    def broadcast(self, kind, data):
        self.events.append((kind, data))

    def viewers(self):
        return self._viewers


def _wire(broken: bool = False, viewers: int = 0, minutes: int = 10):
    discord = FakeDiscord(broken)
    remote = da.DiscordApprovals(announce=lambda m: None, send=discord.send, minutes=minutes)
    window = NoWindow(viewers)
    broker = ApprovalBroker(
        window.broadcast, window.viewers, announce=lambda m: None, remote=remote
    )
    remote.bind(broker)
    return discord, remote, broker, window


def _ask(broker, tool="run_command", args=None, prefer_remote=True):
    """Run one request on its own thread, as dispatch() would."""
    result: list[bool] = []
    thread = threading.Thread(
        target=lambda: result.append(
            broker.request(tool, args or {"command": "git push origin main"},
                           prefer_remote=prefer_remote)
        )
    )
    thread.start()
    return thread, result


def _wait_for(probe, what, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def allow_deny_checks() -> None:
    for reply, expected in (("yes", True), ("no", False), ("YES  ", True), ("Deny", False)):
        discord, remote, broker, _ = _wire()
        thread, result = _ask(broker)
        _wait_for(lambda: discord.sent, "the DM to go out")

        ask = discord.sent[0]
        assert "run_command" in ask and "git push origin main" in ask, ask
        assert "code `" in ask and "10 minutes" in ask, ask

        assert remote.handle_reply(DM_CHANNEL, reply) is not None
        thread.join(timeout=3)
        assert result == [expected], (reply, result)
        assert broker.decisions[-1]["resolution"] == ("approved" if expected else "denied")
    print("ok  reply: yes/no (any case) resolves the exact request the DM asked about")


def always_checks() -> None:
    discord, remote, broker, _ = _wire()
    allowlisted: list[tuple[str, dict]] = []
    broker._on_always = lambda tool, args: allowlisted.append((tool, args))
    thread, result = _ask(broker)
    _wait_for(lambda: discord.sent, "the DM")

    out = remote.handle_reply(DM_CHANNEL, "always")
    thread.join(timeout=3)
    assert result == [True]
    assert allowlisted and allowlisted[0][0] == "run_command", allowlisted
    assert broker.decisions[-1]["resolution"] == "approved-always"
    assert "stop asking" in out
    print("ok  reply: ALWAYS authorizes and writes the persistent allowlist entry")


def wrong_channel_checks() -> None:
    discord, remote, broker, _ = _wire()
    thread, result = _ask(broker)
    _wait_for(lambda: discord.sent, "the DM")

    # A "yes" in a server channel is not an authorization, even from the owner.
    assert remote.handle_reply("guild-channel-1", "yes") is None, "answered outside the DM"
    assert remote.open_count == 1
    remote.handle_reply(DM_CHANNEL, "no")
    thread.join(timeout=3)
    assert result == [False]

    # And a stranger cannot reach this code at all: the gateway drops them.
    stranger = {"author": {"id": "999"}, "content": "yes", "mentions": []}
    assert should_respond(stranger, bot_id="1", owner_id="42") is False
    print("ok  scope: only the owner, only in the DM the question was asked in")


def ambiguity_checks() -> None:
    discord, remote, broker, _ = _wire()
    first, first_result = _ask(broker, args={"command": "git push"})
    _wait_for(lambda: len(discord.sent) == 1, "the first DM")
    second, second_result = _ask(broker, tool="gmail_send", args={"to": "a@b.c"})
    _wait_for(lambda: len(discord.sent) == 2, "the second DM")

    # Two open questions, one bare "yes" -> refuse to guess.
    out = remote.handle_reply(DM_CHANNEL, "yes")
    assert "Which one" in out and discord.code(0) in out and discord.code(1) in out, out
    assert remote.open_count == 2, "a bare yes resolved something"

    # An unknown code resolves nothing either.
    assert "No open authorization" in remote.handle_reply(DM_CHANNEL, "yes zzzz")
    assert remote.open_count == 2

    # Coded answers hit exactly their own request.
    remote.handle_reply(DM_CHANNEL, f"no {discord.code(0)}")
    first.join(timeout=3)
    assert first_result == [False]
    remote.handle_reply(DM_CHANNEL, f"yes {discord.code(1)}")
    second.join(timeout=3)
    assert second_result == [True]
    print("ok  ambiguity: two open asks need the code; each answer hits only its own")


def unparseable_checks() -> None:
    discord, remote, broker, _ = _wire()
    thread, result = _ask(broker)
    _wait_for(lambda: discord.sent, "the DM")

    for text in ("no wait actually yes", "what does it want", "", "y e s", "sure why not"):
        out = remote.handle_reply(DM_CHANNEL, text)
        assert out is not None and "Still waiting" in out, (text, out)
        assert remote.open_count == 1, f"{text!r} resolved something"

    remote.handle_reply(DM_CHANNEL, "no")
    thread.join(timeout=3)
    assert result == [False]
    print("ok  parsing: anything that is not a clear answer resolves nothing")


def replay_checks() -> None:
    discord, remote, broker, _ = _wire()
    thread, result = _ask(broker)
    _wait_for(lambda: discord.sent, "the DM")
    code = discord.code()
    remote.handle_reply(DM_CHANNEL, "yes")
    thread.join(timeout=3)
    assert result == [True]

    # The same "yes" again, and the same code again, are dead.
    assert remote.handle_reply(DM_CHANNEL, "yes") is None
    assert remote.handle_reply(DM_CHANNEL, f"yes {code}") is None

    # A later request does not inherit the old answer.
    thread, result = _ask(broker, args={"command": "rm -rf ~"})
    _wait_for(lambda: len(discord.sent) == 2, "the second DM")
    remote.handle_reply(DM_CHANNEL, "no")
    thread.join(timeout=3)
    assert result == [False]
    print("ok  replay: an answered code is spent; a new ask starts from unanswered")


def failure_mode_checks() -> None:
    # Discord down, no window -> the existing denial, and nothing hangs.
    discord, remote, broker, _ = _wire(broken=True)
    assert broker.request("run_command", {"command": "x"}, prefer_remote=True) is False
    assert broker.decisions[-1]["resolution"] == "nowhere-to-ask"

    # Nobody answers -> denied on timeout, and the owner is told it expired.
    discord, remote, broker, _ = _wire()
    remote.timeout_s = 0.3
    assert broker.request("run_command", {"command": "x"}, prefer_remote=True) is False
    assert broker.decisions[-1]["resolution"] == "timeout"
    assert "closed" in discord.sent[-1] and "Nothing ran" in discord.sent[-1]
    assert remote.open_count == 0

    # The HUD window closing must not kill a question that also went by DM.
    discord, remote, broker, window = _wire(viewers=1)
    thread, result = _ask(broker)
    _wait_for(lambda: discord.sent, "the DM")
    assert broker.deny_all("window-closed", include_remote=False) == 0
    assert remote.open_count == 1, "the DM question died with the window"
    remote.handle_reply(DM_CHANNEL, "yes")
    thread.join(timeout=3)
    assert result == [True]

    # Shutdown still denies everything, remote included.
    discord, remote, broker, _ = _wire()
    thread, result = _ask(broker)
    _wait_for(lambda: discord.sent, "the DM")
    assert broker.deny_all("shutdown") == 1
    thread.join(timeout=3)
    assert result == [False]
    print("ok  failures: no channel, timeout, window-closed, shutdown — all deny")


def card_checks() -> None:
    """With a window open too, the card carries the real deadline."""
    discord, remote, broker, window = _wire(viewers=1)
    thread, result = _ask(broker)
    _wait_for(lambda: discord.sent, "the DM")
    kind, card = window.events[0]
    assert kind == "approval" and card["remote"] is True, card
    assert card["timeout_s"] == round(da.REMOTE_TIMEOUT_S), card
    # Either surface can answer; the id is one-shot, so the first wins.
    assert broker.resolve(card["id"], True) is True
    thread.join(timeout=3)
    assert result == [True]
    assert remote.handle_reply(DM_CHANNEL, "yes") is None
    print("ok  card: window and DM share one request; the card shows the DM deadline")


def main() -> int:
    allow_deny_checks()
    always_checks()
    wrong_channel_checks()
    ambiguity_checks()
    unparseable_checks()
    replay_checks()
    failure_mode_checks()
    card_checks()
    print("\nall discord approval checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
