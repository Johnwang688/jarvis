"""Check how the nightly job reports failures and respects its due predicate."""

from datetime import datetime
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from jarvis import config
from jarvis.v3 import consolidate, jobs
from jarvis.v3.index import Index


class ConsolidateScheduleCheck(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.index = self.root / "index" / "index.sqlite"
        self.state = self.root / "jobs" / "state.json"
        self.enterContext(patch.object(config, "V3_DIR", self.root / "v3"))
        self.cli = self.enterContext(patch.object(consolidate.claude_jobs, "resolve_cli",
                                                  return_value=None))
        self.capture = self.enterContext(patch.object(consolidate.claude_jobs, "_capture",
                                                      side_effect=AssertionError))
        self.now = datetime(2026, 11, 24, 3, 30, tzinfo=ZoneInfo("America/Chicago")).timestamp()

    def scheduler(self):
        nightly = next(job for job in jobs.default_jobs(index_path=self.index)
                       if job.name == "consolidate")
        return jobs.Scheduler([nightly], state=self.state, clock=lambda: self.now)

    def tick(self, scheduler):
        original_init = Index.__init__

        def open_index(index, *args, **kwargs):
            original_init(index, *args, **kwargs)
            self.addCleanup(index.close)

        with patch.object(Index, "__init__", open_index):
            return scheduler.tick()

    def test_restart_after_index_error_cannot_retry_before_next_nightly_window(self):
        self.now += (20 * 60 + 29) * 60 + 30  # 23:59:30 Chicago
        self.index.mkdir(parents=True)
        with patch.object(consolidate, "run", return_value=consolidate.Result("nothing")) as run:
            self.assertEqual(self.tick(self.scheduler())[0]["outcome"], "error")
            run.assert_not_called()
            self.index.rmdir()
            self.now += 60  # 00:00:30 the next day, after the scheduler's retry delay
            self.assertFalse(consolidate.due(self.now, last_run=self.now - 60))
            self.assertEqual(self.tick(self.scheduler()), [])
            run.assert_not_called()

    def test_missing_claude_cli_is_an_error_in_job_status(self):
        stamp = time.time()
        with Index(self.index) as index:
            index.add("claude", "fake-session", title="Fake transcript", ts=stamp,
                      messages=[("user", "Synthetic test message.", stamp)])
        scheduler = self.scheduler()
        rows = self.tick(scheduler)
        self.cli.assert_called_once_with()
        self.capture.assert_not_called()
        log = self.root / "v3" / "consolidate" / "log.jsonl"
        self.assertEqual(json.loads(log.read_text())["outcome"], "error")
        self.assertEqual(rows[0]["outcome"], "error")
        self.assertEqual(scheduler.status()["consolidate"]["last_outcome"], "error")


if __name__ == "__main__":
    unittest.main()
