"""Acceptance tests for clicking, scrolling and switching windows (jarvis/v3/computer.py).

Written by the lead before the build. The implementer may not edit this file.
"""
import json
import unittest

from jarvis.desktop import DesktopError
from jarvis.v3.computer import ComputerDrive, ComputerError

SHOT = {"image_b64": "aGVsbG8=", "bytes": 8, "marks": 0, "size": [800, 600]}
A, B, C = "a" * 16, "b" * 16, "c" * 16


def row(window, *, front=False, access="drivable", app="notepad.exe", title="Notes"):
    return {"window": window, "app": app, "title": title, "access": access, "front": front}


class FakeDesktop:
    """Stands in for jarvis.desktop.DesktopSession."""

    def __init__(self, windows):
        self.windows, self.calls, self.connected = list(windows), [], True
        self.fail, self.during = {}, None
        self.control = {"name": "Send", "role": "button", "rect": [10, 10, 90, 40]}

    def request(self, op, timeout=90.0, *, require_pair=False, current=None, **fields):
        self.calls.append((op, fields))
        if self.during is not None:
            self.during(op)
        if op in self.fail:
            raise DesktopError(self.fail[op])
        if op == "windows":
            return {"windows": [dict(item) if isinstance(item, dict) else item for item in self.windows]}
        if op == "window_screenshot":
            return dict(SHOT)
        if op == "window_probe":
            return self.control if not isinstance(self.control, dict) else dict(self.control)
        if op == "window_click":
            return {"clicked": True}
        if op == "window_scroll":
            return {"scrolled": fields["amount"]}
        if op == "window_front":
            return {"front": True}
        raise AssertionError(f"unexpected bridge op {op}")


