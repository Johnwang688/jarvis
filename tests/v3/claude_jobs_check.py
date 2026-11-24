"""Exercise K2 process and stream edges with instrumented copies of the fake CLI."""

import concurrent.futures
import json
import os
import shutil
import time
import unittest
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from jarvis.v3 import claude_jobs
from k2_acceptance_check import Base, PLANTED, alive


class ClaudeJobs(Base):
    def inject(self, before, code):
        source = self.cli.read_text()
        self.assertIn(before, source)
        self.cli.write_text(source.replace(before, code + "\n" + before, 1))

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("fake CLI did not reach the expected state")

    def test_exit_observed_at_deadline_wins_over_timeout_and_cancel(self):
        cancel = Event()
        exited = False
        popen = claude_jobs.subprocess.Popen
        read = claude_jobs._Output.read
        job_pid = None

        def capture_pid(argv, **kwargs):
            nonlocal job_pid
            proc = popen(argv, **kwargs)
            if "-p" in argv:
                job_pid = proc.pid
            return proc

        def observe_exit(output, fd, stream):
            nonlocal exited
            more = read(output, fd, stream)
            if stream == "stdout" and job_pid is not None and output.stdout:
                os.waitid(os.P_PID, job_pid, os.WEXITED | os.WNOWAIT)
                exited = True
                cancel.set()
            return more

        clock = SimpleNamespace(
            monotonic=lambda: time.monotonic() + (1000 if exited else 0),
            sleep=time.sleep,
        )
        with (patch.object(claude_jobs, "time", clock),
              patch.object(claude_jobs.subprocess, "Popen", capture_pid),
              patch.object(claude_jobs._Output, "read", observe_exit)):
            result = self.run_job(cancel=cancel)
        self.assertTrue(exited)
        self.assertEqual(result.outcome, "ok", result)

    def test_closing_output_does_not_end_the_job(self):
        self.control(sleep=600)
        self.inject("time.sleep(control.get", "os.close(1)\nos.close(2)")
        result = self.run_job(timeout=0.8)
        self.assertEqual(result.outcome, "timeout", result)
        self.assertFalse(alive(self.seen()["pid"]))

    def test_partial_utf8_in_stdout_and_stderr(self):
        self.inject('if "raw" in control:',
                    'os.write(1, b\'{"result":"\\xc3\')\nos.write(2, b"detail: \\xc3")\nsys.exit(0)')
        result = self.run_job()
        self.assertEqual(result.outcome, "error")
        self.assertIn("detail: \ufffd", result.error)
        self.assertEqual(result.text, "")

    def test_stderr_is_scrubbed_even_when_stdout_is_valid_json(self):
        self.control(exit=1, stderr=f"fake detail {PLANTED}\r\nmore\x1b")
        result = self.run_job()
        self.assertIn("fake detail", result.error)
        for unsafe in (PLANTED, "\r", "\n", "\x1b"):
            self.assertNotIn(unsafe, result.error)

    def test_probe_uses_the_job_environment(self):
        self.inject(
            '    print(control.get("version"',
            '    with open(os.path.join(here, "probe-env.json"), "w") as probe:\n'
            '        json.dump(dict(os.environ), probe)',
        )
        with patch.dict(os.environ, {"JARVIS_UNRELATED": "fake-daemon-value"}):
            result = self.run_job(review=True, env_extra={"JARVIS_EXTRA": "fake-job-value"})
        self.assertTrue(result.ok, result)
        probe_env = json.loads((self.bin / "probe-env.json").read_text())
        self.assertEqual(probe_env, self.seen()["env"])
        self.assertEqual(probe_env["JARVIS_EXTRA"], "fake-job-value")
        self.assertNotIn("JARVIS_UNRELATED", probe_env)

    def test_probe_has_its_own_timeout_and_cleans_up_its_group(self):
        self.inject(
            '    print(control.get("version"',
            '    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])\n'
            '    with open(os.path.join(here, "probe-pid"), "w") as probe:\n'
            '        probe.write(str(child.pid))\n'
            '    time.sleep(600)',
        )
        with patch.object(claude_jobs, "_VERSION_TIMEOUT", 0.8):
            result = self.run_job(timeout=30)
        self.assertEqual(result.outcome, "error", result)
        self.assertLess(result.seconds, 5)
        child = int((self.bin / "probe-pid").read_text())
        self.wait_for(lambda: not alive(child))
        self.assertFalse((self.bin / "seen.json").exists())
        self.assertEqual(self.leftovers(), [])

    def test_concurrent_jobs_keep_separate_private_files(self):
        self.inject(
            "time.sleep(control.get",
            'while not os.path.exists(os.path.join(os.getcwd(), "release")):\n'
            '    time.sleep(0.01)',
        )
        other_bin = self.tmp / "bin2"
        shutil.copytree(self.bin, other_bin)
        other_cli = other_bin / "claude"
        other_seen = other_bin / "seen.json"
        release = self.cwd / "release"
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.run_job, mcp_servers={"one": {"command": "fake-one"}})
            second = executor.submit(self.run_job, cli=str(other_cli),
                                     mcp_servers={"two": {"command": "fake-two"}})
            try:
                self.wait_for(lambda: (self.bin / "seen.json").exists() and other_seen.exists())
                one = Path(self.seen()["mcp"]["path"])
                two = Path(json.loads(other_seen.read_text())["mcp"]["path"])
                self.assertNotEqual(one.parent, two.parent)
                self.assertEqual(json.loads(one.read_text()), {"mcpServers": {"one": {"command": "fake-one"}}})
                self.assertEqual(json.loads(two.read_text()), {"mcpServers": {"two": {"command": "fake-two"}}})
            finally:
                release.touch()
            self.assertTrue(first.result().ok)
            self.assertTrue(second.result().ok)
        self.assertEqual(self.leftovers(), [])


if __name__ == "__main__":
    unittest.main()
