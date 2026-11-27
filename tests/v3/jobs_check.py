"""Concurrency, failure handling and startup coverage for daemon jobs."""

from contextlib import closing, contextmanager
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from jarvis.v3 import daemon, jobs
from jarvis.v3.jobs import Job, Scheduler


class JobsCheck(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.state = self.root / "jobs" / "state.json"
        self.now = 1_800_000_000

    def scheduler(self, *entries, **kwargs):
        return Scheduler(entries, state=self.state, clock=lambda: self.now, **kwargs)

    def test_two_schedulers_share_one_state_file(self):
        active, release = threading.Event(), threading.Event()
        duplicate = Mock()

        def hold():
            active.set()
            release.wait(5)

        first = self.scheduler(Job("shared", hold, every_s=60), tick_s=0.01)
        second = self.scheduler(Job("shared", duplicate, every_s=60),
                                Job("other", duplicate, every_s=60))
        first.start()
        try:
            self.assertTrue(active.wait(5))
            self.assertEqual(second.tick(), [])
            duplicate.assert_not_called()
        finally:
            release.set()
            first.stop()
        self.assertEqual(first.status()["shared"]["last_outcome"], "ok")
        self.assertEqual([row["job"] for row in second.tick()], ["other"])
        self.assertEqual(set(json.loads(self.state.read_text())), {"shared", "other"})

    def test_stalled_job_leaves_status_responsive_and_stop_bounded(self):
        script = """
import threading
import time
from pathlib import Path
import sys
from jarvis.v3.jobs import Job, Scheduler

active, forever = threading.Event(), threading.Event()

def stuck():
    active.set()
    forever.wait()

scheduler = Scheduler([Job("stuck", stuck, every_s=60)],
                      state=Path(sys.argv[1]), tick_s=0.01)
scheduler.start()
assert active.wait(3)
assert scheduler.status()["stuck"]["last_run"] is None
assert scheduler.tick() == []
started = time.monotonic()
scheduler.stop()
assert time.monotonic() - started < 2
"""
        subprocess.run([sys.executable, "-c", script, str(self.state)], check=True, timeout=7)

    def test_stop_prevents_the_rest_of_a_stalled_tick(self):
        active, release = threading.Event(), threading.Event()
        after = Mock()

        def hold():
            active.set()
            release.wait(5)

        s = self.scheduler(Job("first", hold, every_s=60), Job("after", after, every_s=60),
                           tick_s=0.01)
        s.start()
        try:
            self.assertTrue(active.wait(5))
            worker = s._thread
            self.assertTrue(worker.daemon)
            s.stop()
        finally:
            release.set()
            s.stop()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        after.assert_not_called()

    def test_control_flow_exceptions_and_raising_due_are_isolated(self):
        due = Mock(side_effect=ValueError("fake error detail"))
        idle = Mock(side_effect=KeyboardInterrupt)
        after = Mock()
        s = self.scheduler(
            Job("exit", Mock(side_effect=SystemExit("fake error detail")), every_s=30),
            Job("interrupt", Mock(side_effect=KeyboardInterrupt), every_s=30),
            Job("due", Mock(), due=due),
            Job("idle", Mock(), every_s=30, needs_idle_s=1),
            Job("after", after, every_s=60),
            idle=idle,
        )
        expected = {"exit": "error", "interrupt": "error", "due": "error",
                    "idle": "busy", "after": "ok"}
        self.assertEqual({r["job"]: r["outcome"] for r in s.tick()}, expected)
        self.assertEqual(s.status()["idle"]["failures"], 0)
        self.assertNotIn("fake error detail", self.state.read_text())
        self.now += 59
        self.assertEqual([r["job"] for r in s.tick()], ["idle"])
        due.assert_called_once()
        self.now += 1
        self.assertEqual({r["job"]: r["outcome"] for r in s.tick()}, expected)
        self.assertEqual(after.call_count, 2)

    def test_backoff_starts_when_a_long_job_fails(self):
        def slow_error():
            self.now += 120
            raise RuntimeError

        s = self.scheduler(Job("slow", slow_error, every_s=30))
        self.assertEqual(s.tick()[0]["outcome"], "error")
        self.assertEqual(s.status()["slow"]["last_run"], self.now - 120)
        self.now += 59
        self.assertEqual(s.tick(), [])
        self.now += 1
        self.assertEqual(s.tick()[0]["outcome"], "error")

    def test_malformed_state_is_repaired_even_when_no_job_is_due(self):
        entries = (Job("first", Mock(), every_s=60), Job("second", Mock(), every_s=60))
        s = self.scheduler(*entries)
        self.assertEqual(len(s.tick()), 2)
        expected = json.loads(self.state.read_text())
        older_row = {**expected["first"], "last_run": self.now - 120}
        for corrupt in ('{"first":', json.dumps({"first": older_row, "second": None})):
            with self.subTest(corrupt=corrupt):
                self.state.write_text(corrupt)
                self.assertEqual(s.tick(), [])
                repaired = self.state.read_text()
                self.assertNotEqual(repaired, corrupt)
                self.assertEqual(json.loads(repaired), expected)
                self.assertEqual(self.scheduler(*entries).tick(), [])

    def test_state_folder_without_owner_write_permission(self):
        self.state.parent.mkdir(mode=0o500)
        self.addCleanup(self.state.parent.chmod, 0o700)
        ran = Mock()
        s = self.scheduler(Job("job", ran, every_s=60))
        self.assertEqual(s.tick()[0]["outcome"], "ok")
        self.assertFalse(self.state.exists())
        self.assertEqual(s.tick(), [])
        ran.assert_called_once()
        self.state.parent.chmod(0o700)
        self.now += 60
        self.assertEqual(s.tick()[0]["outcome"], "ok")
        self.assertEqual(json.loads(self.state.read_text())["job"]["last_run"], self.now)

    def test_hundreds_of_jobs_and_per_job_persistence(self):
        names = [f"job-{i}" for i in range(320)]
        seen = []

        def record(name, previous):
            if previous is not None:
                seen.append((previous, json.loads(self.state.read_text())[previous]["last_outcome"]))
            seen.append(name)

        entries = [Job(name, lambda name=name, prior=names[i-1] if i else None: record(name, prior),
                       every_s=600) for i, name in enumerate(names)]
        s = self.scheduler(*entries)
        self.assertEqual([r["outcome"] for r in s.tick()], ["ok"] * len(names))
        self.assertEqual(seen[::2], names)
        self.assertEqual(seen[1::2], [(name, "ok") for name in names[:-1]])
        self.assertEqual(self.scheduler(*entries).tick(), [])

    def test_idle_scan_bounds_directory_iteration(self):
        root = self.root / "sessions"
        root.mkdir()
        for i in range(jobs._IDLE_SCAN_ENTRIES + 128):
            (root / f"{i}.jsonl").touch()
        scan = os.scandir
        visited = []

        @contextmanager
        def counted(directory):
            def count(entries):
                for entry in entries:
                    visited.append(entry.name)
                    yield entry
            with scan(directory) as entries:
                yield count(entries)

        with patch.object(jobs.os, "scandir", counted):
            self.assertEqual(jobs.idle_seconds([root]), 0)
        self.assertEqual(len(visited), jobs._IDLE_SCAN_ENTRIES + 1)

    def test_default_jobs_use_one_feed_and_the_curators_due_predicate(self):
        feed, store, curate = Mock(), Mock(), Mock()
        due = Mock(return_value=False)
        modules = {
            "jarvis.v3.feed": SimpleNamespace(Feed=feed),
            "jarvis.v3.skill_curator": SimpleNamespace(Curator=curate, due=due),
            "jarvis.v3.skill_store": SimpleNamespace(SkillStore=store),
        }
        with patch.dict(sys.modules, modules):
            entries = [job for job in jobs.default_jobs() if job.name in {"feed", "skill-curator"}]
            s = self.scheduler(*entries, idle=lambda: 7200)
            self.assertEqual([r["job"] for r in s.tick()], ["feed"])
            due.assert_called_once_with(self.now, last_run=None, idle_s=7200)
            store.assert_not_called()
            due.return_value = True
            self.now += 60
            self.assertEqual([r["job"] for r in s.tick()], ["feed", "skill-curator"])
        feed.assert_called_once_with()
        self.assertEqual(feed.return_value.tick.call_count, 2)
        store.assert_called_once_with()
        curate.assert_called_once_with(store.return_value)
        curate.return_value.run.assert_called_once_with()

    def test_failed_default_imports_stay_inside_jobs(self):
        with patch.dict(sys.modules, {"jarvis.v3.feed": None, "jarvis.v3.skill_curator": None}):
            entries = [job for job in jobs.default_jobs() if job.name in {"feed", "skill-curator"}]
            s = self.scheduler(*entries, idle=lambda: 7200)
            self.assertEqual({r["job"]: r["outcome"] for r in s.tick()},
                             {"feed": "error", "skill-curator": "error"})

    def test_daemon_restarts_its_worker_and_persists_progress(self):
        ran = threading.Event()
        d = daemon.Daemon(port=0, data_dir=self.root, jobs=[Job("one", ran.set, every_s=60)])
        d._scheduler._tick_s = 0.01
        self.addCleanup(d.stop)
        for _ in range(3):
            d.start()
            worker = d._scheduler._thread
            d.start()
            self.assertIs(d._scheduler._thread, worker)
            self.assertTrue(ran.wait(5))
            with closing(HTTPConnection(*d.address, timeout=2)) as connection:
                connection.request("GET", "/v3/status")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIn("one", json.load(response)["jobs"])
            d.stop()
            self.assertFalse(worker.is_alive())
            self.assertEqual(d._scheduler.status()["one"]["last_outcome"], "ok")
        self.assertEqual(json.loads(self.state.read_text())["one"]["last_outcome"], "ok")

    def test_agentd_starts_with_optional_job_and_model_imports_blocked(self):
        script = """
from contextlib import closing
from http.client import HTTPConnection
import json
import runpy
import signal
import sys

forbidden = {"jarvis.agent", "jarvis.llm", "jarvis.models", "jarvis.v2.providers",
             "jarvis.v3.feed", "jarvis.v3.skill_curator", "jarvis.v3.skill_store"}

class BlockImports:
    def find_spec(self, fullname, *args):
        if fullname in forbidden:
            raise AssertionError(fullname)

sys.meta_path.insert(0, BlockImports())
from jarvis.v3.daemon import Daemon
start = Daemon.start

def checked_start(self):
    start(self)
    with closing(HTTPConnection(*self.address, timeout=2)) as connection:
        connection.request("GET", "/v3/status")
        response = connection.getresponse()
        assert response.status == 200
        assert set(json.load(response)["jobs"]) == {
            "feed", "skill-curator", "transcripts", "reviewer", "gmail", "calendar",
            "schoolwork", "github", "triggers", "work-watch", "planner", "executor", "consolidate", "morning-brief",
            "learning-digest", "evening-wrap", "browser-reap",
        }
    assert not forbidden.intersection(sys.modules)
    signal.raise_signal(signal.SIGTERM)

Daemon.start = checked_start
sys.argv = ["jarvis", "agentd"]
runpy.run_module("jarvis", run_name="__main__")
"""
        env = {**os.environ, "JARVIS_V3_PORT": "0", "JARVIS_V3_DIR": str(self.root)}
        subprocess.run([sys.executable, "-c", script], env=env, check=True, timeout=10)


if __name__ == "__main__":
    unittest.main()
