#!/usr/bin/env python3
"""Measure one resident English Laya on unlabelled voice transcripts.

Run with the isolated Laya SDK's Python under the 3 GiB, zero-swap scope.
The runtime JSON requires repo, revision and checkpoint. Optional device
(default cuda), dtype (default checkpoint precision), and expected_sha256
pin the device, CUDA autocast precision, and checkpoint file digests.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import re
import resource
import socket
import sys
from time import perf_counter_ns


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jarvis.v2 import laya_routing


REPO = "convaiinnovations/laya"
MODES = ("assistant", "wake", "dictation")
QUESTION = {
    "route": {
        "type": "choice",
        "instructions": (
            "Classify the speaker's primary intent in this voice transcript. "
            "Ignore filler and courtesy. Base the choice on what they want done."
        ),
        "criteria": {
            "ask": "Give an answer, explanation or comparison in conversation, or help with a task in chat.",
            "navigate": "Visit an explicitly named website in a browser.",
            "open_app": "Launch a named desktop application.",
            "switch_app": "Return to or focus a named open app or window.",
            "search": "Perform a web search for the speaker's query.",
            "read_selection": "Read aloud text already selected on the desktop.",
            "clarify": "The requested outcome is unclear or contradictory.",
        },
    },
}


def read_runtime(path):
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or config.get("repo") != REPO:
        raise ValueError(f"runtime repo must be {REPO}")
    if not re.fullmatch(r"[a-f0-9]{40}", config.get("revision", "")):
        raise ValueError("runtime revision must be the full checkpoint commit SHA")
    checkpoint = (path.resolve().parent / config["checkpoint"]).resolve(strict=True)
    if not checkpoint.is_dir():
        raise ValueError("runtime checkpoint must be a local directory")

    config = dict(config, checkpoint=str(checkpoint), device=config.get("device", "cuda"))
    if not re.fullmatch(r"(?:cuda(?::[0-9]+)?|cpu)", config["device"]):
        raise ValueError("runtime device must be cuda, cuda:N, or cpu")
    dtype = config.get("dtype")
    if dtype not in (None, "bfloat16", "float16", "float32"):
        raise ValueError("runtime dtype must be bfloat16, float16, or float32")
    if config["device"] == "cpu" and dtype not in (None, "float32"):
        raise ValueError("CPU benchmarking uses float32")
    return config


def read_rows(path):
    rows = []
    ids = set()
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if (not isinstance(row, dict) or
                    row.keys() != {"id", "mode", "text", "apps", "windows"} or
                    type(row["id"]) not in (str, int) or row["id"] in ids or
                    row["mode"] not in MODES or not isinstance(row["text"], str) or
                    any(not isinstance(row[key], list) or
                        not all(isinstance(name, str) for name in row[key])
                        for key in ("apps", "windows"))):
                raise ValueError(f"{path}:{line_number}: invalid row or duplicate id")
            ids.add(row["id"])
            rows.append(row)
    if not rows:
        raise ValueError(f"{path}: expected at least one benchmark input")
    return rows


def elapsed_ms(start):
    return (perf_counter_ns() - start) / 1_000_000


def percentile(samples, fraction):
    """Nearest-rank percentile, with null for empty samples."""
    if samples:
        return sorted(samples)[math.ceil(fraction * len(samples)) - 1]
    return None


def run_rows(rows, predict, synchronize):
    reports = []
    for row in rows:
        trace = {"id": row["id"], "model_calls": 0}

        def classify(transcript):
            try:
                synchronize()
                start = perf_counter_ns()
                trace["model_calls"] += 1
                trace["raw_scores"] = None
                try:
                    response = predict(transcript)
                finally:
                    try:
                        synchronize()
                    finally:
                        trace["inference_ms"] = elapsed_ms(start)
                scores = response["answers"]["route"]["probabilities"]
                trace["raw_scores"] = scores
                if set(scores) != set(laya_routing.CHOICES):
                    raise ValueError("Laya returned a different set of choices")
                usage = response["usage"]
                if (usage.get("truncated") or "route" in usage.get("truncated_questions", ()) or
                        usage.get("options", {}).get("route")):
                    raise ValueError("Laya truncated the transcript or collapsed route options")
                return scores
            except Exception as exc:
                trace["classifier_error"] = type(exc).__name__
                raise

        start = perf_counter_ns()
        trace.update(laya_routing.decide(row["mode"], row["text"], classify,
                                        apps=row["apps"], windows=row["windows"]))
        trace["latency_ms"] = elapsed_ms(start)
        reports.append(trace)
    return reports


def timing(reports):
    attempted = [row for row in reports if row["model_calls"]]
    first = attempted[0] if attempted else {}
    # Use the same population as the gate: every model turn after the first.
    warm = attempted[1:]
    return {
        "model_calls": len(attempted),
        "unavailable_rows": sum(row["kind"] == "unavailable" for row in reports),
        "bypass_rows": sum(row["kind"] in ("literal", "dictate") for row in reports),
        "first_inference_ms": first.get("inference_ms"),
        "first_routing_ms": first.get("latency_ms"),
        "warm_samples": len(warm),
        "warm_p50_ms": percentile([row["latency_ms"] for row in warm], 0.50),
        "warm_p95_ms": percentile([row["latency_ms"] for row in warm], 0.95),
        "warm_inference_p95_ms": percentile([row["inference_ms"] for row in warm], 0.95),
    }


def _offline_socket_guard(event, args):
    # Allow local IPC (AF_UNIX) and block network sockets and name lookups.
    if (event == "socket.__new__" and args[1] not in (socket.AF_UNSPEC, socket.AF_UNIX)) or event in {
        "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr",
        "socket.gethostbyname_ex", "socket.getnameinfo",
    }:
        raise PermissionError("network disabled in the offline Laya benchmark")


def load_model(config):
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                      HF_HUB_DISABLE_IMPLICIT_TOKEN="1", HF_HUB_DISABLE_TELEMETRY="1")
    os.environ.pop("LAYA_CPU_AMP", None)
    os.environ.pop("LAYA_CUDA_AMP", None)
    if config["device"].startswith("cuda") and config.get("dtype"):
        os.environ["LAYA_CUDA_AMP"] = config["dtype"]
    sys.addaudithook(_offline_socket_guard)

    start = perf_counter_ns()
    import laya
    import torch

    imports_ms = elapsed_ms(start)
    device = torch.device(config["device"])
    cuda = device.type == "cuda"

    def synchronize():
        if cuda:
            torch.cuda.synchronize(device)

    start = perf_counter_ns()
    if cuda:
        synchronize()
        torch.cuda.reset_peak_memory_stats(device)
    device_init_ms = elapsed_ms(start)

    start = perf_counter_ns()
    model = laya.load(config["checkpoint"], device=str(device), backend="eager",
                      expected_sha256=config.get("expected_sha256"))
    synchronize()
    cold_load_ms = elapsed_ms(start)
    if model.device.type != device.type:
        raise RuntimeError(f"Laya loaded on {model.device}, expected {device}")
    dtype = str(model.dtype_for(1)).removeprefix("torch.")
    if config.get("dtype") and config["dtype"] != dtype:
        raise RuntimeError(f"Laya uses {dtype}, expected {config['dtype']}")

    metadata = {
        "repo": config["repo"],
        "revision": config["revision"],
        "checkpoint": config["checkpoint"],
        "expected_sha256": config.get("expected_sha256"),
        "device": str(model.device),
        "device_name": torch.cuda.get_device_name(device) if cuda else platform.processor() or "CPU",
        "cuda_capability": torch.cuda.get_device_capability(device) if cuda else None,
        "backend": "eager",
        "dtype": dtype,
        "parameter_dtypes": sorted({str(parameter.dtype) for parameter in model.model.parameters()}),
        "versions": {
            "python": platform.python_version(),
            "pytorch": torch.__version__,
            "cuda": torch.version.cuda,
            **{name: importlib.metadata.version(name)
               for name in ("laya", "transformers", "tokenizers", "safetensors")},
        },
        "imports_ms": imports_ms,
        "device_init_ms": device_init_ms,
        "cold_load_ms": cold_load_ms,
    }

    def predict(transcript):
        before = model.cpu_fallback_count
        response = model.predict(transcript, QUESTION, lang="en")
        if model.cpu_fallback_count != before:
            raise RuntimeError("Laya fell back to CPU")
        return response

    def memory():
        return {
            "peak_cuda_allocated_bytes": torch.cuda.max_memory_allocated(device) if cuda else None,
            "peak_cuda_reserved_bytes": torch.cuda.max_memory_reserved(device) if cuda else None,
            "peak_process_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "cpu_fallback_count": model.cpu_fallback_count,
        }

    return predict, synchronize, metadata, memory


def add_legacy_decisions(rows, reports):
    from jarvis.v2.voice_intent import route

    for row, report in zip(rows, reports, strict=True):
        source = "wake" if row["mode"] == "wake" else "hotkey"
        decision = route(row["text"], source)
        report["legacy"] = {key: decision[key] for key in ("kind", "target", "text")}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def json_safe(value):
    """Keep a score trace serializable even when a broken classifier emits NaN."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("--runtime", "--input", "--output"):
        parser.add_argument(flag, required=True, type=Path)
    parser.add_argument("--compare-legacy", action="store_true",
                        help="record the installed baseline router's decision on each input")
    args = parser.parse_args()

    if args.output.resolve() in (args.input.resolve(), args.runtime.resolve()):
        parser.error("--output must differ from --input and --runtime")
    config = read_runtime(args.runtime)
    rows = read_rows(args.input)
    predict, synchronize, metadata, memory = load_model(config)
    reports = run_rows(rows, predict, synchronize)
    metadata.update(timing(reports), **memory())
    metadata.update(
        question=QUESTION,
        language="en",
        min_confidence=laya_routing.MIN_CONFIDENCE,
        min_margin=laya_routing.MIN_MARGIN,
        percentile_method="nearest rank",
        input_sha256=sha256(args.input),
        runtime_sha256=sha256(args.runtime),
        adapter_sha256=sha256(Path(laya_routing.__file__)),
        benchmark_sha256=sha256(Path(__file__)),
        compare_legacy=args.compare_legacy,
    )
    if args.compare_legacy:
        add_legacy_decisions(rows, reports)
    args.output.write_text(
        json.dumps(json_safe({"rows": reports, "metadata": metadata}),
                   allow_nan=False, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
