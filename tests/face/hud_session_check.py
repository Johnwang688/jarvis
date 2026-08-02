"""Synthetic checks for the HUD's session picker. Free — no API calls.

Same puppet-string pattern as hud_state_check: the real `jarvis.html` is
served, but `/config`, `/sessions` and `/session` are stubs this test scripts.
The lesson that pattern exists for (CLAUDE.md, the "Fault: HTTP 200" bug) is
that route tests do not cover the window's own JS — and a session switch is
almost entirely window JS: redrawing the log, resetting the counters, and
staying out of the way of a turn.

Run:  .venv/bin/python tests/face/hud_session_check.py
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

PORT = 8476
BASE = f"http://127.0.0.1:{PORT}"

SSE: queue.Queue = queue.Queue()
SWITCHES: list[dict] = []  # what the window POSTed to /session

CURRENT = {
    "id": "20260731-090000-aaaa",
    "title": "TTS latency chase",
    "turns": 2,
    "cost_usd": 0.0125,
    "surface": "face",
    "when": "today 09:00",
    "tail": [
        {"role": "you", "text": "the tts is still like 3 seconds"},
        {"role": "jarvis", "text": "It is the provider, not the model."},
    ],
}

OTHER = {
    "id": "20260730-140000-bbbb",
    "title": "VEX arm assembly",
    "turns": 7,
    "cost_usd": 0.031,
    "surface": "chat",
    "when": "Wed 14:00",
}


class ScriptedHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
            self._json(
                {
                    "llm": "test/model", "stt": "test/stt", "tts": "test/tts",
                    "voice": "test", "speed": 1.0, "session": CURRENT,
                }
            )
            return
        if self.path == "/sessions":
            self._json({"current": {k: v for k, v in CURRENT.items() if k != "tail"},
                        "sessions": [{k: v for k, v in CURRENT.items() if k != "tail"}, OTHER]})
            return
        super().do_GET()

    def do_POST(self):
        if self.path != "/session":
            self.send_error(404)
            return
        data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        SWITCHES.append(data)
        if data.get("new"):
            self._json({"id": "20260731-120000-cccc", "title": "untitled", "turns": 0,
                        "cost_usd": 0.0, "surface": "face", "when": "1m ago", "tail": []})
        else:
            self._json({**OTHER, "tail": [{"role": "you", "text": "insert two c-channels"}]})


def texts(page, selector: str) -> list[str]:
    return page.eval_on_selector_all(selector, "els => els.map(e => e.textContent)")


def run_checks(page) -> None:
    # 1. Boot onto a session that already has history.
    assert page.inner_text("#session-title") == "TTS LATENCY CHASE"
    log = texts(page, "#transcript .msg")
    assert any("still like 3 seconds" in t for t in log), log
    assert any("the provider, not the model" in t for t in log), log
    assert page.inner_text("#stat-turns") == "2"
    assert page.inner_text("#stat-cost") == "$0.0125"
    print("ok  boot: a resumed conversation shows its title, its log, and its counters")

    # 2. The picker lists sessions and marks the live one.
    page.click("#session-row")
    page.wait_for_selector("#sessions.on")
    rows = texts(page, ".sess")
    assert any("VEX arm assembly" in r for r in rows), rows
    here = texts(page, ".sess.here")
    assert len(here) == 1 and "TTS latency chase" in here[0], here
    print("ok  picker: lists recent conversations, marks the current one")

    # 3. Push-to-talk is inert while it is open — talking into a conversation
    #    you are halfway out of loses the turn.
    if page.evaluate("() => !!stream"):
        page.evaluate("() => pressToTalk()")
        assert page.evaluate("() => recorder === null"), "PTT started with the picker open"
        print("ok  picker: push-to-talk is inert while it is open")

    # 4. Switching redraws the log from the session joined, not the one left.
    page.click(".sess:not(.here)")
    page.wait_for_function("() => document.getElementById('session-title').textContent"
                           " === 'VEX ARM ASSEMBLY'", timeout=5_000)
    assert SWITCHES[-1] == {"id": OTHER["id"]}, SWITCHES
    log = texts(page, "#transcript .msg")
    assert any("insert two c-channels" in t for t in log), log
    assert not any("3 seconds" in t for t in log), "the old conversation's log survived a switch"
    assert page.inner_text("#stat-turns") == "7"
    assert page.inner_text("#stat-cost") == "$0.0310"
    assert not page.is_visible("#sessions"), "picker stayed open after a switch"
    print("ok  switch: log redrawn from the joined session, counters follow it")

    # 5. A new session starts empty.
    page.click("#session-row")
    page.wait_for_selector("#sessions.on")
    page.click("#sess-new")
    page.wait_for_function("() => document.getElementById('stat-turns').textContent === '0'",
                           timeout=5_000)
    assert SWITCHES[-1] == {"new": True}, SWITCHES
    assert texts(page, "#transcript .msg") == []
    print("ok  new: NEW SESSION clears the log and the counters")

    # 6. Escape closes the picker without switching anything.
    before = len(SWITCHES)
    page.click("#session-row")
    page.wait_for_selector("#sessions.on")
    page.keyboard.press("Escape")
    page.wait_for_function("() => !document.getElementById('sessions').classList.contains('on')")
    assert len(SWITCHES) == before
    print("ok  picker: Escape closes it, switching nothing")

    # 7. An SSE session event (another window switched) relabels only.
    SSE.put({"kind": "session", "data": {**OTHER, "title": "renamed elsewhere"}})
    page.wait_for_function("() => document.getElementById('session-title').textContent"
                           " === 'RENAMED ELSEWHERE'", timeout=5_000)
    assert texts(page, "#transcript .msg") == [], "an SSE relabel must not redraw the log"
    print("ok  sse: a session broadcast updates the label only")


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
            page.wait_for_function("() => S.booted || S.state === 'error'", timeout=15_000)
            run_checks(page)
            assert not errors, f"page errors: {errors}"
            browser.close()
    finally:
        SSE.put(None)
        server.shutdown()
        server.server_close()
    print("\nall HUD session checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
