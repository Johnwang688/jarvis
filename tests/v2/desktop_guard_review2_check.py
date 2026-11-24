"""Second review of CU-1's desktop guard (fix round jarvis/cu1-fix, head 21aa18fc).

Each test is one finding and fails against the reviewed head. The fakes here
are written for this review, not shared with the other guard tests.
"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from windows import windowpolicy as policy
from windows.desktopguard import Grant, Guard, MaskedTree, Rect, Refused
from windows.uiatree import StaleRef, resolve, snapshot
from windows.windowpolicy import Window

WINDOW = Rect(0, 0, 400, 300)


class Desk:
    """One ordinary drivable window (hwnd 1) in the foreground."""

    def __init__(self):
        self.order = [1]
        self.facts_ = {1: Window(r"C:\Windows\notepad.exe", False, "Notepad")}

    def top_windows(self):
        return list(self.order)

    def facts(self, hwnd):
        return self.facts_[hwnd]

    def rect(self, hwnd):
        return WINDOW

    def see_through(self, hwnd):
        return False

    def title(self, hwnd):
        return "Notes"

    def pid(self, hwnd):
        return 4000

    def is_hud(self, hwnd):
        return False

    def hud_regions(self, hwnd):
        return []

    def foreground(self):
        return 1

    def window_at(self, x, y):
        return 1


class WindowsKeySpellings(unittest.TestCase):
    """Finding 1 (LOW). Rule 1 refuses any chord with a Windows-key name, but
    the guard matches only eight exact names ("win", "lwin", ...). Other
    spellings of the same key pass as an ordinary single key. The bridge's
    key table has no Windows key today, so nothing reaches SendInput; the
    guard's own rule is still not what it says."""

    def test_windows_key_spellings_are_refused(self):
        desk = Desk()
        guard = Guard(desk)
        window = guard.windows()[0]["window"]
        for chord in (["VK_LWIN"], ["LWinKey"], ["{LWIN}"], ["win+r"], ["Left Win"],
                      ["lwin\u200b"]):
            with self.subTest(chord=chord), self.assertRaises(Refused):
                guard.act(window, keys=chord, focus=Rect(10, 10, 20, 20))


def node(name, rect, *, role="group", interactive=False, kids=()):
    return {"name": name, "rect": rect, "role": role, "interactive": interactive,
            "kids": list(kids)}


class Tree:
    def __init__(self, kids):
        self.root = node("root", (0, 0, 400, 300), kids=kids)

    def children(self, n):
        return list(n["kids"])

    def describe(self, n):
        return {"role": n["role"], "name": n["name"], "value": "", "checked": None,
                "offscreen": False, "enabled": True, "interactive": n["interactive"],
                "rect": n["rect"]}


MASK = Rect(200, 100, 300, 200)


def touches(rect, mask):
    left, top, right, bottom = rect
    return (left <= mask.right and mask.left <= right
            and top <= mask.bottom and mask.top <= bottom)


class RefResolvesBesideAMask(unittest.TestCase):
    """Finding 2 (LOW). A node that touches a mask is kept as a nameless
    "group" placeholder. `resolve` matches on role and name, so a ref taken
    for an unnamed interactive group resolves to the placeholder once the
    tree shifts: the ref lands on a node whose own bounds touch the mask.
    `Grant.check_control` tests strict overlap, so a placeholder that only
    shares an edge with the padded mask is then clicked."""

    def test_ref_does_not_resolve_to_a_node_touching_a_mask(self):
        button = node("", (10, 10, 50, 40), interactive=True)
        before = Tree([button])
        _, refs = snapshot(MaskedTree(before, (MASK,)))
        entry = refs["e1"]
        self.assertEqual(entry, {"path": [0], "role": "group", "name": ""})
        # The button goes; a panel whose right edge meets the mask's left
        # edge moves into its slot.
        beside = node("Account panel", (100, 100, 200, 200),
                      kids=[node("Label", (110, 110, 150, 130))])
        after = Tree([beside])
        # Lead's rewrite after the fix: the placeholder now has a role no ref
        # can carry, so the ref is stale instead of resolving beside the mask,
        # and the geometry check refuses a control that shares an edge with it.
        with self.assertRaises(StaleRef):
            resolve(MaskedTree(after, (MASK,)), entry)
        with self.assertRaises(Refused):
            Grant(1, WINDOW, (MASK,)).check_control(Rect(100, 100, 200, 200))


class CoverageForSurvivingMutants(unittest.TestCase):
    """Pass on the reviewed head; each kills a mutant the existing tests miss."""

    def test_desktop_without_see_through_counts_windows_behind(self):
        # Mutant: the missing-method fallback for see_through says "opaque".
        desk = NoSeeThroughDesk()
        desk.order = [1, 2]
        desk.facts_[2] = Window(r"C:\Program Files\KeePass\KeePass.exe", False, "KeePass")
        guard = Guard(desk)
        window = guard.windows()[0]["window"]
        with self.assertRaises(Refused):
            guard.capture(window)

    def test_lone_leaf_touching_a_mask_is_never_a_ref_target(self):
        # Mutant: keep an overlapping leaf as a nameless placeholder.
        button = node("", (10, 10, 50, 40), interactive=True)
        _, refs = snapshot(MaskedTree(Tree([button]), (MASK,)))
        after = Tree([node("", (150, 120, 220, 160))])
        with self.assertRaises(StaleRef):
            resolve(MaskedTree(after, (MASK,)), refs["e1"])


class NoSeeThroughDesk(Desk):
    @property
    def see_through(self):
        raise AttributeError("see_through")


if __name__ == "__main__":
    unittest.main(verbosity=2)
