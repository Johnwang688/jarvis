"""Synthetic checks for the HUD's turn state machine. Free — no API calls.

The real `/converse` costs money and takes seconds, which is exactly why the
state machine was never tested: this serves a *scripted* turn instead — a
stub `/converse` that emits the real NDJSON shape on cue, and a stub `/events`
that emits tool traffic on cue — and drives the actual `jarvis.html`.

What it pins down (all of it regression from 2026-07-31 complaints):

  1. every phase shows immediately: SENDING -> TRANSCRIBING -> THINKING
  2. what you said appears when STT lands, not when the whole turn ends
  3. a finished tool stops being displayed as running — the window goes back
     to THINKING instead of sitting on "RUNNING · X"
  4. TTS is its own visible phase, so the last tool is not still on screen
     while the voice is being synthesized
  5. the elapsed clock ticks during the wait and stops once he is speaking
  6. a tool event that arrives late cannot overwrite a later phase

Run:  .venv/bin/python tests/face/hud_state_check.py
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

PORT = 8474
BASE = f"http://127.0.0.1:{PORT}"

# The turn is driven from the test: each put() releases one more line.
STREAM: queue.Queue = queue.Queue()
SSE: queue.Queue = queue.Queue()
CANCELS: list[str] = []  # /cancel posts the window made


class ScriptedHandler(SimpleHTTPRequestHandler):
    """Serves the real HUD, but with /converse and /events on puppet strings."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/events":
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
        if self.path == "/cancel":
            CANCELS.append(self.headers.get("Origin", ""))
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
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
            # Boot (getUserMedia) settles asynchronously; a turn started
            # before it finishes is a different test than this one.
            page.wait_for_function("() => S.booted || S.state === 'error'", timeout=15_000)
            run_checks(page)
            assert not errors, f"page errors: {errors}"
            browser.close()
    finally:
        STREAM.put(None)
        SSE.put(None)
        server.shutdown()
        server.server_close()

    print("all HUD state checks passed")
    return 0


def status(page) -> str:
    return page.evaluate("() => window.__hud.status()")


