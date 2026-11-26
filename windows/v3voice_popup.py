"""A nonactivating Windows voice pop-up, with an offline PNG preview mode."""

import argparse
import ctypes
from ctypes import wintypes as w
from dataclasses import replace
from math import ceil
import os
from pathlib import Path
import struct
import threading
from time import monotonic, sleep

from PIL import Image

if __package__:
    from . import v3voice_render as drawing
else:
    import v3voice_render as drawing


WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
LLMHF_INJECTED = 0x01
LLMHF_LOWER_IL_INJECTED = 0x02
_INJECTED = LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED
_FRAME_SECONDS = 1 / 30
_ANIMATED = ("listening", "working", "speaking")
_STATIC = ("resting", "finished", "problem")
_WINDOW_STYLE = 0x00080000 | 0x00000008 | 0x08000000 | 0x00000080
#               LAYERED      TOPMOST      NOACTIVATE   TOOLWINDOW


def physical_click(flags, message):
    return message == WM_LBUTTONUP and not flags & _INJECTED


def placement(work_area, dimensions, scale):
    left, _, right, bottom = work_area
    width, height = dimensions
    return left + (right - left - width) // 2, bottom - round(80 * scale) - height


def premultiplied_bgra(image):
    red, green, blue, alpha = image.convert("RGBa").split()
    return Image.merge("RGBA", (blue, green, red, alpha)).tobytes()


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("size", w.DWORD), ("screen", w.RECT), ("work", w.RECT), ("flags", w.DWORD)]


class _MouseEvent(ctypes.Structure):
    _fields_ = [("point", w.POINT), ("mouse_data", w.DWORD), ("flags", w.DWORD),
                ("time", w.DWORD), ("extra", ctypes.c_size_t)]


class _Message(ctypes.Structure):
    _fields_ = [("hwnd", w.HWND), ("message", w.UINT), ("wparam", w.WPARAM), ("lparam", w.LPARAM),
                ("time", w.DWORD), ("point", w.POINT), ("private", w.DWORD)]


