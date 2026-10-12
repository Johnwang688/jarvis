"""The `/vex/*` and `/projects/{id}/vex*` routes of `docs/hud-api.md` (VX-1),
for the HUD mock (VX-2's view).

Free and hermetic: no VEX tool, no device, no make. A build, a download and
the brain terminal are **rows** in the mock's terminal list (`program`
`"vex-build"`, `"vex-download"`, `"vex-brain"`), whose output the test's fake
PTY plays (`tests/face/hud_v2_terminal_check.FakePty`); the build's record
moves to its end when the test says so (`finish_build`) or, with
`vex["build_auto"]` set, on its second read.

The world's `vex` section (`state(mock)`):
  - `status`: what `GET /vex/status` says (device and brain are computed from
    the rows, as the daemon's are);
  - `projects`: project id → its detection; any other project is "not a VEX
    project", as the daemon says of a folder with no settings file;
  - `builds`: build id → the record; `build_auto` → `{"state", "errors"}`;
  - `bin_fresh`: whether a download finds a program built after its
    sources — false answers the daemon's 409 "…; Build first" (a build that
    succeeds sets it);
  - `stays_stale`: a build that succeeds leaves the program "older than its
    sources" all the same (the HUD then says Rebuild instead of looping);
  - `download_refuse`: sentences the next downloads answer 409 with, in order
    (no `code`); `download_exit`: the download program's exit code;
    `download_delay`: seconds each download waits before it answers;
  - `info` / `info_refuse`: what device info answers.

Owner-only on the real daemon: a request with an `Origin` that is not the
mock's own is 403 here too. Bodies are held to exactly the daemon's keys, so
a HUD that ever sent a path, a program or an argument would be refused.
"""

from __future__ import annotations

import re
import secrets
import time

try:
    from tests.face import hud_v2_mock_terminals as mock_terminals
except ImportError:  # run as a script from tests/face
    import hud_v2_mock_terminals as mock_terminals  # type: ignore[no-redef]

DOWNLOAD_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,19}", re.ASCII)
NOT_VEX = "not a VEX project (no .vscode/vex_project_settings.json)"
BUILD_FIRST = "build/{name}.bin is older than the sources; Build first, then download"
BUILD_FIRST_CODE = "build_first"


def state(mock) -> dict:
    return mock.world.setdefault("vex", {
        "status": {"configured": True, "hint": None, "vexcom_version": "1.0.2.2",
                   "toolchain": {}, "sdks": {"cpp": "V5_20240802_15_00_00", "python": "V5_1_0_1_25"},
                   "extension_version": "0.8.2026020401", "templates": True,
                   "project_folder": "/mnt/c/Users/johnw/Documents/Robotics"},
        "projects": {},
        "builds": {},
        "build_auto": None,
        "bin_fresh": True,
        "stays_stale": False,
        "download_delay": 0,
        "download_refuse": [],
        "download_exit": 0,
        "downloads": [],
        "info": {"text": "", "exit_code": 0},
        "info_refuse": None,
        "brain": None,
    })


def vex_project(pid: str, name: str = "override", **over) -> dict:
    rec = {"project_id": pid, "vex": True, "name": name, "language": "cpp", "slot": 1,
           "sdk_version": "V5_20240802_15_00_00", "printf_float": False, "python_main": None,
           "on_windows_drive": True, "builds": True}
    rec.update(over)
    return rec


def _rows(mock) -> list:
    return mock.world.setdefault("terminals", [])


def _program_row(mock, program: str, title: str, project_id: str | None, **over) -> dict:
    seq = mock.__dict__.get("terminal_seq", 0) + 1
    mock.terminal_seq = seq
    rec = mock_terminals.row(f"{0xb0000000 + seq:08x}", title, project_id=project_id,
                             folder="/mnt/c/Users/johnw/OneDrive/Desktop/override",
                             integration="none", integrated=False, marked=False,
                             program=program, input=program == "vex-brain")
    rec.update(over)
    _rows(mock).append(rec)
    return rec


