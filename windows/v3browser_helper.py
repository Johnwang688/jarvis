"""Relay line-delimited JSON to a Chromium browser's private CDP pipes."""

import argparse
from contextlib import ExitStack, suppress
import io
import json
import os
from pathlib import Path, PureWindowsPath
from queue import Empty, Queue
import signal
import subprocess
import sys
from threading import Thread
import time


MAX_REQUEST = 16 * 1024 * 1024
MAX_MESSAGE = 64 * 1024 * 1024
# A browser that is closing by itself gets this long to exit, and its other
# processes this long after that, before anything is killed. Killing it the
# moment its pipe closes loses what it was about to save (measured: a cookie
# set seconds before `Browser.close` was gone on the next start).
EXIT_WAIT = 5.0
SAVE_WAIT = 3.0
_QUIET = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class WindowsAdapter:
    def candidates(self, browser, env=os.environ):
        suffix = {
            "chrome": r"Google\Chrome\Application\chrome.exe",
            "edge": r"Microsoft\Edge\Application\msedge.exe",
        }[browser]
        locations = ["ProgramFiles", "ProgramFiles(x86)"]
        if browser == "chrome":
            locations.append("LocalAppData")
        for name in locations:
            root = PureWindowsPath(env.get(name, ""))
            if root.is_absolute():
                yield root / suffix

    def pipe_flags(self, read, write):
        return [f"--remote-debugging-io-pipes={read},{write}"]

    def _join_job(self):
        import ctypes as c
        from ctypes import wintypes as w
        import _winapi

        class BasicLimits(c.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", c.c_longlong),
                ("PerJobUserTimeLimit", c.c_longlong),
                ("LimitFlags", w.DWORD),
                ("MinimumWorkingSetSize", c.c_size_t),
                ("MaximumWorkingSetSize", c.c_size_t),
                ("ActiveProcessLimit", w.DWORD),
                ("Affinity", c.c_size_t),
                ("PriorityClass", w.DWORD),
                ("SchedulingClass", w.DWORD),
            ]

        class IOCounters(c.Structure):
            _fields_ = [(name, c.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class ExtendedLimits(c.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits), ("IoInfo", IOCounters),
                ("ProcessMemoryLimit", c.c_size_t), ("JobMemoryLimit", c.c_size_t),
                ("PeakProcessMemoryUsed", c.c_size_t), ("PeakJobMemoryUsed", c.c_size_t),
            ]

        kernel = c.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        kernel.CreateJobObjectW.restype = w.HANDLE
        kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        kernel.SetInformationJobObject.restype = w.BOOL
        kernel.GetCurrentProcess.argtypes = []
        kernel.GetCurrentProcess.restype = w.HANDLE
        kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        kernel.AssignProcessToJobObject.restype = w.BOOL

        kill_on_close = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        extended_limit_information = 9  # JobObjectExtendedLimitInformation
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = kill_on_close
        job = kernel.CreateJobObjectW(None, None)
        if not job:
            raise c.WinError(c.get_last_error())
        if (not kernel.SetInformationJobObject(job, extended_limit_information,
                                               c.byref(limits), c.sizeof(limits))
                or not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess())):
            error = c.get_last_error()
            _winapi.CloseHandle(job)
            raise c.WinError(error)
        # The helper joins before creating the browser, which inherits the job
        # at creation. Keep this non-inheritable handle open until process exit.
        self._job = job

    def launch(self, executable, args):
        import _winapi
        import msvcrt

        self._join_job()
        with ExitStack() as browser_ends, ExitStack() as helper_ends, ExitStack() as streams:
            browser_read, to_browser = _winapi.CreatePipe(None, 0)
            browser_ends.callback(_winapi.CloseHandle, browser_read)
            helper_ends.callback(_winapi.CloseHandle, to_browser)
            from_browser, browser_write = _winapi.CreatePipe(None, 0)
            helper_ends.callback(_winapi.CloseHandle, from_browser)
            browser_ends.callback(_winapi.CloseHandle, browser_write)
            os.set_handle_inheritable(browser_read, True)
            os.set_handle_inheritable(browser_write, True)

            def wrap(handle, flags, mode):
                try:
                    fd = msvcrt.open_osfhandle(handle, flags | os.O_BINARY)
                except BaseException:
                    _winapi.CloseHandle(handle)
                    raise
                # The CRT owns this handle now.
                try:
                    return streams.enter_context(os.fdopen(fd, mode, buffering=0))
                except BaseException:
                    os.close(fd)
                    raise

            # Transfer the two independent handles one at a time.
            helper_ends.pop_all()
            try:
                writer = wrap(to_browser, os.O_WRONLY, "wb")
            except BaseException:
                _winapi.CloseHandle(from_browser)
                raise
            reader = wrap(from_browser, os.O_RDONLY, "rb")

            startup = subprocess.STARTUPINFO()
            startup.lpAttributeList = {"handle_list": [browser_read, browser_write]}
            command = [executable, *_browser_args(args, self.pipe_flags(browser_read, browser_write))]
            process = subprocess.Popen(command, executable=executable, startupinfo=startup,
                                       close_fds=True, **_QUIET)
            streams.pop_all()
        return process, reader, writer

    def _others_in_job(self):
        import ctypes as c
        from ctypes import wintypes as w

        class Accounting(c.Structure):
            _fields_ = [
                ("TotalUserTime", c.c_longlong), ("TotalKernelTime", c.c_longlong),
                ("ThisPeriodTotalUserTime", c.c_longlong), ("ThisPeriodTotalKernelTime", c.c_longlong),
                ("TotalPageFaultCount", w.DWORD), ("TotalProcesses", w.DWORD),
                ("ActiveProcesses", w.DWORD), ("TotalTerminatedProcesses", w.DWORD),
            ]

        kernel = c.WinDLL("kernel32", use_last_error=True)
        kernel.QueryInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p]
        kernel.QueryInformationJobObject.restype = w.BOOL
        info = Accounting()
        basic_accounting_information = 1  # JobObjectBasicAccountingInformation
        if not kernel.QueryInformationJobObject(self._job, basic_accounting_information,
                                                c.byref(info), c.sizeof(info), None):
            raise OSError("job query failed")
        return info.ActiveProcesses - 1  # the helper itself is a member

    def settle(self, process):
        """Let a browser that is closing by itself finish saving its profile."""
        with suppress(subprocess.TimeoutExpired):
            process.wait(EXIT_WAIT)
        deadline = time.monotonic() + SAVE_WAIT
        while time.monotonic() < deadline:
            try:
                if self._others_in_job() <= 0:
                    return
            except OSError:
                pass  # cannot tell: use the whole wait
            time.sleep(0.05)

    def stop(self, process):
        process.kill()
        process.wait()


