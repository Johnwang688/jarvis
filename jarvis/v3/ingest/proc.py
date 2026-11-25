"""Bounded stdout collection for the daemon's read-only CLI calls."""

from contextlib import suppress
import os
import selectors
import signal
import subprocess
import time


class ProcError(Exception):
    pass


def run_bounded(argv, *, timeout_s, max_bytes):
    deadline = time.monotonic() + timeout_s
    keep = ("PATH", "HOME", "LANG")
    if os.path.basename(argv[0]) == "sw":
        # The WSL wrapper starts the Windows CLI, which uses its saved login.
        keep += ("WSL_INTEROP",)
    env = {name: os.environ[name] for name in keep if name in os.environ}
    try:
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, start_new_session=True, env=env)
    except (OSError, ValueError):
        raise ProcError("Process unavailable.") from None

    output = bytearray()
    try:
        with child.stdout, selectors.DefaultSelector() as ready:
            ready.register(child.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not ready.select(remaining):
                    raise ProcError("Process timeout.")
                chunk = os.read(child.stdout.fileno(), min(65536, max_bytes - len(output) + 1))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > max_bytes:
                    raise ProcError("Process output too large.")
        code = child.wait(timeout=max(0, deadline - time.monotonic()))
    except BaseException as error:
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        child.wait()
        if isinstance(error, subprocess.TimeoutExpired):
            raise ProcError("Process timeout.") from None
        if isinstance(error, OSError):
            raise ProcError("Process read failed.") from None
        raise
    return code, output.decode("utf-8", errors="replace")
