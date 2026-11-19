"""CU-0 review: window policy inputs that get a weaker answer than its own rules.

`Findings` fails on 8562f6cc (each test is one finding). `Coverage` passes there and
kills mutants of windowpolicy.py that survive window_policy_check.py.
"""
from pathlib import Path
import math
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from windows import windowpolicy as wp
from windows.windowpolicy import DRIVABLE, PROTECTED, SEE_ONLY, Rect, Window


def kind(image, elevated=False, window_class="", own=False, **more):
    return wp.classify(Window(image, elevated, window_class, own), **more)


class Findings(unittest.TestCase):
    # 1 (MED) Window's docstring: "None means unreadable"; module: "Anything the
    # bridge could not read is protected". An unreadable class is read as "", so
    # a console program (python.exe in conhost) whose class read failed is drivable.
    def test_1_unreadable_window_class_is_protected(self):
        self.assertEqual(kind("python.exe", window_class=None), PROTECTED)

    # 2 (MED) The bridge's existing key format is a SendKeys string (op_key sends
    # req["keys"] verbatim, e.g. "{Ctrl}a{Del}"). A string is iterated per
    # character, so Shift+Delete written that way is allowed.
    def test_2_shift_delete_as_a_string_is_refused(self):
        for keys in ("{Shift}{Del}", "+{DEL}", "shift+delete", "Shift+Delete",
                     ["shift+delete"], ["Shift+Del"], ["{Shift}", "{Del}"]):
            with self.subTest(keys=keys):
                self.assertTrue(wp.refused_keys(keys))

    # 3 (LOW) Other common names for the same two keys (W3C code, X11 keysym,
    # Win32 VK). Either refuse them or document the vocabulary the caller must
    # normalise to.
    def test_3_common_key_spellings_are_refused(self):
        for keys in (["ShiftLeft", "Delete"], ["Shift_L", "Delete"], ["VK_SHIFT", "VK_DELETE"],
                     ["left shift", "delete"], ["shift", "Del\x00"]):
            with self.subTest(keys=keys):
                self.assertTrue(wp.refused_keys(keys))

    # 4 (LOW) image_name refuses control characters as untrustworthy; the class
    # name does not, so a NUL-terminated buffer read turns a console window drivable.
    def test_4_class_with_control_characters_is_not_drivable(self):
        self.assertNotEqual(kind("python.exe", window_class="ConsoleWindowClass\x00"), DRIVABLE)

    # 5 (LOW) The owner's list: a bare string is iterated per character, an int
    # raises, and one generator reused across calls protects only on the first.
    def test_5_owner_password_manager_list_fails_closed(self):
        self.assertEqual(kind("vault.exe", extra_password_managers="vault.exe"), PROTECTED)
        try:
            self.assertEqual(kind("vault.exe", extra_password_managers=5), PROTECTED)
        except TypeError as e:
            self.fail("classify raised %r" % e)
        names = (n for n in ["vault.exe"])
        self.assertEqual(kind("vault.exe", extra_password_managers=names), PROTECTED)
        self.assertEqual(kind("vault.exe", extra_password_managers=names), PROTECTED)

    # 6 (LOW) masked/overlaps/refused_keys treat None as "unreadable, refuse";
    # may_capture raises on it instead of returning False.
    def test_6_may_capture_unreadable_overlap_is_false(self):
        try:
            self.assertFalse(wp.may_capture(DRIVABLE, None))
        except TypeError as e:
            self.fail("may_capture raised %r" % e)

    # 7 (LOW) "Unreadable is masked": a malformed mask (inverted, NaN) passed to
    # masked/overlaps masks nothing. hud_view rejects these, the public helpers do not.
    def test_7_malformed_mask_masks_everything(self):
        self.assertTrue(wp.masked(150, 150, [Rect(300, 200, 100, 100)]))
        self.assertTrue(wp.masked(150, 150, [Rect(math.nan, 0, 999, 999)]))
        self.assertTrue(wp.overlaps(Rect(0, 0, 999, 999), [Rect(math.nan, 0, 999, 999)]))

    # 8 (LOW) An 8.3 short name is the same file as the long name. A process
    # started by its short path reports it; `~N` cannot be resolved here, so it
    # should not be trusted (or the bridge must call GetLongPathNameW first).
    def test_8_short_names_are_not_drivable(self):
        for image in (r"C:\PROGRA~1\KEEPAS~1\KEEPAS~1.EXE", "BITWAR~1.EXE", "1PASSW~1.EXE"):
            with self.subTest(image=image):
                self.assertNotEqual(kind(image), DRIVABLE)

    # 9 (LOW) Mainstream Windows terminals missing from the list.
    def test_9_mainstream_terminals_are_see_only(self):
        for image in ("MobaXterm.exe", "SecureCRT.exe"):
            with self.subTest(image=image):
                self.assertEqual(kind(image), SEE_ONLY)


class Coverage(unittest.TestCase):
    """Passes on 8562f6cc; each kills a mutant window_policy_check.py lets live."""

    def test_alternate_data_stream_and_colon(self):          # ':' dropped from the refused set
        for image in ("keepass.exe:stream", "keepass.exe::$DATA", "C:keepass.exe"):
            self.assertEqual(kind(image), PROTECTED, image)

    def test_leading_whitespace_is_untrusted(self):           # name != name.strip() dropped
        self.assertEqual(kind(" chrome.exe"), PROTECTED)

    def test_masked_needs_a_whole_y(self):                    # _whole(y) dropped
        self.assertTrue(wp.masked(10, None, ()))
        self.assertTrue(wp.masked(10, 1.5, ()))
        self.assertTrue(wp.masked(10, True, ()))

    def test_every_listed_console_class(self):                # class entries dropped
        for cls in ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS", "PseudoConsoleWindow",
                    "mintty", "VirtualConsoleClass"):
            self.assertEqual(kind("python.exe", window_class=cls.upper()), SEE_ONLY, cls)

    def test_every_listed_system_prompt_and_password_manager(self):   # seed entries dropped
        for image in wp.SYSTEM_PROMPT_IMAGES | wp.PASSWORD_MANAGER_IMAGES:
            self.assertEqual(kind(image.upper()), PROTECTED, image)
        for image in ("CredentialEnrollmentManager.exe", "ProtonPass.exe", "NordPass.exe",
                      "Enpass.exe", "KeeperPasswordManager.exe", "RoboForm.exe"):
            self.assertEqual(kind(image), PROTECTED, image)

    def test_every_listed_terminal(self):
        for image in wp.TERMINAL_IMAGES:
            self.assertEqual(kind(image.upper()), SEE_ONLY, image)


if __name__ == "__main__":
    unittest.main()
