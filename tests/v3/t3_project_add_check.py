"""Check the T3 project verb against a fake executable."""

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from jarvis.v3.t3_client import T3, T3Error


class ProjectAdd(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.binary = self.home / "fake-t3"
        self.capture = self.home / "call.json"
        self.env = {"HOME": str(self.home), "PATH": "/usr/bin", "LANG": "C.UTF-8",
                    "T3CODE_HOME": str(self.home / "t3-state")}
        self.enterContext(patch.dict(os.environ, {**self.env, "JARVIS_T3_BIN": str(self.binary),
                                                 "EXAMPLE_SECRET": "fake-token"}, clear=True))

    def fake(self, source):
        self.binary.write_text(f"#!{sys.executable} -I\nimport json, os, sys, time\n{source}\n", encoding="utf-8")
        self.binary.chmod(0o700)

    def test_project_add_argv_environment_and_no_json_requirement(self):
        self.fake(
            "with open(os.path.join(os.environ['HOME'], 'call.json'), 'w') as file:\n"
            "    json.dump([sys.argv[1:], dict(os.environ), sys.stdin.read()], file)\n"
            "print('fake human-readable output')"
        )
        for title, option in ((None, []), ("Example title", ["--title", "Example title"])):
            with self.subTest(title=title):
                self.assertIsNone(T3().project_add(Path("/home/user/demo project"), title=title))
                self.assertEqual(json.loads(self.capture.read_text()),
                                 [["project", "add", *option, "/home/user/demo project"], self.env, ""])

    def test_project_add_any_nonzero_exit_fails(self):
        for code in (1, 2, 17):
            with self.subTest(code=code):
                self.fake(f"print('fake-private-output')\nraise SystemExit({code})")
                with self.assertRaises(T3Error) as caught:
                    T3().project_add("/home/user/project")
                self.assertEqual(caught.exception.kind, "failed")
                self.assertEqual(str(caught.exception), str(T3Error("failed")))

    def test_project_add_uses_the_command_deadline(self):
        self.fake("os.close(1)\ntime.sleep(30)")
        began = time.monotonic()
        with self.assertRaises(T3Error) as caught:
            T3(timeout=0.25).project_add("/home/user/project")
        self.assertEqual(caught.exception.kind, "timeout")
        self.assertLess(time.monotonic() - began, 3)


if __name__ == "__main__":
    unittest.main()
