"""Saved sign-in orchestration and CLI, with no real browser or keyring."""

from collections import deque
from contextlib import redirect_stderr, redirect_stdout
import io
import socket
import subprocess
import unittest
from unittest.mock import Mock, patch

from lg1_login_store_check import MemoryBackend, NAME, ORIGIN, TYPED, USER

from jarvis.v3 import cli
from jarvis.v3.browser import signin
from jarvis.v3.browser.link import LinkError
from jarvis.v3.browser.session import BrowserError, Landing, Location
from jarvis.v3.logins import LoginError, Store


PAGE = ORIGIN + "/"
OTHER = "https://other.example.invalid/"
WRONG_DESTINATION = (
    "The form on that page would have sent its contents to another site or put them "
    "in the address, and the page did nothing else with them. "
    "Jarvis did not sign in."
)


def location(url=PAGE, loader="main"):
    return Location(url, "root", loader)


def refused(reason):
    return BrowserError("Fixture error.", reason=reason)


class FakeSession:
    def __init__(self, trace, *, scripts=(), locations=(), opening=None):
        self.scripts = deque(scripts)
        self.locations = deque(locations)
        self.opening = opening if opening is not None else Landing(PAGE, "loaded")
        self.trace = trace
        self.location_timeouts = []

    def open(self, page):
        self.trace.append(("open", page))
        return self.reply(self.opening)

    def document_call(self, origin, script, arguments):
        self.trace.append(("script", origin, script, arguments))
        expected, response = self.scripts.popleft()
        if script is not expected:
            raise AssertionError("Unexpected sign-in script.")
        if isinstance(response, str) and response not in ("moved", "failed"):
            key = "signature" if script is signin.INSPECT else "before"
            return {"result": response, key: [21, 0]}
        return self.reply(response)

    def location(self, *, timeout=None):
        self.location_timeouts.append(timeout)
        value = self.locations.popleft()
        self.trace.append(("location", value))
        return self.reply(value)

    @staticmethod
    def reply(value):
        if isinstance(value, Exception):
            raise value
        return value


