"""Offline K3 checks for the usage fetch and the cross-process ledger lock."""

from datetime import datetime
import fcntl
from io import BytesIO
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jarvis.v3 import budget, claude_usage
from jarvis.v3.claude_jobs import JobResult

TOKEN = "fake-token"
RESET = "2026-11-24T18:00:00Z"
BODY = {
    "five_hour": {"utilization": 14.5, "resets_at": RESET},
    "seven_day": {"utilization": 40, "resets_at": RESET},
    "seven_day_opus": {"utilization": 100, "resets_at": RESET},
}
WINDOWS = [
    {"name": "5h", "used_percent": 14.5, "resets_at": RESET},
    {"name": "week", "used_percent": 40.0, "resets_at": RESET},
]


class Response(BytesIO):
    def __init__(self, status, body):
        super().__init__(body)
        self.status = status
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class Usage(unittest.TestCase):
    def setUp(self):
        self.credentials = Path(self.enterContext(tempfile.TemporaryDirectory())) / "credentials.json"
        self.login(TOKEN)
        self.enterContext(patch.dict(os.environ, {"JARVIS_CLAUDE_CREDENTIALS": str(self.credentials)}))
        self.enterContext(patch("jarvis.v2.providers.claude.usage_agent_version", return_value="2.1.325"))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("network")))
        self.enterContext(patch("socket.getaddrinfo", side_effect=AssertionError("DNS")))
        self.enterContext(patch.multiple(claude_usage, _cached=None, _retry_at=0.0))
        self.now = 1000
        self.enterContext(patch.object(claude_usage.time, "monotonic", lambda: self.now))
        self.enterContext(patch.object(claude_usage.time, "time", return_value=datetime.fromisoformat(RESET).timestamp() - 3600))
        self.requests = []
        self.status = 200
        self.body = json.dumps(BODY).encode()
        self.failure = None
        self.response = None

    def login(self, token):
        self.credentials.write_text(json.dumps({"claudeAiOauth": {"accessToken": token}}))

    def opener(self, request, *, timeout):
        self.requests.append((request, timeout))
        if self.failure is not None:
            raise self.failure
        self.response = Response(self.status, self.body)
        return self.response

    def read(self):
        return claude_usage.read_windows(opener=self.opener)

    def test_wire_contract_and_success(self):
        self.assertEqual(self.read(), WINDOWS)
        ((request, timeout),) = self.requests
        self.assertEqual(request.full_url, "https://api.anthropic.com/api/oauth/usage")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(timeout, 8)
        self.assertEqual({key.lower(): value for key, value in request.header_items()}, {
            "authorization": "Bearer " + TOKEN,
            "anthropic-beta": "oauth-2025-04-20",
            "accept": "application/json",
            "user-agent": "claude-code/2.1.325",
        })
        self.assertTrue(self.response.closed)

    def test_401_429_and_redirect_skip_body(self):
        for status in (401, 429, 302):
            with self.subTest(status=status):
                self.status = status
                self.assertIsNone(self.read())
                self.assertEqual(self.response.read_sizes, [])
                self.assertTrue(self.response.closed)
                self.now += 30

    def test_timeout_and_echoed_token_stay_private(self):
        with self.assertNoLogs(level="DEBUG"):
            self.failure = TimeoutError("request header contained " + TOKEN)
            self.assertIsNone(self.read())
            self.failure = None
            self.now += 30
            self.body = json.dumps({
                "five_hour": {"utilization": 5, "resets_at": TOKEN},
                "ignored": TOKEN,
            }).encode()
            reading = self.read()
            self.assertEqual(reading, [{"name": "5h", "used_percent": 5.0, "resets_at": ""}])
            self.assertNotIn(TOKEN, repr(reading))
            self.assertNotIn(TOKEN, repr(claude_usage._cached))

    def test_huge_body_is_rejected_at_the_read_limit(self):
        self.body += b" " * (1024 * 1024)
        self.assertIsNone(self.read())
        self.assertEqual(self.response.read_sizes, [64 * 1024 + 1])
        self.assertTrue(self.response.closed)

    def test_wrong_payloads(self):
        for body in (
            b"not json",
            b"[]",
            b"{}",
            b'{"five_hour": []}',
            b'{"five_hour": {"utilization": true}}',
            b'{"five_hour": {"utilization": -1}}',
            b'{"five_hour": {"utilization": NaN}}',
            b'{"five_hour": {"utilization": 1e500}}',
            b'{"five_hour": {"utilization": "45"}}',
            b'{"five_hour": {"utilization": 10}, "seven_day": {"utilization": null}}',
        ):
            with self.subTest(body=body):
                self.body = body
                self.assertIsNone(self.read())
                self.now += 30

    def test_wrong_credential_shapes_make_no_request(self):
        for body in ('[1]', '{"claudeAiOauth":[]}', '{"claudeAiOauth":{"accessToken":5}}', 'x'):
            with self.subTest(body=body):
                self.credentials.write_text(body)
                self.assertIsNone(self.read())
                self.now += 30
        self.assertEqual(self.requests, [])

    def test_cache_then_back_off_and_retry_with_current_login(self):
        reading = self.read()
        reading[0]["used_percent"] = 100
        self.login("fake-rotated-token")
        self.now += 299
        self.assertEqual(self.read(), WINDOWS)
        self.assertEqual(len(self.requests), 1)
        self.now += 1
        self.status = 429
        self.assertIsNone(self.read())
        self.assertEqual(len(self.requests), 2)
        self.now += 29
        self.assertIsNone(self.read())
        self.assertEqual(len(self.requests), 2)
        self.now += 1
        self.status = 200
        self.assertEqual(self.read(), WINDOWS)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.requests[-1][0].get_header("Authorization"), "Bearer fake-rotated-token")

    def test_default_credential_path_is_under_temporary_home(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HOME": home, "JARVIS_CLAUDE_CREDENTIALS": ""}):
            self.credentials = Path(home) / ".claude" / ".credentials.json"
            self.credentials.parent.mkdir()
            self.login(TOKEN)
            self.assertEqual(self.read(), WINDOWS)


def _budget(path):
    reset = datetime.fromisoformat(RESET).timestamp()
    return budget.Budget(path, daily_cap=1, now=lambda: reset - 60, usage=lambda: WINDOWS)


def _writer(path, writing, finish):
    b = _budget(path)
    admission = b.admit("reviewer")
    if not admission.ok:
        raise AssertionError(admission)
    write = os.write

    def pause_before_write(fd, line):
        writing.set()
        if not finish.wait(10):
            raise TimeoutError("writer was never released")
        return write(fd, line)

    with patch.object(budget.os, "write", pause_before_write):
        b.record(admission, JobResult("reviewer", "haiku", True, "ok"))


def _contender(path, racing, result):
    b = _budget(path)
    racing.set()
    admission = b.admit("planner")
    result.put((admission.outcome, b.used()["total"]))


class Ledger(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.tmp / "v3" / "budget.jsonl"

    def test_two_processes_race_during_record(self):
        ctx = multiprocessing.get_context("fork")
        writing, finish, racing = ctx.Event(), ctx.Event(), ctx.Event()
        result = ctx.Queue()
        self.addCleanup(result.close)
        writer = ctx.Process(target=_writer, args=(self.path, writing, finish))
        contender = ctx.Process(target=_contender, args=(self.path, racing, result))
        processes = []
        try:
            writer.start()
            processes.append(writer)
            self.assertTrue(writing.wait(5), "writer did not reach the append")
            contender.start()
            processes.append(contender)
            self.assertTrue(racing.wait(5), "contender did not reach admission")
            # Check the real kernel lock while the first process is paused.
            # In the lock-free mutant this succeeds, regardless of scheduling.
            with self.path.with_suffix(".jsonl.lock").open("rb") as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finish.set()
            self.assertEqual(result.get(timeout=5), ("skipped-budget", 1))
            for process in processes:
                process.join(5)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(_budget(self.path).used()["jobs"], {"reviewer": 1})
        finally:
            finish.set()
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join(5)

    def test_short_write_retains_ticket_and_can_be_retried_once(self):
        b = _budget(self.path)
        admission = b.admit("reviewer")
        result = JobResult("reviewer", "haiku", True, "ok")
        write = os.write
        with patch.object(budget.os, "write", side_effect=lambda fd, line: write(fd, line[:-1])) as partial:
            with self.assertRaises(OSError):
                b.record(admission, result)
            partial.assert_called_once()
        self.assertEqual(b.used()["total"], 0)
        self.assertEqual(b.admit("planner").outcome, "skipped-budget")
        b.record(admission, result)
        self.assertEqual(b.used()["jobs"], {"reviewer": 1})
        self.assertEqual(len(self.path.read_text().splitlines()), 1)

    def test_links_are_refused_without_touching_their_targets(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        self.path.parent.symlink_to(elsewhere, target_is_directory=True)
        self.assertFalse(_budget(self.path).admit("reviewer").ok)
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.path.parent.unlink()
        self.path.parent.mkdir()
        target = elsewhere / "target"
        target.write_text("untouched")
        lock = self.path.with_suffix(".jsonl.lock")
        lock.symlink_to(target)
        self.assertFalse(_budget(self.path).admit("reviewer").ok)
        lock.unlink()
        self.path.hardlink_to(target)
        self.assertFalse(_budget(self.path).admit("reviewer").ok)
        self.assertEqual(target.read_text(), "untouched")


if __name__ == "__main__":
    unittest.main()
