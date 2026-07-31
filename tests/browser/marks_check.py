"""Synthetic checks for marked screenshots (set-of-marks). Free — no API.

Verifies against the local vocab drill that:
  1. a marked screenshot assigns clickable refs — click("e0") works with no
     snapshot ever taken, which is what vision mode depends on
  2. the badge overlay is removed from the DOM after capture
  3. marks are drawn for every interactive element on a busier screen

Writes the captured PNGs next to this check (git-ignored /tmp path if given)
so a human can eyeball badge placement.

Run:  .venv/bin/python tests/browser/marks_check.py [out_dir]
"""

from __future__ import annotations

import base64
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from jarvis.browser import BrowserPolicy, Session

PAGE_DIR = Path(__file__).resolve().parents[2] / "vocabbench"
PORT = 8399


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), partial(_Quiet, directory=str(PAGE_DIR)))
    threading.Thread(target=server.serve_forever, daemon=True).start()

    policy = BrowserPolicy()
    policy.headless = True
    session = Session(policy)
    try:
        session.goto(f"http://localhost:{PORT}/?seed=42&n=6")

        b64, size, marks = session.screenshot_b64(marked=True)
        assert marks == 1, f"start screen should mark exactly the Start button, got {marks}"
        assert session.eval_js("document.querySelectorAll('#__jarvis-marks').length") == 0, (
            "overlay left in DOM"
        )
        (out_dir / "marks_start.png").write_bytes(base64.b64decode(b64))

        # The real point: refs from a marked screenshot are clickable — no
        # snapshot has ever run in this session.
        session.click("e0")
        assert "Question 1" in session.eval_js("document.body.innerText")

        b64, size, marks = session.screenshot_b64(marked=True)
        assert marks >= 5, f"question screen should mark options + Next, got {marks}"
        assert session.eval_js("document.querySelectorAll('#__jarvis-marks').length") == 0, (
            "overlay left in DOM"
        )
        (out_dir / "marks_question.png").write_bytes(base64.b64decode(b64))

        print(f"ok  marked screenshots: refs clickable without snapshot, overlay cleaned up")
        print(f"    saved {out_dir / 'marks_start.png'} and {out_dir / 'marks_question.png'}")
    finally:
        session.stop(trace_name="marks-check")
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