class SignInCheck(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(socket, "socket", side_effect=AssertionError("no network")))
        self.enterContext(patch.object(subprocess, "Popen", side_effect=AssertionError("no browser process")))
        self.backend = MemoryBackend()
        self.store = Store(self.backend)
        self.store.add(NAME, ORIGIN, USER, TYPED, browser="edge")
        self.trace = []
        self.now = 0
        self.enterContext(patch.object(self.store, "secret_for", side_effect=self.read_secret))

    def read_secret(self, name, url):
        self.trace.append(("secret", name, url))
        return Store.secret_for(self.store, name, url)

    def run_flow(self, expected, *, scripts=(), locations=(), opening=None, page=None, timeout=20.0,
                 secret_reads=0, use_current=False):
        session = FakeSession(self.trace, scripts=scripts, locations=locations, opening=opening)
        output = io.StringIO()

        def advance(delay):
            self.assertEqual(delay, 0.3)
            self.now += delay

        with patch.object(signin.time, "sleep", side_effect=advance), redirect_stdout(output), redirect_stderr(output):
            outcome = signin.sign_in(session, self.store, NAME, page=page, clock=lambda: self.now,
                                     step_timeout=timeout, use_current=use_current)
        self.assertEqual(outcome.result, expected)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(session.scripts, deque())
        self.assertEqual(session.locations, deque())
        self.assertTrue(all(budget is None or 0 < budget <= timeout
                            for budget in session.location_timeouts))
        self.assert_no_disclosure(repr(outcome))
        self.assertEqual(len(self.backend.reads), secret_reads)
        calls = [entry for entry in self.trace if entry[0] == "secret"]
        actual_transfers = [(index, entry) for index, entry in enumerate(self.trace)
                            if entry[0] == "script" and TYPED in (entry[3] or [])]
        self.assertEqual(len(calls), secret_reads)
        self.assertEqual(len(actual_transfers), secret_reads)
        if secret_reads:
            index, entry = actual_transfers[0]
            self.assertIs(entry[2], signin.ACT)
            self.assertEqual(self.trace[index - 1], ("secret", NAME, self.trace[index - 2][1].url))
            self.assertEqual(self.trace[index - 2][0], "location")
            self.assertEqual(entry[3].count(TYPED), 1)
        return outcome

    def assert_no_disclosure(self, value):
        for data in (TYPED, USER, ORIGIN):
            self.assertNotIn(data, value)

    def test_one_page_form(self):
        outcome = self.run_flow("signed_in", scripts=[
            (signin.INSPECT, "both"), (signin.ACT, "submitted"), (signin.INSPECT, "none"),
        ], locations=[location()] * 3, secret_reads=1)
        self.assertEqual(outcome.detail, "The site accepted the form.")
        self.assertIn(("script", ORIGIN, signin.ACT, ["both", USER, TYPED]), self.trace)

    def test_current_page_starts_at_inspect_without_navigating(self):
        current = location(ORIGIN + "/sign-in")
        self.run_flow("signed_in", scripts=[
            (signin.INSPECT, "both"), (signin.ACT, "submitted"), (signin.INSPECT, "none"),
        ], locations=[current] * 3, secret_reads=1, use_current=True)
        self.assertEqual(self.trace[0], ("script", ORIGIN, signin.INSPECT, None))
        self.assertFalse(any(entry[0] == "open" for entry in self.trace))

    def test_two_page_flow_waits_through_rewritten_addresses_and_changed_snapshots(self):
        first = location(ORIGIN + "/enter", "next")
        second = location(ORIGIN + "/enter#fragment", "next")
        third = location(ORIGIN + "/check", "next")
        self.run_flow("signed_in", scripts=[
            (signin.INSPECT, "username"), (signin.ACT, "handled"),
            (signin.INSPECT, "password"), (signin.ACT, "submitted"),
        ], locations=[first, refused("changed"), second, third, third, third,
                      location(OTHER, "done"), location(OTHER, "done")], secret_reads=1)
        self.assertIn(("script", ORIGIN, signin.ACT, ["username", USER]), self.trace)
        self.assertIn(("script", ORIGIN, signin.ACT, ["password", "", TYPED]), self.trace)

    def test_password_only(self):
        self.run_flow("signed_in", scripts=[
            (signin.INSPECT, "password"), (signin.ACT, "submitted"), (signin.INSPECT, "none"),
        ], locations=[location()] * 3, secret_reads=1)
        self.assertIn(("script", ORIGIN, signin.ACT, ["password", "", TYPED]), self.trace)

    def test_no_form(self):
        self.run_flow("no_form", scripts=[(signin.INSPECT, "none")])

    def test_wrong_page(self):
        self.run_flow("wrong_site", page=OTHER)
        self.assertEqual(self.trace, [])

    def test_moved_at_inspect(self):
        self.run_flow("moved", scripts=[(signin.INSPECT, "moved")])

    def test_moved_at_act_stops_everything(self):
        self.run_flow("moved", scripts=[(signin.INSPECT, "both"), (signin.ACT, "moved")],
                      locations=[location()], secret_reads=1)

    def test_handled_without_a_page_change_reports_wrong_destination(self):
        outcome = self.run_flow("wrong_destination", scripts=[
            (signin.INSPECT, "both"), (signin.ACT, "handled"), (signin.INSPECT, "both"),
        ], locations=[location()] * 3, secret_reads=1)
        self.assertEqual(outcome.detail, WRONG_DESTINATION)

    def test_handled_with_a_page_change_reports_rejected_after_settling(self):
        outcome = self.run_flow("rejected", scripts=[
            (signin.INSPECT, "both"),
            (signin.ACT, {"result": "handled", "before": [21, 0]}),
            (signin.INSPECT, {"result": "both", "signature": [42, 1]}),
        ], locations=[location()] * 3, secret_reads=1)
        self.assertEqual(tuple(outcome), ("rejected", "The site is still asking for a password."))
        self.assertEqual(self.trace[-3:], [
            ("location", location()), ("location", location()),
            ("script", ORIGIN, signin.INSPECT, ["after"]),
        ])
        self.assertEqual(self.now, 0.3)

    def test_handled_with_only_username_left_in_same_document_reports_failed(self):
        self.run_flow("failed", scripts=[
            (signin.INSPECT, "both"), (signin.ACT, "handled"), (signin.INSPECT, "username"),
        ], locations=[location(), location(PAGE + "#step"), location(PAGE + "#step")],
            secret_reads=1)

    def test_handled_then_navigated_away_reports_signed_in(self):
        self.run_flow("signed_in", scripts=[
            (signin.INSPECT, "both"), (signin.ACT, "handled"),
        ], locations=[location(), location(OTHER, "next"), location(OTHER, "next")], secret_reads=1)

    def test_handled_in_new_document_keeps_the_normal_classification(self):
        self.run_flow("rejected", scripts=[
            (signin.INSPECT, "both"), (signin.ACT, "handled"), (signin.INSPECT, "password"),
        ], locations=[location(), location(loader="next"), location(loader="next")], secret_reads=1)

    def test_different_origin_before_secret_lookup(self):
        self.run_flow("moved", scripts=[(signin.INSPECT, "both")], locations=[location(OTHER)])

    def test_refused_snapshot_before_secret_lookup(self):
        self.run_flow("moved", scripts=[(signin.INSPECT, "both")], locations=[refused("not_a_site")])

    def test_moved_at_settle(self):
        self.run_flow("moved", scripts=[(signin.INSPECT, "username"), (signin.ACT, "submitted")],
                      locations=[refused("moved")])

    def test_unsettled_timeout(self):
        self.run_flow("moved", scripts=[(signin.INSPECT, "username"), (signin.ACT, "submitted")],
                      locations=[refused("unsettled"), location()], timeout=0.5)

    def test_redirect_after_username_does_not_read_secret(self):
        self.run_flow("moved", scripts=[(signin.INSPECT, "username"), (signin.ACT, "submitted")],
                      locations=[location(OTHER)] * 2)

    def test_rejected(self):
        self.run_flow("rejected", scripts=[
            (signin.INSPECT, "both"), (signin.ACT, "submitted"), (signin.INSPECT, "both"),
        ], locations=[location()] * 3, secret_reads=1)

    def test_needs_code_by_field(self):
        self.run_flow("needs_code", scripts=[(signin.INSPECT, "code")])

    def test_needs_code_after_submission_checks_text(self):
        self.run_flow("needs_code", scripts=[
            (signin.INSPECT, "both"), (signin.ACT, "submitted"), (signin.INSPECT, "code"),
        ], locations=[location()] * 3, secret_reads=1)
        self.assertEqual(self.trace[-1], ("script", ORIGIN, signin.INSPECT, ["after"]))

    def test_step_limit(self):
        username_step = [(signin.INSPECT, "username"), (signin.ACT, "submitted")]
        self.run_flow("failed", scripts=[*username_step * 4, (signin.INSPECT, "both")],
                      locations=[location()] * 8)

    def test_open_refused(self):
        self.run_flow("failed", opening=refused("refused"))

    def test_open_failed(self):
        self.run_flow("failed", opening=Landing("", "failed"))


