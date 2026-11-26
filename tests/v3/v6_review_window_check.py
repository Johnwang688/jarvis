"""Exercise Voice layout, polling, and controls in Chromium."""

from threading import Event
import unittest
from unittest.mock import patch

import e2e_voice_panel_check as fixture
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from jarvis.v3.daemon import Daemon


class VoiceWindowReview(unittest.TestCase):
    setUpClass = classmethod(fixture.VoiceWindow.setUpClass.__func__)
    open = fixture.VoiceWindow.open
    open_details = fixture.VoiceWindow.open_details

    def setUp(self):
        self.slow = False
        self.started = Event()
        self.overlap = Event()
        self.release = Event()
        self.enterContext(patch.object(Daemon, "_voice_panel", self.slow_panel))
        fixture.VoiceWindow.setUp(self)
        self.addCleanup(self.release.set)

    def slow_panel(self, body):
        if self.started.is_set():
            self.overlap.set()
        self.started.set()
        self.release.wait(15)
        return 200, self.snapshot

    def route(self, route):
        path = route.request.url.removeprefix(self.origin)
        if path == fixture.VOICE and self.slow:
            route.continue_()
        elif path == fixture.VOICE + "/helper":
            action = route.request.post_data_json["action"]
            self.helper_actions.append(action)
            route.fulfill(json={"message": f"Fixture accepted {action}."})
        else:
            fixture.VoiceWindow.route(self, route)

    def test_slow_mic_poll_does_not_overlap_at_daemon(self):
        self.snapshot["helper"]["connected"] = True
        self.open()
        self.open_details()
        self.slow = True
        with self.page.expect_request(self.origin + fixture.VOICE):
            self.panel.get_by_role("button", name="Mic test").click()
        self.assertTrue(self.started.wait(2), "The first mic poll did not reach the daemon")
        self.page.clock.run_for(5_000)
        try:
            self.panel.get_by_text("Voice status is unavailable.").wait_for(timeout=500)
        except PlaywrightTimeout:
            pass
        self.page.clock.run_for(500)
        # Give the intercepted retry time to reach the daemon while the first is held.
        self.page.wait_for_timeout(500)
        self.assertFalse(self.overlap.is_set(), "A second mic poll reached the daemon before the first returned")

    def test_long_transcript_does_not_overflow_at_window_sizes(self):
        self.snapshot["turns"][0]["text"] = "M" * 300
        self.open()
        self.open_details()
        self.panel.locator(".voice-recent > summary").click()
        self.panel.locator(".voice-turn > summary").click()
        for width, height in ((1024, 700), (1280, 720), (1920, 1080)):
            with self.subTest(width=width, height=height):
                self.page.set_viewport_size({"width": width, "height": height})
                sizes = self.panel.evaluate("""panel => [panel, panel.parentElement,
                    document.querySelector('.agent-window'), document.documentElement].map(
                        e => [e.scrollWidth, e.clientWidth, e.scrollHeight, e.clientHeight])""")
                self.assertTrue(all(sw <= cw for sw, cw, _, _ in sizes), sizes)
                self.assertTrue(all(sh <= ch for _, _, sh, ch in sizes[-2:]), sizes)
                scroll, client = self.panel.locator(".voice-details-body").evaluate(
                    "el => [el.scrollWidth, el.clientWidth]")
                self.assertLessEqual(scroll, client)


if __name__ == "__main__":
    unittest.main()
