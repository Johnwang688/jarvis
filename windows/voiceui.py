"""Nonactivating Windows voice popup and display-edge indicator, owned by the Tk thread."""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import ctypes
from ctypes import wintypes as w
import math
import os
import queue
import struct
import time

if __package__:
    from . import driveui
else:
    import driveui

STATES = ("hidden", "listening", "transcribing", "thinking", "answering", "profile_choice", "done", "error")
CYAN, AMBER, RED = "#67e8f9", "#fbbf24", "#fb7185"
PANEL, INK = "#14232e", "#edf6fa"
HEADER_HEIGHT = 29
WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW = 0x08000000, 0x00000080
SWP_NOACTIVATE, SWP_SHOWWINDOW, SW_SHOWNOACTIVATE = 0x10, 0x40, 4


def bars(levels, n=24):
    if n <= 0:
        return []
    latest = list(levels)[-n:]
    return [0.0] * (n - len(latest)) + [
        min(1.0, math.sqrt(max(0.0, float(level)) / 0.25)) for level in latest
    ]


def place(screen_w, screen_h, w, h):
    return (screen_w - w) // 2, screen_h - h - 80


class _DisplayState:
    """A single deadline avoids old hide callbacks hiding a new recording."""

    def __init__(self, clock=time.monotonic, recording=lambda: False):
        self.clock = clock
        self.recording = recording
        self.state, self.text, self.hide_at = "hidden", "", None
        self.dismissed = False

    def dismiss(self):
        self.state, self.text, self.hide_at = "hidden", "", None
        self.dismissed = True

    def set(self, state, text=None, hide_at=None):
        # A wake timeout or the previous reply may already be queued when
        # the hotkey acquires the mic. Only its listening state belongs here.
        if self.recording() and state != "listening":
            return False
        if self.dismissed:
            if state != "listening":
                if state in ("done", "error", "hidden", "profile_choice"):
                    self.dismissed = False
                return False
            self.dismissed = False
        self.state = state
        labels = {"listening": "Listening…", "transcribing": "Transcribing…",
                  "thinking": "Thinking…", "done": "Done", "error": "Voice unavailable"}
        self.text = "" if state == "hidden" else str(text if text is not None else labels.get(state, ""))
        delay = {"done": 4.0, "error": 5.0}.get(state)
        self.hide_at = self.clock() + delay if delay is not None else hide_at
        return True

    def expire(self):
        if self.hide_at is not None and self.clock() >= self.hide_at:
            return self.set("hidden")
        return False