class CliCheck(unittest.TestCase):
    def setUp(self):
        self.store = Store(MemoryBackend())
        self.store.add(NAME, ORIGIN, USER, TYPED, browser="edge")
        self.session = Mock()
        self.store_factory = self.enterContext(patch.object(cli, "_login_store", return_value=self.store))
        self.session_factory = self.enterContext(patch.object(cli, "_login_session", return_value=self.session))
        self.sign_in = self.enterContext(patch.object(signin, "sign_in"))
        self.tty = self.enterContext(patch.object(cli.sys.stdin, "isatty", return_value=False))
        self.wait = self.enterContext(patch("builtins.input"))

    def invoke(self, *flags):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = cli.main(["login", "test", NAME, *flags])
        for data in (TYPED, USER, ORIGIN):
            self.assertNotIn(data, out.getvalue() + err.getvalue())
        return result, out.getvalue(), err.getvalue()

    def test_outcomes_exit_codes_and_lines(self):
        for result, detail in signin._DETAILS.items():
            with self.subTest(result=result):
                self.session.reset_mock()
                self.sign_in.return_value = signin.Outcome(result, detail)
                self.assertEqual(self.invoke("--page", PAGE), (
                    {"signed_in": 0, "needs_code": 2}.get(result, 1), f"{NAME}: {detail}\n", "",
                ))
                self.store_factory.assert_called_with()
                self.session_factory.assert_called_with("edge", window=True)
                self.sign_in.assert_called_with(self.session, self.store, NAME, page=PAGE)
                self.session.start.assert_called_once()
                self.session.close.assert_called_once()
        self.wait.assert_not_called()

    def test_wrong_destination_prints_fixed_line_and_exits_one(self):
        self.sign_in.return_value = signin._outcome("wrong_destination")
        self.assertEqual(self.invoke(), (1, f"{NAME}: {WRONG_DESTINATION}\n", ""))
        self.session.close.assert_called_once()

    def test_window_waits_only_when_terminal_and_after_print_before_close(self):
        self.sign_in.return_value = signin.Outcome("needs_code", "The site is asking for a verification code.")

        def wait(prompt=""):
            self.assertIn(self.sign_in.return_value.detail, cli.sys.stdout.getvalue())
            self.session.close.assert_not_called()

        self.wait.side_effect = wait
        self.invoke("--window")
        self.session_factory.assert_called_with("edge", window=True)
        self.wait.assert_not_called()
        self.session.close.assert_called_once()
        self.session.reset_mock()
        self.tty.return_value = True
        self.invoke()
        self.wait.assert_called_once_with("Press Enter to close the browser. ")
        self.session.close.assert_called_once()

    def test_hidden_suppresses_window_and_terminal_prompt(self):
        self.sign_in.return_value = signin._outcome("signed_in")
        self.tty.return_value = True
        for flags in (("--hidden",), ("--window", "--hidden")):
            with self.subTest(flags=flags):
                self.assertEqual(self.invoke(*flags)[0], 0)
                self.session_factory.assert_called_with("edge", window=False)
        self.wait.assert_not_called()

    def test_wrong_site_does_not_start_browser(self):
        self.sign_in.side_effect = lambda *a, **kw: real_sign_in(*a, **kw)
        self.assertEqual(self.invoke("--page", OTHER)[0], 1)
        self.session.start.assert_not_called()
        self.session.open.assert_not_called()
        self.session.close.assert_called_once()

    def test_errors_use_fixed_text_and_close(self):
        for operation, error, detail in (
            (self.session.start, BrowserError(TYPED, reason="closed"), "The browser session could not be used."),
            (self.sign_in, LinkError(USER), "The browser session could not be used."),
            (self.sign_in, LoginError(ORIGIN), "The saved login could not be loaded."),
        ):
            with self.subTest(kind=type(error)):
                self.session.reset_mock()
                operation.side_effect = error
                self.assertEqual(self.invoke(), (1, "", f"Error: {detail}\n"))
                self.session.close.assert_called_once()
                operation.side_effect = None

    def test_store_factory_error_never_constructs_session(self):
        self.store_factory.side_effect = LoginError(TYPED)
        self.assertEqual(self.invoke(), (1, "", "Error: The saved login could not be loaded.\n"))
        self.session_factory.assert_not_called()

    def test_close_error_uses_fixed_text(self):
        self.sign_in.return_value = signin.Outcome("signed_in", "The site accepted the form.")
        self.session.close.side_effect = LinkError(TYPED)
        self.assertEqual(self.invoke(), (1, "", "Error: The browser session could not be used.\n"))
        self.session.close.assert_called_once()


real_sign_in = signin.sign_in


if __name__ == "__main__":
    unittest.main()
