"""K2 regressions exercised against an isolated copy of the fake CLI."""

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import config
from jarvis.v3 import claude_jobs


class K2Review(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.jobs = self.root / "v3" / "jobs"
        self.enterContext(patch.object(config, "V3_DIR", self.jobs.parent))
        self.cli = self.root / "claude"
        shutil.copyfile(Path(__file__).parent / "fakes" / "fake_claude.py", self.cli)
        self.cli.chmod(0o700)
        self.control()

    def control(self, **settings):
        (self.root / "control.json").write_text(json.dumps(settings), encoding="utf-8")

    def run_job(self, prompt="hello", timeout=5, **kwargs):
        return claude_jobs.run_job("review", prompt, model="haiku", cwd=self.root,
                                   timeout=timeout, cli=str(self.cli), **kwargs)

    def test_timeout_includes_version_probe(self):
        source = self.cli.read_text(encoding="utf-8")
        probe = '    print(control.get("version"'
        self.assertIn(probe, source)
        self.cli.write_text(source.replace(probe, "    time.sleep(2)\n" + probe, 1), encoding="utf-8")

        start = time.monotonic()
        result = self.run_job(timeout=0.2)
        self.assertEqual(result.outcome, "timeout")
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertFalse((self.root / "seen.json").exists())

    def test_keyboard_interrupt_during_term_grace_reaps_and_closes_pipes(self):
        self.control(sleep=60, ignore_term=True)
        popen = claude_jobs.subprocess.Popen
        jobs = []

        real_sleep, interrupts = time.sleep, [KeyboardInterrupt()]

        def interrupt_once(seconds):       # Popen.wait sleeps too: interrupt the grace only
            if interrupts:
                raise interrupts.pop()
            real_sleep(seconds)

        def start(argv, **kwargs):
            proc = popen(argv, **kwargs)
            if "-p" in argv:
                jobs.append(proc)
                self.addCleanup(proc.wait, timeout=2)
                self.addCleanup(proc.kill)
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    self.addCleanup(stream.close)
            return proc

        with (patch.object(claude_jobs.subprocess, "Popen", side_effect=start),
              patch.object(claude_jobs.time, "sleep", side_effect=interrupt_once)):
            with self.assertRaises(KeyboardInterrupt):
                self.run_job(timeout=0.5)

        proc, = jobs
        with self.subTest(resource="process"):
            with self.assertRaises(ChildProcessError):
                os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        with self.subTest(resource="pipes"):
            self.assertTrue(all(stream.closed for stream in (proc.stdin, proc.stdout, proc.stderr)))
        self.assertEqual(list(self.jobs.iterdir()), [])

    def test_failure_diagnostics_exclude_private_payloads(self):
        prompt = "private transcript fragment"
        config_value = "private configuration fragment"
        env_value = "private environment fragment"
        self.control(exit=1, stderr=f"Could not start server: {config_value}; {env_value}; {prompt}\n")
        result = self.run_job(prompt, mcp_servers={"test": {"command": "example.invalid", "args": [config_value]}},
                              env_extra={"JARVIS_FIXTURE": env_value})
        self.assertEqual(result.outcome, "error")
        for field, value in (("prompt", prompt), ("MCP config", config_value), ("environment", env_value)):
            with self.subTest(field=field):
                self.assertTrue(value not in result.error and value not in repr(result),
                                "private payload reached the returned diagnostics")

    def test_result_repr_excludes_cli_session_metadata(self):
        answer = "private answer fragment"
        self.control(result={"type": "result", "subtype": "success", "is_error": False,
                             "result": answer, "session_id": answer})
        result = self.run_job()
        self.assertEqual(result.text, answer)
        self.assertTrue(answer not in repr(result), "CLI-controlled session metadata reached repr")

    def test_preexisting_jobs_directory_is_private_or_refused(self):
        self.jobs.mkdir(parents=True)
        self.jobs.chmod(0o755)
        result = self.run_job()
        if result.ok:
            self.assertEqual(self.jobs.stat().st_mode & 0o777, 0o700)
        else:
            self.assertFalse((self.root / "seen.json").exists())

    def test_conflicting_result_members_are_rejected(self):
        self.control(raw='{"type":"result","subtype":"success","is_error":true,'
                         '"is_error":false,"result":"done","usage":{}}')
        self.assertEqual(self.run_job().outcome, "error")


if __name__ == "__main__":
    unittest.main()
