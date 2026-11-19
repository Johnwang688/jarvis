"""Exercise the desktop guard without Windows or a running bridge."""

from __future__ import annotations

import itertools
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from windows.desktopguard import Grant, Guard, MaskedTree, Rect, Refused, black_out, require_expected_control
from windows.uiatree import snapshot
from windows.windowpolicy import DRIVABLE, SEE_ONLY, SEE_ONLY_MESSAGE, Window

TARGET = Rect(0, 0, 100, 100)
HUD_REGION = Rect(40, 40, 60, 60)
SECRET = "Credential window — account recovery secret"


class FakeDesktop:
    def __init__(self):
        self.order = [10]
        self.info = {10: Window(r"C:\Windows\Notepad.exe", False, "Notepad")}
        self.pids = {10: 100}
        self.bounds = {10: TARGET}
        self.titles = {10: "Notes"}
        self.hud = set()
        self.regions = {}
        self.front = 10
        self.hit = 10

    def top_windows(self):
        return list(self.order)

    def facts(self, hwnd):
        return self.info[hwnd]

    def rect(self, hwnd):
        return self.bounds[hwnd]

    def title(self, hwnd):
        return self.titles[hwnd]

    def pid(self, hwnd):
        return self.pids.get(hwnd)

    def is_hud(self, hwnd):
        return hwnd in self.hud

    def hud_regions(self, hwnd):
        return self.regions.get(hwnd)

    def foreground(self):
        return self.front

    def window_at(self, x, y):
        return self.hit

    def add(self, hwnd, facts, bounds, title, *, front=True):
        self.order.insert(0 if front else len(self.order), hwnd)
        self.info[hwnd] = facts
        self.pids[hwnd] = 100 + hwnd
        self.bounds[hwnd] = bounds
        self.titles[hwnd] = title


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.desktop = FakeDesktop()
        numbers = itertools.count(1)
        self.guard = Guard(self.desktop, extra_password_managers=["Private.exe"],
                           new_id=lambda: f"{next(numbers):016x}")
        self.window = self.guard.windows()[0]["window"]

    def make_hud(self):
        self.desktop.hud.add(10)
        self.desktop.regions[10] = [HUD_REGION]

    def test_opaque_ids_and_z_order(self):
        guard = Guard(self.desktop)
        self.desktop.add(20, Window("powershell.exe", False), TARGET, "Shell")
        first = guard.windows()
        self.assertEqual([(entry["title"], entry["app"], entry["access"]) for entry in first],
                         [("Shell", "powershell.exe", SEE_ONLY), ("Notes", "notepad.exe", DRIVABLE)])
        ids = [entry["window"] for entry in first]
        self.assertEqual(len(set(ids)), 2)
        for window_id in ids:
            self.assertRegex(window_id, r"^[0-9a-f]{16}$")
        self.desktop.order.reverse()
        self.assertEqual([entry["window"] for entry in guard.windows()], ids[::-1])
        for unknown in (None, 10, "10", "missing", ["bad"]):
            with self.subTest(unknown=unknown), self.assertRaises(Refused):
                guard.capture(unknown)

    def test_gone_ids_stay_revoked(self):
        self.desktop.order.clear()
        with self.assertRaises(Refused):
            self.guard.capture(self.window)
        with self.assertRaises(Refused):
            self.guard.act(self.window, point=(2, 2))
        self.assertEqual(self.guard.windows(), [])
        self.desktop.order.append(10)
        self.assertNotEqual(self.guard.windows()[0]["window"], self.window)
        with self.assertRaises(Refused):
            self.guard.capture(self.window)

    def test_recycled_handle_rechecks_pid_and_full_image(self):
        original = self.desktop.info[10]
        for pid, facts in (
            (101, original),
            (100, Window(r"C:\Other\Notepad.exe", False, "Notepad")),
            (101, Window("wordpad.exe", False)),
        ):
            with self.subTest(pid=pid, facts=facts):
                self.desktop.pids[10] = pid
                self.desktop.info[10] = facts
                with self.assertRaises(Refused):
                    self.guard.capture(self.window)
                with self.assertRaises(Refused):
                    self.guard.act(self.window, point=(2, 2))
                self.assertNotEqual(self.guard.windows()[0]["window"], self.window)
                with self.assertRaises(Refused):
                    self.guard.capture(self.window)
                self.desktop.pids[10] = 100
                self.desktop.info[10] = original
                self.window = self.guard.windows()[0]["window"]

    def test_policy_changes_at_use_and_title_stays_private(self):
        self.desktop.titles[10] = SECRET
        self.desktop.info[10] = Window(self.desktop.info[10].image, True)
        with patch.object(self.desktop, "title", side_effect=AssertionError(SECRET)):
            self.assertEqual(self.guard.windows(), [])
        for call in (lambda: self.guard.capture(self.window),
                     lambda: self.guard.act(self.window, point=(2, 2))):
            with self.assertRaisesRegex(Refused, "protected") as exc:
                call()
            self.assertNotIn(SECRET, str(exc.exception))
        self.desktop.info[10] = Window(self.desktop.info[10].image, False, "ConsoleWindowClass")
        self.assertEqual(self.guard.windows()[0]["window"], self.window)
        self.assertEqual(self.guard.capture(self.window).rect, TARGET)
        with self.assertRaisesMessage(Refused, SEE_ONLY_MESSAGE):
            self.guard.act(self.window, point=(2, 2))

    def assertRaisesMessage(self, exc_type, message):
        return self.assertRaisesRegex(exc_type, f"^{re.escape(message)}$")

    def test_extra_manager_and_unreadable_facts_are_hidden(self):
        for hwnd, facts in (
            (20, Window("Private.exe", False)),
            (21, Window("other.exe", None)),
            (22, Window("other.exe", False, None)),
            (23, Window(None, False)),
            (24, Window("other.exe", False, own=True)),
            (25, Window("other.exe", False)),
        ):
            self.desktop.add(hwnd, facts, TARGET, SECRET)
        self.desktop.pids[25] = None
        self.assertEqual([entry["title"] for entry in self.guard.windows()], ["Notes"])

    def test_capture_checks_intersection_and_only_windows_in_front(self):
        self.desktop.add(20, Window("keepass.exe", False), Rect(99, 99, 200, 200), SECRET)
        with self.assertRaisesRegex(Refused, "protected window") as exc:
            self.guard.capture(self.window)
        self.assertNotIn(SECRET, str(exc.exception))
        self.desktop.bounds[20] = Rect(100, 99, 200, 200)
        self.assertEqual(self.guard.capture(self.window).masks, ())
        self.desktop.bounds[20] = TARGET
        self.desktop.order.reverse()
        self.assertEqual(self.guard.capture(self.window).masks, ())
        self.desktop.order.reverse()
        self.desktop.info[20] = Window("cmd.exe", False)
        self.assertEqual(self.guard.capture(self.window).masks, ())

    def test_unreadable_rects_fail_closed(self):
        self.desktop.add(20, Window("paint.exe", False), None, "Paint")
        with self.assertRaisesRegex(Refused, "protected window"):
            self.guard.capture(self.window)
        self.desktop.order.reverse()
        self.assertEqual(self.guard.capture(self.window).rect, TARGET)
        for unreadable in (None, Rect(1, 1, 1, 30), Rect(False, 0, 100, 100)):
            self.desktop.bounds[10] = unreadable
            with self.subTest(unreadable=unreadable), self.assertRaises(Refused):
                self.guard.capture(self.window)
            with self.assertRaises(Refused):
                self.guard.act(self.window, point=(2, 2))

    def test_point_bounds_and_topmost_owner(self):
        for point in ((0, 0), (99, 99)):
            self.assertEqual(self.guard.act(self.window, point=point).hwnd, 10)
        for point in ((-1, 0), (0, -1), (100, 0), (0, 100), (True, 3), (2.0, 2), (2,)):
            with self.subTest(point=point), self.assertRaises(Refused):
                self.guard.act(self.window, point=point)
        self.desktop.hit = 20
        with self.assertRaisesRegex(Refused, "covers"):
            self.guard.act(self.window, point=(2, 2))
        self.desktop.hit = None
        with self.assertRaises(Refused):
            self.guard.act(self.window, point=(2, 2))

    def test_act_needs_input_and_checks_foreground_and_key_chords(self):
        with self.assertRaises(Refused):
            self.guard.act(self.window, focus=TARGET)
        self.assertEqual(self.guard.act(self.window, keys=("Ctrl", "A")).hwnd, 10)
        self.assertEqual(self.guard.act(self.window, keys=["Delete"]).hwnd, 10)
        for keys in (["Shift", "Delete"], ["CTRL", " RShift ", "DEL"], [None], "Shift+Delete"):
            with self.subTest(keys=keys), self.assertRaisesRegex(Refused, "key combination"):
                self.guard.act(self.window, keys=keys)
        self.desktop.front = 20
        with self.assertRaisesRegex(Refused, "foreground"):
            self.guard.act(self.window, keys=["A"])
        self.assertEqual(self.guard.act(self.window, point=(2, 2)).hwnd, 10)

    def test_hud_count_and_rectangles(self):
        self.make_hud()
        self.assertEqual(self.guard.windows(), [])
        with self.assertRaises(Refused):
            self.guard.capture(self.window)
        for unknown in (None, True, -1, "1", 0.0):
            self.guard.set_hud(unknown)
            with self.subTest(unknown=unknown), self.assertRaises(Refused):
                self.guard.act(self.window, point=(2, 2))
        self.guard.set_hud(0)
        self.desktop.regions[10] = None
        with patch.object(self.desktop, "hud_regions", side_effect=AssertionError):
            self.assertEqual(self.guard.windows()[0]["access"], DRIVABLE)
            self.assertEqual(self.guard.capture(self.window).masks, ())
            self.assertEqual(self.guard.act(self.window, point=(50, 50)).masks, ())
        self.guard.set_hud(1)
        with self.assertRaises(Refused):
            self.guard.capture(self.window)
        self.desktop.regions[10] = [HUD_REGION]
        self.assertEqual(self.guard.capture(self.window).masks, (Rect(32, 32, 68, 68),))
        self.guard.set_hud(2)
        with self.assertRaises(Refused):
            self.guard.capture(self.window)
        self.desktop.regions[10].append(Rect(10, 10, 20, 20))
        self.assertEqual(len(self.guard.capture(self.window).masks), 2)
        self.desktop.regions[10][1] = Rect(10, 10, 10, 20)
        with self.assertRaises(Refused):
            self.guard.capture(self.window)

    def test_hud_cannot_relax_base_class(self):
        self.make_hud()
        image = self.desktop.info[10].image
        self.desktop.info[10] = Window(image, False, "ConsoleWindowClass")
        self.guard.set_hud(1)
        self.assertEqual(self.guard.windows()[0]["access"], SEE_ONLY)
        self.assertEqual(self.guard.capture(self.window).masks, (Rect(32, 32, 68, 68),))
        with self.assertRaisesMessage(Refused, SEE_ONLY_MESSAGE):
            self.guard.act(self.window, keys=["A"], focus=Rect(0, 0, 10, 10))
        self.desktop.info[10] = Window(image, True)
        self.guard.set_hud(0)
        self.assertEqual(self.guard.windows(), [])
        with self.assertRaises(Refused):
            self.guard.capture(self.window)

    def test_hud_in_front_blocks_or_contributes_only_intersecting_masks(self):
        self.desktop.add(20, Window("chrome.exe", False), Rect(40, 40, 250, 250), "HUD")
        self.desktop.hud.add(20)
        self.desktop.regions[20] = [Rect(95, 95, 105, 105), Rect(200, 200, 210, 210)]
        with self.assertRaises(Refused):
            self.guard.capture(self.window)
        self.guard.set_hud(0)
        self.assertEqual(self.guard.capture(self.window).masks, ())
        self.guard.set_hud(2)
        self.assertEqual(self.guard.capture(self.window).masks, (Rect(87, 87, 113, 113),))
        self.desktop.regions[20] = None
        with self.assertRaises(Refused):
            self.guard.capture(self.window)

    def test_hud_point_element_and_focus_guards(self):
        self.make_hud()
        self.guard.set_hud(1)
        for point in ((40, 50), (59, 50), (50, 40), (50, 59), (60, 50), (50, 60),
                      (32, 50), (67, 50), (50, 32), (50, 67)):
            with self.subTest(point=point), self.assertRaisesRegex(Refused, "protected area"):
                self.guard.act(self.window, point=point)
        for point in ((31, 50), (68, 50), (50, 31), (50, 68)):
            self.assertEqual(self.guard.act(self.window, point=point).hwnd, 10)
        safe = Rect(10, 10, 32, 32)
        crossing = Rect(10, 10, 33, 33)
        self.assertEqual(self.guard.act(self.window, element=safe).hwnd, 10)
        with self.assertRaises(Refused):
            self.guard.act(self.window, point=(20, 20), element=crossing)
        self.assertEqual(self.guard.act(self.window, keys=["A"], focus=safe).hwnd, 10)
        for focus in (None, crossing):
            with self.subTest(focus=focus), self.assertRaisesRegex(Refused, "Focus"):
                self.guard.act(self.window, keys=["A"], focus=focus)

    def test_each_desktop_method_fails_closed_without_exception_text(self):
        self.make_hud()
        self.guard.set_hud(1)
        calls = {
            "top_windows": self.guard.windows,
            "facts": lambda: self.guard.capture(self.window),
            "rect": lambda: self.guard.capture(self.window),
            "title": self.guard.windows,
            "pid": lambda: self.guard.capture(self.window),
            "is_hud": lambda: self.guard.capture(self.window),
            "hud_regions": lambda: self.guard.capture(self.window),
            "foreground": lambda: self.guard.act(self.window, keys=["A"]),
            "window_at": lambda: self.guard.act(self.window, point=(2, 2)),
        }
        for method, call in calls.items():
            for failure in (RuntimeError(SECRET), Refused(SECRET)):
                with self.subTest(method=method, failure=type(failure)):
                    with patch.object(self.desktop, method, side_effect=failure):
                        with self.assertRaises(Refused) as exc:
                            call()
                        self.assertNotIn(SECRET, str(exc.exception))
                        self.assertIsNone(exc.exception.__cause__)


