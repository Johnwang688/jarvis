"""Regression tests from the independent review of `jarvis computer screenshot`."""
import ast
import base64
from contextlib import contextmanager, redirect_stdout
from http import HTTPStatus
import io
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageGrab

from jarvis.desktop import DesktopError, DesktopSession
from jarvis.v3 import cli
from jarvis.v3.computer import ComputerDrive, ComputerError
from tests.v2 import desktop_guard_check
from tests.v2.desktop_guard_check import FakeDesktop
from tests.v3.computer_cli_check import PICTURE, SHOT
from windows.desktopguard import Guard, Rect, Refused, black_out
from windows.windowpolicy import Window


def bridge_ops(guard):
    """The bridge's own list and screenshot operations over a fake desktop."""
    source = Path(desktop_guard_check.__file__).resolve().parents[2] / "windows" / "bridge.py"
    tree = ast.parse(source.read_text())
    helpers = {"op_windows", "op_window_screenshot", "_window_grant", "_same_window", "_window_context"}
    tree.body = [node for node in tree.body if getattr(node, "name", None) in helpers]
    scope = dict(_WINDOW_GUARD=guard, _WINDOW_SCREENSHOTS={}, Refused=Refused,
                 black_out=black_out, io=io, base64=base64)
    exec(compile(tree, str(source), "exec"), scope)
    return scope


class FocusChangeCheck(unittest.TestCase):
    def setUp(self):
        self.desktop = FakeDesktop()
        self.desktop.add(20, Window("paint.exe", False, "Paint"), Rect(110, 0, 210, 100), "Paint")
        self.bridge = bridge_ops(Guard(self.desktop))
        self.after_listing = None

    def drive(self):
        check = self

        class Peer:
            connected = True

            def request(self, op, **fields):
                try:
                    result = check.bridge["op_" + op](fields)
                except Refused as exc:
                    raise DesktopError(str(exc)) from None
                if op == "windows" and check.after_listing is not None:
                    check.desktop.front = check.after_listing
                return result

        return ComputerDrive(Peer(), enabled=lambda: True)

    def shoot(self):
        drive = self.drive()
        with patch.object(ImageGrab, "grab", return_value=Image.new("RGB", (100, 100))):
            return drive.screenshot(drive.start())

    def test_capture_refuses_when_the_listed_window_loses_focus(self):
        self.after_listing = 20
        with self.assertRaises(ComputerError) as caught:
            self.shoot()
        self.assertIn("no longer in front", str(caught.exception))

    def test_capture_of_a_window_still_in_front_works(self):
        self.assertEqual(self.shoot()["size"], [100, 100])

    def test_the_older_callers_of_the_bridge_screenshot_are_not_held_to_the_front(self):
        rows = self.bridge["op_windows"]({})["windows"]
        behind = next(row for row in rows if not row["front"])
        with patch.object(ImageGrab, "grab", return_value=Image.new("RGB", (100, 100))):
            shot = self.bridge["op_window_screenshot"]({"window": behind["window"], "scope": "fake-scope"})
        self.assertEqual(shot["size"], [100, 100])


class QueuedRevocationCheck(unittest.TestCase):
    def test_replaced_grant_is_reported_as_ended_when_bridge_slot_opens(self):
        desktop = DesktopSession(port=0, managed=True)
        desktop._server = Mock()
        desktop._server.getsockname.return_value = ("127.0.0.1", 0)
        desktop._paired = True
        desktop._conn = Mock()
        desktop._stream = Mock()
        drive = ComputerDrive(desktop, enabled=lambda: True)
        old_code = drive.start()

        @contextmanager
        def bridge_slot():
            drive.stop()
            drive.start()
            yield

        desktop._requests = bridge_slot()
        with self.assertRaises(ComputerError) as caught:
            drive.screenshot(old_code)
        desktop._stream.write.assert_not_called()
        self.assertEqual(str(caught.exception), "The desktop driving grant ended.")