def _device(mock) -> tuple[str | None, str | None]:
    v = state(mock)
    rows = _rows(mock)
    brain = v["brain"] if any(r["id"] == v["brain"] and not r["exited"] for r in rows) else None
    if brain is None:
        v["brain"] = None
    running = next((r for r in rows if r.get("program") == "vex-download" and not r["exited"]), None)
    if running:
        return "download", None
    return ("brain" if brain else None), brain


def finish_build(mock, build_id: str, state_: str = "succeeded", errors=None, exit_code=None):
    """The build's program ended: its record and its row say so."""
    v = state(mock)
    rec = v["builds"][build_id]
    rec["state"] = state_
    rec["exit_code"] = (0 if state_ == "succeeded" else 2) if exit_code is None else exit_code
    rec["errors"] = list(errors or [])
    rec["ended"] = "2026-10-11T12:00:09+00:00"
    if state_ == "succeeded" and rec["action"] != "clean":
        rec["bin"] = {"path": "build/override.bin", "size": 56288, "mtime": "2026-10-11T12:00:09+00:00"}
        v["bin_fresh"] = not v.get("stays_stale")
    for r in _rows(mock):
        if r["id"] == rec["terminal_id"]:
            r["exited"], r["exit_code"] = True, rec["exit_code"]


def terminal_closing(mock, path: str) -> None:
    """`DELETE /terminals/{id}` of a running build's program: the daemon's exit
    hook ends the build, as a killed make does (a Cancel in the HUD)."""
    parts = [p for p in path.split("/") if p]
    if len(parts) != 2 or parts[0] != "terminals" or "vex" not in mock.world:
        return
    for rec in state(mock)["builds"].values():
        if rec["terminal_id"] == parts[1] and rec["state"] == "running":
            rec.update(state="failed", exit_code=-1, ended="2026-10-11T12:00:09+00:00")
    mock.__dict__.setdefault("closed_terminals", []).append(parts[1])


