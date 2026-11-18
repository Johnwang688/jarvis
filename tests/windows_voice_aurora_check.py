"""Real Windows Aurora edge windows on an unseen desktop.

Run with the Windows voice venv: python tests/windows_voice_aurora_check.py.
The test uses rectangles, window styles and Win32 hit testing. It never reads
screen pixels, sends input, opens a microphone, or switches the input desktop.
"""
import ctypes
from ctypes import wintypes as wt
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def probe():
    from windows_voice_hold_overlay_check import isolated_desktop
    isolated_desktop()
    import tkinter as tk
    from windows import voiceui as aurora

    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    enum_window = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    enum_monitor = ctypes.WINFUNCTYPE(wt.BOOL, wt.HANDLE, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)

    def bind(lib, name, result, *args):
        method = getattr(lib, name)
        method.argtypes, method.restype = list(args), result
        return method

    thread_id = bind(kernel, "GetCurrentThreadId", wt.DWORD)
    desktop_for = bind(user, "GetThreadDesktop", wt.HANDLE, wt.DWORD)
    enum_desktop = bind(user, "EnumDesktopWindows", wt.BOOL, wt.HANDLE, enum_window, wt.LPARAM)
    monitors = bind(user, "EnumDisplayMonitors", wt.BOOL, wt.HDC, ctypes.POINTER(wt.RECT), enum_monitor, wt.LPARAM)
    title = bind(user, "GetWindowTextW", ctypes.c_int, wt.HWND, wt.LPWSTR, ctypes.c_int)
    visible = bind(user, "IsWindowVisible", wt.BOOL, wt.HWND)
    get_rect = bind(user, "GetWindowRect", wt.BOOL, wt.HWND, ctypes.POINTER(wt.RECT))
    styles = bind(user, "GetWindowLongW", wt.LONG, wt.HWND, ctypes.c_int)
    active = bind(user, "GetActiveWindow", wt.HWND)
    activate = bind(user, "SetActiveWindow", wt.HWND, wt.HWND)
    locate = bind(user, "WindowFromPoint", wt.HWND, wt.POINT)
    create = bind(user, "CreateWindowExW", wt.HWND, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR,
                  wt.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                  wt.HWND, wt.HMENU, wt.HINSTANCE, ctypes.c_void_p)
    destroy = bind(user, "DestroyWindow", wt.BOOL, wt.HWND)
    dpi = bind(user, "SetThreadDpiAwarenessContext", wt.HANDLE, wt.HANDLE)
    desktop = desktop_for(thread_id())
    root = tk.Tk()
    root.withdraw()

    def pump(duration):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            root.update()
            time.sleep(0.005)
        root.update()

    def edge_windows():
        result = {}
        @enum_window
        def collect(hwnd, unused):
            name = ctypes.create_unicode_buffer(64)
            title(hwnd, name, len(name))
            if name.value == "Jarvis Voice Aurora":
                rect = wt.RECT()
                assert get_rect(hwnd, ctypes.byref(rect))
                result[hwnd] = (rect.left, rect.top, rect.right, rect.bottom)
            return True
        assert enum_desktop(desktop, collect, 0)
        return result

    rectangles = []
    @enum_monitor
    def collect_monitor(monitor, dc, bounds, data):
        r = bounds.contents
        rectangles.append((r.left, r.top, r.right, r.bottom))
        return True

    previous_dpi = dpi(ctypes.c_void_p(-4))  # match physical monitor coordinates
    assert previous_dpi
    underneath = []
    edges = None
    try:
        assert monitors(None, None, collect_monitor, 0) and rectangles
        expected = set()
        for left, top, right, bottom in rectangles:
            hwnd = create(0, "BUTTON", "Private Aurora hit-test target", 0x90000000,
                          left, top, right-left, bottom-top, None, None, None, None)
            assert hwnd, ctypes.get_last_error()
            underneath.append(hwnd)
            l, t, r, b = left + 8, top + 8, right - 8, bottom - 8
            expected.update(((l, t, r, t + 7), (l, b - 7, r, b),
                             (l, t + 7, l + 7, b - 7), (r - 7, t + 7, r, b - 7)))
        pump(0.05)  # let Tk finish initial focus bookkeeping on the private desktop
        activate(underneath[0])
        original_active = active()
        assert original_active == underneath[0], "private test window did not activate"
        points = [wt.POINT((l+r)//2, (t+b)//2) for l, t, r, b in sorted(expected)]
        prior_hits = [locate(point) for point in points]
        assert all(hit in underneath for hit in prior_hits), "private hit-test target must cover each edge"
        prior_timers = root.tk.call("after", "info")
        edges = aurora.AuroraEdges(root)
        assert not edge_windows(), "construction displayed an edge before recording"
        get_dpi = bind(user, "GetThreadDpiAwarenessContext", wt.HANDLE)
        dpi_equal = bind(user, "AreDpiAwarenessContextsEqual", wt.BOOL, wt.HANDLE, wt.HANDLE)
        saved_dpi = dpi(ctypes.c_void_p(-1))
        try:
            edges.set_visible(True)
            assert dpi_equal(get_dpi(), ctypes.c_void_p(-1)), "renderer changed Tk's thread DPI context"
        finally:
            dpi(saved_dpi)
        pump(0.16)
        windows = edge_windows()
        assert len(windows) == len(expected) and set(windows.values()) == expected, (windows, expected)
        flags = 0x08000000 | 0x00000080 | 0x8 | 0x20 | 0x00080000  # noactivate/tool/topmost/transparent/layered
        for hwnd in windows:
            assert visible(hwnd) and styles(hwnd, -20) & flags == flags
        assert active() == original_active, "showing the indicator stole activation"
        assert [locate(point) for point in points] == prior_hits, "an edge intercepted hit testing"
        edges.set_visible(True)
        pump(0.08)
        assert edge_windows() == windows, "a repeated show duplicated or replaced native surfaces"
        edges.set_visible(False)
        pump(0.15)
        assert not edge_windows() and active() == original_active
        assert root.tk.call("after", "info") == prior_timers, "hidden edge retained a Tk timer"
        # Count actual paints of each off-screen DIB. No screen pixels are read.
        with patch.object(aurora._AuroraWin32, "animated", return_value=False), patch.object(
                aurora._AuroraStrip, "paint", autospec=True, side_effect=aurora._AuroraStrip.paint) as paints:
            edges.set_visible(True)
            pump(2.15)  # includes a full reduced-motion settings refresh
            assert set(edge_windows().values()) == expected
            assert paints.call_count == len(expected), "reduced motion repainted a static layout"
        tile = aurora._raster("top", 128, 7, 19)
        assert tile == aurora._raster("top", 128, 7, 19) != aurora._raster("top", 128, 7, 64)
        assert len(tile) == 128 * 7 * 4 and len(set(tile[3::4])) > 1
        assert all(max(tile[i:i+3]) <= tile[i+3] for i in range(0, len(tile), 4))
        edges.close()
        pump(0.15)
        assert not edge_windows() and active() == original_active
        assert root.tk.call("after", "info") == prior_timers, "closed edge retained a Tk timer"
        print(f"PASS: {len(rectangles)} monitor(s), {len(windows)} real inset edge windows, "
              "hit-test passthrough, original activation and DPI, static reduced motion, gradient, "
              "hide/reopen/close and timer cleanup.")
    finally:
        if edges is not None:
            edges.close()
        for hwnd in underneath:
            destroy(hwnd)
        dpi(previous_dpi)
        root.destroy()


if __name__ == "__main__":
    if sys.platform == "win32":
        probe()
    else:
        print("skipped: requires Windows")
