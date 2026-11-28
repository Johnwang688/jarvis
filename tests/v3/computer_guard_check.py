"""Foreground markers on the Windows guard's existing window list."""

import unittest
from unittest.mock import patch

from tests.v2.desktop_guard_check import FakeDesktop, TARGET
from windows.desktopguard import Guard, Refused
from windows.windowpolicy import PROTECTED_MESSAGE, Window


class ComputerGuardCheck(unittest.TestCase):
    def setUp(self):
        self.desktop = FakeDesktop()
        self.desktop.add(20, Window("paint.exe", False), TARGET, "Paint")
        self.guard = Guard(self.desktop)

    def flags(self):
        return [(row["title"], row["front"]) for row in self.guard.windows()]

    def test_foreground_need_not_be_first_in_list(self):
        self.assertEqual(self.flags(), [("Paint", False), ("Notes", True)])
        self.desktop.front = 20
        self.assertEqual(self.flags(), [("Paint", True), ("Notes", False)])

    def test_unreadable_foreground_marks_nothing(self):
        with patch.object(self.desktop, "foreground", side_effect=OSError):
            self.assertEqual(self.flags(), [("Paint", False), ("Notes", False)])

    def test_protected_foreground_marks_nothing(self):
        self.desktop.info[10] = Window("notepad.exe", True)
        self.assertEqual(self.flags(), [("Paint", False)])

    def test_absent_foreground_marks_nothing(self):
        for foreground in (None, 99):
            with self.subTest(foreground=foreground):
                self.desktop.front = foreground
                self.assertEqual(self.flags(), [("Paint", False), ("Notes", False)])

    def test_forward_accepts_drivable_and_see_only_windows(self):
        self.desktop.add(30, Window("powershell.exe", False), TARGET, "Shell")
        windows = {item["title"]: item["window"] for item in self.guard.windows()}
        self.assertEqual(self.guard.forward(windows["Notes"]), 10)
        self.assertEqual(self.guard.forward(windows["Shell"]), 30)

    def test_forward_refuses_a_window_that_became_protected(self):
        window = next(item["window"] for item in self.guard.windows() if item["title"] == "Paint")
        self.desktop.info[20] = Window("paint.exe", True)
        with self.assertRaises(Refused) as caught:
            self.guard.forward(window)
        self.assertEqual(str(caught.exception), PROTECTED_MESSAGE)

    def test_forward_refuses_an_unknown_id(self):
        with self.assertRaisesRegex(Refused, "Unknown window"):
            self.guard.forward("fake-unknown-window")

    def test_forward_refuses_a_window_that_changed_identity(self):
        window = self.guard.windows()[0]["window"]
        self.desktop.pids[20] += 1
        with self.assertRaisesRegex(Refused, "window has changed"):
            self.guard.forward(window)


if __name__ == "__main__":
    unittest.main()
