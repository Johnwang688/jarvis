"""BT3 browser desk and follow transport checks with an in-memory login store."""

from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, call, patch

import bt2_read_check
from lg1_login_store_check import MemoryBackend, NAME, ORIGIN, TYPED, USER

from jarvis.v3.browser import desk, read, signin
from jarvis.v3.browser.link import LinkError
from jarvis.v3.browser.service import BrowserBusy, BrowserUnavailable
from jarvis.v3.browser.session import BrowserError, BrowserSession, Landing, Location
from jarvis.v3.feed import Feed, FeedError
from jarvis.v3.logins import LoginError, Store


PAGE = "https://example.invalid/private-path?hidden=query#section"
LINK = "http://other.example.invalid/somewhere"
LOCATION = Location(PAGE, "frame", "loader")
CONTEXT = 17
ITEMS = [{"kind": "button", "label": "button label", "labelLines": []},
         {"kind": "link", "label": "link label", "labelLines": []}]


class Service:
    def __init__(self, session):
        self.session = session
        self.leases = []
        self.held = False
        self.error = None

    @contextmanager
    def use(self, browser):
        if self.error is not None:
            raise self.error
        assert not self.held
        self.held = True
        self.leases.append(browser)
        try:
            yield self.session
        finally:
            self.held = False


