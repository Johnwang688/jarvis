"""A dry run of the owner-run `jarvis computer` check against a made-up desktop.

`tests/v3/computer_live_smoke.py` acts on a real desktop and is never run here.
This drives its `check` function with the real ComputerDrive over a fake bridge.
"""
from base64 import b64encode
from io import BytesIO
from pathlib import Path
from random import Random
import sys
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))

import computer_live_smoke as smoke  # noqa: E402
from jarvis.desktop import DesktopError  # noqa: E402
from jarvis.v3.computer import ComputerDrive  # noqa: E402

SIZE = (800, 600)
MENU_BUTTON = (10, 30, 60, 54)
MENU = (10, 54, 210, 300)
NOTEPAD, OTHER, CHARMAP = "n" * 16, "o" * 16, "c" * 16


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds):
        self.now += seconds

    def monotonic(self):
        return self.now


def inside(box, x, y):
    return box[0] <= x < box[2] and box[1] <= y < box[3]


class FakeWindows:
    """A Notepad showing the check file, one other window, and later Character Map."""

    def __init__(self, *, reversed_wheel=False, menus_reachable=False, charmap_takes_front=True,
                 notepad_title="scroll-check.txt - Notepad", stolen_after=None, other_accepts_input=False):
        self.connected, self.calls = True, []
        self.front, self.top, self.menu_open, self.charmap = None, 2000, False, False
        self.reversed_wheel, self.menus_reachable = reversed_wheel, menus_reachable
        self.charmap_takes_front, self.notepad_title = charmap_takes_front, notepad_title
        self.stolen_after = stolen_after  # the other window takes the front after this many listings
        self.other_accepts_input = other_accepts_input
        rng = Random(7)
        self.page = Image.new("L", (SIZE[0], 6000), "white")
        draw = ImageDraw.Draw(self.page)
        for y in range(0, 6000, 14):
            draw.text((60, y), f"line {y} " + "note " * rng.randrange(1, 12), fill="black")

    def open_program(self, name):
        if name == "notepad":
            self.front = NOTEPAD
        else:
            self.charmap = True
            if self.charmap_takes_front:
                self.front = CHARMAP

    def rows(self):
        rows = [{"window": OTHER, "app": "chrome.exe", "title": "Private page", "access": "drivable"}]
        if self.front is not None:
            rows.append({"window": NOTEPAD, "app": "Notepad.exe", "title": self.notepad_title, "access": "drivable"})
        if self.charmap:
            rows.append({"window": CHARMAP, "app": "charmap.exe", "title": "Character Map", "access": "drivable"})
        return [{**row, "front": row["window"] == self.front} for row in rows]

    def picture(self):
        image = Image.new("L", SIZE, "white")
        image.paste(self.page.crop((0, self.top, SIZE[0], self.top + SIZE[1] - 60)), (0, 60))
        draw = ImageDraw.Draw(image)
        draw.rectangle(MENU_BUTTON, outline="black")
        if self.menu_open:
            draw.rectangle(MENU, fill=200, outline="black")
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return {"image_b64": b64encode(buffer.getvalue()).decode(), "size": list(SIZE), "bytes": 1, "marks": 0}

    def request(self, op, timeout=90.0, *, require_pair=False, current=None, **fields):
        self.calls.append((op, fields))
        if op == "windows":
            listings = sum(1 for name, _ in self.calls if name == "windows")
            if self.stolen_after is not None and listings > self.stolen_after:
                self.front = OTHER
            return {"windows": self.rows()}
        if op == "window_screenshot" and fields["window"] == OTHER:
            return self.picture()  # looking at the front window is allowed; acting on it is not
        if fields["window"] == OTHER and self.other_accepts_input and op != "window_front":
            return {"name": "Example", "role": "document", "rect": [0, 0, 1, 1]}  # recorded in self.calls
        if fields["window"] != NOTEPAD:
            raise AssertionError(f"{op} reached a window other than the check's Notepad")
        if op == "window_front":
            self.front = NOTEPAD
            return {"front": True}
        if op == "window_screenshot":
            return self.picture()
        x, y = fields["x"], fields["y"]
        on_menu = self.menu_open and inside(MENU, x, y)
        if on_menu and not self.menus_reachable:
            raise DesktopError("Refused: The requested point is covered.")
        if op == "window_probe":
            if on_menu or inside(MENU_BUTTON, x, y):
                return {"name": "File", "role": "menuitem", "rect": [0, 0, 1, 1]}
            return {"name": "Text editor", "role": "document", "rect": [0, 0, 1, 1]}
        if self.front != NOTEPAD:
            raise DesktopError("Refused: Another window covers this point.")
        if op == "window_click":
            self.menu_open = inside(MENU_BUTTON, x, y)
            return {"clicked": True}
        if op == "window_scroll":
            self.top -= fields["amount"] * 40 * (-1 if self.reversed_wheel else 1)
            return {"scrolled": fields["amount"]}
        raise AssertionError(f"unexpected bridge op {op}")