def handle(h, mock, method: str, path: str, body) -> bool:
    parts = [p for p in path.split("/") if p]
    is_vex = bool(parts) and parts[0] == "vex"
    is_project = len(parts) >= 3 and parts[0] == "projects" and parts[2] == "vex"
    if not (is_vex or is_project):
        return False
    origin = h.headers.get("Origin")
    if origin is not None and origin != f"http://127.0.0.1:{mock.port}":
        h._err(403, "only the owner can do this, from the HUD")
        return True
    v = state(mock)
    body = body if body is not None else {}

    def exactly(keys: set[str], required: set[str]) -> bool:
        if not isinstance(body, dict) or set(body) - keys or required - set(body):
            h._err(400, f"expected exactly {sorted(keys)}")
            return False
        return True

    if is_project:
        pid = parts[1]
        project = next((p for p in mock.world["projects"] if p["id"] == pid), None)
        if project is None:
            h._err(404, f"project {pid} not found")
            return True
        det = v["projects"].get(pid) or {"project_id": pid, "vex": False, "reason": NOT_VEX}
        if parts[3:] == [] and method == "GET":
            h._json(det)
            return True
        if parts[3:] == ["build"] and method == "POST":
            if not exactly({"action"}, {"action"}):
                return True
            if body["action"] not in ("build", "rebuild", "clean"):
                h._err(400, "action must be one of build, rebuild, clean")
                return True
            if not det.get("vex"):
                h._err(409, f"{project['name']} is not a VEX V5 project Jarvis can use: {det.get('reason')}")
                return True
            if not v["status"].get("configured"):
                h._err(409, v["status"].get("hint") or "VEX is not set up: run `jarvis vex setup` in a terminal")
                return True
            if any(b["state"] == "running" and b["project_id"] == pid for b in v["builds"].values()):
                h._err(409, "a build of this project is already running (here or through vexbuild); "
                            "wait for it to finish")
                return True
            row = _program_row(mock, "vex-build", f"vex {body['action']} · {det['name']}", pid)
            bid = secrets.token_hex(4)
            v["builds"][bid] = {"id": bid, "project_id": pid, "action": body["action"], "state": "running",
                                "terminal_id": row["id"], "exit_code": None, "errors": [], "bin": None,
                                "started": "2026-10-11T12:00:00+00:00", "ended": None, "_reads": 0}
            h._json({"build_id": bid, "terminal_id": row["id"]}, 202)
            return True
        if parts[3:] == ["download"] and method == "POST":
            if not exactly({"slot", "name"}, {"slot", "name"}):
                return True
            slot, name = body["slot"], body["name"]
            if type(slot) is not int or not 1 <= slot <= 8:
                h._err(400, "slot must be a whole number from 1 to 8")
                return True
            if not isinstance(name, str) or not DOWNLOAD_NAME.fullmatch(name):
                h._err(400, "name must be 1-20 letters, digits, _ or -, not starting with -")
                return True
            if not det.get("vex"):
                h._err(409, f"{project['name']} is not a VEX V5 project Jarvis can use: {det.get('reason')}")
                return True
            v["downloads"].append({"slot": slot, "name": name, "project_id": pid})
            if v.get("download_delay"):
                time.sleep(v["download_delay"])
            if v["download_refuse"]:
                h._err(409, v["download_refuse"].pop(0))
                return True
            if not v["bin_fresh"]:
                # VX-1's 409 carries its code beside the sentence (`BuildFirst`).
                h._json({"error": BUILD_FIRST.format(name=det["name"]), "code": BUILD_FIRST_CODE}, 409)
                return True
            device, _brain = _device(mock)
            if device == "brain":
                h._err(409, "The brain terminal is using the brain's port; close it first. "
                            "One device action at a time.")
                return True
            if device:
                h._err(409, "A download is running. One device action at a time.")
                return True
            code = v["download_exit"]
            row = _program_row(mock, "vex-download", f"vex download · {det['name']} → slot {slot}", pid,
                               exited=True, exit_code=code)
            v["downloads"][-1]["terminal_id"] = row["id"]
            v["downloads"][-1]["sent"] = True
            mock.emit("vex_device", {"action": "download", "at": "2026-10-11T12:00:10+00:00"})
            h._json({"terminal_id": row["id"]}, 202)
            return True
        h._err(404, "route not found")
        return True

    if parts == ["vex", "status"] and method == "GET":
        device, brain = _device(mock)
        h._json({**v["status"], "device": device, "brain": brain})
        return True
    if len(parts) == 3 and parts[1] == "builds" and method == "GET":
        rec = v["builds"].get(parts[2])
        if rec is None:
            h._err(404, f"build {parts[2]} not found")
            return True
        rec["_reads"] += 1
        auto = v.get("build_auto")
        if auto and rec["state"] == "running" and rec["_reads"] >= 2:
            finish_build(mock, rec["id"], auto.get("state", "succeeded"), auto.get("errors"))
        h._json({k: val for k, val in rec.items() if not k.startswith("_")})
        return True
    if parts == ["vex", "brain"] and method == "POST":
        if not exactly(set(), set()):
            return True
        device, brain = _device(mock)
        if brain:
            h._json({"terminal_id": brain, "started": False})
            return True
        if device:
            h._err(409, "A download is running. One device action at a time.")
            return True
        row = _program_row(mock, "vex-brain", "vex brain", None)
        v["brain"] = row["id"]
        mock.emit("vex_device", {"action": "brain", "at": "2026-10-11T12:00:11+00:00"})
        h._json({"terminal_id": row["id"], "started": True})
        return True
    if parts == ["vex", "device", "info"] and method == "POST":
        if not exactly(set(), set()):
            return True
        if v["info_refuse"]:
            h._err(409, v["info_refuse"])
            return True
        mock.emit("vex_device", {"action": "info", "at": "2026-10-11T12:00:12+00:00"})
        h._json(dict(v["info"]))
        return True
    h._err(404, "route not found")
    return True
