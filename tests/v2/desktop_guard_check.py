"""Exercise the desktop guard without Windows or a running bridge."""

from __future__ import annotations

import itertools
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from windows.desktopguard import (
    Grant, Guard, MaskedTree, Rect, Refused, black_out, own_image, require_expected_control,
)
from windows.uiatree import resolve, snapshot
from windows import windowpolicy as policy
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
        self.transparent = set()

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

    def test_repeating_id_cannot_be_reissued_to_another_live_window(self):
        ids = iter(("repeated", "repeated", "another"))
        guard = Guard(self.desktop, new_id=lambda: next(ids))
        original = guard.windows()[0]["window"]
        self.desktop.add(20, Window("paint.exe", False), TARGET, "Paint")
        with self.assertRaisesRegex(Refused, "listed safely"):
            guard.windows()
        windows = {entry["title"]: entry["window"] for entry in guard.windows()}
        self.assertEqual(windows, {"Notes": original, "Paint": "another"})
        self.assertEqual(guard.capture(original).hwnd, 10)

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

    def test_owner_manager_input_is_used_as_given(self):
        names = (name for name in ["private.exe"])
        for value in ("private.exe", 5, names):
            with self.subTest(value=value):
                guard = Guard(self.desktop, extra_password_managers=value)
                self.assertEqual(guard.windows(), [])
                self.assertEqual(guard.windows(), [])
        self.assertEqual(next(names), "private.exe")
        editable = []
        guard = Guard(self.desktop, extra_password_managers=editable)
        window = guard.windows()[0]["window"]
        editable.append("notepad.exe")
        with self.assertRaisesRegex(Refused, "protected"):
            guard.capture(window)

    def test_capture_checks_intersection_in_front_and_behind(self):
        self.desktop.add(20, Window("keepass.exe", False), Rect(99, 99, 200, 200), SECRET)
        with self.assertRaisesRegex(Refused, "protected window") as exc:
            self.guard.capture(self.window)
        self.assertNotIn(SECRET, str(exc.exception))
        self.desktop.bounds[20] = Rect(100, 99, 200, 200)
        self.assertEqual(self.guard.capture(self.window).masks, ())
        self.desktop.bounds[20] = TARGET
        self.desktop.order.reverse()
        with self.assertRaisesRegex(Refused, "protected window"):
            self.guard.capture(self.window)
        self.desktop.order.reverse()
        self.desktop.info[20] = Window("cmd.exe", False)
        self.assertEqual(self.guard.capture(self.window).masks, ())

    def test_unreadable_rects_fail_closed(self):
        self.desktop.add(20, Window("paint.exe", False), None, "Paint")
        with self.assertRaisesRegex(Refused, "protected window"):
            self.guard.capture(self.window)
        self.desktop.order.reverse()
        with self.assertRaisesRegex(Refused, "protected window"):
            self.guard.capture(self.window)
        self.desktop.bounds[20] = Rect(500, 500, 600, 600)
        self.assertEqual(self.guard.capture(self.window).rect, TARGET)
        for unreadable in (None, Rect(1, 1, 1, 30), Rect(False, 0, 100, 100)):
            self.desktop.bounds[10] = unreadable
            with self.subTest(unreadable=unreadable), self.assertRaises(Refused):
                self.guard.capture(self.window)
            with self.assertRaises(Refused):
                self.guard.act(self.window, point=(2, 2))

    def test_checks_protected_window_below_frontmost_window(self):
        self.desktop.add(20, Window("keepass.exe", False), TARGET, SECRET)
        self.desktop.add(30, Window("paint.exe", False), TARGET, "Paint")
        with self.assertRaisesRegex(Refused, "protected window"):
            self.guard.capture(self.window)

    def test_windows_behind_the_target_count_whatever_it_says_about_itself(self):
        self.desktop.add(20, Window("keepass.exe", False), TARGET, SECRET, front=False)
        with self.assertRaisesRegex(Refused, "protected window"):
            self.guard.capture(self.window)
        self.desktop.transparent.add(10)
        for bounds in (TARGET, None):
            self.desktop.bounds[20] = bounds
            with self.subTest(bounds=bounds), self.assertRaisesRegex(Refused, "protected window"):
                self.guard.capture(self.window)
        self.desktop.bounds[20] = Rect(100, 0, 200, 100)
        self.assertEqual(self.guard.capture(self.window).rect, TARGET)
        with patch.object(self.desktop, "occlusion_rect", create=True, return_value=TARGET):
            with self.assertRaisesRegex(Refused, "protected window"):
                self.guard.capture(self.window)

    def test_a_window_with_no_area_covers_nothing(self):
        # Found by the first real run (2026-11-19): the bridge's own hidden
        # zero-size PseudoConsoleWindow refused every capture on the desktop.
        self.desktop.add(20, Window("python.exe", False, "PseudoConsoleWindow", True), TARGET, SECRET, front=False)
        for bounds in (Rect(0, 0, 0, 0), Rect(5, 5, 5, 40), Rect(5, 5, 40, 5)):
            self.desktop.bounds[20] = bounds
            with self.subTest(bounds=bounds):
                self.assertEqual(self.guard.capture(self.window).rect, TARGET)
        # Unreadable, reversed or out-of-range frames still protect.
        for bounds in (None, Rect(40, 5, 5, 40), Rect(0, 0, 2 ** 31, 0), Rect(0, 0, True, 0), (0, 0, 0, 0)):
            self.desktop.bounds[20] = bounds
            with self.subTest(bounds=bounds), self.assertRaisesRegex(Refused, "protected window"):
                self.guard.capture(self.window)

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

    def test_key_names_are_a_closed_ascii_vocabulary(self):
        self.desktop.front = 10 if hasattr(self.desktop, "front") else None
        for keys in (["a"], ["Ctrl", "A"], [" SHIFT\t", "f5"], ["alt", "left"]):
            with self.subTest(keys=keys):
                try:
                    self.guard.act(self.window, keys=keys)
                except Refused as exc:
                    self.assertNotIn("key combination", str(exc))
        for keys in (["ctrl", "ctrl", "a"], ["shift", "Shift", "a"], ["\u212a"], ["ctrl", "\u212a"],
                     ["VK_LWIN"], ["win"], ["lwin", "r"], ["win+r"], ["ctrl+a"], ["{Enter}"],
                     ["menu"], ["apps"], ["option", "a"], [""], ["a", ""]):
            with self.subTest(refused=keys), self.assertRaisesRegex(Refused, "key combination"):
                self.guard.act(self.window, keys=keys)

    def test_switcher_and_start_chords_are_refused(self):
        for keys in (["alt", "tab"], ["ctrl", "alt", "tab"], ["shift", "alt", "tab"], ["ralt", "tab"],
                     ["altgr", "tab"], ["alt", "esc"], ["alt", "escape"], ["ctrl", "esc"],
                     ["control", "escape"], ["rctrl", "shift", "esc"]):
            with self.subTest(refused=keys), self.assertRaisesRegex(Refused, "key combination"):
                self.guard.act(self.window, keys=keys)
        for keys in (["tab"], ["ctrl", "tab"], ["shift", "tab"], ["esc"], ["alt", "left"], ["shift", "escape"]):
            with self.subTest(allowed=keys):
                try:
                    self.guard.act(self.window, keys=keys)
                except Refused as exc:
                    self.assertNotIn("key combination", str(exc))

    def test_window_switcher_surfaces_are_protected(self):
        for window_class in ("XamlExplorerHostIslandWindow", "MultitaskingViewFrame", "TaskListThumbnailWnd",
                             "TaskSwitcherWnd", " tasklistoverlaywnd "):
            with self.subTest(window_class=window_class):
                self.assertEqual(policy.classify(Window(r"C:\Windows\explorer.exe", False, window_class)),
                                 policy.PROTECTED)
        self.assertEqual(policy.classify(Window(r"C:\Windows\explorer.exe", False, "CabinetWClass")),
                         policy.DRIVABLE)

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

    def test_keys_on_ordinary_windows_require_exactly_one_non_modifier(self):
        modifiers = ("shift", "lshift", "rshift", "ctrl", "control", "lctrl", "rctrl",
                     "alt", "lalt", "ralt", "altgr")
        windows_keys = ("win", "windows", "lwin", "rwin", "meta", "super", "cmd", "command")
        for keys in ([], ["shift"], modifiers, ["tab", "x"], ["shift", "tab", "x"],
                     *[(f" {key.upper()} ",) for key in windows_keys],
                     *[(f" {key.upper()} ", "r") for key in windows_keys]):
            with self.subTest(keys=keys), self.assertRaisesRegex(Refused, "key combination"):
                self.guard.act(self.window, keys=keys)
        for modifier in modifiers:
            with self.subTest(modifier=modifier):
                self.assertEqual(self.guard.act(self.window, keys=[f" {modifier.upper()} ", "A"]).hwnd, 10)

    def test_hud_count_and_rectangles(self):
        self.make_hud()
        self.assertEqual(self.guard.windows(), [])
        with self.assertRaises(Refused):
            self.guard.capture(self.window)
        for unknown in (None, True, -1, "1", 0.0):
            self.guard.set_hud(unknown)
            with self.subTest(unknown=unknown), patch.object(self.desktop, "hud_regions") as scan:
                with self.assertRaises(Refused):
                    self.guard.act(self.window, point=(2, 2))
                scan.assert_not_called()
        self.guard.set_hud(0)
        self.desktop.regions[10] = []
        with patch.object(self.desktop, "hud_regions", wraps=self.desktop.hud_regions) as scan_regions:
            self.assertEqual(self.guard.windows()[0]["access"], DRIVABLE)
            self.assertEqual(self.guard.capture(self.window).masks, ())
            self.assertEqual(self.guard.act(self.window, point=(50, 50)).masks, ())
            self.assertEqual(scan_regions.call_count, 3)
        for scan in ([HUD_REGION], None, "", b"", {}, [None]):
            with self.subTest(scan=scan):
                self.desktop.regions[10] = scan
                with self.assertRaises(Refused):
                    self.guard.capture(self.window)
                with self.assertRaises(Refused):
                    self.guard.act(self.window, point=(2, 2))
                self.assertEqual(self.guard.windows(), [])
        self.desktop.regions[10] = None
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

    def test_nested_hud_regions_hide_window_and_refuse_input_with_existing_id(self):
        self.make_hud()
        self.guard.set_hud(2)
        self.desktop.regions[10].append(Rect(45, 45, 55, 55))
        self.assertEqual(self.guard.windows(), [])
        with self.assertRaisesRegex(Refused, "protected"):
            self.guard.act(self.window, point=(2, 2))

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
        with self.assertRaises(Refused):
            self.guard.capture(self.window)
        self.desktop.regions[20] = []
        self.assertEqual(self.guard.capture(self.window).masks, ())
        self.guard.set_hud(2)
        self.desktop.regions[20] = [Rect(95, 95, 105, 105), Rect(200, 200, 210, 210)]
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

    def test_lazy_desktop_reads_fail_with_fixed_refusal(self):
        self.make_hud()
        self.guard.set_hud(1)

        def broken(first):
            yield first
            raise RuntimeError(SECRET)

        for method, first in (("top_windows", 10), ("hud_regions", HUD_REGION)):
            with self.subTest(method=method), patch.object(self.desktop, method, return_value=broken(first)):
                with self.assertRaisesMessage(Refused, "Windows could not be inspected safely."):
                    self.guard.capture(self.window)


