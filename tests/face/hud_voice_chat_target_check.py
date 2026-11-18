"""Real local HUD/daemon/paired voice routing, two actual Chromium tabs under Xvfb.
Ephemeral listener, isolated owner settings, fake providers and generated silent audio.
"""
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from playwright.sync_api import expect, sync_playwright
from jarvis import config
from tests.v2.voice_link_va245_impl_check import Base, existing
from windows import voicewire


BROWSER_CACHE = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache/ms-playwright"))


class SelectedChat(Base):
    def test_foreground_selects_chat_and_only_one_player_reads_each_reply(self):
        project = self.d.stores.projects.create("Voice destination checks", str(self.root))
        audio = voicewire.wav_bytes(bytes(320))
        native, hud = [], []
        with (patch.object(config, "HUD_DEV", True), patch.object(config, "HUD_DIST", ROOT / "hud/dist"),
              patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": BROWSER_CACHE})):
            existing.Base.start(self)
            def chat(title):
                return self.d.open_thread(project.id, "chat", "fast", {}, title=title)
            first, second = chat("First chat"), chat("Second chat")
            self.link.speaker = lambda words: audio
            peer = self.client()
            url = f"http://127.0.0.1:{self.d.face_port}"
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=False, args=[
                    "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--mute-audio",
                ])
                try:
                    ctx = browser.new_context(viewport={"width": 1408, "height": 853}, permissions=["microphone"])
                    def new_tab():
                        tab = ctx.new_page()
                        # Playwright emulates focus on every attached page,
                        # even behind other tabs. Use actual Chromium focus.
                        ctx.new_cdp_session(tab).send("Emulation.setFocusEmulationEnabled", {"enabled": False})
                        return tab
                    def say(route):
                        hud.append((route.request.frame.page, route.request.post_data_json.get("text")))
                        route.fulfill(content_type="audio/wav", body=audio)
                    ctx.route(url + "/say", say)
                    def wait_speech(page, condition):
                        expiry = time.monotonic() + 6
                        while time.monotonic() < expiry:
                            if page.evaluate("() => window.__hud && (" + condition + ")"):
                                return
                            page.wait_for_timeout(20)
                        self.fail(f"Speech condition {condition!r} timed out: {page.evaluate('window.__hud.speech()')}")
                    def open_chat(page, thread):
                        page.bring_to_front()
                        project_row = page.get_by_test_id(f"project-{project.id}")
                        expect(project_row).to_be_visible(timeout=8000)
                        row = page.get_by_test_id(f"thread-{thread.id}")
                        if project_row.locator(".tw").inner_text() == "▸":
                            project_row.click()
                        row.click(timeout=5000)
                        expect(page.get_by_test_id("input").first).to_be_visible()
                    def wait_target(page, thread_id):
                        expiry = time.monotonic() + 5
                        target_file = self.d.stores.root / "voice-target.json"
                        while time.monotonic() < expiry:
                            if target_file.is_file() and json.loads(target_file.read_text()) == thread_id:
                                return
                            page.wait_for_timeout(20)
                        self.fail("Selected chat did not reach the paired voice destination")

                    alpha = new_tab()
                    alpha.goto(url)
                    open_chat(alpha, first)
                    wait_target(alpha, first.id)
                    beta = new_tab()
                    beta.goto(url)
                    open_chat(beta, second)
                    wait_target(beta, second.id)
                    self.assertFalse(alpha.evaluate("document.hasFocus()"))
                    wait_speech(beta, "window.__hud.speech().speaker")
                    wait_speech(alpha, "!window.__hud.speech().speaker")
                    # This passive tab can reset its UI but cannot take the
                    # other tab's selected voice destination.
                    alpha.get_by_test_id("new-thread").evaluate("el => el.click()")
                    alpha.wait_for_timeout(100)
                    wait_target(beta, second.id)
                    external = new_tab()
                    external.goto("about:blank")
                    external.bring_to_front()
                    self.assertFalse(beta.evaluate("document.hasFocus()"))
                    self.assertEqual(peer.ask_spoken("A fabricated voice question", lambda _: None, native.append), "Hello")
                    expect(beta.get_by_test_id("via-voice")).to_be_visible()
                    expect(beta.get_by_text("Hello", exact=True)).to_be_visible()
                    self.assertEqual(beta.evaluate("window.__hud.speech().past.length"), 0)
                    self.assertEqual(native, [audio])
                    self.assertEqual(hud, [])

                    beta.bring_to_front()
                    box = beta.get_by_test_id("input").first
                    box.fill("A fabricated typed follow-up")
                    box.press("Enter")
                    expiry = time.monotonic() + 6
                    while time.monotonic() < expiry and not hud:
                        beta.wait_for_timeout(20)
                    self.assertEqual(hud, [(beta, "Hello")], beta.evaluate("window.__hud.speech()"))
                    wait_speech(beta, "window.__hud.speech().past.some(item => item.state === 'done')")
                    self.assertEqual(len(beta.evaluate("window.__hud.speech().past")), 1)
                    self.assertEqual(native, [audio])
                    alpha.bring_to_front()
                    wait_target(alpha, None)
                    with self.assertRaises(voicewire.LinkError):
                        peer.ask("Question with no selected chat")
                    self.assertEqual(len(self.fast.messages), 2)
                    self.assertEqual({t.id for t in self.d.stores.threads.list()}, {first.id, second.id})
                finally:
                    browser.close()


if __name__ == "__main__":
    unittest.main()
