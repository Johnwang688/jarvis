"""Synthetic checks for the dangerous-tool approval gate. Free — no API calls.

Covers, in three layers:

  1. the broker (jarvis/face/approvals.py) with a fake window — approve, deny,
     timeout, nowhere-to-ask, replay, double-answer, window-closed
  2. the HTTP route against a real server on a spare port — a real SSE
     subscriber receives the card, POSTs the answer, and the blocked thread
     wakes with it; plus the same-origin and content-type refusals
  3. the self-approval block — the browser and web tools must refuse the
     face's own origin, or the agent could open its own HUD and click its own
     AUTHORIZE button — and, since BR-0, every port in `config.control_ports()`
     (v2's API listener serves the same HUD page), in every loopback spelling,
     while the workshop and ordinary dev ports stay open

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

import httpx  # noqa: E402

from jarvis import config, runtime, tools  # noqa: E402
from jarvis.browser import BrowserPolicy  # noqa: E402
from jarvis.face import server as face_server  # noqa: E402
from jarvis.face.approvals import ApprovalBroker  # noqa: E402

# Not the face's real port: the server under test is disposable, and binds
# port 0 (`http_checks` sets these), since a fixed port failed whenever
# another run held it.
PORT = 0
BASE = ""


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
    global PORT, BASE
    server = face_server.create_server(0)
    PORT = server.server_address[1]
    assert PORT not in (8402, 8403, 8405), "never the live ports"
    BASE = f"http://127.0.0.1:{PORT}"
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


# Every spelling of "this machine" a browser connects to locally. The first
# seven are what `is_loopback` always knew; the rest are BR-0's additions.
LOOPBACK_SPELLINGS = (
    "localhost", "LOCALHOST", "127.0.0.1", "127.0.0.2", "[::1]", "0.0.0.0", "foo.localhost",
    "localhost.", "127.1", "0x7f000001", "2130706433", "0177.0.0.1", "[::ffff:127.0.0.1]",
    "[0:0:0:0:0:0:0:1]", "[::]", "0",
)


def control_port_checks() -> None:
    """BR-0: every Jarvis control port is refused, not only the face's.

    v2's API listener (8405) serves the very same HUD page as the face's port,
    and on a49423c `is_face_origin` refused only 8402 — so v1's browser and
    `fetch_page` could load the HUD there. Each assertion on 8404/8405/8406
    fails on a49423c; the numeric spellings fail there on every port.
    """
    ports = config.control_ports()
    assert {8402, 8404, 8405, 8406} <= ports, ports
    assert 8403 not in ports, "the workshop stays reachable: v1's designer screenshots its own work"
    default = BrowserPolicy()
    star = BrowserPolicy(allowed_hosts=["*"])
    for port in (8402, 8404, 8405, 8406):
        for host in LOOPBACK_SPELLINGS:
            for scheme in ("http", "https"):
                url = f"{scheme}://{host}:{port}/"
                assert config.is_face_origin(url) is True, url
                assert default.allows(url) is False, url
                assert star.allows(url) is False, url
        assert config.is_face_origin(f"http://localhost:{port}/approvals/abcd") is True
    print("ok  control ports: 8402, 8404, 8405 and 8406 refused in "
          f"{len(LOOPBACK_SPELLINGS)} loopback spellings, http and https")

    for port in (8403, 3000, 5173, 8000, 8080, 8431):
        for host in ("localhost", "127.0.0.1", "foo.localhost", "[::1]"):
            url = f"http://{host}:{port}/"
            assert config.is_face_origin(url) is False, url
        assert default.allows(f"http://localhost:{port}/") is True, port
        assert default.allows(f"http://127.0.0.1:{port}/") is True, port
    assert config.is_face_origin("http://localhost/") is False, "no port is 80, not a control port"
    assert config.is_face_origin("http://93.184.216.34:8405/") is False, "a public host is not us"
    assert config.is_face_origin("http://example.com:8405/") is False
    assert config.is_face_origin("http://localhost:99999/") is True, "unparseable local port: refuse"
    print("ok  control ports: the workshop (8403) and ordinary dev ports stay open")

    real_get = httpx.get

    def must_not_fetch(*args, **kwargs):
        raise AssertionError("fetch_page reached a control-plane port")

    httpx.get = must_not_fetch
    try:
        for port in (8402, 8404, 8405, 8406):
            for host in ("localhost", "127.0.0.1", "[::1]", "foo.localhost"):
                out = tools.dispatch("fetch_page", json.dumps({"url": f"http://{host}:{port}/"}))
                assert "control plane" in out.text, (host, port, out.text)
    finally:
        httpx.get = real_get
    print("ok  web: fetch_page refuses every control port before a byte moves")


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
    control_port_checks()
    print("all approval checks passed")
    return 0


if __name__ == "__main__":
    # dispatch() refuses when no toolset is bound (PR #34's follow-up), so
    # this suite says what it calls directly — and is held to it.
    with runtime.tools_bound({"fetch_page", "run_command"}):
        sys.exit(main())
