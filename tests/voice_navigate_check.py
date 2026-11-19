"""`Actions.navigate`: a validated URL to the foreground Chrome's own profile, or the default handler.

Linux-only and free: fake windows, fake processes, fake clock. The real handoff
is exercised by the opt-in tests/voice_navigate_real.py (disposable Chrome on a
hidden desktop, loopback proxy, no site visited).
"""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from windows import voiceactions as va
from windows.chromepicker import WindowIdentity

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
URL = "https://youtube.com"
REGULAR = ("New Tab - Google Chrome - Beta Probe", ("Beta Probe",))


def ident(hwnd=5, aumid="Chrome.Profile1", **kw):
    fields = dict(hwnd=hwnd, pid=77, class_name="Chrome_WidgetWin_1", exe=CHROME, aumid=aumid,
                  title="New Tab - Google Chrome", visible=True)
    fields.update(kw)
    return WindowIdentity(**fields)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Process:
    def __init__(self, codes):
        self.codes = list(codes)

    def poll(self):
        return self.codes.pop(0) if len(self.codes) > 1 else self.codes[0]


class Rig:
    def __init__(self, identity=..., foreground=None, cmdline=..., names=REGULAR, codes=(None, 0)):
        self.identities = [ident() if identity is ... else identity]
        self.foregrounds = [foreground if foreground is not None else (5, "New Tab - Google Chrome", CHROME)]
        self.cmdline = [CHROME, "--flag-switches-begin"] if cmdline is ... else cmdline
        self.names, self.codes = names, codes
        self.launches, self.opened, self.reads = [], [], []
        self.clock = Clock()
        self.hooks = {}
        picker = SimpleNamespace(identity=self.identity)
        desktop = SimpleNamespace(foreground_window=self.foreground)
        self.actions = va.Actions(desktop, clock=self.clock, sleep=self.clock.sleep,
                                  runner=lambda *a, **kw: SimpleNamespace(returncode=1, stdout=""),
                                  launcher=self.launch, opener=self.opened.append, picker=picker,
                                  command_line=self.command_line, window_names=self.window_names)

    def _next(self, items):
        return items.pop(0) if len(items) > 1 else items[0]

    def identity(self, hwnd):
        self.hooks.get("identity", lambda: None)()
        value = self._next(self.identities)
        if isinstance(value, Exception):
            raise value
        return value

    def foreground(self):
        self.foreground_calls = getattr(self, "foreground_calls", 0) + 1
        self.hooks.get("foreground", lambda n: None)(self.foreground_calls)
        return self._next(self.foregrounds)

    def command_line(self, pid):
        self.reads.append(pid)
        self.hooks.get("command_line", lambda: None)()
        if isinstance(self.cmdline, Exception):
            raise self.cmdline
        return self.cmdline

    def window_names(self, hwnd):
        if isinstance(self.names, Exception):
            raise self.names
        return self.names

    def launch(self, argv, **kw):
        self.launches.append(argv)
        if "launch" in self.hooks:
            return self.hooks["launch"]()
        return Process(self.codes)

    def go(self, url=URL, guard=lambda: True):
        with va.action_scope(guard):
            return self.actions.navigate(url)


class ProfileFromAumid(unittest.TestCase):
    def test_default_directory_profiles(self):
        for aumid, expected in (("Chrome", "Default"), ("Chrome.Profile1", "Profile 1"),
                                ("Chrome.Profile12", "Profile 12"), ("Chrome.Default", "Default")):
            self.assertEqual(va.chrome_profile_directory(aumid, ""), expected, aumid)

    def test_custom_directory_needs_its_component(self):
        self.assertEqual(va.chrome_profile_directory("Chrome.jarvisnav8u.Profile1", r"C:\d"), "Profile 1")
        self.assertEqual(va.chrome_profile_directory("Chrome.jarvisnav8u.Default", r"C:\d"), "Default")
        for aumid in ("Chrome", "Chrome.Profile1"):  # the process says custom, the window says default
            self.assertIsNone(va.chrome_profile_directory(aumid, r"C:\d"), aumid)
        self.assertIsNone(va.chrome_profile_directory("Chrome.x.Profile1", ""))  # and the reverse

    def test_refuses_everything_chrome_did_not_name(self):
        for aumid in (None, 7, "", "Chrome.SystemProfile", "Chrome.GuestProfile", "Chrome.Profile0",
                      "Chrome.Profile01", "Chrome.Profile 1", "Chrome.Work", "ChromeBeta.Profile1", "MSEdge",
                      "MSEdge.Profile1", "Chrome._crx_abc", "Chrome..Profile1", "Chrome.Profile1.",
                      "chrome.Profile1", "Chrome.a.b.Profile1", "Chrome.Ŝ", "Chrome." + "P" * 130):
            self.assertIsNone(va.chrome_profile_directory(aumid, ""), aumid)
        self.assertIsNone(va.chrome_profile_directory("Chrome.Profile1", None))


