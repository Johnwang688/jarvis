"""Synthetic checks for the dangerous-tool approval gate. Free — no API calls.

Covers, in three layers:

  1. the broker (jarvis/face/approvals.py) with a fake window — approve, deny,
     timeout, nowhere-to-ask, replay, double-answer, window-closed
  2. the HTTP route against a real server on a spare port — a real SSE
     subscriber receives the card, POSTs the answer, and the blocked thread
     wakes with it; plus the same-origin and content-type refusals
  3. the self-approval block — the browser and web tools must refuse the
     face's own origin, or the agent could open its own HUD and click its own
     AUTHORIZE button

Run:  .venv/bin/python tests/face/approval_check.py
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from queue import Empty, Queue

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis import config, tools  # noqa: E402
from jarvis.browser import BrowserPolicy  # noqa: E402
from jarvis.face import server as face_server  # noqa: E402
from jarvis.face.approvals import ApprovalBroker  # noqa: E402

PORT = 8471  # not the face's real port; the server under test is disposable
BASE = f"http://127.0.0.1:{PORT}"


class FakeWindow:
    """Stands in for a connected HUD: counts as a viewer, records broadcasts."""

    def __init__(self, connected: int = 1):
        self.connected = connected
        self.sent: list[tuple[str, dict]] = []

    def broadcast(self, kind, data):
        self.sent.append((kind, data))

    def viewers(self):
        return self.connected

    def kinds(self):
        return [k for k, _ in self.sent]

    def last(self, kind):
        return [d for k, d in self.sent if k == kind][-1]


def broker_checks() -> None:
    # -- no window: denied without even asking
    win = FakeWindow(connected=0)
    broker = ApprovalBroker(win.broadcast, win.viewers, announce=lambda m: None)
    assert broker.request("run_command", {"command": "rm -rf /"}) is False
    assert win.sent == [], "nothing to broadcast with no window open"
    assert broker.decisions[-1]["resolution"] == "nowhere-to-ask"
    print("ok  broker: no window connected -> denied immediately, nothing broadcast")

    # -- approve and deny, answered from another thread
    win = FakeWindow()
    broker = ApprovalBroker(win.broadcast, win.viewers, announce=lambda m: None)

    for allow in (True, False):
        result: list[bool] = []
        waiter = threading.Thread(
            target=lambda: result.append(
                broker.request("run_command", {"command": "echo hi", "reason": "test"})
            )
        )
        waiter.start()
        card = _await(lambda: win.last("approval") if "approval" in win.kinds() else None)
        assert card["tool"] == "run_command", card
        assert card["args"]["command"] == "echo hi", card
        assert broker.resolve(card["id"], allow) is True
        waiter.join(timeout=5)
        assert result == [allow], result
        assert win.last("approval_closed")["resolution"] == ("approved" if allow else "denied")
        win.sent.clear()
    print("ok  broker: a decision from another thread unblocks the waiting one")

    # -- replay and double-answer
    win = FakeWindow()
    broker = ApprovalBroker(win.broadcast, win.viewers, announce=lambda m: None)
    result = []
    waiter = threading.Thread(target=lambda: result.append(broker.request("run_command", {})))
    waiter.start()
    card = _await(lambda: win.last("approval") if "approval" in win.kinds() else None)
    assert broker.resolve("not-a-real-id", True) is False, "unknown id must not resolve"
    assert broker.resolve(card["id"], False) is True
    waiter.join(timeout=5)
    assert broker.resolve(card["id"], True) is False, "an id must be single-use"
    assert result == [False], "the replay must not flip a decided request"
    print("ok  broker: ids are one-shot — unknown and replayed answers refused")

    # -- timeout
    win = FakeWindow()
    broker = ApprovalBroker(win.broadcast, win.viewers, timeout_s=0.3, announce=lambda m: None)
    started = time.monotonic()
    assert broker.request("run_command", {"command": "sleep 1"}) is False
    assert 0.25 < time.monotonic() - started < 3.0
    assert win.last("approval_closed")["resolution"] == "timeout"
    assert broker.pending_count == 0
    print("ok  broker: nobody answers -> denied on timeout, card withdrawn")

    # -- the window goes away mid-question
    win = FakeWindow()
    broker = ApprovalBroker(win.broadcast, win.viewers, timeout_s=30, announce=lambda m: None)
    result = []
    waiter = threading.Thread(target=lambda: result.append(broker.request("run_command", {})))
    waiter.start()
    _await(lambda: win.last("approval") if "approval" in win.kinds() else None)
    assert broker.deny_all("window-closed") == 1
    waiter.join(timeout=5)
    assert result == [False], result
    print("ok  broker: window closed mid-question -> waiter released as denied")


def dispatch_checks() -> None:
    """The gate has to hold through dispatch(), which is what actually runs tools."""
    win = FakeWindow(connected=0)
    broker = ApprovalBroker(win.broadcast, win.viewers, announce=lambda m: None)
    out = tools.dispatch(
        "run_command",
        json.dumps({"command": "echo SHOULD-NOT-RUN", "reason": "test"}),
        approve=broker.approver(),
    )
    assert "declined" in out.text.lower(), out.text
    assert "SHOULD-NOT-RUN" not in out.text
    print("ok  dispatch: a denied dangerous tool never executes")

    win = FakeWindow()
    broker = ApprovalBroker(win.broadcast, win.viewers, announce=lambda m: None)
    threading.Thread(
        target=lambda: broker.resolve(
            _await(lambda: win.last("approval") if "approval" in win.kinds() else None)["id"],
            True,
        ),
        daemon=True,
    ).start()
    out = tools.dispatch(
        "run_command",
        json.dumps({"command": "echo AUTHORIZED-OK", "reason": "test"}),
        approve=broker.approver(),
    )
    assert "AUTHORIZED-OK" in out.text, out.text
    print("ok  dispatch: an authorized dangerous tool runs for real")


def http_checks() -> None:
    server = face_server.create_server(PORT)
    broker = face_server.APPROVALS
    try:
        events = _subscribe()
        _await(lambda: face_server._viewers() or None)  # SSE connection established

        # -- the real path: card over SSE, answer over POST
        result: list[bool] = []
        waiter = threading.Thread(
            target=lambda: result.append(
                broker.request("run_command", {"command": "systemctl restart nginx"})
            )
        )
        waiter.start()
        card = _await(lambda: _next_kind(events, "approval"))
        assert card["tool"] == "run_command", card
        assert card["args"]["command"] == "systemctl restart nginx", card
        assert card["timeout_s"] > 0

        # -- refusals, all against the live id (none may resolve it)
        assert _post("/approve", {"id": card["id"], "allow": True},
                     origin="https://evil.example") == 403
        assert _post("/approve", {"id": card["id"], "allow": True},
                     content_type="text/plain") == 415
        assert _post("/approve", {"id": "guessed", "allow": True}) == 409
        assert _post("/approve", {"id": card["id"], "allow": "yes"}) == 400
        assert not result, "no refused request may have decided anything"

        # -- the window's own origin is accepted (matched against Host, so a
        # server on a non-default port still works — this test is on 8471)
        assert _post("/approve", {"id": card["id"], "allow": True},
                     origin=f"http://127.0.0.1:{PORT}") == 200
        waiter.join(timeout=5)
        assert result == [True], result
        closed = _await(lambda: _next_kind(events, "approval_closed"))
        assert closed["resolution"] == "approved", closed
        assert _post("/approve", {"id": card["id"], "allow": True}) == 409, "no replay"
        print("ok  http: SSE card -> POST /approve -> the blocked thread wakes approved")
        print("ok  http: cross-origin 403, non-JSON 415, unknown id 409, bad body 400")
    finally:
        broker.deny_all("test-teardown")
        server.shutdown()
        server.server_close()


def block_checks() -> None:
    face = f"http://localhost:{config.FACE_PORT}/jarvis.html"
    policy = BrowserPolicy()
    assert policy.allows(face) is False
    assert BrowserPolicy(allowed_hosts=["*"]).allows(face) is False, "'*' must not open the face"
    assert BrowserPolicy(allowed_hosts=["*"]).allows(
        f"http://foo.localhost:{config.FACE_PORT}/"
    ) is False, "*.localhost resolves to loopback in Chrome"
    assert BrowserPolicy(allowed_hosts=["*"]).allows(
        f"http://127.0.0.2:{config.FACE_PORT}/"
    ) is False, "all of 127/8 is loopback"
    assert policy.allows("http://localhost:8000/page.html") is True, "other local ports still fine"
    assert BrowserPolicy(allowed_hosts=["localhost"]).allows(
        "http://example.com"
    ) is False, "a policy without '*' still pins to its named hosts"
    print("ok  browser: the face's own origin is refused even with allowed_hosts='*'")

    out = tools.dispatch("fetch_page", json.dumps({"url": face}))
    assert "control plane" in out.text, out.text
    print("ok  web: fetch_page refuses the face's origin too")


# -- helpers ----------------------------------------------------------------


def _await(probe, timeout: float = 5.0):
    """Poll until `probe()` returns something truthy, or fail."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = probe()
        except (IndexError, Empty):
            value = None
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out waiting for a condition")


def _subscribe() -> Queue:
    """A real SSE subscriber, delivering parsed events onto a queue."""
    out: Queue = Queue()

    def reader():
        try:
            with urllib.request.urlopen(f"{BASE}/events", timeout=30) as stream:
                for raw in stream:
                    line = raw.decode().strip()
                    if line.startswith("data: "):
                        out.put(json.loads(line[6:]))
        except Exception:
            pass

    threading.Thread(target=reader, daemon=True).start()
    return out


def _next_kind(events: Queue, kind: str):
    """The next event of `kind`, skipping anything else already queued."""
    while True:
        event = events.get_nowait()  # raises Empty -> _await retries
        if event.get("kind") == kind:
            return event["data"]


def _post(path: str, body: dict, origin: str | None = None,
          content_type: str = "application/json") -> int:
    request = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": content_type, **({"Origin": origin} if origin else {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def main() -> int:
    broker_checks()
    dispatch_checks()
    http_checks()
    block_checks()
    print("all approval checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
