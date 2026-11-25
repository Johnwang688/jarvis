"""Orchestrator-owned acceptance tests for J1 (implementers: do not edit; if one is wrong, stop and say why).

Contract: the v3 daemon's job loop. Until now every scheduled piece of v3 (curator, feed expiry, ingest, consolidation, review queue)
shipped a `due()` or `tick()` that nothing called.
  jarvis/v3/jobs.py
    Job(name, run, *, every_s=None, due=None, needs_idle_s=0)
        run() -> anything; either every_s (seconds between starts) or due(now, last_run) -> bool; exactly one of the two
    Scheduler(jobs, *, state=None, clock=time.time, idle=None, tick_s=30.0)
        state default config.V3_DIR / "jobs" / "state.json" (0600 in a 0700 folder, temp + rename, no link followed)
        idle() -> seconds since the owner's last activity (default: jobs.idle_seconds()); an exception or None counts as busy
      tick() -> list of {"job", "outcome": "ok" | "error" | "busy", "seconds"}      only jobs that were due appear
        Jobs run one after another, in the order given. A job that raises is "error": the others still run, the text of the
        exception goes nowhere (its class name may be logged). last_run is saved after each job, so a crash repeats at most one.
        After an error a job is retried no sooner than 60 s later, doubling to at most one hour, until it succeeds.
        A job with needs_idle_s that finds the owner busy is "busy": not run, not marked, tried again next tick.
        A clock that went backwards never makes a job run in a burst: a last_run in the future is treated as now.
      start() / stop()      one background thread ticking every tick_s; the first tick comes after tick_s, never inside start();
                            stop() returns within a few seconds even while a job runs, and nothing runs after it returns.
      status() -> {name: {"last_run", "last_outcome", "failures"}}
    idle_seconds(roots=None, *, now=None) -> float      seconds since the newest change under the session folders
                            (default ~/.claude/projects and ~/.codex/sessions), a bounded scan; no folder -> a very large number
    default_jobs() -> list[Job]      what is merged today: the feed's tick (every 60 s) and the skill curator (its own due(), 2 h idle).
  jarvis/v3/daemon.py: Daemon(..., jobs=None) starts default_jobs() (or the given list) after the listener is up and stops them first
  on stop(). Daemon.start() itself runs no job and calls no model. GET /v3/status gains "jobs": Scheduler.status().
"""
import http.client
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path

from jarvis.v3 import daemon, jobs
from jarvis.v3.jobs import Job, Scheduler


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        self.now, self.idle, self.ran = 1_800_000_000.0, 10 ** 6, []
        self.state = self.tmp / "v3" / "jobs" / "state.json"

    def job(self, name, **kw):
        kw.setdefault("every_s", 300)
        return Job(name, lambda: self.ran.append(name), **kw)

    def sched(self, *job_list, **kw):
        return Scheduler(list(job_list), state=self.state, clock=lambda: self.now, idle=lambda: self.idle, **kw)

    def outcomes(self, rows):
        return [(row["job"], row["outcome"]) for row in rows]


class Ticks(Base):
    def test_due_jobs_run_in_order_and_then_wait(self):
        s = self.sched(self.job("a"), self.job("b", every_s=600))
        self.assertEqual(self.outcomes(s.tick()), [("a", "ok"), ("b", "ok")])
        self.assertEqual(s.tick(), [])
        self.now += 300
        self.assertEqual(self.outcomes(s.tick()), [("a", "ok")])
        self.now += 300
        self.assertEqual(self.outcomes(s.tick()), [("a", "ok"), ("b", "ok")])
        self.assertEqual(self.ran, ["a", "b", "a", "a", "b"])
        self.assertEqual((stat.S_IMODE(self.state.stat().st_mode), stat.S_IMODE(self.state.parent.stat().st_mode)), (0o600, 0o700))
        fresh = self.sched(self.job("a"), self.job("b", every_s=600))            # a restart does not rerun what just ran
        self.assertEqual(fresh.tick(), [])
        self.assertEqual(set(fresh.status()), {"a", "b"})
        self.assertEqual(fresh.status()["a"]["last_outcome"], "ok")

    def test_a_due_function_decides(self):
        calls = []
        s = self.sched(self.job("nightly", every_s=None, due=lambda now, last_run: (calls.append((now, last_run)), now - (last_run or 0) >= 1000)[1]))
        self.assertEqual(self.outcomes(s.tick()), [("nightly", "ok")])
        self.now += 999
        self.assertEqual(s.tick(), [])
        self.now += 1
        self.assertEqual(self.outcomes(s.tick()), [("nightly", "ok")])
        self.assertEqual(calls[0], (1_800_000_000.0, None))
        self.assertEqual(calls[-1][1], 1_800_000_000.0)
        for kw in ({"every_s": None}, {"every_s": 60, "due": lambda now, last: True}, {"every_s": 0}, {"every_s": -5}):
            with self.assertRaises((ValueError, TypeError)):
                Job("bad", lambda: None, **kw)
        with self.assertRaises((ValueError, TypeError)):
            self.sched(self.job("a"), self.job("a"))

    def test_an_error_stops_nothing_and_backs_off(self):
        def boom():
            self.ran.append("boom")
            raise RuntimeError("private words from a mail")
        s = self.sched(Job("boom", boom, every_s=30), self.job("after"))
        rows = s.tick()
        self.assertEqual(self.outcomes(rows), [("boom", "error"), ("after", "ok")])
        self.assertNotIn("private", json.dumps(rows) + json.dumps(s.status()) + self.state.read_text())
        waits = []
        for _ in range(8):
            start = self.now
            while not [r for r in s.tick() if r["job"] == "boom"]:
                self.now += 10
            waits.append(self.now - start)
            self.now += 0.001
        self.assertEqual([round(w, -1) for w in waits], [60, 120, 240, 480, 960, 1920, 3600, 3600])
        self.assertEqual(s.status()["boom"]["failures"], 9)

    def test_idle_only_jobs_wait_for_the_owner(self):
        s = self.sched(self.job("quiet", needs_idle_s=7200), self.job("always"))
        self.idle = 7199
        self.assertEqual(self.outcomes(s.tick()), [("quiet", "busy"), ("always", "ok")])
        self.idle = 7200
        self.assertEqual(self.outcomes(s.tick()), [("quiet", "ok")])
        broken = Scheduler([self.job("quiet2", needs_idle_s=60)], state=self.tmp / "s2.json", clock=lambda: self.now, idle=lambda: 1 / 0)
        self.assertEqual(self.outcomes(broken.tick()), [("quiet2", "busy")])
        self.assertNotIn("quiet2", self.ran)

    def test_clock_going_backwards_and_bad_state(self):
        s = self.sched(self.job("a"))
        s.tick()
        self.now -= 86400
        self.assertEqual(s.tick(), [])
        self.now += 299
        self.assertEqual(s.tick(), [])
        self.now += 1
        self.assertEqual(self.outcomes(s.tick()), [("a", "ok")])                 # one run, 300 s after the clock was noticed
        for junk in ("{", "[]", '{"a": 5}', '{"a": {"last_run": "x"}}', "\x00"):
            self.state.write_text(junk)
            self.ran.clear()
            self.assertEqual(self.outcomes(self.sched(self.job("a")).tick()), [("a", "ok")], junk)
        outside = self.tmp / "outside.json"
        outside.write_text("{}")
        self.state.unlink()
        self.state.symlink_to(outside)
        self.sched(self.job("a")).tick()
        self.assertEqual(outside.read_text(), "{}")