class BridgeCallCheck(unittest.TestCase):
    """Two mutants the review found alive: no `current` callback, and a longer timeout."""

    def test_each_bridge_call_carries_a_live_check_of_the_grant_and_a_30_second_limit(self):
        seen = []

        class Desktop:
            connected = True

            def request(self, op, timeout=90.0, *, require_pair=False, current=None, **fields):
                seen.append((op, timeout, current))
                if op == "windows":
                    return {"windows": [{"window": "a" * 16, "app": "notepad.exe", "title": "Notes",
                                         "access": "drivable", "front": True}]}
                return {"image_b64": "aGVsbG8=", "bytes": 8, "marks": 0, "size": [8, 6]}

        drive = ComputerDrive(Desktop(), enabled=lambda: True)
        drive.screenshot(drive.start())
        self.assertEqual([(op, timeout) for op, timeout, _ in seen], [("windows", 30), ("window_screenshot", 30)])
        for _, _, current in seen:
            self.assertIs(current(), True)
        drive.stop("owner")
        for _, _, current in seen:
            self.assertIs(current(), False)


class CliCheck(unittest.TestCase):
    def test_disappearing_old_file_does_not_fail_a_successful_save(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / f"jarvis-computer-{os.getuid()}"
            folder.mkdir(mode=0o700)
            for index in range(20):
                shot = folder / f"shot-20261123-000000-{index:08x}.png"
                shot.touch()
                os.utime(shot, (index + 1, index + 1))

            unlink = os.unlink

            def unlink_after_cleanup(name, *, dir_fd=None):
                unlink(name, dir_fd=dir_fd)
                unlink(name, dir_fd=dir_fd)

            with patch.object(cli.tempfile, "gettempdir", return_value=root):
                with patch.object(cli.os, "unlink", side_effect=unlink_after_cleanup):
                    with redirect_stdout(StringIO()) as output:
                        exit_code = cli._print_screenshot(SHOT, None)

            self.assertEqual(exit_code, 0, output.getvalue())
            saved = Path(output.getvalue().splitlines()[0])
            self.assertEqual(saved.read_bytes(), PICTURE)
            self.assertEqual(len(list(folder.iterdir())), 20)

    def test_window_text_is_printed_as_one_plain_line(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "shot.png"
            shot = {**SHOT, "app": "note\x07pad.exe", "title": "Notes\x1b[31m\r\nsecond third"}
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(cli._print_screenshot(shot, path), 0)
            self.assertEqual(path.read_bytes(), PICTURE)
            lines = output.getvalue().split("\n")
            self.assertEqual(lines[1], "window: note pad.exe - Notes [31m second third")
            self.assertEqual(len(lines), 4)

    def reply(self, status, body, action="screenshot"):
        args = SimpleNamespace(action=action, code="fake-code", out=None)
        with patch.object(cli, "HTTPConnection") as connection, redirect_stdout(StringIO()) as output:
            response = connection.return_value.getresponse.return_value
            response.status = status
            response.read.return_value = json.dumps(body).encode()
            return cli.computer_command(args), output.getvalue().splitlines()

    def test_a_refusal_from_the_http_layer_keeps_its_reason(self):
        refusal = HTTPStatus.REQUEST_ENTITY_TOO_LARGE
        self.assertEqual(self.reply(refusal, {"error": refusal.phrase}), (1, [refusal.phrase]))

    def test_a_refusal_with_no_words_still_fails_plainly(self):
        for body in ({}, {"error": ""}, {"error": 5}, {"message": None}):
            with self.subTest(body=body):
                self.assertEqual(self.reply(409, body),
                                 (1, ["The v3 daemon returned an unexpected desktop driving reply."]))

    def test_the_daemons_own_message_wins_and_is_one_plain_line(self):
        self.assertEqual(self.reply(409, {"message": "Refused:\x1b[2J no\nwindow", "error": "other"}),
                         (1, ["Refused: [2J no window"]))


if __name__ == "__main__":
    unittest.main()
