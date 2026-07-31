"""Synthetic checks for the HUD input bar. Free — no API calls.

Drives the real `jarvis.html` in headless Chromium against a scripted
`/converse` (the hud_state_check puppet pattern). What it pins down:

  1. typing + Enter sends a JSON turn with the text, and the words appear in
     the transcript immediately (not when the server echoes them)
  2. staged attachments ride the next send and the staging clears after
  3. an empty box + no files sends nothing
  4. pressing Space while typing does NOT trigger push-to-talk
  5. the turn completes: reply renders, state settles back to idle

Run:  .venv/bin/python tests/face/hud_input_check.py
"""

from __future__ import annotations

import base64
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

PORT = 8477
BASE = f"http://127.0.0.1:{PORT}"

STREAM: queue.Queue = queue.Queue()
BODIES: list[dict] = []


class ScriptedHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            while True:
                time.sleep(3600)
        if self.path == "/config":
            body = json.dumps(
                {"llm": "test/model", "stt": "test/stt", "tts": "test/tts",
                 "voice": "test", "speed": 1.0}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def do_POST(self):
        if self.path != "/converse":
            self.send_error(404)
            return
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        BODIES.append(json.loads(raw))
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        while True:
            line = STREAM.get()
            if line is None:
                return
            self.wfile.write((json.dumps(line) + "\n").encode())
            self.wfile.flush()


def _finish_turn(heard: str, reply: str) -> None:
    STREAM.put({"type": "phase", "phase": "thinking"})
    STREAM.put({"type": "meta", "heard": heard, "reply": reply,
                "ms": {"stt": 0, "agent": 5}, "steps": 1, "cost_usd": 0})
    STREAM.put({"type": "done", "tts_ms": 0, "muted": True})
    STREAM.put(None)


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

            # 1. Typed turn: Enter sends JSON, words show up immediately.
            page.fill("#textin", "hello world")
            page.press("#textin", "Enter")
            _wait(lambda: len(BODIES) == 1, what="typed send")
            assert BODIES[0]["text"] == "hello world", BODIES[0]
            assert BODIES[0]["attachments"] == [] and "audio_b64" not in BODIES[0]
            assert "hello world" in page.inner_text("#transcript")
            assert page.input_value("#textin") == ""
            _finish_turn("hello world", "hi there")
            _wait(lambda: "hi there" in page.inner_text("#transcript"), what="reply render")
            _wait(lambda: page.evaluate("__hud.state()") == "idle", what="idle settle")

            # 2. Staged attachment rides the next send, then clears.
            b64 = base64.b64encode(b"file body").decode()
            page.evaluate(f"__hud.stage('notes.txt', 'text/plain', '{b64}', 9)")
            assert page.evaluate("__hud.stagedCount()") == 1
            assert page.locator(".chip").count() == 1
            assert "notes.txt" in page.inner_text("#chips")
            page.fill("#textin", "with file")
            page.press("#textin", "Enter")
            _wait(lambda: len(BODIES) == 2, what="attachment send")
            att = BODIES[1]["attachments"]
            assert len(att) == 1 and att[0]["name"] == "notes.txt", att
            assert att[0]["data_b64"] == b64 and att[0]["mime"] == "text/plain"
            assert page.evaluate("__hud.stagedCount()") == 0
            assert page.locator(".chip").count() == 0
            _finish_turn("with file", "got it")
            _wait(lambda: page.evaluate("__hud.state()") == "idle", what="idle settle 2")

            # 3. Empty box sends nothing.
            page.press("#textin", "Enter")
            time.sleep(0.3)
            assert len(BODIES) == 2, "empty Enter must not send"

            # 4. Space while typing is a space, not push-to-talk.
            page.click("#textin")
            page.keyboard.press("Space")
            time.sleep(0.3)
            assert page.evaluate("__hud.state()") == "idle", "Space in input started PTT"
            assert page.input_value("#textin") == " "

            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
    print("ok  typed turn sends JSON, transcript updates at send time")
    print("ok  staged files ride the next send and clear after")
    print("ok  empty Enter is inert; Space in the box is not push-to-talk")
    print("\nall hud input checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
