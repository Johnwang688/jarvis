"""Desktop talk shortcuts. The Windows probe never switches the user's desktop or sends keys."""
import ctypes
import os
import queue
import threading
import unittest
import uuid
from unittest.mock import Mock, patch

from windows import voice_agent as voice


class Poll(unittest.TestCase):
    def run_poll(self, frames):
        # Each poll has a deterministic Windows key snapshot and monotonic time.
        recorder = object.__new__(voice.Agent)
        recorder.generation = 0
        recorder.next_generation = Mock()
        recorder.start_capture = Mock()
        recorder.finish_capture = Mock()
        recorder.show = Mock()
        recorder.player = Mock()
        recorder.desktop_key = Mock(hud=False)
        recorder.user32 = Mock()
        recorder.done = Mock()
        step = 0
        events = []
        def tick(_):
            nonlocal step
            step += 1
        def hotkey():
            recorder.desktop_key.hud = frames[step][3]
            return frames[step][2]
        recorder.start_capture.side_effect = lambda: events.append(("start", step))
        recorder.finish_capture.side_effect = lambda cancel: events.append(("cancel" if cancel else "stop", step))
        recorder.done.is_set.side_effect = lambda: step >= len(frames)
        recorder.done.wait.side_effect = tick
        recorder.desktop_key.poll.side_effect = hotkey
        recorder.user32.GetAsyncKeyState.side_effect = lambda k: 0x8000 if k in frames[step][1] else 0
        with patch.object(voice.time, "monotonic_ns", lambda: frames[step][0] * 1_000_000):
            recorder.poll()
        recorder.desktop_key.close.assert_called_once()
        self.assertEqual(events.pop(), ("cancel", len(frames)))  # unconditional shutdown cleanup
        return events, recorder

    def test_right_ctrl_hold_survives_pointer_clicks_and_timer_then_releases_once(self):
        right = {voice.VK_RCONTROL, voice.VK_CONTROL}
        frames = [(0, right, False, False), (500, right | {1}, False, False),
                  (1500, right | {2, 4}, False, False), (3500, right | {5, 6}, False, False),
                  (6000, right, False, False), (6100, set(), False, False)]
        events, _ = self.run_poll(frames)
        self.assertEqual(events, [("start", 0), ("stop", 5)])

    def test_right_ctrl_tap_toggles_until_another_clean_tap(self):
        right = {voice.VK_RCONTROL, voice.VK_CONTROL}
        frames = [(0, right, False, False), (150, set(), False, False),
                  (800, {1}, False, False), (5000, right | {ord("C")}, False, False),
                  (5020, set(), False, False), (6500, right, False, False),
                  (6510, set(), False, False)]
        self.assertEqual(self.run_poll(frames)[0], [("start", 0), ("stop", 5)])

    def test_ctrl_d_is_one_capture_even_if_right_ctrl_releases_last(self):
        right = {voice.VK_RCONTROL, voice.VK_CONTROL}
        frames = [(0, right, False, False), (20, right | {voice.VK_D}, True, False),
                  (1000, right | {voice.VK_D, 1}, False, False), (1600, right, False, False),
                  (1700, right, False, False), (2100, set(), False, False)]
        self.assertEqual(self.run_poll(frames)[0], [("start", 0), ("stop", 3)])

    def test_left_ctrl_d_hold_and_release_modifier_first(self):
        left = {voice.VK_LCONTROL, voice.VK_CONTROL}
        frames = [(0, left, False, False), (20, left | {voice.VK_D}, True, False),
                  (1000, left | {voice.VK_D}, False, False), (1200, {voice.VK_D}, False, False),
                  (1250, set(), False, False)]
        self.assertEqual(self.run_poll(frames)[0], [("start", 1), ("stop", 3)])

    def test_fast_ctrl_d_tap_while_keys_already_up_is_toggle(self):
        frames = [(0, set(), True, False), (15, set(), False, False),
                  (3000, set(), False, False), (3100, set(), True, False)]
        self.assertEqual(self.run_poll(frames)[0], [("start", 0), ("stop", 3)])

    def test_hud_owns_ctrl_d_and_reentry_cancels_native_capture(self):
        chord = {voice.VK_CONTROL, voice.VK_LCONTROL, voice.VK_D}
        frames = [(0, chord, False, True), (600, set(), False, True),
                  (800, chord, True, False), (1600, chord, False, True),
                  (1800, set(), False, True)]
        self.assertEqual(self.run_poll(frames)[0], [("start", 2), ("cancel", 3)])

    def test_other_keyboard_chord_cancels_with_feedback_and_recovers(self):
        right = {voice.VK_RCONTROL, voice.VK_CONTROL}
        frames = [(0, right, False, False), (400, right | {ord("S")}, False, False),
                  (800, right, False, False), (1000, set(), False, False),
                  (2000, right, False, False), (3000, set(), False, False)]
        events, recorder = self.run_poll(frames)
        self.assertEqual(events, [("start", 0), ("cancel", 1), ("start", 4), ("stop", 5)])
        self.assertIn("shortcut", recorder.show.call_args.args[1])

    def test_shift_altgr_windows_and_bare_left_ctrl_are_not_desktop_gestures(self):
        for modifier in (0x10, 0x12, 0xA5, 0x5B):
            with self.subTest(modifier=modifier):
                keys = {voice.VK_D, voice.VK_CONTROL, voice.VK_LCONTROL, modifier}
                frames = [(0, {voice.VK_LCONTROL}, False, False), (50, keys, False, False),
                          (60, keys, True, False), (350, set(), False, False)]
                self.assertEqual(self.run_poll(frames)[0], [])


