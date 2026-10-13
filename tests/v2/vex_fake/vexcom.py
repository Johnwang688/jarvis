#!/usr/bin/python3 -I
"""A stand-in for VEX's `vexcom.exe` (tests/v2/vex_check.py). Never VEX's
tool, and it touches no device: it records its argv, working folder and
environment *names* to `calls.jsonl` beside itself, then:

* `--user`: the brain's user port — echoes each line typed as `brain> …`
  until its input ends or it is killed;
* `--write`: a download — prints progress, waits while `HOLD` exists beside
  it (so a test can hold a download open), prints `Finished`;
* `--json` (exactly; vexcom's "system info in json format"): one line of
  JSON with markup in a string and an escape written as `\\u001b`;
* `--programs` (exactly; "shows programs"): a text list with markup and a raw
  escape in it.

Both reads hang while `HOLD_INFO` exists beside it. The output shapes are
plausible guesses, not vexcom's: the real ones are only seen on a real brain.
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
if args in (["--json"], ["--programs"]):
    while (HERE / "HOLD_INFO").exists():
        time.sleep(0.05)
    if args == ["--json"]:
        print(json.dumps({"vexcom": "1.0.2b2", "devices": [{
            "type": "V5 Brain", "port": "COM5", "name": "<b onmouseover=x>BLUE-1</b>",
            "team": "1234A\u001b[31m red", "vexos": "1.1.5", "battery": 87,
            "slots_used": [1, 3]}]}), flush=True)
    else:
        print("slot  name        language", flush=True)
        print("1     override    cpp", flush=True)
        print("3     <img src=x onerror=y>skills\x1b[31m  python", flush=True)
    sys.exit(0)
print("vexcom fake: unknown arguments", flush=True)
sys.exit(64)
