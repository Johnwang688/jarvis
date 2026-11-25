"""Check pointer reachability in the built dashboard with a long approval card."""

import unittest

import e2e_window_check as window


class Composer(window.Window):
    def open_long_approval(self):
        body = "\n".join(f"Fixture vault line {n:02}" for n in range(1, 61))
        self.start_daemon(self.reply("Transcript fixture.\n" * 160),
                          self.reply(calls=[("vault_add", window.VAULT_ENTRY | {"body": body})]),
                          self.reply("Finished."))
        self.open_window()
        self.page.set_viewport_size({"width": 1280, "height": 720})
        self.send("Fill the transcript.")
        self.expect_idle()
        self.send("Save the fixture note.")
        card = self.chat_panel.get_by_role("region", name="Tool approval")
        self.expect(card.get_by_role("button", name="Approve")).to_be_enabled()
        card.get_by_text("Details", exact=True).click()
        return card

    def break_chat_poll(self):
        self.page.route(self.origin + "/v3/owner/chat/events", lambda route: route.fulfill(json={}))
        self.expect(self.chat_panel.get_by_text("Chat unavailable. Retrying…")).to_be_visible()

    def test_send_remains_reachable_in_a_short_window_with_an_open_card(self):
        self.open_long_approval()
        self.composer.fill("Keep this draft.")
        field = self.composer.bounding_box()
        x, y = field["x"] + field["width"] - 5, field["y"] + field["height"] - 5
        self.page.mouse.move(x, y)
        self.page.mouse.down()
        self.page.mouse.move(x, y + 132, steps=6)
        self.page.mouse.up()
        # The drag asks for more height than the column may have left; the
        # composer takes what there is and never less than it had.
        self.assertGreaterEqual(self.composer.bounding_box()["height"], field["height"] - 1)

        self.break_chat_poll()
        self.page.set_viewport_size({"width": 1280, "height": 520})
        self.expect(self.chat_panel.get_by_role("button", name="Stop")).to_be_enabled()
        send = self.chat_panel.get_by_role("button", name="Send")
        self.expect(send).to_be_enabled()
        self.assertTrue(send.evaluate("""button => {
            const {x, y, width, height} = button.getBoundingClientRect();
            return button.contains(document.elementFromPoint(x + width / 2, y + height / 2));
        }"""), f"Send cannot receive a pointer press at 1280×520: {send.bounding_box()}")


for name in dir(window.Window):
    if name.startswith("test_"):
        setattr(Composer, name, None)


if __name__ == "__main__":
    unittest.main()