class DeskCheck(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home)}))
        self.enterContext(patch("jarvis.tools.secrets.secret_values", return_value=[]))
        self.enterContext(patch("jarvis.tools.secrets._process_env_values", return_value=[]))
        self.session = Mock(spec=BrowserSession)
        self.session.location.return_value = LOCATION
        self.session.is_current.return_value = True
        self.session.open.return_value = Landing(PAGE, "loaded")
        self.session.read_call.return_value = (dict(title="page\u200b title", text="body line",
                                                   items=ITEMS, more=True), CONTEXT)
        self.session.follow_call.return_value = LINK
        self.service = Service(self.session)
        self.backend = MemoryBackend()
        self.store = Store(self.backend)
        self.login = self.store.add(NAME, ORIGIN + ":8443", USER, TYPED, browser="edge")
        self.factory = Mock(return_value=self.store)
        self.feed = Mock()
        self.now = 600
        self.desk = desk.BrowserDesk(self.service, store_factory=self.factory,
                                     feed_post=self.feed, clock=lambda: self.now)

    def tearDown(self):
        self.assertFalse(self.service.held)

    def assert_page(self, result, *, cut=True):
        header, fence, *body, end = result.splitlines()
        self.assertEqual(header, "Site: https://example.invalid. " +
                         ("Text was cut." if cut else "Text was not cut."))
        self.assertIn("https://example.invalid", fence)
        self.assertTrue(end.startswith("[end of web content "), end)
        self.assertEqual(body, ["page title", "[1] button: button label", "[2] link: link label", "", "body line"])
        for hidden in (PAGE, LINK, "private-path", "hidden=query"):
            self.assertNotIn(hidden, result)

    def test_availability_checks_only_the_installation(self):
        with patch.object(desk, "bridge_available", side_effect=[False, True]):
            self.assertFalse(self.desk.available())
            self.assertTrue(self.desk.available())
        self.assertEqual(self.service.leases, [])
        self.factory.assert_not_called()

    def test_open_and_read_fence_the_complete_cleaned_page_with_fresh_nonces(self):
        opened = self.desk.open(PAGE)
        result, _ = self.session.read_call.return_value
        self.session.read_call.return_value = (dict(result, more=False), CONTEXT)
        reread = self.desk.read()
        self.assert_page(opened)
        self.assert_page(reread, cut=False)
        self.assertNotEqual(opened.splitlines()[1], reread.splitlines()[1])
        self.session.open.assert_called_once_with(PAGE)
        self.assertEqual(self.session.read_call.call_args_list, [call(LOCATION, with_context=True)] * 2)
        self.assertEqual(self.service.leases, ["chrome", "chrome"])

    def test_open_failures_clear_the_last_read(self):
        for outcome, expected in (("failed", desk._FAILED), ("timeout", desk._FAILED),
                                  ("refused", desk._REFUSED)):
            with self.subTest(outcome=outcome):
                self.desk.read()
                self.session.open.return_value = Landing(PAGE, outcome)
                self.session.read_call.reset_mock()
                self.assertEqual(self.desk.open(PAGE), expected)
                self.assertIn("Read the current page", self.desk.follow(2))
                self.session.read_call.assert_not_called()

    def test_follow_rejects_bad_numbers_stale_reads_movement_and_non_http(self):
        self.assertIn("Read the current page", self.desk.follow(2))
        self.desk.read()
        for number in (0, -1, 1, 3, True, "2"):
            self.assertEqual(self.desk.follow(number), desk._NOT_LINK)
        self.session.follow_call.assert_not_called()
        self.session.is_current.return_value = False
        self.assertEqual(self.desk.follow(2), desk._MOVED)
        self.session.follow_call.assert_not_called()
        self.session.is_current.return_value = True
        self.desk.read()
        for href in (None, "javascript:void(0)", "data:text/plain,fixture"):
            self.session.follow_call.return_value = href
            self.assertEqual(self.desk.follow(2), desk._NOT_LINK)
        self.session.follow_call.return_value = "moved"
        self.assertEqual(self.desk.follow(2), desk._MOVED)
        self.assertIn("Read the current page", self.desk.follow(2))
        self.session.open.assert_not_called()

    def test_follow_without_a_read_takes_no_lease(self):
        # Nothing was read: neither resolving nor following may start a browser.
        expected = "Refused: Read the current page before following a link."
        self.assertEqual(self.desk.resolve_follow(1), expected)
        self.assertEqual(self.desk.follow(1), expected)
        self.assertEqual(self.service.leases, [])

    def test_follow_uses_the_last_world_routes_navigation_through_open_and_reads(self):
        self.desk.read()
        result, _ = self.session.read_call.return_value
        self.session.read_call.return_value = (result, CONTEXT + 1)
        self.desk.read()
        self.assert_page(self.desk.follow(2))
        self.session.follow_call.assert_called_once_with(CONTEXT + 1, 2)
        self.session.open.assert_called_once_with(LINK)
        self.assertEqual(self.session.read_call.call_count, 3)
        self.assertEqual(self.service.leases, ["chrome"] * 3)

    def test_fixed_errors_release_leases_and_do_not_render_exception_text(self):
        for error, message in (
            (BrowserBusy(PAGE), "Error: The browser is in use."),
            (BrowserUnavailable(PAGE), "Error: The browser is unavailable."),
        ):
            self.service.error = error
            self.assertEqual(self.desk.open(PAGE), message)
        self.service.error = None
        for error, message in (
            (BrowserError(PAGE, reason="refused"), desk._REFUSED),
            (BrowserError(PAGE, reason="moved"), desk._MOVED),
            (BrowserError(PAGE, reason="not_a_site"), "Refused: The browser is not showing a public site."),
            (BrowserError(PAGE, reason="unsettled"), "Error: The browser could not complete the request."),
            (LinkError(PAGE, browser_message=TYPED), "Error: The browser connection could not complete the request."),
        ):
            self.session.location.side_effect = error
            self.assertEqual(self.desk.read(), message)
            self.assertFalse(self.service.held)
        self.session.location.side_effect = RuntimeError(PAGE)
        with self.assertLogs(desk.LOG, level="WARNING") as logs:
            self.assertEqual(self.desk.read(), "Error: The browser could not complete the request.")
        self.assertEqual(logs.output, ["WARNING:jarvis.v3.browser.desk:Unexpected browser desk error."])
        self.assertFalse(self.service.held)

    def test_sign_in_uses_saved_browser_and_default_page_and_posts_exact_notice(self):
        self.desk.read()
        self.session.document_call.side_effect = [
            {"result": "both"}, {"result": "submitted"}, {"result": "none"},
        ]
        landing = Location(self.login.origin + "/", "frame", "login")
        self.session.location.side_effect = [LOCATION, landing, landing]
        with patch.object(signin, "_settle", return_value=landing):
            self.assertEqual(self.desk.sign_in(NAME), signin._DETAILS["signed_in"])
        self.session.open.assert_called_once_with(self.login.origin + "/")
        self.assertEqual(self.service.leases, ["chrome", "edge"])
        self.assertEqual(len(self.backend.reads), 1)
        self.feed.assert_called_once_with(
            actor="jarvis", kind="news", rank=4, ask="Jarvis used the saved login for accounts.example.invalid.",
            idempotency_key=sha256(f"browser-sign-in:{NAME}:10".encode()).hexdigest(),
        )
        self.assertIn("Read the current page", self.desk.follow(2))

    def test_sign_in_uses_current_page_and_reports_cross_origin_landing(self):
        current = Location(self.login.origin + "/sign-in", "frame", "login")
        landing = Location("https://other.invalid/done", "frame", "done")
        self.session.location.side_effect = [current, current, landing]
        self.session.document_call.side_effect = [{"result": "both"}, {"result": "submitted"}]
        with patch.object(signin, "_settle", return_value=landing):
            self.assertEqual(
                self.desk.sign_in(NAME),
                "The site accepted the form and moved to another site. Jarvis cannot confirm the sign-in.",
            )
        self.session.open.assert_not_called()
        self.assertEqual(self.session.document_call.call_args_list[0], call(self.login.origin, signin.INSPECT, None))
        self.assertEqual(len(self.backend.reads), 1)
        self.assertEqual(self.feed.call_args.kwargs["ask"],
                         "Jarvis used the saved login for accounts.example.invalid.")

    def test_sign_in_falls_back_to_root_when_current_page_is_unsettled(self):
        self.session.location.side_effect = BrowserError(PAGE, reason="unsettled")
        self.session.document_call.return_value = {"result": "none"}
        self.assertEqual(self.desk.sign_in(NAME), signin._DETAILS["no_form"])
        self.session.open.assert_called_once_with(self.login.origin + "/")

    def test_sign_in_keys_deduplicate_by_name_and_minute(self):
        with patch.object(desk, "sign_in", return_value=signin.Outcome("signed_in", PAGE)):
            self.desk.sign_in(NAME)
            self.now += 59
            self.desk.sign_in(NAME)
            self.now += 1
            self.desk.sign_in(NAME)
            another = "another"
            self.store.add(another, ORIGIN, USER, TYPED)
            self.desk.sign_in(another)
        keys = [entry.kwargs["idempotency_key"] for entry in self.feed.call_args_list]
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(len(set(keys)), 3)

    def test_sign_in_outcomes_and_errors_never_echo_details_or_post(self):
        self.assertIn("Use a saved login name", self.desk.sign_in("../example"))
        self.factory.assert_not_called()
        self.assertEqual(self.desk.sign_in("unknown"), "Error: The saved login could not be used.")
        self.assertEqual(self.service.leases, [])
        for result, sentence in signin._DETAILS.items():
            if result == "signed_in":
                continue
            self.desk.read()
            with patch.object(desk, "sign_in", return_value=signin.Outcome(result, TYPED)):
                expected = desk._NEEDS_CODE if result == "needs_code" else sentence
                self.assertEqual(self.desk.sign_in(NAME), expected)
            self.assertIn("Read the current page", self.desk.follow(2))
        self.factory.side_effect = LoginError(PAGE)
        self.assertEqual(self.desk.sign_in(NAME), "Error: The saved login could not be used.")
        self.feed.assert_not_called()

    def test_login_replacement_cannot_use_the_old_browser_or_report_the_old_site(self):
        changed = replace(self.login, origin=ORIGIN, browser="chrome")
        with patch.object(self.store, "list", side_effect=[[self.login], [changed]]):
            self.assertEqual(self.desk.sign_in(NAME), "Error: The saved login could not be used.")
        self.session.open.assert_not_called()
        self.feed.assert_not_called()

    def test_feed_failure_returns_a_fixed_result_after_releasing_the_lease(self):
        self.feed.side_effect = FeedError(PAGE)
        with patch.object(desk, "sign_in", return_value=signin.Outcome("signed_in", TYPED)):
            self.assertEqual(self.desk.sign_in(NAME),
                             "Error: The site accepted the form, but Jarvis could not write the feed notice.")

    def test_long_saved_hostname_can_be_written_and_replayed_by_the_real_feed(self):
        host = ".".join(["a" * 63] * 3 + ["b" * 45, "example", "invalid"])
        self.store.add("long-site", "https://" + host, USER, TYPED)
        root = self.home / "feed"
        feed = Feed(root)
        browser = desk.BrowserDesk(self.service, store_factory=self.factory,
                                   feed_post=lambda *, actor, **fields: feed.post(fields, actor=actor))
        self.session.location.return_value = Location("https://" + host, "frame", "login")
        with patch.object(desk, "sign_in", return_value=signin.Outcome("signed_in", TYPED)):
            self.assertEqual(browser.sign_in("long-site"), signin._DETAILS["signed_in"])
        rows = Feed(root).briefs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ask"], "Jarvis used the saved login for " + host + ".")
        with self.assertRaises(FeedError):
            feed.post({"kind": "news", "rank": 4, "ask": rows[0]["ask"]}, actor="agent", session="example")


