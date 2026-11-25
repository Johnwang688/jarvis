"""Open an origin/main index and resume both ingesters after migration."""

import base64
from importlib import import_module, util
import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from jarvis.v3 import consolidate
from jarvis.v3.budget import Budget
from jarvis.v3.claude_jobs import JobResult
from jarvis.v3.index import Index
from jarvis.v3.ingest.transcripts import ingest


NOW = 1_895_000_000
DAY = 86_400



def _need(name):
    """Comparisons with main's files run only when the caller supplies them."""
    value = os.environ.get(name)
    if not value:
        raise unittest.SkipTest(f"set {name} to run this comparison with main's files")
    return value

class Mailbox:
    updated = False

    def profile(self):
        return {"emailAddress": "user@example.com", "historyId": "100"}

    def list(self, query, page_token):
        return {"messages": [{"id": "abc"}]}

    def history(self, start_history_id, page_token):
        assert start_history_id == "100"
        return {"historyId": "101", "history": [{
            "labelsAdded": [{"message": {"id": "abc"}}],
            "messagesAdded": [{"message": {"id": "def"}}],
        }]}

    def get(self, ref):
        body = base64.urlsafe_b64encode(f"Fixture mail {ref}".encode()).decode()
        return {"internalDate": str((NOW - 10) * 1000),
                "labelIds": ["SENT"] if self.updated else ["INBOX"],
                "payload": {"mimeType": "text/plain", "body": {"data": body},
                            "headers": [{"name": "Subject", "value": f"Jarvis: fixture {ref}"}]}}


class Migration(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.path = self.root / "index.sqlite"
        self.roots = {kind: self.root / kind for kind in ("claude", "codex", "jarvis")}
        self.roots["claude"].mkdir()
        self.transcript = self.roots["claude"] / "fixture.jsonl"
        source = Path(_need("P1_MAIN_FILES"))
        spec = util.spec_from_file_location("p1_main", source / "__init__.py")
        main = util.module_from_spec(spec)
        self.enterContext(patch.dict(sys.modules, {"p1_main": main}))
        spec.loader.exec_module(main)
        self.main_ingest = import_module("p1_main.ingest")
        self.main_gmail = import_module("p1_main.ingest.gmail")
        self.mail = Mailbox()
        self.write_message("Original transcript fixture.")
        with import_module("p1_main.index").Index(self.path) as index:
            runner = self.main_ingest.Runner(index, [self.main_gmail.GmailIngester(self.mail, clock=lambda: NOW)])
            self.assertEqual(runner.tick()["gmail"]["docs"], 1)
            self.assertEqual(ingest(index, **self.roots).chunks, 1)

    def write_message(self, body, *, mode="a"):
        with self.transcript.open(mode, encoding="utf-8") as stream:
            row = {"type": "user", "timestamp": NOW - 10, "message": {"role": "user", "content": body}}
            stream.write(json.dumps(row) + "\n")

    def snapshot(self):
        with sqlite3.connect(self.path) as db:
            return {table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                    for table in ("docs", "chunks", "cursors", "ingest_cursors")}

    def test_migration_keeps_all_rows_and_transcript_changes_are_seen_once(self):
        before = self.snapshot()
        with Index(self.path) as index:
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(ingest(index, **self.roots).chunks, 0)
        self.write_message("Appended transcript fixture.")
        with Index(self.path) as index:
            self.assertEqual(ingest(index, **self.roots).chunks, 1)
        self.write_message("Replacement transcript fixture.", mode="w")
        with Index(self.path) as index:
            self.assertEqual(ingest(index, **self.roots).chunks, 1)
            hit = index.discover("Replacement transcript")[0]
            self.assertEqual([m["text"] for m in index.read(hit["id"])["messages"]],
                             ["Replacement transcript fixture."])
            self.assertEqual(index.discover("Original transcript"), [])
        self.assertEqual(self.snapshot()["ingest_cursors"], before["ingest_cursors"])

    def test_gmail_resumes_saved_cursor_and_replaces_mail(self):
        self.mail.updated = True
        with Index(self.path) as index:
            try:
                from jarvis.v3.ingest import Runner
                from jarvis.v3.ingest.gmail import GmailIngester
            except ImportError as exc:
                self.fail(f"Cannot resume main's Gmail cursor: {exc}")
            runner = Runner(index, [GmailIngester(self.mail, clock=lambda: NOW)])
            outcome = runner.tick()["gmail"]
            self.assertEqual((outcome["docs"], outcome["error"]), (2, None))
            self.assertEqual(index.get_ingest_cursor("gmail"), (2, {"history": "101"}))
            mail = {row["ref"]: index.read(row["id"])["messages"] for row in index.recent(10)
                    if row["source"] == "gmail"}
            self.assertEqual({ref: [item["role"] for item in messages] for ref, messages in mail.items()},
                             {"abc": ["owner"], "def": ["owner"]})

if __name__ == "__main__":
    unittest.main()
