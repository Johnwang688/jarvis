# Adapted from jarvis-trading-firm/firm/codex_rpc.py (John W., 2026).
"""Small, cancellable stdio transport for the Codex app-server protocol.

One wake thread owns this object. Reader threads only drain pipes; callbacks
run on the owner thread, preserving the caller's runtime context. Server stderr
and malformed protocol text are never included in exceptions or application logs.
"""

from __future__ import annotations

import itertools
import json
import os
import queue
import selectors
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from types import SimpleNamespace

config = SimpleNamespace(
    CODEX_RPC_POLL_SECONDS=0.05, CODEX_RPC_TIMEOUT_SECONDS=30.0,
    CODEX_RPC_CLOSE_GRACE_SECONDS=0.25, CODEX_RPC_THREAD_JOIN_SECONDS=0.25,
    CODEX_RPC_LINE_LIMIT=1_048_576, CODEX_RPC_QUEUE_LIMIT=4096,
    CODEX_RPC_STDERR_CHUNK=4096, CODEX_RPC_METHOD_NOT_FOUND=-32601,
    CODEX_RPC_CLIENT_NAME="jarvis_v2", CODEX_RPC_CLIENT_VERSION="2.0.0",
)


class RpcError(RuntimeError):
    """The transport or a JSON-RPC request failed."""


class RpcTimeout(RpcError):
    """The request deadline expired."""


class RpcCancelled(RpcError):
    """The scheduler requested cancellation."""


