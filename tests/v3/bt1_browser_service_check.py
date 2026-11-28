"""Exercise daemon browser ownership with fake sessions and no browser process."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest
from unittest.mock import Mock, patch

from lg2b_browser_session_check import FakeGate, FakeLink, OTHER, PUBLIC, Trace

from jarvis.v3.browser.desk import BrowserDesk
from jarvis.v3.browser.link import LinkError
from jarvis.v3.browser.service import BrowserBusy, BrowserService, BrowserUnavailable
from jarvis.v3.browser.session import BrowserError, BrowserSession
from jarvis.v3.daemon import Daemon


CLOSED = {"chrome": "closed", "edge": "closed"}


class FakeSession:
    def __init__(self, browser, *, window=False):
        self.browser = browser
        self.window = window
        self.alive = False
        self.starts = self.closes = 0
        self.fail_start = self.fail_close = False

    def start(self):
        self.starts += 1
        self.alive = True
        if self.fail_start:
            raise OSError("Start failed for https://example.invalid at /example/profile")

    def close(self):
        self.closes += 1
        self.alive = False
        if self.fail_close:
            raise OSError("Fixture close failed")


class BrowserServiceCheck(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.sessions = []
        self.service = BrowserService(session_factory=self.make_session, clock=lambda: self.now,
                                      idle_seconds=10, lease_wait=0.05)
        self.addCleanup(self.service.close)

    def make_session(self, browser, *, window=False):
        session = FakeSession(browser, window=window)
        self.sessions.append(session)
        return session

    def borrow(self, browser):
        with self.service.use(browser) as session:
            return session

    def test_lazy_and_invalid_selection(self):
        self.assertEqual(self.service.status(), CLOSED)
        self.assertEqual(self.service.reap(), 0)
        for invalid in ("firefox", None, []):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.borrow(invalid)
        self.service.close()
        self.assertEqual(self.service.status(), CLOSED)
        self.assertEqual(self.service.reap(), 0)
        with self.assertRaisesRegex(BrowserUnavailable, r"^The browser service is closed\.$"):
            self.borrow("chrome")
        self.assertEqual(self.sessions, [])

    def test_reuse_and_replace_dead_sessions(self):
        with self.service.use("chrome") as first:
            self.assertTrue(first.alive)
            self.assertEqual(self.service.status(), {"chrome": "in_use", "edge": "closed"})
        self.assertEqual(self.service.status(), {"chrome": "open", "edge": "closed"})
        self.assertIs(self.borrow("chrome"), first)
        self.assertEqual(first.starts, 1)

        first.alive = False
        self.assertEqual(self.service.status(), {"chrome": "failed", "edge": "closed"})
        with (self.assertRaisesRegex(BrowserError, r"^The browser session is closed\.$"),
              self.service.use("chrome") as second):
            self.assertIsNot(second, first)
            self.assertEqual(first.closes, 1)
            second.alive = False
        self.assertEqual(second.closes, 1)
        self.assertEqual(self.service.status(), CLOSED)
        self.assertIsNot(self.borrow("chrome"), second)
        self.assertEqual(len(self.sessions), 3)

    def test_factory_receives_window_mode_and_reap_closes_visible_session(self):
        self.assertFalse(self.borrow("chrome").window)
        factory = Mock(side_effect=self.make_session)
        service = BrowserService(window=True, session_factory=factory, clock=lambda: self.now,
                                 idle_seconds=10)
        self.addCleanup(service.close)
        with service.use("edge") as visible:
            self.assertTrue(visible.window)
        factory.assert_called_once_with("edge", window=True)
        self.assertEqual(service.status(), {"chrome": "closed", "edge": "open"})
        self.now += 11
        self.assertEqual(service.reap(), 1)
        self.assertEqual(visible.closes, 1)
        self.assertEqual(service.status(), CLOSED)

    def test_lost_main_tab_during_use_returns_fixed_error_and_next_use_restarts(self):
        trace = Trace()
        links = []

        def make_session(browser, *, window):
            gate = FakeGate(trace)
            link = FakeLink(trace, gate)
            links.append(link)
            return BrowserSession(browser, window=window, link_factory=link.factory,
                                  gate_factory=gate.factory)

        factory = Mock(side_effect=make_session)
        service = BrowserService(window=True, session_factory=factory)
        self.addCleanup(service.close)
        desk = BrowserDesk(service, store_factory=Mock(), feed_post=Mock())
        with service.use("chrome") as first:
            self.assertEqual(first.open(PUBLIC).outcome, "loaded")
        link = links[0]

        def disconnect(_url):
            link.emit("Target.targetDestroyed", {"targetId": "target-1"}, session=None)
            raise LinkError("Fixture transport error")

        link.navigation = disconnect
        self.assertEqual(desk.open(OTHER), "Error: The browser session is closed.")
        self.assertFalse(first.alive)
        self.assertEqual(service.status(), CLOSED)
        self.assertEqual(trace.items.count(("link.close",)), 1)
        with service.use("chrome") as second:
            self.assertIsNot(second, first)
            self.assertEqual(second.open(OTHER).outcome, "loaded")
        self.assertEqual(factory.call_count, 2)

    def test_failed_start_is_closed_redacted_and_retryable(self):
        broken = self.make_session("chrome")
        broken.fail_start = broken.fail_close = True
        with patch.object(self.service, "_session_factory", return_value=broken) as factory:
            with self.assertRaisesRegex(BrowserUnavailable, r"^The browser could not be started\.$") as error:
                self.borrow("chrome")
        factory.assert_called_once_with("chrome", window=False)
        self.assertIsNone(error.exception.__cause__)
        self.assertIsNone(error.exception.__context__)
        self.assertEqual((broken.starts, broken.closes), (1, 1))
        self.assertEqual(self.service.status(), {"chrome": "failed", "edge": "closed"})
        self.assertTrue(self.borrow("chrome").alive)
        self.assertEqual(self.service.status(), {"chrome": "open", "edge": "closed"})

    def test_waiting_user_reuses_session_and_other_browser_is_independent(self):
        service = BrowserService(session_factory=self.make_session, lease_wait=2)
        self.addCleanup(service.close)
        requesting = Event()
        acquired = Event()

        def borrow_chrome():
            requesting.set()
            with service.use("chrome") as session:
                acquired.set()
                return session

        with ThreadPoolExecutor() as workers:
            with service.use("chrome") as chrome:
                waiting = workers.submit(borrow_chrome)
                self.assertTrue(requesting.wait(1))
                self.assertFalse(acquired.wait(0.02))
                with service.use("edge") as edge:
                    self.assertEqual(edge.browser, "edge")
                    self.assertEqual(service.status(), {"chrome": "in_use", "edge": "in_use"})
            self.assertIs(waiting.result(timeout=2), chrome)
        self.assertEqual(len(self.sessions), 2)

    def test_lease_timeout_and_release_when_body_raises(self):
        def contend():
            before = time.monotonic()
            with self.assertRaisesRegex(BrowserBusy, r"^The browser is in use\.$"):
                self.borrow("chrome")
            self.assertGreaterEqual(time.monotonic() - before, 0.05)

        with ThreadPoolExecutor() as workers:
            with self.assertRaisesRegex(RuntimeError, "fixture body failure"):
                with self.service.use("chrome") as chrome:
                    workers.submit(contend).result(timeout=2)
                    self.now += 100
                    raise RuntimeError("fixture body failure")
        self.assertEqual(self.service.reap(), 0)
        self.assertIs(self.borrow("chrome"), chrome)

    def test_reap_counts_only_expired_free_sessions_even_if_close_raises(self):
        chrome = self.borrow("chrome")
        edge = self.borrow("edge")
        self.now += 10
        self.assertEqual(self.service.reap(), 0)
        self.now += 1
        chrome.fail_close = True
        with self.service.use("edge"):
            slot = self.service._slots["edge"]
            with patch.object(slot, "lease", wraps=slot.lease) as lease:
                self.assertEqual(self.service.reap(), 1)
                lease.acquire.assert_called_once_with(blocking=False)
            self.assertEqual(self.service.status(), {"chrome": "closed", "edge": "in_use"})
        self.assertEqual((chrome.closes, edge.closes), (1, 0))
        self.assertEqual(self.service.reap(), 0)
        self.now += 11
        self.assertEqual(self.service.reap(), 1)
        self.assertEqual(edge.closes, 1)
        self.assertEqual(self.service.status(), CLOSED)

    def test_close_times_out_held_lease_and_closes_both_once(self):
        edge = self.borrow("edge")
        with ThreadPoolExecutor() as workers:
            with self.service.use("chrome") as chrome:
                chrome.fail_close = True
                before = time.monotonic()
                workers.submit(self.service.close).result(timeout=2)
                self.assertGreaterEqual(time.monotonic() - before, 0.05)
                self.assertEqual((chrome.closes, edge.closes), (1, 1))
                with self.assertRaises(BrowserUnavailable):
                    self.borrow("chrome")
        self.service.close()
        self.assertEqual((chrome.closes, edge.closes), (1, 1))
        self.assertEqual(self.service.status(), CLOSED)

    def test_shutdown_waits_for_start_to_finish_and_close(self):
        for fail in (False, True):
            with self.subTest(fail=fail), ThreadPoolExecutor() as workers:
                started, finish, waiting = Event(), Event(), Event()
                session = FakeSession("chrome")
                session.fail_start = fail
                start = session.start
                service = BrowserService(session_factory=lambda _, *, window: session, lease_wait=0)
                slot = service._slots["chrome"]
                original_lease = slot.lease
                lease = Mock(wraps=original_lease)

                def acquire(*args, **kwargs):
                    if started.is_set():
                        waiting.set()
                    return original_lease.acquire(*args, **kwargs)

                lease.acquire.side_effect = acquire
                slot.lease = lease

                def delayed_start():
                    try:
                        start()
                    finally:
                        started.set()
                        finish.wait(2)

                def borrow():
                    with service.use("chrome"):
                        self.fail("Shutdown must reject the pending browser user")

                with patch.object(session, "start", side_effect=delayed_start):
                    pending = workers.submit(borrow)
                    try:
                        self.assertTrue(started.wait(1))
                        shutdown = workers.submit(service.close)
                        self.assertTrue(waiting.wait(1))
                        lease.acquire.assert_called_with()
                        self.assertFalse(shutdown.done())
                        self.assertTrue(session.alive)
                        self.assertEqual(service.status()["chrome"], "in_use")
                    finally:
                        finish.set()
                    shutdown.result(timeout=2)
                    self.assertFalse(session.alive)
                    with self.assertRaisesRegex(BrowserUnavailable, r"^The browser service is closed\.$"):
                        pending.result(timeout=2)
                self.assertEqual((session.starts, session.closes), (1, 1))
                self.assertEqual(service.status(), CLOSED)

    def test_shutdown_retries_a_reap_still_closing_after_lease_timeout(self):
        session = self.borrow("chrome")
        self.now += 11
        entered, finish = Event(), Event()
        close = session.close

        def slow_first_close():
            if not entered.is_set():
                entered.set()
                finish.wait(2)
            close()

        with patch.object(session, "close", side_effect=slow_first_close), ThreadPoolExecutor() as workers:
            reap = workers.submit(self.service.reap)
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(session.alive)
                self.assertEqual(self.service.status()["chrome"], "in_use")
                workers.submit(self.service.close).result(timeout=1)
                self.assertFalse(session.alive)
                self.assertEqual(session.closes, 1)
                self.assertEqual(self.service.status()["chrome"], "in_use")
            finally:
                finish.set()
            self.assertEqual(reap.result(timeout=2), 1)
        self.assertEqual(session.closes, 2)
        self.assertEqual(self.service.status(), CLOSED)

    def test_later_shutdown_retries_a_close_failure_that_left_session_alive(self):
        for reap_first in (False, True):
            with self.subTest(reap_first=reap_first):
                session = FakeSession("chrome")
                service = BrowserService(session_factory=lambda _, *, window: session, clock=lambda: self.now,
                                         idle_seconds=10)
                self.addCleanup(service.close)
                with service.use("chrome"):
                    pass
                self.now += 11
                close = session.close

                def fail_once():
                    if teardown.call_count == 1:
                        raise OSError("Fixture close failed")
                    close()

                with patch.object(session, "close", side_effect=fail_once) as teardown:
                    if reap_first:
                        self.assertEqual(service.reap(), 1)
                    else:
                        service.close()
                    self.assertTrue(session.alive)
                    self.assertEqual(service.status()["chrome"], "open")
                    service.close()
                    self.assertFalse(session.alive)
                    self.assertEqual(teardown.call_count, 2)
                self.assertEqual(service.status(), CLOSED)


class DaemonBrowserCheck(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"HOME": str(root), "XDG_CONFIG_HOME": str(root)}))
        self.popen = self.enterContext(
            patch.object(subprocess, "Popen", side_effect=AssertionError("Unexpected process")))
        self.options = dict(port=0, voice_link_port=0, phone_host=None, data_dir=root / "data", jobs=())

    def status(self, daemon):
        with closing(HTTPConnection(*daemon.address, timeout=2)) as connection:
            connection.request("GET", "/v3/status")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            return json.load(response)

    def test_browser_status_reap_schedule_and_defensive_stop(self):
        service = BrowserService(session_factory=FakeSession)
        daemon = Daemon(**self.options, browser=service)
        self.addCleanup(daemon.stop)
        self.assertIs(daemon.browser, service)
        scheduler = daemon._scheduler
        now = 100.0
        scheduler._clock = lambda: now
        with patch.object(service, "reap_async", wraps=service.reap_async) as reap:
            daemon.start()
            reap.assert_not_called()
            with service.use("chrome") as session:
                self.assertEqual(self.status(daemon)["browser"], {"chrome": "in_use", "edge": "closed"})
            status = self.status(daemon)
            self.assertEqual(status["browser"], {"chrome": "open", "edge": "closed"})
            self.assertIn("browser-reap", status["jobs"])
            scheduler.tick()
            now += 59
            scheduler.tick()
            reap.assert_called_once_with()
            now += 1
            scheduler.tick()
            self.assertEqual(reap.call_count, 2)
        with patch.object(daemon._voice_brain, "close", side_effect=RuntimeError("Fixture close failure")):
            daemon.stop()
        self.assertEqual(session.closes, 1)
        self.assertEqual(service.status(), CLOSED)
        self.popen.assert_not_called()

    def test_periodic_reap_returns_while_teardown_runs_and_does_not_overlap(self):
        now = 100.0
        service = BrowserService(session_factory=FakeSession, clock=lambda: now, idle_seconds=10)
        daemon = Daemon(**self.options, browser=service)
        self.addCleanup(daemon.stop)
        scheduler = daemon._scheduler
        scheduler._clock = lambda: now
        with service.use("chrome") as session:
            pass
        now += 11
        entered, finish = Event(), Event()
        close = session.close

        def delayed_close():
            entered.set()
            finish.wait(2)
            close()

        with (patch.object(session, "close", side_effect=delayed_close),
              patch.object(service, "reap", wraps=service.reap) as reap,
              ThreadPoolExecutor() as workers):
            try:
                first = workers.submit(scheduler.tick)
                self.assertTrue(entered.wait(1))
                self.assertEqual(first.result(timeout=1)[0]["outcome"], "ok")
                self.assertTrue(session.alive)
                reaper = service._reaper
                now += 60
                self.assertEqual(workers.submit(scheduler.tick).result(timeout=1)[0]["outcome"], "ok")
                self.assertIs(service._reaper, reaper)
                reap.assert_called_once_with()
                self.assertTrue(reaper.is_alive())
            finally:
                finish.set()
            with patch.object(reaper, "join", wraps=reaper.join) as join:
                daemon.stop()
                join.assert_called_once()
                self.assertLessEqual(join.call_args.args[0], service._lease_wait)
            self.assertFalse(reaper.is_alive())
        self.assertFalse(session.alive)
        self.assertEqual(session.closes, 1)
        self.assertEqual(service.status(), CLOSED)

    def test_default_service_stays_lazy_through_start_stop_and_restart(self):
        for override, window in ((None, True), ("1", False), ("true", True)):
            with (self.subTest(override=override), patch.dict(os.environ),
                  patch("jarvis.v3.daemon.BrowserService", wraps=BrowserService) as factory):
                if override is None:
                    os.environ.pop("JARVIS_BROWSER_HEADLESS", None)
                else:
                    os.environ["JARVIS_BROWSER_HEADLESS"] = override
                self.check_default_service_restart()
                self.assertEqual(factory.call_count, 2)
                self.assertTrue(all(call.args == () and call.kwargs == {"window": window}
                                    for call in factory.call_args_list))

    def check_default_service_restart(self):
        daemon = Daemon(**self.options)
        self.addCleanup(daemon.stop)
        original = daemon.browser
        daemon.start()
        self.assertIs(daemon.browser, original)
        self.assertIs(daemon._chat._browser_desk._service, original)
        self.assertEqual(self.status(daemon)["browser"], CLOSED)
        daemon.stop()
        with self.assertRaises(BrowserUnavailable), original.use("chrome"):
            self.fail("A stopped service cannot be used")
        daemon.start()
        self.assertIsNot(daemon.browser, original)
        self.assertIs(daemon._chat._browser_desk._service, daemon.browser)
        self.assertEqual(self.status(daemon)["browser"], CLOSED)
        daemon.stop()
        self.popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
