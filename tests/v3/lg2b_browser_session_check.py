"""LG2b session checks with a scripted CDP link and an in-memory gate."""

import os
import socket
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jarvis import config
from jarvis.v2.browser import policy
from jarvis.v3.browser import session as S
from jarvis.v3.browser import signin
from jarvis.v3.browser.link import LinkError


BLANK = "about:blank"
PUBLIC = "https://example.invalid/"
OTHER = "https://other.example.invalid/"
FILE = "file:///home/user/example"
ROOT_FRAME = "root-frame"
SESSION = "main-session"


class Trace:
    def __init__(self):
        self.items = []
        self.changed = threading.Condition()

    def add(self, *item):
        with self.changed:
            self.items.append(item)
            self.changed.notify_all()

    def wait_for(self, item, *, count=1):
        with self.changed:
            if not self.changed.wait_for(lambda: self.items.count(item) >= count, timeout=2):
                raise AssertionError(f"Missing call: {item}")


class FakeGate:
    port = 19000

    def __init__(self, trace):
        self.trace = trace
        self.refused = None
        self.names = []
        self.forgotten = []
        self.pending = {}
        self.waits = []
        self.passes = {1}

    def factory(self, *, refused):
        self.refused = refused
        return self

    def start(self):
        self.trace.add("gate.start")
        return self.port

    def stop(self):
        self.trace.add("gate.stop")

    def expect_check(self, name):
        self.trace.add("gate.expect", name)
        self.names.append(name)
        event = threading.Event()
        self.pending[name] = event

        def wait(timeout):
            self.waits.append(timeout)
            return event.is_set()

        return SimpleNamespace(wait=wait)

    def forget_check(self, name):
        self.trace.add("gate.forget", name)
        self.forgotten.append(name)
        del self.pending[name]

    def visit(self, url):
        name = url.removeprefix("http://").removesuffix("/")
        if len(self.names) in self.passes:
            self.pending[name].set()


class FakeLink:
    def __init__(self, trace, gate):
        self.trace = trace
        self.gate = gate
        self.alive = False
        self.fail = None
        self.navigation = self.loaded
        self.blank_navigation = self.loaded
        self.history = None
        self.main_frame = {"id": ROOT_FRAME, "url": BLANK, "loaderId": "blank-loader"}
        self.frame_tree = lambda: self.main_frame.copy()
        self.create_world = lambda: {"executionContextId": 7}
        self.document_reply = lambda: {"result": {"type": "string", "value": "none"}}
        self.targets = {}
        self.window = False
        self.next_target = 0
        self.callback = None
        self.reader = threading.local()

    def factory(self, browser, *, proxy_port, window):
        self.trace.add("link.factory", browser, proxy_port, window)
        self.window = window
        return self

    def start(self):
        self.trace.add("link.start")
        self.alive = True
        if self.fail == "start":
            raise LinkError(FILE)

    def close(self):
        self.trace.add("link.close")
        self.alive = False

    def subscribe(self, callback):
        self.callback = callback

    def emit(self, method, params, *, session=SESSION):
        event = {"method": method, "params": params}
        if session is not None:
            event["sessionId"] = session
        self.reader.in_callback = True
        try:
            self.callback(event)
        finally:
            self.reader.in_callback = False

    def frame(self, url, *, frame=ROOT_FRAME, loader="loader", session=SESSION, **fields):
        document = {"id": frame, "url": url, "loaderId": loader, **fields}
        if session == SESSION and "parentId" not in fields:
            self.main_frame = document.copy()
        self.emit("Page.frameNavigated", {"frame": document}, session=session)

    def load(self, loader="loader", frame=ROOT_FRAME):
        self.emit("Page.lifecycleEvent", {"name": "load", "loaderId": loader, "frameId": frame})

    def same_document(self, url, frame=ROOT_FRAME):
        if frame == self.main_frame["id"]:
            self.main_frame.pop("urlFragment", None)
            self.main_frame["url"] = url
        self.emit("Page.navigatedWithinDocument", {"frameId": frame, "url": url})

    def loaded(self, url):
        self.frame(url)
        self.load()
        return {"frameId": ROOT_FRAME, "loaderId": "loader"}

    def target_created(self, target, url, kind="page"):
        self.targets[target] = url
        self.emit("Target.targetCreated", {"targetInfo": {"type": kind, "targetId": target}},
                  session=None)

    def call(self, method, params=None, *, session=None, timeout=30.0):
        if getattr(self.reader, "in_callback", False):
            raise AssertionError("CDP call from the reader callback")
        if not self.alive:
            raise LinkError("The link is dead.")
        self.trace.add(method, params, session)
        if self.fail == method:
            raise LinkError("The browser refused the request.", browser_message=FILE)
        if method == "Target.setDiscoverTargets" and not self.window:
            self.target_created("startup", BLANK)
        elif method == "Target.createTarget":
            self.next_target += 1
            target = f"target-{self.next_target}"
            url = params["url"]
            self.target_created(target, url)
            if url != BLANK:
                self.gate.visit(url)
            return {"targetId": target}
        elif method == "Target.attachToTarget":
            return {"sessionId": SESSION}
        elif method == "Target.closeTarget":
            target = params["targetId"]
            if self.targets.pop(target, None) is None:
                return {"success": False}
            self.emit("Target.targetDestroyed", {"targetId": target}, session=None)
            return {"success": True}
        elif method == "Page.getFrameTree":
            return {"frameTree": {"frame": self.frame_tree()}}
        elif method == "Page.createIsolatedWorld":
            return self.create_world()
        elif method == "Runtime.callFunctionOn":
            return self.document_reply()
        elif method == "Page.navigate":
            url = params["url"]
            return self.blank_navigation(url) if url == BLANK else self.navigation(url)
        elif method == "Page.getNavigationHistory":
            if self.history is not None:
                return self.history()
            raise AssertionError("Location must use the committed frame tree")
        return {}


