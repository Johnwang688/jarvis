"""Acceptance tests for the daemon's desktop screenshot service (jarvis/v3/computer.py).

Written by the lead before the build. The implementer may not edit this file.
"""
import unittest

from jarvis.desktop import DesktopError
from jarvis.v3.computer import ComputerDrive, ComputerError

SHOT = {"image_b64": "aGVsbG8=", "bytes": 8, "marks": 0, "size": [800, 600]}


def row(window, *, front=False, access="drivable", app="notepad.exe", title="Notes"):
    return {"window": window, "app": app, "title": title, "access": access, "front": front}


class FakeDesktop:
    """Stands in for jarvis.desktop.DesktopSession."""

    def __init__(self, windows=()):
        self.windows, self.calls, self.connected = list(windows), [], True
        self.fail, self.during = None, None

    def request(self, op, timeout=90.0, *, require_pair=False, current=None, **fields):
        self.calls.append((op, require_pair, fields))
        if self.during is not None:
            self.during(op)
        if self.fail is not None:
            raise DesktopError(self.fail)
        if op == "windows":
            return {"windows": [dict(item) if isinstance(item, dict) else item for item in self.windows]}
        if op == "window_screenshot":
            return dict(SHOT)
        raise AssertionError(f"unexpected bridge op {op}")


