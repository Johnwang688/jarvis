"""Indexed Windows app actions. Model text is never a command line."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
import difflib
from enum import IntEnum
import json
import ntpath
import os
import re
import subprocess
import threading
import time
import unicodedata
from urllib.parse import quote_plus

if __package__:
    from . import chromepicker
    from .desktopwire import launch_name
    from .voicetype import WindowsInput, web_destination
else:
    import chromepicker
    from desktopwire import launch_name
    from voicetype import WindowsInput, web_destination


def _normal(text):
    return " ".join("".join(c if c.isalnum() else " " for c in text.casefold()).split())


ALIASES = {"vs code": "visual studio code", "vscode": "visual studio code",
           "code": "visual studio code", "chrome": "google chrome", "edge": "microsoft edge"}


# Each voice clip supplies its own revocation check. Capture the callable
# before passing an operation to a different thread or COM apartment.
action_guard = ContextVar("voice_action_guard", default=lambda: True)


def _allowed(guard):
    try:
        return guard() is True
    except Exception:  # noqa: BLE001  (an unreadable revocation check revokes)
        return False


@contextmanager
def action_scope(current):
    token = action_guard.set(current)
    try:
        yield
    finally:
        action_guard.reset(token)


class LaunchStatus(IntEnum):
    NOT_FOUND = 0
    LAUNCHED = 1
    CHROME_PROFILE_REQUIRED = 2
    CHROME_WINDOW_UNCONFIRMED = 3


def _sound(text):
    # The recognizer hears "Codex" as "codecs": treat x and cs/ks/cks alike.
    return re.sub(r"cks|ks|cs", "x", text)


def exact_start_app(name, apps, *, reject_alias_conflicts=False):
    """Resolve Start display names and known aliases, preserving the indexed AppID."""
    label = name.casefold()
    matches = {app["id"]: app for app in apps if app["name"].casefold() == label}
    alias = ALIASES.get(label)
    # A named request to the desktop bridge prefers the given display name.
    # An unaddressed shortcut must also exclude an alias for a different app.
    if alias and (reject_alias_conflicts or not matches):
        matches.update((app["id"], app) for app in apps if app["name"].casefold() == alias)
    if len(matches) > 1:
        raise ValueError("Multiple Start apps have this name. Use a different display name.")
    return next(iter(matches.values()), None)


def match_app(name, apps):
    name = _normal(name)
    name = ALIASES.get(name, name)
    if not name:
        return None
    entries = [(app, _normal(app["name"])) for app in apps]
    for app, label in entries:
        if label == name:
            return app
    # Short names need an exact indexed match (or an explicit alias above).
    if len(name.replace(" ", "")) < 3:
        return None
    for app, label in entries:
        if label.startswith(name) or f" {name} " in f" {label} ":
            return app
    best, score = None, 0.75
    for app, label in entries:
        ratio = max(difflib.SequenceMatcher(None, name, label).ratio(),
                    difflib.SequenceMatcher(None, _sound(name), _sound(label)).ratio())
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


# The image an application ID spells out: "Microsoft.Office.WINWORD.EXE.15".
_APP_ID_IMAGE = re.compile(r"(?:^|[.!_])([^.!_]+)\.exe(?=$|[.!_])", re.IGNORECASE)


def _app_images(target, apps):
    """Process image names (no extension) that are the app ``target`` names."""
    canonical = ALIASES.get(target, target)
    images = {target, canonical}
    images.update(alias for alias, name in ALIASES.items() if name == canonical)
    for app in apps or ():
        name, ident = app.get("name"), app.get("id")
        if not isinstance(name, str) or not isinstance(ident, str) or _normal(name) not in (target, canonical):
            continue
        # Only an image the entry spells out. A classic entry is a path and
        # names its whole file ("...\\Acme\\acme.report.exe"), never a folder
        # or a piece of the file name. A vendor or suite word in an
        # application ID ("Microsoft", "Office") names no process.
        if "\\" in ident or "/" in ident:
            base = ntpath.basename(ident)
            if base.casefold().endswith(".exe"):
                images.add(_normal(base[:-4]))
        else:
            images.update(_normal(image) for image in _APP_ID_IMAGE.findall(ident))
    images.discard("")
    return images


def grounded_window(target, windows, apps=()):
    """The window owned by the app ``target`` names exactly, or None.

    For a routed Laya switch, whose target is an offered Start catalog name.
    Only the owning process identifies a window: its image must be the app's
    name, a fixed alias of it, or the image the app's Start entry spells out.
    Titles are never read. A document, page or console title is content, and
    content can say anything ("Review draft - Google Chrome" in Notepad).
    A window hosted by a shared frame process, or a packaged app whose image
    is neither its name nor in its Start entry, is therefore not switchable.
    """
    target = _normal(target) if isinstance(target, str) else ""
    if not target:
        return None
    images = _app_images(target, apps)
    for hwnd, _title, exe in windows:
        if isinstance(exe, str) and _normal(ntpath.splitext(ntpath.basename(exe))[0]) in images:
            return hwnd
    return None


def search_url(q):
    return "https://www.google.com/search?q=" + quote_plus(q)


# -- Website navigation in the foreground Chrome --------------------------------
# A browser window's AppUserModelID names its profile (measured on Chrome 157):
# `Chrome` is the default user-data directory's Default profile, `Chrome.Profile1`
# its "Profile 1", and another directory adds one shortened component of its own
# (`Chrome.<dir>.Default`, `Chrome.<dir>.Profile1`). Only the directory names
# Chrome itself creates are mapped back; anything else is refused.
_CHROME_PROFILE_PART = re.compile(r"Profile([1-9][0-9]{0,3})", re.ASCII)


def installed_chrome_path(path):
    """Match the stable browser's executable in a Windows Chrome install location."""
    chrome = r"Google\Chrome\Application\chrome.exe"
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        root = os.environ.get(variable)
        if root and ntpath.normcase(path) == ntpath.normcase(ntpath.join(root, chrome)):
            return True
    return False


