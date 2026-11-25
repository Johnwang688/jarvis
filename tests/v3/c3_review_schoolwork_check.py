import sqlite3
import unittest
from unittest.mock import Mock, patch

from c3_acceptance_check import Base, FakeSw
from jarvis.v3.feed import Feed
from jarvis.v3.ingest.schoolwork import SchoolworkIngester


class SchoolworkBriefReview(Base):
    @patch("jarvis.tools.secrets.secret_values", return_value=[])
    def test_durable_sign_in_brief_survives_index_rollback_without_repost(self, _secrets):
        sw = FakeSw()
        push = Mock()
        feed_root = self.tmp / "feed"

        def runner():
            feed = Feed(feed_root, push=push)
            return self.runner(SchoolworkIngester(sw, post=feed.post, signals_dir=self.sig))

        self.assertIsNone(runner().tick()["schoolwork"]["error"])
        self.assertEqual(Feed(feed_root).briefs(), [])
        sw.review["health"] = [{"code": "needs_login", "severity": "error", "message": "Fake CLI detail"}]
        checkpoint = self.index.get_ingest_cursor("schoolwork")
        with patch.object(self.index, "save_ingest_cursor", side_effect=sqlite3.OperationalError("disk full")):
            self.assertEqual(runner().tick()["schoolwork"]["error"], "ingest_error")
        self.assertEqual(self.index.get_ingest_cursor("schoolwork"), checkpoint)
        self.assertIsNone(runner().tick()["schoolwork"]["error"])

        push.assert_not_called()
        briefs = Feed(feed_root).briefs()
        self.assertEqual(len(briefs), 1)
        self.assertEqual((briefs[0]["kind"], briefs[0]["route"]), ("news", "batch"))
        self.assertIn("sw login-msa", briefs[0]["ask"])
        self.assertNotIn("Fake CLI detail", briefs[0]["ask"])


if __name__ == "__main__":
    unittest.main()
