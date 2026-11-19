"""With the paired mic and AUTO selected, Ctrl+D fills the draft once."""
from pathlib import Path
import sys

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.face.hud_v2_check import FAKE_RECOGNIZER, guard, until
from tests.face.hud_v2_mock import MockDaemon
from tests.face.hud_v2_ptt_check import _box, _open_t1


def main():
    mock = MockDaemon(0).start()
    try:
        url = f"http://127.0.0.1:{mock.port}"
        mock.world["stt_text"] = "Open youtube.com"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            guard(context)
            context.add_init_script(FAKE_RECOGNIZER)
            context.add_init_script("""
                localStorage.setItem("jarvis.dictation", "auto");
                const orig = window.fetch.bind(window);
                window.fetch = (path, options) => {
                    if (path !== "/voice-agent/microphone") return orig(path, options);
                    const pcm = new ReadableStream({start(controller) {
                        window.testPCM = controller;
                        controller.enqueue(new Uint8Array(1024));
                        const timer = setInterval(() => controller.enqueue(new Uint8Array(1024)), 32);
                        options.signal.addEventListener("abort", () => clearInterval(timer), {once: true});
                    }});
                    return Promise.resolve(new Response(pcm, {headers: {"Content-Type": "application/octet-stream"}}));
                };
            """)
            page = context.new_page()
            page.route(url + "/voice-agent/status", lambda route: route.fulfill(json={
                "state": "ready", "shared_mic": True, "shared_capable": True, "can_stop": True,
            }))
            page.goto(url)
            assert until(lambda: page.evaluate("!!window.testPCM && !!window.__hud"), timeout=5)
            _open_t1(page, until)
            _box(page).click()
            page.keyboard.down("Control")
            page.keyboard.down("d")
            assert page.evaluate("!!window.__hud.capture.ptt")
            page.evaluate("""() => {
                const samples = new Int16Array(24576);
                for (let i = 0; i < samples.length; i++) samples[i] = Math.round(7000 * Math.sin(i * 0.18));
                window.testPCM.enqueue(new Uint8Array(samples.buffer));
            }""")
            assert until(lambda: page.evaluate("window.__hud.capture.ring.written - window.__hud.capture.ptt.at >= 24576"))
            page.keyboard.up("d")
            page.keyboard.up("Control")
            expect(_box(page)).to_have_value("Open youtube.com")
            assert len(mock.sent("POST", "/stt")) == 1
            assert not mock.sent("POST", "/threads/t1/send")
            browser.close()
    finally:
        mock.stop()


if __name__ == "__main__":
    main()
