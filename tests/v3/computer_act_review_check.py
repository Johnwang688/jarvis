"""Regression tests from the independent review of click, scroll and window switching."""
import ast
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
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
        desktop = SimpleNamespace(connected=True, send_gate=gate, drop_bridge=Mock())
        drive = ComputerDrive(desktop, enabled=lambda: True)
        drive.start()
        drive.stop()
        self.assertEqual(gate.acquire.call_args_list, [unittest.mock.call(timeout=2)] * 2)
        self.assertEqual(gate.release.call_count, 2)
        desktop.drop_bridge.assert_not_called()
        # A send that never finishes: the connection is closed so its bytes never arrive, the
        # grant is revoked all the same, and the gate is not released by us.
        gate.reset_mock()
        gate.acquire.return_value = False
        code = drive.start()
        self.assertTrue(drive.stop())
        with self.assertRaises(ComputerError):
            drive.screenshot(code)
        gate.release.assert_not_called()
        self.assertEqual(desktop.drop_bridge.call_count, 2)

    def test_stop_cuts_off_a_send_that_is_stuck(self):
        # From the second review: stop gave up waiting after two seconds and the buffered click
        # could still reach Windows when the bridge started reading again.
        session = self.session()
        conn = Mock()
        session._conn = conn
        sent, flushing = [], Event()
        stream = session._stream = Mock()
        stream.write.side_effect = lambda data: sent.append(json.loads(data)["op"])

        def reply(result):
            stream.readline.return_value = json.dumps({"id": session._next_id, "ok": True, "result": result}).encode()

        def flush():
            reply({"windows": [row(A, front=True)]})
            if sent[-1] != "window_front":
                return
            flushing.set()
            sent.pop()  # the bytes sit in the buffer; the bridge is not reading
            for _ in range(800):
                if conn.shutdown.called:
                    raise BrokenPipeError("the connection was closed under the send")
                time.sleep(0.01)
            sent.append("window_front")  # the bridge read them after all

        stream.flush.side_effect = flush
        stream.close.side_effect = BrokenPipeError("bytes still buffered")
        drive = ComputerDrive(session, enabled=lambda: True, log=lambda record: None)
        code = drive.start()
        drive.windows(code)
        with ThreadPoolExecutor() as pool:
            action = pool.submit(drive.front, code, 1)
            self.assertTrue(flushing.wait(2))
            started = time.monotonic()
            self.assertTrue(drive.stop())
            self.assertLess(time.monotonic() - started, 4)
            with self.assertRaises(ComputerError):
                action.result(timeout=4)
        self.assertNotIn("window_front", sent)
        self.assertFalse(session.connected)

    def test_a_connection_dropped_before_the_last_check_gets_nothing_written(self):
        session = self.session()
        stream = session._stream = Mock()
        session._conn = SimpleNamespace(settimeout=lambda _: setattr(session, "_conn", None))
        with self.assertRaises(DesktopError) as caught:
            session.request("ping")
        self.assertIn("disconnected", str(caught.exception))
        stream.write.assert_not_called()

    def test_stopping_a_session_survives_a_stream_that_cannot_flush_on_close(self):
        session = DesktopSession(port=0)
        session.start()
        stream = session._stream = Mock()
        stream.close.side_effect = BrokenPipeError("bytes still buffered")
        session._conn = Mock()
        session.stop()
        self.assertIsNone(session.bound_port)
        self.assertFalse(session.connected)


class CheckingDesktop(FakeDesktop):
    """Like the real session, it asks `current()` right before sending."""

    before_check = None

    def request(self, op, timeout=90.0, *, require_pair=False, current=None, **fields):
        self.current = current
        if self.before_check is not None:
            self.before_check(op)
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

    def test_a_selection_replaced_while_the_call_waits_for_the_bridge_is_not_sent(self):
        # The check made before handing the call to the bridge session is not enough: the call
        # can wait there, so the session's own last check has to ask again.
        desktop = CheckingDesktop([row(A, front=True)])
        drive = ComputerDrive(desktop, enabled=lambda: True, log=lambda record: None)
        code = drive.start()
        drive.screenshot(code)

        def newer_screenshot(op):
            if op == "window_click":
                desktop.before_check = None
                drive.screenshot(code)

        desktop.before_check = newer_screenshot
        with self.assertRaises(ComputerError) as caught:
            drive.click(code, 1, 1)
        self.assertIn("Look again", str(caught.exception))
        self.assertNotIn("window_click", [op for op, _ in desktop.calls])

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

    def test_front_refuses_input_that_becomes_held_during_target_check(self):
        fixture = self.fixture()
        forward, held = fixture.guard.forward, Event()

        def inspect(window):
            hwnd = forward(window)
            held.set()
            return hwnd

        def idle():
            if held.is_set():
                raise Refused("Release held mouse buttons or modifiers and try again.")

        with patch.object(fixture.guard, "forward", side_effect=inspect), \
                patch.object(fixture.desktop, "require_idle_input", side_effect=idle):
            with self.assertRaises(Refused):
                fixture.front()
        fixture.force.assert_not_called()

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

    def test_short_write_cannot_remove_another_loggers_successful_append(self):
        first, second = ComputerActionLog(self.root), ComputerActionLog(self.root)
        record = {"state": "done"}
        write = os.write
        with ThreadPoolExecutor() as pool:
            append = None

            def short_write(fd, data):
                nonlocal append
                if b'"broken"' not in data:
                    return write(fd, data)
                append = pool.submit(second, record)
                try:
                    append.result(timeout=0.5)  # a writer that waits its turn finishes after the rollback
                except TimeoutError:
                    pass
                return write(fd, data[:3])

            with patch("jarvis.v3.computer.os.write", side_effect=short_write):
                with self.assertRaises(OSError):
                    first({"state": "broken"})
                append.result(timeout=2)
        self.assertEqual(self.path.read_text(), json.dumps(record) + "\n")

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