class OwnImageTests(unittest.TestCase):
    def test_protects_bridge_home_and_both_python_installations(self):
        locations = dict(executable=r"C:\Launcher\python.exe", prefix=r"C:\Jarvis\Bridge\venv",
                         base_executable=r"D:\Launcher\python.exe", base_prefix=r"D:\Python")
        for path in ("c:/launcher/PYTHON.EXE", r"C:\Jarvis\Bridge\venv\Scripts\helper.exe",
                     r"C:\Jarvis\Bridge\helper.exe", r"C:\Jarvis\Bridge\venv\..\helper.exe",
                     r"d:\LAUNCHER\python.exe", r"\\?\D:\Python\Scripts\helper.exe"):
            with self.subTest(path=path):
                self.assertTrue(own_image(path, **locations))
        for path in (r"C:\Jarvis\BridgeBackup\helper.exe", r"C:\Jarvis\helper.exe",
                     r"D:\PythonBackup\python.exe", r"C:\Windows\notepad.exe"):
            with self.subTest(path=path):
                self.assertFalse(own_image(path, **locations))
        for path in (None, 5, "", "python.exe", r"C:python.exe", r"C:\JARVIS~1\helper.exe"):
            with self.subTest(path=path):
                self.assertTrue(own_image(path, **locations))
        self.assertTrue(own_image(r"C:\Windows\notepad.exe", **(locations | {"base_prefix": None})))


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

    def test_snapshot_keeps_only_safe_nodes_through_masked_containers(self):
        def node(name, rect, *children):
            return {"name": name, "rect": rect, "children": children}

        root = node("masked root", [0, 0, 20, 20],
                    node(SECRET, [4, 4, 7, 7], node("secret descendant", [10, 10, 15, 15])),
                    node("outside", [0, 0, 2, 2]),
                    node("masked container", [0, 0, 20, 10],
                         node("straddling leaf", [1, 1, 4, 4]),
                         node("touching leaf", [1, 3, 3, 5]),
                         node("unreadable", None, node("unreadable descendant", [9, 1, 10, 2])),
                         node("outside grandchild one", [10, 1, 12, 3]),
                         node("outside grandchild two", [13, 1, 15, 3])))

        class Backend:
            def children(self, node):
                return node["children"]

            def describe(self, node):
                return {"name": node["name"], "rect": node["rect"], "role": "button",
                        "interactive": True, "value": node["name"] + " value", "offscreen": False,
                        "enabled": True, "checked": None}

        backend = Backend()
        backend.root = root
        mask = Rect(3, 3, 8, 8)
        text, refs = snapshot(MaskedTree(backend, (mask,)), ref_prefix="opaque-e")
        expected = ["outside", "outside grandchild one", "outside grandchild two"]
        self.assertEqual(text.splitlines(), [
            f'[opaque-e{i}] button "{name}" value="{name} value"' for i, name in enumerate(expected, 1)
        ])
        self.assertEqual([entry["name"] for entry in refs.values()], expected)
        for entry in refs.values():
            self.assertEqual(resolve(MaskedTree(backend, (mask,)), entry)["name"], entry["name"])
        unmasked, refs = snapshot(MaskedTree(backend, ()))
        self.assertIn(SECRET, unmasked)
        self.assertIn("secret descendant", unmasked)
        self.assertEqual(len(refs), 10)
        root["rect"] = [4, 4, 7, 7]
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
