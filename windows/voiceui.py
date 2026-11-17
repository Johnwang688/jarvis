"""Thread-safe, nonactivating voice pop-up; all Tk work stays on its thread."""
from __future__ import annotations

from collections import deque
import ctypes
from ctypes import wintypes
import logging
import math
import os
import queue
import time

STATES = ("hidden", "listening", "transcribing", "thinking", "answering", "done", "error")
ACCENT, AMBER, RED = "#6fc3df", "#fbbf24", "#fb7185"
PANEL, INK = "#16212a", "#edf6fa"
# ABGR: HUD --glass-float RGB, with enough tint for text over bright windows.
GLASS_TINT = 0xD92A2116
# Close to the tint so antialiased glyph edges blend into the acrylic.
GLASS_KEY, SHEEN = "#16212b", "#323c44"
# --accent-line (45% accent over the floating tint); DWM takes opaque COLORREF.
ACCENT_LINE = 0x007B6A3E
log = logging.getLogger("jarvis.voice")
WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW = 0x08000000, 0x00000080
SWP_NOACTIVATE, SWP_SHOWWINDOW, SW_SHOWNOACTIVATE = 0x10, 0x40, 4


class ACCENT_POLICY(ctypes.Structure):
    _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_int)]


class WINDOWCOMPOSITIONATTRIBDATA(ctypes.Structure):
    _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.c_void_p),
                ("SizeOfData", ctypes.c_size_t)]


def _win32_function(library, name, argtypes, restype):
    function = getattr(library, name)
    try:
        function.argtypes, function.restype = argtypes, restype
    except AttributeError:
        pass  # Python bound methods used by headless native-API fakes.
    return function


def transparency_on(reader=None):
    """Windows' "Transparency effects" switch. Off, DWM paints acrylic as a solid
    tint (found live 2026-11-17), so the pop-up uses its see-through fallback."""
    try:
        if reader is None:
            import winreg

            def reader():
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                    r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                    return winreg.QueryValueEx(key, "EnableTransparency")[0]
        return bool(reader())
    except Exception:
        return True  # unknown: try acrylic; DWM falls back itself


