"""I2 must reject malformed schoolwork sign-in metadata before evaluation."""

import unittest
from unittest.mock import Mock

from i2_acceptance_check import Base, NOW, pr, sw
from jarvis.v3 import triggers
from jarvis.v3.feed import Feed


class SchoolworkPreflight(Base):
    def test_malformed_sign_in_metadata_blocks_only_schoolwork(self):
        push = Mock()
        feed = Feed(self.tmp / "feed", push=push)
        self.posts = feed.post
        sources = ("schoolwork", "github")
        essay = sw("essay", "2027-01-17T08:00:00Z")
        self.put("schoolwork", [essay], needs_sign_in=False)
        self.put("github", [pr(1)])
        self.assertEqual(self.tick(sources)["emitted"], 1)

        self.put("schoolwork", [sw("quiz", essay["due"])], needs_sign_in="true")
        self.put("github", [pr(1, review_requested=1)])
        first = self.tick(sources)
        second = self.tick(sources, at=NOW + 300)

        with self.subTest("blocked source and concurrent GitHub signal"):
            self.assertEqual([row["kind"] for row in self.log(after=1)], ["gh.review_requested"])
            self.assertEqual(first, {"emitted": 1, "blocked_config": {"schoolwork": "unreadable"}})
            self.assertEqual(second, {"emitted": 0, "blocked_config": {"schoolwork": "unreadable"}})
        with self.subTest("one fixed notice and no push"):
            push.assert_not_called()
            notices = feed.batch()
            self.assertEqual(len(notices), 1)
            notice = notices[0]
            self.assertEqual((notice["kind"], notice["rank"], notice["ask"]),
                             ("news", 4, triggers.BLOCKED_ASK["schoolwork"]))
        self.put("schoolwork", [essay], needs_sign_in=False)
        self.assertEqual(self.tick(sources, at=NOW + 600)["emitted"], 0)


if __name__ == "__main__":
    unittest.main()