class ComputerActCheck(unittest.TestCase):
    def setUp(self):
        self.records, self.log_fail = [], set()
        self.desktop = FakeDesktop([row(A), row(B, front=True, app="chrome.exe", title="Docs"), row(C)])
        self.drive = ComputerDrive(self.desktop, enabled=lambda: True, log=self.log)
        self.code = self.drive.start()

    def log(self, record):
        if len(self.records) in self.log_fail:
            self.records.append(None)
            raise OSError("disk full")
        self.records.append(json.loads(json.dumps(record)))

    def ops(self):
        return [op for op, _ in self.desktop.calls]

    def fields(self, op):
        return [fields for name, fields in self.desktop.calls if name == op]

    def refused(self, call):
        with self.assertRaises(ComputerError) as caught:
            call()
        self.assertTrue(str(caught.exception))
        return str(caught.exception)

    def looked(self):
        self.drive.screenshot(self.code)
        self.desktop.calls.clear()

    # -- click ---------------------------------------------------------------

    def test_click_probes_then_clicks_the_window_of_the_latest_screenshot(self):
        self.looked()
        result = self.drive.click(self.code, 400, 300)
        self.assertEqual(self.ops(), ["window_probe", "window_click"])
        probe, click = self.fields("window_probe")[0], self.fields("window_click")[0]
        self.assertEqual((probe["window"], probe["x"], probe["y"]), (B, 400, 300))
        self.assertEqual((click["window"], click["x"], click["y"]), (B, 400, 300))
        self.assertEqual(click["expect"], {"name": "Send", "role": "button"})
        self.assertNotIn("ref", click)
        self.assertEqual(result, {"control": {"name": "Send", "role": "button"},
                                  "app": "chrome.exe", "title": "Docs"})

    def test_every_act_call_is_paired_and_scoped_like_the_screenshot(self):
        self.drive.screenshot(self.code)
        self.drive.click(self.code, 1, 2)
        self.drive.scroll(self.code, 1, 2, -3)
        scopes = {fields["scope"] for _, fields in self.desktop.calls}
        self.assertEqual(len(scopes), 1)
        for _, fields in self.desktop.calls:
            self.assertIsNone(fields["hud_sensitive"])
            self.assertNotIn(self.code, json.dumps(fields))

    def test_click_is_logged_before_and_after(self):
        self.looked()
        self.drive.click(self.code, 400, 300)
        start, done = self.records
        self.assertEqual(start["state"], "start")
        self.assertEqual((start["op"], start["x"], start["y"]), ("click", 400, 300))
        self.assertEqual((start["app"], start["title"]), ("chrome.exe", "Docs"))
        self.assertEqual(start["control"], {"name": "Send", "role": "button"})
        self.assertEqual(done["state"], "done")
        self.assertEqual(done["id"], start["id"])
        self.assertTrue(start["id"])
        for record in self.records:
            self.assertIsInstance(record["at"], str)
            self.assertNotIn(self.code, json.dumps(record))

    def test_click_needs_a_screenshot_first(self):
        self.refused(lambda: self.drive.click(self.code, 1, 1))
        self.assertEqual((self.desktop.calls, self.records), ([], []))

    def test_click_needs_the_code(self):
        self.looked()
        for bad in ("", "nope", None, self.code + "x"):
            self.refused(lambda: self.drive.click(bad, 1, 1))
        self.assertEqual((self.desktop.calls, self.records), ([], []))

    def test_click_point_must_be_whole_numbers_inside_the_picture(self):
        self.looked()
        for x, y in ((-1, 5), (5, -1), (800, 5), (5, 600), (1.5, 5), (5, "5"), (True, 5), (None, 5), (10**9, 5)):
            with self.subTest(point=(x, y)):
                self.refused(lambda: self.drive.click(self.code, x, y))
        self.assertEqual((self.desktop.calls, self.records), ([], []))
        self.drive.click(self.code, 0, 0)
        self.drive.click(self.code, 799, 599)

    def test_a_refused_probe_sends_no_click_and_logs_nothing(self):
        self.looked()
        self.desktop.fail["window_probe"] = "Refused: The requested point is covered."
        self.assertIn("covered", self.refused(lambda: self.drive.click(self.code, 5, 5)))
        self.assertEqual(self.ops(), ["window_probe"])
        self.assertEqual(self.records, [])

    def test_a_malformed_probe_sends_no_click(self):
        self.looked()
        for control in (None, [], {}, {"name": "x"}, {"role": "button"}, {"name": 5, "role": "button"},
                        {"name": "x", "role": None}, {"name": "x", "role": ""}):
            with self.subTest(control=control):
                self.desktop.control = control
                self.refused(lambda: self.drive.click(self.code, 5, 5))
        self.assertNotIn("window_click", self.ops())
        self.assertEqual(self.records, [])

    def test_an_unnamed_control_can_still_be_clicked(self):
        self.looked()
        self.desktop.control = {"name": "", "role": "pane", "rect": [0, 0, 5, 5]}
        result = self.drive.click(self.code, 5, 5)
        self.assertEqual(result["control"], {"name": "", "role": "pane"})
        self.assertEqual(self.fields("window_click")[0]["expect"], {"name": "", "role": "pane"})

    def test_a_long_control_name_is_cut_for_the_reply_and_the_log_but_not_for_the_bridge(self):
        self.looked()
        name = "word " * 1000
        self.desktop.control = {"name": name, "role": "text", "rect": [0, 0, 5, 5]}
        result = self.drive.click(self.code, 5, 5)
        self.assertEqual(self.fields("window_click")[0]["expect"]["name"], name)
        self.assertLessEqual(len(result["control"]["name"]), 200)
        self.assertLessEqual(len(self.records[0]["control"]["name"]), 200)

    def test_no_log_no_click(self):
        self.looked()
        self.log_fail = {0}
        self.refused(lambda: self.drive.click(self.code, 5, 5))
        self.assertEqual(self.ops(), ["window_probe"])

    def test_a_drive_without_a_log_refuses_to_act_but_still_looks(self):
        drive = ComputerDrive(self.desktop, enabled=lambda: True)
        code = drive.start()
        drive.screenshot(code)
        self.desktop.calls.clear()
        self.refused(lambda: drive.click(code, 5, 5))
        self.refused(lambda: drive.scroll(code, 5, 5, 1))
        self.assertNotIn("window_click", self.ops())
        self.assertNotIn("window_scroll", self.ops())

    def test_a_failed_outcome_line_does_not_hide_a_click_that_happened(self):
        self.looked()
        self.log_fail = {1}
        self.assertEqual(self.drive.click(self.code, 5, 5)["control"]["name"], "Send")
        self.assertEqual(self.ops(), ["window_probe", "window_click"])

    def test_a_refused_click_is_logged_as_refused(self):
        self.looked()
        self.desktop.fail["window_click"] = "Refused: The pointer or control moved. Inspect the window again."
        self.assertIn("pointer or control moved", self.refused(lambda: self.drive.click(self.code, 5, 5)))
        self.assertEqual([record["state"] for record in self.records], ["start", "refused"])
        self.assertIn("pointer or control moved", self.records[1]["detail"])

    def test_a_lost_bridge_is_logged_as_failed(self):
        self.looked()
        self.desktop.fail["window_click"] = "lost the desktop bridge mid-request (OSError). Restart it on the Windows side."
        self.refused(lambda: self.drive.click(self.code, 5, 5))
        self.assertEqual([record["state"] for record in self.records], ["start", "failed"])

    def test_a_stop_after_the_probe_sends_no_click(self):
        self.looked()
        self.desktop.during = lambda op: self.drive.stop("owner") if op == "window_probe" else None
        self.refused(lambda: self.drive.click(self.code, 5, 5))
        self.assertEqual(self.ops(), ["window_probe"])
        self.assertEqual(self.records, [])

    def test_a_new_grant_starts_without_a_screenshot(self):
        self.looked()
        code = self.drive.start()
        self.refused(lambda: self.drive.click(code, 5, 5))
        self.assertEqual(self.desktop.calls, [])

    def test_the_newest_screenshot_decides_the_window(self):
        self.drive.screenshot(self.code)
        self.desktop.windows = [row(A, front=True), row(B, app="chrome.exe", title="Docs"), row(C)]
        self.drive.screenshot(self.code)
        self.desktop.calls.clear()
        result = self.drive.click(self.code, 5, 5)
        self.assertEqual(self.fields("window_click")[0]["window"], A)
        self.assertEqual((result["app"], result["title"]), ("notepad.exe", "Notes"))

    def test_a_failed_screenshot_forgets_the_earlier_one(self):
        self.drive.screenshot(self.code)
        self.desktop.windows = [row(A), row(B), row(C)]
        self.refused(lambda: self.drive.screenshot(self.code))
        self.desktop.calls.clear()
        self.refused(lambda: self.drive.click(self.code, 5, 5))
        self.assertEqual(self.desktop.calls, [])

    # -- scroll --------------------------------------------------------------

    def test_scroll_probes_then_turns_the_wheel(self):
        self.looked()
        result = self.drive.scroll(self.code, 400, 300, -3)
        self.assertEqual(self.ops(), ["window_probe", "window_scroll"])
        scroll = self.fields("window_scroll")[0]
        self.assertEqual((scroll["window"], scroll["x"], scroll["y"], scroll["amount"]), (B, 400, 300, -3))
        self.assertEqual(scroll["expect"], {"name": "Send", "role": "button"})
        self.assertEqual(result, {"control": {"name": "Send", "role": "button"},
                                  "app": "chrome.exe", "title": "Docs", "amount": -3})
        self.assertEqual((self.records[0]["op"], self.records[0]["amount"]), ("scroll", -3))
        self.assertEqual(self.records[1]["state"], "done")

    def test_scroll_amount_is_a_whole_number_from_1_to_100_either_way(self):
        self.looked()
        for amount in (0, 101, -101, 1.0, "3", True, None, 10**6):
            with self.subTest(amount=amount):
                self.refused(lambda: self.drive.scroll(self.code, 5, 5, amount))
        self.assertEqual((self.desktop.calls, self.records), ([], []))
        for amount in (1, -1, 100, -100):
            self.drive.scroll(self.code, 5, 5, amount)

    def test_scroll_needs_a_screenshot_and_a_point_inside_it(self):
        self.refused(lambda: self.drive.scroll(self.code, 5, 5, 1))
        self.looked()
        self.refused(lambda: self.drive.scroll(self.code, 800, 5, 1))
        self.assertEqual((self.desktop.calls, self.records), ([], []))

    # -- windows and front ---------------------------------------------------

    def test_windows_are_numbered_and_carry_no_bridge_ids(self):
        listed = self.drive.windows(self.code)
        self.assertEqual(listed, [
            {"n": 1, "app": "notepad.exe", "title": "Notes", "front": False, "access": "drivable"},
            {"n": 2, "app": "chrome.exe", "title": "Docs", "front": True, "access": "drivable"},
            {"n": 3, "app": "notepad.exe", "title": "Notes", "front": False, "access": "drivable"},
        ])
        self.assertEqual(self.ops(), ["windows"])

    def test_windows_needs_the_code(self):
        self.refused(lambda: self.drive.windows("nope"))
        self.assertEqual(self.desktop.calls, [])

    def test_a_malformed_row_refuses_the_whole_list(self):
        for windows in (["x"], [row(None)], [row("")], [row(A, access="protected")], [row(A, access=5)],
                        [row(A), row(A)]):
            with self.subTest(windows=windows):
                self.desktop.windows = windows
                self.refused(lambda: self.drive.windows(self.code))

    def test_front_brings_the_numbered_window_forward(self):
        self.drive.windows(self.code)
        self.desktop.calls.clear()
        result = self.drive.front(self.code, 3)
        self.assertEqual(self.ops(), ["window_front"])
        self.assertEqual(self.fields("window_front")[0]["window"], C)
        self.assertEqual(result, {"app": "notepad.exe", "title": "Notes"})

    def test_front_needs_a_listing_and_a_number_from_it(self):
        self.refused(lambda: self.drive.front(self.code, 1))
        self.drive.windows(self.code)
        self.desktop.calls.clear()
        for n in (0, 4, -1, 1.0, "2", True, None):
            with self.subTest(n=n):
                self.refused(lambda: self.drive.front(self.code, n))
        self.assertEqual(self.desktop.calls, [])

    def test_front_uses_the_latest_listing_only(self):
        self.drive.windows(self.code)
        self.desktop.windows = [row(C, front=True)]
        self.drive.windows(self.code)
        self.desktop.calls.clear()
        self.refused(lambda: self.drive.front(self.code, 2))
        self.drive.front(self.code, 1)
        self.assertEqual(self.fields("window_front")[0]["window"], C)

    def test_after_front_a_new_screenshot_is_needed(self):
        self.drive.screenshot(self.code)
        self.drive.windows(self.code)
        self.drive.front(self.code, 3)
        self.desktop.calls.clear()
        self.refused(lambda: self.drive.click(self.code, 5, 5))
        self.assertEqual(self.desktop.calls, [])

    def test_a_refused_front_keeps_the_earlier_screenshot(self):
        self.drive.screenshot(self.code)
        self.drive.windows(self.code)
        self.desktop.fail["window_front"] = "Refused: Windows did not bring that window to the front."
        self.assertIn("did not bring", self.refused(lambda: self.drive.front(self.code, 3)))
        self.drive.click(self.code, 5, 5)

    def test_a_new_grant_forgets_the_listing(self):
        self.drive.windows(self.code)
        code = self.drive.start()
        self.desktop.calls.clear()
        self.refused(lambda: self.drive.front(code, 1))
        self.assertEqual(self.desktop.calls, [])

    def test_front_is_logged(self):
        self.drive.windows(self.code)
        self.drive.front(self.code, 3)
        start, done = self.records
        self.assertEqual((start["state"], start["op"], start["app"], start["title"]),
                         ("start", "front", "notepad.exe", "Notes"))
        self.assertEqual((done["state"], done["id"]), ("done", start["id"]))


if __name__ == "__main__":
    unittest.main()
