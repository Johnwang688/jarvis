"""Lazy, offline Laya voice router backed by one resident CUDA process.

Install the runtime manifest at ~/.config/jarvis/laya-runtime.json, or set
JARVIS_LAYA_RUNTIME to its absolute path. The manifest uses the benchmark's
repo, revision, python, checkpoint, and device fields. The interpreter and
checkpoint paths must be absolute, and device must select CUDA.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import select
import subprocess
import threading
import time

from jarvis.v2 import laya_routing
from jarvis.v2.laya_schema import ENGLISH_REVISION, MAX_FRAME, REPO


RUNTIME_ENV = "JARVIS_LAYA_RUNTIME"
DEFAULT_RUNTIME = Path.home() / ".config" / "jarvis" / "laya-runtime.json"
_STARTUP_TIMEOUT = 20.0
_REQUEST_TIMEOUT = 3.0
_CLOSE_POLL = 0.05
_ROOT = Path(__file__).resolve().parents[2]
_BOOTSTRAP = (
    "import sys; sys.path.insert(0, sys.argv.pop(1)); "
    "from jarvis.v2.laya_worker import main; main()"
)


def _launch_worker():
    manifest = Path(os.environ.get(RUNTIME_ENV, str(DEFAULT_RUNTIME)))
    if not manifest.is_absolute() or not manifest.is_file():
        raise ValueError("Laya requires an absolute local runtime manifest")
    with manifest.open("rb") as stream:
        data = stream.read(MAX_FRAME + 1)
    if len(data) > MAX_FRAME:
        raise ValueError("Laya runtime manifest too large")
    config = json.loads(data)
    if (not isinstance(config, dict) or config.get("repo") != REPO
            or config.get("revision") != ENGLISH_REVISION):
        raise ValueError("Laya requires the pinned English checkpoint")
    python, checkpoint = Path(config["python"]), Path(config["checkpoint"])
    if not python.is_absolute() or not python.is_file():
        raise ValueError("Laya requires an absolute interpreter path")
    if not checkpoint.is_absolute() or not checkpoint.is_dir():
        raise ValueError("Laya requires an absolute local checkpoint directory")
    device = config["device"]
    if not re.fullmatch(r"cuda(?::[0-9]+)?", device):
        raise ValueError("Laya requires a CUDA device")
    dtype = config.get("dtype")
    if dtype not in (None, "bfloat16", "float16", "float32"):
        raise ValueError("Invalid Laya CUDA precision")

    env = {key: os.environ[key] for key in (
        "HOME", "LANG", "LC_ALL", "XDG_CACHE_HOME", "CUDA_VISIBLE_DEVICES",
        "CUDA_DEVICE_ORDER",
    ) if key in os.environ}
    env.update(PATH=os.defpath, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
               HF_HUB_DISABLE_IMPLICIT_TOKEN="1", HF_HUB_DISABLE_TELEMETRY="1",
               DO_NOT_TRACK="1", OMP_NUM_THREADS="2", MKL_NUM_THREADS="2",
               OPENBLAS_NUM_THREADS="2", LAYA_CUDA_AMP=dtype or "bfloat16")
    # Preserve the virtualenv launcher path, which may be a symlink to base Python.
    return subprocess.Popen(
        [str(python), "-I", "-B", "-c", _BOOTSTRAP, str(_ROOT), str(checkpoint), device],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        cwd="/", env=env, bufsize=0,
    )


class LayaRouter:
    """Apply the locked routing rules using a worker started on first inference."""

    def __init__(self):
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._worker = None
        self._request_id = 0

    def route(self, mode, text, *, apps=(), windows=()):
        if self._closed.is_set():
            return {"kind": "unavailable", "target": "", "scores": {}}

        scores = {}

        def classify(transcript):
            nonlocal scores
            scores = self._classify(transcript)
            return scores

        decision = laya_routing.decide(mode, text, classify, apps=apps, windows=windows)
        if self._closed.is_set():
            return {"kind": "unavailable", "target": "", "scores": {}}
        return dict(decision, scores=scores)

    def _classify(self, transcript):
        # Fast reject before escaping, which can otherwise allocate from unbounded input.
        if len(transcript) > MAX_FRAME:
            raise ValueError("Laya transcript too large")
        deadline = time.monotonic() + _STARTUP_TIMEOUT + _REQUEST_TIMEOUT
        if not self._lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise TimeoutError("Laya worker busy")
        try:
            self._check_deadline(deadline)
            self._request_id += 1
            request_id = self._request_id
            frame = json.dumps({"id": request_id, "text": transcript}).encode("ascii") + b"\n"
            if len(frame) > MAX_FRAME:
                raise ValueError("Laya request too large")

            if self._worker is not None and self._worker.stdin.closed:
                # A killed worker can briefly remain in an uninterruptible OS wait.
                if self._worker.poll() is None:
                    raise RuntimeError("Previous Laya worker still exiting")
                self._worker = None

            try:
                if self._worker is None:
                    startup_deadline = min(deadline, time.monotonic() + _STARTUP_TIMEOUT)
                    self._worker = _launch_worker()
                    os.set_blocking(self._worker.stdin.fileno(), False)
                    os.set_blocking(self._worker.stdout.fileno(), False)
                    ready = self._read(startup_deadline)
                    if (not isinstance(ready, dict) or ready.keys() != {"ready"}
                            or ready["ready"] is not True):
                        raise ValueError("Invalid Laya startup response")

                request_deadline = min(deadline, time.monotonic() + _REQUEST_TIMEOUT)
                self._write(frame, request_deadline)
                response = self._read(request_deadline)
                if (not isinstance(response, dict) or response.keys() != {"id", "scores"}
                        or type(response["id"]) is not int or response["id"] != request_id):
                    raise ValueError("Invalid Laya response")
                scores = response["scores"]
                if scores is not None:
                    if not isinstance(scores, dict) or set(scores) != set(laya_routing.CHOICES):
                        raise ValueError("Invalid Laya score choices")
                    # Reuse the grounding adapter's numeric validation before retaining this worker.
                    laya_routing._rank(scores)
            except Exception:
                self._stop_worker()
                raise

            if scores is None:
                raise ValueError("Laya could not score the full request")
            return scores
        finally:
            self._lock.release()

    def _check_deadline(self, deadline):
        if self._closed.is_set():
            raise RuntimeError("Laya router closed")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Laya worker deadline exceeded")
        return remaining

    def _wait(self, fd, event, deadline):
        poller = select.poll()
        poller.register(fd, event)
        while True:
            remaining = self._check_deadline(deadline)
            if poller.poll(math.ceil(min(remaining, _CLOSE_POLL) * 1000)):
                self._check_deadline(deadline)
                return

    def _write(self, frame, deadline):
        fd = self._worker.stdin.fileno()
        rest = memoryview(frame)
        while rest:
            self._wait(fd, select.POLLOUT, deadline)
            rest = rest[os.write(fd, rest):]

    def _read(self, deadline):
        fd = self._worker.stdout.fileno()
        frame = bytearray()
        while len(frame) < MAX_FRAME:
            self._wait(fd, select.POLLIN, deadline)
            chunk = os.read(fd, MAX_FRAME - len(frame))
            if not chunk:
                raise EOFError("Laya worker exited")
            frame.extend(chunk)
            if b"\n" in chunk:
                if frame.find(b"\n") != len(frame) - 1:
                    raise ValueError("Unexpected extra Laya output")
                return json.loads(frame)
        raise ValueError("Laya response too large")

    def _stop_worker(self):
        worker = self._worker
        if worker is None:
            return
        worker.kill()
        worker.stdin.close()
        worker.stdout.close()
        try:
            worker.wait(timeout=1)
        except subprocess.TimeoutExpired:
            # Keep the handle for reaping and block launches until it exits.
            return
        self._worker = None

    def close(self):
        """Stop the worker, cancel any active pipe wait, and prevent restarts."""
        self._closed.set()
        with self._lock:
            self._stop_worker()
