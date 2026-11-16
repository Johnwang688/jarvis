"""Indexed Windows app actions. Model text is never a command line."""
from __future__ import annotations

import difflib
import json
import ntpath
import os
import subprocess
import time
from urllib.parse import quote_plus

if __package__:
    from .voicetype import WindowsInput
else:
    from voicetype import WindowsInput


def _normal(text):
    return " ".join("".join(c if c.isalnum() else " " for c in text.casefold()).split())


ALIASES = {"vs code": "visual studio code", "vscode": "visual studio code",
           "code": "visual studio code", "chrome": "google chrome", "edge": "microsoft edge"}


def match_app(name, apps):
    name = _normal(name)
    name = ALIASES.get(name, name)
    if not name:
        return None
    entries = [(app, _normal(app["name"])) for app in apps]
    for app, label in entries:
        if label == name:
            return app
    for app, label in entries:
        if label.startswith(name) or f" {name} " in f" {label} ":
            return app
    best, score = None, 0.75
    for app, label in entries:
        ratio = difflib.SequenceMatcher(None, name, label).ratio()
        if ratio >= score and (best is None or ratio > score):
            best, score = app, ratio
    return best


def match_window(target, windows):
    target = _normal(target)
    if not target:
        return None
    canonical = ALIASES.get(target, target)
    variants = {target, canonical}
    variants.update(alias for alias, name in ALIASES.items() if name == canonical)
    windows = list(windows)
    for hwnd, title, exe in windows:
        if _normal(ntpath.splitext(ntpath.basename(exe))[0]) in variants:
            return hwnd
    for hwnd, title, exe in windows:
        title = f" {_normal(title)} "
        if any(f" {variant} " in title for variant in variants):
            return hwnd
    return None


def search_url(q):
    return "https://www.google.com/search?q=" + quote_plus(q)


class WindowsDesktop:
    def __init__(self):
        import ctypes
        from ctypes import wintypes as w
        self.ctypes, self.w = ctypes, w
        self.user32, self.kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        u, k = self.user32, self.kernel32
        u.GetForegroundWindow.restype = w.HWND
        u.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
        u.GetWindowThreadProcessId.restype = w.DWORD
        for name in ("IsWindowVisible", "BringWindowToTop", "SetForegroundWindow", "IsIconic"):
            getattr(u, name).argtypes = [w.HWND]
        u.SetActiveWindow.argtypes, u.SetActiveWindow.restype = [w.HWND], w.HWND
        u.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
        u.AttachThreadInput.argtypes = [w.DWORD, w.DWORD, w.BOOL]
        u.GetWindowTextLengthW.argtypes = [w.HWND]
        u.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
        k.OpenProcess.argtypes, k.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
        k.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
        k.CloseHandle.argtypes = [w.HANDLE]
        u.OpenClipboard.argtypes = [w.HWND]
        u.GetClipboardData.argtypes, u.GetClipboardData.restype = [w.UINT], w.HANDLE
        u.SetClipboardData.argtypes, u.SetClipboardData.restype = [w.UINT, w.HANDLE], w.HANDLE
        u.GetClipboardSequenceNumber.restype = w.DWORD
        u.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     w.HWND, w.HMENU, w.HINSTANCE, ctypes.c_void_p]
        u.CreateWindowExW.restype = w.HWND
        u.DestroyWindow.argtypes = [w.HWND]
        k.GlobalAlloc.argtypes, k.GlobalAlloc.restype = [w.UINT, ctypes.c_size_t], w.HGLOBAL
        k.GlobalLock.argtypes, k.GlobalLock.restype = [w.HGLOBAL], ctypes.c_void_p
        k.GlobalUnlock.argtypes = [w.HGLOBAL]
        k.GlobalFree.argtypes, k.GlobalFree.restype = [w.HGLOBAL], w.HGLOBAL
        self.keys = WindowsInput()
        self.clipboard_hwnd = None

    def windows(self):
        c, w, u, k = self.ctypes, self.w, self.user32, self.kernel32
        result = []
        callback_type = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

        @callback_type
        def visit(hwnd, unused):
            if not u.IsWindowVisible(hwnd):
                return True
            size = u.GetWindowTextLengthW(hwnd)
            if not size:
                return True
            title = c.create_unicode_buffer(size + 1)
            u.GetWindowTextW(hwnd, title, len(title))
            pid = w.DWORD()
            u.GetWindowThreadProcessId(hwnd, c.byref(pid))
            process = k.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
            exe = ""
            if process:
                try:
                    path, length = c.create_unicode_buffer(32768), w.DWORD(32768)
                    if k.QueryFullProcessImageNameW(process, 0, path, c.byref(length)):
                        exe = path.value
                finally:
                    k.CloseHandle(process)
            result.append((hwnd, title.value, exe))
            return True

        u.EnumWindows.argtypes = [callback_type, w.LPARAM]
        u.EnumWindows(visit, 0)
        return result

    def focus(self, hwnd):
        u, k = self.user32, self.kernel32
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, 9)  # SW_RESTORE
        for attempt in range(3):
            if u.GetForegroundWindow() == hwnd:
                return True
            fore = u.GetForegroundWindow()
            tid_fore = u.GetWindowThreadProcessId(fore, None) if fore else 0
            tid_self = k.GetCurrentThreadId()
            attached = bool(tid_fore and tid_fore != tid_self
                            and u.AttachThreadInput(tid_self, tid_fore, True))
            try:
                u.BringWindowToTop(hwnd)
                u.SetForegroundWindow(hwnd)
                u.SetActiveWindow(hwnd)
            finally:
                if attached:
                    u.AttachThreadInput(tid_self, tid_fore, False)
            if u.GetForegroundWindow() == hwnd:
                return True
            if attempt == 0:
                u.keybd_event(0x12, 0, 0, 0)
                u.keybd_event(0x12, 0, 2, 0)
            time.sleep(0.2)
        return u.GetForegroundWindow() == hwnd

    def clipboard_sequence(self):
        return self.user32.GetClipboardSequenceNumber()

    def _open_clipboard(self):
        if self.clipboard_hwnd is None:
            # EmptyClipboard needs a real owner for the subsequent SetClipboardData.
            # A message-only window never appears or takes focus.
            self.clipboard_hwnd = self.user32.CreateWindowExW(
                0, "STATIC", "", 0, 0, 0, 0, 0, self.w.HWND(-3), None, None, None)
            if not self.clipboard_hwnd:
                raise OSError("Clipboard unavailable")
        for _ in range(10):
            if self.user32.OpenClipboard(self.clipboard_hwnd):
                return
            time.sleep(0.01)
        raise OSError("Clipboard unavailable")

    def clipboard_text(self):
        self._open_clipboard()
        try:
            handle = self.user32.GetClipboardData(13)  # CF_UNICODETEXT
            if not handle:
                return None
            pointer = self.kernel32.GlobalLock(handle)
            if not pointer:
                raise OSError("Clipboard unavailable")
            try:
                return self.ctypes.wstring_at(pointer)
            finally:
                self.kernel32.GlobalUnlock(handle)
        finally:
            self.user32.CloseClipboard()

    def set_clipboard_text(self, text):
        data = ((text or "") + "\0").encode("utf-16-le")
        c, k, u = self.ctypes, self.kernel32, self.user32
        handle = k.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        if not handle:
            raise OSError("Clipboard unavailable")
        try:
            pointer = k.GlobalLock(handle)
            if not pointer:
                raise OSError("Clipboard unavailable")
            try:
                c.memmove(pointer, data, len(data))
            finally:
                k.GlobalUnlock(handle)
            self._open_clipboard()
            try:
                if not u.EmptyClipboard() or not u.SetClipboardData(13, handle):
                    raise OSError("Clipboard unavailable")
                handle = None  # clipboard owns it now
            finally:
                u.CloseClipboard()
        finally:
            if handle:
                k.GlobalFree(handle)

    def copy(self):
        self.keys.key("ctrl+c")

    def close(self):
        if self.clipboard_hwnd:
            self.user32.DestroyWindow(self.clipboard_hwnd)
            self.clipboard_hwnd = None


