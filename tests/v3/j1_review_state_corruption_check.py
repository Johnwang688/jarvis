import tempfile
import unittest
from pathlib import Path

from jarvis.v3.jobs import Job, Scheduler


class StateCorruptionCheck(unittest.TestCase):
    def test_corruption_does_not_reset_live_schedule_or_backoff(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        state = root / "jobs/state.json"
        now = 1_800_000_000
        attempts = []

        def fail():
            attempts.append("retry")
            raise OSError("fixture temporarily unavailable")

        scheduler = Scheduler([
            Job("weekly", lambda: attempts.append("weekly"), every_s=7 * 86400),
            Job("retry", fail, every_s=30),
        ], state=state, clock=lambda: now)
        self.assertEqual([row["outcome"] for row in scheduler.tick()], ["ok", "error"])
        self.assertEqual(attempts, ["weekly", "retry"])

        state.write_text('{"weekly":')
        now += 30
        self.assertEqual(scheduler.tick(), [])
        self.assertEqual(attempts, ["weekly", "retry"])
        now += 30
        self.assertEqual([row["job"] for row in scheduler.tick()], ["retry"])
        self.assertEqual(scheduler.status()["retry"]["failures"], 2)


if __name__ == "__main__":
    unittest.main()