class Scope(unittest.TestCase):
    def test_foreground_tab_titles_and_nonbrowser(self):
        for winclass, title, outside in [
            ("Chrome_WidgetWin_1", "J.A.R.V.I.S.", False),
            ("Chrome_WidgetWin_1", "J.A.R.V.I.S. - Google Chrome", False),
            ("MozillaWindowClass", "J.A.R.V.I.S. — Mozilla Firefox", False),
            ("Chrome_WidgetWin_1", "Jarvis source code - Google Chrome", True),
            ("Notepad", "J.A.R.V.I.S.", True),
        ]:
            with self.subTest(title=title, window_class=winclass):
                api = Mock()
                api.GetForegroundWindow.return_value = 123
                api.GetClassNameW.side_effect = lambda hwnd, buf, n: setattr(buf, "value", winclass) or len(winclass)
                api.GetWindowTextW.side_effect = lambda hwnd, buf, n: setattr(buf, "value", title) or len(title)
                key = voice.DesktopTalkKey(api, Mock())
                self.assertEqual(key.outside_hud(), outside)
                api.GetForegroundWindow.return_value = 0
                self.assertFalse(key.outside_hud())

    def test_collision_is_bounded_warns_then_retries(self):
        api, warning = Mock(), Mock()
        api.RegisterHotKey.side_effect = [False, False, True]
        api.PeekMessageW.return_value = False
        key = voice.DesktopTalkKey(api, warning)
        with patch.object(key, "outside_hud", return_value=True), patch.object(
            voice.time, "monotonic", side_effect=[0., 0., 1., 6., 6., 12.]
        ):
            for _ in range(4):
                key.poll()
        warning.assert_called_once()
        self.assertTrue(key.registered)
        key.close()
        api.UnregisterHotKey.assert_called_once()