def chrome_profile_directory(aumid, user_data_dir):
    """The `--profile-directory` of a stable-Chrome browser window, or None."""
    if not isinstance(aumid, str) or len(aumid) > 128 or not isinstance(user_data_dir, str):
        return None
    parts = aumid.split(".")
    if parts[0] != "Chrome" or not all(part.isascii() and part.isalnum() for part in parts):
        return None
    if (user_data_dir and len(parts) != 3) or (not user_data_dir and len(parts) > 2):
        return None  # the window and its process disagree about the directory
    last = parts[-1] if len(parts) > 1 else "Default"
    if last == "Default":
        return "Default"
    number = _CHROME_PROFILE_PART.fullmatch(last)
    return f"Profile {number[1]}" if number else None


def chrome_user_data_dir(argv):
    """The browser process's `--user-data-dir` ("" for the default), or None if unsure.

    Read as Chromium reads its Windows command line: `--`, `-` and `/` prefix a
    switch, names are case-insensitive, and `--` or `--single-argument` ends them.
    """
    if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(a, str) for a in argv):
        return None
    found = []
    for arg in argv[1:]:
        if arg == "--":
            break
        prefix = next((p for p in ("--", "-", "/") if arg.startswith(p)), None)
        if prefix is None:
            continue
        name, equals, value = arg[len(prefix):].partition("=")
        name = name.casefold()
        if name == "single-argument":
            break
        if name == "user-data-dir":
            found.append(value if equals else None)
    if not found:
        return ""
    value = found[0]
    if len(found) > 1 or not value or not ntpath.isabs(value) or len(value) > 1024 \
            or any(ch == '"' or ord(ch) < 0x20 for ch in value):
        return None
    return value


