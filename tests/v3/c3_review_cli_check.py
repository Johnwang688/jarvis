import os
import unittest
from unittest.mock import patch

from c3_acceptance_check import Base
from jarvis.v3 import jobs
from jarvis.v3.ingest import proc


class CLIEnvironmentReview(Base):
    def test_read_only_jobs_do_not_pass_unrelated_daemon_credentials(self):
        local_bin = self.tmp / ".local" / "bin"
        local_bin.mkdir(parents=True)
        for name in ("sw", "gh"):
            path = local_bin / name
            path.touch(mode=0o700)

        environment = {"HOME": str(self.tmp), "PATH": "/usr/bin:/bin",
                       "GH_TOKEN": "fake-github-token", "OPENROUTER_API_KEY": "fake-provider-key"}
        with patch.dict(os.environ, environment, clear=True), \
             patch.object(proc.subprocess, "Popen", side_effect=FileNotFoundError) as spawn:
            table = {job.name: job for job in jobs.default_jobs(index_path=self.tmp / "jobs" / "index.sqlite",
                                                                feed_root=self.tmp / "feed", signals_dir=self.sig)}
            for name in ("schoolwork", "github"):
                with self.assertRaisesRegex(RuntimeError, "ingestion failed"):
                    table[name].run()

        self.assertEqual([call.args[0][0] for call in spawn.call_args_list],
                         [str(local_bin / "sw"), str(local_bin / "gh")])
        for call in spawn.call_args_list:
            with self.subTest(executable=call.args[0][0]):
                self.assertFalse(call.kwargs.get("shell", False))
                self.assertNotIn("GH_TOKEN", call.kwargs["env"])
                self.assertNotIn("OPENROUTER_API_KEY", call.kwargs["env"])


if __name__ == "__main__":
    unittest.main()
