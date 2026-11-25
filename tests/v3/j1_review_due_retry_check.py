import tempfile
import unittest
from pathlib import Path

from jarvis.v3.jobs import Job, Scheduler
from jarvis.v3.skill_curator import IDLE_S, WEEK_S, due


class DueRetryCheck(unittest.TestCase):
    def test_weekly_job_retries_after_restart_and_resumes_weekly_schedule(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        now = 1_800_000_000
        attempts = []

        def run():
            attempts.append(now)
            if len(attempts) == 1:
                raise ImportError("fixture dependency unavailable")

        job = Job("weekly", run, due=lambda now, last: due(now, last_run=last, idle_s=IDLE_S))
        options = dict(state=root / "jobs/state.json", clock=lambda: now, idle=lambda: IDLE_S)
        first = Scheduler([job], **options)
        self.assertEqual([row["outcome"] for row in first.tick()], ["error"])

        resumed = Scheduler([job], **options)
        now += 59
        self.assertEqual(resumed.tick(), [])
        now += 1
        self.assertEqual(resumed.tick(), [])
        self.assertEqual(resumed.status()["weekly"]["failures"], 1)
        now += WEEK_S - 61
        self.assertEqual(resumed.tick(), [])
        now += 1
        self.assertEqual([row["outcome"] for row in resumed.tick()], ["ok"])
        self.assertEqual(resumed.status()["weekly"]["failures"], 0)


if __name__ == "__main__":
    unittest.main()
