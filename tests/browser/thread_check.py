"""The browser must be usable from any thread and park no event loop on its
callers. Free — no API calls.

Guards two real failure modes of Playwright's sync API, which is thread-
affine and parks a *running* asyncio loop on whichever thread starts it:

  * 2026-07-31 chat-REPL crash: after the first browser tool ran inside
    `jarvis chat`, the next prompt died with "asyncio.run() cannot be called
    from a running event loop" — prompt_toolkit refuses to prompt on a
    thread that has a live loop.
  * The face answers every request on a fresh thread (ThreadingHTTPServer),
    so without a dedicated browser thread, a voice turn that browses would
    break every later browser call.

The fix under test: Session marshals all work onto its own thread
(Session._submit), so callers never host the loop and any thread may call.

Run:  .venv/bin/python tests/browser/thread_check.py
"""

from __future__ import annotations

import asyncio
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis.browser import BrowserError, BrowserPolicy, Session

PAGE_DIR = Path(__file__).resolve().parent / "pages" / "math-drill"
PORT = 8433


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def _in_thread(fn):
    """Run fn on a fresh thread, re-raising anything it raised."""
    result: list = [None, None]

    def runner():
        try:
            result[0] = fn()
        except BaseException as exc:
            result[1] = exc

    t = threading.Thread(target=runner)
    t.start()
    t.join()
    if result[1] is not None:
        raise result[1]
    return result[0]


def main() -> int:
    server = ThreadingHTTPServer(
        ("127.0.0.1", PORT), partial(_Quiet, directory=str(PAGE_DIR))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()

    policy = BrowserPolicy(allowed_hosts=["localhost", "127.0.0.1"], headless=True)
    session = Session(policy)
    try:
        # Main thread can drive the browser…
        session.goto(f"http://localhost:{PORT}/")

        # …without inheriting Playwright's event loop. This is the exact
        # condition that crashed the chat REPL.
        try:
            asyncio.get_running_loop()
            raise AssertionError("an event loop is parked on the calling thread")
        except RuntimeError:
            pass
        print("ok  no event loop parked on the caller after browser use")

        # prompt_toolkit — the thing that actually died — works afterwards.
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from prompt_toolkit.shortcuts import PromptSession

        with create_pipe_input() as pipe:
            pipe.send_text("hello\n")
            answer = PromptSession(input=pipe, output=DummyOutput()).prompt("you ")
        assert answer == "hello", answer
        print("ok  prompt_toolkit prompts fine after a browser action")

        # Fresh threads (the face's request threads) can keep using the same
        # session, including one that started nothing itself.
        snap = _in_thread(lambda: session.snapshot())
        assert "INTERACTIVE" in snap
        again = _in_thread(lambda: session.goto(f"http://127.0.0.1:{PORT}/"))
        assert "Loaded" in again
        print("ok  two fresh threads reused the same browser session")

        # Errors cross the thread boundary intact.
        try:
            _in_thread(lambda: session.click("e999"))
            raise AssertionError("bogus ref did not raise")
        except BrowserError as exc:
            assert "no element" in str(exc)
        print("ok  BrowserError propagates to the calling thread")
    finally:
        session.stop(trace_name="thread-check")
        server.shutdown()
        server.server_close()

    print("\nall thread checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
