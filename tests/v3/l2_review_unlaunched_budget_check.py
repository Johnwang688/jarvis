"""An unlaunched reviewer job should leave room in the daily cap."""

import unittest
from unittest.mock import patch

import l2_review_queue_lifecycle_check as lifecycle
from jarvis.v3 import claude_jobs, reviewer


class UnlaunchedReview(unittest.TestCase):
    def setUp(self):
        fixture = lifecycle.ReviewerLifecycle()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.fx = fixture
        fixture.append_turns(0, 10)
        fixture.ingest()
        fixture.enqueue()

    def test_missing_cli_leaves_capacity_for_retry_after_recovery(self):
        fx = self.fx
        fake_job = fx.run_job
        fx.run_job = claude_jobs.run_job
        with patch.object(claude_jobs, "resolve_cli", return_value=None):
            self.assertEqual(fx.tick(fx.budget(cap=1)), ["error"])

        fx.run_job = fake_job
        self.assertEqual(fx.tick(fx.budget(cap=1)), ["ok"])
        fake_job.assert_called_once()

    def test_crash_before_runner_entry_leaves_capacity_for_retry(self):
        fx = self.fx
        with patch.object(reviewer.Reviewer, "_run_admitted", side_effect=lifecycle.Crash):
            with self.assertRaises(lifecycle.Crash):
                fx.tick(fx.budget(cap=1))
        fx.run_job.assert_not_called()

        self.assertEqual(fx.tick(fx.budget(cap=1)), ["ok"])
        fx.run_job.assert_called_once()


if __name__ == "__main__":
    unittest.main()