class PosixAdapter:
    """Linux test adapter for Chromium's POSIX fd 3/4 convention."""

    # Run the fd setup in a fresh interpreter so Popen needs no preexec_fn.
    _EXEC = """
import ctypes
import fcntl
import os
import signal
import sys

parent, read, write = map(int, sys.argv[1:4])
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(1, signal.SIGKILL, 0, 0, 0) or os.getppid() != parent:
    sys.exit(1)
copies = [fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 5) for fd in (read, write)]
os.close(read)
os.close(write)
for fd, target in zip(copies, (3, 4)):
    os.dup2(fd, target)
    os.close(fd)
os.execv(sys.argv[4], sys.argv[4:])
"""

    def candidates(self, browser, env=os.environ):
        # Tests supply an explicit fake executable. Never search installed browsers.
        return ()

    def pipe_flags(self, read, write):
        return []

    def launch(self, executable, args):
        with ExitStack() as browser_ends, ExitStack() as streams:
            browser_read, to_browser = os.pipe()
            browser_ends.callback(os.close, browser_read)
            writer = streams.enter_context(os.fdopen(to_browser, "wb", buffering=0))
            from_browser, browser_write = os.pipe()
            browser_ends.callback(os.close, browser_write)
            reader = streams.enter_context(os.fdopen(from_browser, "rb", buffering=0))
            command = [sys.executable, "-c", self._EXEC, str(os.getpid()),
                       str(browser_read), str(browser_write), executable,
                       *_browser_args(args, self.pipe_flags(browser_read, browser_write))]
            process = subprocess.Popen(command, pass_fds=(browser_read, browser_write),
                                       start_new_session=True, **_QUIET)
            streams.pop_all()
        return process, reader, writer

    def settle(self, process):
        with suppress(subprocess.TimeoutExpired):
            process.wait(EXIT_WAIT)

    def stop(self, process):
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


