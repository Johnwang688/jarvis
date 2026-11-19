"""Third review of CU-1's desktop guard (fix round jarvis/cu1-fix, head 4469d0e5).

The first three tests are findings and fail against the reviewed head. The
rest pin what this round closed, so a later change cannot quietly reopen it.
The bridge cannot be imported off Windows, so the bridge checks read its
source (ast) or execute its exact class text against ctypes stand-ins.
"""

from __future__ import annotations

import ast
import ctypes
import ctypes.wintypes as wt
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from windows import desktopguard as guard_module
from windows.desktopguard import KEY_NAMES, MASKED_STEP_ROLE, Grant, Guard, MaskedTree, Rect, Refused
from windows.uiatree import StaleRef, resolve, snapshot
from windows.windowpolicy import DRIVABLE, Window

BRIDGE = ROOT / "windows" / "bridge.py"
SCREEN = Rect(0, 0, 1920, 1080)


def _bridge_tree():
    return ast.parse(BRIDGE.read_text(encoding="utf-8"))


class Desk:
    """A fake desktop: hwnd 1 is a drivable Notepad in the foreground."""

    def __init__(self):
        self.order = [1]
        self.facts_ = {1: Window(r"C:\Windows\notepad.exe", False, "Notepad")}
        self.rects = {1: Rect(0, 0, 800, 600)}
        self.huds = {}

    def add(self, hwnd, facts, rect, *, front=True):
        self.order.insert(0 if front else len(self.order), hwnd)
        self.facts_[hwnd] = facts
        self.rects[hwnd] = rect

    def top_windows(self):
        return list(self.order)

    def facts(self, hwnd):
        return self.facts_[hwnd]

    def rect(self, hwnd):
        return self.rects[hwnd]

    def see_through(self, hwnd):
        return False

    def title(self, hwnd):
        return f"window {hwnd}"

    def pid(self, hwnd):
        return 4000 + hwnd

    def is_hud(self, hwnd):
        return hwnd in self.huds

    def hud_regions(self, hwnd):
        return self.huds.get(hwnd)

    def foreground(self):
        return 1

    def window_at(self, x, y):
        return 1


def _ids(guard):
    return {int(entry["title"].split()[1]): entry["window"] for entry in guard.windows()}


