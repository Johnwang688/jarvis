"""Regression probes for I4a's approval and shutdown boundaries."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import UTC, datetime, timedelta
import threading
import unittest
from unittest.mock import patch

import executor_check
from jarvis.v3.daemon import Daemon
from jarvis.v3.jobs import default_jobs
from jarvis.v3.policy import set_rule
from jarvis.v3.proposals import ProposalStore


class ExecutorReview(unittest.TestCase):
    def setUp(self):
        self.fixture = executor_check.ExecutorChecks()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def test_stop_request_prevents_later_starts_in_the_active_sweep(self):
        fx = self.fixture
        fx.instant = datetime.now(UTC)
        first, second = fx.add("first"), fx.add("second")
        fx.answer(first)
        fx.answer(second)
        entered, release = threading.Event(), threading.Event()

        def blocked_start(*_):
            entered.set()
            if not release.wait(8):
                raise RuntimeError("Fake start was not released.")

        fx.t3.before_start = blocked_start
        with patch("jarvis.v3.daemon.WorkService", return_value=fx.work):
            daemon = Daemon(port=0, data_dir=fx.data, jobs=[], phone_host=None)
            daemon.start()
            worker = daemon._executor_thread
            with ThreadPoolExecutor(max_workers=1) as pool:
                stopped = pool.submit(self.stop_after_start, daemon, entered)
                try:
                    self.assertTrue(entered.wait(3))
                    self.assertTrue(daemon._executor_stop.wait(3))
                finally:
                    release.set()
                stopped.result(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(fx.t3.starts), 1)

    def test_scheduler_cannot_start_more_work_after_daemon_stop(self):
        fx = self.fixture
        fx.instant = datetime.now(UTC)
        for key in ("first", "second"):
            fx.answer(fx.add(key))
        entered, release = threading.Event(), threading.Event()

        def blocked_start(*_):
            entered.set()
            if not release.wait(8):
                raise RuntimeError("Fake start was not released.")

        fx.t3.before_start = blocked_start
        jobs = [job for job in default_jobs(executor=lambda: daemon._sweep_executor())
                if job.name == "executor"]
        with patch("jarvis.v3.daemon.WorkService", return_value=fx.work):
            daemon = Daemon(port=0, data_dir=fx.data, jobs=jobs, phone_host=None)
            daemon._scheduler._tick_s = 0.01
            with patch.object(daemon._executor_worker, "_run_sweep"):
                daemon.start()
                scheduler = daemon._scheduler._thread
                with ThreadPoolExecutor(max_workers=1) as pool:
                    stopped = pool.submit(self.stop_after_start, daemon, entered)
                    try:
                        self.assertTrue(entered.wait(3))
                        try:
                            stopped.result(timeout=2)
                        except TimeoutError:
                            pass
                    finally:
                        release.set()
                    stopped.result(timeout=3)
                scheduler.join(3)
        self.assertFalse(scheduler.is_alive())
        self.assertEqual(len(fx.t3.starts), 1)

    @staticmethod
    def stop_after_start(daemon, entered):
        entered.wait(3)
        daemon.stop()


if __name__ == "__main__":
    unittest.main()
