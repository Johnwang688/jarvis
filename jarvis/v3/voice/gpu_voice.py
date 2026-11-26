"""A lazy Kokoro voice backed by a separate, owner-provisioned Python."""

from __future__ import annotations

import json
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
from tempfile import TemporaryDirectory
from threading import Event, Lock
import time

from jarvis import config

from .engines import EngineUnavailable, _kokoro_pcm, _kokoro_speed


_WORKER = Path(__file__).resolve().with_name("kokoro_worker.py")
_LAUNCH_ENV = ("PATH", "LANG", "HOME")
_MISSING = "GPU voice environment is not installed"
_INSTALLING = ".jarvis-voice-installing"
_INSTALLED = ".jarvis-voice-installed"
_START_TIMEOUT = 120
_FIRST_TIMEOUT = 30
_REQUEST_TIMEOUT = 5
_FAILURE_WINDOW = 600
_CLOSE_GRACE = 0.5
_MAX_REQUEST = 8192
_MAX_HEADER = 1024
_MAX_SAMPLES = 8_000_000
_MAX_DURATION = 60


def _trusted_directory(path):
    info = path.stat()
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o022


def _trusted_ancestor(path):
    info = path.stat()
    return (stat.S_ISDIR(info.st_mode) and info.st_uid in (0, os.getuid())
            and (not info.st_mode & 0o022 or bool(info.st_mode & stat.S_ISVTX)))


def worker_python(path: Path, *, provisioning=False) -> Path:
    # Keep the final Python symlink so the interpreter discovers pyvenv.cfg
    # beside its invocation path. Check both the launch and resolved paths.
    try:
        path = path.expanduser().absolute()
        real_python = path.resolve(strict=True)
        binary = real_python.stat()
        real_bin = path.parent.resolve(strict=True)
        environment = path.parent.parent.resolve(strict=True)
        if (_trusted_directory(path.parent.parent) and _trusted_directory(path.parent)
                and all(_trusted_ancestor(folder) for folder in
                        {*path.parents, real_bin, *real_bin.parents, *environment.parents, *real_python.parents})
                and stat.S_ISREG(binary.st_mode) and binary.st_uid in (0, os.getuid())
                and not binary.st_mode & 0o022 and os.access(path, os.X_OK)
                and (provisioning or (not os.path.lexists(environment.parent / _INSTALLING)
                                      and stat.S_ISREG((environment / _INSTALLED).lstat().st_mode)))):
            return path
    except (OSError, RuntimeError, ValueError):
        pass
    raise EngineUnavailable(_MISSING)


def _signal_group(process, sig):
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


def _finish_group(process, *, graceful):
    try:
        if graceful:
            process.stdin.close()
            # Keep the leader unreaped until the group is done so its ID
            # cannot be reused while we still need to signal the group.
            try:
                leader = os.pidfd_open(process.pid)
            except (AttributeError, OSError):
                _signal_group(process, signal.SIGTERM)
            else:
                try:
                    poll = select.poll()
                    poll.register(leader, select.POLLIN)
                    if not poll.poll(int(_CLOSE_GRACE * 1000)):
                        _signal_group(process, signal.SIGTERM)
                        poll.poll(int(_CLOSE_GRACE * 1000))
                finally:
                    os.close(leader)
    finally:
        _signal_group(process, signal.SIGKILL)
        process.wait()


