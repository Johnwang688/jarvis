"""Thread-safe, nonactivating voice pop-up; all Tk work stays on its thread."""
from __future__ import annotations

from collections import deque
import math
import os
import queue
import time

STATES = ("hidden", "listening", "transcribing", "thinking", "answering", "done", "error")
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

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.state, self.text, self.hide_at = "hidden", "", None
        self.dismissed = False

    def dismiss(self):
        self.set("hidden")
        self.dismissed = True

    def set(self, state, text=None):
        if self.dismissed:
            if state != "listening":
                if state in ("done", "error", "hidden"):
                    self.dismissed = False
                return False
            self.dismissed = False
        self.state = state
        labels = {"listening": "Listening…", "transcribing": "Transcribing…",
                  "thinking": "Thinking…", "done": "Done", "error": "Voice unavailable"}
        self.text = "" if state == "hidden" else str(text if text is not None else labels.get(state, ""))
        delay = {"done": 4.0, "error": 5.0}.get(state)
        self.hide_at = self.clock() + delay if delay is not None else None
        return True

    def expire(self):
        if self.hide_at is not None and self.clock() >= self.hide_at:
            self.set("hidden")
            return True
        return False


class Overlay:
    def __init__(self, root):
        import tkinter as tk

        self.root = root
        self.events = queue.Queue()
        self.display = _DisplayState()
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
        self.canvas.create_text(16, 15, text="Voice Anywhere", anchor="w",
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

    def set_state(self, state, text=None):
        if state not in STATES:
            raise ValueError("Unknown voice state")
        self.events.put(("state", state, text))

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
                                  fill=RED if state == "error" else
                                  AMBER if state in ("transcribing", "thinking") else INK)
        self.canvas.coords(self.label, self.width / 2, 78)
        box = self.canvas.bbox(self.label)
        self.height = min(254, max(110, (box[3] if box else 94) + 18))
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
                kind, value, text = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "timing":
                self.canvas.itemconfigure(self.metric, text=f"{value}: {text} ms")
            elif kind == "level":
                self.levels.append(value)
            else:
                if not self.display.set(value, text):
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
