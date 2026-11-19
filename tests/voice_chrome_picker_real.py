"""Opt-in: the real Chrome profile picker, chosen by voice actions, on a hidden desktop.

From WSL run this script with --run. It uses the configured Windows desktop
bridge venv by default (export JARVIS_WINDOWS_PYTHON to the WSL path of another
Windows python.exe with comtypes to override).

Isolation, so the owner's session is never touched:
* a disposable Chrome user-data directory under %TEMP% with made-up profiles
  (no owner profile is read, copied or opened), deleted afterwards;
* Chrome runs on a freshly created, never-switched-to desktop, so its windows
  cannot appear on screen or take the owner's focus, and every Invoke lands
  there; all its processes sit in a kill-on-close job object;
* background networking, sync, component updates and first-run UI are off.

The production `Actions` and `WindowsChromePicker` run unchanged; only the
launcher (which starts that disposable Chrome instead of explorer.exe) and
"foreground" (the hidden desktop's active window, since GetForegroundWindow
only reports the owner's input desktop) are substituted.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve()
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PROFILES = {"Default": "Alpha Probe", "Profile 1": "Beta Probe", "Profile 2": "Twin Probe", "Profile 3": "twin probe"}


def scenario(choice, expected_last):
    import ctypes
    import ctypes.wintypes as w
    import shutil
    import tempfile
    import threading
    import time
    import uuid
    from types import SimpleNamespace

    from unittest.mock import Mock
    sys.path.insert(0, str(HERE.parents[1]))
    from jarvis.v2.voice_intent import route
    from windows import chromepicker, voiceactions
    from windows.voiceflow import Flow

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

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [("cbSize", w.DWORD), ("flags", w.DWORD), ("hwndActive", w.HWND), ("hwndFocus", w.HWND),
                    ("hwndCapture", w.HWND), ("hwndMenuOwner", w.HWND), ("hwndMoveSize", w.HWND),
                    ("hwndCaret", w.HWND), ("rcCaret", w.RECT)]

    u.CreateDesktopW.restype = w.HANDLE
    u.CreateDesktopW.argtypes = [w.LPCWSTR, w.LPCWSTR, ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.c_void_p]
    u.SetThreadDesktop.argtypes = [w.HANDLE]
    u.CloseDesktop.argtypes = [w.HANDLE]
    u.GetForegroundWindow.restype = w.HWND
    k.CreateJobObjectW.restype = w.HANDLE
    k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    k.ResumeThread.argtypes = [w.HANDLE]
    k.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
    k.CloseHandle.argtypes = [w.HANDLE]
    enum_proc = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    u.EnumDesktopWindows.argtypes = [w.HANDLE, enum_proc, w.LPARAM]
    u.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
    u.GetWindowThreadProcessId.restype = w.DWORD
    u.GetGUIThreadInfo.argtypes = [w.DWORD, ctypes.POINTER(GUITHREADINFO)]

    report = {"choice": choice}
    owner_before = u.GetForegroundWindow()  # this thread stays on the owner's desktop
    desk_name = "JarvisPickerProbe-" + uuid.uuid4().hex[:10]
    desk = u.CreateDesktopW(desk_name, None, None, 0, 0x10000000, None)  # GENERIC_ALL
    if not desk:
        raise OSError(ctypes.get_last_error() or "CreateDesktop failed")
    job = k.CreateJobObjectW(None, None)
    limits = EXTENDED_LIMITS()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not job or not k.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        if job:
            k.CloseHandle(job)
        u.CloseDesktop(desk)
        raise OSError("Could not configure isolated Chrome process cleanup")
    data = tempfile.mkdtemp(prefix="jarvis-picker-probe-")
    now = time.time()
    for folder in PROFILES:
        os.makedirs(os.path.join(data, folder))
    with open(os.path.join(data, "Local State"), "w", encoding="utf-8") as handle:
        json.dump({"profile": {"info_cache": {folder: {"name": name, "active_time": now, "is_using_default_name": False,
                                                       "avatar_icon": "chrome://theme/IDR_PROFILE_AVATAR_26"}
                                              for folder, name in PROFILES.items()},
                               "last_used": "Default", "profiles_order": list(PROFILES), "picker_shown": True},
                   "browser": {"show_profile_picker_on_startup": True}}, handle)

    def enter_desktop():
        if not u.SetThreadDesktop(desk):
            raise OSError("SetThreadDesktop failed")

    def chrome_windows():
        found = []

        @enum_proc
        def visit(hwnd, unused):
            name = ctypes.create_unicode_buffer(64)
            u.GetClassNameW(hwnd, name, 64)
            if u.IsWindowVisible(hwnd) and name.value == chromepicker.PICKER_CLASS:
                found.append(int(hwnd))
            return True

        u.EnumDesktopWindows(desk, visit, 0)
        return found

    override = {}

    def hidden_foreground():
        if "fg" in override:
            return override["fg"]
        for hwnd in chrome_windows():
            info = GUITHREADINFO()
            info.cbSize = ctypes.sizeof(info)
            if u.GetGUIThreadInfo(u.GetWindowThreadProcessId(hwnd, None), ctypes.byref(info)) and info.hwndActive:
                return int(info.hwndActive)
        return None

    launches = []

    def launch(argv, **unused):
        if argv != ["explorer.exe", "shell:AppsFolder\\chrome-probe-id"]:
            raise AssertionError(argv)
        startup = STARTUPINFO()
        startup.cb = ctypes.sizeof(startup)
        startup.lpDesktop = "WinSta0\\" + desk_name
        info = PROCESS_INFORMATION()
        command = (f'"{CHROME}" --user-data-dir="{data}" --no-first-run --no-default-browser-check --disable-sync '
                   "--disable-background-networking --disable-component-update --no-pings --disable-gpu")
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
            k.CloseHandle(info.hProcess)
        launches.append(argv)

    def body():
        enter_desktop()
        picker = chromepicker.WindowsChromePicker(foreground=hidden_foreground, thread_setup=enter_desktop)

        trace, real_identity = [], picker.identity

        def traced_identity(hwnd):
            identity = real_identity(hwnd)
            row = (hwnd, identity and (identity.class_name, identity.title[:40], identity.aumid, identity.pid,
                                       identity.visible))
            if not trace or trace[-1][1:] != row:
                trace.append((round(time.monotonic(), 2),) + row)
            return identity
        picker.identity = traced_identity
        report["trace"] = trace

        def foreground_window():
            hwnd = hidden_foreground()
            identity = picker.identity(hwnd) if hwnd else None
            return (identity.hwnd, identity.title, identity.exe) if identity else None

        index = json.dumps([{"Name": "Google Chrome", "AppID": "chrome-probe-id"}])
        class ProbeActions(voiceactions.Actions):
            def open_app(self, *args, **kwargs):
                status = super().open_app(*args, **kwargs)
                report["open"] = status.name
                return status

            def choose_chrome_profile(self, name):
                status = super().choose_chrome_profile(name)
                self.last_choice = status
                return status

        actions = ProbeActions(SimpleNamespace(foreground_window=foreground_window),
                                       runner=lambda *a, **kw: SimpleNamespace(returncode=0, stdout=index),
                                       launcher=launch, picker=picker)
        client, typer, ui, player = Mock(), Mock(), Mock(), Mock()
        client.action_receipts = True
        client.route.side_effect = lambda words, source, **kw: dict(route(words, source), receipt="probe")
        player.cancelled = threading.Event()
        source = "hotkey" if expected_last == "Default" else "wake"
        Flow(client, typer, actions, ui, player).on_clip(None, source, transcript="Open Google Chrome.")
        client.confirm_action.assert_not_called()
        context = actions.picker_context
        report["picker_trusted"] = context is not None
        if context is None:
            seen = [picker.identity(hwnd) for hwnd in chrome_windows()]
            report["untrusted_windows"] = [dict(cls=i.class_name, exe=os.path.basename(i.exe), aumid=i.aumid,
                                                visible=i.visible, title=i.title) for i in seen if i]
            return
        recorded = context[0]
        report["picker_aumid_shape"] = recorded.aumid.split(".")[0] + ".*." + recorded.aumid.split(".")[-1]
        started = time.monotonic()
        report["options"] = list(actions.chrome_profile_options())
        report["options_seconds"] = round(time.monotonic() - started, 2)
        report["duplicate"] = actions.choose_chrome_profile("Twin Probe").name
        report["after_duplicate_picker_open"] = picker.identity(recorded.hwnd) is not None
        override["fg"] = 0  # the owner's focus moved away from the picker
        report["focus_moved"] = actions.choose_chrome_profile(choice).name
        del override["fg"]
        report["after_focus_picker_open"] = picker.identity(recorded.hwnd) is not None
        started = time.monotonic()
        Flow(client, typer, actions, ui, player).on_clip(None, source, transcript=choice)
        client.confirm_action.assert_called_once_with("probe")
        client.route.assert_called_once()
        typer.run.assert_not_called()
        report["chosen"] = actions.last_choice.name
        report["voice_source"] = source
        report["choose_seconds"] = round(time.monotonic() - started, 2)
        # Chrome can hide the picker as the new browser takes focus, then
        # destroy its HWND on a later UI tick. Verify eventual destruction.
        close_deadline = time.monotonic() + 3
        while picker.identity(recorded.hwnd) is not None and time.monotonic() < close_deadline:
            time.sleep(0.05)
        report["picker_closed"] = picker.identity(recorded.hwnd) is None
        fresh = [picker.identity(hwnd) for hwnd in chrome_windows()]
        # Measured with a non-default directory: `Chrome.<dir>.Default` / `Chrome.<dir>.Profile1`
        # (Chrome shortens <dir> differently than in the picker's own ID).
        report["profile_window"] = [
            i.title.endswith(" - Google Chrome") and i.pid == recorded.pid
            and len(i.aumid.split(".")) == 3 and i.aumid.split(".")[-1] == expected_last
            for i in fresh if i and i.aumid]
        report["retry"] = actions.choose_chrome_profile(choice).name
        report["options_after"] = list(actions.chrome_profile_options())

    errors = []

    def run():
        try:
            body()
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    try:
        worker = threading.Thread(target=run, name="picker-probe")
        worker.start()
        worker.join(120)
        report["finished"] = not worker.is_alive()
    finally:
        k.CloseHandle(job)  # kills every disposable Chrome process
        u.CloseDesktop(desk)
        for unused in range(20):  # crashpad may hold files for a moment after the kill
            shutil.rmtree(data, ignore_errors=True)
            if not os.path.exists(data):
                break
            time.sleep(0.5)
    report["errors"] = errors
    report["launches"] = len(launches)
    report["profile_dir_removed"] = not os.path.exists(data)
    report["owner_foreground_unchanged"] = u.GetForegroundWindow() == owner_before  # informational
    expected_options = sorted(PROFILES.values())
    report["ok"] = bool(
        not errors and report.get("finished") and report.get("open") == "CHROME_PROFILE_REQUIRED"
        and report.get("picker_trusted") and sorted(report.get("options", [])) == expected_options
        and report.get("duplicate") == "CHROME_PROFILE_REQUIRED" and report.get("after_duplicate_picker_open")
        and report.get("focus_moved") == "CHROME_WINDOW_UNCONFIRMED" and report.get("after_focus_picker_open")
        and report.get("chosen") == "LAUNCHED" and report.get("picker_closed")
        and report.get("profile_window") == [True] and report.get("retry") == "CHROME_WINDOW_UNCONFIRMED"
        and report.get("options_after") == [] and report["launches"] == 1 and report["profile_dir_removed"])
    return report


def windows_worker():
    failures = 0
    for choice, last in (("beta probe.", "Profile1"), ("Alpha Probe", "Default")):
        report = scenario(choice, last)
        if report["ok"]:
            report.pop("trace", None)
        failures += int(not report["ok"])
        print(json.dumps(report, ensure_ascii=True), flush=True)
    return int(bool(failures))


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
