from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from jarvis.tools import secrets
from jarvis.v3 import daily_briefs
from jarvis.v3.claude_jobs import JobResult
from jarvis.v3.feed import Feed


class BriefPromptRegressionCheck(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.now = datetime(2030, 1, 15, 7, 30, tzinfo=ZoneInfo("America/Chicago"))
        self.feed = Feed(self.root / "feed", clock=lambda: self.now)
        self.enterContext(patch.object(secrets, "secret_values", return_value=[]))
        self.runner = Mock(return_value=JobResult("brief", "haiku", ok=True, outcome="ok", text="Lab today."))
        self.budget = Mock()
        self.budget.admit.return_value.ok = True

    def post(self, title, event_id="example"):
        def calendar(start, end):
            return [{"id": event_id, "title": title, "start": start}]

        result = daily_briefs.run("morning", feed=self.feed, budget=self.budget,
                                  now=self.now.timestamp(), state_dir=self.root / "daily-briefs",
                                  calendar=calendar, run_job=self.runner)
        self.assertEqual(result.outcome, "posted")
        return self.feed.get(result.brief_id), self.runner.call_args.args[1]

    def test_source_refs_stay_in_feed_and_out_of_model_prompt(self):
        brief, prompt = self.post("Lab today", event_id="example-opaque-event-id")
        self.assertEqual(brief["context"], [{"label": "Lab today", "ref": "calendar:example-opaque-event-id"}])
        self.assertIn("Lab today", prompt)
        self.assertNotIn("example-opaque-event-id", prompt)

    def test_multiline_private_key_tail_is_scrubbed_before_flattening(self):
        fake_lines = ("ZmFrZVRva2VuQQ==", "ZmFrZVRva2VuQg==")
        title = "Lab\n" + "\n".join((*fake_lines, "-----END PRIVATE KEY-----"))
        brief, prompt = self.post(title)
        label = brief["context"][0]["label"]
        self.assertTrue(label.startswith("Lab"))
        for surface, text in (("feed", label), ("prompt", prompt)):
            with self.subTest(surface=surface):
                for fake_line in fake_lines:
                    self.assertNotIn(fake_line, text)


if __name__ == "__main__":
    unittest.main()
