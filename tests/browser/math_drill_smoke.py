"""Manual browser smoke test: complete the math drill page.

Serves tests/browser/pages/math-drill on localhost and runs one agent turn
asking Jarvis to click through it, answer the four questions, and submit.
Makes real API calls (a fraction of a cent).

The browser window stays open after the run (KEEP_OPEN seconds, default 1800)
so a human can click "Copy JSON" on the results screen and grade the answers.

Run:  .venv/bin/python tests/browser/math_drill_smoke.py
"""

from __future__ import annotations

import os
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from jarvis import agent as agent_mod
from jarvis.browser import SESSION

PAGE_DIR = Path(__file__).resolve().parent / "pages" / "math-drill"
PORT = int(os.environ.get("DRILL_PORT", "8123"))

TASK = (
    f"Open http://localhost:{PORT}/ and complete the math practice drill. "
    "Click Start, then answer all 4 questions correctly and submit. The page "
    "re-renders after every click, so take a fresh snapshot before each action. "
    "Stop when you reach the results screen that shows the answers as JSON — "
    "do NOT click the Copy JSON button, a human will do that. "
    "Finish by listing the answers you gave."
)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # keep the run transcript readable
        pass


def main() -> int:
    server = ThreadingHTTPServer(
        ("127.0.0.1", PORT), partial(QuietHandler, directory=str(PAGE_DIR))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"serving {PAGE_DIR} at http://localhost:{PORT}/")

    def on_event(kind, data):
        if kind == "tool_start":
            name, raw = data
            print(f"  -> {name}({raw[:120]})", flush=True)
        elif kind == "text" and data:
            print(f"\njarvis: {data}", flush=True)

    browser_tools = [
        "browser_goto",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_screenshot",
    ]
    jarvis = agent_mod.Agent(tool_names=browser_tools, max_steps=24, on_event=on_event)
    print(f"model: {jarvis.model}")

    turn = jarvis.run_turn(TASK)
    print(
        f"\ndone: {turn.steps} step(s) · {turn.latency_s:.1f}s · ${turn.cost_usd:.4f}"
        + (" · STOPPED EARLY" if turn.stopped_early else "")
    )

    keep_open = int(os.environ.get("KEEP_OPEN", "1800"))
    print(f"\nbrowser stays open {keep_open}s — click 'Copy JSON' in the window.")
    try:
        if sys.stdin.isatty():
            input("press Enter to close the browser... ")
        else:
            time.sleep(keep_open)
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        trace = SESSION.stop(trace_name="math-drill-smoke")
        server.shutdown()
        server.server_close()
        if trace:
            print(f"trace: {trace}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
