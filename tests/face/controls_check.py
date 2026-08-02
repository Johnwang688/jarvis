"""Face-server checks for mute and permission controls. Free — no API.

Against a real threaded server: /config reports mute + permissions state,
POST /mute flips voicectl and broadcasts to SSE subscribers, POST /model
re-points the live agent and broadcasts so the CORE readout stays honest
(/config is fetched once at boot, so the broadcast is the only thing keeping
it true), and POST /approve with always=true both unblocks the waiting agent
thread and writes the allowlist entry.

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

from jarvis import config, llm, permissions
from jarvis.tools import voicectl

PORT = 8441

# Pin the capability lookup: these checks are about the route, and a real
# catalog fetch would put the network on the critical path of a free test.
llm.catalog = lambda refresh=False: {  # type: ignore[assignment]
    entry["id"]: {"context_length": 200_000, "vision": True, "tools": True}
    for entry in config.SWITCHABLE.values()
} | {config.TIERS["orchestrator"]: {"context_length": 1_000_000, "vision": True, "tools": True}}


def _post(path: str, payload: dict, origin: str | None = None) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("localhost", PORT, timeout=5)
    body = json.dumps(payload)
    conn.request(
        "POST", path, body,
        {
            "Content-Type": "application/json",
            "Origin": origin or f"http://localhost:{PORT}",
        },
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

            assert cfg["llm"] == config.TIERS["orchestrator"], cfg
            aliases = [entry["alias"] for entry in cfg["roster"]]
            assert aliases[0] == "default" and "opus" in aliases, aliases
            assert any(e["note"] for e in cfg["roster"]), "roster notes must reach the window"

            target = config.SWITCHABLE["opus"]["id"]
            status, data = _post("/model", {"model": target})
            assert status == 200 and data["model"] == target, (status, data)
            msg = json.loads(sse.get(timeout=2))
            assert msg["kind"] == "model" and msg["data"]["model"] == target, msg
            assert server._get_agent().model == target, "the live agent did not move"
            assert _get_config()["llm"] == target, "/config still reports the boot model"

            status, data = _post("/model", {"model": "gpt-4"})
            assert status == 400 and "roster" in data["error"], (status, data)
            assert server._get_agent().model == target, "a refused switch moved the agent"

            status, _ = _post("/model", {"model": "default"}, origin="http://evil.example")
            assert status == 403, f"cross-origin switch was accepted ({status})"
            assert server._get_agent().model == target, "cross-origin switch moved the agent"

            _post("/model", {"model": "default"})
            sse.get(timeout=2)
            assert server._get_agent().model == config.TIERS["orchestrator"]
            print("ok  model: /model switches + broadcasts; off-roster 400, cross-origin 403")

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
