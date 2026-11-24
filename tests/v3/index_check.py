"""Focused checks for v3 index recovery, Unicode and Codex tool records."""

from contextlib import closing
import json
import multiprocessing
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import config
from jarvis.tools import secrets
from jarvis.v3 import index as index_module
from jarvis.v3.index import Index
from jarvis.v3.ingest import transcripts
from jarvis.v3.tools.session_search import session_search


class IndexCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.tmp / "index" / "index.sqlite"
        self.roots = {source: self.tmp / source for source in ("claude", "codex", "jarvis")}
        for root in self.roots.values():
            root.mkdir()

    def write(self, source, rows):
        path = self.roots[source] / "fixture.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    @staticmethod
    def claude(text):
        return {"type": "user", "message": {"role": "user", "content": text}}

    def test_crash_between_batches_keeps_rows_and_cursor_in_step(self):
        transcript = self.write("claude", [self.claude(f"fixtureword{i}") for i in range(7)])
        first_batch_end = len(b"".join(transcript.read_bytes().splitlines(keepends=True)[:2]))

        def ingest_until_crash():
            with patch.object(transcripts, "_BATCH_LINES", 2), Index(self.path) as index:
                original = index._append
                calls = 0

                def crash_after_insert(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    result = original(*args, **kwargs)
                    if calls == 2:
                        os._exit(77)
                    return result

                with patch.object(index, "_append", side_effect=crash_after_insert):
                    transcripts.ingest(index, **self.roots)

        writer = multiprocessing.get_context("fork").Process(target=ingest_until_crash)
        writer.start()
        writer.join(10)
        if writer.is_alive():
            writer.kill()
            writer.join()
        self.assertEqual(writer.exitcode, 77)
        writer.close()

        with Index(self.path) as index:
            with closing(sqlite3.connect(self.path)) as db:
                self.assertEqual(db.execute("SELECT offset FROM cursors").fetchone()[0], first_batch_end)
                self.assertEqual(db.execute("SELECT count(*) FROM chunks").fetchone()[0], 2)
            self.assertFalse(index.discover("fixtureword2"))
            stats = transcripts.ingest(index, **self.roots)
            self.assertEqual((stats.lines, stats.chunks), (5, 5))
            self.assertEqual(transcripts.ingest(index, **self.roots).chunks, 0)
            with closing(sqlite3.connect(self.path)) as db:
                self.assertEqual(db.execute("SELECT seq FROM chunks ORDER BY seq").fetchall(),
                                 [(i,) for i in range(1, 8)])

    def test_schema_bump_rebuilds_rows_fts_and_cursors(self):
        self.write("claude", [self.claude("rebuildfixture")])
        with Index(self.path) as index:
            transcripts.ingest(index, **self.roots)
        bumped = index_module.SCHEMA_VERSION + 1
        with patch.object(index_module, "SCHEMA_VERSION", bumped), Index(self.path) as index:
            self.assertFalse(index.discover("rebuildfixture"))
            self.assertEqual(transcripts.ingest(index, **self.roots).chunks, 1)
            self.assertTrue(index.discover("rebuildfixture"))
            with closing(sqlite3.connect(self.path)) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], bumped)
        with self.assertRaisesRegex(RuntimeError, "newer"):
            Index(self.path)

    def test_unicode_and_chunk_boundaries(self):
        with Index(self.path) as index:
            body = "é " * 1_999 + "東京大学 日本 Café 🐦 Straße"
            self.assertEqual(index.add("claude", "fixture", title="Unicode", ts=1,
                                       messages=[("user", body, None)]), 2)
            for query, expected in (("cafe", "Café"), ("京大学", "東京大学"), ("日本", "日本")):
                self.assertIn(expected, session_search(query, index=index))
            doc = index.recent(1)[0]["id"]
            self.assertIn(body, session_search(mode="read", doc=doc, index=index))

    def test_codex_tool_names_and_outputs(self):
        records = [
            {"type": "function_call", "name": "lookup_fixture", "arguments": "excludedarguments"},
            {"type": "function_call_output", "output": "functionoutput"},
            {"type": "custom_tool_call", "name": "write_fixture", "input": "excludedinput"},
            {"type": "custom_tool_call_output", "output": [{"type": "input_text", "text": "customoutput"}]},
        ]
        self.write("codex", [{"type": "response_item", "payload": row} for row in records])
        with Index(self.path) as index:
            self.assertEqual(transcripts.ingest(index, **self.roots).chunks, 4)
            doc = index.recent(1)[0]["id"]
            out = session_search(mode="read", doc=doc, index=index)
            for seq, expected in enumerate(("lookup_fixture", "functionoutput", "write_fixture", "customoutput"), 1):
                self.assertIn(f"#{seq} tool {expected}", out)
            self.assertNotIn("excluded", out)

    def test_same_inode_regrowth_checks_cursor_bytes(self):
        path = self.write("claude", [self.claude("before rewrite")])
        with Index(self.path) as index:
            transcripts.ingest(index, **self.roots)
            original = path.stat()
            self.write("claude", [self.claude("after rewrite"), self.claude("tail")])
            updated = path.stat()
            self.assertEqual(original.st_ino, updated.st_ino)
            self.assertGreater(updated.st_size, original.st_size)
            self.assertEqual(transcripts.ingest(index, **self.roots).chunks, 2)
            self.assertFalse(index.discover("before"))
            self.assertTrue(index.discover("after"))

    def test_index_default_and_override_are_resolved_at_open(self):
        with patch.dict("os.environ", JARVIS_INDEX_DB=""), patch.object(config, "V3_DIR", self.tmp / "v3"):
            with Index() as index:
                self.assertEqual(index.path, config.V3_DIR / "index.sqlite")
        with patch.dict("os.environ", JARVIS_INDEX_DB=str(self.path)), Index() as index:
            self.assertEqual(index.path, self.path)

    def test_metadata_is_redacted_before_json_escaping(self):
        fake = 'fake"token\\with-escapes'
        with Index(self.path) as index, patch.object(secrets, "secret_values", return_value=[fake]):
            index.add("claude", "fixture", title="Metadata fixture", ts=1, messages=[],
                      meta={"items": [{fake: fake}]})
            metadata = json.loads(index.recent(1)[0]["meta_json"])
        self.assertEqual(metadata, {"items": [{secrets.REDACTED: secrets.REDACTED}]})


if __name__ == "__main__":
    unittest.main()
