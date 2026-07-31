"""Face-server checks for mute and permission controls. Free — no API.

Against a real threaded server: /config reports mute + permissions state,
POST /mute flips voicectl and broadcasts to SSE subscribers, and POST
/approve with always=true both unblocks the waiting agent thread and writes
the allowlist entry.

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
