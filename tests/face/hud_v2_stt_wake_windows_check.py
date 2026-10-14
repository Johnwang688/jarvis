"""V13: switching cloud fallback off in one HUD window stops Chrome's wake recognizer in the others.

Chrome's `webkitSpeechRecognition` streams the microphone to Google. It may run only while the
owner allows uploads, in EVERY window: a second monitor's window must stop the moment the switch is
flipped in the first, not at its next focus or poll. The mechanism is the daemon's `stt_settings`
event on `/events`; the control run below drops that event and shows the other window keeps
streaming, so the pass above it depends on the event and nothing else (the 60 s poll is a backstop
and is not waited for).

Two pages in one context, one mock daemon that fans every event out to every connection. No real
audio, model, call or live port. Run with temporary HOME, dummy keys, worktree PYTHONPATH, a 3G/0swap
systemd scope and HUD_V2_CHECK_PORT=18475.
"""
from __future__ import annotations
import sys
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
assert not (REPO / ".env").exists(), "use a credential-free worktree"
from playwright.sync_api import sync_playwright
from tests.face.hud_v2_mock import MockDaemon
from tests.face.hud_v2_check import FAKE_RECOGNIZER, guard_live, watch_csp, open_settings
from tests.face.hud_v2_voice_diagnostics_check import FAKE_MIC

COUNT_STARTS = """(() => { const P = window.SpeechRecognition.prototype, s = P.start;
  P.start = function () { window.__srStarts = (window.__srStarts || 0) + 1; return s.call(this); }; })();"""


def main():
    failures: list[str] = []
    count = 0

    def check(name, ok):
        nonlocal count
        count += 1
        print(("ok   " if ok else "FAIL ") + name)
        if not ok:
            failures.append(name)

    mock = MockDaemon(0).start()
    mock.fanout = True
    try:
        base = f"http://127.0.0.1:{mock.port}"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 900})

            def route(r):
                url = urlsplit(r.request.url)
                if url.scheme in ("http", "https") and (url.hostname != "127.0.0.1" or url.port not in {
                        mock.port, mock.workshop_port, mock.api_port, mock.dev_port}):
                    failures.append("request outside isolated mock")
                    r.abort()
                else:
                    r.continue_()
            context.route("**/*", route)
            guard_live(context, failures.append)
            watch_csp(context, failures.append)
            context.add_init_script(FAKE_RECOGNIZER + COUNT_STARTS + FAKE_MIC)
            a, b = context.new_page(), context.new_page()
            for page in (a, b):
                page.on("pageerror", lambda e: failures.append(f"page error {e}"))
                page.goto(base)
                page.wait_for_selector('[data-testid="sidebar"]', state="attached")
            for name, page in (("A", a), ("B", b)):
                page.wait_for_function("() => !!window.__hudRecog", timeout=10000)
                check(f"window {name}: the wake recognizer runs while uploads are allowed", True)
            # Both SSE connections are live (each window got the same frames).
            deadline_ok = False
            for _ in range(60):
                if mock.sse_connections() >= 2:
                    deadline_ok = True
                    break
                a.wait_for_timeout(100)
            check("both windows hold an event stream", deadline_ok)

            def running(page):
                return page.evaluate("!!window.__hudRecog")

            def toggle_in_a():
                open_settings(a, "Voice")
                a.wait_for_selector('[data-testid="stt-cloud-fallback"]')
                a.locator('[data-testid="stt-cloud-fallback"]').click()

            # --- the event: A switches the cloud off, B stops at once
            toggle_in_a()
            a.wait_for_function("() => !window.__hudRecog", timeout=5000)
            b.wait_for_function("() => !window.__hudRecog", timeout=5000)
            check("cloud fallback OFF in window A stops the recognizer in A", not running(a))
            check("and in window B, with no focus or visibility change", not running(b))
            starts = b.evaluate("window.__srStarts || 0")
            b.wait_for_timeout(800)
            check("B does not start it again", b.evaluate("window.__srStarts || 0") == starts and not running(b))
            # --- and back on
            a.locator('[data-testid="stt-cloud-fallback"]').click()
            b.wait_for_function("() => !!window.__hudRecog", timeout=5000)
            check("switching it back on starts the recognizer in B again", running(b) and running(a))

            # --- control: with the event dropped, B keeps streaming (only the slow poll would save it)
            mock.stt_publish = False
            a.locator('[data-testid="stt-cloud-fallback"]').click()
            a.wait_for_function("() => !window.__hudRecog", timeout=5000)
            b.wait_for_timeout(2500)
            check("control: without the event, window B is still streaming (the bug this check guards)", running(b))
            mock.stt_publish = True
            a.locator('[data-testid="stt-cloud-fallback"]').click()      # on
            a.locator('[data-testid="stt-cloud-fallback"]').click()      # off, published this time
            b.wait_for_function("() => !window.__hudRecog", timeout=5000)
            check("and the next published change stops it", not running(b))
            context.close()
            browser.close()
        print(f"{count - len(failures)}/{count} checks passed; {len(failures)} failures")
        if failures:
            print("\n".join(failures))
        return 1 if failures else 0
    finally:
        mock.stop()


if __name__ == "__main__":
    raise SystemExit(main())
