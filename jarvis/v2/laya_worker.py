"""Private worker entry point, run by the isolated Laya CUDA interpreter."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sys

from jarvis.v2.laya_schema import MAX_FRAME, QUESTION


# Artifact hashes from the official English revision pinned in laya_schema.
_CHECKPOINT_DIGESTS = {
    "config.json": "7add7c041b53f7091e65908553af79ab3f077946e5f2312d510ae13b17c7c134",
    "encoder/config.json": "bf3ab80598fdccf414855a2ce80f22859e4492d06ca8a62ddd1cfb63972f8979",
    "model.safetensors": "891102d372688fc2a094dac56a384bc537b87c63f21f9f3dac0be2b7cbc8d86c",
    "rl_agent_config.json": "ae287b56bbcf5f8c4f4541ae9dfd00c914c4c48b940b8398c3058af37ba92bbd",
    "tokenizer/tokenizer.json": "6c8aaa9a542084f2457eab775d4eeb51f92a70c0fd9de28d5edb0ddec3c08d30",
    "tokenizer/tokenizer_config.json": "50044de60daaa73df97d262e15a40d4faf0160e7d742df64b377877a1320dd12",
}


def _offline_socket_guard(event, args):
    if (event == "socket.__new__" and args[1] not in (socket.AF_UNSPEC, socket.AF_UNIX)
            or event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr",
                         "socket.gethostbyname_ex", "socket.getnameinfo"}):
        raise PermissionError("Laya worker network is disabled")


def _load(checkpoint, device_name):
    sys.addaudithook(_offline_socket_guard)

    import laya
    import torch

    # Require the local path in this process as well. The SDK also accepts Hub IDs.
    if not Path(checkpoint).is_dir():
        raise ValueError("Laya checkpoint directory missing")
    device = torch.device(device_name)
    torch.cuda.synchronize(device)
    model = laya.load(checkpoint, device=str(device), backend="eager",
                      expected_sha256=_CHECKPOINT_DIGESTS)
    torch.cuda.synchronize(device)
    expected = device.index if device.index is not None else torch.cuda.current_device()
    if model.device != torch.device("cuda", expected):
        raise RuntimeError("Laya loaded on an unexpected device")
    if str(model.dtype_for(1)) != "torch." + os.environ["LAYA_CUDA_AMP"]:
        raise RuntimeError("Laya loaded with unexpected CUDA precision")
    return model


def _serve(wire):
    checkpoint, device = sys.argv[1:]
    model = _load(checkpoint, device)

    def send(message):
        wire.write(json.dumps(message, allow_nan=False).encode("ascii") + b"\n")
        wire.flush()

    send({"ready": True})
    while line := sys.stdin.buffer.readline(MAX_FRAME + 1):
        if len(line) > MAX_FRAME or not line.endswith(b"\n"):
            raise ValueError("Invalid Laya request frame")
        request = json.loads(line)
        before = model.cpu_fallback_count
        response = model.predict(request["text"], QUESTION, lang="en")
        if model.cpu_fallback_count != before:
            raise RuntimeError("Laya fell back to CPU")
        scores = response["answers"]["route"]["probabilities"]
        usage = response["usage"]
        if (usage.get("truncated") or "route" in usage.get("truncated_questions", ())
                or usage.get("options", {}).get("route")):
            scores = None
        send({"id": request["id"], "scores": scores})


def main():
    # Keep SDK and native stdout away from the wire. The parent discards stderr.
    with os.fdopen(os.dup(1), "wb") as wire, open(os.devnull, "wb") as sink:
        os.dup2(sink.fileno(), 1)
        _serve(wire)
