"""Review findings for CU-1's desktop guard (jarvis/cu1-guard, head 87bf8fe8).

Each test is one finding; it fails against the reviewed head. The fake desktop
here is written for the review, not shared with desktop_guard_check.py.
"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from windows import windowpolicy as policy
from windows.desktopguard import Guard, Rect, Refused
from windows.windowpolicy import Window

WINDOW = Rect(0, 0, 400, 300)
CARD = Rect(200, 100, 300, 200)          # the approval card the HUD labels
EDITOR = Rect(10, 200, 150, 290)         # the credential editor (second sensitive area)
OUTSIDE = Rect(10, 10, 60, 40)           # an ordinary HUD field


class Desk:
    """One HUD window (hwnd 1) and, optionally, other windows."""

    def __init__(self):
        self.order = [1]
        self.facts_ = {1: Window(r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                                 False, "Chrome_WidgetWin_1")}
        self.pids = {1: 4000}
        self.rects = {1: WINDOW}
        self.titles = {1: "J.A.R.V.I.S."}
        self.huds = {1}
        self.regions = {1: [CARD]}
        self.fg = 1

    def top_windows(self):
        return list(self.order)

    def facts(self, hwnd):
        return self.facts_[hwnd]

    def rect(self, hwnd):
        return self.rects[hwnd]

    def title(self, hwnd):
        return self.titles[hwnd]

    def pid(self, hwnd):
        return self.pids[hwnd]

    def is_hud(self, hwnd):
        return hwnd in self.huds

    def hud_regions(self, hwnd):
        return list(self.regions.get(hwnd, []))

    def foreground(self):
        return self.fg

    def window_at(self, x, y):
        return 1


def listed(guard):
    return {entry["app"]: entry["window"] for entry in guard.windows()}


class ChordIsOneKeyAtATime(unittest.TestCase):
    """Finding 1. A chord is checked against the focus once, but the bridge
    presses its keys in sequence (`_send_window_keys`: every key down in
    order, then up). On the HUD with masks, Tab moves focus before the next
    key of the same chord lands, so that key reaches whatever Tab focused,
    which the focus check never saw (the credential editor, for one)."""

    def test_focus_moving_key_with_another_key_is_refused_on_masked_hud(self):
        desk = Desk()
        guard = Guard(desk)
        guard.set_hud(1)
        hud = listed(guard)["chrome.exe"]
        for chord in (("tab", "x"), ("shift", "tab", "x"), ("tab", "space"),
                      ("tab", "enter")):
            with self.subTest(chord=chord), self.assertRaises(Refused):
                guard.act(hud, keys=chord, focus=OUTSIDE)


class OwnerListShape(unittest.TestCase):
    """Finding 2. Guard turns `extra_password_managers` into a tuple before
    the policy sees it, so one string becomes a tuple of letters: the shape
    check commit 68792304 added (a bare string is unreadable, so protect)
    never fires and the owner's password manager is listed as drivable."""

    def test_a_bare_string_does_not_unprotect_the_owners_manager(self):
        desk = Desk()
        desk.huds.clear()
        desk.facts_[1] = Window(r"C:\Apps\Private.exe", False, "Main")
        desk.titles[1] = "Vault"
        guard = Guard(desk, extra_password_managers="Private.exe")
        self.assertEqual(guard.windows(), [])


class OneAreaCannotStandForTwo(unittest.TestCase):
    """Finding 3. Equal rectangles count once (spec point of 68792304), but a
    labelled node nested inside another labelled node is a different
    rectangle, so one card can satisfy a count of two and the second
    sensitive area, which the HUD failed to label, is left unmasked."""

    def test_nested_regions_do_not_satisfy_two(self):
        inner = Rect(CARD.left + 4, CARD.top + 4, CARD.right - 4, CARD.bottom - 4)
        self.assertEqual(policy.hud_view(2, [CARD, inner], 0.0)[0], policy.PROTECTED)

    def test_unlabelled_editor_is_not_clickable(self):
        desk = Desk()
        inner = Rect(CARD.left + 4, CARD.top + 4, CARD.right - 4, CARD.bottom - 4)
        desk.regions[1] = [CARD, inner]
        guard = Guard(desk)
        guard.set_hud(2)
        hud = listed(guard)["chrome.exe"]
        with self.assertRaises(Refused):
            guard.act(hud, point=(EDITOR.left + 5, EDITOR.top + 5))


class LazyDesktopErrors(unittest.TestCase):
    """Finding 4 (LOW). `_read` wraps the call, not the iteration: a
    `top_windows` that yields lazily and then fails raises its own exception
    (with its own text) out of `Guard.windows`, not `Refused`."""

    def test_error_while_iterating_window_order_is_a_refusal(self):
        desk = Desk()
        guard = Guard(desk)

        def lazy():
            yield 1
            raise OSError("raw desktop error text")

        desk.top_windows = lazy
        with self.assertRaises(Refused):
            guard.windows()


class EveryFrontWindowCounts(unittest.TestCase):
    """Coverage for surviving mutant M16 (passes on the reviewed head): a
    protected window that is second in front, behind a harmless one, still
    refuses the capture."""

    def test_protected_window_second_in_front_refuses_capture(self):
        desk = Desk()
        desk.huds.clear()
        desk.facts_[1] = Window("notepad.exe", False, "Notepad")
        desk.order = [3, 2, 1]
        desk.facts_[2] = Window("keepass.exe", False, "KeePass")
        desk.facts_[3] = Window("mspaint.exe", False, "Paint")
        desk.pids.update({2: 2000, 3: 3000})
        desk.rects.update({2: Rect(5, 5, 50, 50), 3: Rect(300, 200, 390, 290)})
        desk.titles.update({2: "vault", 3: "paint"})
        guard = Guard(desk)
        notes = listed(guard)["notepad.exe"]
        with self.assertRaises(Refused):
            guard.capture(notes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
