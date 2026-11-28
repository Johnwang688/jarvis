"""Regression tests from the independent review of click, scroll and window switching."""
import ast
from dataclasses import replace
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from jarvis.desktop import DesktopError, DesktopSession
from jarvis.v3.computer import ComputerActionLog, ComputerDrive, ComputerError
from tests.v3 import computer_bridge_check as bridge_check
from tests.v3 import computer_cli_check
from tests.v3.computer_act_check import A, B, C, SHOT, FakeDesktop, row
from windows.desktopguard import Refused


class StopBeforeWireCheck(unittest.TestCase):
    def session(self):
        session = DesktopSession(port=0)
        session._server = object()
        session._paired = True
        return session

    def test_stop_before_transport_write_prevents_input(self):
        session = self.session()
        trace, records = [], []
        drive = ComputerDrive(session, enabled=lambda: True, log=records.append)
        code = drive.start()
        result = {
            "windows": {"windows": [row(A, front=True)]},
            "window_screenshot": SHOT,
            "window_probe": {"name": "Example", "role": "button"},
            "window_click": {"clicked": True},
        }

        def write(data):
            request = json.loads(data)
            trace.append(request["op"])
            stream.readline.return_value = json.dumps({
                "id": request["id"], "ok": True, "result": result[request["op"]],
            }).encode()

        def settimeout(_):
            if trace[-1:] == ["window_probe"]:
                self.assertTrue(drive.stop())
                trace.append("stop returned")

        stream = Mock()
        stream.write.side_effect = write
        session._stream = stream
        session._conn = SimpleNamespace(settimeout=settimeout)
        drive.screenshot(code)
        with self.assertRaises(ComputerError):
            drive.click(code, 1, 1)

        self.assertIn("stop returned", trace)
        self.assertNotIn("window_click", trace)
        self.assertEqual([record["state"] for record in records], ["start", "failed"])

    def test_the_last_check_and_the_write_hold_the_send_gate(self):
        session = self.session()
        held = []
        stream = Mock()
        stream.write.side_effect = lambda data: held.append(("write", session.send_gate.locked()))
        stream.readline.return_value = json.dumps({"id": 1, "ok": True}).encode()
        session._stream, session._conn = stream, Mock()

        def current():
            held.append(("check", session.send_gate.locked()))
            return True

        session.request("ping", current=current)
        self.assertEqual(held, [("check", True), ("write", True)])
        self.assertFalse(session.send_gate.locked())

    def test_stop_and_start_take_the_send_gate_and_do_not_wait_long_for_a_stuck_send(self):
        gate = Mock()
        gate.acquire.return_value = True
        drive = ComputerDrive(SimpleNamespace(connected=True, send_gate=gate), enabled=lambda: True)
        drive.start()
        drive.stop()
        self.assertEqual(gate.acquire.call_args_list, [unittest.mock.call(timeout=2)] * 2)
        self.assertEqual(gate.release.call_count, 2)
        # A send that never finishes: the grant is revoked all the same, and the gate is not released by us.
        gate.reset_mock()
        gate.acquire.return_value = False
        code = drive.start()
        self.assertTrue(drive.stop())
        with self.assertRaises(ComputerError):
            drive.screenshot(code)
        gate.release.assert_not_called()


class CheckingDesktop(FakeDesktop):
    """Like the real session, it asks `current()` right before sending."""

    def request(self, op, timeout=90.0, *, require_pair=False, current=None, **fields):
        self.current = current
        if current is not None and not current():
            raise DesktopError("The owner chat turn is no longer current.")
        return super().request(op, timeout, require_pair=require_pair, current=current, **fields)


class StaleSelectionCheck(unittest.TestCase):
    def test_replaced_screenshot_or_list_cancels_pending_action(self):
        for action, look, wire_op in (("click", "screenshot", "window_click"),
                                      ("scroll", "screenshot", "window_scroll"),
                                      ("front", "windows", "window_front")):
            with self.subTest(action=action):
                desktop = CheckingDesktop([row(A, front=True), row(B)])
                refreshed, records = [], []

                def log(record):
                    records.append(record)
                    if record["state"] == "start":
                        desktop.windows = [row(B, front=True), row(A)]
                        refreshed.append(getattr(drive, look)(code))

                drive = ComputerDrive(desktop, enabled=lambda: True, log=log)
                code = drive.start()
                getattr(drive, look)(code)
                args = {"click": (1, 1), "scroll": (1, 1, -2), "front": (1,)}[action]
                with self.assertRaises(ComputerError) as caught:
                    getattr(drive, action)(code, *args)
                self.assertIn("Look again", str(caught.exception))
                self.assertTrue(refreshed)
                self.assertNotIn(wire_op, [op for op, _ in desktop.calls])
                self.assertEqual([record["state"] for record in records], ["start", "failed"])

    def test_an_input_call_carries_a_live_check_that_ends_with_the_grant(self):
        desktop = CheckingDesktop([row(A, front=True)])
        drive = ComputerDrive(desktop, enabled=lambda: True, log=lambda record: None)
        code = drive.start()
        drive.screenshot(code)
        drive.click(code, 1, 1)
        self.assertEqual(desktop.calls[-1][0], "window_click")
        self.assertIs(desktop.current(), True)
        drive.stop()
        self.assertIs(desktop.current(), False)


