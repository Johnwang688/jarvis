import json
import unittest
from unittest.mock import Mock, patch

from c3_acceptance_check import Base, gh_pr
from jarvis.v3.ingest.github import GitHubIngester


class GitHubDelta(Base):
    @patch("jarvis.tools.secrets.secret_values", return_value=[])
    def test_shrinking_search_does_not_delete_a_displaced_pr(self, _secrets):
        def page(rows, count, after=None):
            return {"nodes": rows, "issueCount": count,
                    "pageInfo": {"hasNextPage": after is not None, "endCursor": after}}

        repo = "example-org/demo"
        watched = gh_pr(repo, 1, "Still open")
        newer = [gh_pr(repo, i, "Recent change") for i in range(2, 102)]
        review = page([], 0)
        responses = [
            {"authored": page([watched], 1), "review": review},
            {"authored": page(newer, 101, "next-page"), "review": review},
            # A result before the cursor closed, shifting the last PR into
            # the previous page. The terminal page is now empty.
            {"authored": page([], 100)},
        ]
        cli = Mock(side_effect=[(0, json.dumps({"data": data})) for data in responses])
        runner = self.runner(GitHubIngester(cli, signals_dir=self.sig))
        self.assertIsNone(runner.tick()["github"]["error"])
        checkpoint = self.index.get_ingest_cursor("github")
        before = (self.sig / "github.json").read_bytes()
        self.now += 301
        result = runner.tick()["github"]

        self.assertEqual(cli.call_count, 3)
        self.assertIn("authoredAfter=next-page", cli.call_args.args[0])
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(self.index.get_ingest_cursor("github"), checkpoint)
        self.assertEqual((self.sig / "github.json").read_bytes(), before)

    @patch("jarvis.tools.secrets.secret_values", return_value=[])
    def test_review_request_with_missing_author_is_not_deleted(self, _secrets):
        repo = "example-org/demo"
        row = gh_pr(repo, 1, "Open review request")

        def cli(_argv):
            return 0, json.dumps({"data": {"authored": {"nodes": []},
                                         "review": {"nodes": [row], "issueCount": 1}}})

        runner = self.runner(GitHubIngester(cli, signals_dir=self.sig))
        self.assertIsNone(runner.tick()["github"]["error"])
        row["author"] = None
        self.now += 301
        result = runner.tick()["github"]

        self.assertEqual(result["deleted"], 0)
        self.assertIn(f"{repo}#1", self.metas())
        self.assertIn(f"{repo}#1", self.items("github"))


if __name__ == "__main__":
    unittest.main()
