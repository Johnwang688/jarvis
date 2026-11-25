from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from jarvis.v3.feed import Feed
from jarvis.v3.tools.feed import FeedTools


class AgentReplyCheck(unittest.TestCase):
    def test_capacity_reply_does_not_distinguish_other_sessions_activity(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        now = datetime(2031, 1, 10, 14, tzinfo=UTC)
        replies = []
        for name, other_posts in (("session-only", 0), ("both-limits", 15)):
            feed = Feed(root / name, clock=lambda: now)
            caller = FeedTools(feed, session="caller")
            for _ in range(5):
                self.assertEqual(caller.brief_post("news", "Build finished."), "Brief posted.")
            for i in range(other_posts):
                other = FeedTools(feed, session=f"other-{i}")
                self.assertEqual(other.brief_post("news", "Build finished."), "Brief posted.")
            replies.append(caller.brief_post("news", "Build finished."))

        # The caller has the same five open briefs in both stores. Closing one
        # still leaves a refusal in the second store, with the same public text.
        mine = next(row for row in feed.briefs() if row["session"] == "caller")
        feed.answer(mine["id"], "not_now")
        replies.append(caller.brief_post("news", "Build finished."))
        for reply in replies:
            self.assertTrue(reply.startswith("Error:"), reply)
        self.assertEqual(replies[0], replies[1])
        self.assertEqual(replies[0], replies[2])


if __name__ == "__main__":
    unittest.main()