class RecordsCheck(unittest.TestCase):
    def test_no_record_or_reply_carries_a_bridge_window_id_or_the_code(self):
        desktop = FakeDesktop([row(A), row(B, front=True, app="chrome.exe", title="Docs"), row(C)])
        records = []
        drive = ComputerDrive(desktop, enabled=lambda: True, log=records.append)
        code = drive.start()
        replies = [drive.windows(code), drive.front(code, 1)]
        shot = drive.screenshot(code)
        replies += [{key: value for key, value in shot.items() if key != "image_b64"},
                    drive.click(code, 1, 1), drive.scroll(code, 1, 1, 3)]
        for op in ("window_click", "window_front"):
            desktop.fail = {op: "Refused: The window moved."}
            with self.assertRaises(ComputerError):
                drive.click(code, 1, 1) if op == "window_click" else drive.front(code, 1)
        text = json.dumps([records, replies])
        self.assertEqual(len(records), 10)
        for secret in (A, B, C, code):
            self.assertNotIn(secret, text)
        self.assertEqual([record["detail"] for record in records if record["state"] == "done"], ["", "", ""])


class BridgeFrontCheck(unittest.TestCase):
    def fixture(self):
        fixture = bridge_check.ComputerBridgeCheck()
        fixture.setUp()
        return fixture

    def test_target_that_becomes_protected_is_never_raised(self):
        fixture = self.fixture()

        def idle():
            fixture.desktop.info[10] = replace(fixture.desktop.info[10], elevated=True)

        with patch.object(fixture.desktop, "require_idle_input", side_effect=idle):
            with self.assertRaises(Refused):
                fixture.front()
        fixture.force.assert_not_called()

    def run_real_helper(self, fixture, **call):
        tree = ast.parse(bridge_check.BRIDGE.read_text())
        helper = next(node for node in tree.body if getattr(node, "name", None) == "_force_foreground")
        user32 = Mock()
        user32.GetForegroundWindow.return_value = 20
        user32.GetWindowThreadProcessId.return_value = 4
        kernel = SimpleNamespace(GetCurrentThreadId=lambda: 5)
        scope = {"user32": user32, "time": SimpleNamespace(sleep=Mock()),
                 "ctypes": SimpleNamespace(windll=SimpleNamespace(kernel32=kernel))}
        exec(compile(ast.Module([helper], []), str(bridge_check.BRIDGE), "exec"), scope)
        return scope["_force_foreground"], user32

    def test_failed_foreground_does_not_inject_keys(self):
        fixture = self.fixture()
        helper, user32 = self.run_real_helper(fixture)
        fixture.force.side_effect = helper
        with self.assertRaises(Refused):
            fixture.front()
        user32.keybd_event.assert_not_called()
        self.assertEqual(user32.SetForegroundWindow.call_count, 3)

    def test_the_older_callers_of_the_helper_keep_their_alt_tap(self):
        helper, user32 = self.run_real_helper(self.fixture())
        self.assertFalse(helper(10))
        self.assertEqual(user32.keybd_event.call_count, 2)


class LogFileCheck(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.path = self.root / "computer_actions.jsonl"
        self.log = ComputerActionLog(self.root)

    def test_short_append_does_not_corrupt_next_record(self):
        old = {"id": "fake-old", "state": "done"}
        new = {"id": "fake-new", "state": "start"}
        self.log(old)
        write = os.write
        with patch("jarvis.v3.computer.os.write", side_effect=lambda fd, data: write(fd, data[:8])):
            with self.assertRaises(OSError):
                self.log({"id": "fake-incomplete", "state": "start"})
        self.log(new)
        expected = "".join(json.dumps(item) + "\n" for item in (old, new))
        self.assertEqual(self.path.read_text(), expected)

    def test_a_hard_link_to_the_log_is_refused(self):
        self.log({"state": "start"})
        before = self.path.read_bytes()
        os.link(self.path, self.root / "second-name")
        with self.assertRaises(OSError):
            self.log({"state": "done"})
        self.assertEqual(self.path.read_bytes(), before)


class CliTerminalCheck(unittest.TestCase):
    def test_window_and_control_text_is_printed_as_inert_text(self):
        fixture = computer_cli_check.ComputerCliCheck()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.reply({"app": "example.exe", "title": "First\nSecond\x1b",
                       "control": {"name": "Go\x1b[2J\r\nnow", "role": "button\x07"}})
        status, lines = fixture.run_cli("front", "1", "--code", "fake-code")
        self.assertEqual((status, lines), (0, ["front: example.exe - First Second"]))
        status, lines = fixture.run_cli("click", "1", "2", "--code", "fake-code")
        self.assertEqual((status, lines), (0, ['clicked: button "Go [2J now" in example.exe - First Second']))


if __name__ == "__main__":
    unittest.main()
