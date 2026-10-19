"""Small helpers the check scripts share (CL-1, 2026-10-18).

They used to be copied into each file that needed them. Only exact copies
moved here: a check whose helper behaves differently keeps its own.
"""
from __future__ import annotations

import socket
import time


def free_port() -> int:
    """A loopback port that was free a moment ago (bind to 0, read it, close)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(predicate, what: str, timeout: float = 30.0, interval: float = 0.02) -> None:
    """Return as soon as `predicate()` is true; raise AssertionError naming
    `what` if it is still false after `timeout` seconds."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what}")