@unittest.skipUnless(os.name == "nt", "requires Win32")
class PrivateDesktop(unittest.TestCase):
    def test_real_registration_collision_message_and_release(self):
        from ctypes import wintypes as w
        user = ctypes.WinDLL("user32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        user.CreateDesktopW.argtypes = [w.LPCWSTR, w.LPCWSTR, ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.c_void_p]
        user.CreateDesktopW.restype = w.HANDLE
        user.SetThreadDesktop.argtypes = [w.HANDLE]
        user.CloseDesktop.argtypes = [w.HANDLE]
        user.PostThreadMessageW.argtypes = [w.DWORD, w.UINT, w.WPARAM, w.LPARAM]
        desktop = user.CreateDesktopW("JarvisVoiceTest-" + uuid.uuid4().hex, None, None, 0, 0x01FF, None)
        self.assertTrue(desktop, ctypes.get_last_error())
        self.addCleanup(user.CloseDesktop, desktop)
        errors = queue.Queue()

        def run(func):
            def worker():
                try:
                    self.assertTrue(user.SetThreadDesktop(desktop), ctypes.get_last_error())
                    func()
                except BaseException as err:
                    errors.put(err)
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            if not errors.empty():
                raise errors.get()

        def probe():
            warning = Mock()
            from contextlib import ExitStack

            # The hidden desktop cannot become the machine's foreground. Read
            # its thread-local active window and use real HWND classes/titles.
            class Api:
                GetForegroundWindow = user.GetActiveWindow
                def __getattr__(self, name):
                    return getattr(user, name)
            key = voice.DesktopTalkKey(Api(), warning)

            class WindowClass(ctypes.Structure):
                _fields_ = [("style", w.UINT), ("proc", ctypes.c_void_p),
                            ("clsExtra", ctypes.c_int), ("wndExtra", ctypes.c_int),
                            ("instance", w.HANDLE), ("icon", w.HANDLE), ("cursor", w.HANDLE),
                            ("background", w.HANDLE), ("menu", w.LPCWSTR), ("name", w.LPCWSTR)]
            kernel.GetModuleHandleW.restype = w.HANDLE
            instance = kernel.GetModuleHandleW(None)
            cls = WindowClass()
            cls.proc = ctypes.cast(user.DefWindowProcW, ctypes.c_void_p).value
            cls.instance, cls.name = instance, "Chrome_WidgetWin_1"
            user.RegisterClassW.argtypes = [ctypes.POINTER(WindowClass)]
            user.UnregisterClassW.argtypes = [w.LPCWSTR, w.HANDLE]
            user.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HANDLE, ctypes.c_void_p]
            user.CreateWindowExW.restype = w.HWND
            user.SetActiveWindow.argtypes = [w.HWND]
            user.SetWindowTextW.argtypes = [w.HWND, w.LPCWSTR]
            user.DestroyWindow.argtypes = [w.HWND]
            def competing(allowed):
                acquired = bool(user.RegisterHotKey(None, 771, key.MODS, voice.VK_D))
                try:
                    self.assertEqual(acquired, allowed)
                finally:
                    if acquired:
                        user.UnregisterHotKey(None, 771)
            try:
                with ExitStack() as cleanup:
                    self.assertTrue(user.RegisterClassW(ctypes.byref(cls)), ctypes.get_last_error())
                    cleanup.callback(user.UnregisterClassW, cls.name, instance)
                    def make_window(class_name, title):
                        hwnd = user.CreateWindowExW(0, class_name, title, 0x10CF0000,
                                                    1, 1, 50, 50, None, None, instance, None)
                        self.assertTrue(hwnd, ctypes.get_last_error())
                        cleanup.callback(user.DestroyWindow, hwnd)
                        return hwnd
                    browser = make_window(cls.name, "J.A.R.V.I.S. - Google Chrome")
                    editor = make_window("STATIC", "Synthetic editor")
                    user.SetActiveWindow(editor)
                    self.assertEqual(user.GetActiveWindow(), editor)
                    self.assertFalse(key.poll())
                    self.assertTrue(key.registered, ctypes.get_last_error())
                    run(lambda: competing(False))
                    # Deliver a message only to this test thread; never synthesize
                    # keystrokes or activate any physical desktop/window.
                    self.assertTrue(user.PostThreadMessageW(kernel.GetCurrentThreadId(), key.WM_HOTKEY,
                                                            key.HOTKEY_ID, (voice.VK_D << 16) | 2))
                    self.assertTrue(key.poll())
                    self.assertFalse(key.poll())
                    user.SetActiveWindow(browser)
                    self.assertEqual(user.GetActiveWindow(), browser)
                    self.assertFalse(key.poll())
                    run(lambda: competing(True))
                    user.SetWindowTextW(browser, "Another tab - Google Chrome")
                    key.poll()
                    self.assertTrue(key.registered)
            finally:
                key.close()
            run(lambda: competing(True))
            warning.assert_not_called()
        run(probe)


if __name__ == "__main__":
    unittest.main()