class Actions:
    def __init__(self, desktop=None, clock=time.monotonic, sleep=time.sleep,
                 runner=subprocess.run, launcher=subprocess.Popen, opener=None):
        self.desktop = desktop if desktop is not None else WindowsDesktop()
        self.clock, self.sleep = clock, sleep
        self.runner, self.launcher = runner, launcher
        self.opener = opener
        self.apps, self.refreshed = [], None
        self.refresh_apps()

    def refresh_apps(self):
        now = self.clock()
        if self.refreshed is not None and now - self.refreshed < 600:
            return
        self.refreshed = now  # throttle failed refreshes too
        try:
            got = self.runner(["powershell", "-NoProfile", "-Command",
                               "Get-StartApps | ConvertTo-Json"], capture_output=True,
                              text=True, encoding="oem" if os.name == "nt" else "utf-8", timeout=20,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if got.returncode:
                return
            entries = json.loads(got.stdout.lstrip("\ufeff"))
            if isinstance(entries, dict):
                entries = [entries]
            if not isinstance(entries, list):
                return
            self.apps = [{"name": entry["Name"], "id": entry["AppID"]} for entry in entries
                         if isinstance(entry, dict) and isinstance(entry.get("Name"), str)
                         and isinstance(entry.get("AppID"), str) and entry["AppID"]]
        except (OSError, ValueError, subprocess.SubprocessError):
            pass  # keep the previous index if PowerShell is temporarily unavailable

    def open_app(self, target):
        self.refresh_apps()
        app = match_app(target, self.apps)
        if app is None:
            return False
        self.launcher(["explorer.exe", "shell:AppsFolder\\" + app["id"]],
                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True

    def switch_app(self, target):
        hwnd = match_window(target, self.desktop.windows())
        return bool(hwnd and self.desktop.focus(hwnd))

    def search(self, query):
        (self.opener or os.startfile)(search_url(query))
        return True

    def read_selection(self):
        previous = self.desktop.clipboard_text()
        sequence = self.desktop.clipboard_sequence()
        try:
            self.desktop.copy()
            deadline = self.clock() + 0.3
            while self.clock() < deadline:
                if self.desktop.clipboard_sequence() != sequence:
                    selection = self.desktop.clipboard_text()
                    return selection[:4000] if selection else None
                self.sleep(0.01)
            return None
        finally:
            self.desktop.set_clipboard_text(previous)

    def close(self):
        close = getattr(self.desktop, "close", None)
        if close:
            close()
