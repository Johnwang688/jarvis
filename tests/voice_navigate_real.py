"""Opt-in: `Actions.navigate` hands a URL to the foreground Chrome's own profile, offline.

From WSL run this script with --run. It uses the configured Windows desktop
bridge venv by default (export JARVIS_WINDOWS_PYTHON to the WSL path of another
Windows python.exe with comtypes to override).

Isolation, as in tests/voice_chrome_picker_real.py:
* a disposable Chrome user-data directory under %TEMP% with two made-up
  profiles (no owner profile is read, copied or opened), deleted afterwards;
* Chrome runs on a freshly created, never-switched-to desktop inside a
  kill-on-close job, so nothing appears on screen or takes the owner's focus;
* no site is visited: every request goes to a loopback proxy in this script
  that records the CONNECT target and answers 502, and the host resolver maps
  everything else to NOTFOUND. Background networking, sync and updates are off.
* the Windows default handler is never called (it would open the owner's
  default browser); the non-Chrome branch is covered by the Linux check.

The production `Actions.navigate`, `windows_command_line` and the picker's
Win32 `identity` run unchanged. Substituted: "foreground" (the hidden
desktop's windows, since GetForegroundWindow only reports the owner's input
desktop) and the launcher, which starts the exact argv `navigate` built, but
on the hidden desktop and inside the job (a forwarder on the owner's desktop
could not find the hidden Chrome's singleton window).

Observed per case: the argv (`--profile-directory`, `--single-argument`, URL),
the forwarder's exit code, the proxy's CONNECT log, every Chrome window's
title, and through UI Automation each window's tab names and address-bar text.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve()
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PROFILES = {"Default": "Alpha Probe", "Profile 1": "Beta Probe"}


def scenario():
    import ctypes
    import ctypes.wintypes as w
    import shutil
    import socket
    import tempfile
    import threading
    import time
    import uuid
    from types import SimpleNamespace
    from unittest.mock import Mock

    sys.path.insert(0, str(HERE.parents[1]))
    from windows import chromepicker, voiceactions
    from jarvis.v2.voice_intent import route
    from windows.voiceflow import Flow
    from windows.voicetype import navigation_target

    u, k = ctypes.windll.user32, ctypes.windll.kernel32

    class STARTUPINFO(ctypes.Structure):
        _fields_ = [("cb", w.DWORD), ("lpReserved", w.LPWSTR), ("lpDesktop", w.LPWSTR), ("lpTitle", w.LPWSTR),
                    ("dwX", w.DWORD), ("dwY", w.DWORD), ("dwXSize", w.DWORD), ("dwYSize", w.DWORD),
                    ("dwXCountChars", w.DWORD), ("dwYCountChars", w.DWORD), ("dwFillAttribute", w.DWORD),
                    ("dwFlags", w.DWORD), ("wShowWindow", w.WORD), ("cbReserved2", w.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", w.HANDLE), ("hStdOutput", w.HANDLE),
                    ("hStdError", w.HANDLE)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", w.HANDLE), ("hThread", w.HANDLE), ("dwProcessId", w.DWORD), ("dwThreadId", w.DWORD)]

    class BASIC_LIMITS(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", w.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]

    class EXTENDED_LIMITS(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC_LIMITS), ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    u.CreateDesktopW.restype = w.HANDLE
    u.CreateDesktopW.argtypes = [w.LPCWSTR, w.LPCWSTR, ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.c_void_p]
    u.SetThreadDesktop.argtypes = [w.HANDLE]
    u.CloseDesktop.argtypes = [w.HANDLE]
    u.GetForegroundWindow.restype = w.HWND
    u.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    u.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    u.IsWindowVisible.argtypes = [w.HWND]
    k.CreateJobObjectW.restype = w.HANDLE
    k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    k.ResumeThread.argtypes = [w.HANDLE]
    k.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
    k.CloseHandle.argtypes = [w.HANDLE]
    k.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
    enum_proc = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    u.EnumDesktopWindows.argtypes = [w.HANDLE, enum_proc, w.LPARAM]

    report = {"cases": {}}
    owner_before = u.GetForegroundWindow()
    desk_name = "JarvisNavigateProbe-" + uuid.uuid4().hex[:10]
    desk = u.CreateDesktopW(desk_name, None, None, 0, 0x10000000, None)  # GENERIC_ALL
    if not desk:
        raise OSError("CreateDesktop failed")
    job = k.CreateJobObjectW(None, None)
    limits = EXTENDED_LIMITS()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not job or not k.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        if job:
            k.CloseHandle(job)
        u.CloseDesktop(desk)
        raise OSError("Could not configure isolated Chrome process cleanup")
    data = tempfile.mkdtemp(prefix="jarvis-navigate-probe-")
    now = time.time()
    for folder in PROFILES:
        os.makedirs(os.path.join(data, folder))
    with open(os.path.join(data, "Local State"), "w", encoding="utf-8") as handle:
        json.dump({"profile": {"info_cache": {folder: {"name": name, "active_time": now, "is_using_default_name": False,
                                                       "avatar_icon": "chrome://theme/IDR_PROFILE_AVATAR_26"}
                                              for folder, name in PROFILES.items()},
                               "last_used": "Default", "profiles_order": list(PROFILES), "picker_shown": True},
                   "browser": {"show_profile_picker_on_startup": False}}, handle)

    # -- the loopback proxy: the only network Chrome can reach ------------------
    proxy_log, proxy_stop = [], threading.Event()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    listener.settimeout(0.2)
    proxy_port = listener.getsockname()[1]

    def proxy():
        while not proxy_stop.is_set():
            try:
                conn, unused = listener.accept()
            except OSError:
                continue
            with conn:
                conn.settimeout(2)
                try:
                    line = conn.recv(4096).split(b"\r\n", 1)[0].decode("latin-1")
                    proxy_log.append(line[:200])
                    conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                except OSError:
                    pass

    proxy_thread = threading.Thread(target=proxy, name="navigate-probe-proxy", daemon=True)
    proxy_thread.start()

    base_flags = [f"--user-data-dir={data}", "--no-first-run", "--no-default-browser-check", "--disable-sync",
                  "--disable-background-networking", "--disable-component-update", "--no-pings", "--disable-gpu",
                  f"--proxy-server=http://127.0.0.1:{proxy_port}",
                  "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1"]

    def enter_desktop():
        if not u.SetThreadDesktop(desk):
            raise OSError("SetThreadDesktop failed")

    def text_of(hwnd):
        buffer = ctypes.create_unicode_buffer(512)
        u.GetWindowTextW(hwnd, buffer, 512)
        return buffer.value

    def chrome_windows():
        found = []

        @enum_proc
        def visit(hwnd, unused):
            name = ctypes.create_unicode_buffer(64)
            u.GetClassNameW(hwnd, name, 64)
            if u.IsWindowVisible(hwnd) and name.value == chromepicker.PICKER_CLASS and text_of(hwnd):
                found.append(int(hwnd))
            return True

        u.EnumDesktopWindows(desk, visit, 0)
        return found

    class Process:
        def __init__(self, handle, pid):
            self.handle, self.pid = handle, pid

        def poll(self):
            code = w.DWORD()
            if not k.GetExitCodeProcess(self.handle, ctypes.byref(code)):
                raise OSError("GetExitCodeProcess failed")
            return None if code.value == 259 else code.value  # STILL_ACTIVE

        def close(self):
            k.CloseHandle(self.handle)

    processes = []

    def start(argv):
        startup = STARTUPINFO()
        startup.cb = ctypes.sizeof(startup)
        startup.lpDesktop = "WinSta0\\" + desk_name
        info = PROCESS_INFORMATION()
        command = subprocess.list2cmdline(argv)
        if not k.CreateProcessW(None, ctypes.create_unicode_buffer(command), None, None, False,
                                0x4, None, None, ctypes.byref(startup), ctypes.byref(info)):  # CREATE_SUSPENDED
            raise OSError("CreateProcess failed")
        try:
            if not k.AssignProcessToJobObject(job, info.hProcess):
                k.TerminateProcess(info.hProcess, 1)
                raise OSError("Could not attach the suspended probe to its job")
            if k.ResumeThread(info.hThread) == -1:
                raise OSError("Could not resume disposable Chrome")
        finally:
            k.CloseHandle(info.hThread)
        process = Process(info.hProcess, info.dwProcessId)
        processes.append(process)
        return process

    def wait_for(predicate, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.1)
        return predicate()

    def body():
        enter_desktop()
        import comtypes
        import comtypes.client
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
        except OSError:
            pass
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA
        uia = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)

        def browser_view(hwnd):
            """Tab names and address-bar text, read from Chrome's native views only."""
            root = uia.ElementFromHandle(hwnd)
            tabs = root.FindAll(UIA.TreeScope_Descendants,
                                uia.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId, UIA.UIA_TabItemControlTypeId))
            names = [tabs.GetElement(i).CurrentName for i in range(tabs.Length)]
            edits = root.FindAll(UIA.TreeScope_Descendants,
                                 uia.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId, UIA.UIA_EditControlTypeId))
            address = None
            for i in range(edits.Length):
                edit = edits.GetElement(i)
                if edit.CurrentFrameworkId == "Chrome" and edit.CurrentClassName == "OmniboxViewViews":
                    pattern = edit.GetCurrentPattern(UIA.UIA_ValuePatternId)
                    if pattern:
                        address = pattern.QueryInterface(UIA.IUIAutomationValuePattern).CurrentValue
            return {"title": text_of(hwnd), "tabs": names, "address": address}

        picker = chromepicker.WindowsChromePicker(foreground=lambda: override.get("fg"), thread_setup=enter_desktop)
        override = {}

        def foreground_window():
            hwnd = override.get("fg")
            identity = picker.identity(hwnd) if hwnd else None
            return (identity.hwnd, identity.title, identity.exe) if identity else None

        launches = []

        def launcher(argv, **unused):
            launches.append(list(argv))
            return start(argv)

        actions = voiceactions.Actions(SimpleNamespace(foreground_window=foreground_window),
                                       runner=lambda *a, **kw: SimpleNamespace(returncode=1, stdout=""),
                                       launcher=launcher, opener=lambda *a: report.setdefault("opener", []).append(a),
                                       picker=picker)

        # Profile 1 starts the browser; Default joins it (that start is itself a forwarder).
        first = start([CHROME, *base_flags, "--profile-directory=Profile 1", "chrome://newtab/"])
        beta = wait_for(lambda: [h for h in chrome_windows() if text_of(h).startswith("New Tab")], 30)
        if len(beta) != 1:
            report["setup"] = "no Profile 1 window"
            return
        beta = beta[0]
        joiner = start([CHROME, *base_flags, "--profile-directory=Default", "chrome://newtab/"])
        alpha = wait_for(lambda: [h for h in chrome_windows() if h != beta and text_of(h).startswith("New Tab")], 30)
        report["joiner_exit"] = wait_for(joiner.poll, 15)
        report["browser_alive"] = first.poll() is None
        if len(alpha) != 1:
            report["setup"] = "no Default window"
            return
        alpha = alpha[0]
        ids = {"beta": picker.identity(beta), "alpha": picker.identity(alpha)}
        report["aumids"] = {name: i.aumid.split(".")[0] + ".<dir>." + i.aumid.split(".")[-1] for name, i in ids.items()}
        wait_for(lambda: all(browser_view(h)["address"] is not None for h in (alpha, beta)), 10)
        before = {"alpha": browser_view(alpha), "beta": browser_view(beta)}
        report["before"] = json.loads(json.dumps(before))
        report["setup_proxy"] = list(proxy_log)

        def case(name, hwnd, url, expect_host):
            override["fg"] = hwnd
            proxy_mark, launch_mark = len(proxy_log), len(launches)
            started = time.monotonic()
            typer, ui = Mock(), Mock()
            source = "hotkey" if hwnd == beta else "wake"
            client = SimpleNamespace(route=route)
            Flow(client, typer, actions, ui, None).on_clip(None, source, transcript="Go to " + expect_host)
            typer.run.assert_not_called()
            result = ui.set_state.call_args.args == ("done", "Sent the website to the browser.")
            row = {"result": result, "seconds": round(time.monotonic() - started, 2),
                   "argv": [a if not a.startswith("--user-data-dir=") else "--user-data-dir=<disposable>"
                            for a in (launches[launch_mark][1:] if len(launches) > launch_mark else [])],
                   "forwarder_exit": processes[-1].poll() if len(launches) > launch_mark else None}
            target = "alpha" if hwnd == alpha else "beta"
            other = "beta" if target == "alpha" else "alpha"
            handle = {"alpha": alpha, "beta": beta}
            wait_for(lambda: len(browser_view(hwnd)["tabs"]) > len(before[target]["tabs"])
                     and expect_host in (browser_view(hwnd)["address"] or ""), 10)
            wait_for(lambda: any(expect_host in line for line in proxy_log[proxy_mark:]), 5)
            after = {key: browser_view(handle[key]) for key in handle}
            row["after"] = after
            row["proxy"] = proxy_log[proxy_mark:]
            row["ok"] = bool(
                result is True and row["forwarder_exit"] in actions.CHROME_HANDOFF_EXIT_CODES
                and len(launches) == launch_mark + 1
                and row["argv"][-2:] == ["--single-argument", url]
                and f"--profile-directory={'Default' if target == 'alpha' else 'Profile 1'}" in row["argv"]
                and len(after[target]["tabs"]) == len(before[target]["tabs"]) + 1
                and expect_host in (after[target]["address"] or "")
                and after[target]["title"].startswith(expect_host)
                and after[other]["tabs"] == before[other]["tabs"] and after[other]["title"] == before[other]["title"]
                and after[target]["tabs"][-1].startswith(expect_host)
                # The first request after the handoff is the site itself; a typed
                # search would have gone to the search engine first and shown it.
                and row["proxy"][:1] == [f"CONNECT {expect_host}:443 HTTP/1.1"]
                and after[target]["address"] == expect_host)
            before[target], before[other] = after[target], after[other]
            report["cases"][name] = row

        # The Default window opened last, so Default is Chrome's last-used
        # profile: going to Profile 1 shows the window's own profile decides.
        url = navigation_target("Go to youtube.com", "hotkey")
        report["routed_url"] = url
        case("profile1_window_not_last_used", beta, url, "youtube.com")
        case("profile1_after_error", beta, "https://example.edu", "example.edu")
        case("default_window", alpha, "https://wikipedia.org", "wikipedia.org")

        # Revoked before the launch: nothing starts.
        override["fg"] = beta
        mark = len(launches)
        with voiceactions.action_scope(lambda: False):
            revoked = actions.navigate("https://example.org")
        report["cases"]["revoked"] = {"result": revoked, "launched": len(launches) - mark,
                                      "ok": revoked is False and len(launches) == mark}
        # A non-canonical value (would be a typed search in an address bar): nothing starts.
        with voiceactions.action_scope(lambda: True):
            bad = [actions.navigate(v) for v in ("go to youtube.com", "youtube.com", "https://youtube.com/watch?v=1",
                                                 "file:///C:/Windows", "https://localhost")]
        report["cases"]["not_canonical"] = {"results": bad, "ok": bad == [False] * 5 and len(launches) == mark}

        # Incognito: same AppUserModelID as its profile (measured below), so
        # record what a handoff from it would do rather than assume.
        incog = start([CHROME, *base_flags, "--profile-directory=Profile 1", "--incognito", "chrome://newtab/"])
        known = {alpha, beta}
        found = wait_for(lambda: [h for h in chrome_windows() if h not in known], 15)
        report["incognito_forwarder_exit"] = wait_for(incog.poll, 10)
        if found:
            identity = picker.identity(found[0])
            report["incognito_window"] = {"title": identity.title, "aumid_same_as_profile1":
                                          identity.aumid == ids["beta"].aumid,
                                          "view": browser_view(found[0])}
            override["fg"] = found[0]
            mark = len(launches)
            with voiceactions.action_scope(lambda: True):
                result = actions.navigate("https://example.com")
            report["cases"]["incognito_refused"] = {"result": result, "launched": len(launches) - mark,
                                                    "ok": result is False and len(launches) == mark}
        # Regular-window names as production reads them (timed), for the record.
        for name, hwnd in (("alpha", alpha), ("beta", beta)):
            started = time.monotonic()
            names = picker.worker.call(lambda client, UIA: voiceactions.browser_window_names(client, UIA, hwnd), 5)[1]
            report.setdefault("window_names", {})[name] = {
                "names": names, "regular": voiceactions.regular_browser_window(names),
                "seconds": round(time.monotonic() - started, 2)}
        report["launches"] = len(launches)
        report["opener_called"] = "opener" in report

    errors = []

    def run():
        try:
            body()
        except Exception as exc:  # noqa: BLE001
            import traceback
            errors.append(traceback.format_exc()[-1500:])

    try:
        worker = threading.Thread(target=run, name="navigate-probe")
        worker.start()
        worker.join(180)
        report["finished"] = not worker.is_alive()
    finally:
        k.CloseHandle(job)  # kills every disposable Chrome process
        for process in processes:
            process.close()
        proxy_stop.set()
        proxy_thread.join(2)
        listener.close()
        u.CloseDesktop(desk)
        for unused in range(20):  # crashpad may hold files for a moment after the kill
            shutil.rmtree(data, ignore_errors=True)
            if not os.path.exists(data):
                break
            time.sleep(0.5)
    report["errors"] = errors
    report["profile_dir_removed"] = not os.path.exists(data)
    report["owner_foreground_unchanged"] = u.GetForegroundWindow() == owner_before  # informational
    cases = report["cases"]
    report["ok"] = bool(
        not errors and report.get("finished") and report.get("routed_url") == "https://youtube.com"
        and all(cases.get(name, {}).get("ok") for name in
                ("profile1_window_not_last_used", "profile1_after_error", "default_window", "revoked", "not_canonical",
                 "incognito_refused"))
        and not report.get("opener_called") and report["profile_dir_removed"])
    return report


def windows_worker():
    report = scenario()
    print(json.dumps(report, ensure_ascii=True, indent=1), flush=True)
    return int(not report["ok"])


def check():
    native = os.environ.get("JARVIS_WINDOWS_PYTHON")
    if not native:
        sys.path.insert(0, str(HERE.parents[1]))
        from jarvis import config
        bridge = subprocess.check_output(["wslpath", "-u", config.DESKTOP_BRIDGE_DIR], text=True).strip()
        native = str(Path(bridge) / "venv" / "Scripts" / "python.exe")
    script = subprocess.check_output(["wslpath", "-w", str(HERE)], text=True).strip()
    return subprocess.run([native, "-X", "utf8", script, "--windows-worker"], timeout=400).returncode


if __name__ == "__main__":
    if sys.argv[1:] == ["--windows-worker"]:
        raise SystemExit(windows_worker())
    elif sys.argv[1:] == ["--run"]:
        raise SystemExit(check())
    else:
        print("skipped: use --run on the WSL/Windows machine with Chrome installed")
