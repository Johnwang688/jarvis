#!/usr/bin/env python3
"""A fake `claude`. Reads control.json beside itself, writes seen.json beside itself. Orchestrator-owned."""
import json
import os
import subprocess
import sys
import time

here = os.path.dirname(os.path.abspath(__file__))
control = json.load(open(os.path.join(here, "control.json")))
if sys.argv[1:] == ["--version"]:
    print(control.get("version", "2.1.325 (Claude Code)"))
    sys.exit(0)
argv = sys.argv[1:]
seen = {"argv": argv, "env": dict(os.environ), "cwd": os.getcwd(), "stdin": sys.stdin.read(), "pid": os.getpid()}
if "--mcp-config" in argv:
    path = argv[argv.index("--mcp-config") + 1]
    info = os.stat(path)
    seen["mcp"] = {"path": path, "mode": info.st_mode & 0o777, "dirmode": os.stat(os.path.dirname(path)).st_mode & 0o777,
                   "text": open(path).read()}
if control.get("grandchild"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    seen["grandchild"] = child.pid
tmp = os.path.join(here, "seen.json.tmp")
json.dump(seen, open(tmp, "w"))
os.replace(tmp, os.path.join(here, "seen.json"))
if control.get("ignore_term"):
    import signal
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(control.get("sleep", 0))
sys.stderr.write(control.get("stderr", ""))
flood = control.get("flood", 0)
while flood > 0:
    sys.stdout.write("x" * 65536)
    flood -= 65536
if "raw" in control:
    sys.stdout.write(control["raw"])
else:
    sys.stdout.write(json.dumps(control.get("result", {
        "type": "result", "subtype": "success", "is_error": False, "result": "done", "session_id": "s-1", "num_turns": 1,
        "total_cost_usd": 0.0012, "duration_ms": 900,
        "usage": {"input_tokens": 10, "cache_creation_input_tokens": 20, "cache_read_input_tokens": 30, "output_tokens": 4}})))
sys.stdout.flush()
sys.exit(control.get("exit", 0))
