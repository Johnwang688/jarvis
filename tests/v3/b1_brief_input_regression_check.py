from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from jarvis.v3.feed import Feed, FeedError
from jarvis.v3.tools.feed import FeedTools


class BriefInputCheck(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.feed = Feed(self.root, clock=lambda: datetime(2031, 1, 10, 14, tzinfo=UTC))

    def test_non_iso_separator_is_refused(self):
        with self.assertRaises(FeedError):
            self.feed.post({"kind": "news", "rank": 2, "ask": "Build finished.",
                            "due": "2031-01-10_15:00:00+00:00"})
        self.assertEqual(self.feed.briefs(), [])

    def test_url_password_is_withheld_before_append(self):
        password = "fake%40token"
        tool = FeedTools(self.feed, session="caller")
        reply = tool.brief_post("news", f"Check https://user:{password}@example.invalid/build")
        self.assertNotIn(password, reply)
        if reply.startswith("Error:"):
            self.assertEqual(self.feed.briefs(), [])
        else:
            row, = self.feed.batch()
            self.assertNotIn(password, (self.root / "briefs.jsonl").read_text())
            self.assertNotIn(password, row["ask"])


if __name__ == "__main__":
    unittest.main()
