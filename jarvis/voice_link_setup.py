"""Human-only provisioning; never imported by a model tool or daemon startup."""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import subprocess
import tempfile

from . import config, desktop_setup


def _stage(path, data):
    # The temporary copy is credential-named too, including on /mnt/c.
    # A crash before replacement must not leave an agent-readable key file.
    fd, name = tempfile.mkstemp(prefix=".env.voice-link-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), 0o600)
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
            staged.append((_stage(dest, data), dest))
        for tmp, dest in staged:
            os.replace(tmp, dest)
    finally:
        for tmp, _ in staged:
            tmp.unlink(missing_ok=True)


def setup():
    console = desktop_setup.console
    python = desktop_setup._find_python()
    if python is None:
        console.print("No Windows Python found. Run jarvis desktop setup first.")
        return 1
    converted = subprocess.run(["wslpath", "-u", config.DESKTOP_BRIDGE_DIR],
                               capture_output=True, text=True, timeout=30)
    if converted.returncode or not converted.stdout.strip():
        console.print("Could not locate the Windows bridge folder.")
        return 1
    bridge = Path(converted.stdout.strip())
    venv_python = bridge / "venv" / "Scripts" / "python.exe"
    if not venv_python.is_file():
        console.print("Bridge venv missing. Run jarvis desktop setup first.")
        return 1
    installed = subprocess.run([str(venv_python), "-m", "pip", "install", "--upgrade", "sounddevice", "numpy"],
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
    console.print(rf"Paired. Start {win}\run-voice.cmd on Windows. Hold Right Ctrl, or tap to toggle.")
    console.print("Restart the daemon yourself to load the new pairing. VA-1 displays text only.")
    return 0
