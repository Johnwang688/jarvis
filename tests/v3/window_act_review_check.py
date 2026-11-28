"""Checks of the owner-run window check's own judgement, on made-up pictures.

`tests/v3/window_act_smoke.py` acts on a real desktop and is never run here.
These tests load only its pure helpers and feed them fake bridge replies.
"""
import ast
from base64 import b64decode, b64encode
from io import BytesIO
from pathlib import Path
from random import Random
from types import SimpleNamespace
import unittest

from PIL import Image, ImageDraw

SMOKE = Path(__file__).with_name("window_act_smoke.py")
HELPERS = {"Report", "_pixels", "changed_fraction", "content_shift", "_describe", "drive", "pick_window"}
CONSTANTS = {"CHANGED", "MENUS", "SETTLE", "CHECK_FILE"}


def smoke_helpers():
    source = ast.parse(SMOKE.read_text(), filename=str(SMOKE))
    source.body = [node for node in source.body if getattr(node, "name", None) in HELPERS
                   or (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                       and node.targets[0].id in CONSTANTS)]
    scope = dict(b64decode=b64decode, BytesIO=BytesIO, time=SimpleNamespace(sleep=lambda _: None))
    exec(compile(source, str(SMOKE), "exec"), scope)
    return SimpleNamespace(**scope)


def shot(image):
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return {"image_b64": b64encode(buffer.getvalue()).decode(), "size": list(image.size)}


def page():
    document = Image.new("L", (400, 600), "white")
    draw = ImageDraw.Draw(document)
    rng = Random(7)
    for y in range(0, 580, 14):
        draw.text((24, y), f"line {y} " + "note " * rng.randrange(1, 8), fill="black")
    return document


def view(document, top):
    return shot(document.crop((0, top, 400, top + 240)))


def run(smoke, pictures):
    images = iter(pictures)

    def ask(op, **fields):
        if op == "window_screenshot":
            return next(images)
        if op == "window_probe":
            return {"name": "Text", "role": "edit"}
        return {}

    output = []
    report = smoke.Report(out=output.append)
    smoke.drive(ask, "fake-window", report)
    return report, "\n".join(output)


class SmokeJudgementCheck(unittest.TestCase):
    def setUp(self):
        self.smoke = smoke_helpers()
        self.document = page()

    def test_a_scroll_is_measured_in_both_directions(self):
        top, lower = view(self.document, 0), view(self.document, 28)
        self.assertEqual(self.smoke.content_shift(top, lower), 28)
        self.assertEqual(self.smoke.content_shift(lower, top), -28)
        self.assertEqual(self.smoke.content_shift(top, top), 0)

    def test_down_then_back_up_passes(self):
        top, lower = view(self.document, 0), view(self.document, 28)
        report, output = run(self.smoke, (top, top, lower, top))
        self.assertEqual(report.failed, [], output)

    def test_a_file_opened_at_its_end_still_passes(self):
        end, higher = view(self.document, 300), view(self.document, 272)
        report, output = run(self.smoke, (end, end, end, higher))
        self.assertEqual(report.failed, [], output)

    def test_a_reversed_wheel_fails(self):
        top, lower = view(self.document, 0), view(self.document, 28)
        report, output = run(self.smoke, (lower, lower, top, lower))
        self.assertIn("a negative amount scrolls down and a positive one up", report.failed, output)

    def test_a_page_that_does_not_come_back_fails(self):
        top, lower = view(self.document, 0), view(self.document, 28)
        report, output = run(self.smoke, (top, top, lower, lower))
        self.assertEqual(report.failed, ["the opposite turn moved the page back"], output)

    def test_a_wheel_that_moves_nothing_fails(self):
        top = view(self.document, 0)
        report, output = run(self.smoke, (top, top, top, top))
        self.assertEqual(report.failed, ["the wheel moved the page"], output)

    def test_a_resize_between_pictures_is_not_a_scroll(self):
        before = shot(Image.new("L", (400, 240), "white"))
        after = shot(Image.new("L", (400, 280), "white"))
        self.assertIsNone(self.smoke.content_shift(before, after))

    def test_only_a_window_showing_the_check_file_is_picked(self):
        pick = self.smoke.pick_window

        def window(name, title, app="notepad.exe", access="drivable"):
            return {"window": name, "app": app, "access": access, "title": title}

        other = window("old", "Meeting notes - Notepad")
        ours = window("new", "scroll-check.txt - Notepad")
        self.assertIsNone(pick([other], [other]))
        self.assertEqual(pick([other], [other, ours])["window"], "new")
        # Notepad opened the file as a tab in a window that was already there.
        tabbed = dict(other, title="Scroll-Check.txt - Notepad")
        self.assertEqual(pick([other], [tabbed])["window"], "old")
        self.assertIsNone(pick([], [window("x", "scroll-check.txt - Editor", app="chrome.exe")]))
        self.assertIsNone(pick([], [window("x", "scroll-check.txt - Notepad", access="see_only")]))
        self.assertIsNone(pick([], [window("x", None)]))


if __name__ == "__main__":
    unittest.main()