class UserDataDir(unittest.TestCase):
    def test_default_and_custom(self):
        self.assertEqual(va.chrome_user_data_dir([CHROME]), "")
        self.assertEqual(va.chrome_user_data_dir([CHROME, "--no-startup-window", "https://x.test"]), "")
        for arg in ("--user-data-dir=C:\\d", "-user-data-dir=C:\\d", "/user-data-dir=C:\\d", "--USER-DATA-DIR=C:\\d"):
            self.assertEqual(va.chrome_user_data_dir([CHROME, arg]), "C:\\d", arg)

    def test_switches_end_where_chromium_ends_them(self):
        self.assertEqual(va.chrome_user_data_dir([CHROME, "--", "--user-data-dir=C:\\d"]), "")
        self.assertEqual(va.chrome_user_data_dir([CHROME, "--single-argument", "--user-data-dir=C:\\d"]), "")

    def test_unsure_is_none(self):
        for argv in ([CHROME, "--user-data-dir=C:\\a", "--user-data-dir=C:\\b"], [CHROME, "--user-data-dir"],
                     [CHROME, "--user-data-dir="], [CHROME, "--user-data-dir=relative"],
                     [CHROME, '--user-data-dir=C:\\a"b'], [CHROME, "--user-data-dir=C:\\a\nb"],
                     [], None, "chrome.exe", [CHROME, 7]):
            self.assertIsNone(va.chrome_user_data_dir(argv), argv)


class RegularWindow(unittest.TestCase):
    def test_regular_windows(self):
        for names in (REGULAR, ("YouTube - Google Chrome", ()), ("YouTube - Google Chrome", ("Person 1",)),
                      ("a - Google Chrome (Incognito) - Google Chrome - Work", ("Work",))):
            self.assertTrue(va.regular_browser_window(names), names)

    def test_incognito_guest_and_unreadable(self):
        for names in (("New Incognito Tab - Google Chrome (Incognito)", ("Incognito",)),
                      ("New Incognito Tab - Google Chrome (Incognito)", ()),
                      # a page title cannot supply the product suffix
                      ("x - Google Chrome - y - Google Chrome (Incognito)", ()),
                      ("x - Google Chrome", ("Incognito",)), ("x - Google Chrome", ("Incognito (2)",)),
                      ("x - Google Chrome", ("Guest",)), ("x - Google Chrome", ("a", "b")),
                      ("Google Chrome", ()), ("x - Microsoft Edge", ()), ("x - Google Chromeium", ()),
                      None, (), ("x - Google Chrome",), (7, ()), ("x - Google Chrome", ["a"])):
            self.assertFalse(va.regular_browser_window(names), names)