class BrowserSessionCheck(unittest.TestCase):
    def setUp(self):
        home = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"HOME": home, "XDG_CONFIG_HOME": home}))
        self.enterContext(patch.object(subprocess, "Popen", side_effect=AssertionError("no process")))
        self.enterContext(patch.object(socket, "socket", side_effect=AssertionError("no socket")))
        self.trace = Trace()
        self.gate = FakeGate(self.trace)
        self.link = FakeLink(self.trace, self.gate)
        self.now = 10.0
        self.sleeps = []
        self.enterContext(patch.object(S, "time", SimpleNamespace(monotonic=time.monotonic,
                                                                sleep=self.sleeps.append)))
        self.browser = self.make_browser(window=True)

    def make_browser(self, *, window):
        browser = S.BrowserSession("edge", window=window, link_factory=self.link.factory,
                                   gate_factory=self.gate.factory, clock=lambda: self.now,
                                   load_timeout=0.04, settle_timeout=0.10,
                                   extra_refused_ports={19001})
        self.addCleanup(browser.close)
        return browser

    def refused(self, action, reason):
        with self.assertRaises(S.BrowserError) as caught:
            action()
        self.assertEqual(caught.exception.reason, reason)
        self.assertNotIn(FILE, str(caught.exception))
        return str(caught.exception)

    def probe_calls(self):
        return [call for call in self.trace.items if call[0] == "Target.createTarget"
                and call[1]["url"] != BLANK]

    def assert_closed(self):
        self.assertFalse(self.browser.alive)
        self.assertEqual(self.trace.items[-2:], [("link.close",), ("gate.stop",)])

    def test_start_order_and_dynamic_refused_ports(self):
        self.browser.start()
        self.browser.start()
        calls = self.trace.items
        self.assertEqual(calls[:3], [("gate.start",), ("link.factory", "edge", self.gate.port, True),
                                     ("link.start",)])
        self.assertEqual(self.gate.refused(), policy.refused_ports() | {19001, self.gate.port})
        self.assertTrue(config.control_ports() <= self.gate.refused())
        with patch.multiple(config, V3_API_PORT=19110, REMOTE_PORT=19108, VOICE_LINK_PORT=19109):
            self.assertTrue({19108, 19109, 19110, 8408, 8409, 8410} <= self.gate.refused())
        self.assertEqual([item for item in calls if item[0][0].isupper()],
                         self.setup_calls({"url": BLANK, "newWindow": True, "background": True}))
        self.assertEqual(self.link.targets, {"target-1": BLANK})
        self.refused(self.browser.location, "not_a_site")

    def setup_calls(self, main_params):
        name, = self.gate.names
        return [
            ("Target.setDiscoverTargets", {"discover": True}, None),
            ("Target.createTarget", main_params, None),
            ("Target.attachToTarget", {"targetId": "target-1", "flatten": True}, None),
            ("Page.enable", None, SESSION),
            ("Network.enable", None, SESSION),
            ("Browser.setDownloadBehavior", {"behavior": "deny"}, None),
            ("Page.setLifecycleEventsEnabled", {"enabled": True}, SESSION),
            ("Page.getFrameTree", None, SESSION),
            ("Target.createTarget", {"url": f"http://{name}/", "background": True}, None),
            ("Target.closeTarget", {"targetId": "target-2"}, None),
        ]

    def test_headless_control_calls_match_original_setup(self):
        self.make_browser(window=False).start()
        startup_close = ("Target.closeTarget", {"targetId": "startup"}, None)
        self.trace.wait_for(startup_close)
        calls = [item for item in self.trace.items if item[0][0].isupper()]
        self.assertEqual(calls.count(startup_close), 1)
        calls.remove(startup_close)
        self.assertEqual(calls, self.setup_calls({"url": BLANK}))

    def test_link_start_and_setup_failures_close_link_and_gate(self):
        for method in ("start", "Network.enable"):
            with self.subTest(method=method):
                self.link.fail = method
                self.trace.items.clear()
                self.refused(self.browser.start, "start_failed")
                self.assert_closed()
                self.browser.close()
                self.assertEqual(self.trace.items.count(("gate.stop",)), 1)

    def test_gate_check_passes_on_third_attempt(self):
        self.browser = self.make_browser(window=False)
        self.gate.passes = {3}
        self.browser.start()
        self.assertTrue(self.browser.alive)
        self.assertEqual(self.sleeps, [2, 2])
        self.assertEqual(len(set(self.gate.names)), 3)
        self.assertEqual(self.gate.forgotten, self.gate.names)
        self.assertFalse(self.gate.pending)
        self.assertEqual(len(self.gate.waits), 3)
        self.assertTrue(all(0 < timeout <= 5 for timeout in self.gate.waits))
        for number, name in enumerate(self.gate.names, start=2):
            self.assertRegex(name, r"^[0-9a-f]{32}\.gate-check\.invalid$")
            arm = self.trace.items.index(("gate.expect", name))
            visit = self.trace.items.index(("Target.createTarget",
                                            {"url": f"http://{name}/", "background": True}, None))
            forget = self.trace.items.index(("gate.forget", name))
            closed = ("Target.closeTarget", {"targetId": f"target-{number}"}, None)
            self.assertEqual(self.trace.items.count(closed), 1)
            self.assertLess(arm, visit)
            self.assertLess(visit, forget)
            self.assertLess(forget, self.trace.items.index(closed))

    def test_gate_check_failure_closes_everything(self):
        self.browser = self.make_browser(window=False)
        self.gate.passes.clear()
        message = self.refused(self.browser.start, "gate_bypassed")
        self.assertEqual(message, "The browser did not use Jarvis's network gate.")
        self.assertEqual(len(self.probe_calls()), 3)
        self.assertEqual(self.gate.forgotten, self.gate.names)
        self.assert_closed()

    def test_refused_open_never_calls_link(self):
        self.browser.start()
        self.now += 301
        count = len(self.trace.items)
        urls = ("http://192.0.2.1/", "http://localhost/", "http://127.0.0.1/",
                "http://[::ffff:127.0.0.1]/", "http://example.invalid:8410/",
                f"https://example.invalid:{self.gate.port}/",
                "https://example.invalid:19001/", FILE, BLANK,
                "https://user@example.invalid/", "https://example.invalid/\tx", PUBLIC + "\n")
        for url in urls:
            with self.subTest(url=url):
                self.refused(lambda: self.browser.open(url), "refused")
        self.assertEqual(len(self.trace.items), count)

    def test_loaded_timeout_and_failed(self):
        self.browser.start()
        self.assertEqual(self.browser.open(PUBLIC), S.Landing(PUBLIC, "loaded"))

        def stale_events(url):
            self.link.frame(url, loader="new")
            self.link.load(loader="old")
            self.link.load(loader="new", frame="iframe")
            return {"frameId": ROOT_FRAME, "loaderId": "new"}

        self.link.navigation = stale_events
        self.assertEqual(self.browser.open(OTHER), S.Landing(OTHER, "timeout"))
        self.assertNotIn(("Page.stopLoading", None, SESSION), self.trace.items)

        def failed(_url):
            self.link.frame("chrome-error://chromewebdata/", loader="error")
            return {"frameId": ROOT_FRAME, "loaderId": "error", "errorText": "fixture failure"}

        self.link.navigation = failed
        self.assertEqual(self.browser.open(PUBLIC), S.Landing("", "failed"))
        self.link.navigation = lambda _url: {"frameId": ROOT_FRAME, "errorText": "fixture failure"}
        self.link.frame(OTHER)
        self.assertEqual(self.browser.open(PUBLIC), S.Landing("", "failed"))

    def test_same_document_open_has_no_loader(self):
        self.browser.start()
        self.browser.open(PUBLIC)

        def within(url):
            self.link.same_document(url)
            return {"frameId": ROOT_FRAME}

        self.link.navigation = within
        self.assertEqual(self.browser.open(PUBLIC + "#anchor"),
                         S.Landing(PUBLIC + "#anchor", "loaded"))
        self.assertEqual(self.browser.location(), S.Location(PUBLIC + "#anchor", ROOT_FRAME, "loader"))

    def test_open_commit_can_precede_same_document_change_and_navigate_reply(self):
        self.browser.start()

        def committed(url):
            self.link.frame(url, loader="new")
            self.link.same_document(url + "#anchor")
            self.link.load(loader="new")
            return {"frameId": ROOT_FRAME, "loaderId": "new"}

        self.link.navigation = committed
        self.assertEqual(self.browser.open(PUBLIC), S.Landing(PUBLIC + "#anchor", "loaded"))
        self.assertEqual(self.browser.location(), S.Location(PUBLIC + "#anchor", ROOT_FRAME, "new"))

    def test_recheck_age_and_failure(self):
        self.browser.start()
        self.now += 300
        self.browser.open(PUBLIC)
        self.assertEqual(len(self.probe_calls()), 1)
        self.now += 1
        self.gate.passes.add(2)
        self.assertEqual(self.browser.open(PUBLIC).outcome, "loaded")
        self.assertEqual(len(self.probe_calls()), 2)
        visits = self.trace.items.count(("Page.navigate", {"url": PUBLIC}, SESSION))
        self.now += 301
        with patch.multiple(S, _CHECK_TIMEOUT=0.01, _CHECK_RETRY=0):
            self.refused(lambda: self.browser.open(PUBLIC), "gate_bypassed")
        self.assertEqual(len(self.probe_calls()), 5)
        self.assertEqual(self.trace.items.count(("Page.navigate", {"url": PUBLIC}, SESSION)), visits)
        self.assert_closed()

    def test_redirect_blanked_and_pending_open_refused(self):
        self.browser.start()
        for count, landing in enumerate(
                ("http://localhost/", f"https://example.invalid:{self.gate.port}/"), start=1):
            with self.subTest(landing=landing):
                def redirect(_url):
                    self.link.frame(landing, loader="redirect")
                    self.link.load(loader="redirect")
                    return {"frameId": ROOT_FRAME, "loaderId": "redirect"}

                self.link.navigation = redirect
                self.assertEqual(self.browser.open(PUBLIC), S.Landing(BLANK, "refused"))
                self.assertEqual(self.trace.items.count(("Page.navigate", {"url": BLANK}, SESSION)),
                                 count)

    def test_unsolicited_landings_and_same_document_attributed_to_main_frame(self):
        self.browser.start()
        self.browser.open(PUBLIC)
        self.link.frame(FILE, frame="iframe", parentId=ROOT_FRAME)
        self.link.same_document(FILE, frame="iframe")
        self.link.frame(FILE, session="other-session")
        for method in ("Page.frameStartedNavigating", "Page.frameRequestedNavigation",
                       "Page.frameStartedLoading", "Page.frameStoppedLoading"):
            self.link.emit(method, {"frameId": "iframe", "loaderId": "child"})
            self.link.emit(method, {"frameId": ROOT_FRAME, "loaderId": "other"},
                           session="other-session")
        self.assertEqual(self.browser.location(), S.Location(PUBLIC, ROOT_FRAME, "loader"))
        for count, land in enumerate((self.link.frame, self.link.same_document), start=1):
            with self.subTest(event=land.__name__):
                self.link.target_created("popup", BLANK)
                self.link.emit("Page.javascriptDialogOpening", {"message": "fake message"})
                land(FILE)
                self.trace.wait_for(("Page.navigate", {"url": BLANK}, SESSION), count=count)
                self.assertEqual(self.browser.open(OTHER), S.Landing(BLANK, "refused"))
                self.assertNotIn(("Page.navigate", {"url": OTHER}, SESSION), self.trace.items)
                self.refused(self.browser.location, "not_a_site")

    def test_popups_closed_and_dialogs_dismissed(self):
        self.browser.start()
        self.link.target_created("popup", BLANK)
        self.link.target_created("worker", BLANK, kind="service_worker")
        for kind in ("alert", "confirm", "prompt", "beforeunload"):
            self.link.emit("Page.javascriptDialogOpening", {"type": kind, "message": "fake message"})
        self.trace.wait_for(("Page.handleJavaScriptDialog", {"accept": False}, SESSION), count=3)
        self.trace.wait_for(("Page.handleJavaScriptDialog", {"accept": True}, SESSION))
        self.assertEqual(self.link.targets, {"target-1": BLANK, "worker": BLANK})
        self.assertEqual(self.browser._tab.popups_closed, 1)
        self.assertTrue(self.browser.alive)

    def test_unconfirmed_blank_closes_browser(self):
        self.browser.start()
        self.link.blank_navigation = lambda _url: {"frameId": ROOT_FRAME, "loaderId": "blank"}
        with patch.object(S, "_GUARD_TIMEOUT", 0.04):
            self.link.frame(FILE)
            self.refused(lambda: self.browser.open(PUBLIC), "closed")
            self.trace.wait_for(("link.close",))
        self.browser.close()
        self.assert_closed()

    def test_location_asks_each_time_and_requires_a_judged_agreement(self):
        self.browser.start()
        self.link.frame(PUBLIC, urlFragment="#anchor")
        expected = S.Location(PUBLIC + "#anchor", ROOT_FRAME, "loader")
        self.assertEqual(self.browser.location(), expected)
        self.assertEqual(self.browser.location(), expected)
        call = ("Page.getFrameTree", None, SESSION)
        self.assertEqual(self.trace.items.count(call), 3)  # Also queried at startup.
        for field, value in (("url", OTHER), ("id", "other-frame"), ("loaderId", "other-loader"),
                             ("loaderId", "")):
            with self.subTest(field=field, value=value):
                self.link.frame_tree = lambda: {**self.link.main_frame, field: value}
                self.refused(self.browser.location, "unsettled")

        self.link.frame_tree = lambda: self.link.main_frame.copy()
        self.link.frame("https://example.invalid:19110/")
        with patch.object(config, "V3_API_PORT", 19110):
            self.refused(self.browser.location, "not_a_site")

    def test_location_retries_when_commit_arrives_after_reply_comparison(self):
        self.browser.start()
        self.link.frame(PUBLIC, loader="document-a")
        judge = S._judge
        delivered = False

        def judge_and_deliver(url, refused, *, landed=False):
            nonlocal delivered
            if not delivered:
                delivered = True
                self.link.frame(PUBLIC, loader="document-b")
            return judge(url, refused, landed=landed)

        with patch.object(S, "_judge", side_effect=judge_and_deliver):
            self.assertEqual(self.browser.location(), S.Location(PUBLIC, ROOT_FRAME, "document-b"))
        self.assertEqual(self.trace.items.count(("Page.getFrameTree", None, SESSION)), 3)

    def test_main_frame_start_events_make_the_previous_document_ineligible(self):
        self.browser.start()
        self.link.frame(PUBLIC)
        for method in ("Page.frameStartedNavigating", "Page.frameRequestedNavigation",
                       "Page.frameStartedLoading"):
            with self.subTest(method=method):
                self.link.emit(method, {"frameId": ROOT_FRAME, "loaderId": "next"})
                started = time.monotonic()
                self.refused(self.browser.location, "unsettled")
                elapsed = time.monotonic() - started
                self.assertGreaterEqual(elapsed, 0.09)
                self.assertLess(elapsed, 1)
                self.link.frame(PUBLIC, loader="next")
                self.assertEqual(self.browser.location(), S.Location(PUBLIC, ROOT_FRAME, "next"))

    def test_pending_open_requires_its_matching_commit_even_after_open_times_out(self):
        self.browser.start()
        self.link.frame(PUBLIC, loader="old")
        self.link.navigation = lambda _url: {"frameId": ROOT_FRAME, "loaderId": "next"}
        self.assertEqual(self.browser.open(OTHER), S.Landing(PUBLIC, "timeout"))
        self.link.frame(PUBLIC, loader="unrelated")
        self.refused(self.browser.location, "unsettled")
        self.link.frame(OTHER, loader="next")
        self.assertEqual(self.browser.location(), S.Location(OTHER, ROOT_FRAME, "next"))

    def test_stop_can_cancel_the_pending_main_frame_navigation(self):
        self.browser.start()
        self.link.frame(PUBLIC, loader="old")
        self.link.emit("Page.frameStartedNavigating", {"frameId": ROOT_FRAME, "loaderId": "next"})
        # A stop from the previous document does not cancel a new navigation.
        self.link.emit("Page.frameStoppedLoading", {"frameId": ROOT_FRAME})
        self.refused(self.browser.location, "unsettled")
        self.link.emit("Page.frameStartedLoading", {"frameId": ROOT_FRAME})
        self.link.emit("Page.frameStoppedLoading", {"frameId": ROOT_FRAME})
        self.assertEqual(self.browser.location(), S.Location(PUBLIC, ROOT_FRAME, "old"))

    def test_location_unblocks_after_commit_even_if_subresources_are_loading(self):
        self.browser.start()
        self.link.frame(PUBLIC)
        self.link.emit("Page.frameStartedLoading", {"frameId": ROOT_FRAME})

        def settle():
            self.link.frame(OTHER, loader="new")
            self.link.emit("Page.frameStartedLoading", {"frameId": "iframe"})
            self.link.emit("Page.frameStoppedLoading", {"frameId": ROOT_FRAME})

        # The frame-tree call must wait for the main frame and then return
        # even though the iframe never stops. A commit alone also suffices.
        with patch.object(self.browser._tab.changed, "wait", side_effect=lambda _timeout: settle()):
            self.assertEqual(self.browser.location(), S.Location(OTHER, ROOT_FRAME, "new"))
        self.link.emit("Page.frameStartedLoading", {"frameId": ROOT_FRAME})
        self.link.frame(PUBLIC, loader="newer")
        self.assertEqual(self.browser.location(), S.Location(PUBLIC, ROOT_FRAME, "newer"))

    def test_non_sites_include_error_documents_reporting_a_public_address(self):
        self.browser.start()
        for url in (BLANK, "blob:https://example.invalid/fake", "data:text/plain,fake",
                    "chrome-error://chromewebdata/"):
            with self.subTest(url=url):
                self.link.frame(url)
                self.refused(self.browser.location, "not_a_site")

        self.link.history = lambda: {"currentIndex": 0, "entries": [{"url": PUBLIC}]}
        self.link.frame(PUBLIC, loader="error", unreachableUrl=PUBLIC)
        self.refused(self.browser.location, "not_a_site")
        self.link.main_frame.pop("unreachableUrl")  # Still refused by the commit event.
        self.refused(self.browser.location, "not_a_site")
        self.link.frame(PUBLIC, loader="fresh")
        self.link.main_frame["unreachableUrl"] = PUBLIC  # The reply itself is also checked.
        self.refused(self.browser.location, "not_a_site")
        self.assertNotIn(("Page.getNavigationHistory", None, SESSION), self.trace.items)

    def test_is_current_rechecks_loader_address_and_navigation_state(self):
        self.browser.start()
        self.link.frame(PUBLIC, loader="document-a")
        original = self.browser.location()
        self.assertTrue(self.browser.is_current(original))
        self.link.emit("Page.frameStartedNavigating", {"frameId": ROOT_FRAME, "loaderId": "document-b"})
        self.assertFalse(self.browser.is_current(original))
        self.link.frame(PUBLIC, loader="document-b")
        self.assertFalse(self.browser.is_current(original))
        current = self.browser.location()
        self.assertTrue(self.browser.is_current(current))
        self.link.same_document(PUBLIC + "#new")
        self.assertFalse(self.browser.is_current(current))

    def test_document_call_uses_fresh_main_frame_world_and_guard_first(self):
        self.browser.start()
        self.link.frame(PUBLIC)
        self.link.frame(OTHER, frame="iframe", parentId=ROOT_FRAME)
        origin = PUBLIC.rstrip("/")
        for args in (None, ["after"]):
            self.assertEqual(self.browser.document_call(origin, signin.INSPECT, args), "none")
        methods = [entry for entry in self.trace.items if entry[0] in
                   ("Page.createIsolatedWorld", "Runtime.callFunctionOn")]
        self.assertEqual([method for method, _, _ in methods],
                         ["Page.createIsolatedWorld", "Runtime.callFunctionOn"] * 2)
        self.assertNotEqual(methods[0][1]["worldName"], methods[2][1]["worldName"])
        for index, (_, world, session) in enumerate(methods[::2]):
            self.assertEqual(world["frameId"], ROOT_FRAME)
            self.assertEqual(session, SESSION)
            _, call, session = methods[2 * index + 1]
            self.assertEqual(session, SESSION)
            self.assertEqual(call["executionContextId"], 7)
            self.assertIs(call["returnByValue"], True)
            self.assertEqual(call["arguments"], [{"value": origin}] +
                             ([{"value": "after"}] if index else []))
            first_statement = call["functionDeclaration"].splitlines()[1:4]
            self.assertEqual(first_statement, [
                '    if (window.top !== window || location.protocol !== "https:" ||',
                '        location.origin !== origin || self.origin !== origin ||',
                '        document.domain !== location.hostname) return "moved";',
            ])
        self.assertFalse(any(call[0] == "Runtime.enable" for call in self.trace.items))

    def test_document_call_refuses_other_scripts_and_origins_before_world(self):
        self.browser.start()
        self.link.frame(OTHER)
        origin = PUBLIC.rstrip("/")
        for source in (signin.INSPECT.source, signin.Script(signin.INSPECT.source)):
            with self.assertRaisesRegex(TypeError, "built-in sign-in script"):
                self.browser.document_call(origin, source, None)
        self.refused(lambda: self.browser.document_call(origin, signin.INSPECT, None), "moved")
        self.assertFalse(any(call[0] == "Page.createIsolatedWorld" for call in self.trace.items))

    def test_document_call_drops_raw_protocol_errors(self):
        self.browser.start()
        self.link.frame(PUBLIC)

        def destroyed():
            raise LinkError(FILE, browser_message="Cannot find context with specified id")

        def protocol_error():
            raise LinkError(FILE, browser_message=FILE)

        for response, reason in ((destroyed, "moved"), (protocol_error, "unsettled"),
                                 (lambda: {"exceptionDetails": {"text": FILE}}, "unsettled")):
            with self.subTest(reason=reason):
                self.link.document_reply = response
                try:
                    self.browser.document_call(PUBLIC.rstrip("/"), signin.ACT, ["after"])
                except S.BrowserError as error:
                    self.assertEqual(error.reason, reason)
                    self.assertNotIn(FILE, str(error))
                    self.assertIsNone(error.__context__)
                    frame = error.__traceback__.tb_next
                    self.assertIsNone(frame.tb_next)
                    self.assertNotIn("arguments", frame.tb_frame.f_locals)
                else:
                    self.fail("Expected the document call to fail.")

    def test_document_call_with_no_time_left_never_creates_world(self):
        self.browser.start()
        self.link.frame(PUBLIC)
        self.refused(lambda: self.browser.document_call(PUBLIC.rstrip("/"), signin.ACT, None,
                                                       timeout=0), "unsettled")
        self.assertFalse(any(call[0] == "Page.createIsolatedWorld" for call in self.trace.items))

    def test_document_call_does_not_send_arguments_after_a_commit(self):
        self.browser.start()
        self.link.frame(PUBLIC)

        def world_after_commit():
            self.link.frame(PUBLIC, loader="different-document")
            return {"executionContextId": 8}

        self.link.create_world = world_after_commit
        self.refused(lambda: self.browser.document_call(PUBLIC.rstrip("/"), signin.ACT, ["after"]),
                     "moved")
        self.assertFalse(any(call[0] == "Runtime.callFunctionOn" for call in self.trace.items))

    def test_close_after_link_death_is_idempotent(self):
        self.browser.start()
        self.link.alive = False
        self.assertFalse(self.browser.alive)
        self.refused(self.browser.location, "closed")
        self.refused(lambda: self.browser.is_current(S.Location(PUBLIC, ROOT_FRAME, "loader")),
                     "closed")
        self.refused(lambda: self.browser.open(PUBLIC), "closed")
        self.browser.close()
        self.browser.close()
        self.assert_closed()
        self.assertEqual(self.trace.items.count(("gate.stop",)), 1)
        self.assertEqual(self.trace.items.count(("link.close",)), 1)

    def test_main_target_loss_closes_the_link_and_unblocks_an_in_flight_call(self):
        for method, params in (
            ("Target.targetDestroyed", {"targetId": "target-1"}),
            ("Target.targetCrashed", {"targetId": "target-1"}),
            ("Target.detachedFromTarget", {"sessionId": SESSION}),
        ):
            with self.subTest(method=method):
                self.gate.passes.add(len(self.gate.names) + 1)
                self.link.next_target = 0
                self.trace.items.clear()
                self.browser.start()

                def lose_tab(_url):
                    self.link.emit(method, params, session=None)
                    self.assertFalse(self.browser.alive)
                    self.trace.wait_for(("link.close",))
                    raise LinkError(FILE)

                self.link.navigation = lose_tab
                for url in (PUBLIC, OTHER):
                    self.assertEqual(self.refused(lambda: self.browser.open(url), "closed"),
                                     "The browser session is closed.")
                self.browser.close()
                self.assertEqual(self.trace.items.count(("link.close",)), 1)

    def test_main_target_lost_before_setup_records_the_reply(self):
        for command, event, params in (
            ("Target.createTarget", "Target.targetDestroyed", {"targetId": "target-1"}),
            ("Target.createTarget", "Target.targetCrashed", {"targetId": "target-1"}),
            ("Target.attachToTarget", "Target.detachedFromTarget", {"sessionId": SESSION}),
        ):
            with self.subTest(event=event):
                self.link.next_target = 0
                self.trace.items.clear()
                original = self.link.call

                def close_before_reply(method, *args, **kwargs):
                    result = original(method, *args, **kwargs)
                    if method == command:
                        self.link.emit(event, params, session=None)
                    return result

                with patch.object(self.link, "call", side_effect=close_before_reply):
                    self.assertEqual(self.refused(self.browser.start, "closed"),
                                     "The browser session is closed.")
                self.assertEqual(self.gate.names, [])
                self.assert_closed()

    def test_main_target_loss_discards_a_late_document_reply(self):
        self.browser.start()
        self.link.frame(PUBLIC)

        def late_reply():
            self.link.emit("Target.targetDestroyed", {"targetId": "target-1"}, session=None)
            return {"result": {"type": "string", "value": "none"}}

        self.link.document_reply = late_reply
        self.assertEqual(
            self.refused(lambda: self.browser.document_call(PUBLIC.rstrip("/"), signin.INSPECT, None),
                         "closed"),
            "The browser session is closed.",
        )

    def test_death_during_navigation_or_document_call_reports_closed(self):
        def disconnect(*args):
            self.link.alive = False
            raise LinkError(FILE)

        self.gate.passes = {1, 2, 3}
        for operation in ("navigate", "read", "signin"):
            with self.subTest(operation=operation):
                self.browser.start()
                self.assertEqual(self.browser.open(PUBLIC).outcome, "loaded")
                location = self.browser.location()
                if operation == "navigate":
                    self.link.navigation = disconnect
                    call = lambda: self.browser.open(OTHER)
                elif operation == "read":
                    self.link.document_reply = disconnect
                    call = lambda: self.browser.read_call(location)
                else:
                    self.link.document_reply = disconnect
                    call = lambda: self.browser.document_call(PUBLIC.rstrip("/"), signin.ACT, [])
                self.assertEqual(self.refused(call, "closed"), "The browser session is closed.")
                self.assertFalse(self.browser.alive)
                self.link.navigation = self.link.loaded
                self.browser.close()


if __name__ == "__main__":
    unittest.main()
