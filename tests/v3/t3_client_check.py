"""Exercise the T3 client with fake executables in a temporary HOME."""

import json
from datetime import datetime, timezone
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from jarvis.v3 import t3_client
from jarvis.v3.t3_client import T3, T3Error

THREAD = {
    "threadId": "t:1", "status": "completed", "title": "測試 🧪 cafe\u0301",
    "providerInstanceId": "codex", "model": "example", "pendingRequestCount": 0,
}
SHOW = {"thread": THREAD, "messages": [{"role": "assistant", "text": "例\n\x1b[31m"}]}


class Client(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home), "T3CODE_HOME": str(self.home)}))
        os.environ.pop("JARVIS_T3_BIN", None)
        self.binary = self.home / ".t3" / "wsl-runtime" / "fake" / "t3"
        self.binary.parent.mkdir(parents=True)

    def fake(self, source, path=None):
        path = self.binary if path is None else path
        path.write_text(f"#!{sys.executable} -I\nimport os, sys, time\n{source}\n", encoding="utf-8")
        path.chmod(0o700)

    def result(self, data, *, exit_code=0, path=None):
        self.fake(f"sys.stdout.buffer.write({json.dumps(data, ensure_ascii=False).encode('utf-8')!r})\n"
                  f"sys.exit({exit_code})", path=path)

    def error(self, kind, call, *args, **kwargs):
        with self.assertRaises(T3Error) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.kind, kind)
        self.assertEqual(len(str(caught.exception).splitlines()), 1)
        return str(caught.exception)

    def test_project_at_reads_only_exact_active_roots(self):
        path = self.home / "userdata" / "statev2.sqlite"
        path.parent.mkdir()
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE projection_projects (project_id, workspace_root, deleted_at, archived_at)")
            db.executemany("INSERT INTO projection_projects VALUES (?, ?, ?, ?)", [
                ("parent-id", "/home/user", None, None),
                ("active-id", "/home/user/active", None, None),
                ("archived-id", "/home/user/archived", None, "2030-01-01"),
                ("deleted-id", "/home/user/deleted", "2030-01-01", None),
            ])

        client = T3()
        with patch.object(t3_client.subprocess, "Popen") as popen:
            self.assertEqual(client.project_at("/home/user/active"), "active-id")
            for root in ("/home/user/active/child", "/home/user/archived", "/home/user/deleted"):
                self.assertIsNone(client.project_at(root))
            popen.assert_not_called()

    def test_stdout_cap_accepts_exactly_the_limit(self):
        self.fake("os.write(1, b'[]' + b' ' * 8190)")
        with patch.object(t3_client, "_STDOUT_CAP", 8192):
            self.assertEqual(T3().list("."), [])

    def test_stdout_over_cap(self):
        self.fake("while True: os.write(1, b'x' * 65536)")
        with patch.object(t3_client, "_STDOUT_CAP", 8192):
            message = self.error("shape", T3(timeout=5).list, ".")
        self.assertIn("output limit", message)

    def test_stderr_flood(self):
        self.fake("for _ in range(256): os.write(2, b'x' * 65536)\nos.write(1, b'[]')")
        self.assertEqual(T3(timeout=5).list("."), [])

    def test_binary_replaced_after_selection(self):
        self.result(SHOW)
        replacement = self.home / "replacement"
        self.result({"thread": {**THREAD, "title": "next version"}, "messages": []}, path=replacement)
        open_binary = os.open

        def replace_before_open(path, flags):
            replacement.replace(self.binary)
            return open_binary(path, flags)

        client = T3()
        with patch.object(t3_client.os, "open", side_effect=replace_before_open):
            self.error("missing", client.show, "t:1")
        self.assertEqual(client.show("t:1").title, "next version")

    def test_binary_replaced_after_open(self):
        self.result(SHOW)
        replacement = self.home / "replacement"
        self.result({"thread": {**THREAD, "title": "next version"}, "messages": []}, path=replacement)
        popen = subprocess.Popen

        def replace_before_exec(*args, **kwargs):
            replacement.replace(self.binary)
            return popen(*args, **kwargs)

        client = T3()
        with patch.object(t3_client.subprocess, "Popen", side_effect=replace_before_exec):
            self.assertEqual(client.show("t:1").title, THREAD["title"])
        self.assertEqual(client.show("t:1").title, "next version")

    def test_new_runtime_wins_while_old_runtime_is_still_installed(self):
        self.result(SHOW)
        os.utime(self.binary, (1_700_000_000, 1_700_000_000))
        client = T3()
        self.assertEqual(client.show("t:1").title, THREAD["title"])
        newer = self.binary.parent.parent / "newer" / "t3"
        newer.parent.mkdir()
        self.result({"thread": {**THREAD, "title": "next version"}, "messages": []}, path=newer)
        os.utime(newer, (1_800_000_000, 1_800_000_000))
        self.assertEqual(client.show("t:1").title, "next version")

    def test_non_utf8_output(self):
        self.fake("os.write(1, b'\"\\xff\"')")
        self.error("shape", T3().list, ".")

    def test_show_reads_optional_settlement_time(self):
        at = datetime(2030, 1, 1, tzinfo=timezone.utc)
        for stamp, expected in ((at.isoformat(), at.timestamp()), (None, None), ("2030-01-01", None)):
            with self.subTest(stamp=stamp):
                self.result({**SHOW, "thread": {**THREAD, "settledAt": stamp}})
                self.assertEqual(T3().show("t:1").settled_at, expected)

    def test_wait_clock_jumps(self):
        self.result({**SHOW, "thread": {**THREAD, "status": "running"}})
        samples = iter((10, 9, 8, 100))
        sleeps = []
        self.error("timeout", T3().wait, "t:1", timeout=5, clock=lambda: next(samples), sleep=sleeps.append)
        self.assertEqual(sleeps, [2.0])

    def test_wait_budget_applies_to_a_hung_show(self):
        self.fake("os.close(1)\ntime.sleep(30)")
        self.error("timeout", T3(timeout=30).wait, "t:1", timeout=0.25)

    def test_hung_cli_that_does_not_read_stdin(self):
        self.fake("time.sleep(30)")
        self.error("timeout", T3(timeout=0.25).send, "t:1", "x" * 256_000)

    def test_zero_and_negative_timeouts_never_launch(self):
        self.fake("raise SystemExit(1)")
        with patch.object(t3_client.subprocess, "Popen") as popen:
            for timeout in (0, -1):
                with self.subTest(timeout=timeout):
                    self.error("timeout", T3(timeout=timeout).list, ".")
                    self.error("timeout", T3().wait, "t:1", timeout=timeout)
            popen.assert_not_called()

    def test_large_thread_list_preserves_order_and_unicode(self):
        row = {k: v for k, v in THREAD.items() if k != "pendingRequestCount"}
        rows = [{**row, "threadId": f"t:{i}"} for i in range(5_000)]
        response = self.home / "list.json"
        response.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        self.fake("with open(os.path.join(os.environ['HOME'], 'list.json'), 'rb') as f:\n"
                  "    sys.stdout.buffer.write(f.read())")
        states = T3().list(".")
        self.assertEqual([state.thread_id for state in states], [row["threadId"] for row in rows])
        self.assertTrue(all(state.title == THREAD["title"] and state.pending_requests == 0
                            and state.messages == [] for state in states))

    def test_concurrent_calls_keep_their_own_input_and_output(self):
        self.fake("import json\n"
                  "thread = sys.argv[-2]\n"
                  "text = sys.stdin.buffer.read().decode('utf-8')\n"
                  "assert text == thread * 20_000\n"
                  "print(json.dumps(dict(threadId=thread, runId='r:' + thread, status='queued')))")
        ids = [f"t:{i}" for i in range(24)]
        client = T3(timeout=10)
        with ThreadPoolExecutor(max_workers=8) as pool:
            runs = list(pool.map(lambda thread: client.send(thread, thread * 20_000), ids))
        self.assertEqual([(run.thread_id, run.run_id) for run in runs],
                         [(thread, f"r:{thread}") for thread in ids])

    def test_show_preserves_text_and_exit_two_signals_waiting(self):
        self.result(SHOW, exit_code=2)
        state = T3().show("t:1")
        self.assertTrue(state.waiting_on_owner)
        self.assertEqual(state.title, THREAD["title"])
        self.assertEqual(state.messages, [("assistant", SHOW["messages"][0]["text"])])
        self.assertNotIn(THREAD["title"], repr(state))
        self.assertNotIn("例", repr(state))

    def test_errors_exclude_cli_output(self):
        self.fake("os.write(2, b'secret prompt\\nsecond line')\n"
                  "os.write(1, b'secret title')\nsys.exit(1)")
        message = self.error("failed", T3().show, "t:1")
        self.assertNotIn("secret", message)

    def test_argument_controls_and_size_are_refused_before_launch(self):
        client = T3()
        with patch.object(t3_client.subprocess, "Popen") as popen:
            for value in ("a\x1fb", "a\x85b", "a\u2028b", "é" * 150, "--help"):
                with self.subTest(value=value):
                    self.error("arguments", client.show, value)
                    self.error("arguments", client.start, ".", "hello", provider="codex", model=value)
            self.error("arguments", client.list, "/work/\rproject")
            self.error("arguments", client.start, ".", "hello", provider="codex", model="x", branch="-b")
            popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