class RedactionTests(unittest.TestCase):
    def test_probe_geometry_and_input_expectation(self):
        grant = Grant(10, Rect(-50, 20, 10, 80), (Rect(-20, 40, 0, 60),))
        self.assertEqual(grant.screenshot_point(5, 3), (-45, 23))
        with self.assertRaises(Refused):
            grant.screenshot_point(60, 3)
        with self.assertRaises(Refused):
            grant.screenshot_point(30, 20)
        bounds = Rect(-48, 20, -40, 29)
        grant.check_control(bounds, (-45, 23))
        with self.assertRaises(Refused):
            grant.check_control(bounds, (-38, 23))
        with self.assertRaises(Refused):
            grant.check_control(Rect(-22, 40, -19, 60))

        live = {"name": "Save", "role": "button", "rect": bounds}
        require_expected_control({"name": "Save", "role": "button"}, live)
        for expect in (None, {}, {"name": "Delete", "role": "button"},
                       {"name": "Save", "role": "link"}):
            with self.subTest(expect=expect), self.assertRaises(Refused):
                require_expected_control(expect, live)

    def test_snapshot_prunes_subtrees_before_rendering_or_assigning_refs(self):
        root = {"rect": [0, 0, 1, 1], "children": [
            {"name": "outside", "rect": [0, 0, 3, 3], "children": []},
            {"name": SECRET, "rect": [4, 4, 8, 8], "children": [
                {"name": "secret descendant", "rect": [10, 10, 20, 20], "children": []},
            ]},
            {"name": "unreadable", "rect": [0, 0, 0, 0], "children": []},
        ]}

        class Backend:
            def children(self, node):
                return node["children"]

            def describe(self, node):
                return {"name": node.get("name", ""), "rect": node["rect"], "role": "button",
                        "interactive": True, "value": "", "offscreen": False,
                        "enabled": True, "checked": None}

        backend = Backend()
        backend.root = root
        mask = Rect(3, 3, 5, 5)
        text, refs = snapshot(MaskedTree(backend, (mask,)), ref_prefix="opaque-e")
        self.assertIn("outside", text)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("descendant", text)
        self.assertNotIn("unreadable", text)
        self.assertEqual({ref: info["name"] for ref, info in refs.items()}, {"opaque-e1": "outside"})
        unmasked, refs = snapshot(MaskedTree(backend, ()))
        self.assertIn(SECRET, unmasked)
        self.assertIn("secret descendant", unmasked)
        self.assertEqual(len(refs), 4)
        root["rect"] = [0, 0, 20, 20]
        self.assertEqual(snapshot(MaskedTree(backend, (mask,)))[1], {})

    def test_image_masks_are_translated_clipped_and_half_open(self):
        class Image:
            width = 5
            height = 4

            def __init__(self):
                self.pixels = [[1] * self.width for _ in range(self.height)]

            def paste(self, color, box):
                left, top, right, bottom = box
                for y in range(top, bottom):
                    for x in range(left, right):
                        self.pixels[y][x] = color

        image = Image()
        black_out(image, (-5, 30), (
            Rect(-10, 20, -3, 32), Rect(-2, 33, 8, 40), Rect(0, 30, 1, 35),
        ))
        self.assertEqual(image.pixels, [
            [0, 0, 1, 1, 1],
            [0, 0, 1, 1, 1],
            [1, 1, 1, 1, 1],
            [1, 1, 1, 0, 0],
        ])


if __name__ == "__main__":
    unittest.main()
