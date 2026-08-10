"""Synthetic checks for progressive reply rendering in the HUD. Free — no API.

hud_state_check's puppet pattern: a scripted `/converse` emits the real NDJSON
shape on cue against the actual `jarvis.html`, so every delta lands exactly
when the test says so.

What must hold:
  1. text appears while he is still writing, not only when the turn completes
  2. the streamed draft is PLAIN TEXT — half a markdown document is not
     markdown, and rendering `**bold` mid-word flickers or mangles
  3. the finished reply *replaces* the draft: one message in the log, rendered
  4. a cancel mid-sentence removes the draft rather than leaving words he
     never finished saying
  5. streamed text cannot inject markup into the window that gates approvals

Run:  .venv/bin/python tests/face/hud_delta_check.py
"""

from __future__ import annotations

import json
import queue
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from playwright.sync_api import sync_playwright  # noqa: E402

from jarvis.face.server import STATIC_DIR  # noqa: E402

PORT = 8479
BASE = f"http://127.0.0.1:{PORT}"
STREAM: queue.Queue = queue.Queue()
SSE: queue.Queue = queue.Queue()


class ScriptedHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/events":
            # Held open on a queue, released with None at teardown. A busy-wait
            # here keeps a handler thread alive past shutdown and hangs the run.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            while True:
                event = SSE.get()
                if event is None:
                    return
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                self.wfile.flush()
        if self.path == "/config":
            body = json.dumps({"llm": "test/model", "stt": "s", "tts": "t",
                               "voice": "v", "speed": 1.0}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def do_POST(self):
        if self.path == "/cancel":
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path != "/converse":
            self.send_error(404)
            return
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        while True:
            line = STREAM.get()
            if line is None:
                return
            self.wfile.write((json.dumps(line) + "\n").encode())
            self.wfile.flush()


def send(page):
    # A Blob, like the real push-to-talk path — and deliberately not returning
    # the promise, since converse() only settles when the whole turn is done.
    page.evaluate(
        "() => { converse(new Blob([new Uint8Array(4096)], {type: 'audio/webm'})); }"
    )


def live_text(page) -> str:
    return page.evaluate("() => (document.querySelector('.msg.live .md')||{}).textContent || ''")


def run_checks(page) -> None:
    # --- 1 & 2: text shows up mid-turn, and as plain text -------------------
    send(page)
    STREAM.put({"type": "phase", "phase": "thinking"})
    STREAM.put({"type": "heard", "text": "explain it"})
    STREAM.put({"type": "delta", "text": "**Done.** Here is "})
    page.wait_for_function("() => document.querySelector('.msg.live')", timeout=5000)
    STREAM.put({"type": "delta", "text": "the answer."})
    page.wait_for_function(
        "() => (document.querySelector('.msg.live .md')||{}).textContent === "
        "'**Done.** Here is the answer.'",
        timeout=5000,
    )
    # Plain text on purpose: the draft must not be rendered, or a partial
    # `**bold` would flicker into markup and back out again.
    assert page.evaluate(
        "() => document.querySelectorAll('.msg.live .md strong').length"
    ) == 0, "the streamed draft was markdown-rendered"
    print("ok  delta: text appears while he is still writing, unrendered")

    # --- 3: the finished reply replaces the draft ---------------------------
    STREAM.put({
        "type": "meta", "heard": "explain it", "reply": "**Done.** Here is the answer.",
        "steps": 1, "cost_usd": 0.0, "ms": {"stt": 1, "agent": 2},
    })
    STREAM.put({"type": "done", "tts_ms": 0})
    STREAM.put(None)  # close this turn's response
    page.wait_for_function("() => !document.querySelector('.msg.live')", timeout=5000)
    replies = page.evaluate(
        "() => [...document.querySelectorAll('.msg.jarvis')].map(e => e.querySelector('.md').textContent)"
    )
    assert replies == ["Done. Here is the answer."], replies
    assert page.evaluate(
        "() => document.querySelectorAll('.msg.jarvis .md strong').length"
    ) == 1, "the finished reply was not markdown-rendered"
    print("ok  delta: the finished reply replaces the draft, rendered once")

    # --- 4: a cancel mid-sentence drops the partial -------------------------
    send(page)
    STREAM.put({"type": "phase", "phase": "thinking"})
    STREAM.put({"type": "delta", "text": "I was about to say someth"})
    page.wait_for_function("() => document.querySelector('.msg.live')", timeout=5000)
    STREAM.put({"type": "cancelled", "ms": {"stt": 0, "agent": 1}})
    STREAM.put(None)
    page.wait_for_function("() => !document.querySelector('.msg.live')", timeout=5000)
    assert "about to say someth" not in page.evaluate("() => $('transcript').textContent"), (
        "a cancelled half-sentence was left in the log as if he had said it"
    )
    print("ok  delta: a cancelled draft is removed, not left in the log")

    # --- 5: streamed text cannot inject markup ------------------------------
    send(page)
    STREAM.put({"type": "phase", "phase": "thinking"})
    STREAM.put({"type": "delta",
                "text": '<img src=x onerror="window.__pwned=1"><a href="javascript:void(0)">x</a>'})
    page.wait_for_function("() => document.querySelector('.msg.live')", timeout=5000)
    assert page.evaluate("() => document.querySelectorAll('.msg.live img, .msg.live a').length") == 0, (
        "streamed text created elements — this is the window that gates approvals"
    )
    assert page.evaluate("() => window.__pwned === undefined"), "streamed text ran script"
    assert "<img" in live_text(page), "the markup should be visible as text"
    STREAM.put({"type": "cancelled", "ms": {"stt": 0, "agent": 1}})
    STREAM.put(None)
    page.wait_for_function("() => !document.querySelector('.msg.live')", timeout=5000)
    print("ok  delta: streamed text is inert — textContent, never innerHTML")


def main() -> int:
    server = ThreadingHTTPServer(
        ("127.0.0.1", PORT), partial(ScriptedHandler, directory=str(STATIC_DIR))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--use-fake-ui-for-media-stream",
                      "--use-fake-device-for-media-stream",
                      "--autoplay-policy=no-user-gesture-required"],
            )
            page = browser.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"{BASE}/jarvis.html", wait_until="load")
            page.wait_for_function("() => window.__hud && window.__hud.state")
            page.wait_for_function("() => S.booted || S.state === 'error'", timeout=15_000)
            run_checks(page)
            assert not errors, f"page errors: {errors}"
            browser.close()
    finally:
        STREAM.put(None)
        SSE.put(None)
        server.shutdown()
        server.server_close()
    print("\nall HUD delta checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
