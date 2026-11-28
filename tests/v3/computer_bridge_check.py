"""Exercise the scoped window-front operation with fake Win32 calls."""

import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from tests.v2.desktop_guard_check import FakeDesktop
from windows.desktopguard import Guard, Refused


BRIDGE = Path(__file__).resolve().parents[2] / "windows" / "bridge.py"


def load_front(guard, desktop, force_foreground):
    tree = ast.parse(BRIDGE.read_text(encoding="utf-8"))
    wanted = {"WindowsDesktop", "op_window_front", "_scoped_window_op"}
    table = next(node for node in tree.body if isinstance(node, ast.Assign)
                 and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "_WINDOW_OPS")
    scope = {node.id: Mock(side_effect=AssertionError("Unexpected window operation"))
             for node in table.value.values}
    scope.update(Refused=Refused, _WINDOW_GUARD=guard, _WINDOW_DESKTOP=desktop,
                 _force_foreground=force_foreground, os=SimpleNamespace(getpid=lambda: 1),
                 time=SimpleNamespace(sleep=Mock()))
    nodes = [node for node in tree.body if getattr(node, "name", None) in wanted or node is table]
    exec(compile(ast.Module(nodes, []), str(BRIDGE), "exec"), scope)
    return scope


class ComputerBridgeCheck(unittest.TestCase):
    def setUp(self):
        self.desktop = FakeDesktop()
        self.guard = Guard(self.desktop)
        self.desktop.front = 20
        self.req = {"window": self.guard.windows()[0]["window"], "scope": "fake-scope"}
        self.force = Mock(side_effect=self.raise_target)
        scope = load_front(self.guard, self.desktop, self.force)
        self.call = scope["_scoped_window_op"]
        self.desktop._process_info = Mock(return_value=("fake-bridge.exe", False))
        self.desktop.set_dpi_context = Mock(return_value=-1)
        self.desktop.user = SimpleNamespace(IsIconic=Mock(return_value=False), ShowWindow=Mock(),
                                            GetForegroundWindow=lambda: self.desktop.front,
                                            GetAsyncKeyState=Mock(return_value=0))
        for name in ("bring_forward", "require_idle_input", "foreground"):
            method = getattr(scope["WindowsDesktop"], name)
            setattr(self.desktop, name, method.__get__(self.desktop))

    def raise_target(self, hwnd):
        self.desktop.front = hwnd

    def front(self, *, paired=True, request=None):
        return self.call("window_front", self.req if request is None else request, paired)

    def test_raises_the_window_without_resizing(self):
        self.assertEqual(self.front(), {"front": True})
        self.force.assert_called_once_with(10)
        self.desktop.user.ShowWindow.assert_not_called()

    def test_restores_a_minimized_target_before_raising(self):
        self.desktop.user.IsIconic.return_value = True

        def raise_after_restore(hwnd):
            self.desktop.user.ShowWindow.assert_called_once_with(hwnd, 9)
            self.raise_target(hwnd)

        self.force.side_effect = raise_after_restore
        self.assertEqual(self.front(), {"front": True})

    def test_held_mouse_button_or_modifier_refuses_before_raising(self):
        for held in (0x01, 0x10):
            with self.subTest(held=held):
                self.desktop.user.GetAsyncKeyState.side_effect = lambda key: 0x8000 if key == held else 0
                with self.assertRaisesRegex(Refused, "Release held mouse buttons or modifiers"):
                    self.front()
        self.force.assert_not_called()
        self.desktop.user.ShowWindow.assert_not_called()

    def test_a_foreground_failure_is_refused(self):
        self.force.side_effect = None
        self.force.return_value = True
        with self.assertRaisesRegex(Refused, "did not bring that window to the front"):
            self.front()
        self.force.assert_called_once_with(10)

    def test_rechecks_the_window_identity_after_raising(self):
        def replace(hwnd):
            self.raise_target(hwnd)
            self.desktop.pids[hwnd] += 1

        self.force.side_effect = replace
        with self.assertRaisesRegex(Refused, "window has changed"):
            self.front()

    def test_a_different_handle_on_recheck_is_refused(self):
        self.guard.forward = Mock(side_effect=[10, 20])
        with self.assertRaisesRegex(Refused, "window changed"):
            self.front()

    def test_requires_a_scope_and_a_paired_bridge(self):
        with self.assertRaisesRegex(Refused, "need a scope"):
            self.front(request={"window": self.req["window"]})
        with self.assertRaisesRegex(Refused, "paired"):
            self.front(paired=False)
        self.force.assert_not_called()
        self.desktop.user.ShowWindow.assert_not_called()


if __name__ == "__main__":
    unittest.main()