def wait_for(probe, what: str, timeout: float = 5.0):
    """Poll a Python-side condition (the stub server's records)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = probe()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def wait_status(page, fragment: str, timeout: int = 8_000) -> None:
    page.wait_for_function(
        "f => window.__hud.status().includes(f)", arg=fragment, timeout=timeout
    )


def run_checks(page) -> None:
    # Kick off a turn with a dummy blob; the server side is on puppet strings.
    # Deliberately not returning the promise — converse() only settles at the
    # end of the turn, which this test is the one feeding.
    page.evaluate("() => { converse(new Blob([new Uint8Array(4096)], {type: 'audio/webm'})); }")

    # 1. the upload is visible before the server has said anything at all
    wait_status(page, "SENDING")
    STREAM.put({"type": "phase", "phase": "transcribing"})
    wait_status(page, "TRANSCRIBING")
    print("ok  state: SENDING before the first server byte, then TRANSCRIBING")

    # 2. the transcript lands at STT, not at the end of the turn
    STREAM.put({"type": "heard", "text": "check my mail", "ms": {"stt": 400}})
    page.wait_for_function(
        "() => [...document.querySelectorAll('#transcript .msg.you')]"
        ".some(m => m.textContent.includes('check my mail'))",
        timeout=8_000,
    )
    STREAM.put({"type": "phase", "phase": "thinking"})
    wait_status(page, "THINKING")
    print("ok  state: your words appear at STT, mid-turn, not after the reply")

    # 5a. the clock is running and actually advances
    first = _clock(page)
    page.wait_for_timeout(400)
    assert _clock(page) > first, "the elapsed clock is not ticking"
    print(f"ok  clock: ticking during the wait ({first:.1f}s -> {_clock(page):.1f}s)")

    # 3. a tool starts, then finishes -> back to THINKING, not stuck on RUNNING
    SSE.put({"kind": "tool", "data": {"name": "gmail_search", "args": "is:unread"}})
    wait_status(page, "RUNNING · GMAIL_SEARCH")
    assert page.locator(".op.running").count() == 1
    SSE.put({"kind": "tool_done", "data": {"name": "gmail_search", "ok": True}})
    wait_status(page, "THINKING")
    assert page.locator(".op.running").count() == 0
    assert page.locator(".op .took").first.text_content().strip(), "no duration recorded"
    print("ok  state: a finished tool releases the status back to THINKING")

    # 4. TTS gets its own phase — the tool label is gone before audio starts
    STREAM.put({"type": "meta", "heard": "check my mail", "reply": "Two unread.",
                "ms": {"stt": 400, "agent": 2200}, "steps": 3, "cost_usd": 0.0004})
    STREAM.put({"type": "phase", "phase": "composing"})
    wait_status(page, "SYNTHESIZING VOICE")
    assert "GMAIL_SEARCH" not in status(page), status(page)
    assert page.evaluate("() => window.__hud.state()") == "composing"
    print("ok  state: SYNTHESIZING VOICE while TTS runs — no stale tool label")

    # 6. a late tool event must not overwrite the composing phase
    SSE.put({"kind": "tool", "data": {"name": "late_tool", "args": "x"}})
    page.wait_for_timeout(250)
    assert page.evaluate("() => window.__hud.state()") == "composing", status(page)
    assert page.locator('.op[data-tool="late_tool"]').count() == 1, "it should still be logged"
    print("ok  state: a late tool event is logged but cannot rewind the phase")

    # 5b. the clock stops once he is actually talking
    page.evaluate("() => setState('speaking')")
    assert status(page) == "RESPONDING", status(page)
    assert _clock(page) == 0.0, "the clock must stop when the wait is over"
    print("ok  clock: stops once Jarvis is speaking")

    STREAM.put({"type": "done", "tts_ms": 700})
    STREAM.put(None)
    cancel_checks(page)


def cancel_checks(page) -> None:
    """Seeing a bad transcript is only useful if you can act on it."""
    # Let the previous turn finish unwinding first: its stream-close cleanup
    # runs a tick later and would otherwise land on top of this turn's status.
    page.wait_for_function("() => S.state === 'idle' && !S.busy", timeout=8_000)
    page.evaluate("() => { converse(new Blob([new Uint8Array(4096)], {type: 'audio/webm'})); }")
    wait_status(page, "SENDING")
    STREAM.put({"type": "phase", "phase": "transcribing"})
    STREAM.put({"type": "heard", "text": "delete the wrong file", "ms": {"stt": 300}})
    STREAM.put({"type": "phase", "phase": "thinking"})
    wait_status(page, "THINKING")

    # what he misheard is on screen while he is still thinking about it
    page.wait_for_function(
        "() => [...document.querySelectorAll('#transcript .msg.you')]"
        ".some(m => m.textContent.includes('delete the wrong file'))",
        timeout=8_000,
    )
    assert page.evaluate("() => S.busy") is True
    assert "INTERRUPT" in page.text_content("#hint"), page.text_content("#hint")

    # take it back: press-to-talk mid-thought. The recorder is cleared by hand
    # so this does not turn into a second /converse.
    page.evaluate("() => { pressToTalk(); if (recorder) { recorder.stop(); recorder = null; } }")
    wait_for(lambda: CANCELS, "the window to POST /cancel")
    assert page.evaluate("() => S.busy") is False, "the turn must be released"
    print("ok  interrupt: press-to-talk mid-THINKING cancels the turn server-side")

    page.evaluate("() => setState('idle')")
    assert "TO SPEAK" in page.text_content("#hint")
    print("ok  interrupt: the hint says INTERRUPT mid-turn and reverts when idle")
    STREAM.put(None)


def _clock(page) -> float:
    """The '· 1.2s' suffix the status is showing, or 0.0 if there is none."""
    text = status(page)
    if "·" not in text:
        return 0.0
    tail = text.rsplit("·", 1)[1].strip()
    return float(tail[:-1]) if tail.endswith("s") and tail[:-1].replace(".", "").isdigit() else 0.0


if __name__ == "__main__":
    sys.exit(main())