class FollowProtocolCheck(unittest.TestCase):
    setUp = bt2_read_check.ReadCallCheck.setUp
    world_calls = bt2_read_check.ReadCallCheck.world_calls

    def test_only_last_read_context_can_follow_and_no_new_world_is_created(self):
        snapshot = bt2_read_check.LOCATION
        self.link.document_reply = lambda: {"result": {"value": {}}}
        _, context = self.browser.read_call(snapshot, with_context=True)
        with self.assertRaises(BrowserError) as caught:
            self.browser.follow_call(context + 1, 1)
        self.assertEqual(caught.exception.reason, "moved")
        with self.assertRaises(TypeError):
            self.browser.read_call(snapshot, read.FOLLOW)
        self.link.document_reply = lambda: {"result": {"value": LINK}}
        self.assertEqual(self.browser.follow_call(context, 4), LINK)
        calls = self.world_calls()
        self.assertEqual([item[0] for item in calls],
                         ["Page.createIsolatedWorld", "Runtime.callFunctionOn", "Runtime.callFunctionOn"])
        self.assertEqual(calls[-1][1]["executionContextId"], calls[-2][1]["executionContextId"])
        read_args = calls[-2][1]["arguments"]
        self.assertEqual(read_args[0], {"value": bt2_read_check.ORIGIN})
        self.assertRegex(read_args[1]["value"], r"^[a-f0-9]{32}$")
        self.assertEqual(calls[-1][1]["arguments"], [*read_args, {"value": 4}])
        self.assertEqual(calls[-1][1]["functionDeclaration"], read.document_function(read.FOLLOW))

    def test_destroyed_context_fails_closed(self):
        snapshot = bt2_read_check.LOCATION
        _, context = self.browser.read_call(snapshot, with_context=True)

        def gone():
            raise LinkError(PAGE, browser_message="Execution context was destroyed.")

        self.link.document_reply = gone
        with self.assertRaises(BrowserError) as caught:
            self.browser.follow_call(context, 1)
        self.assertEqual(caught.exception.reason, "moved")
        self.assertNotIn(PAGE, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
