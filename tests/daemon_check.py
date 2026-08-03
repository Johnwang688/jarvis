"""Synthetic checks for the headless daemon (`jarvis daemon`). Free.

No network anywhere: the gateway listener is a fake injected through
`listener_factory`, the Discord approval channel is a stub, and the only
sockets are loopback HTTP to the daemon's own health endpoint. What matters
here:

  - the shared DiscordResponder keeps the safety rule that used to live in
    face/server.py: a spoken (STT'd) turn never reaches the approval parser;
  - a broker with no window and no deliverable remote denies as
    nowhere-to-ask, and detach_remote() makes remote asks stop entirely;
  - the health endpoint is the single-instance lock, and the daemon refuses
    to start over a running face (double-IDENTIFY prevention);
  - stop() releases every blocked approval waiter with a denial and stops
    the listener — never leave a waiter blocked.

Run:  .venv/bin/python tests/daemon_check.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import types
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis import config
from jarvis import daemon as daemon_mod
from jarvis.discord_agent import DiscordResponder
from jarvis.face.approvals import ApprovalBroker


class StubChannel:
    """Stands in for DiscordApprovals: records asks/closes, delivery scripted."""

    timeout_s = 0.3

    def __init__(self, delivered: bool = True):
        self.delivered = delivered
        self.asks: list = []
        self.closes: list = []
        self.replies: list = []

    def ask(self, item) -> bool:
        self.asks.append(item)
        return self.delivered

    def close(self, request_id: str, resolution: str) -> None:
        self.closes.append((request_id, resolution))

    def bind(self, broker) -> None:
        self.broker = broker

    def handle_reply(self, channel_id: str, text: str):
        self.replies.append((channel_id, text))
        return "authorized"


class FakeListener:
    def __init__(self, run_turn):
        self.run_turn = run_turn
        self.started = False
        self.stopped = False
        self.bot_id = ""

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def responder_isolation_check() -> None:
    """Typed replies may resolve an authorization; transcriptions never can."""
    channel = StubChannel()
    fake_agent = types.SimpleNamespace(
        run_turn=lambda text: types.SimpleNamespace(text=f"agent saw: {text}")
    )
    notes = []
    responder = DiscordResponder(
        broker=None,  # never consulted: the agent is injected
        channel=channel,
        on_note=notes.append,
        agent_factory=lambda: fake_agent,
    )
    assert responder.turn("yes", "c1") == "authorized"
    assert channel.replies == [("c1", "yes")]
    spoken = responder.turn("yes", "c1", spoken=True)
    assert len(channel.replies) == 1, "a transcription reached the approval parser"
    assert spoken == "agent saw: [voice note] yes", spoken
    assert any("voice note" in n for n in notes)
    print("ok  responder: typed replies resolve approvals, voice notes never do")


def headless_broker_check() -> None:
    """viewers=0 forever: remote-or-deny, and detach_remote() ends the asking."""
    # Not delivered (Discord down) -> immediate nowhere-to-ask denial.
    broker = ApprovalBroker(
        broadcast=lambda kind, data: None,
        viewers=lambda: 0,
        announce=lambda text: None,
        remote=StubChannel(delivered=False),
    )
    assert broker.request("run_command", {"command": "rm -rf /"}) is False
    assert broker.decisions[-1]["resolution"] == "nowhere-to-ask"

    # Delivered but unanswered -> denied on the remote timeout, channel closed.
    channel = StubChannel(delivered=True)
    broker = ApprovalBroker(
        broadcast=lambda kind, data: None,
        viewers=lambda: 0,
        announce=lambda text: None,
        remote=channel,
    )
    assert broker.request("gmail_send", {"to": "x@y.z"}) is False
    assert broker.decisions[-1]["resolution"] == "timeout"
    assert channel.closes and channel.closes[-1][1] == "timeout"

    # detach_remote(): the channel is never asked again.
    broker.detach_remote()
    before = len(channel.asks)
    assert broker.request("gmail_send", {"to": "x@y.z"}) is False
    assert broker.decisions[-1]["resolution"] == "nowhere-to-ask"
    assert len(channel.asks) == before, "detached broker still asked the remote"
    print("ok  broker: nowhere-to-ask denies, remote timeout denies, detach stops asks")


class _FakeFaceHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps({"stt": "fake", "tts": "fake"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _dead_port() -> int:
    """A port with nothing on it (bound and released just now)."""
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_daemon(token_path: Path, channel=None) -> daemon_mod.Daemon:
    # Point the face probe at a dead port: a real `jarvis face` running on
    # this machine (entirely likely on the owner's box) must not fail the test.
    real_token, real_face = config.DISCORD_TOKEN_PATH, config.FACE_PORT
    config.DISCORD_TOKEN_PATH = token_path
    config.FACE_PORT = _dead_port()
    try:
        d = daemon_mod.Daemon(
            port=0,
            listener_factory=FakeListener,
            channel=channel or StubChannel(),
            announce=lambda text: None,
        )
        d.start()
        return d
    finally:
        config.DISCORD_TOKEN_PATH, config.FACE_PORT = real_token, real_face


def lifecycle_check(tmp: Path) -> None:
    """Health marker, single-instance lock, face refusal, clean shutdown."""
    token = tmp / "discord_token.json"
    token.write_text("{}")

    # No token bundle -> refuse with the auth hint.
    real_token = config.DISCORD_TOKEN_PATH
    config.DISCORD_TOKEN_PATH = tmp / "missing.json"
    try:
        try:
            daemon_mod.Daemon(port=0).start()
            raise AssertionError("started without a Discord bundle")
        except daemon_mod.DaemonError as exc:
            assert "auth discord" in str(exc)
    finally:
        config.DISCORD_TOKEN_PATH = real_token

    # ALWAYS answers write the allowlist; make sure a test can never touch
    # the real one (the `apt` incident rule).
    real_allow = config.ALLOWLIST_PATH
    config.ALLOWLIST_PATH = tmp / "allowlist.json"
    channel = StubChannel(delivered=True)
    channel.timeout_s = 30  # long: the waiter is released by stop(), not time
    daemon = _start_daemon(token, channel=channel)
    try:
        assert daemon.listener.started

        # The health endpoint is up and marked, and is_running() sees it.
        assert daemon_mod.is_running(daemon.port)
        with urllib.request.urlopen(
            f"http://localhost:{daemon.port}/status", timeout=2
        ) as response:
            status = json.loads(response.read())
        assert status["jarvis-daemon"] is True and status["pid"]

        # Same port again -> the lock holds.
        second = daemon_mod.Daemon(port=daemon.port, announce=lambda t: None)
        real_token2, real_face2 = config.DISCORD_TOKEN_PATH, config.FACE_PORT
        config.DISCORD_TOKEN_PATH, config.FACE_PORT = token, _dead_port()
        try:
            second.start()
            raise AssertionError("two daemons on one port")
        except daemon_mod.DaemonError as exc:
            assert "already running" in str(exc)
        finally:
            config.DISCORD_TOKEN_PATH, config.FACE_PORT = real_token2, real_face2

        # A blocked approval waiter is released, denied, by stop().
        results = []
        waiter = threading.Thread(
            target=lambda: results.append(
                daemon.broker.request("run_command", {"command": "reboot"})
            )
        )
        waiter.start()
        deadline = time.monotonic() + 2
        while not daemon.broker.pending_count and time.monotonic() < deadline:
            time.sleep(0.01)
        assert daemon.broker.pending_count == 1
        daemon.stop()
        waiter.join(timeout=2)
        assert results == [False]
        assert daemon.broker.decisions[-1]["resolution"] == "shutdown"
        assert daemon.listener.stopped
        assert not daemon_mod.is_running(daemon.port)
    finally:
        daemon.stop()  # idempotent-enough: health already gone, listener stopped
        config.ALLOWLIST_PATH = real_allow
    print("ok  daemon: health lock, single instance, shutdown releases waiters")


def face_exclusion_check(tmp: Path) -> None:
    """A running face means the gateway is taken: the daemon must refuse."""
    fake_face = ThreadingHTTPServer(("127.0.0.1", 0), _FakeFaceHandler)
    threading.Thread(target=fake_face.serve_forever, daemon=True).start()
    port = fake_face.server_address[1]
    assert daemon_mod.face_is_serving(port)

    token = tmp / "discord_token.json"
    token.write_text("{}")
    real_token, real_face = config.DISCORD_TOKEN_PATH, config.FACE_PORT
    config.DISCORD_TOKEN_PATH, config.FACE_PORT = token, port
    try:
        try:
            daemon_mod.Daemon(port=0, announce=lambda t: None).start()
            raise AssertionError("daemon started over a running face")
        except daemon_mod.DaemonError as exc:
            assert "face" in str(exc)
    finally:
        config.DISCORD_TOKEN_PATH, config.FACE_PORT = real_token, real_face
        fake_face.shutdown()
        fake_face.server_close()

    # And the reverse probe: something that is not a daemon is not "running".
    assert not daemon_mod.is_running(port)
    print("ok  exclusion: daemon refuses over a face, non-daemon ports read as absent")


def main() -> int:
    responder_isolation_check()
    headless_broker_check()
    with tempfile.TemporaryDirectory() as tmp:
        lifecycle_check(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        face_exclusion_check(Path(tmp))
    print("\nall daemon checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
