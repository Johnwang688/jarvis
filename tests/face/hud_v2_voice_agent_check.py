"""Isolated HUD launch check. Intercept the POST; no Windows process or mic."""
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from playwright.sync_api import expect, sync_playwright  # noqa: E402

from tests.face.hud_v2_check import FAILURES, guard, until  # noqa: E402
from tests.face.hud_v2_declutter_check import _fold_left  # noqa: E402
from tests.face.hud_v2_mock import MockDaemon  # noqa: E402


def main():
    mock = MockDaemon(0).start()
    url = f"http://127.0.0.1:{mock.port}"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(viewport={"width": 1280, "height": 800})
            guard(ctx)
            page = ctx.new_page()
            requests = []
            pending = []

            def hold(route):
                requests.append(route.request.post_data_json)
                pending.append(route)

            status = {"state": "off", "detail": "Start Voice Anywhere"}
            page.route(url + "/voice-agent/start", hold)
            page.route(url + "/voice-agent/status", lambda route: route.fulfill(json=status))
            page.goto(url)
            page.locator("#orb").wait_for(state="visible")
            button = page.get_by_role("button", name="Start Voice Anywhere")
            result = page.locator("#voice-agent-result")
            expect(page.locator("#micstrip #voice-agent-start")).to_be_visible()

            button.click()
            expect(page.get_by_role("button", name="Starting Voice Anywhere…")).to_be_disabled()
            until(lambda: len(pending) == 1)
            assert requests == [{}]
            pending.pop().fulfill(json={"connected": True})
            status.update(state="connected", detail="Waiting for the wake listener.")
            expect(page.get_by_role("button", name="Voice Anywhere connected")).to_be_disabled()
            status.update(state="ready", detail="Say Jarvis, then a command.")
            expect(page.get_by_role("button", name="Voice Anywhere ready")).to_be_disabled()
            page.evaluate("""() => {
                const capture = window.__hud.capture;
                const realWake = capture.onWake.bind(capture);
                window.__wakeClaims = 0;
                capture.onWake = (...args) => { window.__wakeClaims++; return realWake(...args); };
                window.__hud.mic.wake();
            }""")
            assert page.evaluate("window.__wakeClaims") == 0
            status.update(state="microphone_unavailable", detail="The microphone is unavailable.")
            expect(result).to_have_text("The microphone is unavailable.")
            status.update(state="off", detail="Start Voice Anywhere")
            expect(button).to_be_enabled()

            page.evaluate("window.__hud.mic.wake()")
            assert page.evaluate("window.__wakeClaims") == 1

            page.set_viewport_size({"width": 1280, "height": 500})
            expect(page.locator("#voice-agent-start.beside")).to_be_visible()
            page.set_viewport_size({"width": 1280, "height": 800})
            _fold_left(page, until)
            expect(page.locator("#voice-agent-start.rail")).to_be_visible()
            button.click()
            until(lambda: len(pending) == 1)
            pending.pop().fulfill(status=409, json={"error": "Voice link is offline."})
            expect(result).to_have_text("Voice link is offline.")
            expect(button).to_be_enabled()
            assert requests == [{}, {}]

            page.get_by_test_id("titlebar-settings").click()
            expect(page.locator("#voice-agent-start")).to_be_disabled()
            page.get_by_test_id("settings-close").click()
            expect(button).to_be_enabled()
            mock.emit("approval_requested", {
                "req_id": "voice-launch-card", "code": "VL01", "tool": "run_command",
                "args": {"command": "ls"}, "command": "ls", "reason": "test",
                "layer": "human", "origin": "test", "allowlistable": True, "timeout_s": 120,
            })
            expect(page.get_by_test_id("approval-card")).to_be_visible()
            expect(page.locator("#voice-agent-start")).to_be_disabled()
            assert requests == [{}, {}]
            assert not FAILURES, FAILURES

            browser.close()
    finally:
        mock.stop()
    print("HUD voice launch passed in full, compact and short-window layouts")


if __name__ == "__main__":
    main()
