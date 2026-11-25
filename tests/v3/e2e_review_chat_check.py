"""Review races in the built window with the isolated e2e daemon and fake model."""

from threading import Event
import unittest

import e2e_window_check as window


class ChatReview(window.Window):
    def wait_for_server_idle(self):
        self.page.wait_for_function(
            "async () => (await (await fetch('/v3/owner/chat')).json()).state === 'idle'",
            polling=50,
        )

    def owner_post(self, route, body):
        return self.page.request.post(
            self.origin + "/v3/owner/chat/" + route,
            headers={"Origin": self.origin}, data=body, max_redirects=0,
        )

    def test_reload_keeps_a_real_running_tool(self):
        card = self.request_vault_add("The note was saved.")
        tool = self.transcript.locator(".chat-tools li").filter(has_text="vault_add")
        self.expect(tool).to_have_text("vault_add · Running")
        self.page.reload(wait_until="load")
        self.expect(card.get_by_role("heading", name="Add a vault entry")).to_be_visible()
        self.assertEqual(self.chat.snapshot()["approvals"], 1)
        self.assertIn("vault_add", self.chat.snapshot()["tools"])
        self.expect(tool).to_have_text("vault_add · Running", timeout=2_000)
        self.expect(self.transcript.locator(".chat-text")).to_have_text(["Save the fixture note."])

    def test_new_session_excludes_old_events_when_first_poll_is_delayed(self):
        release = Event()

        def held_reply(_):
            if not release.wait(15):
                raise TimeoutError("The browser did not release the first completion")
            return self.reply("Reply from the old chat.")

        self.start_daemon(held_reply, self.reply("Reply from the new chat."))
        self.addCleanup(release.set)
        self.open_window()
        events_path = self.origin + "/v3/owner/chat/events"
        failures = []

        def delay_poll(route):
            try:
                release.set()
                self.wait_for_server_idle()
                new_chat = self.owner_post("new", {})
                self.assertEqual(new_chat.status, 200)
                self.assertEqual(self.owner_post("send", {"text": "Prompt from the new chat."}).status, 202)
                self.wait_for_server_idle()
                self.assertEqual(self.chat.snapshot()["session_id"], new_chat.json()["session_id"])
            except Exception as error:
                failures.append(error)
            finally:
                route.continue_()

        self.page.route(events_path, delay_poll, times=1)
        with self.page.expect_response(events_path):
            self.send("Prompt from the old chat.")
        if failures:
            raise failures[0]
        self.expect(self.transcript.get_by_text("Reply from the new chat.", exact=True)).to_be_visible()
        self.expect(self.transcript.locator(".chat-text")).to_have_text([
            "Prompt from the new chat.", "Reply from the new chat.",
        ], timeout=7_000)

    def test_late_steer_appears_once_when_it_becomes_a_followup(self):
        entered, release = Event(), Event()

        def held_reply(_):
            entered.set()
            if not release.wait(15):
                raise TimeoutError("The browser did not release the first completion")
            return self.reply("The first answer.")

        self.start_daemon(held_reply, self.reply("The follow-up answer."))
        self.addCleanup(release.set)
        self.open_window()
        self.send("Give the first answer.")
        self.assertTrue(entered.wait(5))
        self.send("Now give the follow-up answer.")
        queued = self.transcript.get_by_role("article").filter(has_text="Now give the follow-up answer.")
        self.expect(queued.get_by_text("· Added to this turn")).to_be_visible()
        release.set()
        followup = self.transcript.get_by_text("The follow-up answer.", exact=True)
        self.expect(followup).to_be_visible()
        self.expect_idle()
        self.assertEqual([row["text"] for row in self.chat.snapshot()["history"]], [
            "Give the first answer.", "The first answer.",
            "Now give the follow-up answer.", "The follow-up answer.",
        ])
        messages = ["Give the first answer.", "The first answer.",
                    "Now give the follow-up answer.", "The follow-up answer."]
        self.expect(self.transcript.locator(".chat-text")).to_have_text(messages, timeout=2_000)
        self.expect(queued.get_by_text("· Added to this turn")).to_have_count(0)
        self.page.reload(wait_until="load")
        self.expect(self.transcript.locator(".chat-text")).to_have_text(messages, timeout=2_000)
        self.expect(queued.get_by_text("· Added to this turn")).to_have_count(0)

    def test_provider_error_marks_the_undelivered_steer(self):
        entered, release = Event(), Event()

        def held_failure(_):
            entered.set()
            if not release.wait(15):
                raise TimeoutError("The browser did not release the first completion")
            raise RuntimeError("Fixture provider failure.")

        self.start_daemon(held_failure)
        self.addCleanup(release.set)
        self.open_window()
        self.send("Start the failing turn.")
        self.assertTrue(entered.wait(5))
        self.send("This message will not reach the provider.")
        queued = self.transcript.get_by_role("article").filter(has_text="This message will not reach the provider.")
        self.expect(queued.get_by_text("· Added to this turn")).to_be_visible()
        release.set()
        self.expect(self.chat_panel.get_by_text("The turn failed. Try sending a message again.")).to_be_visible()
        self.expect(queued.get_by_text("· Discarded before delivery")).to_be_visible()
        self.expect(queued.get_by_text("· Added to this turn")).to_have_count(0)
        self.assertEqual(self.chat.snapshot()["state"], "error")
        self.assertEqual(len(self.model.seen), 1)


if __name__ == "__main__":
    unittest.main()
