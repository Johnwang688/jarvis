"""Check review delivery when a provider reuses a transcript path."""

import unittest

import l2_review_queue_lifecycle_check as lifecycle
from jarvis.v3.reviewer import MAX_AGE_S


class TranscriptReplacement(unittest.TestCase):
    def setUp(self):
        fixture = lifecycle.ReviewerLifecycle()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.fx = fixture

    def test_fresh_rotated_session_does_not_inherit_expired_receipt(self):
        fx = self.fx
        fx.append_turns(0, 10)
        fx.enqueue()
        self.assertEqual(fx.tick(), [])
        fx.run_job.assert_not_called()

        fx.transcript.rename(fx.transcript.with_suffix(".old"))
        fx.now += MAX_AGE_S + 1
        fx.append_turns(10, 20)
        fx.ingest()
        fx.enqueue()

        self.assertIn("ok", fx.restart_and_tick())
        fx.run_job.assert_called_once()

    def test_truncated_and_regrown_session_gets_a_fresh_review(self):
        fx = self.fx
        fx.append_turns(0, 10)
        fx.enqueue()
        self.assertEqual(fx.tick(), [])
        old_size = fx.transcript.stat().st_size
        old_inode = fx.transcript.stat().st_ino

        fx.transcript.write_text("", encoding="utf-8")
        fx.append_turns(10, 20)
        self.assertEqual(fx.transcript.stat().st_ino, old_inode)
        self.assertGreaterEqual(fx.transcript.stat().st_size, old_size)
        fx.ingest()
        fx.enqueue()

        self.assertEqual(fx.restart_and_tick(), ["ok"])
        fx.run_job.assert_called_once()


if __name__ == "__main__":
    unittest.main()
