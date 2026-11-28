"""File safety and grant revocation while the computer action log is in use."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jarvis.v3.computer import ComputerActionLog, ComputerDrive, ComputerError
from tests.v3.computer_act_check import A, FakeDesktop, row


class ComputerLogCheck(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.path = self.root / "computer_actions.jsonl"
        self.log = ComputerActionLog(self.root)

    def test_creates_a_private_file_and_appends_complete_json_lines(self):
        records = [{"id": "fake-action", "state": "start", "title": "Example\nwindow"},
                   {"id": "fake-action", "state": "done", "detail": ""}]
        with patch("jarvis.v3.computer.os.write", wraps=os.write) as write:
            for record in records:
                self.log(record)
            self.assertEqual(write.call_count, 2)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        lines = self.path.read_bytes().splitlines(keepends=True)
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(line.endswith(b"\n") for line in lines))
        self.assertEqual([json.loads(line) for line in lines], records)

    def test_refuses_a_symlink_without_touching_target(self):
        target = self.root / "target"
        target.touch(mode=0o600)
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            self.log({"state": "start"})
        self.assertEqual(target.read_bytes(), b"")

    def test_refuses_a_group_readable_file(self):
        self.path.touch(mode=0o600)
        self.path.chmod(0o640)
        with self.assertRaises(OSError):
            self.log({"state": "start"})
        self.assertEqual(self.path.read_bytes(), b"")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)

    def test_refuses_a_directory(self):
        self.path.mkdir()
        with self.assertRaises(OSError):
            self.log({"state": "start"})

    def test_refuses_a_file_with_a_different_owner(self):
        info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=os.getuid() + 1, st_nlink=1)
        with patch("jarvis.v3.computer.os.fstat", return_value=info), self.assertRaises(OSError):
            self.log({"state": "start"})
        self.assertEqual(self.path.read_bytes(), b"")

    def test_stop_can_revoke_a_grant_while_a_start_record_is_written(self):
        writing, release = Event(), Event()

        def blocked_log(record):
            if record["state"] == "start":
                writing.set()
                self.assertTrue(release.wait(timeout=5))
            self.log(record)

        desktop = FakeDesktop([row(A, front=True)])
        drive = ComputerDrive(desktop, enabled=lambda: True, log=blocked_log)
        code = drive.start()
        drive.screenshot(code)
        with ThreadPoolExecutor(max_workers=2) as pool:
            action = pool.submit(drive.click, code, 5, 5)
            try:
                self.assertTrue(writing.wait(timeout=5))
                self.assertTrue(pool.submit(drive.stop).result(timeout=2))
            finally:
                release.set()
            with self.assertRaisesRegex(ComputerError, "grant ended"):
                action.result(timeout=5)
        self.assertNotIn("window_click", [op for op, _ in desktop.calls])
        self.assertEqual([json.loads(line)["state"] for line in self.path.read_bytes().splitlines()],
                         ["start", "failed"])

    def test_already_sent_click_records_success_after_stop(self):
        desktop = FakeDesktop([row(A, front=True)])
        drive = ComputerDrive(desktop, enabled=lambda: True, log=self.log)
        code = drive.start()
        drive.screenshot(code)
        desktop.during = lambda op: drive.stop() if op == "window_click" else None
        self.assertEqual(drive.click(code, 5, 5)["control"], {"name": "Send", "role": "button"})
        self.assertEqual([json.loads(line)["state"] for line in self.path.read_bytes().splitlines()],
                         ["start", "done"])


if __name__ == "__main__":
    unittest.main()