def browser_window_names(client, UIA, hwnd):
    """(BrowserRootView name, AvatarToolbarButton names) of a Chrome window, or None.

    Read from Chrome's native views only (UI Automation thread): web content is
    never entered, and the walk is bounded.
    """
    walker = client.RawViewWalker
    root = client.ElementFromHandle(hwnd)
    if not root:
        return None
    views, child, count = [], walker.GetFirstChildElement(root), 0
    while child and count < 16:
        count += 1
        if child.CurrentClassName == "BrowserRootView" and child.CurrentFrameworkId == "Chrome":
            views.append(child)
        child = walker.GetNextSiblingElement(child)
    if len(views) != 1:
        return None
    avatars, frontier, seen = [], [(views[0], 0)], 0
    while frontier:
        node, depth = frontier.pop(0)
        child = walker.GetFirstChildElement(node)
        while child:
            seen += 1
            if seen > 1500:
                return None
            kind = child.CurrentClassName or ""
            if (child.CurrentControlType != chromepicker.DOCUMENT and kind != "Chrome_RenderWidgetHostHWND"
                    and child.CurrentFrameworkId == "Chrome"):
                if kind == "AvatarToolbarButton":
                    avatars.append(child.CurrentName or "")
                elif depth < 10:
                    frontier.append((child, depth + 1))
            child = walker.GetNextSiblingElement(child)
    return views[0].CurrentName or "", tuple(avatars)


def regular_browser_window(names):
    """True only for a window that is positively a regular (not incognito or guest) one.

    Measured on Chrome 157: the root view is named "<tab> - Google Chrome", with
    " - <profile name>" appended when several profiles exist; an incognito window
    is "<tab> - Google Chrome (Incognito)" and its avatar button says
    "Incognito". The tab title comes first, so only what follows the *last*
    product name counts.
    """
    if not isinstance(names, tuple) or len(names) != 2:
        return False
    title, avatars = names
    if not isinstance(title, str) or not isinstance(avatars, tuple) or len(avatars) > 1:
        return False
    marker = " - Google Chrome"
    at = title.rfind(marker)
    if at < 0:
        return False
    rest = title[at + len(marker):]
    if rest and not rest.startswith(" - "):
        return False
    return not any(_normal(name).startswith(("incognito", "guest")) for name in avatars if isinstance(name, str))


