"""Fixture regressions for transcript edits and ingest budgets."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest

from jarvis.v3.index import Index
from jarvis.v3.ingest import transcripts


def line(text):
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n"


class IngestReview(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        project = root / "claude" / "project"
        project.mkdir(parents=True)
        self.path = project / "session.jsonl"
        self.roots = dict(claude=project.parent, codex=root / "codex", jarvis=root / "jarvis")
        self.index = self.enterContext(Index(root / "index" / "index.sqlite"))

    def ingest(self, **kwargs):
        return transcripts.ingest(self.index, **self.roots, **kwargs)

    def texts(self):
        return [row["text"] for row in self.index.read(self.index.recent(1)[0]["id"])["messages"]]

    def test_writer_honors_budget_during_lock_contention(self):
        self.path.write_text(line("waiting to be indexed"))
        with Index(self.index.path) as contender:
            locked = threading.Event()
            release = threading.Event()

            def hold_lock():
                with closing(sqlite3.connect(self.index.path, isolation_level=None)) as db:
                    db.execute("BEGIN IMMEDIATE")
                    locked.set()
                    release.wait(1.5)

            holder = threading.Thread(target=hold_lock)
            holder.start()
            try:
                self.assertTrue(locked.wait(2))
                start = time.monotonic()
                result = transcripts.ingest(contender, **self.roots, budget_s=0.05)
                elapsed = time.monotonic() - start
            finally:
                release.set()
                holder.join()
        self.assertLess(elapsed, 0.75, "a busy writer outlasted the ingest budget")
        self.assertFalse(result.done)
        self.ingest()
        self.assertEqual(self.texts(), ["waiting to be indexed"])


if __name__ == "__main__":
    unittest.main()
