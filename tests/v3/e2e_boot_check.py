"""The startup animation in the built v3 window, against the isolated daemon of `e2e_window_check`.

Automation never sees it unless this suite forces it (`window.__hudBootForce`), which can only make it
play. Set E2E_SHOTS to a folder to keep screenshots of it mid-play.
"""

import os
from pathlib import Path
import unittest

import e2e_window_check as window

FORCE = "window.__hudBootForce = true;"
BOOT = "#hud-boot"


class Boot(window.Window):
    def open_forced(self):
        """Reload with the force set. The first, unforced load never mounts the overlay, so the
        window's once-per-window flag is still unset."""
        self.assertEqual(self.page.locator(BOOT).count(), 0)
        self.assertIsNone(self.page.evaluate("sessionStorage.getItem('jarvis.hud.boot.played')"))
        self.page.add_init_script(FORCE)
        self.page.reload(wait_until="load")

    def shot(self, name):
        shots = os.environ.get("E2E_SHOTS")
        if shots:
            self.page.screenshot(path=str(Path(shots) / f"{name}.png"))

    def test_it_plays_once_then_leaves_the_window_as_it_was(self):
        self.start_daemon()
        self.open_window()
        self.page.set_viewport_size({"width": 1280, "height": 720})
        self.open_forced()
        overlay = self.page.locator(BOOT)
        self.expect(overlay).to_have_count(1)
        self.assertEqual(overlay.locator("canvas").count(), 2)
        box = overlay.bounding_box()
        self.assertEqual((round(box["x"]), round(box["y"]), round(box["width"]), round(box["height"])), (0, 0, 1280, 720))
        # It never takes the keyboard, and the window loads underneath it.
        self.assertTrue(self.page.evaluate("document.activeElement === document.body"))
        self.expect(self.page.get_by_role("region", name="Status", exact=True).get_by_role("status")).to_have_text("Running")
        self.page.wait_for_timeout(1500)
        self.shot("boot-1500ms")
        self.page.wait_for_timeout(1500)
        self.shot("boot-3000ms")
        # It ends by itself, well inside its hard cap, and leaves nothing behind.
        self.expect(overlay).to_have_count(0, timeout=9_000)
        self.shot("boot-after")
        self.assertTrue(self.page.evaluate("document.activeElement === document.body"))
        self.expect(self.composer).to_be_visible()
        self.assertEqual(self.page.evaluate("window.e2eFocus"), [])
        # A reload in the same window does not replay it.
        self.page.reload(wait_until="load")
        self.page.wait_for_timeout(400)
        self.assertEqual(self.page.locator(BOOT).count(), 0)
        self.assertEqual(self.page.evaluate("sessionStorage.getItem('jarvis.hud.boot.played')"), "1")

    def test_a_click_skips_it_and_presses_nothing_underneath(self):
        self.start_daemon(self.reply("This reply must not be requested."))
        self.open_window()
        self.open_forced()
        overlay = self.page.locator(BOOT)
        self.expect(overlay).to_have_count(1)
        # The press lands where New chat is; the overlay takes it.
        target = self.chat_panel.get_by_role("button", name="New chat").bounding_box()
        self.page.mouse.click(target["x"] + target["width"] / 2, target["y"] + target["height"] / 2)
        self.expect(overlay).to_have_count(0, timeout=3_000)
        self.assertEqual(self.chat.snapshot()["state"], "idle")
        self.assertEqual(len(self.model.seen), 0)

    def test_a_key_skips_it_and_still_types(self):
        self.start_daemon()
        self.open_window()
        self.open_forced()
        overlay = self.page.locator(BOOT)
        self.expect(overlay).to_have_count(1)
        self.page.keyboard.press("Escape")
        self.expect(overlay).to_have_count(0, timeout=3_000)

    def test_an_approval_card_removes_it(self):
        card = self.request_vault_add("The note was saved.")
        self.open_forced()
        # The card is waiting when the window opens: the animation gives way as soon as the
        # window knows, long before it would have ended by itself.
        self.expect(card.get_by_role("heading", name="Add a vault entry")).to_be_visible(timeout=window.WAIT_MS)
        self.expect(self.page.locator(BOOT)).to_have_count(0, timeout=1_500)
        self.chat_panel.get_by_role("button", name="Deny").click()
        self.expect_idle()

    def test_automation_never_sees_it_unforced(self):
        self.start_daemon()
        self.open_window()
        self.page.wait_for_timeout(600)
        self.assertEqual(self.page.locator(BOOT).count(), 0)
        self.assertIsNone(self.page.evaluate("sessionStorage.getItem('jarvis.hud.boot.played')"))


# The inherited window tests run in their own file.
for _name in dir(window.Window):
    if _name.startswith("test_"):
        setattr(Boot, _name, None)


if __name__ == "__main__":
    unittest.main()
