"""Show an automatic action and the outside-text warning in the built window."""

import unittest

import e2e_layout_check as layout
import e2e_window_check as window
from jarvis import config
from jarvis.v3.policy import set_auto


class AutoWindow(layout.Layout):
    def test_clean_write_then_tainted_card(self):
        set_auto(config.V3_POLICY_FILE, True)
        entry = window.VAULT_ENTRY
        target = f"{entry['folder']}/{entry['name']}"
        self.start_daemon(
            self.reply(calls=[("vault_add", entry)]), self.reply("Fixture note saved."),
            self.reply(calls=[("vault_list", {}), ("vault_add", dict(entry, name="after-read"))]),
            self.reply("Fixture note declined."),
        )
        self.open_window()
        card = self.chat_panel.get_by_role("region", name="Tool approval")
        outside = self.chat_panel.get_by_text("Outside text read: sensitive actions ask", exact=True)
        self.send("Save the fixture note.")
        auto = self.transcript.locator(".chat-auto")
        self.expect(auto.locator("summary")).to_have_text(f"Ran without asking · Add a vault entry · {target}")
        self.expect(card).to_have_count(0)
        self.expect(outside).to_have_count(0)
        self.expect_idle()
        self.assertEqual(self.vault.read(target).body, entry["body"])
        self.assertEqual(self.approval_states(), ["auto"])
        self.assertEqual(self.approval_answers, [])
        seen = self.measure(1280, 720, "r1b-clean-auto")
        self.assert_one_screen(seen, inside=("chat", "feed", "status", "cron"))

        self.page.reload(wait_until="load")
        self.expect(auto).to_have_count(1)
        auto.locator("summary").click()
        self.expect(auto.get_by_text("Policy level L0 · default:vault.update")).to_be_visible()
        auto.locator("summary").click()

        self.send("Read the vault and save another fixture note.")
        self.expect(card.get_by_text("hard:tainted", exact=True)).to_be_visible(timeout=window.WAIT_MS)
        self.expect(outside).to_be_visible()
        seen = self.measure(1280, 720, "r1b-tainted-card")
        self.assert_one_screen(seen, inside=("chat", "feed", "status", "cron"))
        card.get_by_role("button", name="Deny").click()
        self.expect_idle()
        self.assertEqual(self.approval_states(), ["auto", "pending", "denied"])
        self.chat_panel.get_by_role("button", name="New chat").click()
        self.expect(outside).to_have_count(0)
        self.expect(auto).to_have_count(0)


def load_tests(_loader, _tests, _pattern):
    return unittest.TestSuite([AutoWindow("test_clean_write_then_tainted_card")])


if __name__ == "__main__":
    unittest.main()
