from datetime import timedelta
import unittest
from unittest.mock import Mock

import messages_check as fixtures
from jarvis.v3 import daily_briefs


class OutsideText(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.Delivery()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def evening(self, events):
        fixture = self.fixture
        runner = Mock(return_value=fixtures.JobResult(
            "brief", "haiku", ok=True, outcome="ok", text="Invalid email fixture."
        ))
        result = daily_briefs.run(
            "evening", feed=fixture.feed, budget=fixture.budget, now=fixture.clock(),
            calendar=lambda *_: events, messages=fixture.messages, run_job=runner,
        )
        self.assertEqual(runner.call_args.kwargs["tools"], ())
        return result, runner.call_args.args[1]

    def test_fallback_does_not_relay_hidden_markup_from_a_calendar_title(self):
        fixture = self.fixture
        result, _ = self.evening([{
            "id": "fixture-event",
            "title": "Planning session <!-- fixture annotation -->",
            "start": fixture.now + timedelta(days=1),
        }])
        self.assertIn(result.outcome, {"sent", "posted"})
        for call in fixture.t3.start.call_args_list:
            self.assertNotIn("<!--", call.args[1])

    def test_evening_writer_sees_at_most_six_items(self):
        fixture = self.fixture
        events = [
            {"id": f"event-{index}", "title": f"Planning session {index}",
             "start": fixture.now + timedelta(days=1, minutes=index)}
            for index in range(1, 10)
        ]
        _, prompt = self.evening(events)
        self.assertIn("Planning session 6", prompt)
        self.assertNotIn("Planning session 7", prompt)
        self.assertNotIn("Planning session 9", prompt)


if __name__ == "__main__":
    unittest.main()
