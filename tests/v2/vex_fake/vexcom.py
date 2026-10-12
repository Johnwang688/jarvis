#!/usr/bin/python3 -I
"""A stand-in for VEX's `vexcom.exe` (tests/v2/vex_check.py). Never VEX's
tool, and it touches no device: it records its argv, working folder and
environment *names* to `calls.jsonl` beside itself, then:

* `--user`: the brain's user port — echoes each line typed as `brain> …`
  until its input ends or it is killed;
* `--write`: a download — prints progress, waits while `HOLD` exists beside
  it (so a test can hold a download open), prints `Finished`;
* `--test-info`: the device-info read the tests patch in (the real flag is a
  spike item) — prints text with markup and an escape in it, and hangs while
  `HOLD_INFO` exists beside it.
"""
import json
import os
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
args = sys.argv[1:]
with open(HERE / "calls.jsonl", "a", encoding="utf-8") as log:
    log.write(json.dumps({"tool": "vexcom", "argv": args, "cwd": os.getcwd(),
                          "env": sorted(os.environ), "wslenv": os.environ.get("WSLENV")}) + "\n")
if "--user" in args:
    print("brain user port open", flush=True)
    for line in sys.stdin:
        print("brain> " + line.rstrip("\n"), flush=True)
    sys.exit(0)
if "--write" in args:
    print("Downloading User Program", flush=True)
    until = time.monotonic() + 30
    while (HERE / "HOLD").exists() and time.monotonic() < until:
        time.sleep(0.05)
    print("Finished", flush=True)
    sys.exit(0)
if "--test-info" in args:
    while (HERE / "HOLD_INFO").exists():
        time.sleep(0.05)
    print("Brain name: <b onmouseover=x>BLUE-1</b>\x1b[31m red\x1b[0m", flush=True)
    print("VEXos 1.1.5", flush=True)
    sys.exit(0)
print("vexcom fake: unknown arguments", flush=True)
sys.exit(64)
