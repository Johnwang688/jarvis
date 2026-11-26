"""Message threads with pending requests use the same waiting state as work."""

import time
import unittest

import e2e_window_check as window


class WaitingMessages(window.Window):
    def test_running_message_with_pending_request_is_amber(self):
        from jarvis.v3.t3_client import _state
        from jarvis.v3.work import Ledger

        ledger = Ledger(self.data / "work" / "threads.jsonl")
        now = time.time()
        for message, thread_id in [(True, "fixture-message"), (False, "fixture-work")]:
            state = _state({
                "threadId": thread_id, "status": "running", "pendingRequestCount": 1,
                "title": f"Pending {thread_id}", "providerInstanceId": "fake", "model": "fake",
            }, [])
            ledger.record_start(
                thread_id=thread_id, run_id=f"run-{thread_id}", project="fixture",
                title=state.title, branch=None, brief_sha256="a" * 64, at=now, message=message,
            )
            ledger.record_status(
                thread_id, status=state.status, waiting_on_owner=state.waiting_on_owner, at=now,
            )

        self.start_daemon()
        self.open_window()
        work = self.page.get_by_role("region", name="T3", exact=True)
        reference = work.get_by_text("waiting on you", exact=True)
        self.expect(reference).to_be_visible()
        read_label_and_color = "el => [el.textContent, getComputedStyle(el).color]"
        messages = self.feed_panel.get_by_role("region", name="Messages", exact=True)
        status = messages.get_by_role("listitem").filter(
            has_text="Pending fixture-message"
        ).locator(".work-status")
        self.expect(status).to_be_visible()
        self.assertEqual(status.evaluate(read_label_and_color), reference.evaluate(read_label_and_color))


if __name__ == "__main__":
    unittest.main()