class RpcProcess:
    """JSON-lines client with explicit event access and optional callbacks.

    ``request`` returns the matching result. Its ``on_request`` callback gets
    the complete server request and returns the result to reply with; absent
    callbacks refuse server requests. Notifications are passed to
    ``on_notification``, or preserved for ``next_message``. ``send`` and
    ``reply`` support a fully manual event loop instead.
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        stop: Callable[[], bool] | None = None,
    ):
        self.argv = list(argv)
        self.cwd = cwd
        self.env = None if env is None else dict(env)
        self.stop = stop
        self.process: subprocess.Popen | None = None
        self._ids = itertools.count()
        self._incoming: queue.Queue[dict | RpcError] = queue.Queue(config.CODEX_RPC_QUEUE_LIMIT)
        self._pending: deque[dict] = deque()
        self._closed = threading.Event()
        self.failed = threading.Event()
        self._overflow = threading.Event()
        self._write_lock = threading.Lock()
        self._readers: list[threading.Thread] = []

    def start(self) -> RpcProcess:
        if self._closed.is_set():
            raise RpcError("Codex app-server transport is closed")
        if self.process is not None:
            return self
        try:
            self.process = subprocess.Popen(
                self.argv, cwd=self.cwd, env=self.env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", start_new_session=True,
            )
        except OSError as exc:
            raise RpcError(f"Codex app-server could not start ({type(exc).__name__})") from None
        assert self.process.stdin is not None
        os.set_blocking(self.process.stdin.fileno(), False)
        for target in (self._read_stdout, self._drain_stderr):
            reader = threading.Thread(target=target, daemon=True, name="codex-rpc-reader")
            self._readers.append(reader)
            reader.start()
        return self

    def _enqueue(self, value: dict | RpcError) -> None:
        if isinstance(value, RpcError):
            self.failed.set()
        try:
            self._incoming.put_nowait(value)
        except queue.Full:
            # Never block the pipe reader behind a human callback. Overflow
            # invalidates the stream; the consumer fails closed after returning.
            self.failed.set()
            self._overflow.set()

    def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while not self._closed.is_set():
                line = self.process.stdout.readline(config.CODEX_RPC_LINE_LIMIT)
                if not line:
                    self._enqueue(RpcError("Codex app-server closed its output"))
                    return
                if len(line) >= config.CODEX_RPC_LINE_LIMIT and not line.endswith("\n"):
                    self._enqueue(RpcError("Codex app-server protocol message exceeds the size limit"))
                    return
                try:
                    message = json.loads(line)
                except (ValueError, RecursionError):
                    self._enqueue(RpcError("Codex app-server emitted malformed JSON"))
                    return
                if not isinstance(message, dict):
                    self._enqueue(RpcError("Codex app-server emitted an invalid protocol object"))
                    return
                self._enqueue(message)
        except (OSError, ValueError):
            self._enqueue(RpcError("Codex app-server output could not be read"))

    def _drain_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        try:
            while self.process.stderr.read(config.CODEX_RPC_STDERR_CHUNK):
                # Discard in bounded chunks: diagnostics can contain credentials
                # and are never a substitute for typed protocol errors.
                pass
        except (OSError, ValueError):
            pass

    def _check_cancelled(self) -> None:
        if self._overflow.is_set():
            raise RpcError("Codex app-server event queue overflow; stream incomplete")
        if self._closed.is_set():
            raise RpcError("Codex app-server transport is closed")
        if self.stop is not None and self.stop():
            raise RpcCancelled("Codex app-server wake cancelled")

    def _write(self, message: dict, *, deadline: float) -> None:
        with self._write_lock:
            self._write_unlocked(message, deadline=deadline)

    def _write_unlocked(self, message: dict, *, deadline: float) -> None:
        self._check_cancelled()
        self.start()
        assert self.process is not None and self.process.stdin is not None
        pending = memoryview((json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8"))
        stream_fd = self.process.stdin.fileno()
        try:
            with selectors.DefaultSelector() as writable:
                writable.register(stream_fd, selectors.EVENT_WRITE)
                while pending:
                    self._check_cancelled()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RpcTimeout("Codex app-server input deadline expired")
                    if not writable.select(min(remaining, config.CODEX_RPC_POLL_SECONDS)):
                        continue
                    try:
                        count = os.write(stream_fd, pending)
                    except (BlockingIOError, InterruptedError):
                        continue
                    if count <= 0:
                        raise RpcError("Codex app-server closed its input")
                    pending = pending[count:]
        except RpcError:
            # A partial JSON line cannot be safely followed by another request.
            # Terminate the peer instead of leaving a poisoned reusable pipe.
            self.close()
            raise
        except (OSError, ValueError):
            self.close()
            raise RpcError("Codex app-server input could not be written") from None

    def send(
        self, method: str, params: dict | None = None, *,
        timeout: float = config.CODEX_RPC_TIMEOUT_SECONDS, deadline: float | None = None,
    ) -> int:
        request_id = next(self._ids)
        message: dict[str, Any] = {"id": request_id, "method": method}
        # No-params methods in older app-server schemas require `params` to be
        # absent/null.  Do not turn an intentional None into {}, which is a
        # different JSON-RPC shape (notably for account/rateLimits/read on
        # codex-cli 0.153.4).
        if params is not None:
            message["params"] = params
        self._write(message, deadline=deadline if deadline is not None else time.monotonic() + timeout)
        return request_id

    def notify(
        self, method: str, params: dict | None = None, *,
        timeout: float = config.CODEX_RPC_TIMEOUT_SECONDS, deadline: float | None = None,
    ) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        self._write(message, deadline=deadline if deadline is not None else time.monotonic() + timeout)

    def reply(
        self, request_id: int | str, result: Any = None, *, error: dict | None = None,
        timeout: float = config.CODEX_RPC_TIMEOUT_SECONDS, deadline: float | None = None,
    ) -> None:
        self._write({"id": request_id, "error": error} if error is not None else {"id": request_id, "result": result},
                    deadline=deadline if deadline is not None else time.monotonic() + timeout)

    def _read_message(self, deadline: float) -> dict:
        self.start()
        while True:
            self._check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RpcTimeout("Codex app-server response deadline expired")
            try:
                message = self._incoming.get(timeout=min(remaining, config.CODEX_RPC_POLL_SECONDS))
            except queue.Empty:
                if self.failed.is_set():
                    raise RpcError("Codex app-server transport failed")
                continue
            if isinstance(message, RpcError):
                raise message
            return message

    def next_message(
        self, timeout: float = config.CODEX_RPC_TIMEOUT_SECONDS, *, deadline: float | None = None,
    ) -> dict:
        self._check_cancelled()
        if self._pending:
            return self._pending.popleft()
        return self._read_message(deadline if deadline is not None else time.monotonic() + timeout)

    def request(
        self,
        method: str,
        params: dict | None = None,
        *,
        timeout: float = config.CODEX_RPC_TIMEOUT_SECONDS,
        deadline: float | None = None,
        on_request: Callable[[dict], Any] | None = None,
        on_notification: Callable[[dict], None] | None = None,
    ) -> Any:
        end = deadline if deadline is not None else time.monotonic() + timeout
        request_id = self.send(method, params, deadline=end)
        while True:
            message = self._read_message(end)
            if "method" in message:
                if "id" in message:
                    if on_request is None:
                        self.reply(message["id"], error={
                            "code": config.CODEX_RPC_METHOD_NOT_FOUND,
                            "message": "Server requests are not enabled for this operation",
                        }, deadline=end)
                    else:
                        self.reply(message["id"], on_request(message), deadline=end)
                elif on_notification is not None:
                    on_notification(message)
                else:
                    self._preserve(message)
            elif message.get("id") == request_id:
                if "error" in message:
                    error = message["error"]
                    code = error.get("code") if isinstance(error, dict) else None
                    safe_code = str(code) if isinstance(code, int) else "unknown"
                    raise RpcError(f"Codex RPC {method} failed (code {safe_code})")
                if "result" not in message:
                    raise RpcError("Codex app-server response has no result")
                return message["result"]
            else:
                self._preserve(message)

    def _preserve(self, message: dict) -> None:
        if len(self._pending) >= config.CODEX_RPC_QUEUE_LIMIT:
            raise RpcError("Codex app-server has too many unhandled messages")
        self._pending.append(message)

    def initialize(self, *, timeout: float = config.CODEX_RPC_TIMEOUT_SECONDS) -> dict:
        result = self.request("initialize", {
            "clientInfo": {"name": config.CODEX_RPC_CLIENT_NAME, "version": config.CODEX_RPC_CLIENT_VERSION},
            "capabilities": {"experimentalApi": True},
        }, timeout=timeout)
        self.notify("initialized")
        return result

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        proc = self.process
        if proc is None:
            return
        # Each launch owns a new process group. Signal the group even when its
        # leader has exited, so inherited pipes/descendants do not outlive it.
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, signum)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=config.CODEX_RPC_CLOSE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                continue
        for reader in self._readers:
            reader.join(timeout=config.CODEX_RPC_THREAD_JOIN_SECONDS)
        if proc.stdin is not None:
            proc.stdin.close()
        # A pipe reader normally exits on process-group death. Do not acquire
        # its TextIO lock if an externally detached descendant holds the pipe.
        for reader, stream in zip(self._readers, (proc.stdout, proc.stderr)):
            if stream is not None and not reader.is_alive():
                stream.close()

    def __enter__(self) -> RpcProcess:
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.close()