class Overlay:
    def __init__(self, root, recording=lambda: False):
        import tkinter as tk

        self.root = root
        self.events = queue.Queue()
        self.display = _DisplayState(recording=recording)
        self.levels = deque(maxlen=24)
        self.heights = [0.0] * 24
        self.window = tk.Toplevel(root, takefocus=False)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.protocol("WM_DELETE_WINDOW", self._dismiss)
        self.window.attributes("-topmost", True)
        # Transparent corners around the rounded Canvas panel on Windows.
        key = "#010203"
        self.window.configure(bg=key)
        if os.name == "nt":
            self.window.attributes("-transparentcolor", key)
        self.canvas = tk.Canvas(self.window, width=440, height=96, bg=key,
                                highlightthickness=0, takefocus=False)
        self.canvas.pack()
        self.panel = self.canvas.create_polygon(0, 0, 0, 0, fill=PANEL,
                                                 outline="#294553", smooth=True, splinesteps=24)
        self.canvas.create_text(16, 15, text="Jarvis voice", anchor="w",
                                fill="#a4beca", font=("Segoe UI", 9))
        self.metric = self.canvas.create_text(144, 15, text="", anchor="w",
                                              fill="#a4beca", font=("Segoe UI", 9))
        self.close_bg = self.canvas.create_rectangle(0, 0, 0, 0, fill=PANEL, outline="",
                                                     tags="close")
        self.close_icon = self.canvas.create_text(0, 0, text="×", fill=INK, tags="close",
                                                   font=("Segoe UI", 15))
        self.canvas.tag_bind("close", "<Enter>", lambda _: self._hover_close(True))
        self.canvas.tag_bind("close", "<Leave>", lambda _: self._hover_close(False))
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.position = self.drag_offset = None
        self.close_pressed = False
        self.bar_ids = [self.canvas.create_rectangle(0, 0, 0, 0, fill=CYAN, outline="")
                        for _ in range(24)]
        self.label = self.canvas.create_text(220, 66, text="", anchor="n", width=400,
                                             fill=INK, font=("Segoe UI", 12))
        self.profile_list = None
        self.user32 = None
        if os.name == "nt":
            self._native_window()
        self.width, self.height = 440, 96
        self.root.after(33, self._tick)

    def _native_window(self):
        import ctypes
        from ctypes import wintypes

        self.user32 = ctypes.windll.user32
        u = self.user32
        u.GetParent.argtypes, u.GetParent.restype = [wintypes.HWND], wintypes.HWND
        u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        u.GetWindowLongW.restype = ctypes.c_long
        u.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
        u.SetWindowLongW.restype = ctypes.c_long
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.ShowWindow.restype = wintypes.BOOL
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        u.SetWindowPos.restype = wintypes.BOOL
        u.GetSystemMetrics.argtypes, u.GetSystemMetrics.restype = [ctypes.c_int], ctypes.c_int
        self.window.update_idletasks()
        self.hwnd = u.GetParent(self.window.winfo_id()) or self.window.winfo_id()
        style = u.GetWindowLongW(self.hwnd, -20)
        u.SetWindowLongW(self.hwnd, -20, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)

    def set_state(self, state, text=None, *deadline):
        if state not in STATES:
            raise ValueError("Unknown voice state")
        self.events.put(("state", state, text, *deadline))

    def timing(self, label, milliseconds):
        self.events.put(("timing", label, milliseconds))

    def push_level(self, rms):
        self.events.put(("level", float(rms), None))

    def _hover_close(self, hovered):
        self.canvas.itemconfigure(self.close_bg, fill="#294553" if hovered else PANEL)
        self.canvas.configure(cursor="hand2" if hovered else "")

    def _on_close(self, event):
        return self.width - 33 <= event.x <= self.width - 7 and 3 <= event.y < HEADER_HEIGHT

    def _press(self, event):
        self.close_pressed = self._on_close(event)
        if not self.close_pressed and 0 <= event.y < HEADER_HEIGHT:
            self.drag_offset = event.x, event.y
            self.canvas.configure(cursor="fleur")

    def _drag(self, event):
        if self.drag_offset is not None:
            dx, dy = self.drag_offset
            self.position = (event.x_root - dx, event.y_root - dy)
            self._place()

    def _release(self, event):
        close = self.close_pressed and self._on_close(event)
        self.drag_offset = None
        self.close_pressed = False
        self._hover_close(self._on_close(event))
        if close:
            self._dismiss()

    def _dismiss(self):
        self.display.dismiss()
        self.drag_offset = None
        self.close_pressed = False
        self._layout()

    def _place(self):
        w, h = self.width, self.height
        sw, sh = self.window.winfo_screenwidth(), self.window.winfo_screenheight()
        x, y = self.position if self.position is not None else place(sw, sh, w, h)
        left, top, width, height = 0, 0, sw, sh
        if self.user32:
            metric = self.user32.GetSystemMetrics
            # Virtual desktop bounds include monitors left or above the primary.
            left, top, width, height = (metric(i) for i in (76, 77, 78, 79))
        x = max(left, min(x, left + width - w))
        y = max(top, min(y, top + height - h))
        if self.position is not None:
            self.position = x, y
        if self.user32:
            self.user32.SetWindowPos(self.hwnd, -1, x, y, w, h,
                                     SWP_NOACTIVATE | SWP_SHOWWINDOW)
        else:
            self.window.geometry(f"{w}x{h}{x:+d}{y:+d}")

    def _scroll_profiles(self, names, top):
        """Keep the spoken reply instructions visible while the owner browses names."""
        import tkinter as tk

        if self.profile_list is None:
            self.profile_list = tk.Frame(self.canvas, bg=PANEL, takefocus=False)
            self.profile_text = tk.Canvas(self.profile_list, bg=PANEL, highlightthickness=0,
                                          takefocus=False, yscrollincrement=24)
            self.profile_names = self.profile_text.create_text(0, 0, anchor="nw", fill=INK,
                                                                font=("Segoe UI", 12))
            scroll = tk.Scrollbar(self.profile_list, command=self.profile_text.yview, takefocus=False)
            self.profile_text.configure(yscrollcommand=scroll.set)
            self.profile_text.bind("<MouseWheel>", lambda e: self.profile_text.yview_scroll(
                -1 if e.delta > 0 else 1, "units"))
            scroll.pack(side="right", fill="y")
            self.profile_text.pack(side="left", fill="both", expand=True)
            self.profile_scroll = scroll
        wrap_width = self.width - 40 - self.profile_scroll.winfo_reqwidth() - 4
        self.profile_text.itemconfigure(self.profile_names, text=names, width=wrap_width)
        self.profile_text.configure(scrollregion=self.profile_text.bbox(self.profile_names))
        self.profile_text.yview_moveto(0)
        self.profile_list.place(x=20, y=top, width=self.width - 40, height=self.height - top - 18)

    def _layout(self):
        state = self.display.state
        if self.profile_list is not None:
            self.profile_list.place_forget()
            self.profile_text.itemconfigure(self.profile_names, text="")
        if state == "hidden":
            if self.user32:
                self.user32.ShowWindow(self.hwnd, 0)
            else:
                self.window.withdraw()
            self.canvas.itemconfigure(self.label, text="")
            return
        self.width = 640 if state == "answering" else 440
        max_height = 254
        if state == "profile_choice":
            self.width = min(640, self.window.winfo_screenwidth() - 16)
            max_height = self.window.winfo_screenheight() - 80
        self.canvas.itemconfigure(self.label, text=self.display.text, width=self.width - 40,
                                  fill=RED if state == "error" else
                                  AMBER if state in ("transcribing", "thinking") else INK)
        self.canvas.coords(self.label, self.width / 2, 78)
        box = self.canvas.bbox(self.label)
        required_height = max(110, (box[3] if box else 94) + 18)
        self.height = min(max_height, required_height)
        if state == "profile_choice" and required_height > max_height:
            question, names, instruction = self.display.text.split("\n", 2)
            self.canvas.itemconfigure(self.label, text=f"{question}\n{instruction}")
            self._scroll_profiles(names, self.canvas.bbox(self.label)[3] + 8)
        w, h, r = self.width, self.height, 18
        self.canvas.configure(width=w, height=h)
        self.canvas.coords(self.panel, r, 1, w-r, 1, w-1, 1, w-1, r,
                           w-1, h-r, w-1, h-1, w-r, h-1, r, h-1,
                           1, h-1, 1, h-r, 1, r, 1, 1)
        self.canvas.coords(self.close_bg, w - 33, 3, w - 7, HEADER_HEIGHT)
        self.canvas.coords(self.close_icon, w - 20, 15)
        if self.user32:
            # Keep Tk's requested client size, but let Win32 place it in
            # absolute screen coordinates without activating the window.
            self.window.geometry(f"{w}x{h}")
        self._place()
        if self.user32:
            self.user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
        else:
            self.window.deiconify()

    def _tick(self):
        changed = False
        # Bound a single drain so a busy producer cannot starve Tk's event loop.
        for _ in range(256):
            try:
                kind, value, text, *deadline = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "timing":
                self.canvas.itemconfigure(self.metric, text=f"{value}: {text} ms")
            elif kind == "level":
                self.levels.append(value)
            else:
                if not self.display.set(value, text, *deadline):
                    continue
                if value == "listening":
                    self.canvas.itemconfigure(self.metric, text="")
                if value in ("listening", "hidden"):
                    self.levels.clear()
                    self.heights = [0.0] * 24
                changed = True
        changed = self.display.expire() or changed
        if changed:
            self._layout()
        if self.display.state != "hidden":
            target = bars(self.levels)
            for i, ident in enumerate(self.bar_ids):
                self.heights[i] += (target[i] - self.heights[i]) * 0.35
                half = 2 + 18 * self.heights[i]
                left = self.width / 2 - 120 + i * 10
                self.canvas.coords(ident, left, 51-half, left+6, 51+half)
        self.root.after(33, self._tick)


