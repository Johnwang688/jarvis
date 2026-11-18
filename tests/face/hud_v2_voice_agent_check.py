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


def check_voice_geometry(page):
    # Active hit targets, not just CSS visibility. The 132px fixed dock used
    # to put the mic/status over the bottom of the tree and Schedules.
    info = page.evaluate("""() => {
        const footer = document.querySelector('#orbdock').getBoundingClientRect();
        const side = document.querySelector('.voice-sidebar').getBoundingClientRect();
        const button = document.querySelector('#voice-agent-start');
        const controls = [...new Set([button, ...document.querySelectorAll(
          '#micstrip button, #modecycle, #orb, #speechstrip button, #sidebar .sidebar-foot button'
        )])].filter(el => el.getBoundingClientRect().height > 0);
        const covered = controls.filter(el => {
          const r = el.getBoundingClientRect();
          const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
          return !el.contains(hit) || r.right > side.right + 1 || r.left < side.left - 1;
        }).map(el => el.id || el.textContent);
        const schedule = document.querySelector('#sidebar .sidebar-foot').getBoundingClientRect();
        return { covered, footerTop: footer.top, schedulesBottom: schedule.bottom,
                 buttonFits: button.scrollWidth <= button.clientWidth + 1 };
    }""")
    assert not info["covered"] and info["buttonFits"], info
    assert info["schedulesBottom"] <= info["footerTop"] + 1, info


def main():
    mock = MockDaemon(0).start()
    url = f"http://127.0.0.1:{mock.port}"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"])
            ctx = browser.new_context(viewport={"width": 1408, "height": 853}, permissions=["microphone"])
            guard(ctx)
            ctx.add_init_script("""
                window.testMics = [];
                const get = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
                navigator.mediaDevices.getUserMedia = async constraints => {
                    const stream = await get(constraints).catch(e => { window.testMicError = e.name; throw e; });
                    window.testMics.push(...stream.getAudioTracks());
                    return stream;
                };
                const fetchOriginal = window.fetch.bind(window);
                window.fetch = (path, options) => {
                    if (path !== '/voice-agent/microphone') return fetchOriginal(path, options);
                    return Promise.resolve(new Response(new ReadableStream({
                        start(controller) {
                            const timer = setInterval(() => controller.enqueue(new Uint8Array(3200)), 50);
                            options.signal.addEventListener('abort', () => {
                                clearInterval(timer);
                                controller.close();
                            }, {once: true});
                        },
                    }), {headers:{'Content-Type':'application/octet-stream'}}));
                };
            """)
            page = ctx.new_page()
            requests = []
            pending = []

            def hold(route):
                requests.append(route.request.post_data_json)
                pending.append(route)

            status = {"state": "off", "detail": "Start Voice Anywhere"}
            page.route(url + "/voice-agent/start", hold)
            stops = []
            def stop(route):
                stops.append(route.request.post_data_json)
                status.update(state="off", detail="Voice stopped.", hud_mic_paused=True, can_stop=False, shared_mic=False)
                route.fulfill(json={"stopped": True})
            page.route(url + "/voice-agent/stop", stop)
            polls = []
            def read_status(route):
                polls.append(True)
                if len(polls) < 2:
                    route.fulfill(status=503, json={"error": "Restarting"})
                else:
                    route.fulfill(json=status)
            page.route(url + "/voice-agent/status", read_status)
            page.goto(url)
            page.locator("#orb").wait_for(state="visible")
            button = page.get_by_role("button", name="Start Voice Anywhere")
            result = page.locator("#voice-agent-result")
            expect(page.locator("#micstrip #voice-agent-start")).to_be_visible()
            indicator = page.locator(".voice-agent-state")
            assert until(lambda: len(polls) >= 1)
            assert page.evaluate("window.testMics.length") == 0
            expect(indicator).to_have_text("Off")
            meter = page.locator("#level").bounding_box()
            assert meter and meter["width"] > 20 and 0 < meter["height"] < 10

            assert until(lambda: page.evaluate("window.testMics.some(t => t.readyState === 'live')")), page.evaluate("[window.testMicError,window.testMics.map(t => t.readyState)]")
            button.click()
            assert until(lambda: page.evaluate("window.testMics.every(t => t.readyState === 'ended')"))
            expect(page.get_by_role("button", name="Starting Voice Anywhere…")).to_be_disabled()
            expect(indicator).to_have_text("Starting")
            until(lambda: len(pending) == 1)
            assert requests == [{}]
            pending.pop().fulfill(json={"connected": True})
            status.update(state="connected", detail="Waiting for the wake listener.")
            expect(page.get_by_role("button", name="Voice Anywhere connected")).to_be_disabled()
            status.update(state="ready", detail="Say Jarvis, then a command.")
            expect(page.get_by_role("button", name="Voice Anywhere ready")).to_be_disabled()
            expect(indicator).to_have_text("Ready")
            page.evaluate("""() => {
                const capture = window.__hud.capture;
                const realWake = capture.onWake.bind(capture);
                window.__wakeClaims = 0;
                capture.onWake = (...args) => { window.__wakeClaims++; return realWake(...args); };
                window.__hud.mic.wake();
            }""")
            assert page.evaluate("window.__wakeClaims") == 0
            status.update(can_stop=True, shared_mic=True)
            expect(page.get_by_role("button", name="Stop Voice Anywhere")).to_be_enabled()
            expect(result).to_contain_text("share one mic")
            check_voice_geometry(page)
            assert page.evaluate("window.testMics.every(t => t.readyState === 'ended')")
            page.get_by_role("button", name="Stop Voice Anywhere").click()
            expect(indicator).to_have_text("Off")
            assert stops == [{}]
            page.reload()
            expect(indicator).to_have_text("Off")
            assert page.evaluate("window.testMics.length") == 0
            page.evaluate("""() => {
                window.__wakeClaims = 0;
                window.__hud.capture.onWake = () => { window.__wakeClaims++; };
            }""")
            status.update(state="microphone_unavailable", detail="The microphone is unavailable.")
            expect(result).to_have_text("The microphone is unavailable.")
            expect(indicator).to_have_text("Needs attention")
            status.update(state="off", detail="Start Voice Anywhere")
            expect(button).to_be_enabled()

            page.evaluate("window.__hud.mic.wake()")
            assert page.evaluate("window.__wakeClaims") == 0

            page.set_viewport_size({"width": 1280, "height": 500})
            expect(page.locator("#orbdock.tight #voice-agent-start")).to_be_visible()
            check_voice_geometry(page)
            page.set_viewport_size({"width": 1280, "height": 800})
            _fold_left(page, until)
            expect(page.locator("#orbdock.mini #voice-agent-start")).to_be_visible()
            check_voice_geometry(page)
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
