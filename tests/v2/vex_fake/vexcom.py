#!/usr/bin/python3 -I
"""A stand-in for VEX's `vexcom.exe` (tests/v2/vex_check.py). Never VEX's
tool, and it touches no device: it records its argv, working folder and
environment *names* to `calls.jsonl` beside itself, then:

* `--user` (with `--quiet`, and maybe a port, as `brain_argv` builds it):
  the brain's user port. Without `--quiet` it first prints a banner, as a
  real download does (`vexcom utility …`); with `PORT_BUSY` beside it, it
  says the port is busy and exits 3; otherwise it prints `brain user port
  open`, echoes each line typed as `brain> …` until its input ends or it is
  killed, and with `STREAM` beside it also streams the owner's telemetry
  shape (`$RUN`, then a `$POSE,…` line every 20 ms) as a program would;
* `--write`: a download — prints the port and banner a real one prints,
  then progress, waits while `HOLD` exists beside it (so a test can hold a
  download open), prints `Finished` (exits 5 early with `FAIL_DOWNLOAD`;
  with `DOWNLOAD_EXIT` beside it, an error and exits with the code it holds);
* `--json` (exactly; vexcom's "system info in json format"): one line of
  JSON with markup in a string and an escape written as `\\u001b`;
* `--programs` (exactly; "shows programs"): a text list with markup and a raw
  escape in it.

Both reads hang while `HOLD_INFO` exists beside it. With `STARTUP_DSR`
beside it, every mode first waits for interop's cursor reply: `--user` (the
brain terminal) hangs for it, printing nothing, as the owner saw on
2026-10-17; every other mode exits 91 after 2 s without it. The output shapes are
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
if (HERE / "STARTUP_DSR").is_file():
    import select
    import termios
    import tty
    attrs = termios.tcgetattr(0)
    tty.setraw(0)
    for part in (b"\x1b", b"[", b"6", b"n"):
        os.write(1, part)
        time.sleep(0.01)
    reply = b""
    until = time.monotonic() + (30 if "--user" in args else 2)
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
    if "--quiet" not in args:
        print("vexcom utility V1.0.2b2 (fake banner)", flush=True)
    if (HERE / "PORT_BUSY").is_file():
        print("fake: the serial port is in use by another program", flush=True)
        sys.exit(3)
    print("brain user port open", flush=True)
    if (HERE / "STREAM").is_file():
        import threading

        def stream():
            print("$RUN,1,fake", flush=True)
            n = 0
            while True:
                n += 1
                print(f"$POSE,{n},{n * 0.5:.1f},{-n * 0.25:.2f},{n % 360}", flush=True)
                time.sleep(0.02)
        threading.Thread(target=stream, daemon=True).start()
    for line in sys.stdin:
        print("brain> " + line.rstrip("\r\n"), flush=True)
    sys.exit(0)
if "--write" in args:
    # The real one's first lines (2026-10-14): the port it picked, then its
    # banner. `DOWNLOAD_PORT` beside it replaces the port line's text.
    port = HERE / "DOWNLOAD_PORT"
    print(port.read_text() if port.exists() else "Device: COM7", flush=True)
    print("vexcom utility V1.0.2b2", flush=True)
    print("Downloading User Program", flush=True)
    if (HERE / "FAIL_DOWNLOAD").exists():
        sys.exit(5)
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