class ComputerDriveCheck(unittest.TestCase):
    def drive(self, windows=(), *, enabled=True):
        self.desktop = FakeDesktop(windows)
        return ComputerDrive(self.desktop, enabled=lambda: enabled)

    def refused(self, call):
        with self.assertRaises(ComputerError) as caught:
            call()
        self.assertTrue(str(caught.exception))
        return str(caught.exception)

    def test_screenshot_is_of_the_front_window(self):
        drive = self.drive([row("a" * 16), row("b" * 16, front=True, app="chrome.exe", title="Docs"),
                            row("c" * 16)])
        code = drive.start()
        shot = drive.screenshot(code)
        self.assertEqual(shot, {"image_b64": SHOT["image_b64"], "size": [800, 600],
                                "app": "chrome.exe", "title": "Docs"})
        self.assertEqual([op for op, _, _ in self.desktop.calls], ["windows", "window_screenshot"])
        self.assertEqual(self.desktop.calls[1][2]["window"], "b" * 16)

    def test_bridge_calls_are_paired_scoped_and_report_no_hud_count(self):
        drive = self.drive([row("a" * 16, front=True)])
        code = drive.start()
        drive.screenshot(code)
        scopes = set()
        for _op, require_pair, fields in self.desktop.calls:
            self.assertIs(require_pair, True)
            self.assertIn("hud_sensitive", fields)
            self.assertIsNone(fields["hud_sensitive"])
            self.assertIsInstance(fields["scope"], str)
            self.assertTrue(fields["scope"].strip())
            self.assertNotIn(code, fields["scope"])
            scopes.add(fields["scope"])
        self.assertEqual(len(scopes), 1)

    def test_a_see_only_front_window_can_be_shown(self):
        drive = self.drive([row("a" * 16, front=True, access="see_only", app="windowsterminal.exe")])
        self.assertEqual(drive.screenshot(drive.start())["app"], "windowsterminal.exe")

    def test_the_code_is_long_and_fresh_each_time(self):
        drive = self.drive([row("a" * 16, front=True)])
        first, second = drive.start(), drive.start()
        self.assertIsInstance(first, str)
        self.assertGreaterEqual(len(first), 16)
        self.assertNotEqual(first, second)
        self.refused(lambda: drive.screenshot(first))
        self.assertEqual(self.desktop.calls, [])
        drive.screenshot(second)

    def test_a_wrong_or_malformed_code_reaches_no_bridge(self):
        drive = self.drive([row("a" * 16, front=True)])
        code = drive.start()
        for bad in ("", "x", code + "x", code[:-1], code.upper() + "!", None, 7, b"x", [code], "é" * 20):
            with self.subTest(bad=bad):
                self.refused(lambda: drive.screenshot(bad))
        self.assertEqual(self.desktop.calls, [])

    def test_no_grant_means_no_screenshot(self):
        drive = self.drive([row("a" * 16, front=True)])
        self.refused(lambda: drive.screenshot("anything-at-all-long-enough"))
        code = drive.start()
        self.assertIs(drive.stop(), True)
        self.assertIs(drive.stop(), False)
        self.refused(lambda: drive.screenshot(code))
        self.assertEqual(self.desktop.calls, [])

    def test_switched_off_refuses_to_start(self):
        drive = self.drive([row("a" * 16, front=True)], enabled=False)
        self.refused(drive.start)
        self.assertEqual(drive.status(), {"enabled": False, "driving": False, "bridge": True})

    def test_switching_off_later_ends_the_grant(self):
        enabled = [True]
        self.desktop = FakeDesktop([row("a" * 16, front=True)])
        drive = ComputerDrive(self.desktop, enabled=lambda: enabled[0])
        code = drive.start()
        enabled[0] = False
        self.refused(lambda: drive.screenshot(code))
        self.assertEqual(self.desktop.calls, [])
        self.assertIs(drive.status()["driving"], False)

    def test_status_reports_the_three_facts(self):
        drive = self.drive()
        self.assertEqual(drive.status(), {"enabled": True, "driving": False, "bridge": True})
        drive.start()
        self.desktop.connected = False
        self.assertEqual(drive.status(), {"enabled": True, "driving": True, "bridge": False})
        self.assertEqual(self.desktop.calls, [])

    def test_no_front_window_takes_no_picture(self):
        for windows in ([], [row("a" * 16), row("b" * 16)],
                        [row("a" * 16, front="yes")], [row("a" * 16, front=1)],
                        [row("a" * 16, front=True, access="protected")],
                        [row("a" * 16, front=True, access="other")],
                        [row("a" * 16, front=True), row("b" * 16, front=True)],
                        [row(None, front=True)], [row("", front=True)], ["not a row"]):
            with self.subTest(windows=windows):
                drive = self.drive(windows)
                self.refused(lambda: drive.screenshot(drive.start()))
                self.assertEqual([op for op, _, _ in self.desktop.calls], ["windows"])

    def test_a_malformed_bridge_reply_is_refused(self):
        drive = self.drive([row("a" * 16, front=True)])
        code = drive.start()
        for reply in (None, [], {"windows": "x"}, {}):
            with self.subTest(reply=reply):
                self.desktop.request = lambda op, timeout=90.0, reply=reply, **fields: reply
                self.refused(lambda: drive.screenshot(code))

    def test_a_malformed_picture_is_refused(self):
        for shot in ({}, {"image_b64": 5, "size": [1, 1]}, {"image_b64": "", "size": [1, 1]},
                     {"image_b64": "aA==", "size": "big"}, {"image_b64": "aA==", "size": [1]},
                     {"image_b64": "aA==", "size": [1, True]}, {"image_b64": "aA==", "size": [0, 5]}):
            with self.subTest(shot=shot):
                drive = self.drive([row("a" * 16, front=True)])
                code = drive.start()
                original = self.desktop.request

                def request(op, timeout=90.0, shot=shot, original=original, **fields):
                    result = original(op, timeout, **fields)
                    return shot if op == "window_screenshot" else result

                self.desktop.request = request
                self.refused(lambda: drive.screenshot(code))

    def test_a_bridge_refusal_is_passed_on_in_words(self):
        drive = self.drive([row("a" * 16, front=True)])
        code = drive.start()
        self.desktop.fail = "Refused: A protected window overlaps the target."
        self.assertIn("protected window overlaps", self.refused(lambda: drive.screenshot(code)))

    def test_a_stop_during_the_capture_returns_no_picture(self):
        drive = self.drive([row("a" * 16, front=True)])
        code = drive.start()
        self.desktop.during = lambda op: drive.stop("owner") if op == "window_screenshot" else None
        self.refused(lambda: drive.screenshot(code))

    def test_a_new_grant_during_the_capture_returns_no_picture_to_the_old_one(self):
        drive = self.drive([row("a" * 16, front=True)])
        code = drive.start()
        fresh = []
        self.desktop.during = lambda op: (fresh.append(drive.start())
                                          if op == "window_screenshot" and not fresh else None)
        self.refused(lambda: drive.screenshot(code))
        self.desktop.during = None
        drive.screenshot(fresh[0])

    def test_title_and_app_are_text_or_empty(self):
        drive = self.drive([row("a" * 16, front=True, app=None, title=None)])
        shot = drive.screenshot(drive.start())
        self.assertEqual((shot["app"], shot["title"]), ("", ""))


if __name__ == "__main__":
    unittest.main()