class FindingTests(unittest.TestCase):
    def test_1_bridge_binds_key_names_before_its_import_time_check(self):
        """HIGH. bridge.py:1408 reads KEY_NAMES, but bridge.py:52-55 imports
        only Guard, HUD_REGION_NAME, MaskedTree, Rect, Refused, black_out,
        own_image and require_expected_control from desktopguard. Importing
        the bridge raises NameError, so no bridge operation (window or the
        older app ops) can start."""
        tree = _bridge_tree()
        bound = set()
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                bound.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                bound.update(t.id for t in targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.Try):
                for inner in ast.walk(node):
                    if isinstance(inner, (ast.Import, ast.ImportFrom)):
                        bound.update((a.asname or a.name).split(".")[0] for a in inner.names)
        self.assertTrue("KEY_NAMES" in bound, "bridge.py uses KEY_NAMES at import but never binds it")

    def test_2_cloak_query_fits_the_declared_dwm_signature(self):
        """HIGH. bridge.py:993-994 declares DwmGetWindowAttribute's third
        argument as POINTER(RECT); _cloaked (bridge.py:1115-1116) passes
        byref(DWORD). ctypes raises ArgumentError on every call, so
        top_windows raises, the guard turns it into a refusal and every
        list, capture and input is refused. Executes the bridge's own
        WindowsDesktop text against ctypes stand-ins."""
        tree = _bridge_tree()
        wanted = [n for n in tree.body if isinstance(n, ast.ClassDef)
                  and n.name in ("_MonitorInfo", "WindowsDesktop")]
        self.assertEqual(len(wanted), 2)

        written = []

        def dwm_attribute(hwnd, attribute, pointer, size):
            if attribute == 14:
                ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))[0] = 2  # DWM_CLOAKED_SHELL
                written.append(size)
            return 0

        class FakeDLL:
            def __init__(self, name, use_last_error=False):
                self._functions = {}

            def __getattr__(self, name):
                if name.startswith("_"):
                    raise AttributeError(name)
                if name not in self._functions:
                    if name == "DwmGetWindowAttribute":
                        proto = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_uint32,
                                                 ctypes.c_void_p, ctypes.c_uint32)
                        self._functions[name] = proto(dwm_attribute)
                    else:
                        self._functions[name] = ctypes.CFUNCTYPE(ctypes.c_long)(lambda *a: 0)
                return self._functions[name]

        fake_ctypes = types.ModuleType("ctypes")
        fake_ctypes.__dict__.update(ctypes.__dict__)
        fake_ctypes.WinDLL = FakeDLL
        namespace = {"ctypes": fake_ctypes, "wt": wt, "__name__": "bridge_extract"}
        for node in wanted:
            exec(compile(ast.Module([node], []), str(BRIDGE), "exec"), namespace)
        desktop = namespace["WindowsDesktop"]()
        try:
            cloaked = desktop._cloaked(1234)
        except ctypes.ArgumentError as exc:
            self.fail(f"_cloaked cannot call DwmGetWindowAttribute as declared: {exc}")
        self.assertIs(cloaked, True)
        # wt.DWORD is c_ulong: 4 bytes on Windows (what DWM wants), 8 here.
        self.assertEqual(written, [ctypes.sizeof(wt.DWORD)])

    def test_3_bridge_focus_key_lists_pass_the_closed_vocabulary(self):
        """HIGH (functional). op_window_type checks focus before typing with
        verify_focus(["unicode text"]) (bridge.py:1472), which reaches
        Guard.act(keys=["unicode text"]). That name is not in KEY_NAMES, so
        typing any non-empty text into a field is always refused. Every key
        list the bridge passes the guard must be one the guard accepts."""
        literals = []
        for node in ast.walk(_bridge_tree()):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", getattr(node.func, "attr", ""))
            args = list(node.args) if name == "verify_focus" else []
            args += [kw.value for kw in node.keywords if kw.arg == "keys" and name == "_window_input_control"]
            for arg in args:
                if isinstance(arg, ast.List):
                    literals.append(ast.literal_eval(arg))
        self.assertIn(["ctrl", "a"], literals)
        desk = Desk()
        guard = Guard(desk)
        window = _ids(guard)[1]
        for keys in literals:
            with self.subTest(keys=keys):
                try:
                    guard.act(window, keys=keys, focus=Rect(10, 10, 50, 30))
                except Refused as exc:
                    self.fail(f"the bridge's own focus check {keys} is refused: {exc}")

    def test_4_shell_thumbnail_surfaces_do_not_show_protected_windows(self):
        """HIGH (proposed expectation; class names need Windows verification).
        Alt+Tab and Task View (explorer.exe, XamlExplorerHostIslandWindow on
        Windows 11, MultitaskingViewFrame on 10) and taskbar previews
        (TaskListThumbnailWnd) draw live or cached thumbnails of every window,
        including a minimised password manager that top_windows leaves out
        and a protected window whose own rect is nowhere near the target.
        The agent opens one itself: a click on the taskbar's Task View
        button, or the chord ["ctrl", "alt", "tab"], which _one_key_chord
        accepts. These explorer windows classify drivable, so the capture
        hands over the thumbnails."""
        for window_class in ("XamlExplorerHostIslandWindow", "MultitaskingViewFrame",
                             "TaskListThumbnailWnd"):
            with self.subTest(window_class=window_class):
                desk = Desk()
                # A minimised KeePass is simply absent from top_windows.
                desk.add(2, Window(r"C:\Windows\explorer.exe", False, window_class),
                         Rect(300, 200, 1600, 800))
                guard = Guard(desk)
                ids = _ids(guard)
                if 2 in ids:
                    with self.assertRaises(Refused):
                        guard.capture(ids[2])
                with self.assertRaises(Refused):
                    guard.capture(ids[1])


