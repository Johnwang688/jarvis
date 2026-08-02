"""Synthetic checks for the HUD's model row. Free — no API calls.

Drives the real `jarvis.html` in headless Chromium against a scripted
`/config`, `/model` and `/events` (the hud_state_check puppet pattern). The
reason this exists rather than trusting controls_check: `/config` is fetched
exactly once, at boot, so every later truth about which model is answering
arrives over SSE. A route test cannot see the readout go stale.

  1. the CORE readout renders the live model from /config's roster
  2. a roster note (off-policy hosting) is visible on the row, not buried
     in a feed that scrolls — it colors the readout and sets the tooltip
  3. clicking CORE POSTs the *next* roster entry — the row cycles
  4. the readout moves only on the SSE broadcast, so a spoken "switch to
     Opus" updates it exactly like a click does
  5. a refused switch (off-roster, 400) surfaces in OPERATIONS and leaves
     the readout alone

Run:  .venv/bin/python tests/face/hud_model_check.py
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from playwright.sync_api import sync_playwright  # noqa: E402

from jarvis.face.server import STATIC_DIR  # noqa: E402

PORT = 8479
BASE = f"http://127.0.0.1:{PORT}"

ROSTER = [
    {"alias": "default", "id": "openai/gpt-5.6-luna", "note": ""},
    {"alias": "opus", "id": "anthropic/claude-opus-5", "note": ""},
    {"alias": "kimi", "id": "moonshotai/kimi-k3", "note": "China-hosted (Moonshot)"},
]

EVENTS: queue.Queue = queue.Queue()
SWITCHES: list[dict] = []
REFUSE = {"on": False}


class ScriptedHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            while True:
                event = EVENTS.get()
                if event is None:
                    return
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                self.wfile.flush()
        if self.path == "/config":
            self._json(
                {
                    "llm": ROSTER[0]["id"],
                    "roster": ROSTER,
                    "stt": "test/stt",
                    "tts": "test/tts",
                    "voice": "test",
                    "speed": 1.0,
                    "muted": True,
                    "permissions": "ask",
                    "allowlist": 0,
                }
            )
            return
        super().do_GET()

    def do_POST(self):
        if self.path != "/model":
            self.send_error(404)
            return
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        payload = json.loads(raw or b"{}")
        SWITCHES.append(payload)
        if REFUSE["on"]:
            self._json({"error": "'gpt-4' is not on the switchable roster."}, code=400)
            return
        self._json({"ok": True, "model": payload["model"], "report": "Switched."})

    def _json(self, body: dict, code: int = 200) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _wait(predicate, timeout=5.0, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def main() -> int:
    server = ThreadingHTTPServer(
        ("127.0.0.1", PORT), partial(ScriptedHandler, directory=str(STATIC_DIR))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                      "--autoplay-policy=no-user-gesture-required"],
            )
            page = browser.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"{BASE}/jarvis.html", wait_until="load")
            page.wait_for_function("() => window.__hud && window.__hud.state")
            _wait(lambda: page.inner_text("#cfg-llm") != "—", what="config load")

            # 1. The boot readout is the live model, short name.
            assert page.inner_text("#cfg-llm") == "gpt-5.6-luna", page.inner_text("#cfg-llm")
            assert page.eval_on_selector("#cfg-llm", "e => e.style.color") == ""
            print("ok  readout: CORE shows the live model at boot")

            # 2. Click cycles to the next roster entry.
            page.click("#model-row")
            _wait(lambda: len(SWITCHES) == 1, what="model POST")
            assert SWITCHES[0]["model"] == ROSTER[1]["id"], SWITCHES
            print("ok  click: CORE posts the next roster entry")

            # 3. The readout moves on the broadcast, not on the click — which
            #    is what makes a spoken switch update it identically.
            assert page.inner_text("#cfg-llm") == "gpt-5.6-luna", "readout moved without SSE"
            EVENTS.put({"kind": "model", "data": {"model": ROSTER[1]["id"], "report": "Switched."}})
            _wait(lambda: page.inner_text("#cfg-llm") == "claude-opus-5", what="SSE readout")
            assert "Switched." in page.inner_text("#opslist")
            print("ok  broadcast: SSE moves the readout and logs to OPERATIONS")

            # 4. A note-carrying model flags itself on the row.
            EVENTS.put(
                {"kind": "model", "data": {"model": ROSTER[2]["id"], "report": "Switched to kimi."}}
            )
            _wait(lambda: page.inner_text("#cfg-llm") == "kimi-k3", what="kimi readout")
            assert page.eval_on_selector("#cfg-llm", "e => e.style.color") != "", "note not flagged"
            assert "Moonshot" in page.get_attribute("#model-row", "title")
            print("ok  note: an off-policy model colors the readout and sets the tooltip")

            # 5. A refusal is surfaced and does not move the readout.
            REFUSE["on"] = True
            page.click("#model-row")
            _wait(lambda: len(SWITCHES) == 2, what="refused POST")
            _wait(lambda: "not on the switchable roster" in page.inner_text("#opslist"),
                  what="refusal in OPERATIONS")
            assert page.inner_text("#cfg-llm") == "kimi-k3", "a refused switch moved the readout"
            print("ok  refusal: a 400 shows in OPERATIONS and leaves the readout alone")

            assert not errors, f"page errors: {errors}"
            EVENTS.put(None)
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
    print("\nall HUD model checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
