"""The evening wrap-up survives failure of its optional learning source."""

from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from jarvis.v3 import daily_briefs
from jarvis.v3.feed import Feed


class WrapUpCheck(unittest.TestCase):
    def test_rejected_learning_line_does_not_sink_the_wrap_up(self):
        now = datetime(2030, 1, 1, 21, tzinfo=ZoneInfo("America/Chicago"))
        sources = (
            (Mock(side_effect=OSError("fixture failure")), True),
            (lambda: None, False),
            (lambda: {"label": "x" * 121, "ref": "jarvis:learning"}, True),
        )
        for index, (learning, unavailable) in enumerate(sources):
            with self.subTest(source=index), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                feed = Feed(root / "feed", clock=lambda: now)
                run_job = Mock(side_effect=AssertionError("Unexpected model call"))
                budget = Mock()
                budget.admit.return_value.ok = False
                result = daily_briefs.run(
                    "evening", now=now.timestamp(), state_dir=root / "briefs", feed=feed,
                    budget=budget, run_job=run_job, activity=lambda *_: {"notes updated": 2}, learning=learning,
                )
                run_job.assert_not_called()
                self.assertEqual(result.outcome, "posted")
                lines = feed.get(result.brief_id)["context"]
                self.assertEqual(lines[0]["ref"], "jarvis:activity")
                note = {"label": "Learning unavailable", "ref": "jarvis:learning"}
                self.assertEqual(lines.count(note), int(unavailable))


if __name__ == "__main__":
    unittest.main()
