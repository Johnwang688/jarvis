"""Offline review regressions for K3 admission and usage."""

from datetime import datetime
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from jarvis.v3 import budget, claude_usage
from jarvis.v3.claude_jobs import JobResult

CHICAGO = ZoneInfo("America/Chicago")
NOON = datetime(2026, 11, 24, 12, tzinfo=CHICAGO).timestamp()
BEFORE_MIDNIGHT = datetime(2026, 11, 24, 23, 59, 59, tzinfo=CHICAGO).timestamp()


def low_usage():
    return [{"name": "5h", "used_percent": 4, "resets_at": None}]


def limited(path, usage=low_usage, now=lambda: NOON):
    return budget.Budget(path, usage=usage, now=now, daily_cap=1)


def complete():
    return JobResult("reviewer", "haiku", True, "ok")


class Review(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "v3" / "budget.jsonl"

    def child(self, code, *args):
        prelude = """
import os, socket, sys
from pathlib import Path
from unittest.mock import patch

def forbid_network(*args, **kwargs):
    raise SystemExit("unexpected network")

socket.socket.connect = forbid_network
socket.getaddrinfo = forbid_network
from k3_review_check import claude_usage, complete, limited

path = Path(sys.argv[1])
"""
        test_dir = Path(__file__).resolve().parent
        return subprocess.run(
            [sys.executable, "-c", prelude + code, str(self.path), *args],
            env={**os.environ, "HOME": str(self.root),
                 "PYTHONPATH": os.pathsep.join(map(str, (test_dir, test_dir.parents[1])))},
            capture_output=True, text=True, timeout=3,
        )

    def test_usage_request_crossing_midnight_spends_on_new_day(self):
        now = BEFORE_MIDNIGHT

        def usage():
            nonlocal now
            now += 2
            return low_usage()

        shared = limited(self.path, usage, lambda: now)
        held = shared.admit("reviewer")
        self.assertTrue(held.ok, held)
        shared.record(held, complete())
        self.assertEqual(shared.used()["day"], "2026-11-25")
        self.assertEqual(shared.used()["total"], 1)

    def fake_login(self):
        credentials = self.root / "login.json"
        credentials.write_text('{"claudeAiOauth":{"accessToken":"fake-token"}}')
        self.enterContext(patch.dict(os.environ, {"JARVIS_CLAUDE_CREDENTIALS": str(credentials)}))
        self.enterContext(patch("jarvis.v2.providers.claude.usage_agent_version", return_value="2.1.325"))
        self.enterContext(patch.multiple(claude_usage, _cached=None, _retry_at=0.0))

    def test_slow_usage_body_does_not_block_ledger_reader(self):
        self.fake_login()
        entered, finish = threading.Event(), threading.Event()

        class SlowResponse(BytesIO):
            status = 200

            def read(self, size=-1):
                entered.set()
                if not finish.wait(10):
                    raise TimeoutError("test never released the response body")
                return super().read(size)

        def opener(request, *, timeout):
            return SlowResponse(b'{"five_hour":{"utilization":4}}')

        def usage():
            return claude_usage.read_windows(opener=opener)

        shared = limited(self.path, usage)
        worker = threading.Thread(target=shared.admit, args=("reviewer",))
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            try:
                child = self.child('print(limited(path).used()["total"])')
            except subprocess.TimeoutExpired:
                raise self.failureException("ledger reader waited for the slow usage body") from None
            self.assertEqual((child.returncode, child.stdout.strip()), (0, "0"), child.stderr)
        finally:
            finish.set()
            worker.join(5)

    def test_reset_inside_cache_ttl_requires_new_usage_before_admit(self):
        self.fake_login()
        now, requests = NOON, []
        week_reset = datetime.fromtimestamp(NOON + 3600, CHICAGO).isoformat()

        def opener(request, *, timeout):
            requests.append(request.full_url)
            reset = datetime.fromtimestamp(now + 60, CHICAGO).isoformat()
            percent = 4 if len(requests) == 1 else 90
            body = {"five_hour": {"utilization": percent, "resets_at": reset},
                    "seven_day": {"utilization": 4, "resets_at": week_reset}}
            response = BytesIO(json.dumps(body).encode())
            response.status = 200
            return response

        def usage():
            return claude_usage.read_windows(opener=opener)

        with (patch.object(claude_usage.time, "time", lambda: now),
              patch.object(claude_usage.time, "monotonic", lambda: 1000 + now - NOON)):
            shared = limited(self.path, usage, lambda: now)
            first = shared.admit("reviewer")
            self.assertTrue(first.ok, first)
            shared.release(first)
            now += 61
            second = shared.admit("reviewer")
            self.assertEqual((len(requests), second.outcome, second.usage_pct),
                             (2, "skipped-usage", 90))

    def test_fifo_in_place_of_login_fails_closed_without_waiting(self):
        fifo = self.root / "login.json"
        os.mkfifo(fifo, mode=0o600)
        try:
            child = self.child("""
os.environ["JARVIS_CLAUDE_CREDENTIALS"] = sys.argv[2]
print(claude_usage.read_windows(opener=forbid_network))
""", str(fifo))
        except subprocess.TimeoutExpired:
            raise self.failureException("usage reader blocked opening a login FIFO with no writer") from None
        self.assertEqual((child.returncode, child.stdout.strip()), (0, "None"), child.stderr)


if __name__ == "__main__":
    unittest.main()
