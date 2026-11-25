from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from jarvis import config
from jarvis.tools import secrets
from jarvis.v3 import daily_briefs, jobs
from jarvis.v3.claude_jobs import JobResult
from jarvis.v3.feed import Feed


CHICAGO = ZoneInfo("America/Chicago")


class BriefScheduleRegressionCheck(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.state = self.root / "daily-briefs"
        self.feed_root = self.root / "feed"
        self.now = datetime(2030, 1, 15, 7, 30, tzinfo=CHICAGO)
        self.feed = Feed(self.feed_root, clock=lambda: self.now)
        self.enterContext(patch.object(secrets, "secret_values", return_value=[]))
        self.enterContext(patch.multiple(config, V3_DIR=self.root,
                                         V3_INDEX_DB=self.root / "index.sqlite"))
        self.enterContext(patch.object(daily_briefs.time, "time", self.timestamp))
        self.enterContext(patch.object(daily_briefs.claude_jobs, "resolve_cli", return_value=None))

    def timestamp(self):
        return self.now.timestamp()

    def test_quiet_hours_are_checked_after_calendar_and_model(self):
        for advance_during in ("calendar", "model"):
            with self.subTest(advance_during=advance_during):
                self.now = datetime(2030, 1, 15, 22, 59, tzinfo=CHICAGO)
                feed = Feed(self.root / f"feed-{advance_during}", clock=lambda: self.now)

                def calendar(start, end):
                    if advance_during == "calendar":
                        self.now += timedelta(minutes=2)
                    return [{"id": "example", "title": "Lab", "start": start}]

                def summarize(*args, **kwargs):
                    if advance_during == "model":
                        self.now += timedelta(minutes=2)
                    return JobResult("brief", "haiku", ok=True, outcome="ok", text="Lab tomorrow.")

                runner = Mock(side_effect=summarize)
                budget = Mock()
                budget.admit.return_value.ok = True
                daily_briefs.run("evening", feed=feed, budget=budget, calendar=calendar,
                                 state_dir=self.root / advance_during, run_job=runner)
                if advance_during == "calendar":
                    runner.assert_not_called()
                self.assertEqual([brief["created"] for brief in feed.snapshot()["briefs"]], [])

    def test_restart_retries_a_morning_failure_before_the_day_was_claimed(self):
        self.state.mkdir(mode=0o700)
        self.state.chmod(0o755)

        def scheduler():
            defaults = jobs.default_jobs(feed_root=self.feed_root, google_token=self.root / "absent-token.json")
            morning = next(job for job in defaults if job.name == "morning-brief")
            return jobs.Scheduler([morning], state=self.root / "jobs/state.json", clock=self.timestamp)

        first = scheduler().tick()
        self.assertEqual([row["outcome"] for row in first], ["error"])
        self.assertFalse((self.state / "state.json").exists())
        self.state.chmod(0o700)
        self.now += timedelta(minutes=2)
        scheduler().tick()
        self.assertEqual([row["kind"] for row in self.feed.batch()], ["digest"])


if __name__ == "__main__":
    unittest.main()
