import json
import unittest
from unittest.mock import patch

from c3_acceptance_check import Base, gh_pr
from jarvis.v3.ingest.github import GitHubIngester


class GitHubSearchReview(Base):
    @patch("jarvis.tools.secrets.secret_values", return_value=[])
    def test_open_pr_displaced_to_second_page_is_not_deleted(self, _secrets):
        repo = "example-org/demo"
        original = gh_pr(repo, 1, "Still open")
        recent = [gh_pr(repo, n, "More recent activity") for n in range(2, 102)]
        first_poll = self.now

        def cli(argv):
            if self.now == first_poll:
                nodes, count, more = [*recent[:99], original], 100, False
            elif any("fake-next-page" in arg for arg in argv):
                nodes, count, more = [original], 101, False
            else:
                nodes, count, more = recent, 101, True
            authored = {"nodes": nodes, "issueCount": count,
                        "pageInfo": {"hasNextPage": more, "endCursor": "fake-next-page" if more else None}}
            review = {"nodes": [], "issueCount": 0, "pageInfo": {"hasNextPage": False, "endCursor": None}}
            return 0, json.dumps({"data": {"authored": authored, "review": review}})

        runner = self.runner(GitHubIngester(cli, signals_dir=self.sig))
        ref = f"{repo}#1"
        self.assertIsNone(runner.tick()["github"]["error"])
        self.assertIn(ref, self.items("github"))

        # A newly active PR takes the last slot on the first search page.
        self.now += 301
        result = runner.tick()["github"]

        self.assertEqual(result["deleted"], 0)
        self.assertTrue(self.index.discover("Still open"))
        self.assertIn(ref, self.items("github"))


if __name__ == "__main__":
    unittest.main()
