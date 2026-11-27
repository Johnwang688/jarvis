"""Desk and protocol checks using the existing fake session and login store."""

import unittest
from unittest.mock import call, patch

import bt2_read_check
import bt3_desk_check as fixtures
from bt3_desk_check import CONTEXT, LINK, PAGE
from jarvis.v3.browser import act, desk
from jarvis.v3.browser.link import LinkError
from jarvis.v3.browser.session import BrowserError


TYPED = "Write this exact text."


def description(kind="control", **changes):
    return {"kind": kind, "form_has_password": False, "payment": False,
            "destination_origin": "", "method": "", "sensitive": False,
            "textarea": False, "can_submit": False, "binding": 1,
            "button_like": kind in ("control", "submit"), "target_number": 1, **changes}


class ActionDesk(unittest.TestCase):
    setUp = fixtures.DeskCheck.setUp
    tearDown = fixtures.DeskCheck.tearDown
    assert_page = fixtures.DeskCheck.assert_page

    def set_kind(self, *args, **kwargs):
        view = description(*args, **kwargs)
        self.session.act_call.side_effect = lambda _, script, args: view if script is act.DESCRIBE else "done"

    def test_no_read_and_fixed_errors(self):
        self.assertEqual(self.desk.describe(1), desk._ACT_READ_FIRST)
        self.assertEqual(self.desk.click(1), desk._ACT_READ_FIRST)
        self.assertEqual(self.desk.type(1, TYPED), desk._ACT_READ_FIRST)
        self.assertEqual(self.service.leases, [])
        self.desk.read()
        self.assertEqual(self.desk.describe(True), {"kind": "unusable"})
        self.session.act_call.return_value = {"invalid": PAGE}
        self.assertEqual(self.desk.describe(1), desk._ERROR)
        self.session.act_call.side_effect = BrowserError(PAGE, reason="moved")
        self.assertEqual(self.desk.describe(1), desk._MOVED)
        self.assertEqual(self.desk.describe(1), desk._ACT_READ_FIRST)

    def test_control_and_type_scope(self):
        for kind in ("control", "toggle", "submit", "field"):
            with self.subTest(kind=kind):
                self.set_kind(kind, destination_origin="https://other.example.invalid", can_submit=True)
                self.session.reset_mock()
                self.desk.read()
                target = self.desk.describe(1)
                self.assertEqual(target["label"], "button label")
                self.assertEqual(target["kind"], kind)
                self.assertFalse(self.service.held)
                with patch.object(desk, "settle") as settle:
                    if kind == "field":
                        self.assert_page(self.desk.type(1, TYPED, True, expected=target))
                        acting = call(CONTEXT, act.TYPE, [1, 1, TYPED, True])
                    else:
                        self.assert_page(self.desk.click(1, expected=target))
                        acting = call(CONTEXT, act.CLICK, [1, 1])
                    settle.assert_called_once()
                self.assertEqual(self.session.act_call.call_args_list, [
                    call(CONTEXT, act.DESCRIBE, [1]), call(CONTEXT, act.DESCRIBE, [1]), acting,
                ])
                self.assertEqual(self.session.read_call.call_count, 2)

    def test_every_bound_change_refuses_before_acting(self):
        for key, value in (("kind", "toggle"), ("binding", 2), ("payment", True),
                           ("destination_origin", "https://other.example.invalid"),
                           ("method", "post"), ("sensitive", True), ("form_has_password", True)):
            self.set_kind()
            self.desk.read()
            target = self.desk.describe(1)
            self.session.reset_mock()
            self.set_kind(**{key: value})
            self.assertEqual(self.desk.click(1, expected=target), desk._CHANGED)
            self.session.act_call.assert_called_once_with(CONTEXT, act.DESCRIBE, [1])
        self.set_kind()
        target = self.desk.describe(1)
        self.assertEqual(self.desk.click(2, expected=target), desk._CHANGED)
        self.desk.read()
        self.assertEqual(self.desk.click(1, expected=target), desk._CHANGED)
        target = self.desk.describe(1)
        self.session.is_current.return_value = False
        self.assertEqual(self.desk.click(1, expected=target), desk._CHANGED)
        self.assertEqual(self.desk.describe(1), desk._ACT_READ_FIRST)

    def test_validation_precedes_action(self):
        self.set_kind("field")
        self.desk.read()
        for text, error in (
            ("", desk._TEXT), ("x" * 2001, desk._TEXT), ("tab\t", desk._TEXT), ("nul\0", desk._TEXT),
            ("control\x85", desk._TEXT), ("line\nnext", desk._NEWLINE),
            ("pass" + "word: 9-example-value!", desk._CREDENTIAL),
        ):
            with self.subTest(error=error):
                self.session.reset_mock()
                self.assertEqual(self.desk.type(1, text), error)
                self.session.act_call.assert_called_once_with(CONTEXT, act.DESCRIBE, [1])
        self.assertEqual(self.desk.type(1, TYPED, True), desk._NO_SUBMIT)
        self.set_kind("field", sensitive=True)
        self.assertEqual(self.desk.type(1, TYPED), desk._SENSITIVE)
        self.set_kind("field", can_submit=True, form_has_password=True)
        self.assertEqual(self.desk.type(1, TYPED, True), desk._SIGN_IN)
        self.set_kind("submit", form_has_password=True)
        self.assertEqual(self.desk.click(1), desk._SIGN_IN)

    def test_described_link_reuses_follow(self):
        self.desk.read()
        self.set_kind("link", target_number=2)
        target = self.desk.describe(2)
        link = self.desk.resolve_follow(2)
        self.assert_page(self.desk.click(2, expected=target, link=link))
        self.session.open.assert_called_once_with(LINK)

    def test_action_errors_clear_read(self):
        for outcome, expected in (("changed", desk._CHANGED), ("moved", desk._CHANGED),
                                  ("invalid_text", desk._INVALID_TEXT), ("anything", desk._ERROR)):
            self.desk.read()
            self.session.act_call.side_effect = [description(), description(), outcome]
            target = self.desk.describe(1)
            self.assertEqual(self.desk.click(1, expected=target), expected)
            self.assertEqual(self.desk.describe(1), desk._ACT_READ_FIRST)


