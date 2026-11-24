"""Orchestrator-owned acceptance tests for K3 (implementers: do not edit; if one is wrong, stop and say why).

Contract: jarvis/v3/budget.py
  CAPS = {"reviewer": 40, "consolidate": 2, "planner": 24, "style": 2, "brief": 4}; DAILY_CAP = 60; USAGE_LIMIT = 75.0
  Budget(path=None, *, usage, now=time.time, caps=None, daily_cap=None)      path defaults to config.V3_DIR / "budget.jsonl"
      usage() -> list[dict] | None      windows {"name", "used_percent", "resets_at"}; None, [] or an exception = unknown
  admit(job) -> Admission(ok: bool, reason: str, outcome: "ok"|"skipped-budget"|"skipped-usage", usage_pct: float|None, ticket: str|None)
      checked in this order: per-job cap, daily cap, usage. An admitted job HOLDS a slot (ticket) until record/release, so
      concurrent admits cannot pass a cap together. Unknown usage refuses (skipped-usage). usage() is not called when a cap already refused.
  record(admission, result) -> None     appends contract 1.6 and turns the held slot into a spent one
  release(admission) -> None            a job that never started: frees the slot, writes nothing
  skip(job, admission) -> None          appends a line with the skipped outcome and zero tokens; spends nothing
  used() -> {"day": "YYYY-MM-DD", "total": n, "jobs": {...}}
Every started job counts, failed or not. A day is local midnight in America/Chicago. Unknown job name -> ValueError.
jarvis/v3/claude_usage.py: read_windows() -> list[dict] | None, the default reading (copy-on-write from v2's hud_api; the login is read for
that one request, never logged, returned or stored; a failure is None). Importing either module makes no request.
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from jarvis import config
from jarvis.v3 import budget, claude_usage, imports
from jarvis.v3.budget import Budget
from jarvis.v3.claude_jobs import JobResult

REPO = Path(__file__).resolve().parents[2]
CT = ZoneInfo("America/Chicago")
NOON = datetime(2026, 11, 24, 12, tzinfo=CT).timestamp()
KEYS = {"ts", "job", "model", "provider", "input_tokens", "output_tokens", "usage_pct_before", "outcome"}


def windows(*pct, resets=None):
    return [{"name": f"w{i}", "used_percent": p, "resets_at": resets} for i, p in enumerate(pct)]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.tmp / "v3" / "budget.jsonl"
        self.now = NOON
        self.reading = windows(10.0)
        self.calls = 0

    def usage(self):
        self.calls += 1
        if isinstance(self.reading, Exception):
            raise self.reading
        return self.reading

    def budget(self, **kw):
        return Budget(self.path, usage=self.usage, now=lambda: self.now, **kw)

    def lines(self):
        return [json.loads(l) for l in self.path.read_text().splitlines()] if self.path.exists() else []

    def done(self, job="reviewer", ok=True):
        return JobResult(job, "haiku", ok, "ok" if ok else "error", input_tokens=11, output_tokens=3)

    def spend(self, b, job="reviewer", n=1):
        for _ in range(n):
            a = b.admit(job)
            self.assertTrue(a.ok, a)
            b.record(a, self.done(job))


class Caps(Base):
    def test_defaults(self):
        self.assertEqual(budget.CAPS, {"reviewer": 40, "consolidate": 2, "planner": 24, "style": 2, "brief": 4})
        self.assertEqual((budget.DAILY_CAP, budget.USAGE_LIMIT), (60, 75.0))

    def test_per_job_cap(self):
        b = self.budget()
        self.spend(b, "style", 2)
        before = self.calls
        a = b.admit("style")
        self.assertEqual((a.ok, a.outcome, a.ticket), (False, "skipped-budget", None))
        self.assertIn("style", a.reason)
        self.assertEqual(self.calls, before)        # a cap refuses without asking for usage
        self.assertTrue(b.admit("brief").ok)

    def test_daily_cap(self):
        b = self.budget(daily_cap=3)
        self.spend(b, "reviewer", 3)
        a = b.admit("planner")
        self.assertEqual((a.ok, a.outcome), (False, "skipped-budget"))

    def test_failed_jobs_count(self):
        b = self.budget()
        for _ in range(2):
            b.record(b.admit("style"), self.done("style", ok=False))
        self.assertFalse(b.admit("style").ok)

    def test_held_slots_count_and_release_frees_them(self):
        b = self.budget()
        one, two = b.admit("style"), b.admit("style")
        self.assertTrue(one.ok and two.ok)
        self.assertNotEqual(one.ticket, two.ticket)
        self.assertFalse(b.admit("style").ok)
        b.release(one)
        b.release(one)                              # twice is once
        self.assertTrue(b.admit("style").ok)
        self.assertFalse(b.admit("style").ok)
        self.assertEqual(self.lines(), [])

    def test_a_ticket_is_spent_once(self):
        b = self.budget()
        a = b.admit("reviewer")
        b.record(a, self.done())
        with self.assertRaises(ValueError):
            b.record(a, self.done())
        b.release(a)
        self.assertEqual(b.used()["total"], 1)
        with self.assertRaises(ValueError):
            b.record(b.admit("nope") if False else budget.Admission(True, "", "ok", 1.0, "forged"), self.done())

    def test_concurrent_admits_never_pass_a_cap(self):
        b = self.budget()
        got, barrier = [], threading.Barrier(16)

        def go():
            barrier.wait()
            got.append(b.admit("brief").ok)
        threads = [threading.Thread(target=go) for _ in range(16)]
        [t.start() for t in threads]
        [t.join(10) for t in threads]
        self.assertEqual(sum(got), 4)

    def test_two_processes_share_the_day(self):
        self.spend(self.budget(), "style", 2)
        self.assertFalse(self.budget().admit("style").ok)      # a restarted daemon reads the ledger
        a, b = self.budget(), self.budget()
        self.spend(a, "consolidate", 1)
        self.spend(b, "consolidate", 1)
        self.assertFalse(a.admit("consolidate").ok)

    def test_unknown_job(self):
        for job in ("other", "", None, "Reviewer"):
            with self.assertRaises(ValueError):
                self.budget().admit(job)


class Usage(Base):
    def test_threshold(self):
        for reading, ok in ((windows(74.9), True), (windows(75.0), False), (windows(10, 90), False), (windows(100.0), False), (windows(0), True)):
            self.reading = reading
            a = self.budget().admit("reviewer")
            self.assertEqual(a.ok, ok, reading)
            self.assertEqual(a.outcome, "ok" if ok else "skipped-usage")
            self.assertEqual(a.usage_pct, max(w["used_percent"] for w in reading))

    def test_unknown_refuses(self):
        for reading in (None, [], RuntimeError("x"), "no", [{}], windows("80"), windows(None), windows(float("nan")), windows(-5), windows(True), [None],
                        windows(10, None)):
            self.reading = reading
            a = self.budget().admit("reviewer")
            self.assertEqual((a.ok, a.outcome), (False, "skipped-usage"), reading)

    def test_a_window_past_its_reset_is_stale_not_full(self):
        past = datetime.fromtimestamp(NOON - 60, CT).isoformat()
        future = datetime.fromtimestamp(NOON + 60, CT).isoformat()
        self.reading = windows(99, resets=past) + windows(5, resets=future)
        self.assertTrue(self.budget().admit("reviewer").ok)
        self.reading = windows(99, resets=future)
        self.assertFalse(self.budget().admit("reviewer").ok)
        self.reading = windows(99, resets=past)                 # nothing current is known
        self.assertFalse(self.budget().admit("reviewer").ok)

    def test_a_refusal_holds_no_slot(self):
        b = self.budget()
        self.reading = None
        for _ in range(5):
            self.assertFalse(b.admit("style").ok)
        self.reading = windows(1)
        self.assertTrue(b.admit("style").ok and b.admit("style").ok)


class Days(Base):
    def test_rollover_is_chicago_midnight(self):
        self.now = datetime(2026, 11, 24, 23, 59, 30, tzinfo=CT).timestamp()
        b = self.budget()
        self.spend(b, "style", 2)
        self.assertEqual(b.used()["day"], "2026-11-24")
        self.now += 60
        self.assertTrue(b.admit("style").ok)
        self.assertEqual((b.used()["day"], b.used()["total"]), ("2026-11-25", 0))

    def test_a_job_across_midnight_counts_on_the_day_it_was_admitted_or_recorded_once(self):
        self.now = datetime(2026, 11, 24, 23, 59, 59, tzinfo=CT).timestamp()
        b = self.budget()
        a = b.admit("style")
        self.now += 10
        b.record(a, self.done("style"))
        self.assertEqual(len(self.lines()), 1)
        self.assertLessEqual(b.used()["total"], 1)

    def test_dst_days(self):
        for start, hours in ((datetime(2026, 11, 1, 0, 30, tzinfo=CT), 25), (datetime(2027, 3, 14, 0, 30, tzinfo=CT), 23)):
            b = Budget(self.tmp / f"{hours}.jsonl", usage=self.usage, now=lambda: self.now)
            self.now = start.timestamp()
            self.spend(b, "style", 2)
            self.now = start.timestamp() + (hours - 1) * 3600
            self.assertFalse(b.admit("style").ok, hours)
            self.now = start.timestamp() + hours * 3600
            self.assertTrue(b.admit("style").ok, hours)

    def test_a_clock_that_goes_back_does_not_refund(self):
        b = self.budget()
        self.spend(b, "style", 2)
        self.now -= 3600
        self.assertFalse(b.admit("style").ok)


class Ledger(Base):
    def test_line(self):
        b = self.budget()
        a = b.admit("reviewer")
        b.record(a, self.done())
        (line,) = self.lines()
        self.assertEqual(set(line), KEYS)
        self.assertEqual((line["job"], line["model"], line["provider"], line["input_tokens"], line["output_tokens"], line["usage_pct_before"], line["outcome"]),
                         ("reviewer", "haiku", "claude-sub", 11, 3, 10.0, "ok"))
        self.assertEqual(datetime.fromisoformat(line["ts"]).timestamp(), NOON)

    def test_outcomes(self):
        b = self.budget()
        for outcome in ("error", "timeout", "cancelled"):
            b.record(b.admit("reviewer"), JobResult("reviewer", "haiku", False, outcome))
        self.assertEqual([l["outcome"] for l in self.lines()], ["error"] * 3)
        self.reading = None
        b.skip("planner", b.admit("planner"))
        line = self.lines()[-1]
        self.assertEqual((line["outcome"], line["job"], line["input_tokens"], line["usage_pct_before"]), ("skipped-usage", "planner", 0, None))
        self.assertEqual(b.used()["total"], 3)

    def test_private_and_append_only(self):
        b = self.budget()
        self.spend(b)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        first, inode = self.path.read_bytes(), self.path.stat().st_ino
        self.spend(b, n=3)
        self.assertTrue(self.path.read_bytes().startswith(first))
        self.assertEqual(self.path.stat().st_ino, inode)

    def test_bad_lines_are_kept_and_never_refund(self):
        b = self.budget()
        self.spend(b, "style", 2)
        with self.path.open("a") as f:
            f.write("garbage\n[]\n{\"ts\": 5}\n{\"job\": \"style\"}\n\xff\n")
        self.assertFalse(self.budget().admit("style").ok)
        with self.path.open("ab") as f:
            f.write(b'{"ts": "2026-11-2')              # a torn last line
        b2 = self.budget()
        self.spend(b2, "brief")
        self.assertEqual(self.budget().used()["jobs"].get("brief"), 1)

    def test_unreadable_or_unwritable_refuses(self):
        self.path.parent.mkdir(parents=True)
        self.path.mkdir()
        self.assertFalse(self.budget().admit("reviewer").ok)
        self.path.rmdir()
        target = self.tmp / "elsewhere"
        target.write_text("")
        self.path.symlink_to(target)
        self.assertFalse(self.budget().admit("reviewer").ok)
        self.assertEqual(target.read_text(), "")

    def test_default_path(self):
        with patch.object(config, "V3_DIR", self.tmp / "d"):
            b = Budget(usage=self.usage, now=lambda: self.now)
            b.record(b.admit("brief"), self.done("brief"))
        self.assertTrue((self.tmp / "d" / "budget.jsonl").is_file())


class Reading(unittest.TestCase):
    def test_imports_make_no_request_and_stay_light(self):
        code = ("import sys, socket\n"
                "def no(*a, **k): raise SystemExit('network')\n"
                "socket.socket.connect = no; socket.create_connection = no\n"
                "import jarvis.v3.budget, jarvis.v3.claude_usage\n"
                "print([m for m in ('jarvis.v2.daemon','jarvis.v2.hud_api','claude_agent_sdk','jarvis.llm') if m in sys.modules])")
        out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env={**os.environ, "PYTHONPATH": str(REPO)}, capture_output=True, text=True)
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "[]"), out.stderr)

    def test_boundary(self):
        self.assertEqual(imports.violations(REPO / "jarvis/v3"), [])

    def test_no_login_no_request(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"JARVIS_CLAUDE_CREDENTIALS": tmp + "/none.json"}), \
                patch("socket.create_connection", side_effect=AssertionError("network")), patch("socket.socket.connect", side_effect=AssertionError("network")):
            self.assertIsNone(claude_usage.read_windows())


if __name__ == "__main__":
    unittest.main()