def windows_command_line(pid):
    """Another process's command line, split as CommandLineToArgvW does; None if unreadable."""
    import ctypes
    from ctypes import wintypes as w
    k, nt, shell = ctypes.windll.kernel32, ctypes.windll.ntdll, ctypes.windll.shell32

    class UNICODE_STRING(ctypes.Structure):
        _fields_ = [("Length", w.USHORT), ("MaximumLength", w.USHORT), ("Buffer", ctypes.c_void_p)]

    k.OpenProcess.argtypes, k.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
    k.CloseHandle.argtypes = [w.HANDLE]
    k.LocalFree.argtypes, k.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
    nt.NtQueryInformationProcess.argtypes = [w.HANDLE, w.ULONG, ctypes.c_void_p, w.ULONG,
                                             ctypes.POINTER(w.ULONG)]
    nt.NtQueryInformationProcess.restype = ctypes.c_long
    shell.CommandLineToArgvW.argtypes = [w.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(w.LPWSTR)
    process = k.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION is enough (8.1+)
    if not process:
        return None
    try:
        size = w.ULONG(0)
        nt.NtQueryInformationProcess(process, 60, None, 0, ctypes.byref(size))  # ProcessCommandLineInformation
        if not ctypes.sizeof(UNICODE_STRING) <= size.value <= 70000:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if nt.NtQueryInformationProcess(process, 60, buffer, size, ctypes.byref(size)) != 0:
            return None
        text = UNICODE_STRING.from_buffer(buffer)
        first = ctypes.addressof(buffer)
        if (not text.Buffer or text.Length % 2 or text.Buffer < first
                or text.Buffer + text.Length > first + ctypes.sizeof(buffer)):
            return None
        line = ctypes.wstring_at(text.Buffer, text.Length // 2)
    finally:
        k.CloseHandle(process)
    count = ctypes.c_int()
    argv = shell.CommandLineToArgvW(line, ctypes.byref(count))
    if not argv:
        return None
    try:
        return [argv[i] for i in range(count.value)]
    finally:
        k.LocalFree(argv)


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
        self._chrome_picker = None

    def chrome_picker(self):
        if self._chrome_picker is None:
            self._chrome_picker = chromepicker.WindowsChromePicker()
        return self._chrome_picker

    def _visible_window(self, hwnd):
        c, w, u, k = self.ctypes, self.w, self.user32, self.kernel32
        if not hwnd or not u.IsWindowVisible(hwnd):
            return None
        size = u.GetWindowTextLengthW(hwnd)
        if not size:
            return None
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
        return hwnd, title.value, exe

    def foreground_window(self):
        return self._visible_window(self.user32.GetForegroundWindow())

    def windows(self):
        c, w, u = self.ctypes, self.w, self.user32
        result = []
        callback_type = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

        @callback_type
        def visit(hwnd, unused):
            if (window := self._visible_window(hwnd)) is not None:
                result.append(window)
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
        if self._chrome_picker is not None:
            self._chrome_picker.close()
        if self.clipboard_hwnd:
            self.user32.DestroyWindow(self.clipboard_hwnd)
            self.clipboard_hwnd = None


class Actions:
    def __init__(self, desktop=None, clock=time.monotonic, sleep=time.sleep,
                 runner=subprocess.run, launcher=subprocess.Popen, opener=None, *, warm_in_background=False,
                 picker=None, command_line=None, window_names=None):
        self.desktop = desktop if desktop is not None else WindowsDesktop()
        # Reads a browser process's command line (only its --user-data-dir is used).
        self.command_line = command_line
        # hwnd -> `browser_window_names` result; defaults to the picker's UIA thread.
        self.window_names = window_names
        # Only the real desktop (or an explicit adapter) can read a Chrome picker.
        self.picker = picker
        self.picker_context = None
        self.picker_offered = None  # (picker identity, cards the owner was shown)
        self.clock, self.sleep = clock, sleep
        self.runner, self.launcher = runner, launcher
        self.opener = opener
        self.apps, self.refreshed = [], None
        self._index_lock = threading.Lock()
        if warm_in_background:
            threading.Thread(target=self.refresh_apps, name="voice-app-index", daemon=True).start()
        else:
            self.refresh_apps()

    def refresh_apps(self):
        # Never hold up pairing on PowerShell startup. An immediate open joins
        # the in-flight index, so its target is resolved exactly once.
        with self._index_lock:
            self._refresh_apps()

    def _refresh_apps(self):
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

    def open_app(self, target, *, exact=False):
        guard = action_guard.get()  # captured here: the index wait may outlive the clip
        if exact and launch_name(target) is None:
            return LaunchStatus.NOT_FOUND
        self.refresh_apps()
        if not _allowed(guard):
            return LaunchStatus.NOT_FOUND  # stopped while the Start catalog loaded: launch nothing
        if exact:
            try:
                app = exact_start_app(target, self.apps, reject_alias_conflicts=True)
            except ValueError:
                return LaunchStatus.NOT_FOUND
        else:
            app = match_app(target, self.apps)
        if app is None:
            return LaunchStatus.NOT_FOUND
        is_chrome = _normal(app["name"]) == "google chrome"
        self.picker_context = self.picker_offered = None  # a new launch never inherits an older picker
        previous, existing = None, frozenset()
        if is_chrome:
            previous = self.desktop.foreground_window()
            try:  # browser windows already open can never confirm this launch
                existing = frozenset(window[0] for window in self.desktop.windows())
            except Exception:  # noqa: BLE001  (adapters without enumeration keep the focus rule)
                existing = frozenset()
            if not _allowed(guard):
                return LaunchStatus.NOT_FOUND
        self.launcher(["explorer.exe", "shell:AppsFolder\\" + app["id"]],
                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if is_chrome:
            return self._chrome_launch_status(previous[0] if previous else None, existing, guard)
        return LaunchStatus.LAUNCHED

    def _chrome_launch_status(self, previous_hwnd, existing=frozenset(), guard=lambda: True):
        deadline = self.clock() + 3.0
        while _allowed(guard):  # once revoked, the launch stands but is never confirmed
            window = self.desktop.foreground_window()
            if window is not None:
                hwnd, title, exe = window
                if ntpath.basename(exe).casefold() == "chrome.exe":
                    # Chrome's profile picker uses the product name as its
                    # native window title. Browser windows include a tab title.
                    if (hwnd != previous_hwnd and hwnd not in existing
                            and title.strip().casefold().endswith(" - google chrome")):
                        return LaunchStatus.LAUNCHED if _allowed(guard) else LaunchStatus.CHROME_WINDOW_UNCONFIRMED
                    if _normal(title) in {"google chrome", "who s using chrome",
                                           "who is using chrome", "select profile"}:
                        self._remember_picker(hwnd)
                        if not _allowed(guard):
                            self.picker_context = None
                            return LaunchStatus.CHROME_WINDOW_UNCONFIRMED
                        return LaunchStatus.CHROME_PROFILE_REQUIRED
            remaining = deadline - self.clock()
            if remaining <= 0:
                return LaunchStatus.CHROME_WINDOW_UNCONFIRMED
            self.sleep(min(remaining, 0.1))
        return LaunchStatus.CHROME_WINDOW_UNCONFIRMED

    # -- Chrome's profile picker -------------------------------------------------
    # The picker a voice open met is remembered by its handle and identity, and
    # only that window is ever read or invoked. Choosing invokes one card at most
    # once, and reports LAUNCHED only when that picker closed and a new browser
    # window of the same Chrome process took the foreground before the deadline.
    # The clip's revocation check (`action_guard`) is captured on entry, re-read
    # after every blocking read, and carried on the card to the UIA thread.
    PICKER_TTL = 180.0
    PICKER_READ_WAIT = 2.0
    PICKER_CONFIRM_WAIT = 6.0

    def _picker_backend(self):
        if self.picker is None and isinstance(self.desktop, WindowsDesktop):
            try:
                self.picker = self.desktop.chrome_picker()
            except (OSError, AttributeError, ImportError):
                return None
        return self.picker

    def _remember_picker(self, hwnd):
        backend = self._picker_backend()
        if backend is None:
            return
        try:
            identity = backend.identity(hwnd)
        except Exception:  # noqa: BLE001  (an unreadable window is never trusted)
            return
        if chromepicker.is_picker_identity(identity):
            self.picker_context = (identity, self.clock())

    def _live_picker(self, backend):
        if self.picker_context is None:
            return None
        recorded, started = self.picker_context
        if self.clock() - started > self.PICKER_TTL:
            self.picker_context = None
            return None
        try:
            identity = backend.identity(recorded.hwnd)
        except Exception:  # noqa: BLE001
            identity = None
        if not chromepicker.same_picker(identity, recorded):
            self.picker_context = None  # closed, replaced or changed: never follow it
            return None
        return identity

    def _picker_cards(self, backend, identity, wait, guard=lambda: True):
        # Chrome builds its accessibility tree on first request; read until the
        # trusted structure appears or the bounded wait ends.
        deadline = self.clock() + wait
        while True:
            try:
                cards = backend.cards(identity)
            except Exception:  # noqa: BLE001
                cards = None
            if cards or not _allowed(guard):
                return cards
            remaining = deadline - self.clock()
            if remaining <= 0:
                return None
            self.sleep(min(remaining, 0.15))

    def chrome_profile_options(self):
        backend = self._picker_backend()
        identity = self._live_picker(backend) if backend is not None else None
        if identity is None:
            return ()
        cards = self._picker_cards(backend, identity, self.PICKER_READ_WAIT)
        shown = [card for card in cards or () if chromepicker.card_ok(card)]
        self.picker_offered = (identity, frozenset((c.key, c.label, c.button_name) for c in shown))
        return chromepicker.options(shown)

    def choose_chrome_profile(self, display_name):
        guard = action_guard.get()  # this clip's revocation check, never re-looked-up
        unconfirmed = LaunchStatus.CHROME_WINDOW_UNCONFIRMED
        backend = self._picker_backend()
        picker = self._live_picker(backend) if backend is not None else None
        if picker is None or not _allowed(guard):
            return unconfirmed
        if not chromepicker.normal(display_name):
            return LaunchStatus.CHROME_PROFILE_REQUIRED
        try:
            if backend.foreground() != picker.hwnd:
                return unconfirmed  # focus moved; never act on a background picker
        except Exception:  # noqa: BLE001
            return unconfirmed
        cards = self._picker_cards(backend, picker, self.PICKER_READ_WAIT, guard)
        if not _allowed(guard):
            return unconfirmed
        card = chromepicker.unique_card(cards, display_name)
        if card is None:
            return LaunchStatus.CHROME_PROFILE_REQUIRED  # nothing pressed; the picker still waits
        offered = self.picker_offered
        if offered is not None and (not chromepicker.same_picker(picker, offered[0])
                                    or (card.key, card.label, card.button_name) not in offered[1]):
            return unconfirmed  # renamed, replaced or never shown: not what the owner chose
        # Re-read everything immediately before the one Invoke; foreground last.
        try:
            again = backend.identity(picker.hwnd)
            if not chromepicker.same_picker(again, picker) or backend.foreground() != picker.hwnd:
                return unconfirmed
            fresh = chromepicker.unique_card(backend.cards(again), display_name)
            if fresh is None or (fresh.key, fresh.label, fresh.button_name) != (
                    card.key, card.label, card.button_name):
                return unconfirmed
            before = backend.process_windows(picker.pid)
            if not chromepicker.same_picker(backend.identity(picker.hwnd), picker) \
                    or backend.foreground() != picker.hwnd or not _allowed(guard):
                return unconfirmed
        except Exception:  # noqa: BLE001
            return unconfirmed
        self.picker_context = self.picker_offered = None  # one press per picker: no retries, ever

        def allowed():  # runs on the UIA thread just before Invoke
            return _allowed(guard) and backend.foreground() == picker.hwnd

        deadline = self.clock() + self.PICKER_CONFIRM_WAIT
        try:
            backend.invoke(replace(fresh, allowed=allowed))
        except Exception:  # noqa: BLE001
            # Withdrawn, failed or sent without an answer (chromepicker.ClickSent):
            # a window that appears now may be the owner's own choice. No receipt.
            return unconfirmed
        return self._confirm_profile_window(backend, picker, before, deadline, guard)

    def _confirm_profile_window(self, backend, picker, before, deadline, guard=lambda: True):
        while _allowed(guard):
            try:
                now = backend.identity(picker.hwnd)
                closed = now is None or not now.visible or not chromepicker.same_picker(now, picker)
                hwnd = backend.foreground()
                window = backend.identity(hwnd) if hwnd and hwnd != picker.hwnd else None
                # A window that vanished between the two reads decides nothing.
                if window is not None:
                    if window.pid != picker.pid:
                        return LaunchStatus.CHROME_WINDOW_UNCONFIRMED  # focus left this Chrome
                    if closed and chromepicker.is_profile_window(window, picker, before):
                        # Seen after the deadline, or after the clip was revoked, proves nothing.
                        if self.clock() <= deadline and _allowed(guard):
                            return LaunchStatus.LAUNCHED
                        return LaunchStatus.CHROME_WINDOW_UNCONFIRMED
            except Exception:  # noqa: BLE001
                return LaunchStatus.CHROME_WINDOW_UNCONFIRMED
            remaining = deadline - self.clock()
            if remaining <= 0:
                return LaunchStatus.CHROME_WINDOW_UNCONFIRMED
            self.sleep(min(remaining, 0.1))
        return LaunchStatus.CHROME_WINDOW_UNCONFIRMED

    ROUTE_NAMES = 512
    ROUTE_NAME_CHARS = 80

    def route_names(self):
        """(installed app names, names of apps with an open window) for routing.

        Only Start catalog display names and the fixed aliases above are
        offered; window titles and other window text never leave the helper.
        An app counts as open when ``grounded_window`` (what a routed
        ``switch_app(exact=True)`` uses) finds a window for it.
        """
        self.refresh_apps()

        def usable(name):
            return (isinstance(name, str) and 0 < len(name) <= self.ROUTE_NAME_CHARS and name.strip() == name
                    and not any(unicodedata.category(c).startswith("C") for c in name))

        apps, seen = [], set()
        for name in [app["name"] for app in self.apps] + [
                alias for alias, canonical in ALIASES.items()
                if any(_normal(app["name"]) == canonical for app in self.apps)]:
            if usable(name) and name.casefold() not in seen and len(apps) < self.ROUTE_NAMES:
                seen.add(name.casefold())
                apps.append(name)
        try:
            windows = list(self.desktop.windows())
        except Exception:  # noqa: BLE001  (no enumeration: nothing to switch to)
            windows = []
        open_names = tuple(name for name in apps if windows and grounded_window(name, windows, self.apps) is not None)
        return tuple(apps), open_names

    def switch_app(self, target, *, exact=False):
        """Focus ``target``'s window. ``exact`` grounds a routed catalog name.

        The clip's revocation check is read once and asked again after the
        window enumeration, immediately before focus: a capture that was
        superseded while windows were listed never moves the foreground.
        """
        guard = action_guard.get()
        if not _allowed(guard):
            return False
        windows = self.desktop.windows()
        hwnd = grounded_window(target, windows, self.apps) if exact else match_window(target, windows)
        if not hwnd or not _allowed(guard):
            return False
        return bool(self.desktop.focus(hwnd))

    def search(self, query):
        (self.opener or os.startfile)(search_url(query))
        return True

    # -- Website navigation -------------------------------------------------------
    # `url` must already be canonical (`voicetype.web_destination`). With a Chrome
    # browser window in front, the URL goes to that window's own profile: its
    # running chrome.exe is started again with `--profile-directory` (and the
    # process's `--user-data-dir`), which hands the URL to the running browser
    # over Chrome's process singleton; Chrome opens it in a new tab of that
    # profile's most recent window. Nothing is typed, and no key or click reaches
    # any window. True means only that the running Chrome accepted the URL (its
    # forwarding process exited with the handoff code); nobody saw a page load.
    # Any other foreground app gets the Windows default handler for the URL.
    # A Chrome window that cannot be resolved (the profile picker, an incognito,
    # app or guest window, another Chrome channel, a moved focus) gets nothing:
    # an incognito window shares its profile's AppUserModelID, and the handoff
    # would open the site in that profile's regular window.
    NAVIGATE_HANDOFF_WAIT = 8.0
    NAVIGATE_READ_WAIT = 3.0
    # A forwarding chrome.exe exits 0 once the running browser took the URL
    # (measured on Chrome 157); a browser it started itself would keep running.
    CHROME_HANDOFF_EXIT_CODES = frozenset({0})

    def navigate(self, url):
        guard = action_guard.get()  # this clip's revocation check, never re-looked-up
        if not isinstance(url, str) or not url or web_destination(url) != url or not _allowed(guard):
            return False
        try:
            window = self.desktop.foreground_window()
        except Exception:  # noqa: BLE001  (an unreadable foreground decides nothing)
            return False
        if not _allowed(guard):
            return False
        if window is not None and ntpath.basename(window[2] or "").casefold() == "chrome.exe":
            return self._navigate_chrome(window[0], url, guard)
        (self.opener or os.startfile)(url)
        return True

    def _chrome_browser_window(self, backend, hwnd):
        try:
            identity = backend.identity(hwnd)
        except Exception:  # noqa: BLE001
            return None
        if (not isinstance(identity, chromepicker.WindowIdentity) or identity.hwnd != hwnd
                or identity.class_name != chromepicker.PICKER_CLASS or identity.visible is not True
                or not isinstance(identity.exe, str) or not ntpath.isabs(identity.exe)
                or not installed_chrome_path(identity.exe)
                or not isinstance(identity.title, str)
                or not identity.title.strip().casefold().endswith(chromepicker.BROWSER_TITLE_SUFFIX)
                or chromepicker.is_picker_aumid(identity.aumid)):
            return None
        return identity

    def _navigate_chrome(self, hwnd, url, guard):
        backend = self._picker_backend()
        if backend is None:
            return False
        seen = self._chrome_browser_window(backend, hwnd)
        if seen is None:
            return False
        try:
            if self.window_names is not None:
                names = self.window_names(hwnd)
            else:
                names = backend.worker.call(lambda client, UIA: browser_window_names(client, UIA, hwnd),
                                            self.NAVIGATE_READ_WAIT)[1]
        except Exception:  # noqa: BLE001  (unreadable: never assume a regular window)
            return False
        if not regular_browser_window(names) or not _allowed(guard):
            return False
        # Chrome inserts an accessibility status before the product name on
        # network-error pages. Match the complete Win32 tab title in either
        # form so a private-mode prefix cannot be mistaken for the same tab.
        product = " - Google Chrome"
        tab = seen.title.removesuffix(product)
        regular_titles = (seen.title, f"{tab} - Network error{product}")
        if not any(names[0] == title or names[0].startswith(title + " - ") for title in regular_titles):
            return False
        read = self.command_line
        if read is None:
            if os.name != "nt":
                return False
            read = windows_command_line
        try:
            data_dir = chrome_user_data_dir(read(seen.pid))
        except Exception:  # noqa: BLE001
            return False
        profile = chrome_profile_directory(seen.aumid, data_dir) if data_dir is not None else None
        if profile is None:
            return False
        argv = [seen.exe] + ([f"--user-data-dir={data_dir}"] if data_dir else []) + [
            f"--profile-directory={profile}", "--single-argument", url]
        # The profile is bound to the window the owner was looking at: re-read
        # both immediately before the one launch.
        if not _allowed(guard):
            return False
        try:
            again = self._chrome_browser_window(backend, hwnd)
            front = self.desktop.foreground_window()
        except Exception:  # noqa: BLE001
            return False
        if (again is None or front is None or front[0] != hwnd
                or (again.pid, again.exe, again.aumid) != (seen.pid, seen.exe, seen.aumid)
                or not _allowed(guard)):
            return False
        try:
            process = self.launcher(argv, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, ValueError, subprocess.SubprocessError):
            return False
        return self._chrome_handoff(process, guard)

    def _chrome_handoff(self, process, guard):
        poll = getattr(process, "poll", None)
        if not callable(poll):
            return False  # no receipt from this launcher
        deadline = self.clock() + self.NAVIGATE_HANDOFF_WAIT
        while _allowed(guard):  # once revoked the launch stands, but proves nothing
            try:
                code = poll()
            except Exception:  # noqa: BLE001
                return False
            if code is not None:
                return code in self.CHROME_HANDOFF_EXIT_CODES and _allowed(guard)
            remaining = deadline - self.clock()
            if remaining <= 0:
                return False
            self.sleep(min(remaining, 0.05))
        return False

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
        self.picker_context = self.picker_offered = None
        for owner in (self.picker, self.desktop):  # the picker's UIA thread retires too
            close = getattr(owner, "close", None)
            if close:
                close()
