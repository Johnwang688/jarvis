"""The Windows-side desktop bridge.

WSL cannot touch the Windows UI, so this process does it. It runs on Windows
Python (its own venv — see `jarvis desktop setup`), dials *into* the WSL
server, and executes one request at a time.

**The bridge dials out, it does not listen.** WSL2 forwards `localhost` from
Windows into the distro, so an outbound connection needs no firewall rule and
no IP discovery — the WSL gateway address changes on every boot, a Windows
inbound listener needs a firewall exception, and both are things the owner
would have to re-fix. Outbound has neither problem.

Two accessibility backends, because one is not enough (both verified
2026-07-31 on this machine):

  uia   `uiautomation` / UI Automation. Native, UWP, WPF, WinForms. This is
        what the Settings app speaks.
  msaa  MSAA / IAccessible via comtypes. **Chromium — and therefore every
        Electron app, including Claude Desktop — publishes its real tree
        here and NOT over UIA.** Over UIA a Chromium window bottoms out at an
        empty DocumentControl (a bridged stub next to "Chrome Legacy Window");
        the identical window over MSAA yields the whole UI. Electron also has
        to be launched with `--force-renderer-accessibility` or its renderer
        tree stays switched off no matter which API you ask with. Both halves
        are required; neither alone is enough.

The protocol is newline-delimited JSON: the server sends one request object,
the bridge sends back exactly one response object.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import io
import json
import socket
import subprocess
import sys
import time
import traceback

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import comtypes
import comtypes.client
import uiautomation as auto
from comtypes.automation import VARIANT

from uiatree import (  # noqa: E402  (shared with the tests; no Windows imports)
    StaleRef, clean, forbidden_title, resolve, snapshot,
)

comtypes.client.GetModule("oleacc.dll")
from comtypes.gen import Accessibility as ACC  # noqa: E402

BRIDGE_VERSION = "1"

user32 = ctypes.windll.user32
oleacc = ctypes.windll.oleacc

OBJID_CLIENT = -4
STATE_UNAVAILABLE = 0x00000001
STATE_FOCUSABLE = 0x00100000
STATE_INVISIBLE = 0x00008000
STATE_OFFSCREEN = 0x00010000
STATE_READONLY = 0x00000040
STATE_CHECKED = 0x00000010
STATE_PRESSED = 0x00000008
STATE_SELECTED = 0x00000002

auto.SetGlobalSearchTimeout(2)

# ---------------------------------------------------------------------------
# MSAA role table (winuser.h ROLE_SYSTEM_*)
# ---------------------------------------------------------------------------

MSAA_ROLES = {
    1: "titlebar", 2: "menubar", 3: "scrollbar", 4: "grip", 8: "alert",
    9: "window", 10: "client", 11: "menupopup", 12: "menuitem", 13: "tooltip",
    14: "application", 15: "document", 16: "pane", 17: "chart", 18: "dialog",
    19: "border", 20: "group", 21: "separator", 22: "toolbar", 23: "statusbar",
    24: "table", 25: "columnheader", 26: "rowheader", 27: "column", 28: "row",
    29: "cell", 30: "link", 31: "balloon", 33: "list", 34: "listitem",
    35: "outline", 36: "treeitem", 37: "tab", 38: "propertypage",
    39: "indicator", 40: "image", 41: "text", 42: "editable", 43: "button",
    44: "checkbox", 45: "radio", 46: "combobox", 47: "droplist",
    48: "progressbar", 49: "dial", 50: "hotkey", 51: "slider", 52: "spinner",
    53: "diagram", 54: "animation", 55: "equation", 56: "buttondropdown",
    57: "menubutton", 58: "buttondropdowngrid", 59: "whitespace",
    60: "tablist", 61: "clock", 62: "splitbutton",
}

MSAA_INTERACTIVE = {
    12, 30, 34, 36, 37, 42, 43, 44, 45, 46, 47, 51, 52, 56, 57, 62,
}

UIA_INTERACTIVE = {
    "ButtonControl", "EditControl", "ComboBoxControl", "CheckBoxControl",
    "RadioButtonControl", "ListItemControl", "HyperlinkControl",
    "TabItemControl", "SliderControl", "MenuItemControl", "TreeItemControl",
    "SplitButtonControl", "SpinnerControl",
}

UIA_ROLE_NAMES = {
    "ButtonControl": "button", "EditControl": "editable", "TextControl": "text",
    "ComboBoxControl": "combobox", "CheckBoxControl": "checkbox",
    "RadioButtonControl": "radio", "ListItemControl": "listitem",
    "HyperlinkControl": "link", "TabItemControl": "tab",
    "SliderControl": "slider", "MenuItemControl": "menuitem",
    "TreeItemControl": "treeitem", "ImageControl": "image",
    "GroupControl": "group", "PaneControl": "pane", "ListControl": "list",
    "WindowControl": "window", "DocumentControl": "document",
    "TableControl": "table", "CustomControl": "custom",
    "SplitButtonControl": "splitbutton", "SpinnerControl": "spinner",
    "MenuBarControl": "menubar", "ToolBarControl": "toolbar",
    "ScrollBarControl": "scrollbar", "SeparatorControl": "separator",
    "ProgressBarControl": "progressbar", "HeaderControl": "header",
    "HeaderItemControl": "headeritem", "StatusBarControl": "statusbar",
    "ThumbControl": "thumb", "TitleBarControl": "titlebar",
    "ToolTipControl": "tooltip", "TreeControl": "tree", "TabControl": "tab",
    "DataItemControl": "dataitem", "DataGridControl": "datagrid",
    "CalendarControl": "calendar", "ComboBoxItemControl": "listitem",
}


# ---------------------------------------------------------------------------
# Window lookup
# ---------------------------------------------------------------------------

EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def window_title(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def find_window(spec: dict) -> int | None:
    """Find a top-level window matching an app spec.

    `title` is a case-insensitive substring; `class`, when given, must match
    exactly. Invisible windows are skipped — Electron keeps hidden helpers
    around with the same title as the real one.
    """
    want_title = (spec.get("title") or "").lower()
    want_class = spec.get("class")
    hits: list[int] = []

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = window_title(hwnd)
        if not title:
            return True
        if want_title and want_title not in title.lower():
            return True
        if want_class and window_class(hwnd) != want_class:
            return True
        if forbidden_title(title):
            return True
        hits.append(hwnd)
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return hits[0] if hits else None


def _force_foreground(hwnd: int) -> bool:
    """Actually raise a window, working around Windows' foreground lock.

    A bare SetForegroundWindow from a process that is not already in the
    foreground is *ignored* — it returns failure and does nothing, which is
    exactly the silent no-op that made a screenshot of Settings come back
    showing whatever happened to be on top. Windows only honours the call from
    the thread that owns the current foreground window, so attach to that
    thread's input queue for the duration. The synthetic ALT tap is the second
    documented way to clear the lock, kept as a fallback for the case where
    the foreground window belongs to a process we cannot attach to.
    """
    if user32.GetForegroundWindow() == hwnd:
        return True

    kernel32 = ctypes.windll.kernel32
    VK_MENU, KEYEVENTF_KEYUP = 0x12, 0x0002

    for attempt in range(3):
        fore = user32.GetForegroundWindow()
        tid_fore = user32.GetWindowThreadProcessId(fore, None) if fore else 0
        tid_self = kernel32.GetCurrentThreadId()
        attached = False
        if tid_fore and tid_fore != tid_self:
            attached = bool(user32.AttachThreadInput(tid_self, tid_fore, True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetActiveWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(tid_self, tid_fore, False)

        if user32.GetForegroundWindow() == hwnd:
            return True

        if attempt == 0:
            user32.keybd_event(VK_MENU, 0, 0, 0)
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.2)

    return user32.GetForegroundWindow() == hwnd


def content_hwnd(spec: dict, frame: int) -> int:
    """Find the window that actually holds the app's UI.

    A UWP app is two windows: an ApplicationFrameWindow that owns the title
    bar, position and z-order, and a Windows.UI.Core.CoreWindow that owns
    everything the user actually sees. Windows sometimes parents the second
    under the first — in which case reading the frame is enough — and
    sometimes leaves it as its own top-level window, in which case the frame
    exposes seven nodes of chrome and nothing else. That difference showed up
    mid-session on the same Settings window, so it cannot be assumed either
    way; pick whichever root actually yields a tree.

    The frame stays the handle for activation and geometry regardless.
    """
    want_title = (spec.get("title") or "").lower()
    candidates: list[int] = []

    def cb(hwnd, _):
        if hwnd != frame and user32.IsWindowVisible(hwnd):
            if window_class(hwnd) == "Windows.UI.Core.CoreWindow":
                if want_title in window_title(hwnd).lower():
                    candidates.append(hwnd)
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    if not candidates:
        return frame

    def depth_count(handle: int) -> int:
        try:
            return UIABackend(handle)._count()
        except Exception:
            return 0

    best = max([frame] + candidates, key=depth_count)
    return best


def activate(hwnd: int, maximize: bool, require_foreground: bool = False) -> bool:
    """Bring a window to the foreground and wait for its tree to come back.

    UWP apps suspend when they lose the foreground and their accessibility
    tree collapses to zero children, so every read has to activate first.
    Maximizing matters for a second reason: Settings is responsive, and at a
    small width it *removes* the navigation list and collapses the search box
    rather than reflowing them. A predictable window size is a predictable
    tree, the same way a browser test pins a viewport.

    Returns whether the window actually reached the foreground. Reads and
    pattern-based clicks work regardless; anything that goes through the
    screen — a screenshot, synthetic keystrokes, a coordinate click — must
    pass `require_foreground=True` so a failed raise is an error rather than
    input delivered to the wrong window.
    """
    SW_RESTORE, SW_MAXIMIZE = 9, 3
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.ShowWindow(hwnd, SW_MAXIMIZE if maximize else SW_RESTORE)
    raised = _force_foreground(hwnd)
    time.sleep(0.35)
    if require_foreground and not raised:
        raise RuntimeError(
            "could not bring the window to the foreground — another window is "
            "holding focus. Click on the desktop and try again."
        )
    return raised


def _wait_settled(count_nodes, min_nodes: int, timeout: float) -> int:
    """Wait for an accessibility tree to finish populating.

    "Has any children" is not readiness, and believing it cost a whole
    snapshot once: a suspended UWP window still reports its frame children
    (title bar, input sink) while the content window underneath is empty, so
    the check passed instantly and the snapshot captured six lines of window
    chrome. Chromium has the same shape for a different reason — it builds
    the renderer tree lazily after the window exists.

    So wait for the count to both clear a floor and stop changing.
    """
    deadline = time.time() + timeout
    previous = -1
    while time.time() < deadline:
        count = count_nodes()
        if count >= min_nodes and count == previous:
            return count
        previous = count
        time.sleep(0.3)
    return count_nodes()


def _require_foreground(hwnd: int) -> None:
    """Guard for anything that reaches the app through the screen.

    Synthetic keys and coordinate clicks land wherever focus actually is, so
    if the window will not come forward the only safe move is to refuse.
    """
    if not _force_foreground(hwnd):
        raise RuntimeError(
            "the window would not come to the foreground, so keystrokes or "
            "clicks would land in the wrong app. Nothing was sent."
        )


# ---------------------------------------------------------------------------
# Backend: UI Automation
# ---------------------------------------------------------------------------

class UIABackend:
    name = "uia"

    def __init__(self, hwnd: int, root_hwnd: int | None = None):
        # `hwnd` is the window we activate and screenshot; `root_hwnd` is the
        # one we read. For UWP they are different windows (see content_hwnd).
        self.hwnd = hwnd
        self.root = auto.ControlFromHandle(root_hwnd or hwnd)

    def _count(self, cap: int = 60) -> int:
        total = [0]

        def walk(node, depth=0):
            if depth > 6 or total[0] >= cap:
                return
            for c in self.children(node):
                total[0] += 1
                if total[0] >= cap:
                    return
                walk(c, depth + 1)

        walk(self.root)
        return total[0]

    def ready(self, timeout: float = 10.0) -> bool:
        return _wait_settled(self._count, min_nodes=8, timeout=timeout) >= 8

    def children(self, node):
        try:
            return node.GetChildren()
        except Exception:
            return []

    def describe(self, node) -> dict:
        ctype = node.ControlTypeName
        value = ""
        try:
            if ctype in ("EditControl", "ComboBoxControl"):
                value = clean(node.GetValuePattern().Value)
        except Exception:
            pass
        # Windows 11 renders its settings switches as ToggleSwitch, which UIA
        # reports as a *Button* that happens to support TogglePattern. Asking
        # only checkboxes for their state therefore left every switch in the
        # Settings app stateless in the snapshot — the model could see
        # "Bluetooth" but not whether it was on, and answered from thin air.
        checked = None
        try:
            checked = {0: False, 1: True}.get(node.GetTogglePattern().ToggleState)
        except Exception:
            pass
        # Selection is a different fact from checked-ness: a nav item is
        # "selected", a switch is "on". Conflating them made every Settings
        # nav row report checked=false.
        selected = None
        try:
            selected = bool(node.GetSelectionItemPattern().IsSelected)
        except Exception:
            pass
        try:
            offscreen = bool(node.IsOffscreen)
        except Exception:
            offscreen = False
        try:
            enabled = bool(node.IsEnabled)
        except Exception:
            enabled = True
        try:
            r = node.BoundingRectangle
            rect = [r.left, r.top, r.right, r.bottom]
        except Exception:
            rect = [0, 0, 0, 0]
        return {
            "role": UIA_ROLE_NAMES.get(ctype, ctype.replace("Control", "").lower()),
            "name": clean(node.Name),
            "value": value,
            "checked": checked,
            "selected": selected,
            "offscreen": offscreen,
            "enabled": enabled,
            "interactive": ctype in UIA_INTERACTIVE,
            "rect": rect,
        }

    def click(self, node) -> str:
        for attempt in ("GetInvokePattern", "GetSelectionItemPattern",
                        "GetTogglePattern", "GetExpandCollapsePattern",
                        "GetLegacyIAccessiblePattern"):
            try:
                pat = getattr(node, attempt)()
            except Exception:
                continue
            try:
                if attempt == "GetInvokePattern":
                    pat.Invoke()
                elif attempt == "GetSelectionItemPattern":
                    pat.Select()
                elif attempt == "GetTogglePattern":
                    pat.Toggle()
                elif attempt == "GetExpandCollapsePattern":
                    pat.Expand()
                else:
                    pat.DoDefaultAction()
                return attempt.replace("Get", "").replace("Pattern", "")
            except Exception:
                continue
        # Last resort: a real click at the element's centre. This one goes
        # through the screen, so the window has to genuinely be on top.
        _require_foreground(self.hwnd)
        node.Click(simulateMove=False)
        return "MouseClick"

    def set_text(self, node, text: str) -> None:
        try:
            node.GetValuePattern().SetValue(text)
            return
        except Exception:
            pass
        _require_foreground(self.hwnd)
        node.SetFocus()
        auto.SendKeys("{Ctrl}a{Del}", waitTime=0)
        auto.SendKeys(_escape_keys(text), waitTime=0)

    def focus(self, node) -> None:
        node.SetFocus()


# ---------------------------------------------------------------------------
# Backend: MSAA / IAccessible  (Chromium, Electron, Claude Desktop)
# ---------------------------------------------------------------------------

class MSAANode:
    """An IAccessible plus a child id — MSAA's leaf elements are not objects."""

    __slots__ = ("acc", "cid")

    def __init__(self, acc, cid: int = 0):
        self.acc = acc
        self.cid = cid


