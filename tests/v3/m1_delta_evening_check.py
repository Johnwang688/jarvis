from datetime import timedelta
import unittest
from unittest.mock import Mock

import messages_check as fixtures
from jarvis.v3 import daily_briefs


class WriterOutput(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.Delivery()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def test_incomplete_model_draft_does_not_silently_drop_calendar_items(self):
        fixture = self.fixture
        topics = ("Budget", "Design", "Release", "Hiring", "Renewal", "Training",
                  "Research", "Planning", "Inventory")
        events = [
            {"id": f"event-{index}", "title": f"{topic} session",
             "start": fixture.now + timedelta(days=1, minutes=index)}
            for index, topic in enumerate(topics)
        ]
        draft = ("Subject: Tomorrow’s agenda\n\nYour budget discussion is first. "
                 "I’d review the figures at 3:30 — it’s a short read.\n\n"
                 "Reply if you’d like me to prepare the figures.")
        runner = Mock(return_value=fixtures.JobResult("brief", "haiku", ok=True, outcome="ok", text=draft))
        result = daily_briefs.run(
            "evening", feed=fixture.feed, budget=fixture.budget, now=fixture.clock(),
            calendar=lambda *_: events, messages=fixture.messages, run_job=runner,
        )
        self.assertEqual((result.outcome, result.summary), ("sent", "model"))
        footer = '\nAnd three more: reply "more" and I will list them.'
        prompt = runner.call_args.args[1]
        self.assertIn(f"at most {1800 - len(footer):,} characters", prompt)
        for topic in topics[:6]:
            self.assertIn(f"{topic} session", prompt)
        for topic in topics[6:]:
            self.assertNotIn(f"{topic} session", prompt)
        wire = fixture.t3.start.call_args.args[1]
        body = wire.split("\n\n", 3)[-1].rsplit("\n", 1)[0]
        self.assertLessEqual(len(body), 1800)
        self.assertEqual(body, draft.split("\n\n", 1)[1] + footer)

    def test_evening_draft_keeps_an_ordinary_comparison(self):
        draft = ("Subject: Review tomorrow’s draft\n\n"
                 "Let’s meet at 3:30 — I’ve put the details in draft.txt.\n\n"
                 "1. Review the sample where count<limit.\n"
                 "2. Confirm that 3 < 5.\n\nReply with ‘start’ when you’re ready.")
        result = self.fixture.evening(draft)
        self.assertEqual((result.outcome, result.summary), ("sent", "model"))
        self.assertIn("count< limit", self.fixture.t3.start.call_args.args[1])

    def test_completion_quote_keeps_an_ordinary_comparison(self):
        fixture = self.fixture
        fixture.register("example")
        thread = fixture.work_thread("Check the sample", "completed")
        final = "I checked stats.py at 3:30 — the count<limit case passed, as did 3 < 5."
        fixture.states[thread.thread_id] = fixtures.replace(
            fixture.states[thread.thread_id], messages=[("assistant", final)],
        )
        self.assertEqual(fixture.watcher().run().posted, 1)
        self.assertEqual(fixture.t3.start.call_count, 2)
        self.assertIn(final.replace("count<limit", "count< limit"), fixture.t3.start.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