INSET = 8  # physical pixels reserved for Windows' own capture border
ALPHA = (40, 144, 180, 118, 66, 32, 12)  # outside to inside
WASHES = ((42, 143, 160), (111, 195, 223), (52, 104, 196), (100, 80, 196))
AURORA_WINDOW_STYLE = 0x00080000 | 0x00000020 | 0x08000000 | 0x00000080 | 0x00000008
#       WS_EX_LAYERED   TRANSPARENT   NOACTIVATE    TOOLWINDOW    TOPMOST


def _palette(index):
    pos = index * len(WASHES) / 256
    first = WASHES[int(pos)]
    second = WASHES[(int(pos) + 1) % len(WASHES)]
    t = pos % 1
    t = t * t * (3 - 2 * t)
    r, g, b = (a + (z - a) * t for a, z in zip(first, second))
    return tuple(bytes((round(b * alpha / 255), round(g * alpha / 255),
                        round(r * alpha / 255), alpha)) for alpha in ALPHA)


# UpdateLayeredWindow takes premultiplied BGRA. Reuse the colour ramp for every monitor/frame.
PALETTE = tuple(_palette(i) for i in range(256))


def edge_rects(monitor):
    left, top, right, bottom = monitor
    x, y = left + INSET, top + INSET
    width, height = right - left - 2 * INSET, bottom - top - 2 * INSET
    band = len(ALPHA)
    if min(width, height) <= 2 * band:
        return ()
    return (("top", x, y, width, band), ("right", x + width - band, y + band, band, height - 2 * band),
            ("bottom", x, y + height - band, width, band), ("left", x, y + band, band, height - 2 * band))


