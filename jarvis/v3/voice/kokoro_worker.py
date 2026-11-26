"""Standalone CUDA Kokoro worker. Stdout carries only the binary pipe protocol."""

import ctypes
from importlib.metadata import PackageNotFoundError, distribution
import json
import math
import os
import sys


MAX_REQUEST = 8192
CUDA_PROVIDER = "CUDAExecutionProvider"

# Keep the order in sync with engines._preload_cuda. This script runs without Jarvis.
CUDA_WHEELS = (
    ("cuda-runtime", "cuda_runtime", ("libcudart.so.12",)),
    ("nvjitlink", "nvjitlink", ("libnvJitLink.so.12",)),
    ("cuda-nvrtc", "cuda_nvrtc", ("libnvrtc-builtins.so.12.9", "libnvrtc.so.12")),
    ("cublas", "cublas", ("libcublasLt.so.12", "libcublas.so.12")),
    ("cufft", "cufft", ("libcufft.so.11",)),
    ("curand", "curand", ("libcurand.so.10",)),
    ("cudnn", "cudnn", tuple(f"libcudnn{component}.so.9" for component in (
        "_graph", "_ops", "_adv", "_cnn", "_engines_precompiled",
        "_engines_runtime_compiled", "_engines_tensor_ir", "_heuristic", "",
    ))),
)


def _preload_cuda():
    try:
        driver = ctypes.CDLL("libcuda.so.1", mode=ctypes.RTLD_GLOBAL)
    except OSError:
        driver = ctypes.CDLL("/usr/lib/wsl/lib/libcuda.so.1", mode=ctypes.RTLD_GLOBAL)
    handles = [driver]
    for wheel, module, libraries in CUDA_WHEELS:
        try:
            package = distribution(f"nvidia-{wheel}-cu12")
        except PackageNotFoundError:
            continue
        for library in libraries:
            path = package.locate_file(f"nvidia/{module}/lib/{library}")
            handles.append(ctypes.CDLL(str(path), mode=ctypes.RTLD_GLOBAL))
    return handles


def _load(model, voices):
    import onnxruntime as ort
    from kokoro_onnx import Kokoro

    if CUDA_PROVIDER not in ort.get_available_providers():
        raise RuntimeError("CUDA provider unavailable")
    handles = _preload_cuda()
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    options.inter_op_num_threads = 1
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    options.add_session_config_entry("session.inter_op.allow_spinning", "0")
    options.log_severity_level = 4
    session = ort.InferenceSession(model, sess_options=options, providers=[CUDA_PROVIDER])
    if session.get_providers()[0] != CUDA_PROVIDER:
        raise RuntimeError("CUDA provider did not initialize")
    session.disable_fallback()
    return Kokoro.from_session(session, voices), handles


def _reply(out, header, payload=b""):
    out.write(json.dumps(header).encode("ascii") + b"\n")
    out.write(payload)
    out.flush()


def _serve(source, out, args):
    try:
        import numpy as np

        kokoro, _cuda_handles = _load(*args)
    except Exception:
        _reply(out, {"ok": False})
        return 1
    _reply(out, {"ready": True})
    while line := source.readline(MAX_REQUEST + 1):
        if len(line) > MAX_REQUEST or not line.endswith(b"\n"):
            while line and not line.endswith(b"\n"):
                line = source.readline(MAX_REQUEST + 1)
            _reply(out, {"ok": False})
            continue
        request_id = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or set(request) != {"id", "text", "speed"}:
                raise ValueError("invalid request")
            if type(request["id"]) is not int or request["id"] <= 0:
                raise ValueError("invalid request")
            request_id = request["id"]
            text, speed = request["text"], request["speed"]
            if not isinstance(text, str) or type(speed) not in (int, float) or not math.isfinite(speed):
                raise ValueError("invalid request")
            samples, rate = kokoro.create(text, voice="bm_george", speed=speed, lang="en-gb", trim=False)
            samples = np.asarray(samples, dtype="<f4")
            rate = int(rate)
            if samples.ndim != 1 or rate < 5:
                raise ValueError("expected mono audio")
        except Exception:
            error = {"ok": False}
            if request_id is not None:
                error["id"] = request_id
            _reply(out, error)
            continue
        _reply(out, {"id": request_id, "ok": True, "rate": rate, "samples": len(samples)}, samples.tobytes())
    return 0


def main():
    # The libraries can print during import or synthesis, including native code.
    # Reserve a duplicate of stdout for protocol and discard library diagnostics.
    with os.fdopen(os.dup(sys.stdout.fileno()), "wb") as out, open(os.devnull, "wb") as sink:
        os.dup2(sink.fileno(), sys.stdout.fileno())
        os.dup2(sink.fileno(), sys.stderr.fileno())
        return _serve(sys.stdin.buffer, out, sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
