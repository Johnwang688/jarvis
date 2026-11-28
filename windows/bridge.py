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
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import traceback

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from desktopwire import APPS, NAV_KEYS, launch_name, private_key, prove_windows, wire
from desktopguard import (
    Guard, HUD_MARKER_NAME, HUD_REGION_NAME, KEY_NAMES, MaskedTree, Rect as ScreenRect, Refused, black_out,
    own_image, require_expected_control,
)
import windowpolicy as window_policy
from voiceactions import Actions, exact_start_app
from voicetype import WindowsInput

import comtypes
import comtypes.client
import uiautomation as auto
from comtypes.automation import VARIANT

from uiatree import (  # noqa: E402  (shared with the tests; no Windows imports)
    StaleRef, clean, forbidden_title, resolve, snapshot,
)

comtypes.client.GetModule("oleacc.dll")
from comtypes.gen import Accessibility as ACC  # noqa: E402

BRIDGE_VERSION = "2"

user32 = ctypes.windll.user32
oleacc = ctypes.windll.oleacc

OBJID_CLIENT = -4
STATE_UNAVAILABLE = 0x00000001
STATE_FOCUSED = 0x00000004
STATE_PROTECTED = 0x20000000
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


def registered_window(hwnd, spec, required_class=None):
    if not user32.IsWindow(hwnd) or forbidden_title(window_title(hwnd)):
        return False
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    proc = kernel.OpenProcess(0x1000, False, pid.value)
    if not proc:
        return False
    try:
        size = wt.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        return (kernel.QueryFullProcessImageNameW(proc, 0, buf, ctypes.byref(size))
                and os.path.basename(buf.value).casefold() in spec["processes"]
                and window_class(hwnd) == (required_class or spec["class"]))
    finally:
        kernel.CloseHandle(proc)


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
        if not registered_window(hwnd, spec):
            return True
        hits.append(hwnd)
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return hits[0] if hits else None


def _force_foreground(hwnd: int, *, alt_tap: bool = True) -> bool:
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

        if attempt == 0 and alt_tap:
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
            if (want_title in window_title(hwnd).lower()
                    and registered_window(hwnd, spec, "Windows.UI.Core.CoreWindow")):
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
    raised = raised and user32.GetForegroundWindow() == hwnd
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


