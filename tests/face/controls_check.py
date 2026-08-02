"""Face-server checks for mute and permission controls. Free — no API.

Against a real threaded server: /config reports mute + permissions state,
POST /mute flips voicectl and broadcasts to SSE subscribers, POST /approve
with always=true both unblocks the waiting agent thread and writes the
allowlist entry, and POST /session switches which saved conversation the
agent is bound to (rebuilding it) while /sessions lists them.

Run:  .venv/bin/python tests/face/controls_check.py
"""

from __future__ import annotations

import http.client
import json
import queue
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis import config, permissions
from jarvis.tools import voicectl

PORT = 8441


def _post(path: str, payload: dict) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("localhost", PORT, timeout=5)
    body = json.dumps(payload)
    conn.request(
        "POST", path, body,
        {"Content-Type": "application/json", "Origin": f"http://localhost:{PORT}"},
    )
    response = conn.getresponse()
    data = json.loads(response.read() or b"{}")
    conn.close()
    return response.status, data


def _get_config() -> dict:
    conn = http.client.HTTPConnection("localhost", PORT, timeout=5)
    conn.request("GET", "/config")
    data = json.loads(conn.getresponse().read())
    conn.close()
    return data


def _session_checks(server, sse: queue.Queue, tmp: str) -> None:
    """POST /session switches the conversation the agent is bound to."""
    from jarvis import sessions

    sessions.AUTO_TITLE = False
    config.SESSIONS_DIR = Path(tmp) / "sessions"
    config.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    first = sessions.new("face")
    # Give it a turn: a conversation with nothing said in it is not on disk,
    # so it could not be switched back to (by design — see sessions.new).
    first.record(
        "check the mail",
        types.SimpleNamespace(text="nothing new", cost_usd=0.0),
        [{"role": "system", "content": "s"}, {"role": "user", "content": "check the mail"}],
    )
    server.set_session(first)
    while not sse.empty():
        sse.get_nowait()

    bound = server._get_agent()  # builds an agent around `first`
    assert bound.session is not None and bound.session.id == first.id

    status, data = _post("/session", {"new": True})
    assert status == 200 and data["id"] != first.id, data
    assert data["turns"] == 0 and data["tail"] == [], data
    msg = json.loads(sse.get(timeout=2))
    assert msg["kind"] == "session" and msg["data"]["id"] == data["id"], msg
    # The agent is rebuilt around the new transcript, not reused.
    fresh = server._get_agent()
    assert fresh is not bound and fresh.session.id == data["id"]

    status, data = _post("/session", {"id": first.id})
    assert status == 200 and data["id"] == first.id, data
    assert _get_config()["session"]["id"] == first.id
    assert _post("/session", {"id": "no-such-session"})[0] == 404
    assert _post("/session", {})[0] == 400

    conn = http.client.HTTPConnection("localhost", PORT, timeout=5)
    conn.request(
        "POST", "/session", json.dumps({"new": True}),
        {"Content-Type": "application/json", "Origin": "http://evil.example"},
    )
    assert conn.getresponse().status == 403, "cross-origin session switch allowed"
    conn.close()

    conn = http.client.HTTPConnection("localhost", PORT, timeout=5)
    conn.request("GET", "/sessions")
    listing = json.loads(conn.getresponse().read())
    conn.close()
    assert listing["current"]["id"] == first.id
    assert {s["id"] for s in listing["sessions"]} >= {first.id}
    print("ok  sessions: /session switches and rebuilds the agent, /sessions lists, 403/404/400")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        config.ALLOWLIST_PATH = Path(tmp) / "allowlist.json"
        from jarvis.face import server

        srv = server.create_server(port=PORT)
        sse: queue.Queue = queue.Queue(maxsize=50)
        with server._subs_lock:
            server._subscribers.append(sse)
        try:
            cfg = _get_config()
            assert cfg["muted"] is False and cfg["permissions"] == "ask", cfg
            assert cfg["allowlist"] == 0, cfg

            status, data = _post("/mute", {"muted": True})
            assert status == 200 and data["muted"] is True
            assert voicectl.is_muted() is True
            msg = json.loads(sse.get(timeout=2))
            assert msg["kind"] == "mute" and msg["data"]["muted"] is True, msg
            assert _get_config()["muted"] is True
            _post("/mute", {"muted": False})
            sse.get(timeout=2)
            assert voicectl.is_muted() is False
            print("ok  mute: /mute flips voicectl, broadcasts, and shows in /config")

            result = {}

            def agent_side():
                result["allowed"] = server.APPROVALS.request(
                    "run_command", {"command": "git fetch"}
                )

            thread = threading.Thread(target=agent_side)
            thread.start()
            card = None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and card is None:
                msg = json.loads(sse.get(timeout=2))
                if msg["kind"] == "approval":
                    card = msg["data"]
            assert card, "no approval card broadcast"

            status, data = _post("/approve", {"id": card["id"], "allow": True, "always": True})
            assert status == 200 and data["allowed"] is True, (status, data)
            thread.join(timeout=5)
            assert result["allowed"] is True
            assert permissions.allows("run_command", {"command": "git pull"}), "entry missing"
            assert _get_config()["allowlist"] == 1
            print("ok  approve: ALWAYS unblocks the agent and persists the allowlist entry")

            _session_checks(server, sse, tmp)
        finally:
            with server._subs_lock:
                if sse in server._subscribers:
                    server._subscribers.remove(sse)
            srv.shutdown()
            srv.server_close()
    print("\nall control checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