class _Win32:
    """Own the message pump, hooks, window and top-down DIB on one thread."""

    def __init__(self):
        self.hwnd = self.dc = self.bitmap = self.previous = self.hook = self.signal = None
        self.dpi_context = self.registered = None
        self.shown = False
        self.dimensions = None

    def open(self, mouse_handler):
        u = self.user = ctypes.WinDLL("user32", use_last_error=True)
        g = self.gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        k = self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        ptr, integer, result = ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t
        window_proc = ctypes.WINFUNCTYPE(result, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        hook_proc = ctypes.WINFUNCTYPE(result, integer, w.WPARAM, w.LPARAM)

        class WindowClass(ctypes.Structure):
            _fields_ = [("style", w.UINT), ("proc", window_proc), ("class_extra", integer),
                        ("window_extra", integer), ("instance", w.HINSTANCE), ("icon", w.HICON),
                        ("cursor", w.HANDLE), ("background", w.HBRUSH), ("menu", w.LPCWSTR),
                        ("name", w.LPCWSTR)]

        def signature(function, restype, *args):
            function.restype, function.argtypes = restype, args

        signature(k.GetModuleHandleW, w.HMODULE, w.LPCWSTR)
        signature(k.CreateEventW, w.HANDLE, ptr, w.BOOL, w.BOOL, w.LPCWSTR)
        signature(k.SetEvent, w.BOOL, w.HANDLE)
        signature(k.CloseHandle, w.BOOL, w.HANDLE)
        signature(k.GetTickCount64, ctypes.c_uint64)
        signature(u.SetThreadDpiAwarenessContext, ptr, ptr)
        signature(u.RegisterClassW, w.ATOM, ctypes.POINTER(WindowClass))
        signature(u.UnregisterClassW, w.BOOL, w.LPCWSTR, w.HINSTANCE)
        signature(u.CreateWindowExW, w.HWND, w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                  integer, integer, integer, integer, w.HWND, w.HMENU, w.HINSTANCE, ptr)
        signature(u.DestroyWindow, w.BOOL, w.HWND)
        signature(u.ShowWindow, w.BOOL, w.HWND, integer)
        signature(u.DefWindowProcW, result, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        signature(u.GetForegroundWindow, w.HWND)
        signature(u.MonitorFromWindow, w.HMONITOR, w.HWND, w.DWORD)
        signature(u.MonitorFromPoint, w.HMONITOR, w.POINT, w.DWORD)
        signature(u.GetMonitorInfoW, w.BOOL, w.HMONITOR, ctypes.POINTER(_MonitorInfo))
        signature(u.GetDpiForWindow, w.UINT, w.HWND)
        signature(u.WindowFromPoint, w.HWND, w.POINT)
        signature(u.UpdateLayeredWindow, w.BOOL, w.HWND, w.HDC, ptr, ptr, w.HDC, ptr, w.DWORD, ptr, w.DWORD)
        signature(g.CreateCompatibleDC, w.HDC, w.HDC)
        signature(g.CreateDIBSection, w.HBITMAP, w.HDC, ptr, w.UINT, ctypes.POINTER(ptr), w.HANDLE, w.DWORD)
        signature(g.SelectObject, w.HGDIOBJ, w.HDC, w.HGDIOBJ)
        signature(g.DeleteObject, w.BOOL, w.HGDIOBJ)
        signature(g.DeleteDC, w.BOOL, w.HDC)
        signature(u.SetWindowsHookExW, w.HANDLE, integer, hook_proc, w.HINSTANCE, w.DWORD)
        signature(u.CallNextHookEx, result, w.HANDLE, integer, w.WPARAM, w.LPARAM)
        signature(u.UnhookWindowsHookEx, w.BOOL, w.HANDLE)
        signature(u.MsgWaitForMultipleObjectsEx, w.DWORD, w.DWORD, ctypes.POINTER(w.HANDLE),
                  w.DWORD, w.DWORD, w.DWORD)
        signature(u.PeekMessageW, w.BOOL, ctypes.POINTER(_Message), w.HWND, w.UINT, w.UINT, w.UINT)
        signature(u.DispatchMessageW, result, ctypes.POINTER(_Message))

        @window_proc
        def window_event(hwnd, message, wparam, lparam):
            if message == 0x0021:  # WM_MOUSEACTIVATE
                return 3  # MA_NOACTIVATE
            if message in (WM_LBUTTONDOWN, WM_LBUTTONUP):
                return 0  # Clicks are handled exclusively by the low-level hook.
            return u.DefWindowProcW(hwnd, message, wparam, lparam)

        @hook_proc
        def mouse_event(code, message, data):
            try:
                if code == 0 and message in (WM_LBUTTONDOWN, WM_LBUTTONUP):
                    event = ctypes.cast(data, ctypes.POINTER(_MouseEvent)).contents
                    # Expand the message's 32-bit uptime to the same clock
                    # used by paint(), including across a DWORD wrap.
                    now = k.GetTickCount64()
                    event_time = now - ((now - event.time) & 0xFFFFFFFF)
                    mouse_handler(event.flags, message, event.point.x, event.point.y, event_time)
            except Exception:
                pass  # A failed hit check cannot deliver a click.
            return u.CallNextHookEx(None, code, message, data)

        self.window_proc, self.mouse_proc = window_event, mouse_event
        self.dpi_context = self._check(u.SetThreadDpiAwarenessContext(-4))  # per-monitor v2
        self.signal = self._check(k.CreateEventW(None, False, False, None))  # auto-reset
        self.instance = self._check(k.GetModuleHandleW(None))
        self.name = f"JarvisVoicePopup-{id(self):x}"
        klass = WindowClass(proc=self.window_proc, instance=self.instance, name=self.name)
        self.registered = self._check(u.RegisterClassW(ctypes.byref(klass)))
        self.hwnd = self._new_window(0, 0)
        self.dc = self._check(g.CreateCompatibleDC(None))
        self.bits = ptr()
        self.blend = (ctypes.c_ubyte * 4)(0, 0, 255, 1)  # AC_SRC_OVER, AC_SRC_ALPHA

    @staticmethod
    def _check(value):
        if not value:
            raise ctypes.WinError(ctypes.get_last_error())
        return value

    def _new_window(self, x, y):
        return self._check(self.user.CreateWindowExW(
            _WINDOW_STYLE, self.name, "Jarvis Voice", 0x80000000,
            x, y, 1, 1, None, None, self.instance, None))

    def monitor(self):
        u = self.user
        foreground = u.GetForegroundWindow()
        target = self._check(u.MonitorFromWindow(foreground, 2) if foreground
                             else u.MonitorFromPoint(w.POINT(0, 0), 1))  # nearest, or primary
        info = _MonitorInfo(size=ctypes.sizeof(_MonitorInfo))
        self._check(u.GetMonitorInfoW(target, ctypes.byref(info)))
        work = info.work
        if u.MonitorFromWindow(self.hwnd, 2) == target:
            dpi = self._check(u.GetDpiForWindow(self.hwnd))
        else:
            # A foreground app's HWND can report virtualized DPI. Query our own
            # per-monitor-aware HWND on that display, before showing the frame.
            probe = self._new_window((work.left + work.right) // 2, (work.top + work.bottom) // 2)
            try:
                dpi = self._check(u.GetDpiForWindow(probe))
            finally:
                u.DestroyWindow(probe)
        return (work.left, work.top, work.right, work.bottom), dpi / 96

    def _free_bitmap(self):
        if self.previous:
            self.gdi.SelectObject(self.dc, self.previous)
        if self.bitmap:
            self.gdi.DeleteObject(self.bitmap)
        self.bitmap = self.previous = self.dimensions = None

    def paint(self, position, dimensions, data):
        ref = ctypes.byref
        if dimensions != self.dimensions:
            self._free_bitmap()
            width, height = dimensions
            info = struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 32, 0, 0, 0, 0, 0, 0) + bytes(4)
            self.bitmap = self._check(self.gdi.CreateDIBSection(self.dc, info, 0, ref(self.bits), None, 0))
            self._check(self.bits)
            self.previous = self._check(self.gdi.SelectObject(self.dc, self.bitmap))
            self.dimensions = dimensions
        ctypes.memmove(self.bits, data, len(data))
        self._check(self.user.UpdateLayeredWindow(
            self.hwnd, None, ref(w.POINT(*position)), ref(w.SIZE(*dimensions)), self.dc,
            ref(w.POINT(0, 0)), 0, ref(self.blend), 2))  # ULW_ALPHA
        if not self.shown:
            self.user.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE
            self.shown = True
        self.painted_at = self.kernel.GetTickCount64()

    def hide(self):
        if self.shown:
            self.user.ShowWindow(self.hwnd, 0)  # SW_HIDE
            self.shown = False

    def watch_mouse(self, enabled):
        if enabled and not self.hook:
            self.hook = self._check(self.user.SetWindowsHookExW(14, self.mouse_proc, self.instance, 0))
        elif not enabled and self.hook:
            self._check(self.user.UnhookWindowsHookEx(self.hook))
            self.hook = None

    def is_popup_at(self, x, y):
        return self.user.WindowFromPoint(w.POINT(x, y)) == self.hwnd

    def wake(self):
        self._check(self.kernel.SetEvent(self.signal))

    def wait(self, timeout):
        milliseconds = 0xFFFFFFFF if timeout is None else ceil(timeout * 1000)
        handles = (w.HANDLE * 1)(self.signal)
        u = self.user
        if u.MsgWaitForMultipleObjectsEx(1, handles, milliseconds, 0x04FF, 4) == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())  # QS_ALLINPUT, MWMO_INPUTAVAILABLE
        message = _Message()
        ref = ctypes.byref(message)
        for _ in range(32):
            if not u.PeekMessageW(ref, None, 0, 0, 1):  # PM_REMOVE
                break
            if message.message == 0x0012:  # WM_QUIT
                return False
            u.DispatchMessageW(ref)
        return True

    def close(self):
        if self.hook:
            self.user.UnhookWindowsHookEx(self.hook)
        if self.dc:
            self._free_bitmap()
            self.gdi.DeleteDC(self.dc)
        if self.hwnd:
            self.user.DestroyWindow(self.hwnd)
        if self.registered:
            self.user.UnregisterClassW(self.name, self.instance)
        if self.signal:
            self.kernel.CloseHandle(self.signal)
        if self.dpi_context:
            self.user.SetThreadDpiAwarenessContext(self.dpi_context)


class Popup:
    """Publish immutable state from any thread. Clicks run on the window thread.

    The callback receives a dict with id, digest and decision and should return
    promptly. Off Windows, a window requires an explicit adapter.
    """

    def __init__(self, on_click=None, *, win32=None, font_paths=()):
        self._on_click = on_click
        self._fonts = tuple(font_paths)
        self._state = drawing.PopupState()
        self._countdown = None
        self._revision = 0
        self._click_until = None
        self._closed = False
        self._lock = threading.RLock()
        self._thread = self._active = None
        self._shown = self._pressed = self._pending = None
        self.error = None
        if win32 is not None or os.name == "nt":
            api = win32 if win32 is not None else _Win32()
            self._thread = threading.Thread(target=self._run, args=(api,), name="Jarvis voice popup", daemon=True)
            self._thread.start()

    def set(self, state, *, expires_in=None, click_expires_in=None):
        """Publish a state, optionally animating its approval from link seconds.

        Each approval publication supersedes the previous click target, even if
        the text is identical. Pass the duration once for a new approval.
        """
        with self._lock:
            if self._closed:
                return
            if type(state.kind) is not str or state.kind not in ("hidden", "resting", *drawing.LABELS):
                state = drawing.PopupState()
            if type(state.t) not in (int, float) or not -(2**53) <= state.t <= 2**53:
                state = replace(state, t=0.0)
            if state.kind in _STATIC:
                state = drawing.PopupState(kind=state.kind, t=state.t, label=drawing._clean(state.label),
                                           line=drawing._clean(state.line), sub=drawing._clean(state.sub))
                if state == self._state:
                    return
            self._countdown = None
            self._click_until = None
            if state.kind in drawing.APPROVALS and drawing.valid_duration(click_expires_in):
                self._click_until = monotonic() + click_expires_in
            if state.kind in drawing.APPROVALS and expires_in is not None:
                if not drawing.valid_duration(expires_in):
                    state = replace(state, expires=0, buttons=state.buttons and self._click_until is not None)
                elif drawing.buttons_enabled(state):
                    self._countdown = monotonic() + expires_in, expires_in
            self._state = state
            self._revision += 1
            if self._active is not None:
                try:
                    self._active.wake()
                except (OSError, ctypes.ArgumentError) as error:
                    self.error = error

    def close(self):
        with self._lock:
            self._closed = True
            if self._active is not None:
                self._active.wake()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join()

    def _mouse(self, flags, message, x, y, event_time):
        # Hook work only. Deliver the callback after returning to the message pump.
        if message not in (WM_LBUTTONDOWN, WM_LBUTTONUP) or self._shown is None:
            return
        target, state, painted_at = self._shown
        if event_time <= painted_at:
            return  # Same-tick input cannot be placed reliably on either card.
        pressed, self._pressed = self._pressed, None
        if flags & _INJECTED or not self._clickable(target, state):
            return
        _, position, scale = target
        action = drawing.hit_test(state, x - position[0], y - position[1],
                                  scale=scale, font_paths=self._fonts)
        if action is None or not self._active.is_popup_at(x, y):
            return
        hit = target, action
        if message == WM_LBUTTONDOWN:
            self._pressed = hit
        elif physical_click(flags, message) and pressed == hit:
            self._pending = hit

    def _clickable(self, target, state):
        with self._lock:
            return (not self._closed and target[0] == self._revision
                    and drawing.buttons_enabled(state)
                    and (self._click_until is None or monotonic() < self._click_until)
                    and (self._click_until is not None or self._countdown is None
                         or monotonic() < self._countdown[0]))

    def _run(self, api):
        try:
            api.open(self._mouse)
            with self._lock:
                self._active = api
            start = monotonic()
            deadline, last_revision, answered = None, -1, -1
            while True:
                with self._lock:
                    if self._closed:
                        break
                    state, revision, countdown = self._state, self._revision, self._countdown
                    click_until = self._click_until
                now = monotonic()
                timeout = None
                if state.kind == "hidden":
                    if self._shown is not None:
                        self._shown = self._pressed = None
                        api.watch_mouse(False)
                        api.hide()
                else:
                    if revision != last_revision or (deadline is not None and now >= deadline):
                        expires_at = None
                        if countdown is not None:
                            expires_at, duration = countdown
                            left = max(0, (expires_at - now) / duration)
                            state = replace(state, expires=left)
                            if not left and click_until is not None and state.kind == "asking":
                                state = replace(state, kind="needs_click", label=drawing.LABELS["needs_click"],
                                                sub=drawing.CLICK_HINT)
                        if click_until is not None and now >= click_until:
                            state = replace(state, click_live=False)
                        timed = expires_at is not None and now < expires_at
                        animated = state.kind in _ANIMATED or timed
                        if animated:
                            state = replace(state, t=state.t + now - start)
                        work, scale = api.monitor()
                        enabled = drawing.buttons_enabled(state)
                        if not enabled:
                            api.watch_mouse(False)
                        frame = drawing.render(state, scale=scale, font_paths=self._fonts)
                        position = placement(work, frame.size, scale)
                        target = revision, position, scale
                        same_target = self._shown is not None and self._shown[0] == target
                        if not same_target:
                            self._shown = self._pressed = None
                        api.paint(position, frame.size, premultiplied_bgra(frame))
                        painted_at = self._shown[2] if same_target else api.painted_at
                        self._shown = target, state, painted_at
                        if enabled:
                            api.watch_mouse(self._clickable(target, state))
                        deadline = now + _FRAME_SECONDS if animated else None
                        if timed:
                            deadline = min(deadline, expires_at)
                        if click_until is not None and now < click_until:
                            deadline = click_until if deadline is None else min(deadline, click_until)
                    if deadline is not None:
                        timeout = max(0, deadline - monotonic())
                last_revision = revision
                pending, self._pending = self._pending, None
                if pending is not None and self._shown is not None:
                    target, action = pending
                    if target == self._shown[0] and revision != answered and self._on_click is not None:
                        # Serialize delivery with state publications. The callback only queues work.
                        with self._lock:
                            shown_state = self._shown[1]
                            if self._clickable(target, shown_state):
                                answered = revision
                                self._on_click({"id": shown_state.id, "digest": shown_state.digest,
                                                "decision": action})
                if not api.wait(timeout):
                    break
        except (OSError, ctypes.ArgumentError) as error:
            self.error = error
        finally:
            with self._lock:
                self._active = None
                self._closed = True
            api.close()


def _sample_states():
    state = drawing.PopupState()
    events = (
        {"kind": "state", "state": "resting"},
        {"kind": "partial", "text": "open the practice folder and start a…"},
        {"kind": "heard", "text": "Opening the practice folder"},
        {"kind": "say", "text": "The folder is open, and the practice thread is running."},
        {"kind": "asking", "sentence": "Send “use the smaller logo” to the practice thread",
         "expires_in": 60, "id": "sample-approval", "digest": "sample-digest"},
        {"kind": "asking", "sentence": "Pay $12.00 for the example domain renewal",
         "expires_in": 60, "voice_allowed": False, "id": "sample-click", "digest": "click-digest"},
        {"kind": "notice", "title": "Practice thread", "text": "Login page fixed, two tests added."},
        {"kind": "error"},
    )
    states = [state, *(drawing.state_from_event(state, event) for event in events)]
    samples = {
        "listening": {"level": .72}, "working": {"sub": '“open the practice folder and…”'},
        "speaking": {"level": .56}, "asking": {"expires": .62}, "needs_click": {"expires": .88},
    }
    return tuple(replace(state, **samples.get(state.kind, {})) for state in states)


def _render_dir(folder):
    folder.mkdir(parents=True, exist_ok=True)
    for state in _sample_states():
        drawing.render(state).save(folder / f"{state.kind}.png")
        if state.kind == "listening":
            for index, t in enumerate((.25, .5, .75), 1):
                drawing.render(replace(state, t=t)).save(folder / f"listening-{index}.png")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--render-dir", type=Path, metavar="FOLDER")
    mode.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    if args.render_dir is not None:
        _render_dir(args.render_dir)
        return
    if os.name != "nt":
        parser.error("--demo requires Windows")
    popup = Popup()
    try:
        states = _sample_states()
        seconds = 20 / len(states)
        for state in states:
            popup.set(state, expires_in=seconds)
            sleep(seconds)
    finally:
        popup.close()
    if popup.error is not None:
        parser.exit(1, "The Windows pop-up could not be displayed.\n")


if __name__ == "__main__":
    main()
