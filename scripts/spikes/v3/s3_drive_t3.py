"""Spike S3: drive a running T3 Code server through its `t3 thread` CLI.

Not product code. It starts short test threads in a scratch project, sends each
a trivial message, and records exit codes, latency and the SHAPE of every JSON
answer (key names and value types only: no titles, ids, paths or message text
are written, so the output is safe to keep as a fixture).

    python scripts/spikes/v3/s3_drive_t3.py --t3 <path to t3> --project <scratch dir> --out <dir>
        [--provider codex --model <slug>] [--waiting]

The server must already be running; nothing in its settings is changed.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def shape(value, depth=0):
    if isinstance(value, dict):
        return {key: shape(item, depth + 1) for key, item in sorted(value.items())} if depth < 6 else "object"
    if isinstance(value, list):
        return [shape(value[0], depth + 1)] if value else []
    if value is None:
        return "null"
    return type(value).__name__


def run(t3, args, stdin=None, timeout=300):
    started = time.monotonic()
    proc = subprocess.run([t3, *args], input=stdin, capture_output=True, text=True, timeout=timeout)
    seconds = round(time.monotonic() - started, 2)
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        data = None
    return {"argv": ["t3", *[a if a.startswith("-") or a in ("thread", "start", "send", "show", "list") else "<arg>" for a in args]],
            "exit": proc.returncode, "seconds": seconds, "json": data is not None,
            "shape": shape(data) if data is not None else None,
            "stderr_lines": len(proc.stderr.splitlines())}, data


def enums(data, keys=("status", "runtimeMode", "interactionMode", "providerInstanceId", "model", "creationSource", "createdBy", "role", "kind", "type")):
    found = {}

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in keys and isinstance(item, (str, bool)):
                    found.setdefault(key, set()).add(str(item))
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(data)
    return {key: sorted(values) for key, values in sorted(found.items())}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--t3", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--waiting", action="store_true", help="also start a thread that ends waiting on the owner")
    parser.add_argument("--message", default="Reply with the single word pong and do nothing else.")
    options = parser.parse_args()
    out = Path(options.out)
    out.mkdir(parents=True, exist_ok=True)
    pick = (["--provider", options.provider, "--model", options.model] if options.provider else [])
    label = options.provider or "default"
    steps = {}

    steps["start_wait"], started = run(options.t3, ["thread", "start", "--json", "--wait", "--title", "spike-s3 " + label, *pick, options.project, options.message])
    steps["start_wait"]["enums"] = enums(started)
    thread = None
    if isinstance(started, dict):
        thread = started.get("threadId") or (started.get("thread") or {}).get("threadId") or started.get("id")
    if thread:
        steps["show"], shown = run(options.t3, ["thread", "show", "--json", "--messages", "4", thread])
        steps["show"]["enums"] = enums(shown)
        steps["send_wait"], _ = run(options.t3, ["thread", "send", "--json", "--wait", thread, "Reply with the single word ping."])
        steps["send_nowait"], sent = run(options.t3, ["thread", "send", "--json", thread, "Reply with the single word pang."])
        polls, state = [], None
        for _ in range(60):                                  # how a poller sees "turn ended"
            step, data = run(options.t3, ["thread", "show", "--json", "--messages", "1", thread])
            state = (data or {}).get("status") or ((data or {}).get("thread") or {}).get("status")
            polls.append([step["seconds"], state])
            if state not in ("running", "queued", "starting", None):
                break
            time.sleep(1)
        steps["poll_after_send"] = {"polls": len(polls), "first": polls[0], "last": polls[-1], "show_seconds_max": max(p[0] for p in polls)}
    steps["list"], listed = run(options.t3, ["thread", "list", "--json", "--project", options.project])
    steps["list"]["enums"] = enums(listed)
    if options.waiting:
        ask = "Before doing anything, ask me one multiple-choice question with your question tool about which colour I prefer, then wait for my answer."
        steps["start_waiting"], waiting = run(options.t3, ["thread", "start", "--json", "--wait", "--title", "spike-s3 waiting " + label, *pick, options.project, ask])
        steps["start_waiting"]["enums"] = enums(waiting)
    steps["bad_thread"], _ = run(options.t3, ["thread", "show", "--json", "no-such-thread-id"])
    target = out / f"s3_{label}.json"
    target.write_text(json.dumps(steps, indent=1, sort_keys=True) + "\n")
    print(json.dumps({name: {"exit": step.get("exit"), "seconds": step.get("seconds"), "json": step.get("json")} for name, step in steps.items() if "exit" in step}))
    print("wrote", target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
