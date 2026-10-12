#!/usr/bin/python3 -I
"""A stand-in for VEX's `make.exe` (tests/v2/vex_check.py). Never VEX's tool.

It records how it was started — argv, working folder, and the *names* of its
environment (never a value) — to `calls.jsonl` beside itself, then behaves by
what the test planted in the project:

* `clean` in argv: removes `build/`, prints `clean project`, exits 0;
* `src/SLOW`: sleeps (the timeout check kills it);
* `src/FAIL`: prints that file's text (a fixture build log) and exits 2;
* `HOLD` (beside the makefile): waits until the test removes it;
* `NOOP` (beside the makefile): make found nothing to do — touches nothing,
  exits 0 (the binary keeps its old time); `-B` (a rebuild) ignores it, as
  make -B rebuilds everything;
* otherwise writes `build/<P>.bin` and exits 0.

`src/MARKER`, if present, is printed as `OUTPUT-<MARKER upper-cased>`, so the
leak check can look for output that only ever existed on the terminal.
"""
import json
import os
from pathlib import Path
import shutil
import sys
import time

HERE = Path(__file__).resolve().parent
args = sys.argv[1:]
with open(HERE / "calls.jsonl", "a", encoding="utf-8") as log:
    log.write(json.dumps({"tool": "make", "argv": args, "cwd": os.getcwd(),
                          "env": sorted(os.environ), "wslenv": os.environ.get("WSLENV")}) + "\n")
project = next((a[2:] for a in args if a.startswith("P=")), "project")
src = Path("src")
if (src / "MARKER").is_file():
    print("OUTPUT-" + (src / "MARKER").read_text().strip().upper(), flush=True)
if "clean" in args:
    shutil.rmtree("build", ignore_errors=True)
    print("clean project", flush=True)
    sys.exit(0)
if (src / "SLOW").is_file():
    print("compiling slowly", flush=True)
    time.sleep(60)
while Path("HOLD").is_file():
    time.sleep(0.02)
if (src / "FAIL").is_file():
    sys.stdout.write((src / "FAIL").read_text())
    sys.stdout.flush()
    sys.exit(2)
if Path("NOOP").is_file() and "-B" not in args:
    print("make: Nothing to be done for 'all'.", flush=True)
    sys.exit(0)
Path("build").mkdir(exist_ok=True)
Path("build", f"{project}.bin").write_bytes(b"\x00VEXBIN" * 8)
print(f'"LINK build/{project}.elf"', flush=True)
sys.exit(0)
