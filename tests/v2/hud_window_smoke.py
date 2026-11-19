"""Owner-run smoke test of the new window bridge ops on the real Windows desktop.

Read-only: it lists windows, reads one snapshot, takes one screenshot and
probes the focused control. It sends no click, key or text. Run it yourself
from WSL (it starts the Windows bridge on a private port):

    PYTHONPATH=$PWD .venv/bin/python tests/v2/hud_window_smoke.py

It prints program names, counts and sizes, never window titles, snapshot text
or pixels. Each step prints PASS or the bridge's error, and the run goes on to
the next step, so one failure does not hide the others.
"""
from collections import Counter
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from jarvis import config, desktop  # noqa: E402


def step(name, call):
    try:
        result = call()
    except Exception as exc:  # the point is to see every failure
        print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
        return None
    print(f"PASS  {name}")
    return result


def main():
    from jarvis.desktop_setup import _win_path
    session = desktop.DesktopSession(0)
    session.start()
    port = session._server.getsockname()[1]
    prefix = Path(subprocess.check_output(["wslpath", "-u", config.DESKTOP_BRIDGE_DIR], text=True).strip())
    log = open("/tmp/hud_window_smoke_bridge.log", "w")
    proc = subprocess.Popen([str(prefix / "venv/Scripts/python.exe"), _win_path(ROOT / "windows/bridge.py"),
                             "127.0.0.1", str(port)], stdout=log, stderr=subprocess.STDOUT)

    def ask(op, **fields):
        return session.request(op, require_pair=True, scope="smoke", timeout=30,
                               hud_sensitive=None, **fields)

    try:
        if not session.wait_for_bridge(20):
            print("FAIL  the Windows bridge did not connect and pair. Its output: /tmp/hud_window_smoke_bridge.log")
            return
        step("ping (old op still works)", lambda: ask("ping"))
        listed = step("windows", lambda: ask("windows"))
        windows = (listed or {}).get("windows") or []
        if listed is not None:
            access = Counter(w.get("access") for w in windows)
            apps = Counter(w.get("app") for w in windows)
            print(f"      {len(windows)} windows listed: {dict(access)}")
            print("      programs: " + ", ".join(f"{app} x{n}" for app, n in sorted(apps.items(), key=str)))
        drivable = [w for w in windows if w.get("access") == "drivable"]
        see_only = [w for w in windows if w.get("access") == "see_only"]
        for label, group in (("drivable", drivable), ("see-only", see_only)):
            if not group:
                print(f"SKIP  no {label} window is open")
                continue
            target = group[0]
            print(f"      using the first {label} window: {target.get('app')}")
            snap = step(f"window_snapshot ({label})", lambda: ask("window_snapshot", window=target["window"]))
            if snap is not None:
                print(f"      {len(snap.get('snapshot', '').splitlines())} lines, {len(snap.get('refs', {}))} refs")
            shot = step(f"window_screenshot ({label})", lambda: ask("window_screenshot", window=target["window"]))
            if shot is not None:
                print(f"      image size {shot.get('size')}, {shot.get('bytes')} base64 bytes")
        if drivable:
            step("window_probe focus (may refuse when that window is not in front)",
                 lambda: ask("window_probe", window=drivable[0]["window"], focus=True))
        if see_only and drivable:
            refused = step("input to a see-only window is refused (FAIL here is the expected refusal text)",
                           lambda: ask("window_probe", window=see_only[0]["window"], focus=True))
            if refused is not None:
                print("      NOTE: the see-only window answered a focus probe; tell the lead.")
    finally:
        session.stop()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        print("Bridge output saved to /tmp/hud_window_smoke_bridge.log")


if __name__ == "__main__":
    main()
