"""Fixture regressions for M3's searchable text and credential handling."""
import json
from pathlib import Path
import tempfile
import unittest

from jarvis.v3.index import Index
from jarvis.v3.ingest.transcripts import ingest
from jarvis.v3.tools.session_search import session_search


class SearchReview(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.roots = {name: root / name for name in ("claude", "codex", "jarvis")}
        self.roots["claude"].mkdir()
        self.index = self.enterContext(Index(root / "index" / "index.sqlite"))

    def load_claude(self, *rows):
        contents = "".join(json.dumps(row) + "\n" for row in rows)
        (self.roots["claude"] / "example.jsonl").write_text(contents)
        ingest(self.index, **self.roots)

    def add(self, text):
        self.index.add("claude", "fixture", title="Fixture", ts=1, messages=[("user", text, None)])

    def search(self, query="", **kwargs):
        return session_search(query, index=self.index, **kwargs)

    def test_claude_meta_user_rows_are_excluded(self):
        self.load_claude(
            {"type": "user", "isMeta": True,
             "message": {"role": "user", "content": "<system-reminder>hiddenfixture</system-reminder>"}},
            {"type": "user", "message": {"role": "user", "content": "visiblefixture"}},
        )
        self.assertIn("visiblefixture", self.search("visiblefixture"))
        self.assertEqual(self.search("hiddenfixture"), "No matching sessions.")

    def test_code_search_excerpt_includes_the_match(self):
        self.add('Zoë said "hello" about src/cache-key.py in CI. Also typed foo::bar.')
        for query in ("src/cache-key.py", "cache-key"):
            with self.subTest(query=query):
                self.assertIn(query, self.search(query))


if __name__ == "__main__":
    unittest.main()