class ClosedThisRound(unittest.TestCase):
    def test_hud_behind_the_target_still_contributes_its_masks(self):
        desk = Desk()
        card = Rect(100, 100, 200, 160)
        desk.add(3, Window(r"C:\Program Files\Google\Chrome\chrome.exe", False, "Chrome_WidgetWin_1"),
                 SCREEN, front=False)
        desk.huds[3] = [card]
        guard = Guard(desk)
        guard.set_hud(1)
        grant = guard.capture(_ids(guard)[1])
        self.assertEqual(grant.masks, (Rect(92, 92, 208, 168),))

    def test_protected_window_behind_target_refuses_capture(self):
        desk = Desk()
        desk.add(2, Window("keepass.exe", False, "WindowsForms10"), Rect(700, 500, 900, 700), front=False)
        guard = Guard(desk)
        with self.assertRaises(Refused):
            guard.capture(_ids(guard)[1])

    def test_shift_delete_refused_through_every_alias_and_spelling(self):
        shifts = ("shift", "lshift", "rshift", "SHIFT", " Shift ", "LShift\t")
        deletes = ("delete", "del", "DEL", " Delete ", "dEl\n")
        for shift in shifts:
            for delete in deletes:
                for chord in ([shift, delete], [delete, shift], ["ctrl", shift, delete]):
                    with self.subTest(chord=chord):
                        self.assertFalse(guard_module._one_key_chord(chord))

    def test_guard_names_and_bridge_table_agree_and_aliases_share_a_key(self):
        tree = _bridge_tree()
        namespace = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                    getattr(t, "id", "") in ("_WINDOW_MODIFIER_CODES", "_WINDOW_KEY_CODES")
                    for t in node.targets):
                exec(compile(ast.Module([node], []), str(BRIDGE), "exec"), namespace)
        table, modifiers = namespace["_WINDOW_KEY_CODES"], namespace["_WINDOW_MODIFIER_CODES"]
        self.assertEqual(set(table), set(KEY_NAMES))
        self.assertEqual(set(modifiers), set(guard_module._MODIFIERS))
        for names in (("del", "delete"), ("esc", "escape"), ("enter", "return"),
                      ("ctrl", "control", "lctrl"), ("alt", "lalt"), ("shift", "lshift"),
                      ("ralt", "altgr")):
            self.assertEqual(len({table[name] for name in names}), 1, names)
        # No ordinary key sends a modifier and no modifier name sends an ordinary key.
        for name, code in table.items():
            self.assertEqual(name in modifiers, code in set(modifiers.values()), name)
        for name in ("win", "lwin", "VK_LWIN", "ctrl+a", "\u212a", "a\u00a0", "", "f25", "apps"):
            self.assertFalse(guard_module._one_key_chord(["ctrl", name]), name)

    def test_masked_step_never_named_valued_or_resolvable(self):
        class Node:
            def __init__(self, rect, name, role="button", interactive=True, kids=()):
                self.info = {"role": role, "name": name, "value": name, "interactive": interactive,
                             "offscreen": False, "enabled": True, "checked": None, "rect": rect}
                self.kids = list(kids)

        class Backend:
            def __init__(self, root):
                self.root = root

            def children(self, node):
                return node.kids

            def describe(self, node):
                return node.info

        child = Node((0, 0, 50, 20), "Visible")
        container = Node((0, 0, 400, 300), "SECRET-CONTAINER", "list", True, [child])
        root = Node((0, 0, 800, 600), "", "pane", False, [container])
        mask = Rect(300, 200, 360, 260)
        tree = MaskedTree(Backend(root), (mask,))
        text, refs = snapshot(tree)
        self.assertNotIn("SECRET", text)
        self.assertNotIn(MASKED_STEP_ROLE, text)
        self.assertEqual([meta["name"] for meta in refs.values()], ["Visible"])
        with self.assertRaises(StaleRef):
            resolve(MaskedTree(Backend(root), (mask,)),
                    {"path": [0], "role": "list", "name": "SECRET-CONTAINER"})

    def test_edge_touching_control_refused_and_extremes_fail_closed(self):
        mask = Rect(100, 100, 200, 200)
        grant = Grant(1, Rect(0, 0, 1000, 1000), (mask,))
        for bounds in (Rect(50, 100, 100, 150), Rect(200, 120, 250, 150), Rect(120, 50, 150, 100),
                       Rect(120, 200, 150, 260), Rect(50, 50, 100, 100), Rect(200, 200, 260, 260)):
            with self.subTest(bounds=bounds), self.assertRaises(Refused):
                grant.check_control(bounds)
        grant.check_control(Rect(50, 100, 99, 150))
        limit = 2 ** 31 - 8
        edge = Grant(1, Rect(-limit + 1, 0, 10, 10), ())
        with self.assertRaises(Refused):
            edge.check_control(Rect(-limit + 1, 0, 5, 5))


if __name__ == "__main__":
    unittest.main(verbosity=2)
