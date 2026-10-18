#!/usr/bin/python3 -I
"""A stand-in for VEX's `vexcom.exe` (tests/v2/vex_check.py). Never VEX's
tool, and it touches no device: it records its argv, working folder and
environment *names* to `calls.jsonl` beside itself, then:

* `--user`: the brain's user port — echoes each line typed as `brain> …`
  until its input ends or it is killed;
* `--write`: a download — prints progress, waits while `HOLD` exists beside
  it (so a test can hold a download open), prints `Finished` (or, with
  `DOWNLOAD_EXIT` beside it, an error and exits with the code it holds);
* `--json` (exactly; vexcom's "system info in json format"): one line of
  JSON with markup in a string and an escape written as `\\u001b`;
* `--programs` (exactly; "shows programs"): a text list with markup and a raw
  escape in it.

Both reads hang while `HOLD_INFO` exists beside it. With `STARTUP_DSR`
beside it, every mode but `--user` first waits for interop's cursor reply. The output shapes are
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
# Model Windows interop's startup handshake (as vex_fake/make.py): with
# `STARTUP_DSR` beside it, nothing is printed until the cursor reply arrives.
if (HERE / "STARTUP_DSR").is_file() and "--user" not in args:
    import select
    import termios
    import tty
    attrs = termios.tcgetattr(0)
    tty.setraw(0)
    for part in (b"\x1b", b"[", b"6", b"n"):
        os.write(1, part)
        time.sleep(0.01)
    reply = b""
    until = time.monotonic() + 2
    while len(reply) < 6 and time.monotonic() < until:
        if select.select([0], [], [], 0.1)[0]:
            reply += os.read(0, 6 - len(reply))
    if reply != b"\x1b[1;1R":
        sys.exit(91)
    termios.tcsetattr(0, termios.TCSANOW, attrs)
    # What the live console wrote around `--usb`'s JSON after the reply
    # (2026-10-14): mode toggles, a window title (OSC 0), cursor show, and
    # the toggles turned off again after the first line.
    os.write(1, b"\x1b[?9001h\x1b[?1004h\x1b[m\x1b]0;C:\\fake\\vexcom.exe\x07\x1b[?25h")
    CONSOLE_OFF = "\x1b[?9001l\x1b[?1004l"
else:
    CONSOLE_OFF = None
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
    exit_file = HERE / "DOWNLOAD_EXIT"
    code = int(exit_file.read_text() or 0) if exit_file.is_file() else 0
    print("Finished" if code == 0 else f"ERROR: exit {code}", flush=True)
    sys.exit(code)
if args == ["--usb"]:
    while (HERE / "HOLD_USB").exists():
        time.sleep(0.05)
    response = HERE / "USB_RESPONSE"
    text = response.read_text() if response.exists() else json.dumps({"usb": {"vex": [
        {"vid": "2888", "pid": "0501", "serialNo": "FAKE-NO-DEVICE"}], "dfu": []}}, indent=2)
    if CONSOLE_OFF:
        first, _, rest = text.partition("\n")
        text = first + "\n" + CONSOLE_OFF + rest
    print(text, flush=True)
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
