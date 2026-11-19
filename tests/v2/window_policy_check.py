"""Window policy for the driving agent (CU-0): pure rules, no Windows needed."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from windows import windowpolicy as wp
from windows.windowpolicy import DRIVABLE, PROTECTED, SEE_ONLY, Rect, Window


def kind(image, elevated=False, window_class="", own=False, **more):
    return wp.classify(Window(image, elevated, window_class, own), **more)


class Classify(unittest.TestCase):
    def test_ordinary_apps_are_drivable(self):
        for image in ("chrome.exe", r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                      "EXCEL.EXE", "explorer.exe", "Code.exe", "notepad"):
            self.assertEqual(kind(image), DRIVABLE, image)

    def test_unreadable_is_protected(self):
        self.assertEqual(kind(None), PROTECTED)
        self.assertEqual(kind(""), PROTECTED)
        self.assertEqual(kind("   "), PROTECTED)
        self.assertEqual(kind(7), PROTECTED)
        self.assertEqual(kind("chrome.exe", elevated=None), PROTECTED)
        self.assertEqual(kind("chrome.exe", elevated="no"), PROTECTED)
        self.assertEqual(kind("chrome.exe", elevated=0), PROTECTED)
        self.assertEqual(kind("chrome.exe", window_class=5), PROTECTED)
        self.assertEqual(kind("chrome.exe", own=None), PROTECTED)
        self.assertEqual(kind("chro\nme.exe"), PROTECTED)
        self.assertEqual(kind("a|b.exe"), PROTECTED)
        self.assertEqual(wp.classify(("chrome.exe", False)), PROTECTED)
        self.assertEqual(wp.classify(None), PROTECTED)

    def test_elevated_is_protected_whatever_it_is(self):
        for image in ("powershell.exe", "notepad.exe", "chrome.exe", "WindowsTerminal.exe"):
            self.assertEqual(kind(image, elevated=True), PROTECTED, image)

    def test_own_helper_windows_are_protected(self):
        self.assertEqual(kind("python.exe", own=True), PROTECTED)

    def test_system_prompts_are_protected(self):
        for image in ("consent.exe", "CredentialUIBroker.exe", "LogonUI.exe", "LockApp.exe"):
            self.assertEqual(kind(image), PROTECTED, image)
        self.assertEqual(kind("explorer.exe", window_class="Credential Dialog Xaml Host"), PROTECTED)

    def test_password_managers_are_protected(self):
        for image in ("1Password.exe", "Bitwarden.exe", "KeePass.exe", "KeePassXC.exe",
                      "lastpass.exe", "Dashlane.exe", r"C:\Apps\KeePass\KEEPASS.EXE"):
            self.assertEqual(kind(image), PROTECTED, image)

    def test_windows_name_trimming_and_separators(self):
        for image in ("KeePass.exe.", "keepass.exe ", "keepass.exe. .", "c:/apps/keepass.exe",
                      "keepass", "KEEPASS."):
            self.assertEqual(kind(image), PROTECTED, image)
        self.assertEqual(kind("cmd.exe.  "), SEE_ONLY)

    def test_owner_added_password_manager(self):
        self.assertEqual(kind("vault.exe"), DRIVABLE)
        self.assertEqual(kind("Vault.exe", extra_password_managers=["vault"]), PROTECTED)
        self.assertEqual(kind("vault.exe", extra_password_managers=[r"C:\x\VAULT.EXE."]), PROTECTED)
        # A bad entry in the owner's list protects nothing extra and breaks nothing.
        self.assertEqual(kind("chrome.exe", extra_password_managers=[None, "", 3]), DRIVABLE)

    def test_terminals_are_see_only(self):
        for image in ("WindowsTerminal.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "wsl.exe",
                      "conhost.exe", "OpenConsole.exe", "mintty.exe", "ubuntu.exe"):
            self.assertEqual(kind(image), SEE_ONLY, image)

    def test_console_window_of_any_program_is_see_only(self):
        self.assertEqual(kind("python.exe", window_class="ConsoleWindowClass"), SEE_ONLY)
        self.assertEqual(kind("ssh.exe", window_class=" consolewindowclass "), SEE_ONLY)
        self.assertEqual(kind("x.exe", window_class="CASCADIA_HOSTING_WINDOW_CLASS"), SEE_ONLY)
        self.assertEqual(kind("python.exe", window_class="TkTopLevel"), DRIVABLE)

    def test_protected_wins_over_see_only(self):
        self.assertEqual(kind("cmd.exe", elevated=True), PROTECTED)
        self.assertEqual(kind("keepass.exe", window_class="ConsoleWindowClass"), PROTECTED)

    def test_a_name_that_only_contains_a_listed_name_is_not_it(self):
        self.assertEqual(kind("notkeepass.exe"), DRIVABLE)
        self.assertEqual(kind("cmd.exe.bak"), DRIVABLE)
        self.assertEqual(kind("mycmd.exe"), DRIVABLE)


class Actions(unittest.TestCase):
    def test_capture(self):
        self.assertTrue(wp.may_capture(DRIVABLE))
        self.assertTrue(wp.may_capture(SEE_ONLY))
        self.assertFalse(wp.may_capture(PROTECTED))
        self.assertFalse(wp.may_capture("other"))
        self.assertFalse(wp.may_capture(None))
        self.assertTrue(wp.may_capture(DRIVABLE, [DRIVABLE, SEE_ONLY]))
        self.assertFalse(wp.may_capture(DRIVABLE, [DRIVABLE, PROTECTED]))
        self.assertFalse(wp.may_capture(DRIVABLE, [None]))

    def test_input(self):
        self.assertTrue(wp.may_input(DRIVABLE))
        for other in (SEE_ONLY, PROTECTED, "", None, "DRIVABLE", True):
            self.assertFalse(wp.may_input(other), other)

    def test_refusal_text(self):
        self.assertEqual(wp.refusal(DRIVABLE), "")
        self.assertIn("terminal tool", wp.refusal(SEE_ONLY))
        self.assertEqual(wp.refusal(PROTECTED), wp.PROTECTED_MESSAGE)
        self.assertEqual(wp.refusal("anything"), wp.PROTECTED_MESSAGE)

    def test_shift_delete_is_refused(self):
        self.assertTrue(wp.refused_keys(["shift", "delete"]))
        self.assertTrue(wp.refused_keys(["Shift", "DEL"]))
        self.assertTrue(wp.refused_keys(["ctrl", " RShift ", "Delete"]))
        self.assertTrue(wp.refused_keys(["lshift", "del"]))
        self.assertFalse(wp.refused_keys(["delete"]))
        self.assertFalse(wp.refused_keys(["shift", "a"]))
        self.assertFalse(wp.refused_keys([]))
        self.assertTrue(wp.refused_keys(None))
        self.assertTrue(wp.refused_keys(["shift", 3]))


class Hud(unittest.TestCase):
    card = Rect(100, 100, 300, 200)

    def test_nothing_sensitive_leaves_the_hud_open(self):
        self.assertEqual(wp.hud_view(0, None, None), (DRIVABLE, ()))

    def test_fresh_masks_keep_the_rest_drivable(self):
        state, masks = wp.hud_view(1, [self.card], 0.2)
        self.assertEqual(state, DRIVABLE)
        m = wp.MASK_MARGIN
        self.assertEqual(masks, (Rect(100 - m, 100 - m, 300 + m, 200 + m),))

    def test_missing_stale_or_short_reports_protect_the_whole_hud(self):
        bad = [
            (1, None, 0.1), (1, [], 0.1), (2, [self.card], 0.1),
            (1, [self.card], None), (1, [self.card], wp.MASK_MAX_AGE_S + 0.01),
            (1, [self.card], -1), (1, [self.card], float("nan")), (1, [self.card], True),
            (1, [Rect(5, 5, 5, 9)], 0.1), (1, [Rect(9, 5, 5, 9)], 0.1),
            (1, [(100, 100, 300, 200)], 0.1), (1, [Rect(1.5, 1, 9, 9)], 0.1),
            (1, [Rect(True, 1, 9, 9)], 0.1), (1, "abcd", 0.1), (1, 5, 0.1),
            (-1, [self.card], 0.1), (None, [self.card], 0.1), (True, [self.card], 0.1),
            (1.0, [self.card], 0.1),
        ]
        for case in bad:
            self.assertEqual(wp.hud_view(*case), (PROTECTED, ()), case)

    def test_age_limit_is_inclusive(self):
        self.assertEqual(wp.hud_view(1, [self.card], wp.MASK_MAX_AGE_S)[0], DRIVABLE)

    def test_point_under_a_mask(self):
        _, masks = wp.hud_view(1, [self.card], 0)
        m = wp.MASK_MARGIN
        self.assertTrue(wp.masked(150, 150, masks))
        self.assertTrue(wp.masked(100 - m, 100 - m, masks))
        self.assertFalse(wp.masked(100 - m - 1, 150, masks))
        self.assertTrue(wp.masked(300 + m - 1, 150, masks))
        self.assertFalse(wp.masked(300 + m, 150, masks))
        self.assertFalse(wp.masked(10, 10, masks))
        self.assertFalse(wp.masked(10, 10, ()))
        self.assertTrue(wp.masked(1.5, 10, masks))
        self.assertTrue(wp.masked(None, 10, ()))
        self.assertTrue(wp.masked(10, 10, [(0, 0, 5, 5)]))
        self.assertTrue(wp.masked(10, 10, None))

    def test_element_overlapping_a_mask(self):
        masks = (Rect(100, 100, 200, 200),)
        self.assertTrue(wp.overlaps(Rect(150, 150, 400, 400), masks))
        self.assertTrue(wp.overlaps(Rect(0, 0, 101, 101), masks))
        self.assertFalse(wp.overlaps(Rect(0, 0, 100, 100), masks))
        self.assertFalse(wp.overlaps(Rect(200, 100, 300, 200), masks))
        self.assertTrue(wp.overlaps(Rect(0, 0, 1000, 1000), masks))
        self.assertTrue(wp.overlaps(Rect(5, 5, 5, 5), masks))
        self.assertTrue(wp.overlaps(None, masks))
        self.assertTrue(wp.overlaps(Rect(0, 0, 9, 9), None))
        self.assertFalse(wp.overlaps(Rect(0, 0, 9, 9), ()))


if __name__ == "__main__":
    unittest.main()