def _click_control(hwnd, content, rect):
    x0, y0, x1, y1 = rect
    if x1 <= x0 or y1 <= y0:
        raise RuntimeError("Element has no on-screen rectangle.")
    _require_foreground(hwnd)
    point = wt.POINT((x0 + x1) // 2, (y0 + y1) // 2)
    user32.WindowFromPoint.argtypes, user32.WindowFromPoint.restype = [wt.POINT], wt.HWND
    user32.GetAncestor.argtypes, user32.GetAncestor.restype = [wt.HWND, wt.UINT], wt.HWND
    hit = user32.WindowFromPoint(point)
    if not hit or user32.GetAncestor(hit, 2) not in (hwnd, content):
        raise RuntimeError("Another window covers this control. Nothing was clicked.")
    auto.Click(point.x, point.y, waitTime=0)


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
        self.content = root_hwnd or hwnd
        self.root = auto.ControlFromHandle(self.content)

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
            if ctype in ("EditControl", "ComboBoxControl") and not node.IsPassword:
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
        _click_control(self.hwnd, self.content, self.describe(node)["rect"])
        return "MouseClick"

    def set_text(self, node, text: str) -> None:
        try:
            node.GetValuePattern().SetValue(text)
            return
        except Exception:
            pass
        _focus_field(self, node)
        auto.SendKeys("{Ctrl}a{Del}", waitTime=0)
        _require_focused_field(self, node)
        WindowsInput().text(text)

    def focus(self, node) -> None:
        node.SetFocus()

    def focused(self, node):
        return bool(node.HasKeyboardFocus)


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
        state = self._state(node)
        try:
            value = "" if state & STATE_PROTECTED else clean(node.acc.accValue(node.cid))
        except Exception:
            value = ""
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
            _click_control(self.hwnd, self.hwnd, info["rect"])
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
                _click_control(self.hwnd, self.hwnd, info["rect"])

    def set_text(self, node: MSAANode, text: str) -> None:
        _focus_field(self, node)
        auto.SendKeys("{Ctrl}a{Del}", waitTime=0)
        _require_focused_field(self, node)
        WindowsInput().text(text)

    def focused(self, node):
        return bool(self._state(node) & STATE_FOCUSED)


def _require_focused_field(backend, node):
    if user32.GetForegroundWindow() != backend.hwnd or not backend.focused(node):
        raise RuntimeError("The requested text field lost focus. No further keys were sent.")


def _focus_field(backend, node):
    _require_foreground(backend.hwnd)
    backend.focus(node)
    time.sleep(0.10)
    _require_focused_field(backend, node)


_app_actions = None


def op_launch_app(req):
    global _app_actions
    name = launch_name(req.get("app"))
    if name is None:
        raise ValueError("Use a normal Windows Start app display name.")
    if _app_actions is None:
        _app_actions = Actions()
    _app_actions.refresh_apps()
    installed = exact_start_app(name, _app_actions.apps)
    if installed is None:
        raise ValueError(f"{name} is not in the Windows Start menu.")
    _app_actions.launcher(["explorer.exe", "shell:AppsFolder\\" + installed["id"]],
                          creationflags=subprocess.CREATE_NO_WINDOW)
    return {"name": installed["name"]}


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
    if not registered_window(entry["hwnd"], entry["spec"]):
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

def take_snapshot(entry: dict, max_elements: int = 150, scope=None) -> tuple[str, dict]:
    """Snapshot, retrying once if the tree came back empty.

    Chromium can invalidate its accessibility root between the readiness
    check and the traversal, and the symptom is not an error — it is a
    snapshot with nothing in it. Refetch and try again before believing that
    a window really has no elements.
    """
    backend = entry["backend"]
    prefix = secrets.token_hex(8) + "-e" if scope else "e"
    entry["scope"] = scope
    text, refs = snapshot(backend, max_elements, ref_prefix=prefix)
    if not refs and hasattr(backend, "refresh_root"):
        backend.refresh_root()
        text, refs = snapshot(backend, max_elements, ref_prefix=prefix)
    return text, refs


def op_ping(_req):
    return {"version": BRIDGE_VERSION, "python": sys.version.split()[0]}


def op_open(req):
    name = req.get("app") or req.get("spec", {}).get("name")
    if name not in APPS:
        raise ValueError("Unknown registered app.")
    entry = attach({"name": name, **APPS[name]})
    text, refs = take_snapshot(entry, req.get("max_elements", 150), req.get("scope"))
    entry["refs"] = refs
    return {"title": window_title(entry["hwnd"]), "backend": entry["backend"].name,
            "snapshot": text}


def op_snapshot(req):
    entry = current(req["app"])
    refresh_backend(entry)
    text, refs = take_snapshot(entry, req.get("max_elements", 150), req.get("scope"))
    entry["refs"] = refs
    return {"snapshot": text}


def ref_window(req):
    entry = current(req["app"])
    if req.get("scope") != entry.get("scope"):
        raise RuntimeError("Another chat has a newer snapshot. Read a fresh snapshot first.")
    return entry


def op_click(req):
    entry = ref_window(req)
    ref = req["ref"]
    if ref not in entry["refs"]:
        raise RuntimeError(f"unknown ref {ref}. Take a desktop_snapshot first.")
    refresh_backend(entry)
    node = resolve(entry["backend"], entry["refs"][ref])
    how = entry["backend"].click(node)
    time.sleep(req.get("settle", 0.8))
    return {"clicked": entry["refs"][ref]["name"] or ref, "via": how}


def op_type(req):
    entry = ref_window(req)
    ref = req["ref"]
    if ref not in entry["refs"]:
        raise RuntimeError(f"unknown ref {ref}. Take a desktop_snapshot first.")
    refresh_backend(entry)
    node = resolve(entry["backend"], entry["refs"][ref])
    if req.get("scope") and entry["refs"][ref]["role"] not in ("editable", "combobox"):
        raise RuntimeError("This ref does not identify a text field.")
    entry["backend"].set_text(node, req["text"])
    if req.get("submit"):
        _focus_field(entry["backend"], node)
        auto.SendKeys("{Enter}", waitTime=0)
    time.sleep(req.get("settle", 0.6))
    return {"typed_into": entry["refs"][ref]["name"] or ref}


def op_key(req):
    entry = current(req["app"])
    # Synthetic keystrokes go to whatever holds focus, so a failed raise here
    # would type into the owner's other window. Refuse instead.
    activate(entry["hwnd"], entry["spec"].get("maximize", True),
             require_foreground=True)
    if req.get("scope") and req["keys"] not in NAV_KEYS.values():
        raise ValueError("Use a single supported navigation key.")
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


# ---------------------------------------------------------------------------
# Window-scoped desktop adapter and operations
# ---------------------------------------------------------------------------

_BROWSER_IMAGES = frozenset({
    "chrome.exe", "chromium.exe", "msedge.exe", "firefox.exe", "brave.exe",
    "vivaldi.exe", "opera.exe", "opera_gx.exe", "waterfox.exe", "librewolf.exe",
    "iexplore.exe", "tor.exe", "arc.exe", "zen.exe", "browser.exe",
})


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


class WindowsDesktop:
    def __init__(self):
        # CU1 Win32 signatures and last-error handling require Windows verification.
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        self.dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
        signatures = (
            (self.user.GetTopWindow, [wt.HWND], wt.HWND),
            (self.user.GetWindow, [wt.HWND, wt.UINT], wt.HWND),
            (self.user.IsWindowVisible, [wt.HWND], wt.BOOL),
            (self.user.IsIconic, [wt.HWND], wt.BOOL),
            (self.user.ShowWindow, [wt.HWND, ctypes.c_int], wt.BOOL),
            (self.user.GetWindowThreadProcessId, [wt.HWND, ctypes.POINTER(wt.DWORD)], wt.DWORD),
            # c_void_p: the attribute decides the type (a RECT for the frame, a DWORD for the cloak).
            (self.dwm.DwmGetWindowAttribute,
             [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD], ctypes.c_long),
            (self.user.MonitorFromWindow, [wt.HWND, wt.DWORD], wt.HMONITOR),
            (self.user.GetMonitorInfoW, [wt.HMONITOR, ctypes.POINTER(_MonitorInfo)], wt.BOOL),
            (self.user.GetWindowLongW, [wt.HWND, ctypes.c_int], wt.LONG),
            (self.user.GetWindowTextW, [wt.HWND, wt.LPWSTR, ctypes.c_int], ctypes.c_int),
            (self.user.GetClassNameW, [wt.HWND, wt.LPWSTR, ctypes.c_int], ctypes.c_int),
            (self.user.GetForegroundWindow, [], wt.HWND),
            (self.user.WindowFromPoint, [wt.POINT], wt.HWND),
            (self.user.GetAncestor, [wt.HWND, wt.UINT], wt.HWND),
            (self.user.SetCursorPos, [ctypes.c_int, ctypes.c_int], wt.BOOL),
            (self.user.GetCursorPos, [ctypes.POINTER(wt.POINT)], wt.BOOL),
            (self.user.GetAsyncKeyState, [ctypes.c_int], wt.SHORT),
            (self.user.SetThreadDpiAwarenessContext, [wt.HANDLE], wt.HANDLE),
            (self.kernel.OpenProcess, [wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE),
            (self.kernel.QueryFullProcessImageNameW,
             [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)], wt.BOOL),
            (self.kernel.CloseHandle, [wt.HANDLE], wt.BOOL),
            (self.advapi.OpenProcessToken,
             [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)], wt.BOOL),
            (self.advapi.GetTokenInformation,
             [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)], wt.BOOL),
        )
        for function, args, result in signatures:
            function.argtypes, function.restype = args, result

    def top_windows(self):
        # Windows verification pending: walk the actual z-order and refuse on failure or a cycle.
        handles = []
        seen = set()
        ctypes.set_last_error(0)
        hwnd = self.user.GetTopWindow(None)
        while hwnd:
            if hwnd in seen or len(seen) >= 10000:
                raise Refused("The desktop window order is unavailable.")
            seen.add(hwnd)
            if (self.user.IsWindowVisible(hwnd) and not self.user.IsIconic(hwnd)
                    and not self._cloaked(hwnd)):
                handles.append(hwnd)
            ctypes.set_last_error(0)
            hwnd = self.user.GetWindow(hwnd, 2)  # GW_HWNDNEXT
        if ctypes.get_last_error():
            raise Refused("The desktop window order is unavailable.")
        return handles

    def pid(self, hwnd):
        # Windows verification pending: owner PID lookup on a live HWND.
        pid = wt.DWORD()
        thread = self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value if thread and pid.value else None

    def _elevated(self, process):
        # Windows verification pending: TokenElevation is a single DWORD.
        token = wt.HANDLE()
        if not self.advapi.OpenProcessToken(process, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            return None
        try:
            elevated = wt.DWORD()
            size = wt.DWORD()
            if self.advapi.GetTokenInformation(
                    token, 20, ctypes.byref(elevated), ctypes.sizeof(elevated), ctypes.byref(size)):
                if size.value == ctypes.sizeof(elevated):
                    return bool(elevated.value)
            return None
        finally:
            self.kernel.CloseHandle(token)

    def _process_info(self, pid):
        # Windows verification pending: limited process access and full image query.
        if pid is None:
            return None, None
        process = self.kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not process:
            return None, None
        try:
            size = wt.DWORD(32768)
            path = ctypes.create_unicode_buffer(size.value)
            image = path.value if self.kernel.QueryFullProcessImageNameW(
                process, 0, path, ctypes.byref(size)) else None
            return image, self._elevated(process)
        finally:
            self.kernel.CloseHandle(process)

    def facts(self, hwnd):
        pid = self.pid(hwnd)
        image, elevated = self._process_info(pid)
        # Windows verification pending: class lookup and PID stability across the queries.
        name = ctypes.create_unicode_buffer(256)
        size = self.user.GetClassNameW(hwnd, name, len(name))
        if pid is None or pid != self.pid(hwnd):
            return window_policy.Window(None, None, None, None)
        # This also protects any other program run by the base Python, as intended.
        own = pid == os.getpid() or own_image(
            image, executable=sys.executable, prefix=sys.prefix,
            base_executable=getattr(sys, "_base_executable", None), base_prefix=sys.base_prefix)
        return window_policy.Window(image, elevated, name.value if size else None, own)

    def occlusion_rect(self, hwnd):
        # Windows verification pending: use the DWM visible frame in physical screen pixels.
        rect = wt.RECT()
        # DWMWA_EXTENDED_FRAME_BOUNDS
        if self.dwm.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
            return None
        return ScreenRect(rect.left, rect.top, rect.right, rect.bottom)

    def rect(self, hwnd):
        # Windows verification pending: clip to the monitor Windows assigns to the window.
        frame = self.occlusion_rect(hwnd)
        monitor = self.user.MonitorFromWindow(hwnd, 0)  # MONITOR_DEFAULTTONULL
        info = _MonitorInfo(cbSize=ctypes.sizeof(_MonitorInfo))
        if frame is None or not monitor or not self.user.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        bounds = info.rcMonitor
        return ScreenRect(max(frame.left, bounds.left), max(frame.top, bounds.top),
                          min(frame.right, bounds.right), min(frame.bottom, bounds.bottom))

    def _cloaked(self, hwnd):
        # Windows verification pending. A cloaked window (another virtual
        # desktop, a hidden shell surface such as Search or Start) reports
        # itself visible but has no pixels on screen: it is not listed, so it
        # can neither be captured nor stand in front of what is really there.
        # A failed query lists the window; the guard then judges it as usual.
        cloaked = wt.DWORD()
        result = self.dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        return result == 0 and cloaked.value != 0

    def title(self, hwnd):
        # Windows verification pending: zero length can also be a valid empty caption.
        buf = ctypes.create_unicode_buffer(32768)
        ctypes.set_last_error(0)
        size = self.user.GetWindowTextW(hwnd, buf, len(buf))
        if (not size and ctypes.get_last_error()) or size == len(buf) - 1:
            raise Refused("The window title is unavailable.")
        return buf.value

    def is_hud(self, hwnd):
        # Resolve the image even when the title matches to distinguish browsers from apps.
        if not forbidden_title(self.title(hwnd)):
            return False
        image, _ = self._process_info(self.pid(hwnd))
        basename = window_policy.image_name(image)
        if basename is None:
            raise Refused("The window image is unavailable.")
        return basename in _BROWSER_IMAGES

    def hud_regions(self, hwnd):
        # Windows verification pending: browser providers must expose the exact UIA
        # name and screen geometry of the labeled containers. Partial reads fail closed.
        try:
            root = auto.ControlFromHandle(hwnd)
            if root is None:
                return None
            pending = [root]
            regions = []
            visited = 0
            marker = False
            while pending:
                node = pending.pop()
                visited += 1
                if visited > 10000:
                    return None
                name = node.Name
                if name == HUD_MARKER_NAME:
                    marker = True
                if name == HUD_REGION_NAME and not node.IsOffscreen:
                    bounds = node.BoundingRectangle
                    regions.append(ScreenRect(bounds.left, bounds.top, bounds.right, bounds.bottom))
                pending.extend(node.GetChildren())
            if not marker:
                # First real run, 2026-11-19: Chrome showed 5 page nodes on the
                # first scan and 400 on a later one. No marker, no page.
                return None
            return list(dict.fromkeys(regions))
        except Exception:
            return None

    def foreground(self):
        # Windows verification pending: exact foreground HWND lookup.
        return self.user.GetForegroundWindow() or None

    def bring_forward(self, hwnd) -> bool:
        # Windows verification pending: restore only when minimized and verify the final foreground.
        if self.user.IsIconic(hwnd):
            self.user.ShowWindow(hwnd, 9)  # SW_RESTORE
        _force_foreground(hwnd, alt_tap=False)  # no key press reaches whatever is in front
        time.sleep(0.1)
        return self.foreground() == hwnd

    def window_at(self, x, y):
        # Windows verification pending: GA_ROOT maps a child hit to its top-level window.
        hit = self.user.WindowFromPoint(wt.POINT(x, y))
        return (self.user.GetAncestor(hit, 2) or None) if hit else None

    def move_pointer(self, point):
        # Windows verification pending: SetCursorPos and GetCursorPos use guard coordinates.
        if not self.user.SetCursorPos(*point):
            raise Refused("The pointer could not be positioned.")

    def wheel_reversed(self):
        # The Windows mouse setting "Scrolling direction" also flips injected wheel input.
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Mouse") as key:
                return winreg.QueryValueEx(key, "ReverseMouseWheelDirection")[0] == 1
        except OSError:
            return False

    def pointer_at(self, point):
        pos = wt.POINT()
        return self.user.GetCursorPos(ctypes.byref(pos)) and (pos.x, pos.y) == point

    def require_idle_input(self):
        # Windows verification pending: GetAsyncKeyState high bit reports currently held input.
        held = (0x01, 0x02, 0x04, 0x05, 0x06, 0x10, 0x11, 0x12, 0x5B, 0x5C)
        if any(self.user.GetAsyncKeyState(vk) & 0x8000 for vk in held):
            raise Refused("Release held mouse buttons or modifiers and try again.")

    def set_dpi_context(self, context):
        # Windows verification pending: per-monitor awareness aligns Win32 with UIA screen pixels.
        previous = self.user.SetThreadDpiAwarenessContext(context)
        if not previous:
            raise Refused("Windows could not establish screen pixel coordinates.")
        return previous


_WINDOW_DESKTOP = None
_WINDOW_GUARD = None
_WINDOW_REFS = {}
_WINDOW_SCREENSHOTS = {}


def _reset_window_ops():
    global _WINDOW_DESKTOP, _WINDOW_GUARD
    _WINDOW_DESKTOP = None
    _WINDOW_GUARD = None
    _WINDOW_REFS.clear()
    _WINDOW_SCREENSHOTS.clear()


def _window_grant(req):
    return _WINDOW_GUARD.capture(req.get("window"))


def _window_context(req):
    return req["scope"], req.get("window")


def _same_window(before, after):
    if (before.hwnd, before.rect) != (after.hwnd, after.rect):
        raise Refused("The window moved. Inspect it again.")


def _window_backend(hwnd):
    # Windows verification pending: UIA must expose this HWND's live tree without activation.
    return UIABackend(hwnd)


def op_windows(_req):
    return {"windows": _WINDOW_GUARD.windows()}


def op_window_front(req):
    window = req.get("window")
    hwnd = _WINDOW_GUARD.forward(window)
    _WINDOW_DESKTOP.require_idle_input()
    # Judged again last, right before the window is raised: the checks above take time.
    if _WINDOW_GUARD.forward(window) != hwnd:
        raise Refused("The window changed. List windows again.")
    if not _WINDOW_DESKTOP.bring_forward(hwnd):
        raise Refused("Windows did not bring that window to the front.")
    if _WINDOW_GUARD.forward(window) != hwnd:
        raise Refused("The window changed. List windows again.")
    return {"front": True}


def op_window_snapshot(req):
    grant = _window_grant(req)
    tree = MaskedTree(_window_backend(grant.hwnd), grant.masks)
    text, refs = snapshot(tree, ref_prefix=secrets.token_hex(8) + "-e")
    if grant != _window_grant(req):
        raise Refused("The window changed during the snapshot. Inspect it again.")
    _WINDOW_REFS[_window_context(req)] = refs
    return {"snapshot": text, "refs": {
        ref: {"name": info["name"], "role": info["role"]} for ref, info in refs.items()
    }}


def _window_selector(req, *, focus=False, ref_only=False):
    selectors = []
    if "ref" in req:
        if not isinstance(req["ref"], str):
            raise Refused("Use a ref from the current snapshot.")
        selectors.append("ref")
    if "x" in req or "y" in req:
        if any(type(req.get(axis)) is not int for axis in ("x", "y")):
            raise Refused("Use integer screenshot coordinates.")
        selectors.append("point")
    if "focus" in req:
        if req["focus"] is not True or not focus:
            raise Refused("Focus is unavailable for this operation.")
        selectors.append("focus")
    if len(selectors) != 1 or (ref_only and selectors[0] != "ref"):
        raise Refused("Specify one supported control selector.")
    return selectors[0]


def _window_point(req, grant):
    if _WINDOW_SCREENSHOTS.get(_window_context(req)) != grant.rect:
        raise Refused("Take a fresh screenshot of this window before using coordinates.")
    return grant.screenshot_point(req["x"], req["y"])


def _owned_window_control(node, hwnd):
    # Windows verification pending: UIA parent links associate point and focus
    # results with the target, including providers with no child NativeWindowHandle.
    for _ in range(128):
        if node is None:
            break
        if node.NativeWindowHandle == hwnd:
            return True
        node = node.GetParentControl()
    return False


def _window_control(req, grant, selector, point=None):
    backend = _window_backend(grant.hwnd)
    if selector == "ref":
        meta = _WINDOW_REFS.get(_window_context(req), {}).get(req["ref"])
        if meta is None:
            raise Refused("Take a fresh snapshot for this scope and window.")
        try:
            node = resolve(MaskedTree(backend, grant.masks), meta)
        except StaleRef:
            raise Refused("The control changed. Take a fresh snapshot.") from None
    elif selector == "point":
        grant.check_point(point)
        if _WINDOW_DESKTOP.window_at(*point) != grant.hwnd:
            raise Refused("The requested point is covered.")
        # Windows verification pending: UIA hit testing must follow screen coordinates.
        node = auto.ControlFromPoint(*point)
    else:
        if _WINDOW_DESKTOP.foreground() != grant.hwnd:
            raise Refused("The target window must be in the foreground.")
        # Windows verification pending: UIA focused-control lookup and keyboard focus flag.
        node = auto.GetFocusedControl()
        if node is None or not node.HasKeyboardFocus:
            raise Refused("The focused control is unavailable.")
    if not _owned_window_control(node, grant.hwnd):
        raise Refused("The control does not belong to this window.")
    info = backend.describe(node)
    bounds = ScreenRect(*info["rect"])
    if info["offscreen"]:
        raise Refused("The requested control is offscreen.")
    grant.check_control(bounds, point)
    return node, info, bounds


def op_window_probe(req):
    selector = _window_selector(req, focus=True)
    grant = _window_grant(req)
    point = _window_point(req, grant) if selector == "point" else None
    _, info, _ = _window_control(req, grant, selector, point)
    if grant != _window_grant(req):
        raise Refused("The window changed during the probe. Inspect it again.")
    return {"name": info["name"], "role": info["role"], "rect": info["rect"]}


def _window_input_control(req, grant, selector, point, *, keys=None):
    live = _window_grant(req)
    _same_window(grant, live)
    node, info, bounds = _window_control(req, live, selector, point)
    require_expected_control(req.get("expect"), info)
    if not info["enabled"]:
        raise Refused("The control is disabled.")
    if selector == "ref":
        point = ((bounds.left + bounds.right) // 2, (bounds.top + bounds.bottom) // 2)
    acted = _WINDOW_GUARD.act(req["window"], point=point, element=bounds, keys=keys,
                              focus=bounds if keys is not None else None)
    _same_window(live, acted)
    if any(mask not in live.masks for mask in acted.masks):
        raise Refused("The protected areas changed. Inspect the window again.")
    return node, info, point


def _window_mouse(req, *, amount=None):
    selector = _window_selector(req)
    grant = _window_grant(req)
    point = _window_point(req, grant) if selector == "point" else None
    _, _, destination = _window_input_control(req, grant, selector, point)
    _WINDOW_DESKTOP.require_idle_input()
    _WINDOW_DESKTOP.move_pointer(destination)
    # Re-read after moving the pointer: hover can open or replace a control.
    node, _, current = _window_input_control(req, grant, selector, point)
    if destination != current or not _WINDOW_DESKTOP.pointer_at(current):
        raise Refused("The pointer or control moved. Inspect the window again.")
    if selector == "ref":
        # Windows verification pending: a physical click or wheel hit must still be the ref.
        hit = auto.ControlFromPoint(*current)
        if hit is None or not auto.ControlsAreSame(node, hit):
            raise Refused("The pointer is over a different control.")
    if amount is not None and _WINDOW_DESKTOP.foreground() != grant.hwnd:
        raise Refused("The target window must be in the foreground to scroll.")
    _WINDOW_DESKTOP.require_idle_input()
    # Windows verification pending: SendInput mouse flags and wheel direction.
    backend = WindowsInput()
    events = (backend.INPUT * (2 if amount is None else 1))()
    if amount is None:
        events[0].mi.dwFlags, events[1].mi.dwFlags = 0x0002, 0x0004
    else:
        events[0].mi.dwFlags = 0x0800
        # A positive amount scrolls up whichever way the owner's wheel is set (seen live 2026-11-28).
        notches = -amount if _WINDOW_DESKTOP.wheel_reversed() else amount
        events[0].mi.mouseData = (notches * 120) & 0xFFFFFFFF
    sent = backend.user32.SendInput(len(events), events, ctypes.sizeof(backend.INPUT))
    if sent != len(events):
        if amount is None and sent == 1:
            backend.user32.SendInput(1, ctypes.byref(events[1]), ctypes.sizeof(backend.INPUT))
        raise Refused("Windows could not send the complete mouse input.")


def op_window_click(req):
    _window_mouse(req)
    return {"clicked": True}


def op_window_scroll(req):
    amount = req.get("amount")
    if type(amount) is not int or not 0 < abs(amount) <= 100:
        raise Refused("Use a signed scroll amount of 1 to 100 wheel detents.")
    _window_mouse(req, amount=amount)
    return {"scrolled": amount}


_WINDOW_MODIFIER_CODES = {
    "shift": 0xA0, "lshift": 0xA0, "rshift": 0xA1,
    "ctrl": 0xA2, "control": 0xA2, "lctrl": 0xA2, "rctrl": 0xA3,
    "alt": 0xA4, "lalt": 0xA4, "ralt": 0xA5, "altgr": 0xA5,
}
_WINDOW_KEY_CODES = {
    **_WINDOW_MODIFIER_CODES,
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "esc": 0x1B, "escape": 0x1B, "space": 0x20,
    "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "insert": 0x2D, "delete": 0x2E, "del": 0x2E,
    **{char.lower(): ord(char) for char in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"},
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
}
if set(_WINDOW_KEY_CODES) != set(KEY_NAMES):  # the guard passes exactly what this table can send
    raise RuntimeError("The bridge key table and the guard's key names differ.")


def _window_keys(keys):
    if not isinstance(keys, list) or not 1 <= len(keys) <= len(_WINDOW_MODIFIER_CODES) + 1:
        raise Refused("Supply one key name and any modifiers as a list.")
    codes = [_WINDOW_KEY_CODES.get(key.strip().casefold()) if isinstance(key, str) else None
             for key in keys]
    if None in codes or len(set(codes)) != len(codes):
        raise Refused("Use distinct supported key names.")
    return sorted(codes, key=lambda vk: vk not in _WINDOW_MODIFIER_CODES.values())


def _send_window_keys(backend, codes):
    # Windows verification pending: virtual-key chord down/up order and extended flags.
    extended = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0xA3, 0xA5}
    down = [(vk, 0, int(vk in extended)) for vk in codes]
    up = [(vk, scan, flags | 2) for vk, scan, flags in reversed(down)]
    try:
        backend._send(down + up)
    except OSError:
        backend._send(up)
        raise


def op_window_key(req):
    keys = req.get("keys")
    codes = _window_keys(keys)
    grant = _window_grant(req)
    backend = WindowsInput()
    _window_input_control(req, grant, "focus", None, keys=keys)
    _WINDOW_DESKTOP.require_idle_input()
    _send_window_keys(backend, codes)
    return {"sent": keys}


def op_window_type(req):
    selector = _window_selector(req, ref_only=True)
    text = req.get("text")
    submit = req.get("submit", False)
    if (not isinstance(text, str) or any(ord(char) < 32 or ord(char) == 127 for char in text)
            or type(submit) is not bool):
        raise Refused("Provide plain text and a boolean submit flag.")
    grant = _window_grant(req)
    node, info, point = _window_input_control(req, grant, selector, None)
    if info["role"] not in ("editable", "combobox"):
        raise Refused("The ref must identify a text field.")
    if _WINDOW_DESKTOP.foreground() != grant.hwnd:
        raise Refused("The target window must be in the foreground.")
    # Windows verification pending: UIA SetFocus must focus this exact field.
    node.SetFocus()

    def verify_focus(keys):
        focused, _, _ = _window_input_control(req, grant, "focus", None, keys=keys)
        # Windows verification pending: UIA element identity survives a fresh focus lookup.
        if not auto.ControlsAreSame(node, focused) or _WINDOW_DESKTOP.window_at(*point) != grant.hwnd:
            raise Refused("The requested text field lost focus.")
        _WINDOW_DESKTOP.require_idle_input()

    backend = WindowsInput()
    verify_focus(["ctrl", "a"])
    _send_window_keys(backend, [0x11, 0x41])
    if text:
        # Typed text is unmodified character keys: one such key stands for it in the guard.
        verify_focus(["a"])
        # Windows verification pending: Unicode SendInput to the checked text field.
        backend.text(text)
    else:
        verify_focus(["backspace"])
        _send_window_keys(backend, [0x08])
    if submit:
        verify_focus(["enter"])
        _send_window_keys(backend, [0x0D])
    return {"typed": True}


def op_window_screenshot(req):
    from PIL import ImageGrab

    grant = _window_grant(req)
    rect = grant.rect
    # Windows verification pending: multi-monitor screen pixels, including negative origins.
    image = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom), all_screens=True)
    after = _window_grant(req)
    _same_window(grant, after)
    if image.size != (rect.right - rect.left, rect.bottom - rect.top):
        raise Refused("The screenshot does not match the window bounds.")
    if req.get("front") is True and not _WINDOW_GUARD.in_front(grant.hwnd):
        # The caller picked this window because it was in front; the owner has since switched.
        raise Refused("That window is no longer in front. Take the screenshot again.")
    black_out(image, (rect.left, rect.top), grant.masks + after.masks)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    data = base64.b64encode(buf.getvalue()).decode()
    _WINDOW_SCREENSHOTS[_window_context(req)] = rect
    return {"image_b64": data, "bytes": len(data), "marks": 0, "size": list(image.size)}


