import os
from pathlib import Path
import runpy
import signal
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch


class WiringReview(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.data = root / "v3"
        self.enterContext(patch.dict(os.environ, {"HOME": str(root), "JARVIS_V3_DIR": str(self.data)}))

    def test_agentd_starts_with_planner_and_model_stack_unimportable(self):
        blocked = dict.fromkeys(("jarvis.v3.planner", "jarvis.v3.claude_jobs", "jarvis.agent",
                                "jarvis.llm", "claude_agent_sdk"))
        with patch.dict(sys.modules, blocked):
            from jarvis.v3 import daemon, jobs

            try:
                roster = jobs.default_jobs(feed_root=self.data / "feed")
            except ModuleNotFoundError as error:
                if error.name in blocked:
                    self.fail(f"Creating the job roster imported {error.name}")
                raise
            self.assertIn("planner", {job.name for job in roster})
            with (patch.object(daemon.config, "V3_DIR", self.data),
                  patch.object(daemon.config, "remote_host", return_value=None),
                  patch.object(daemon, "ThreadingHTTPServer") as listener,
                  patch.object(jobs.Scheduler, "start"),
                  patch("jarvis.v3.cli.time.sleep", side_effect=lambda _: signal.raise_signal(signal.SIGTERM)),
                  patch.object(sys, "argv", ["jarvis", "agentd"]),
                  self.assertRaises(SystemExit) as stopped):
                runpy.run_module("jarvis", run_name="__main__")
            self.assertEqual(stopped.exception.code, 0)
            listener.assert_called_once()

    def test_planner_exception_leaves_other_jobs_running(self):
        from jarvis.v3 import jobs, planner, t3_client

        roster = jobs.default_jobs(feed_root=self.data / "feed")
        planner_job = next(job for job in roster if job.name == "planner")
        following = Mock()
        scheduler = jobs.Scheduler((planner_job, jobs.Job("fixture", following, every_s=300)),
                                   state=self.data / "jobs" / "state.json")
        with (patch.object(planner, "run", side_effect=RuntimeError("fixture failure")),
              patch.object(t3_client, "T3"), patch.object(planner.config, "V3_DIR", self.data)):
            self.assertEqual({row["job"]: row["outcome"] for row in scheduler.tick()},
                             {"planner": "error", "fixture": "ok"})
        following.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