class BadArguments(ValueError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise BadArguments


def _arguments(argv):
    parser = Parser(add_help=False, allow_abbrev=False)
    parser.add_argument("--browser", required=True, choices=("chrome", "edge"))
    parser.add_argument("--profile-dir", required=True, type=Path)
    parser.add_argument("--proxy-port", required=True, type=int)
    parser.add_argument("--window", action="store_true")
    args = parser.parse_args(argv)
    if (not args.profile_dir.is_absolute() or not 1 <= args.proxy_port <= 65535
            or any(char in str(args.profile_dir) for char in '"\x00\r\n')):
        raise BadArguments
    return args


def _browser_args(args, pipe_flags):
    flags = [
        f"--user-data-dir={args.profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--remote-debugging-pipe",
        *pipe_flags,
        f"--proxy-server=socks5://127.0.0.1:{args.proxy_port}",
        "--proxy-bypass-list=<-loopback>",
        "--disable-quic",
        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-default-apps",
        "--no-pings",
        "--disable-features=MediaRouter,DialMediaRouteProvider,CastMediaRouteProvider,"
        "WebRtcHideLocalIpsWithMdns",
    ]
    if not args.window:
        flags.append("--headless=new")
    return [*flags, "about:blank"]


def _write(stream, data):
    remaining = memoryview(data)
    while remaining:
        count = stream.write(remaining)
        if not count:
            raise OSError("Pipe write failed")
        remaining = remaining[count:]
    stream.flush()


def _invalid_constant(_):
    raise ValueError


def _to_browser(source, target):
    while line := source.readline(MAX_REQUEST + 2):
        if not line.endswith(b"\n") or len(line) > MAX_REQUEST + 1:
            raise ValueError
        message = line[:-1]
        if not isinstance(json.loads(message.decode("utf-8"), parse_constant=_invalid_constant), dict):
            raise ValueError
        _write(target, message + b"\x00")


def _from_browser(source, target):
    message = bytearray()
    while chunk := source.read(64 * 1024):
        parts = chunk.split(b"\x00")
        for index, part in enumerate(parts):
            if len(message) + len(part) > MAX_MESSAGE or b"\n" in part:
                raise ValueError
            message.extend(part)
            if index < len(parts) - 1:
                message.append(10)
                _write(target, message)
                message.clear()
    if message:
        raise ValueError


def _relay(process, source, target, reader, writer):
    """Relay until one side ends. Returns (exit code, whether the browser
    ended its side cleanly: it closed its pipe or exited)."""
    done = Queue()

    def copy(relay, source, target, browser_side):
        try:
            relay(source, target)
        except (OSError, ValueError, RecursionError):
            done.put((1, False))
        else:
            done.put((0, browser_side))

    for relay, src, dst, browser_side in ((_to_browser, source, writer, False),
                                          (_from_browser, reader, target, True)):
        Thread(target=copy, args=(relay, src, dst, browser_side), daemon=True).start()
    while True:
        try:
            return done.get(timeout=0.05)
        except Empty:
            if process.poll() is not None:
                return 0, True


def main(argv=None, *, adapter=None):
    # Keep buffered reads off the interpreter's stdio locks. The input thread
    # may still be blocked when the browser exits, so it cannot be joined.
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    source = io.BufferedReader(getattr(stdin, "raw", stdin))
    target = getattr(stdout, "raw", stdout)

    def error(reason):
        with suppress(OSError):
            _write(target, json.dumps({"helper": "error", "reason": reason}).encode() + b"\n")
        return 1

    try:
        args = _arguments(argv)
    except BadArguments:
        return error("bad arguments")

    if adapter is None:
        adapter = WindowsAdapter() if os.name == "nt" else PosixAdapter()
    try:
        executable = next((str(path) for path in adapter.candidates(args.browser)
                           if Path(path).is_file()), None)
        if executable is None:
            return error("browser not found")
        args.profile_dir.mkdir(parents=True, exist_ok=True)
        process, reader, writer = adapter.launch(executable, args)
    except (OSError, ValueError, subprocess.SubprocessError):
        return error("could not start the browser")

    browser_closing = False
    try:
        ready = {"helper": "ready", "browser": args.browser, "pid": process.pid}
        _write(target, json.dumps(ready).encode() + b"\n")
        code, browser_closing = _relay(process, source, target, reader, writer)
        return code
    except OSError:
        return 1
    finally:
        if browser_closing:
            adapter.settle(process)
        adapter.stop(process)


if __name__ == "__main__":
    sys.exit(main())