def acrylic(hwnd, tint, *, user32, dwmapi):
    """Request acrylic; unsupported Windows builds simply use the tinted panel."""
    try:
        accent = ACCENT_POLICY(4, 0, tint, 0)  # ACCENT_ENABLE_ACRYLICBLURBEHIND
        data = WINDOWCOMPOSITIONATTRIBDATA(19, ctypes.addressof(accent),
                                          ctypes.sizeof(accent))
        compose = _win32_function(user32, "SetWindowCompositionAttribute",
                                 [wintypes.HWND, ctypes.POINTER(WINDOWCOMPOSITIONATTRIBDATA)],
                                 wintypes.BOOL)
        applied = bool(compose(hwnd, ctypes.byref(data)))
    except Exception:
        return False
    # Cosmetic DWM attributes must never turn successful acrylic into a failure.
    for attribute, value in ((33, ctypes.c_int(2)), (34, ctypes.c_uint(ACCENT_LINE))):
        try:
            decorate = _win32_function(dwmapi, "DwmSetWindowAttribute",
                                      [wintypes.HWND, ctypes.c_uint, ctypes.c_void_p,
                                       ctypes.c_uint], ctypes.c_long)
            decorate(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
        except Exception:
            pass
    return applied


def state_colour(state):
    if state == "error":
        return RED
    if state in ("transcribing", "thinking"):
        return AMBER
    return ACCENT


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

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.state, self.text, self.hide_at = "hidden", "", None

    def set(self, state, text=None):
        self.state = state
        labels = {"listening": "Listening…", "transcribing": "Transcribing…",
                  "thinking": "Thinking…", "done": "Done", "error": "Voice unavailable"}
        self.text = "" if state == "hidden" else str(text if text is not None else labels.get(state, ""))
        delay = {"done": 4.0, "error": 5.0}.get(state)
        self.hide_at = self.clock() + delay if delay is not None else None

    def expire(self):
        if self.hide_at is not None and self.clock() >= self.hide_at:
            self.set("hidden")
            return True
        return False


class Overlay:
    def __init__(self, root, *, user32=None, dwmapi=None):
        import tkinter as tk

        self.root = root
        self.events = queue.Queue()
        self.display = _DisplayState()
        self.levels = deque(maxlen=24)
        self.heights = [0.0] * 24
        self.window = tk.Toplevel(root, takefocus=False)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        # Acrylic shows through the keyed background; fallback keeps clear corners.
        key = GLASS_KEY
        self.window.configure(bg=key)
        if os.name == "nt":
            self.window.attributes("-transparentcolor", key)
        self.canvas = tk.Canvas(self.window, width=440, height=96, bg=key,
                                highlightthickness=0, takefocus=False)
        self.canvas.pack()
        self.panel = self.canvas.create_polygon(0, 0, 0, 0, fill=PANEL,
                                                 outline="#3a4a56", smooth=True, splinesteps=24)
        self.sheen = self.canvas.create_line(18, 1, 422, 1, fill=SHEEN, width=1,
                                              state="hidden")
        self.bar_ids = [self.canvas.create_rectangle(0, 0, 0, 0, fill=ACCENT, outline="")
                        for _ in range(24)]
        self.label = self.canvas.create_text(220, 66, text="", anchor="n", width=400,
                                             fill=INK, font=("Segoe UI", 12))
        self.user32 = None
        self.glass = False
        if os.name == "nt":
            self._native_window(user32=user32, dwmapi=dwmapi)
        if self.glass:
            self.canvas.itemconfigure(self.panel, state="hidden")
            self.canvas.itemconfigure(self.sheen, state="normal")
        self.window.attributes("-alpha", 1.0 if self.glass else 0.88)
        log.info("pop-up glass: %s", "acrylic" if self.glass else "tinted fallback")
        self.width, self.height = 440, 96
        self.root.after(33, self._tick)

    def _native_window(self, *, user32=None, dwmapi=None):
        self.user32 = user32 if user32 is not None else ctypes.windll.user32
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
        self.window.update_idletasks()
        self.hwnd = u.GetParent(self.window.winfo_id()) or self.window.winfo_id()
        style = u.GetWindowLongW(self.hwnd, -20)
        u.SetWindowLongW(self.hwnd, -20, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        if dwmapi is None:
            try:
                dwmapi = ctypes.windll.dwmapi
            except (AttributeError, OSError):
                pass
        self.glass = transparency_on() and acrylic(self.hwnd, GLASS_TINT, user32=u, dwmapi=dwmapi)

    def set_state(self, state, text=None):
        if state not in STATES:
            raise ValueError("Unknown voice state")
        self.events.put(("state", state, text))

    def push_level(self, rms):
        self.events.put(("level", float(rms), None))

    def _layout(self):
        state = self.display.state
        if state == "hidden":
            if self.user32:
                self.user32.ShowWindow(self.hwnd, 0)
            else:
                self.window.withdraw()
            self.canvas.itemconfigure(self.label, text="")
            return
        self.width = 640 if state == "answering" else 440
        self.canvas.itemconfigure(self.label, text=self.display.text, width=self.width - 40,
                                  fill=RED if state == "error" else INK)
        for ident in self.bar_ids:
            self.canvas.itemconfigure(ident, fill=state_colour(state))
        self.canvas.coords(self.label, self.width / 2, 64)
        box = self.canvas.bbox(self.label)
        self.height = min(240, max(96, (box[3] if box else 80) + 18))
        w, h, r = self.width, self.height, 18
        self.canvas.configure(width=w, height=h)
        self.canvas.coords(self.panel, r, 1, w-r, 1, w-1, 1, w-1, r,
                           w-1, h-r, w-1, h-1, w-r, h-1, r, h-1,
                           1, h-1, 1, h-r, 1, r, 1, 1)
        self.canvas.coords(self.sheen, r, 1, w-r, 1)
        x, y = place(self.window.winfo_screenwidth(), self.window.winfo_screenheight(), w, h)
        self.window.geometry(f"{w}x{h}{x:+d}{y:+d}")
        if self.user32:
            # Match VA-1: never deiconify/focus on Windows, including a resize.
            self.user32.SetWindowPos(self.hwnd, -1, x, y, w, h,
                                     SWP_NOACTIVATE | SWP_SHOWWINDOW)
            self.user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
        else:
            self.window.deiconify()

    def _tick(self):
        changed = False
        # Bound a single drain so a busy producer cannot starve Tk's event loop.
        for _ in range(256):
            try:
                kind, value, text = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "level":
                self.levels.append(value)
            else:
                self.display.set(value, text)
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
                self.canvas.coords(ident, left, 34-half, left+6, 34+half)
        self.root.after(33, self._tick)