class ComputerLiveReviewCheck(unittest.TestCase):
    def run_check(self, **options):
        desktop, output, records = FakeWindows(**options), [], []
        report = smoke.base.Report(output.append)
        drive = ComputerDrive(desktop, enabled=lambda: True, log=records.append)

        def ask(op, **fields):
            return desktop.request(op, scope="direct", hud_sensitive=None, **fields)

        with patch.object(smoke, "time", FakeClock()):
            smoke.check(drive, drive.start(), ask, report, desktop.open_program, records)
        return desktop, report, output

    def test_a_working_desktop_passes_every_step(self):
        desktop, report, output = self.run_check()
        self.assertEqual(report.failed, [], output)
        ops = [op for op, _ in desktop.calls]
        for op in ("window_click", "window_scroll", "window_front"):
            self.assertIn(op, ops)
        text = "\n".join(output)
        self.assertIn("PASS  the click opened the menu", text)
        self.assertIn("INFO  a point inside the open menu cannot be reached", text)
        self.assertIn("PASS  Notepad is in front again", text)
        self.assertIn("PASS  after stop the code no longer works", text)

    def test_reachable_menu_items_are_reported(self):
        _, report, output = self.run_check(menus_reachable=True)
        self.assertEqual(report.failed, [], output)
        self.assertIn("INFO  a point inside the open menu can be reached: role menuitem", "\n".join(output))

    def test_a_reversed_wheel_fails_the_wheel_step(self):
        _, report, _ = self.run_check(reversed_wheel=True)
        self.assertEqual(report.failed, ["the wheel moved the page the way the command says"])

    def test_another_front_window_is_never_clicked(self):
        # Notepad shows some other file: the front window is not the check's, so nothing may be sent to it.
        desktop, report, _ = self.run_check(notepad_title="Private notes - Notepad")
        self.assertEqual(report.failed, ["Notepad showing the check file is listed"])
        self.assertEqual({op for op, _ in desktop.calls}, {"windows"})

    def test_a_window_that_takes_the_front_before_the_first_picture_stops_the_run(self):
        desktop, report, output = self.run_check(stolen_after=1)
        self.assertEqual(report.failed, ["the front window is still the check's Notepad (start)"], output)
        self.assertEqual({op for op, _ in desktop.calls}, {"windows", "window_screenshot"})

    def test_a_window_that_takes_the_front_later_gets_no_click_or_wheel(self):
        # From the review: only the first screenshot was checked, so a later one of another
        # window would have been clicked and scrolled.
        for listings in range(2, 11):
            with self.subTest(listings=listings):
                desktop, report, output = self.run_check(stolen_after=listings, other_accepts_input=True)
                self.assertFalse([(op, fields) for op, fields in desktop.calls
                                  if op in ("window_probe", "window_click", "window_scroll", "window_front")
                                  and fields.get("window") == OTHER], output)
                self.assertTrue(report.failed, output)

    def test_output_never_carries_a_window_title(self):
        _, _, output = self.run_check()
        text = "\n".join(output)
        for title in ("Private page", "scroll-check.txt - Notepad", "Character Map"):
            self.assertNotIn(title, text)

    def test_charmap_staying_behind_skips_the_switch_without_failing(self):
        _, report, output = self.run_check(charmap_takes_front=False)
        self.assertEqual(report.failed, [], output)
        self.assertIn("SKIP  Character Map did not take the front", "\n".join(output))


if __name__ == "__main__":
    unittest.main()