class Thread(Base):
    def test_start_runs_nothing_itself_and_stop_is_prompt(self):
        gate, started = threading.Event(), threading.Event()

        def slow():
            started.set()
            gate.wait(20)
            self.ran.append("slow-finished")
        s = Scheduler([Job("slow", slow, every_s=1)], state=self.state, clock=time.time, idle=lambda: 10 ** 6, tick_s=0.2)
        s.start()
        self.assertEqual(self.ran, [])
        self.assertFalse(started.is_set())                                       # nothing inside start()
        self.assertTrue(started.wait(5))
        begun = time.monotonic()
        stopper = threading.Thread(target=s.stop)
        stopper.start()
        stopper.join(6)
        self.assertFalse(stopper.is_alive())
        self.assertLess(time.monotonic() - begun, 6)
        gate.set()
        count = len(self.ran)
        time.sleep(1.0)
        self.assertLessEqual(len(self.ran), count + 1)                           # the one that was running may finish; no new run
        s.stop()                                                                 # idempotent

    def test_idle_seconds(self):
        root, now = self.tmp / "sessions", time.time()
        self.assertGreater(jobs.idle_seconds([root], now=now), 10 ** 6)
        (root / "a" / "b").mkdir(parents=True)
        path = root / "a" / "b" / "s.jsonl"
        path.write_text("x")
        os.utime(path, (now - 500, now - 500))
        self.assertAlmostEqual(jobs.idle_seconds([root, self.tmp / "missing"], now=now), 500, delta=5)
        newer = root / "a" / "t.jsonl"
        newer.write_text("x")
        os.utime(newer, (now - 20, now - 20))
        self.assertAlmostEqual(jobs.idle_seconds([root], now=now), 20, delta=5)
        os.utime(newer, (now + 9999, now + 9999))
        self.assertGreaterEqual(jobs.idle_seconds([root], now=now), 0)


class InDaemon(Base):
    def get(self, port, path="/v3/status"):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, json.loads(body)

    def test_the_daemon_owns_the_loop(self):
        ticked = threading.Event()
        d = daemon.Daemon(port=0, data_dir=self.tmp / "v3", hud_dir=self.tmp / "hud3", jobs=[Job("probe", ticked.set, every_s=3600)])
        d.start()
        try:
            status, body = self.get(d.port)
            self.assertEqual(status, 200)
            self.assertIn("probe", body["jobs"])
            self.assertEqual(body["version"], "v3")
        finally:
            d.stop()
        self.assertFalse((self.tmp / "v3" / "jobs" / "state.json").is_symlink())
        d.stop()

    def test_default_jobs(self):
        names = [job.name for job in jobs.default_jobs()]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("feed", names)
        self.assertIn("skill-curator", names)
        quiet = next(job for job in jobs.default_jobs() if job.name == "skill-curator")
        self.assertEqual(quiet.needs_idle_s, 2 * 3600)
        source = Path(jobs.__file__).read_text()
        for word in ("claude_jobs", "jarvis.v2.daemon", "jarvis.llm"):
            self.assertNotIn(word, source)


if __name__ == "__main__":
    unittest.main()