class MSAABackend:
    name = "msaa"

    def __init__(self, hwnd: int, root_hwnd: int | None = None):
        self.hwnd = hwnd
        self.root_hwnd = root_hwnd or hwnd
        self.refresh_root()

    def refresh_root(self) -> None:
        """Re-fetch the root IAccessible.

        Chromium rebuilds its accessibility tree behind the scenes, and when
        it does, every pointer handed out before becomes inert — it does not
        raise, it just reports zero children from then on. A backend that
        cached the root once therefore saw the real tree exactly once and an
        empty one forever after. Anything that polls has to re-fetch.
        """
        pacc = ctypes.POINTER(ACC.IAccessible)()
        oleacc.AccessibleObjectFromWindow(
            wt.HWND(self.root_hwnd), OBJID_CLIENT,
            ctypes.byref(ACC.IAccessible._iid_), ctypes.byref(pacc))
        self.root = MSAANode(pacc, 0)

    def ready(self, timeout: float = 15.0) -> bool:
        """Chromium builds its tree lazily; wait for real content to appear."""

        def count() -> int:
            self.refresh_root()
            return len(self._collect(self.root, 0, []))

        return _wait_settled(count, min_nodes=8, timeout=timeout) >= 8

    def _collect(self, node, depth, acc_list, cap=40):
        if depth > 6 or len(acc_list) >= cap:
            return acc_list
        for c in self.children(node):
            acc_list.append(c)
            if len(acc_list) >= cap:
                return acc_list
            self._collect(c, depth + 1, acc_list, cap)
        return acc_list

    def children(self, node: MSAANode) -> list[MSAANode]:
        if node.cid:  # a leaf child id has no children of its own
            return []
        try:
            count = node.acc.accChildCount
        except Exception:
            return []
        if not count:
            return []
        arr = (VARIANT * count)()
        got = ctypes.c_long()
        try:
            oleacc.AccessibleChildren(
                ctypes.cast(node.acc, ctypes.POINTER(comtypes.IUnknown)),
                0, count, ctypes.byref(arr), ctypes.byref(got))
        except Exception:
            return []
        out = []
        for i in range(got.value):
            v = arr[i].value
            if v is None:
                continue
            if isinstance(v, int):
                out.append(MSAANode(node.acc, v))
            else:
                try:
                    out.append(MSAANode(v.QueryInterface(ACC.IAccessible), 0))
                except Exception:
                    pass
        return out

    def _state(self, node) -> int:
        try:
            s = node.acc.accState(node.cid)
            return int(s.value if isinstance(s, VARIANT) else s or 0)
        except Exception:
            return 0

    def describe(self, node: MSAANode) -> dict:
        try:
            r = node.acc.accRole(node.cid)
            role_id = int(r.value if isinstance(r, VARIANT) else r)
        except Exception:
            role_id = 0
        try:
            name = clean(node.acc.accName(node.cid))
        except Exception:
            name = ""
        try:
            value = clean(node.acc.accValue(node.cid))
        except Exception:
            value = ""
        state = self._state(node)
        checked = None
        if role_id in (44, 45) or (state & (STATE_CHECKED | STATE_PRESSED)):
            checked = bool(state & (STATE_CHECKED | STATE_PRESSED))
        selected = bool(state & STATE_SELECTED) or None
        interactive = role_id in MSAA_INTERACTIVE
        if role_id == 41 and not (state & STATE_FOCUSABLE):
            interactive = False  # static text
        if role_id == 42 and (state & STATE_READONLY):
            interactive = False
        try:
            l, t, w, h = node.acc.accLocation(node.cid)
            rect = [l, t, l + w, t + h]
        except Exception:
            rect = [0, 0, 0, 0]
        return {
            "role": MSAA_ROLES.get(role_id, str(role_id)),
            "name": name,
            "value": value,
            "checked": checked,
            "selected": selected,
            "offscreen": bool(state & (STATE_OFFSCREEN | STATE_INVISIBLE)),
            "enabled": not (state & STATE_UNAVAILABLE),
            "interactive": interactive,
            "rect": rect,
        }

    def click(self, node: MSAANode) -> str:
        try:
            node.acc.accDoDefaultAction(node.cid)
            return "DoDefaultAction"
        except Exception:
            pass
        info = self.describe(node)
        x0, y0, x1, y1 = info["rect"]
        if x1 > x0 and y1 > y0:
            _require_foreground(self.hwnd)
            auto.Click((x0 + x1) // 2, (y0 + y1) // 2, waitTime=0)
            return "MouseClick"
        raise RuntimeError("element exposes no default action and has no on-screen rect")

    def focus(self, node: MSAANode) -> None:
        SELFLAG_TAKEFOCUS = 0x1
        try:
            node.acc.accSelect(SELFLAG_TAKEFOCUS, node.cid)
        except Exception:
            info = self.describe(node)
            x0, y0, x1, y1 = info["rect"]
            if x1 > x0:
                _require_foreground(self.hwnd)
                auto.Click((x0 + x1) // 2, (y0 + y1) // 2, waitTime=0)

    def set_text(self, node: MSAANode, text: str) -> None:
        _require_foreground(self.hwnd)
        self.focus(node)
        time.sleep(0.15)
        auto.SendKeys("{Ctrl}a{Del}", waitTime=0)
        auto.SendKeys(_escape_keys(text), waitTime=0)


def _escape_keys(text: str) -> str:
    """uiautomation.SendKeys treats {} as special — escape literal text."""
    return text.replace("{", "{{}").replace("}", "{}}")


# ---------------------------------------------------------------------------
# Launching
# ---------------------------------------------------------------------------

class IApplicationActivationManager(comtypes.IUnknown):
    _iid_ = comtypes.GUID("{2e941141-7f97-4756-ba1d-9decde894a3d}")
    _methods_ = [
        comtypes.COMMETHOD([], ctypes.HRESULT, "ActivateApplication",
                           (["in"], wt.LPCWSTR, "appUserModelId"),
                           (["in"], wt.LPCWSTR, "arguments"),
                           (["in"], ctypes.c_int, "options"),
                           (["out"], ctypes.POINTER(wt.DWORD), "processId")),
    ]


CLSID_ApplicationActivationManager = comtypes.GUID(
    "{45BA127D-10A8-46EA-8AB7-56EA9078943C}")


def launch(spec: dict) -> str:
    """Start an app from its registry spec.

    Store/MSIX apps cannot be started from their exe path (WindowsApps is
    ACL'd) and `explorer.exe shell:AppsFolder\\…` drops any arguments, which
    would silently cost Electron its `--force-renderer-accessibility`. The
    activation manager is the only route that both starts a packaged app and
    passes a command line, so that is what "aumid" uses.
    """
    kind = spec["launch"]["kind"]
    target = spec["launch"]["target"]
    args = spec["launch"].get("args", "")

    if kind == "uri":
        subprocess.run(["cmd.exe", "/c", "start", "", target],
                       capture_output=True, timeout=20)
        return f"launched {target}"
    if kind == "aumid":
        comtypes.CoInitialize()
        mgr = comtypes.client.CreateObject(
            CLSID_ApplicationActivationManager,
            interface=IApplicationActivationManager)
        pid = mgr.ActivateApplication(target, args, 0)
        return f"activated {target} (pid {pid})"
    if kind == "exe":
        subprocess.Popen([target] + ([args] if args else []))
        return f"started {target}"
    raise RuntimeError(f"unknown launch kind {kind!r}")


# ---------------------------------------------------------------------------
# Session cache
# ---------------------------------------------------------------------------

STATE: dict[str, dict] = {}


def attach(spec: dict, relaunch_if_missing: bool = True) -> dict:
    """Find (or start) the app's window and build a backend against it."""
    hwnd = find_window(spec)
    if hwnd is None and relaunch_if_missing:
        launch(spec)
        deadline = time.time() + 30
        while time.time() < deadline and hwnd is None:
            time.sleep(1.0)
            hwnd = find_window(spec)
    if hwnd is None:
        raise RuntimeError(f"no window for {spec['name']!r}; it may have failed to start")

    title = window_title(hwnd)
    if forbidden_title(title):
        raise RuntimeError("refusing to attach to Jarvis's own window")

    activate(hwnd, spec.get("maximize", True))
    root_hwnd = (content_hwnd(spec, hwnd)
                 if spec.get("backend") != "msaa" else hwnd)
    backend = (MSAABackend if spec.get("backend") == "msaa"
               else UIABackend)(hwnd, root_hwnd)
    if not backend.ready():
        raise RuntimeError(
            f"{spec['name']} exposed no accessibility tree. "
            + ("Electron apps need --force-renderer-accessibility at launch; "
               "close it completely and let Jarvis relaunch it."
               if spec.get("backend") == "msaa" else
               "The window may still be loading.")
        )
    entry = {"hwnd": hwnd, "root_hwnd": root_hwnd, "backend": backend,
             "refs": {}, "spec": spec}
    STATE[spec["name"]] = entry
    return entry


def current(app: str) -> dict:
    entry = STATE.get(app)
    if entry is None:
        raise RuntimeError(f"{app} is not open. Call desktop_open first.")
    if not user32.IsWindow(entry["hwnd"]):
        raise RuntimeError(f"the {app} window was closed. Call desktop_open again.")
    return entry


def refresh_backend(entry: dict) -> None:
    """Rebuild the backend against the live window before acting on it."""
    spec = entry["spec"]
    activate(entry["hwnd"], spec.get("maximize", True))
    # The content window is re-resolved each time: a UWP app can swap
    # between the nested and top-level arrangement while it is running.
    entry["root_hwnd"] = (content_hwnd(spec, entry["hwnd"])
                          if spec.get("backend") != "msaa" else entry["hwnd"])
    entry["backend"] = (MSAABackend if spec.get("backend") == "msaa"
                        else UIABackend)(entry["hwnd"], entry["root_hwnd"])
    # Re-activating a UWP window wakes it from suspend, and the tree comes
    # back a beat later. Without this wait a snapshot taken right after a
    # click reads the window's chrome and nothing else.
    entry["backend"].ready()


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def take_snapshot(entry: dict, max_elements: int = 150) -> tuple[str, dict]:
    """Snapshot, retrying once if the tree came back empty.

    Chromium can invalidate its accessibility root between the readiness
    check and the traversal, and the symptom is not an error — it is a
    snapshot with nothing in it. Refetch and try again before believing that
    a window really has no elements.
    """
    backend = entry["backend"]
    text, refs = snapshot(backend, max_elements)
    if not refs and hasattr(backend, "refresh_root"):
        backend.refresh_root()
        text, refs = snapshot(backend, max_elements)
    return text, refs


def op_ping(_req):
    return {"version": BRIDGE_VERSION, "python": sys.version.split()[0]}


def op_open(req):
    spec = req["spec"]
    entry = attach(spec)
    text, refs = take_snapshot(entry, req.get("max_elements", 150))
    entry["refs"] = refs
    return {"title": window_title(entry["hwnd"]), "backend": entry["backend"].name,
            "snapshot": text}


def op_snapshot(req):
    entry = current(req["app"])
    refresh_backend(entry)
    text, refs = take_snapshot(entry, req.get("max_elements", 150))
    entry["refs"] = refs
    return {"snapshot": text}


def op_click(req):
    entry = current(req["app"])
    ref = req["ref"]
    if ref not in entry["refs"]:
        raise RuntimeError(f"unknown ref {ref}. Take a desktop_snapshot first.")
    refresh_backend(entry)
    node = resolve(entry["backend"], entry["refs"][ref])
    how = entry["backend"].click(node)
    time.sleep(req.get("settle", 0.8))
    return {"clicked": entry["refs"][ref]["name"] or ref, "via": how}


def op_type(req):
    entry = current(req["app"])
    ref = req["ref"]
    if ref not in entry["refs"]:
        raise RuntimeError(f"unknown ref {ref}. Take a desktop_snapshot first.")
    refresh_backend(entry)
    node = resolve(entry["backend"], entry["refs"][ref])
    entry["backend"].set_text(node, req["text"])
    if req.get("submit"):
        time.sleep(0.2)
        auto.SendKeys("{Enter}", waitTime=0)
    time.sleep(req.get("settle", 0.6))
    return {"typed_into": entry["refs"][ref]["name"] or ref}


def op_key(req):
    entry = current(req["app"])
    # Synthetic keystrokes go to whatever holds focus, so a failed raise here
    # would type into the owner's other window. Refuse instead.
    activate(entry["hwnd"], entry["spec"].get("maximize", True),
             require_foreground=True)
    auto.SendKeys(req["keys"], waitTime=0)
    time.sleep(req.get("settle", 0.6))
    return {"sent": req["keys"]}


def op_screenshot(req):
    from PIL import ImageGrab, ImageDraw

    entry = current(req["app"])
    # ImageGrab copies whatever pixels are on screen at that rectangle, so an
    # unraised window yields a picture of whatever is covering it.
    activate(entry["hwnd"], entry["spec"].get("maximize", True),
             require_foreground=True)
    rect = wt.RECT()
    user32.GetWindowRect(entry["hwnd"], ctypes.byref(rect))
    box = (max(rect.left, 0), max(rect.top, 0), rect.right, rect.bottom)
    image = ImageGrab.grab(bbox=box, all_screens=True)

    marks = 0
    if req.get("marked", True) and entry["refs"]:
        draw = ImageDraw.Draw(image)
        backend = entry["backend"]
        for ref, meta in entry["refs"].items():
            try:
                node = resolve(backend, meta)
                x0, y0, x1, y1 = backend.describe(node)["rect"]
            except Exception:
                continue
            if x1 <= x0 or y1 <= y0:
                continue
            x, y = x0 - box[0], y0 - box[1]
            if not (0 <= x < image.width and 0 <= y < image.height):
                continue
            draw.rectangle([x, y, x1 - box[0], y1 - box[1]], outline=(255, 60, 60), width=2)
            label = ref
            tw = 7 * len(label) + 6
            draw.rectangle([x, max(y - 14, 0), x + tw, max(y - 14, 0) + 14],
                           fill=(255, 60, 60))
            draw.text((x + 3, max(y - 14, 0) + 2), label, fill=(255, 255, 255))
            marks += 1

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    data = base64.b64encode(buf.getvalue()).decode()
    return {"image_b64": data, "bytes": len(data), "marks": marks,
            "size": [image.width, image.height]}


OPS = {
    "ping": op_ping, "open": op_open, "snapshot": op_snapshot,
    "click": op_click, "type": op_type, "key": op_key,
    "screenshot": op_screenshot,
}


# ---------------------------------------------------------------------------
# Connection loop
# ---------------------------------------------------------------------------

def serve_once(host: str, port: int) -> None:
    conn = socket.create_connection((host, port), timeout=10)
    conn.settimeout(None)
    print(f"[bridge] connected to {host}:{port}", flush=True)
    stream = conn.makefile("rwb")
    stream.write((json.dumps({"hello": "jarvis-desktop-bridge",
                              "version": BRIDGE_VERSION}) + "\n").encode())
    stream.flush()

    while True:
        line = stream.readline()
        if not line:
            print("[bridge] server closed the connection", flush=True)
            return
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        op = req.get("op")
        try:
            handler = OPS.get(op)
            if handler is None:
                raise RuntimeError(f"unknown op {op!r}")
            payload = {"id": req.get("id"), "ok": True, "result": handler(req)}
        except Exception as exc:
            payload = {"id": req.get("id"), "ok": False,
                       "error": f"{type(exc).__name__}: {exc}"}
            if req.get("debug"):
                payload["traceback"] = traceback.format_exc()
        stream.write((json.dumps(payload) + "\n").encode())
        stream.flush()
        print(f"[bridge] {op} -> {'ok' if payload['ok'] else payload['error']}", flush=True)


def main() -> None:
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2] if len(sys.argv) > 2 else 8404)
    comtypes.CoInitialize()
    print(f"[bridge] jarvis desktop bridge v{BRIDGE_VERSION}; "
          f"dialing {host}:{port} (Ctrl-C to stop)", flush=True)
    while True:
        try:
            serve_once(host, port)
        except (ConnectionRefusedError, socket.timeout, OSError) as exc:
            print(f"[bridge] waiting for Jarvis ({type(exc).__name__})", flush=True)
        except KeyboardInterrupt:
            print("[bridge] stopped", flush=True)
            return
        time.sleep(2.0)


if __name__ == "__main__":
    main()
