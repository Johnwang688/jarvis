"""Private request and event transport for the Windows browser helper."""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path, PureWindowsPath
import re
import select
import subprocess
import threading
import time

from jarvis import config
from jarvis.v3.voice import installation


_MAX_REQUEST = 16 * 1024 * 1024
_MAX_MESSAGE = 64 * 1024 * 1024
_METHOD = re.compile(r"[A-Z][A-Za-z]+\.[a-zA-Z]+")
_CLOSED = "The browser link is closed."
_TIMED_OUT = "The browser did not answer in time."
_START_ERRORS = {
    "browser not found": "The Windows browser was not found.",
    "bad arguments": "The browser helper rejected its arguments.",
    "could not start the browser": "The Windows browser could not start.",
}
_START_FAILED = "The browser helper did not start."


class LinkError(Exception):
    def __init__(self, message, *, browser_message=None):
        super().__init__(message)
        self.browser_message = browser_message


@dataclass
class _Reply:
    ready: threading.Event = field(default_factory=threading.Event)
    message: dict | None = None


@dataclass
class _Run:
    process: subprocess.Popen
    startup: _Reply = field(default_factory=_Reply)
    pending: dict[int, _Reply] = field(default_factory=dict)
    closed: threading.Event = field(default_factory=threading.Event)
    stop_lock: threading.Lock = field(default_factory=threading.Lock)
    reader: threading.Thread | None = None


