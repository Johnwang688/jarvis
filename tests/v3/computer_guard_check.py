"""Foreground markers on the Windows guard's existing window list."""

import unittest
from unittest.mock import patch

from tests.v2.desktop_guard_check import FakeDesktop, TARGET
from windows.desktopguard import Guard
from windows.windowpolicy import Window


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


if __name__ == "__main__":
    unittest.main()
