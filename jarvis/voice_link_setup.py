"""Human-only provisioning; never imported by a model tool or daemon startup.

The Windows key copy relies on a closed Windows profile folder: name-only
protection has known gaps for copies elsewhere. DrvFS need not support Unix
permission changes; only the WSL copy requires private Unix modes.
"""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import subprocess
import tempfile

from . import config, desktop_setup
from .tools import files


def windows_folder_closed(path) -> bool:
    """Whether a closed profile folder is also refused by the read guard."""
    try:
        root = os.path.realpath(config.windows_users_root()).casefold()
        resolved = os.path.realpath(path)
        parts = Path(resolved.casefold()).relative_to(root).parts
        if not (len(parts) >= 2
                and parts[0] not in {name.casefold() for name in config.WINDOWS_NOT_PROFILES}
                and config.windows_closed_name(parts[1])):
            return False
        # A resolved users root can differ from the read guard's lexical
        # root. Certify only if direct and recursive reads refuse both forms.
        judge = files.credential_folder_judge()
        key = Path(path) / "voice-link.key"
        return all(judge(candidate) for candidate in
                   (path, resolved, key, os.path.realpath(key)))
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


def _stage(path, data):
    # The temporary copy is credential-named too, including on /mnt/c.
    # A crash before replacement must not leave an agent-readable key file.
    fd, name = tempfile.mkstemp(prefix=".env.voice-link-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            # mkstemp creates with mode 0600; do not chmod on Windows/DrvFS.
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        return Path(name)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def write_keys(wsl_path, win_path):
    """Rotate both copies using private temporary files and atomic replacement.

    No key or key-bearing command is printed. Re-pairing requires restarting
    the owner-started agent and daemon so their in-memory copies agree.
    """
    wsl, win = Path(wsl_path), Path(win_path)
    for folder in (wsl.parent, win.parent):
        if folder.is_symlink():
            raise ValueError("Pairing folder must not be a symlink.")
        folder.mkdir(parents=True, exist_ok=True)
    wsl.parent.chmod(0o700)
    data = (secrets.token_hex(32) + "\n").encode("ascii")
    staged = []
    try:
        for dest in (wsl, win):
            tmp = _stage(dest, data)
            staged.append((tmp, dest))
            if dest == wsl:
                tmp.chmod(0o600)
        for tmp, dest in staged:
            os.replace(tmp, dest)
    finally:
        for tmp, _ in staged:
            tmp.unlink(missing_ok=True)


def bridge_dir() -> Path:
    """Locate and certify the same closed Windows folder for all voice verbs."""
    converted = subprocess.run(["wslpath", "-u", config.DESKTOP_BRIDGE_DIR],
                               capture_output=True, text=True, timeout=30)
    if converted.returncode or not converted.stdout.strip():
        raise ValueError("Could not locate the Windows bridge folder.")
    bridge = Path(converted.stdout.strip())
    if not windows_folder_closed(bridge):
        raise ValueError("Windows bridge must be in a closed profile folder (a dot folder or AppData).")
    return bridge


def setup():
    console = desktop_setup.console
    python = desktop_setup._find_python()
    if python is None:
        console.print("No Windows Python found. Run jarvis desktop setup first.")
        return 1
    try:
        bridge = bridge_dir()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        console.print(str(exc))
        return 1
    venv_python = bridge / "venv" / "Scripts" / "python.exe"
    if not venv_python.is_file():
        console.print("Bridge venv missing. Run jarvis desktop setup first.")
        return 1
    installed = subprocess.run([str(venv_python), "-m", "pip", "install", "--upgrade", "sounddevice", "numpy", "sherpa-onnx"],
                               capture_output=True, text=True, timeout=600)
    if installed.returncode:
        console.print("Could not install the voice dependencies.")
        return 1
    try:
        write_keys(config.voice_link_key(), bridge / "voice-link.key")
    except (OSError, ValueError):
        console.print("Could not write the pairing keys.")
        return 1
    src = desktop_setup._win_path(str(config.REPO_ROOT / "windows" / "voice_agent.py"))
    win = config.DESKTOP_BRIDGE_DIR
    (bridge / "run-voice.cmd").write_text(
        "@echo off\r\ntitle Jarvis voice\r\n"
        f'"{win}\\venv\\Scripts\\python.exe" "{src}" 127.0.0.1 '
        f'{config.VOICE_LINK_PORT} "{win}\\voice-link.key"\r\npause\r\n', encoding="utf-8")
    console.print("Paired. Hold Right Ctrl, or tap to toggle. The Windows run-voice.cmd launcher also works.")
    console.print("Restart the daemon yourself to load the new pairing.")
    console.print("Start it with: jarvis voice-link start")
    return 0