class KokoroWorkerVoice:
    name = "kokoro-gpu-worker"

    def __init__(self, *, speed=None, data_dir=None):
        self.speed = _kokoro_speed(speed)
        self._python = worker_python(config.V3_VOICE_GPU_PYTHON)
        self._data_dir = (config.V3_DIR if data_dir is None else Path(data_dir)).absolute()
        self._closed = Event()
        self._lock = Lock()
        self._process = None
        self._directory = None
        self._buffer = bytearray()
        self._request_id = 0
        self._first = True
        self._last_failure = float("-inf")
        self._retry_at = float("-inf")

    def _wait(self, fd, event, deadline):
        poll = select.poll()
        poll.register(fd, event)
        while not self._closed.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Check close even when the child never makes the pipe ready.
            if poll.poll(min(100, max(1, int(remaining * 1000)))):
                return
        raise TimeoutError

    def _read_more(self, deadline):
        fd = self._process.stdout.fileno()
        while True:
            self._wait(fd, select.POLLIN, deadline)
            try:
                chunk = os.read(fd, 65536)
                break
            except BlockingIOError:
                continue
        if not chunk:
            raise EOFError
        self._buffer.extend(chunk)

    def _header(self, deadline):
        while (end := self._buffer.find(b"\n")) == -1:
            if len(self._buffer) >= _MAX_HEADER:
                raise ValueError
            self._read_more(deadline)
        if end >= _MAX_HEADER:
            raise ValueError
        header = json.loads(self._buffer[:end])
        del self._buffer[:end + 1]
        if not isinstance(header, dict):
            raise ValueError
        return header

    def _start(self):
        self._data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._directory = TemporaryDirectory(prefix="voice-worker-", dir=self._data_dir)
        python = worker_python(self._python)
        self._process = subprocess.Popen(
            [str(python), "-I", "-B", str(_WORKER), str(config.KOKORO_MODEL.expanduser().absolute()),
             str(config.KOKORO_VOICES.expanduser().absolute())],
            cwd=self._directory.name, env={key: os.environ[key] for key in _LAUNCH_ENV if key in os.environ},
            shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True, bufsize=0,
        )
        os.set_blocking(self._process.stdin.fileno(), False)
        os.set_blocking(self._process.stdout.fileno(), False)
        header = self._header(time.monotonic() + _START_TIMEOUT)
        if header.keys() != {"ready"} or header["ready"] is not True or self._buffer:
            raise ValueError
        self._first = True

    def _require_idle_stdout(self):
        # One bounded read detects queued data or EOF. On failure the caller
        # kills the worker and closes the pipe, discarding any remaining bytes.
        if self._buffer:
            raise ValueError
        try:
            os.read(self._process.stdout.fileno(), 65536)
        except BlockingIOError:
            return
        raise ValueError

    def _exchange(self, request, request_id):
        deadline = time.monotonic() + (_FIRST_TIMEOUT if self._first else _REQUEST_TIMEOUT)
        fd = self._process.stdin.fileno()
        self._require_idle_stdout()
        remaining = memoryview(request)
        while remaining:
            self._wait(fd, select.POLLOUT, deadline)
            try:
                remaining = remaining[os.write(fd, remaining):]
            except BlockingIOError:
                continue
        header = self._header(deadline)
        if (header.keys() != {"id", "ok", "rate", "samples"} or header["ok"] is not True
                or type(header["id"]) is not int or header["id"] != request_id):
            raise ValueError
        rate, count = header["rate"], header["samples"]
        if type(rate) is not int or not 8_000 <= rate <= 48_000:
            raise ValueError
        if type(count) is not int or not 0 < count <= _MAX_SAMPLES or count > rate * _MAX_DURATION:
            raise ValueError
        size = count * 4
        while len(self._buffer) < size:
            self._read_more(deadline)
        if len(self._buffer) != size:
            raise ValueError
        payload, self._buffer = self._buffer, bytearray()
        # There is no next response yet. Catch surplus bytes even if the read
        # that completed the expected payload happened to end on its boundary.
        self._require_idle_stdout()
        self._first = False
        return payload, rate

    def synthesize(self, text):
        import numpy as np
        from jarvis.voice import speakable

        text = speakable(text)
        if not text:
            return np.empty(0, dtype=np.int16), 24_000
        with self._lock:
            if self._closed.is_set():
                raise EngineUnavailable("GPU voice is closed")
            if time.monotonic() < self._retry_at:
                raise EngineUnavailable("GPU voice is temporarily unavailable")
            request_id = self._request_id + 1
            try:
                request = json.dumps({"id": request_id, "text": text, "speed": self.speed}, ensure_ascii=False,
                                     allow_nan=False, separators=(",", ":")).encode() + b"\n"
            except (TypeError, ValueError):
                raise EngineUnavailable("GPU voice request is invalid") from None
            if len(request) > _MAX_REQUEST:
                raise EngineUnavailable("GPU voice request is too long")
            self._request_id = request_id
            try:
                if self._process is None:
                    self._start()
                payload, rate = self._exchange(request, request_id)
                samples = np.frombuffer(payload, dtype="<f4")
                # The maximum propagates NaN and infinity, which fail this range check.
                if not 0 < np.max(np.abs(samples)) <= 4.0:
                    raise ValueError
                return _kokoro_pcm(samples, rate), rate
            except (EngineUnavailable, OSError, EOFError, ValueError, RecursionError) as error:
                if self._closed.is_set():
                    raise EngineUnavailable("GPU voice is closed") from None
                self._stop(graceful=False)
                now = time.monotonic()
                if now - self._last_failure < _FAILURE_WINDOW:
                    self._retry_at = now + _FAILURE_WINDOW
                self._last_failure = now
                reason = _MISSING if isinstance(error, EngineUnavailable) else "GPU voice worker failed"
                raise EngineUnavailable(reason) from None

    def _stop(self, *, graceful):
        process, self._process = self._process, None
        directory, self._directory = self._directory, None
        self._buffer.clear()
        try:
            if process is not None:
                with process.stdin, process.stdout:
                    _finish_group(process, graceful=graceful)
        finally:
            if directory is not None:
                directory.cleanup()

    def close(self):
        self._closed.set()
        with self._lock:
            self._stop(graceful=True)
