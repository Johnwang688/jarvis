"""`jarvis vex setup` — find VS Code's VEX tools and record them (VX-1, plan §2.9).

Human-only, like `jarvis auth` and `jarvis desktop setup`: a CLI subcommand at
a terminal, never a tool, so no agent decides which executables the HUD's
Build, Download and brain terminal run. It:

1. looks only in the VEX extension's own folders (`config.VEX_STORAGE_ROOTS`,
   its downloads, and `config.VEX_EXTENSION_ROOTS`, its install), for VS Code
   and Cursor, taking the newest version of each thing it finds;
2. prints what it found by name, version and size — `vexcom`, the toolchain,
   the SDKs, the templates — and checks each path by the very rule the daemon
   applies at every use (`vex.parse_config`), so it never writes a file the
   daemon would refuse;
3. asks where new VEX projects go (V-4: default
   `C:\\Users\\johnw\\Documents\\Robotics`), then y/N, then writes
   `~/.config/jarvis/vex.json` (mode 600) — gate state, refused to every
   agent write tool;
4. offers `~/.local/bin/vexbuild`, the agents' build door (V-2): a
   three-line wrapper around `python -m jarvis.v2.vex build`, whose stem the
   gate asks about unless the owner allowlists exactly it.

Nothing is copied, bundled or run: no VEX program is started here, and
`vexcom --help` (the device-info flag, a spike item) is the owner's to run.
After the extension updates, its version folders change: run this again.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile

from jarvis import config
from . import vex

_VERSION_DIR = re.compile(r"[0-9]+(?:_[0-9]+)*", re.ASCII)
_SDK_DIR = re.compile(r"V5_[0-9]+(?:_[0-9]+)*", re.ASCII)
TOOLCHAIN = {"clang": ("clang", "clang.exe"),
             "ar": ("gcc", "arm-none-eabi-ar.exe"),
             "ld": ("gcc", "arm-none-eabi-ld.exe"),
             "objcopy": ("gcc", "arm-none-eabi-objcopy.exe"),
             "size": ("gcc", "arm-none-eabi-size.exe"),
             "make": ("tools", "make.exe")}


def _numbers(name: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"[0-9]+", name))


def _newest(folder: Path, pattern: re.Pattern) -> Path | None:
    try:
        names = [p for p in folder.iterdir() if pattern.fullmatch(p.name) and p.is_dir()
                 and not p.is_symlink()]
    except OSError:
        return None
    return max(names, key=lambda p: _numbers(p.name)) if names else None


def discover() -> dict:
    """What the extension's folders hold, newest version first. Only the
    configured roots are looked in; nothing is followed through a symlink."""
    found: dict = {}
    for root in vex.storage_roots():
        if not root.is_dir():
            continue
        candidate: dict = {}
        version = _newest(root / "tools" / "vexcom", _VERSION_DIR)
        if version is not None:
            candidate["vexcom"] = str(version / "win32" / "vexcom.exe")
            candidate["vexcom_version"] = version.name.replace("_", ".")
        chain = root / "tools" / "cpp" / "toolchain_win32"
        for key, (sub, name) in TOOLCHAIN.items():
            candidate[key] = str(chain / sub / "bin" / name)
        cpp = _newest(root / "sdk" / "cpp" / "V5", _SDK_DIR)
        if cpp is not None:
            candidate["cpp_sdk"] = str(cpp)
        python = _newest(root / "sdk" / "python" / "V5", _SDK_DIR)
        if python is not None:
            candidate["python_sdk"] = str(python)
        complete = all(os.path.isfile(candidate.get(k, "")) for k in vex.EXE_KEYS) \
            and "cpp_sdk" in candidate
        if complete:
            found = candidate
            break
        found = found or candidate
    for root in vex.extension_roots():
        if not root.is_dir():
            continue
        install = _newest(root, re.compile(r"vexrobotics\.vexcode-[0-9][0-9.]*"))
        if install is None:
            continue
        found["extension_version"] = install.name.split("-", 1)[1]
        templates = install / "resources" / "templates" / "projects" / "V5"
        if templates.is_dir():
            found["templates"] = str(templates)
        break
    return found


def _size(path: str) -> str:
    try:
        return f"{os.path.getsize(path):,} bytes"
    except OSError:
        return "missing"


def _current_folder() -> str:
    try:
        return vex.load_config().project_folder
    except vex.VexError:
        return config.VEX_PROJECT_FOLDER


def write_config(doc: dict) -> Path:
    """`vex.json`, atomically, mode 600, in a folder only the owner reads."""
    path = Path(config.VEX_PATH)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".vex.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, indent=2)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def wrapper_text(python: str | None = None) -> str:
    python = python or sys.executable
    return ("#!/bin/sh\n"
            "# Jarvis's VEX build for agents (decision V-2), written by `jarvis vex setup`.\n"
            "# It builds the VEX C++ project in the current folder with the toolchain named\n"
            "# in ~/.config/jarvis/vex.json, and never touches the robot: download is only\n"
            "# ever the owner's click in the HUD. Its stem is `vexbuild`, so the permission\n"
            "# gate asks unless the owner allowlists exactly this tool.\n"
            f"exec {shlex.quote(python)} -m jarvis.v2.vex build \"$@\"\n")


def write_wrapper(text: str) -> Path:
    path = Path(config.VEXBUILD_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".vexbuild.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o755)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def run(*, ask=input, out=print, tty: bool | None = None) -> int:
    """The interactive setup. -> exit code. Refuses without a terminal: the
    owner answers these questions, not a pipe."""
    if tty is None:
        tty = sys.stdin.isatty() and sys.stdout.isatty()
    if not tty:
        out("jarvis vex setup is the owner's to run, at a terminal; nothing changed.")
        return 1
    found = discover()
    out("Looked in the VEX extension's folders:")
    for root in [*vex.storage_roots(), *vex.extension_roots()]:
        out(f"  {root}{'' if root.is_dir() else '  (absent)'}")
    if "vexcom" not in found and "make" not in found:
        out("\nNo VEX downloads found. Install the VEX Robotics extension in VS Code, open a "
            "VEX project once so it downloads its tools, then run this again.")
        return 1
    out("")
    out(f"vexcom {found.get('vexcom_version', '?')}: {found.get('vexcom', 'missing')} "
        f"({_size(found.get('vexcom', ''))})")
    for key in ("clang", "ar", "ld", "objcopy", "size", "make"):
        out(f"{key:8} {found.get(key, 'missing')} ({_size(found.get(key, ''))})")
    out(f"C++ SDK    {os.path.basename(found.get('cpp_sdk', '')) or 'missing'}")
    out(f"Python SDK {os.path.basename(found.get('python_sdk', '')) or 'none'}")
    templates = found.get("templates")
    count = len([p for p in Path(templates).iterdir() if vex.TEMPLATE_FILE.fullmatch(p.name)]) \
        if templates else 0
    out(f"Templates  {count} VS Code templates" + (f" in {templates}" if templates else ""))
    out(f"Extension  {found.get('extension_version', 'not found')}")
    default = _current_folder()
    answer = ask(f"\nNew VEX projects go in [{default}]: ").strip()
    folder = answer or default
    doc = {"version": 1, **found, "project_folder": folder}
    try:
        vex.parse_config(doc)
    except vex.VexError as exc:
        out(f"\nThese paths would not pass the daemon's own check: {exc}. Nothing changed.")
        return 1
    if not vex.on_windows_drive(folder):
        out(f"Note: {folder} is not on a Windows drive; VEX's toolchain builds only there.")
    if ask(f"\nWrite {config.VEX_PATH} with these paths? [y/N] ").strip().lower() not in ("y", "yes"):
        out("Nothing changed.")
        return 1
    out(f"Wrote {write_config(doc)} (mode 600).")
    text = wrapper_text()
    target = Path(config.VEXBUILD_PATH)
    try:
        current = target.read_text(encoding="utf-8") if target.is_file() else None
    except OSError:
        current = None
    if current == text:
        out(f"{target} is already in place.")
        return 0
    verb = "Replace" if target.exists() else "Write"
    if ask(f"{verb} {target}, the agents' build-only door (asks unless you allowlist it)? "
           "[y/N] ").strip().lower() in ("y", "yes"):
        out(f"Wrote {write_wrapper(text)}.")
    return 0
