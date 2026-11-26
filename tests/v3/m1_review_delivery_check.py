from datetime import timedelta
import unittest
from unittest.mock import patch

import messages_check as fixtures


class CompletionDelivery(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.Delivery()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.fixture.register("example")

    def test_started_notice_does_not_also_post_a_feed_card_when_start_record_fails(self):
        fixture = self.fixture
        fixture.work_thread("Check the fixture", "completed")
        with patch.object(fixture.ledger, "record_start", side_effect=OSError("Fixture disk write failed")):
            fixture.watcher().run()
        self.assertEqual(fixture.t3.start.call_count - 1 + len(fixture.feed.briefs()), 1)

    def test_cap_accounts_for_an_accepted_start_whose_record_failed(self):
        fixture = self.fixture
        messages = fixtures.MessageService(fixture.work, clock=fixture.clock, cap_per_day=1)
        with patch.object(fixture.ledger, "record_start", side_effect=OSError("Fixture disk write failed")):
            messages.send(fixtures.Message("First fixture", "Fixture body.", "example"))
        fresh = fixtures.MessageService(fixture.work, clock=fixture.clock, cap_per_day=1)
        fresh.send(fixtures.Message("Second fixture", "Fixture body.", "example"))
        self.assertEqual(fixture.t3.start.call_count, 1)

    def test_feed_fallback_stays_final_after_restart(self):
        fixture = self.fixture
        fixture.work_thread("Check the fixture", "completed")
        with (patch.object(fixture.t3.start, "side_effect", fixtures.T3Error("failed")),
              patch.object(fixture.ledger, "record_status", side_effect=OSError("Fixture disk write failed"))):
            self.assertEqual(fixture.watcher().run().errors, 1)
        self.assertEqual(len(fixture.feed.briefs()), 1)
        fixture.t3.start.reset_mock()
        self.assertEqual(fixture.watcher().run().errors, 0)
        fixture.t3.start.assert_not_called()

    def test_evening_email_that_fails_after_queuing_has_a_feed_fallback(self):
        fixture = self.fixture
        result = fixture.evening("Subject: Draft review\n\nReview the sample first. Reply with where to start.")
        self.assertEqual(result.outcome, "sent")
        (message,) = fixture.ledger.threads()
        fixture.states[message.thread_id] = fixtures.State(
            message.thread_id, "failed", False, 0, message.title, "codex", "gpt-6.2-sol", [],
        )
        self.assertEqual(fixture.watcher().run().errors, 0)
        self.assertEqual(fixture.ledger.get(message.thread_id).status, "failed")
        self.assertEqual(sum(row["kind"] == "digest" for row in fixture.feed.briefs()), 1)

    def test_work_send_and_second_finish_do_not_announce_same_thread_again(self):
        fixture = self.fixture
        coding = fixture.work_thread("Check the fixture", "completed")
        self.assertEqual(fixture.watcher().run().posted, 1)
        fixture.now += timedelta(minutes=3)
        fixture.t3.send.return_value = fixtures.Run(coding.thread_id, "fixture-followup", "queued")
        fixture.work.send(coding.thread_id, "Please check the other fixture.")
        self.assertEqual(fixture.ledger.get(coding.thread_id).status, "running")
        fixture.now += timedelta(minutes=3)
        self.assertEqual(fixture.watcher().run().errors, 0)
        self.assertEqual(fixture.ledger.get(coding.thread_id).status, "completed")
        self.assertEqual(fixture.t3.start.call_count, 2)
        self.assertEqual(fixture.feed.briefs(), [])


if __name__ == "__main__":
    unittest.main()