class ActProtocol(unittest.TestCase):
    setUp = bt2_read_check.ReadCallCheck.setUp
    world_calls = bt2_read_check.ReadCallCheck.world_calls

    def test_identity_read_context_and_context_loss(self):
        _, context = self.browser.read_call(bt2_read_check.LOCATION, with_context=True)
        with self.assertRaises(TypeError):
            self.browser.act_call(context, act.Script(act.DESCRIBE.source), [1])
        with self.assertRaisesRegex(BrowserError, "moved"):
            self.browser.act_call(context + 1, act.DESCRIBE, [1])
        self.link.document_reply = lambda: {"result": {"value": description()}}
        self.assertEqual(self.browser.act_call(context, act.DESCRIBE, [1]), description())
        calls = self.world_calls()
        self.assertEqual([item[0] for item in calls],
                         ["Page.createIsolatedWorld", "Runtime.callFunctionOn", "Runtime.callFunctionOn"])
        self.assertEqual(calls[-1][1]["arguments"], [*calls[-2][1]["arguments"], {"value": 1}])

        def gone():
            raise LinkError(PAGE, browser_message="Execution context was destroyed.")

        self.link.document_reply = gone
        with self.assertRaises(BrowserError) as caught:
            self.browser.act_call(context, act.CLICK, [1, 1])
        self.assertEqual(caught.exception.reason, "moved")


    def test_act_consumes_the_context_even_when_the_document_stays(self):
        _, context = self.browser.read_call(bt2_read_check.LOCATION, with_context=True)
        self.link.document_reply = lambda: {"result": {"value": "done"}}
        self.assertEqual(self.browser.act_call(context, act.CLICK, [1, 1]), "done")
        with self.assertRaisesRegex(BrowserError, "moved"):
            self.browser.act_call(context, act.CLICK, [1, 1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