class BrowserLink:
    def __init__(self, command: list[str], *, start_timeout=30.0,
                 close_request_timeout=5.0, close_exit_timeout=10.0, stop_timeout=5.0,
                 interop: bool = False, cwd=None):
        self._command = list(command)
        self._start_timeout = start_timeout
        self._close_request_timeout = close_request_timeout
        self._close_exit_timeout = close_exit_timeout
        self._stop_timeout = stop_timeout
        self._interop = interop
        self._cwd = cwd
        self._run = None
        self._id = 0
        self._lock = threading.Lock()
        self._lifecycle = threading.Lock()
        self._write_lock = threading.Lock()
        self._callbacks = []
        self._callback_errors = 0

    @property
    def alive(self):
        with self._lock:
            return self._alive(self._run)

    @staticmethod
    def _alive(run):
        return (run is not None and not run.closed.is_set() and run.startup.message is not None
                and run.startup.message.get("helper") == "ready" and run.process.poll() is None)

    @property
    def callback_errors(self):
        with self._lock:
            return self._callback_errors

    def start(self) -> None:
        with self._lifecycle:
            with self._lock:
                if self._alive(self._run):
                    return
                previous = self._run
            if previous is not None:
                self._stop(previous)
            env = None
            if self._interop:
                env = os.environ.copy()
                interop = env.get("WSL_INTEROP")
                if not interop or not Path(interop).is_socket():
                    fallback = installation.WSL_INTEROP_SOCKET
                    if not fallback.is_socket():
                        raise LinkError("Windows launch is unavailable. Open WSL and retry.")
                    env["WSL_INTEROP"] = str(fallback)
            try:
                process = subprocess.Popen(self._command, stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                           cwd=self._cwd, env=env, start_new_session=True)
            except (OSError, ValueError, IndexError):
                raise LinkError(_START_FAILED) from None
            run = _Run(process)
            with self._lock:
                self._run = run
            try:
                os.set_blocking(process.stdin.fileno(), False)
                run.reader = threading.Thread(target=self._read, args=(run,), daemon=True)
                run.reader.start()
                if not run.startup.ready.wait(self._start_timeout):
                    raise LinkError("The browser helper did not answer in time.")
                if not self.alive:
                    message = run.startup.message or {}
                    reason = message.get("reason")
                    raise LinkError(_START_ERRORS.get(reason, _START_FAILED)
                                    if isinstance(reason, str) else _START_FAILED)
            except (OSError, RuntimeError):
                self._stop(run)
                raise LinkError(_START_FAILED) from None
            except BaseException:
                self._stop(run)
                raise

    def subscribe(self, callback) -> None:
        """Run callback on the reader thread. Callbacks should return promptly."""
        with self._lock:
            self._callbacks.append(callback)

    def _read(self, run):
        try:
            with run.process.stdout as source:
                startup = self._read_message(source, 4096)
                if startup.get("helper") == "ready" and (
                    startup.get("browser") not in ("chrome", "edge")
                    or type(startup.get("pid")) is not int or startup["pid"] <= 0
                ):
                    return
                with self._lock:
                    run.startup.message = startup
                    run.startup.ready.set()
                if startup.get("helper") != "ready":
                    return
                while not run.closed.is_set():
                    self._dispatch(run, self._read_message(source, _MAX_MESSAGE))
        except (OSError, ValueError, RecursionError):
            pass
        finally:
            self._stop(run)

    @staticmethod
    def _read_message(source, limit):
        line = source.readline(limit + 2)
        if not line.endswith(b"\n") or len(line) > limit + 1:
            raise ValueError
        message = json.loads(line.decode("utf-8"))
        if not isinstance(message, dict):
            raise ValueError
        return message

    def _dispatch(self, run, message):
        with self._lock:
            if run.closed.is_set():
                return
            if "id" in message:
                ident = message["id"]
                if type(ident) is int and (reply := run.pending.pop(ident, None)) is not None:
                    reply.message = message
                    reply.ready.set()
                return
            callbacks = tuple(self._callbacks)
        for callback in callbacks:
            try:
                callback(message)
            except BaseException:
                with self._lock:
                    self._callback_errors += 1

    def _disconnect(self, run):
        with self._lock:
            run.closed.set()
            run.startup.ready.set()
            for reply in run.pending.values():
                reply.ready.set()
            run.pending.clear()

    def call(self, method, params=None, *, session=None, timeout=30.0) -> dict:
        if not isinstance(method, str) or not _METHOD.fullmatch(method):
            raise LinkError("Invalid browser method.")
        if params is not None:
            if not isinstance(params, dict):
                raise LinkError("Browser parameters must be a JSON object.")
            # Send the same top-level keys that were checked, even if another
            # thread changes the caller's parameters while we wait to write.
            params = dict(params)
            if "proxyServer" in params or "proxyBypassList" in params:
                raise LinkError("The browser request is not allowed.")
        return self._call(method, params, session=session, timeout=timeout)

    def _call(self, method, params=None, *, session=None, timeout, closing=False):
        with self._lock:
            run = self._run
            if not self._alive(run):
                raise LinkError(_CLOSED)
            on_reader = threading.current_thread() is run.reader
            if on_reader and not closing:
                raise LinkError("Browser callbacks cannot wait for replies.")
        deadline = time.monotonic() + timeout
        remaining = deadline - time.monotonic()
        if not self._write_lock.acquire(timeout=max(0, remaining)):
            raise LinkError(_TIMED_OUT)
        try:
            with self._lock:
                if not self._alive(run):
                    raise LinkError(_CLOSED)
                self._id += 1
                ident = self._id
            request = {"id": ident, "method": method}
            if params is not None:
                request["params"] = params
            if session is not None:
                request["sessionId"] = session
            try:
                line = json.dumps(request, allow_nan=False, separators=(",", ":")).encode("utf-8")
            except (TypeError, ValueError, RecursionError):
                raise LinkError("Invalid browser request data.") from None
            if len(line) > _MAX_REQUEST:
                raise LinkError("The browser request is too large.")

            reply = _Reply()
            with self._lock:
                if run.closed.is_set():
                    raise LinkError(_CLOSED)
                run.pending[ident] = reply
            try:
                self._write(run, line + b"\n", deadline)
            except LinkError:
                with self._lock:
                    run.pending.pop(ident, None)
                raise
        finally:
            self._write_lock.release()

        try:
            if on_reader:
                # A callback can close the browser, but cannot read its reply.
                return
            while not reply.ready.is_set():
                if run.process.poll() is not None:
                    self._disconnect(run)
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LinkError(_TIMED_OUT)
                reply.ready.wait(min(remaining, 0.05))
        finally:
            with self._lock:
                run.pending.pop(ident, None)

        message = reply.message
        if message is None:
            raise LinkError(_CLOSED)
        if "error" in message:
            error = message["error"]
            details = error.get("message") if isinstance(error, dict) else None
            raise LinkError("The browser refused the request.",
                            browser_message=details[:300] if isinstance(details, str) else None)
        result = message.get("result")
        if not isinstance(result, dict):
            raise LinkError("The browser sent an invalid reply.")
        return result

    def _write(self, run, data, deadline):
        stream = run.process.stdin
        fd = stream.fileno()
        poller = select.poll()
        poller.register(fd, select.POLLOUT)
        remaining = memoryview(data)
        try:
            while remaining:
                if run.closed.is_set() or run.process.poll() is not None:
                    raise BrokenPipeError
                budget = deadline - time.monotonic()
                if budget <= 0:
                    # If we started this line, EOF prevents the helper from
                    # mistaking a later request for the rest of this one.
                    if len(remaining) < len(data):
                        self._disconnect(run)
                        stream.close()
                    raise LinkError(_TIMED_OUT)
                if not poller.poll(max(1, int(min(budget, 0.05) * 1000))):
                    continue
                try:
                    remaining = remaining[os.write(fd, remaining):]
                except BlockingIOError:
                    continue
        except OSError:
            self._disconnect(run)
            raise LinkError(_CLOSED) from None

    def close(self) -> None:
        with self._lifecycle:
            with self._lock:
                run = self._run
                alive = self._alive(run)
            if run is None:
                return
            if alive:
                try:
                    self._call("Browser.close", timeout=self._close_request_timeout, closing=True)
                except LinkError:
                    pass
                try:
                    run.process.wait(timeout=self._close_exit_timeout)
                except subprocess.TimeoutExpired:
                    pass
            self._stop(run)

    def _stop(self, run):
        # The reader can stop its run while start() or close() holds the
        # lifecycle lock. Reaping belongs to one caller and never joins it.
        with run.stop_lock:
            with self._lock:
                if self._run is not run:
                    return
            self._disconnect(run)
            process = run.process
            with self._write_lock:
                process.stdin.close()
            try:
                process.wait(timeout=self._stop_timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            if run.reader is None or run.reader.ident is None:
                process.stdout.close()
            with self._lock:
                self._run = None


def helper_command(browser, *, proxy_port, window=False) -> list[str]:
    command, _ = _helper_launch(browser, proxy_port=proxy_port, window=window)
    return command


def windows_link(browser, *, proxy_port, window=False, **options) -> BrowserLink:
    command, bridge = _helper_launch(browser, proxy_port=proxy_port, window=window)
    return BrowserLink(command, interop=True, cwd=bridge, **options)


def _helper_launch(browser, *, proxy_port, window):
    if browser not in ("chrome", "edge"):
        raise LinkError("Invalid Windows browser.")
    if type(proxy_port) is not int or not 1 <= proxy_port <= 65535:
        raise LinkError("Invalid browser proxy port.")

    try:
        bridge = installation._bridge()
        if not bridge.is_dir():
            raise LinkError("The closed Windows bridge folder is unavailable.")
        python = bridge / "venv" / "Scripts" / "python.exe"
        profile_suffix = ("browser", browser)
        if bridge.joinpath(*profile_suffix).resolve() != bridge.resolve().joinpath(*profile_suffix):
            raise LinkError("The browser profile must be inside the Windows bridge.")
        if not python.is_file():
            raise LinkError("The Windows bridge Python is missing.")
        script = installation._convert("-w", config.REPO_ROOT / "windows" / "v3browser_helper.py")
        windows_bridge = PureWindowsPath(installation._convert("-w", bridge))
        if not windows_bridge.is_absolute():
            raise LinkError("The browser profile must be inside the Windows bridge.")
    except (installation.HelperUnavailable, OSError, RuntimeError, subprocess.SubprocessError):
        raise LinkError("The closed Windows bridge folder is unavailable.") from None

    windows_profile = str(windows_bridge.joinpath(*profile_suffix))
    command = [str(python), script, "--browser", browser, "--profile-dir", windows_profile,
               "--proxy-port", str(proxy_port)]
    if window:
        command.append("--window")
    return command, bridge
