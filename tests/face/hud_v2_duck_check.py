"""V15: the HUD holds a short lease while Jarvis speaks, in a real browser against the mock daemon.

The mock's `/speech/duck` answers like the daemon's route (one boolean, a 3 s lease). It lowers
nothing; this proves only the window's half: a request when speech turns active, renewals while
it stays so, `off` when it ends, the Settings switch (default on) and that the lease simply stops
when the window goes away. The lowering itself is `tests/v2/speech_duck_check.py`; a physical
speaker and the Windows audio stack are not covered, and not claimed.

Run (after `cd hud && npm run build`, once `hud_slot_wait.sh 3` returns), under the memory scope:
    PYTHONPATH=. HUD_V2_CHECK_PORT=18479 python tests/face/hud_v2_duck_check.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.face.hud_v2_multichat_check import _fresh, _open, _seed  # noqa: E402

ONE = json.dumps({"preset": "single", "focused": 1,
                  "panes": [{"view": "chat"}, {"view": "chat"}, {"view": "file"}, {"view": "task"}]})
REPLY = ("Right, here is what I found. The approval gate had seven separate ways past it, and every one "
         "was an older copy of an idea. I have fixed all seven. ")


def speech(page) -> dict:
    return page.evaluate("window.__hud.speech()")


def main() -> int:
    from playwright.sync_api import sync_playwright

    from tests.face import hud_v2_check as H
    from tests.face.hud_v2_mock import DIST, MockDaemon

    if not (DIST / "index.html").exists():
        print("hud/dist is not built. Run: cd hud && npm ci && npm run build")
        return 2
    check, until = H.check, H.until
    mock = MockDaemon(0).start()

    def ons() -> int:
        return sum(1 for _, on in mock.duck_log if on)

    def last_off() -> bool:
        return bool(mock.duck_log) and not mock.duck_log[-1][1]

    def speak(thread="t1", size=9):
        mock.emit("turn_started", {}, thread_id=thread)
        for i in range(0, len(REPLY), size):
            mock.emit("text_delta", {"text": REPLY[i:i + size]}, thread_id=thread)
        mock.emit("text", {"text": REPLY}, thread_id=thread)

    try:
        _seed(mock)
        base = f"http://127.0.0.1:{mock.port}"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--autoplay-policy=no-user-gesture-required"])
            ctx = browser.new_context(viewport={"width": 1600, "height": 900})
            H.guard(ctx)
            ctx.add_init_script(H.FAKE_RECOGNIZER)
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.set_default_timeout(5000)
            before = mock.sse_connections()
            page.goto(base + "/")
            page.wait_for_selector('[data-testid="sidebar"]', state="attached")
            mock.await_reconnect(before)
            _fresh(page, mock, until, ONE)
            _open(page, until, 1, "t1")
            page.evaluate("() => window.dispatchEvent(new Event('focus'))")
            until(lambda: speech(page)["speaker"], timeout=3)

            print("\nthe switch")
            H.open_settings(page, "Voice")
            box = page.locator('[data-testid="duck-browser-toggle"]')
            check("the Voice settings carry 'Lower Browser sound while Jarvis speaks'",
                  "Lower Browser sound while Jarvis speaks" in page.locator('[data-testid="duck-browser"]').inner_text())
            check("it is on by default", box.is_checked())
            page.locator('[data-testid="settings-close"]').click()
            check("opening Settings asked for nothing", not mock.duck_log)

            print("\nspeaking holds the lease")
            mock.ms_per_char = 45
            speak()
            check("speech turning active asks for the lease",
                  bool(until(lambda: ons() >= 1, timeout=5)))
            check("the daemon would be ducking", mock.ducking())
            check("it is renewed while speech continues (not one-shot)",
                  bool(until(lambda: ons() >= 3, timeout=8)), f"{ons()} requests")
            gaps = [b[0] - a[0] for a, b in zip(mock.duck_log, mock.duck_log[1:]) if a[1] and b[1]]
            check("renewals come well inside the 3 s lease", bool(gaps) and max(gaps) < 2.0, f"gaps {gaps}")
            sent = mock.sent("POST", "/speech/duck")
            check("every request carries one boolean and this window's token (32 hex), nothing else",
                  all(set(b) == {"on", "window"} and isinstance(b["on"], bool) and len(b["window"]) == 32 for b in sent))
            check("one window uses one token", len({b["window"] for b in sent}) == 1)
            until(lambda: speech(page)["view"]["state"] == "idle", timeout=20)
            check("when speech ends the lease is ended with off", bool(until(last_off, timeout=3)))
            check("the daemon is no longer ducking", not mock.ducking())
            n = len(mock.duck_log)
            page.wait_for_timeout(1500)
            check("and nothing more is sent once idle", len(mock.duck_log) == n)

            print("\ncontrols end it too")
            mock.ms_per_char = 60
            n = len(mock.duck_log)
            speak()
            until(lambda: speech(page)["view"]["state"] == "playing", timeout=5)
            check("playing asks again", bool(until(lambda: len(mock.duck_log) > n and mock.duck_log[-1][1], timeout=3)))
            page.locator('[data-testid="speech-toggle"]').click()
            check("pausing ends the lease", bool(until(last_off, timeout=3)))
            page.wait_for_timeout(1500)
            check("a paused queue sends no renewals", last_off())
            page.locator('[data-testid="speech-toggle"]').click()
            check("resuming asks again", bool(until(lambda: mock.duck_log[-1][1], timeout=3)))
            page.locator('[data-testid="speech-stop"]').click()
            check("stopping ends it", bool(until(last_off, timeout=3)))
            until(lambda: speech(page)["view"]["state"] == "idle", timeout=5)

            print("\nthe switch off")
            H.open_settings(page, "Voice")
            page.locator('[data-testid="duck-browser-toggle"]').uncheck()
            page.locator('[data-testid="settings-close"]').click()
            check("the choice is kept in this window",
                  page.evaluate("localStorage.getItem('jarvis.duckBrowser')") == "off")
            n = len(mock.duck_log)
            mock.ms_per_char = 30
            speak()
            until(lambda: speech(page)["view"]["state"] == "playing", timeout=5)
            until(lambda: speech(page)["view"]["state"] == "idle", timeout=20)
            check("with the switch off, speaking asks for nothing", len(mock.duck_log) == n)
            check("speech still played (the switch is not read-aloud)", len(speech(page)["past"]) >= 1)

            print("\nturning it off mid-speech")
            H.open_settings(page, "Voice")
            page.locator('[data-testid="duck-browser-toggle"]').check()
            page.locator('[data-testid="settings-close"]').click()
            mock.ms_per_char = 80
            speak()
            until(lambda: ons() and mock.duck_log[-1][1], timeout=5)
            H.open_settings(page, "Voice")
            page.locator('[data-testid="duck-browser-toggle"]').uncheck()
            check("unchecking while speaking ends the lease", bool(until(last_off, timeout=3)))
            page.locator('[data-testid="settings-close"]').click()
            page.locator('[data-testid="speech-stop"]').click()
            H.open_settings(page, "Voice")
            page.locator('[data-testid="duck-browser-toggle"]').check()
            page.locator('[data-testid="settings-close"]').click()
            until(lambda: speech(page)["view"]["state"] == "idle", timeout=5)

            print("\na window that goes away stops renewing; the lease runs out")
            mock.ms_per_char = 80
            speak()
            until(lambda: mock.duck_log and mock.duck_log[-1][1] and mock.ducking(), timeout=5)
            page.close()
            check("closing the window ends it at once or within the 3 s lease",
                  bool(until(lambda: not mock.ducking(), timeout=5)))
            sent = len(mock.duck_log)
            time.sleep(1.5)
            check("and nothing renews it afterwards", len(mock.duck_log) == sent and not mock.ducking())

            check("no page errors", not errors, "; ".join(errors[:3]))
            ctx.close()
            browser.close()
    finally:
        mock.stop()
    print()
    if H.FAILURES:
        print(f"{len(H.FAILURES)} of {H.CHECKS} checks FAILED:")
        for f in H.FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {H.CHECKS} duck checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
