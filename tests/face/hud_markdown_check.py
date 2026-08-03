"""Checks the HUD renders his markdown instead of printing the source.

Free — no API calls. Serves the real `jarvis.html` (with /config and /events
stubbed, hud_state_check's pattern) and pushes replies through the real
`addMsg`, so this tests the window's own JS rather than a copy of it.

What it pins down:
  1. every construct the model actually emits becomes an element
  2. the owner's own words are NOT rendered — typing *foo* means *foo*
  3. `textContent` still contains the words, which is what the other HUD
     suites assert against
  4. nothing a reply contains can become markup this page did not build:
     an <img onerror> in a reply stays text, and a javascript: link is
     rendered without an href. The HUD is the surface that gates
     approvals — a reply that could inject a button into it could
     approve itself.

Run:  .venv/bin/python tests/face/hud_markdown_check.py
"""

from __future__ import annotations

import json
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from playwright.sync_api import sync_playwright  # noqa: E402

from jarvis.face.server import STATIC_DIR  # noqa: E402

PORT = 8477
BASE = f"http://127.0.0.1:{PORT}"


class StaticHandler(SimpleHTTPRequestHandler):
    """The real HUD, with the two routes it calls at boot stubbed out."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            while not SHUTDOWN.wait(0.1):
                pass
            return
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


SHUTDOWN = threading.Event()


def render(page, text: str) -> dict:
    """Push one reply through the real addMsg; report the last message."""
    return page.evaluate(
        """(t) => {
            document.getElementById('transcript').textContent = '';
            window.__hud.addMsg('jarvis', t);
            const el = document.querySelector('#transcript .msg.jarvis .md');
            return {html: el.innerHTML, text: el.textContent};
        }""",
        text,
    )


def run_checks(page) -> None:
    # 1. the constructs he actually writes
    cases = [
        ("**Done.** I checked *three* things.",
         ["<strong>Done.</strong>", "<em>three</em>"]),
        ("# Report", ['class="md-h md-h1"']),
        ("- one\n- two", ["<ul>", "<li>one</li>", "<li>two</li>"]),
        ("1. first\n2. second", ["<ol>", "<li>first</li>"]),
        ("- top\n  - nested", ["<ul><li>top<ul><li>nested</li>"]),
        ("Run `context.py` first.", ["<code>context.py</code>"]),
        ("```py\nx = 1\n```", ["<pre>x = 1</pre>"]),
        ("> quoted", ["<blockquote>"]),
        ("---", ["<hr>"]),
        ("| name | qty |\n|---|---|\n| bolt | 12 |",
         ["<table>", "<th>name</th>", "<td>bolt</td>"]),
        ("See [the docs](https://example.com).",
         ['<a href="https://example.com" target="_blank"']),
        ("***both***", ["<strong><em>both</em></strong>"]),
        ("~~struck~~", ["<s>struck</s>"]),
    ]
    for source, wanted in cases:
        html = render(page, source)["html"]
        for fragment in wanted:
            assert fragment in html, f"{source!r} -> {html!r}, wanted {fragment!r}"
    print(f"ok  markdown: {len(cases)} constructs render as elements, not source")

    # 2. no raw markdown left where it used to be, and the words survive as
    #    textContent — the other HUD suites read messages that way.
    out = render(page, "**Done.** Fixed `context.py` in *one* pass.")
    assert "*" not in out["text"] and "`" not in out["text"], out["text"]
    assert "Done. Fixed context.py in one pass." == out["text"], out["text"]
    print("ok  markdown: syntax gone from the log, words intact in textContent")

    # 3. prose that only looks like markdown is not mangled
    for plain in ["Use snake_case_name here.", "That is 2 * 3 * 4 = 24."]:
        assert render(page, plain)["text"] == plain, plain
    print("ok  markdown: snake_case and bare asterisks left alone")

    # 4. the owner's own words go up verbatim
    typed = page.evaluate(
        """(t) => {
            document.getElementById('transcript').textContent = '';
            window.__hud.addMsg('you', t);
            const el = document.querySelector('#transcript .msg.you');
            return {html: el.innerHTML, text: el.textContent};
        }""",
        "send **exactly** this",
    )
    assert "<strong>" not in typed["html"], typed["html"]
    assert "send **exactly** this" in typed["text"], typed["text"]
    print("ok  markdown: the owner's own message is not rendered")

    # 5. a reply cannot inject markup into the surface that gates approvals
    hostile = render(page, '<img src=x onerror="window.__pwned=1"> <b>hi</b>')
    assert "<img" not in hostile["html"] and "<b>" not in hostile["html"], hostile["html"]
    assert page.evaluate("() => window.__pwned") is None
    link = render(page, "[click](javascript:window.__pwned=1)")
    assert "<a" not in link["html"], link["html"]
    assert "javascript:" in link["text"], link["text"]  # shown, not clickable
    print("ok  markdown: no injected markup, no javascript: href")


def main() -> int:
    server = ThreadingHTTPServer(
        ("127.0.0.1", PORT), partial(StaticHandler, directory=str(STATIC_DIR))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--use-fake-ui-for-media-stream",
                      "--use-fake-device-for-media-stream"],
            )
            page = browser.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"{BASE}/jarvis.html", wait_until="load")
            page.wait_for_function("() => window.__hud && window.__hud.addMsg")
            run_checks(page)
            assert not errors, f"page errors: {errors}"
            browser.close()
    finally:
        SHUTDOWN.set()
        server.shutdown()
        server.server_close()

    print("\nall HUD markdown checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