class Navigate(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(va.os.environ, {"ProgramFiles": r"C:\Program Files"}))

    def test_chrome_foreground_hands_the_url_to_that_profile(self):
        rig = Rig()
        self.assertIs(rig.go(), True)
        self.assertEqual(rig.launches, [[CHROME, "--profile-directory=Profile 1", "--single-argument", URL]])
        self.assertEqual(rig.opened, [])
        self.assertEqual(rig.reads, [77])

    def test_network_error_tab_accepts_the_next_destination_in_the_same_profile(self):
        title = "youtube.com - Google Chrome"
        rig = Rig(identity=ident(title=title),
                  names=("youtube.com - Network error - Google Chrome - Work", ("Work",)))
        self.assertTrue(rig.go("https://example.edu"))
        self.assertEqual(rig.launches, [[CHROME, "--profile-directory=Profile 1",
                                         "--single-argument", "https://example.edu"]])
        self.assertEqual(rig.opened, [])

    def test_default_profile_and_custom_directory(self):
        rig = Rig(identity=ident(aumid="Chrome"))
        self.assertIs(rig.go("http://example.org"), True)
        self.assertEqual(rig.launches[0][1:], ["--profile-directory=Default", "--single-argument", "http://example.org"])
        rig = Rig(identity=ident(aumid="Chrome.probe8u.Profile2"), cmdline=[CHROME, '--user-data-dir=C:\\T\\p d'])
        self.assertIs(rig.go(), True)
        self.assertEqual(rig.launches[0], [CHROME, "--user-data-dir=C:\\T\\p d", "--profile-directory=Profile 2",
                                           "--single-argument", URL])

    def test_other_foreground_uses_the_default_handler_once(self):
        for window in ((9, "Notes - Notepad", r"C:\Windows\notepad.exe"), (9, "Edge", r"C:\x\msedge.exe")):
            rig = Rig(foreground=window)
            self.assertIs(rig.go(), True)
            self.assertEqual((rig.opened, rig.launches), ([URL], []))
        rig = Rig()
        rig.foregrounds = [None]
        self.assertIs(rig.go(), True)
        self.assertEqual(rig.opened, [URL])

    def test_only_canonical_urls(self):
        for value in ("go to youtube.com", "youtube.com", "https://youtube.com/", "https://YouTube.com",
                      "https://youtube.com/watch?v=1", "https://localhost", "file:///C:/x", "javascript:x",
                      "https://youtube.com --incognito", "", None, 7):
            rig = Rig()
            self.assertIs(rig.go(value), False, value)
            self.assertEqual((rig.launches, rig.opened, rig.reads), ([], [], []), value)

    def test_unresolvable_chrome_windows_get_nothing(self):
        cases = [dict(identity=ident(aumid="Chrome.SystemProfile", title="Google Chrome")),  # the picker
                 dict(identity=ident(aumid="Chrome.GuestProfile")),
                 dict(identity=ident(aumid="Chrome._crx_app")),
                 dict(identity=ident(aumid="ChromeBeta.Profile1")),
                 dict(identity=ident(title="Google Chrome")),
                 dict(identity=ident(visible=False)),
                 dict(identity=ident(class_name="Chrome_WidgetWin_0")),
                 dict(identity=ident(exe=r"C:\Temp\chrome.exe")),
                 dict(names=("Private mode: New Tab - Google Chrome", ("Privat",))),
                 dict(identity=ident(exe="chrome.exe")),
                 dict(identity=ident(hwnd=6)),
                 dict(identity=OSError("gone")),
                 dict(identity=None),
                 dict(names=("New Incognito Tab - Google Chrome (Incognito)", ("Incognito",))),
                 dict(names=OSError("uia timeout")),
                 dict(names=None),
                 dict(cmdline=None), dict(cmdline=OSError("denied")),
                 dict(cmdline=[CHROME, "--user-data-dir=C:\\a", "--user-data-dir=C:\\b"]),
                 dict(identity=ident(aumid="Chrome.x.Profile1")),  # custom window, default process
                 dict(cmdline=[CHROME, "--user-data-dir=C:\\d"])]  # and the reverse
        for case in cases:
            rig = Rig(**case)
            self.assertIs(rig.go(), False, case)
            self.assertEqual((rig.launches, rig.opened), ([], []), case)

    def test_focus_or_window_change_before_launch_launches_nothing(self):
        rig = Rig()
        rig.foregrounds = [(5, "t - Google Chrome", CHROME), (8, "Notes", r"C:\Windows\notepad.exe")]
        self.assertIs(rig.go(), False)
        rig2 = Rig()
        rig2.identities = [ident(), ident(pid=78)]  # the handle now belongs to another process
        self.assertIs(rig2.go(), False)
        rig3 = Rig()
        rig3.identities = [ident(), ident(aumid="Chrome")]
        self.assertIs(rig3.go(), False)
        self.assertEqual((rig.launches, rig2.launches, rig3.launches, rig.opened), ([], [], [], []))

    def test_revocation_before_launch_launches_nothing(self):
        for point in ("start", "command_line", "identity"):
            rig = Rig()
            state = {"ok": point != "start"}
            if point != "start":
                rig.hooks[point] = lambda: state.update(ok=False)
            self.assertIs(rig.go(guard=lambda: state["ok"]), False, point)
            self.assertEqual((rig.launches, rig.opened), ([], []), point)
        # Revoked during the last read before the launch (the foreground re-check).
        rig, state = Rig(), {"ok": True}
        rig.hooks["foreground"] = lambda n: state.update(ok=False) if n == 2 else None
        self.assertIs(rig.go(guard=lambda: state["ok"]), False)
        self.assertEqual(rig.launches, [])
        rig = Rig(foreground=(9, "Notes", r"C:\Windows\notepad.exe"))
        self.assertIs(rig.go(guard=lambda: False), False)
        self.assertEqual(rig.opened, [])
        rig = Rig()
        self.assertIs(rig.go(guard=lambda: 1 / 0), False)  # an unreadable check revokes
        self.assertEqual(rig.launches, [])

    def test_receipt_needs_the_forwarder_to_exit_with_the_handoff_code(self):
        rig = Rig(codes=(None, None, 0))
        self.assertIs(rig.go(), True)
        for codes in ((1,), (None, 21), (None,)):
            rig = Rig(codes=codes)
            self.assertIs(rig.go(), False, codes)
            self.assertEqual(len(rig.launches), 1)
        self.assertGreaterEqual(rig.clock.now, va.Actions.NAVIGATE_HANDOFF_WAIT)  # bounded wait, then no
        for result in (lambda: None, lambda: (_ for _ in ()).throw(OSError("no exe"))):
            rig = Rig()
            rig.hooks["launch"] = result
            self.assertIs(rig.go(), False)

    def test_revoked_while_waiting_reports_nothing(self):
        rig = Rig(codes=(None, None, 0))
        state = {"ok": True}
        rig.hooks["launch"] = lambda: (state.update(ok=False), Process((None, 0)))[1]
        self.assertIs(rig.go(guard=lambda: state["ok"]), False)
        self.assertEqual(len(rig.launches), 1)

    def test_without_a_command_line_reader_off_windows_nothing_launches(self):
        rig = Rig()
        rig.actions.command_line = None
        self.assertIs(rig.go(), False)
        self.assertEqual(rig.launches, [])


if __name__ == "__main__":
    unittest.main()