_WINDOW_OPS = {
    "windows": op_windows, "window_snapshot": op_window_snapshot,
    "window_front": op_window_front,
    "window_probe": op_window_probe, "window_click": op_window_click,
    "window_type": op_window_type, "window_key": op_window_key,
    "window_scroll": op_window_scroll, "window_screenshot": op_window_screenshot,
}


def _scoped_window_op(op, req, paired):
    global _WINDOW_DESKTOP, _WINDOW_GUARD
    if _WINDOW_GUARD is None:
        _WINDOW_DESKTOP = WindowsDesktop()
        _WINDOW_GUARD = Guard(_WINDOW_DESKTOP)
    _WINDOW_GUARD.set_hud(req.get("hud_sensitive"))
    if not paired or _WINDOW_DESKTOP._process_info(os.getpid())[1] is not False:
        raise Refused("Window operations need the paired, non-elevated Windows bridge.")
    if not isinstance(req.get("scope"), str) or not req["scope"].strip():
        raise Refused("Window operations need a scope.")
    previous = _WINDOW_DESKTOP.set_dpi_context(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
    try:
        return _WINDOW_OPS[op](req)
    finally:
        _WINDOW_DESKTOP.set_dpi_context(previous)


OPS = {
    "ping": op_ping, "open": op_open, "snapshot": op_snapshot,
    "click": op_click, "type": op_type, "key": op_key,
    "screenshot": op_screenshot, "launch_app": op_launch_app,
}


# ---------------------------------------------------------------------------
# Connection loop
# ---------------------------------------------------------------------------

def serve_once(host: str, port: int) -> None:
    conn = socket.create_connection((host, port), timeout=10)
    print(f"[bridge] connected to {host}:{port}", flush=True)
    stream = conn.makefile("rwb")
    key_path = Path(sys.prefix).parent / "voice-link.key"
    paired = key_path.exists()
    if paired:
        prove_windows(stream, private_key(key_path))
    else:
        wire.send_json(stream, {"hello": "jarvis-desktop-bridge", "version": "1"})
    conn.settimeout(None)
    STATE.clear()  # a new daemon connection cannot reuse a previous chat snapshot
    _reset_window_ops()

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
        if isinstance(op, str) and op in _WINDOW_OPS:
            try:
                result = _scoped_window_op(op, req, paired)
                payload = {"id": req.get("id"), "ok": True, "result": result}
            except Refused as exc:
                payload = {"id": req.get("id"), "ok": False, "error": f"Refused: {exc}"}
            except Exception as exc:
                # The reply stays general. The cause goes to this console only.
                frame = traceback.extract_tb(exc.__traceback__)[-1]
                print(f"[bridge] {op} failed: {type(exc).__name__} at {Path(frame.filename).name}:{frame.lineno}",
                      flush=True)
                payload = {"id": req.get("id"), "ok": False,
                           "error": "Refused: Windows could not complete the operation safely."}
            stream.write((json.dumps(payload) + "\n").encode())
            stream.flush()
            print(f"[bridge] {op} -> {'ok' if payload['ok'] else payload['error']}", flush=True)
            continue
        try:
            if (req.get("scope") or op == "launch_app") and (not paired or ctypes.windll.shell32.IsUserAnAdmin()):
                raise RuntimeError("HUD desktop actions need the paired, non-elevated Windows bridge.")
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
        except (OSError, wire.LinkError, wire.KeyFileError) as exc:
            print(f"[bridge] waiting for Jarvis ({type(exc).__name__})", flush=True)
        except KeyboardInterrupt:
            print("[bridge] stopped", flush=True)
            return
        time.sleep(2.0)


if __name__ == "__main__":
    main()
