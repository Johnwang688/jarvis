"""Route feedback uses synthetic transcripts and isolated daemon roots."""
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from jarvis.v2 import route_feedback


class RouteFeedbackCheck(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.feedback = route_feedback.RouteFeedback(self.root)
        collection = patch.dict(os.environ, {"JARVIS_ROUTE_FEEDBACK": "1"})
        collection.start()
        self.addCleanup(collection.stop)

    def test_transcript_and_allowlisted_proposal_are_stored_without_context(self):
        transcript = "  Yo, open example.org for me.  "
        revision = "f" * 40
        token = self.feedback.record("assistant", transcript, {
            "kind": "clarify",
            "proposal": {"kind": "open_app", "target": "private window name"},
            "scores": {"open_app": 0.99, "navigate": 0.01, "ask": float("nan"),
                       "private score key": 0.5},
            "revision": revision,
            "text": "private decision text",
            "target": "private target",
            "selection": "private selection",
            "audio": b"private audio",
            "windows": ["private window name"],
        })

        turn, = self.feedback.recent()
        self.assertEqual(turn["id"], token)
        self.assertEqual(turn["text"], transcript)
        self.assertEqual(turn["mode"], "assistant")
        self.assertEqual(turn["kind"], "clarify")
        self.assertEqual(turn["proposal"], "open_app")
        self.assertEqual(turn["scores"], {"open_app": 0.99, "navigate": 0.01})
        self.assertEqual(turn["revision"], revision)
        self.assertEqual(turn["schema_version"], 1)
        self.assertNotIn(b"private", self.feedback.path.read_bytes())
        self.assertEqual(stat.S_IMODE(self.feedback.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.feedback.path.parent.stat().st_mode), 0o700)

    def test_excluded_sources_and_literal_text_never_reach_disk(self):
        secret = "synthetic secret sentinel"
        for mode, text, decision in [
            ("dictation", secret, {"kind": "ask"}),
            ("hotkey", secret, {"kind": "ask"}),
            ("chat", secret, {"kind": "ask"}),
            ("wake", secret, {"kind": "literal", "target": secret}),
            ("assistant", secret, {"kind": "dictate"}),
            ("assistant", secret, {"kind": "ask", "proposal": {"kind": "literal"}}),
            ("wake", f"Jarvis, type the words {secret}", {"kind": "clarify"}),
            ("assistant", f"Could you just type {secret}?", {"kind": "ask"}),
            ("wake", f"Yo, Jarvis, please insert: {secret}", {"kind": "unavailable"}),
            ("assistant", "a" * 2001, {"kind": "ask"}),
        ]:
            with self.subTest(mode=mode, text=text, decision=decision):
                self.assertIsNone(self.feedback.record(mode, text, decision))
        with patch.dict(os.environ, {"JARVIS_ROUTE_FEEDBACK": "0"}):
            self.assertIsNone(self.feedback.record("wake", secret, {"kind": "ask"}))
        self.assertFalse(self.feedback.path.parent.exists())

    def test_outcomes_are_idempotent_and_only_review_produces_examples(self):
        transcript = "Could you open example.org?"
        token = self.feedback.record("wake", transcript, {"kind": "clarify"})
        reopened = route_feedback.RouteFeedback(self.root)
        statuses = ["clarified", "confirmed", "failed", "answered", "cancelled", "unavailable"]
        for status in statuses + statuses + ["private arbitrary status"]:
            reopened.outcome(token, status)
        reopened.outcome("missing", "failed")
        reopened.outcome(None, "failed")

        turn, = reopened.recent()
        self.assertEqual(turn["outcomes"], statuses)
        self.assertEqual(list(reopened.examples()), [])
        with self.assertRaises(ValueError):
            reopened.correct(token, "confirmed")
        self.assertFalse(reopened.correct("missing", "navigate"))
        self.assertTrue(reopened.correct(token, "clarify"))
        self.assertTrue(reopened.correct(token, "navigate"))
        self.assertTrue(reopened.correct(token, "navigate"))
        item, = list(self.feedback.examples())
        self.assertEqual(item, {"id": token, "text": transcript, "label": "navigate",
                                "mode": "wake", "schema_version": 1})

    def test_concurrent_first_writes_and_retention(self):
        def record(_):
            journal = route_feedback.RouteFeedback(self.root)
            token = journal.record("assistant", "What is an example?", {"kind": "ask"})
            journal.outcome(token, "answered")
            return token

        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(record, range(12)))
        self.assertNotIn(None, ids)
        self.assertEqual(len(set(ids)), 12)
        self.assertTrue(all(turn["outcomes"] == ["answered"] for turn in self.feedback.recent()))

        old_id = self.feedback.recent()[-1]["id"]
        self.feedback.correct(old_id, "ask")
        with patch.object(route_feedback, "_MAX_TURNS", 2):
            newest = record(12)
        self.assertEqual(len(self.feedback.recent()), 2)
        self.assertEqual(self.feedback.recent()[0]["id"], newest)
        self.assertEqual(list(self.feedback.examples()), [])
        self.feedback.outcome(old_id, "failed")
        self.assertFalse(self.feedback.correct(old_id, "ask"))
        with closing(sqlite3.connect(self.feedback.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0], 2)

    def test_failed_retention_write_rolls_back_the_new_turn(self):
        old_id = self.feedback.record("wake", "First example", {"kind": "ask"})
        db = sqlite3.connect(self.feedback.path)
        try:
            db.execute("""
                CREATE TRIGGER fail_retention BEFORE DELETE ON turns
                BEGIN SELECT RAISE(ABORT, 'synthetic retention failure'); END
            """)
            with patch.object(route_feedback, "_MAX_TURNS", 1):
                with self.assertLogs(route_feedback.LOG, "WARNING"):
                    self.assertIsNone(
                        self.feedback.record("wake", "Second example", {"kind": "ask"})
                    )
                self.assertEqual([turn["id"] for turn in self.feedback.recent()], [old_id])
                db.execute("DROP TRIGGER fail_retention")
                new_id = self.feedback.record("wake", "Second example", {"kind": "ask"})
                self.assertIsNotNone(new_id)
                self.assertEqual([turn["id"] for turn in self.feedback.recent()], [new_id])
        finally:
            db.close()

    def test_empty_database_left_before_initialization_recovers(self):
        self.feedback.path.parent.mkdir(mode=0o700)
        self.feedback.path.touch(mode=0o600)
        self.assertEqual(self.feedback.recent(), [])
        self.assertIsNotNone(self.feedback.record("wake", "What time is it?", {"kind": "ask"}))

    def test_cli_uses_daemon_root_and_exports_only_the_reviewed_turn(self):
        env = {**os.environ, "JARVIS_V2_DATA": str(self.root)}

        def cli(*args):
            return subprocess.run([sys.executable, "-m", "jarvis.v2.route_feedback", *args],
                                  env=env, capture_output=True, text=True)

        self.assertIn("No voice routing turns", cli("recent").stdout)
        self.assertFalse(self.feedback.path.exists())
        token = self.feedback.record("assistant", "Open example.org for me", {"kind": "clarify"})
        self.feedback.record("wake", "Another question", {"kind": "ask"})
        self.feedback.outcome(token, "clarified")
        self.assertTrue(re.fullmatch(r"[0-9a-f]{32}", token))
        recent = cli("recent")
        self.assertEqual(recent.returncode, 0, recent.stderr)
        self.assertIn(token, recent.stdout)
        self.assertIn("Open example.org for me", recent.stdout)
        self.assertIn("clarified", recent.stdout)
        self.assertEqual(cli("export").stdout, "")
        self.assertEqual(cli("correct", token, "failed").returncode, 2)
        self.assertEqual(cli("correct", "unknown", "navigate").returncode, 1)
        corrected = cli("correct", token, "navigate")
        self.assertEqual(corrected.returncode, 0, corrected.stderr)
        exported = cli("export")
        self.assertEqual(exported.returncode, 0, exported.stderr)
        item, = map(json.loads, exported.stdout.splitlines())
        self.assertEqual((item["id"], item["label"]), (token, "navigate"))


if __name__ == "__main__":
    unittest.main()