def _raster(side, width, height, phase):
    horizontal = side in ("top", "bottom")
    length = width if horizontal else height
    count = min(160, (length + 7) // 8)
    start, direction = {"top": (0, 1), "right": (64, 1), "bottom": (192, -1), "left": (256, -1)}[side]
    bands = range(len(ALPHA)) if side in ("top", "left") else range(len(ALPHA) - 1, -1, -1)
    spans = [(PALETTE[(phase + start + direction * (i * 64 // count)) % 256],
              (i + 1) * length // count - i * length // count) for i in range(count)]
    if horizontal:
        return b"".join(colors[d] * n for d in bands for colors, n in spans)
    return b"".join(b"".join(colors[d] for d in bands) * n for colors, n in spans)


class _AuroraWin32:
    def __init__(self):
        u = self.user = ctypes.WinDLL("user32", use_last_error=True)
        g = self.gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        ptr = ctypes.c_void_p
        self.monitor_proc = ctypes.WINFUNCTYPE(w.BOOL, w.HMONITOR, w.HDC, ctypes.POINTER(w.RECT), w.LPARAM)
        def signature(fn, result, *args):
            fn.restype, fn.argtypes = result, args
        signature(u.EnumDisplayMonitors, w.BOOL, w.HDC, ptr, self.monitor_proc, w.LPARAM)
        signature(u.SystemParametersInfoW, w.BOOL, w.UINT, w.UINT, ptr, w.UINT)
        signature(u.SetThreadDpiAwarenessContext, ptr, ptr)
        signature(u.CreateWindowExW, w.HWND, w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                  ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, ptr)
        signature(u.UpdateLayeredWindow, w.BOOL, w.HWND, w.HDC, ptr, ptr, w.HDC, ptr, w.DWORD, ptr, w.DWORD)
        signature(u.DestroyWindow, w.BOOL, w.HWND)
        signature(u.ShowWindow, w.BOOL, w.HWND, ctypes.c_int)
        signature(g.CreateCompatibleDC, w.HDC, w.HDC)
        signature(g.CreateDIBSection, w.HBITMAP, w.HDC, ptr, w.UINT, ctypes.POINTER(ptr), w.HANDLE, w.DWORD)
        signature(g.SelectObject, w.HGDIOBJ, w.HDC, w.HGDIOBJ)
        signature(g.DeleteObject, w.BOOL, w.HGDIOBJ)
        signature(g.DeleteDC, w.BOOL, w.HDC)

    @contextmanager
    def physical_pixels(self):
        # Tk may use another DPI context. Restore it before dispatching any Tk events.
        previous = self.user.SetThreadDpiAwarenessContext(-4)  # per-monitor aware v2
        if not previous:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield
        finally:
            self.user.SetThreadDpiAwarenessContext(previous)

    def monitors(self):
        found = []
        @self.monitor_proc
        def collect(_monitor, _dc, bounds, _data):
            r = bounds.contents
            found.append((r.left, r.top, r.right, r.bottom))
            return 1
        if not self.user.EnumDisplayMonitors(None, None, collect, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        return tuple(sorted(set(found)))

    def animated(self):
        enabled = w.BOOL()
        return bool(self.user.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0) and enabled.value)


class _AuroraStrip:
    def __init__(self, api, rect):
        self.api = api
        self.side, x, y, self.width, self.height = rect
        self.pos, self.size, self.origin = w.POINT(x, y), w.SIZE(self.width, self.height), w.POINT(0, 0)
        self.blend = (ctypes.c_ubyte * 4)(0, 0, 255, 1)  # AC_SRC_OVER, AC_SRC_ALPHA
        self.hwnd = self.dc = self.bitmap = self.previous = None
        self.bits = ctypes.c_void_p()
        self.shown = False
        try:
            self.hwnd = api.user.CreateWindowExW(AURORA_WINDOW_STYLE, "STATIC", "Jarvis Voice Aurora", 0x80000000,
                                                x, y, self.width, self.height, None, None, None, None)
            self.dc = api.gdi.CreateCompatibleDC(None)
            if not self.hwnd or not self.dc:
                raise ctypes.WinError(ctypes.get_last_error())
            # Top-down 32bpp BI_RGB DIB. Only the strip has a backing surface.
            info = struct.pack("<IiiHHIIiiII", 40, self.width, -self.height, 1, 32, 0, 0, 0, 0, 0, 0) + bytes(4)
            self.bitmap = api.gdi.CreateDIBSection(self.dc, info, 0, ctypes.byref(self.bits), None, 0)
            if not self.bitmap or not self.bits:
                raise ctypes.WinError(ctypes.get_last_error())
            self.previous = api.gdi.SelectObject(self.dc, self.bitmap)
            if not self.previous:
                raise ctypes.WinError(ctypes.get_last_error())
        except Exception:
            self.close()
            raise

    def paint(self, phase):
        data = _raster(self.side, self.width, self.height, phase)
        ctypes.memmove(self.bits, data, len(data))
        ref = ctypes.byref
        if not self.api.user.UpdateLayeredWindow(self.hwnd, None, ref(self.pos), ref(self.size),
                                                 self.dc, ref(self.origin), 0, ref(self.blend), 2):
            raise ctypes.WinError(ctypes.get_last_error())
        if not self.shown:
            self.api.user.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE
            self.shown = True

    def close(self):
        if self.previous:
            self.api.gdi.SelectObject(self.dc, self.previous)
        if self.bitmap:
            self.api.gdi.DeleteObject(self.bitmap)
        if self.dc:
            self.api.gdi.DeleteDC(self.dc)
        if self.hwnd:
            self.api.user.DestroyWindow(self.hwnd)
        self.hwnd = self.dc = self.bitmap = self.previous = None


class AuroraEdges:
    """Paint only the inset screen edges. All calls run on the Tk thread."""

    def __init__(self, root):
        self.root, self.api = root, None
        self.visible = self.closed = False
        self.timer = None
        self.strips = []
        self.layout = self.last_phase = None
        self.next_settings = 0.0
        self.moving = False
        self.failures = 0

    def _clear(self):
        for strip in self.strips:
            strip.close()
        self.strips.clear()
        self.layout = self.last_phase = None
        self.next_settings = 0.0

    def _frame(self):
        self.timer = None
        if not self.visible:
            return
        try:
            if self.api is None:
                self.api = _AuroraWin32()
            with self.api.physical_pixels():
                now = time.monotonic()
                if now >= self.next_settings:
                    layout = self.api.monitors()
                    if layout != self.layout:
                        self._clear()
                        for monitor in layout:
                            for rect in edge_rects(monitor):
                                self.strips.append(_AuroraStrip(self.api, rect))
                        self.layout = layout
                    self.moving = self.api.animated()
                    self.next_settings = now + 2
                phase = int(now * 10) % 256 if self.moving else 0
                if phase != self.last_phase:
                    for strip in self.strips:
                        strip.paint(phase)
                    self.last_phase = phase
            delay = 100 if self.moving else 2000
        except (OSError, ValueError, AttributeError, MemoryError, ctypes.ArgumentError):
            self._clear()
            self.failures += 1
            if self.failures >= 3:
                return  # next explicit off/on can retry
            delay = 1000 * self.failures
        try:
            self.timer = self.root.after(delay, self._frame)
        except Exception:
            # Tk can already be tearing down when its last frame returns.
            self._clear()

    def set_visible(self, visible):
        visible = bool(visible)
        if self.closed or self.visible == visible or os.name != "nt":
            return
        self.visible = visible
        if visible:
            self.failures = 0
            self._frame()
        else:
            if self.timer is not None:
                try:
                    self.root.after_cancel(self.timer)
                except Exception:
                    pass  # an already-destroyed Tk interpreter has no timer
                self.timer = None
            self._clear()

    def close(self):
        self.set_visible(False)
        self.closed = True


class _Paint(ctypes.Structure):
    _fields_ = [("dc", w.HDC), ("erase", w.BOOL), ("rect", w.RECT),
                ("restore", w.BOOL), ("update", w.BOOL), ("reserved", w.BYTE * 32)]


class _KeyEvent(ctypes.Structure):
    _fields_ = [("key", w.DWORD), ("scan", w.DWORD), ("flags", w.DWORD),
                ("time", w.DWORD), ("extra", ctypes.c_size_t)]


class _BadgeMonitorInfo(ctypes.Structure):
    _fields_ = [("size", w.DWORD), ("screen", w.RECT), ("work", w.RECT), ("flags", w.DWORD)]


class _DriveWin32(_AuroraWin32):
    def __init__(self, window_proc, key_proc):
        # Windows verification pending: shared user32/gdi32 loading and DPI/window bindings.
        super().__init__()
        # Windows verification pending: native class, GDI and low-level hook signatures (including on x64).
        u, g = self.user, self.gdi
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        callback = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        hook_callback = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, w.WPARAM, w.LPARAM)
        self.window_proc, self.key_proc = callback(window_proc), hook_callback(key_proc)

        class WindowClass(ctypes.Structure):
            _fields_ = [("style", w.UINT), ("proc", callback), ("class_extra", ctypes.c_int),
                        ("window_extra", ctypes.c_int), ("instance", w.HINSTANCE), ("icon", w.HICON),
                        ("cursor", w.HANDLE), ("background", w.HBRUSH), ("menu", w.LPCWSTR),
                        ("name", w.LPCWSTR)]

        def signature(fn, result, *args):
            fn.restype, fn.argtypes = result, args

        rect = ctypes.POINTER(w.RECT)
        integer = ctypes.c_int
        signature(kernel.GetModuleHandleW, w.HMODULE, w.LPCWSTR)
        signature(u.RegisterClassW, w.ATOM, ctypes.POINTER(WindowClass))
        signature(u.UnregisterClassW, w.BOOL, w.LPCWSTR, w.HINSTANCE)
        signature(u.MonitorFromPoint, w.HMONITOR, w.POINT, w.DWORD)
        signature(u.GetMonitorInfoW, w.BOOL, w.HMONITOR, ctypes.POINTER(_BadgeMonitorInfo))
        signature(u.SetLayeredWindowAttributes, w.BOOL, w.HWND, w.COLORREF, w.BYTE, w.DWORD)
        signature(u.SetWindowPos, w.BOOL, w.HWND, w.HWND, integer, integer, integer, integer, w.UINT)
        signature(u.DefWindowProcW, ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        signature(u.BeginPaint, w.HDC, w.HWND, ctypes.POINTER(_Paint))
        signature(u.EndPaint, w.BOOL, w.HWND, ctypes.POINTER(_Paint))
        signature(u.FillRect, integer, w.HDC, rect, w.HBRUSH)
        signature(u.InvalidateRect, w.BOOL, w.HWND, rect, w.BOOL)
        signature(u.DrawTextW, integer, w.HDC, w.LPCWSTR, integer, rect, w.UINT)
        signature(g.CreateSolidBrush, w.HBRUSH, w.COLORREF)
        signature(g.SetTextColor, w.COLORREF, w.HDC, w.COLORREF)
        signature(g.SetBkMode, integer, w.HDC, integer)
        signature(g.GetStockObject, w.HGDIOBJ, integer)
        signature(u.SetWindowsHookExW, w.HANDLE, integer, hook_callback, w.HINSTANCE, w.DWORD)
        signature(u.CallNextHookEx, ctypes.c_ssize_t, w.HANDLE, integer, w.WPARAM, w.LPARAM)
        signature(u.UnhookWindowsHookEx, w.BOOL, w.HANDLE)

        # Windows verification pending: register the helper-owned window class.
        self.instance = kernel.GetModuleHandleW(None)
        self.name = "JarvisDrivingBadge"
        klass = WindowClass(proc=self.window_proc, instance=self.instance, name=self.name)
        if not u.RegisterClassW(ctypes.byref(klass)):
            raise ctypes.WinError(ctypes.get_last_error())

    def primary_screen(self):
        # Windows verification pending: the primary monitor owns (0, 0) in physical pixels.
        with self.physical_pixels():
            monitor = self.user.MonitorFromPoint(w.POINT(0, 0), 1)  # MONITOR_DEFAULTTOPRIMARY
            info = _BadgeMonitorInfo(size=ctypes.sizeof(_BadgeMonitorInfo))
            if not self.user.GetMonitorInfoW(monitor, ctypes.byref(info)):
                raise ctypes.WinError(ctypes.get_last_error())
        bounds = info.screen
        return driveui.Rect(bounds.left, bounds.top, bounds.right, bounds.bottom)

    def window(self, bounds, title, *, transparent):
        # Windows verification pending: layered WS_EX_TRANSPARENT passes input to other processes.
        style = AURORA_WINDOW_STYLE if transparent else AURORA_WINDOW_STYLE & ~0x20
        left, top, right, bottom = bounds
        with self.physical_pixels():
            hwnd = self.user.CreateWindowExW(style, self.name, title, 0x80000000, left, top,
                                             right - left, bottom - top, None, None, self.instance, None)
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        if not self.user.SetLayeredWindowAttributes(hwnd, 0, 255, 2):  # LWA_ALPHA
            self.user.DestroyWindow(hwnd)
            raise ctypes.WinError(ctypes.get_last_error())
        return hwnd

    def show(self, hwnd):
        # Windows verification pending: raise each window without moving, resizing or activating it.
        if not self.user.SetWindowPos(hwnd, -1, 0, 0, 0, 0, SWP_NOACTIVATE | SWP_SHOWWINDOW | 3):
            raise ctypes.WinError(ctypes.get_last_error())

    def paint(self, hwnd, width, height, text, text_width, button):
        # Windows verification pending: GDI painting on the two layered windows.
        paint = _Paint()
        dc = self.user.BeginPaint(hwnd, ctypes.byref(paint))
        try:
            bounds = w.RECT(0, 0, width, height)
            brush = self.gdi.CreateSolidBrush(0x3C334D if button else 0x2E2314)
            try:
                self.user.FillRect(dc, ctypes.byref(bounds), brush)
            finally:
                self.gdi.DeleteObject(brush)
            self.gdi.SetTextColor(dc, 0xFAF6ED)
            self.gdi.SetBkMode(dc, 1)  # TRANSPARENT
            font = self.gdi.SelectObject(dc, self.gdi.GetStockObject(17))  # DEFAULT_GUI_FONT
            try:
                label = w.RECT(0, 0, text_width, height)
                self.user.DrawTextW(dc, text, -1, ctypes.byref(label), 0x25)  # single line, centred
            finally:
                self.gdi.SelectObject(dc, font)
        finally:
            self.user.EndPaint(hwnd, ctypes.byref(paint))


class DriveBadge:
    """Two topmost windows, so only the Stop rectangle receives input."""

    def __init__(self, stop):
        self.stop = stop
        self.api = self.layout = self.panel = self.button = self.hook = None

    def set_visible(self, visible):
        if not visible or os.name != "nt":
            self._hide()
            return False
        try:
            if self.api is None:
                self.api = _DriveWin32(self._window_event, self._key_event)
            layout = driveui.badge_layout(self.api.primary_screen())
            if layout != self.layout:
                self._hide()
                if layout is None:
                    return False
                self.layout = layout
                self.panel = self.api.window(layout.badge, "Jarvis is driving", transparent=True)
                self.button = self.api.window(layout.stop, "Stop", transparent=False)
                self.api.show(self.panel)
                self.api.show(self.button)
                # Windows verification pending: WH_KEYBOARD_LL on the Tk message pump.
                self.hook = self.api.user.SetWindowsHookExW(13, self.api.key_proc, self.api.instance, 0)
                # Windows verification pending: repaint the panel with the hook result.
                self.api.user.InvalidateRect(self.panel, None, False)
            return self.button is not None
        except (OSError, AttributeError, ctypes.ArgumentError):
            self._hide()
            return False

    def _key_event(self, code, message, data):
        # Windows verification pending: KBDLLHOOKSTRUCT layout and forwarding every event.
        # The key is passed on whatever happens here: an exception that left
        # this callback would leave the keyboard to chance (review, CU-3c).
        try:
            if code == 0 and self.layout is not None and self.button and message in (0x100, 0x104):  # WM_[SYS]KEYDOWN
                event = ctypes.cast(data, ctypes.POINTER(_KeyEvent)).contents
                if driveui.physical_escape(event.key, event.flags, True):
                    self.stop("esc")
        except Exception:
            pass
        return self.api.user.CallNextHookEx(None, code, message, data)

    def _window_event(self, hwnd, message, wparam, lparam):
        if message == 0x21:  # WM_MOUSEACTIVATE
            return 3  # MA_NOACTIVATE
        if self.layout is not None and hwnd in (self.button, self.panel):
            badge, stop = self.layout
            button = hwnd == self.button
            if button and message == 0x201:  # WM_LBUTTONDOWN
                # Windows verification pending: client mouse coordinates and nonactivating click delivery.
                x, y = ctypes.c_short(lparam & 0xFFFF).value, ctypes.c_short(lparam >> 16).value
                if driveui.hits_stop(stop, stop.left + x, stop.top + y):
                    self.stop("badge")
                return 0
            if message == 0xF:  # WM_PAINT
                bounds = stop if button else badge
                width, height = bounds.right - bounds.left, bounds.bottom - bounds.top
                text = "Stop" if button else driveui.badge_text(bool(self.hook))
                self.api.paint(hwnd, width, height, text, width if button else stop.left - badge.left, button)
                return 0
        # Windows verification pending: default dispatch for unhandled window messages.
        return self.api.user.DefWindowProcW(hwnd, message, wparam, lparam)

    def _hide(self):
        self.layout = None
        # Windows verification pending: remove the hook and destroy both windows on the Tk thread.
        if self.hook and self.api.user.UnhookWindowsHookEx(self.hook):
            # A failed unhook keeps its handle so close() tries again.
            self.hook = None
        for hwnd in (self.button, self.panel):
            if hwnd:
                self.api.user.DestroyWindow(hwnd)
        self.panel = self.button = None

    def close(self):
        self._hide()
        if self.api is not None:
            # Windows verification pending: release the class after its last HWND is destroyed.
            self.api.user.UnregisterClassW(self.api.name, self.api.instance)
