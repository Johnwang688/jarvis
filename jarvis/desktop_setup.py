"""One-time provisioning for the Windows desktop bridge.

Human-only, and a CLI subcommand rather than a tool — the same rule the auth
flows follow. The agent must not be able to install or start its own bridge:
that is the difference between "the owner gave Jarvis the desktop" and "Jarvis
took it".

What it does: finds Windows Python, builds a venv outside the repo (Windows
Python cannot share the repo's Linux venv), installs the two dependencies, and
writes a .cmd launcher the owner double-clicks or runs at login.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from rich.console import Console

from . import config

console = Console()

REQUIREMENTS = ["uiautomation", "comtypes", "pillow"]

# Candidate locations for a Windows Python, most specific first. `python.exe`
# on PATH is the WSL interop shim and is the usual answer.
CANDIDATES = [
    "python.exe",
    "/mnt/c/Users/johnw/AppData/Local/Programs/Python/Python312/python.exe",
    "/mnt/c/Users/johnw/AppData/Local/Programs/Python/Python313/python.exe",
]


def _win_path(p: str) -> str:
    """Translate a /mnt/c path to a Windows path."""
    out = subprocess.run(["wslpath", "-w", p], capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else p


def _find_python() -> str | None:
    for candidate in CANDIDATES:
        path = shutil.which(candidate) if not candidate.startswith("/") else candidate
        if not path or not Path(path).exists():
            continue
        probe = subprocess.run([path, "-c", "import sys; print(sys.platform)"],
                               capture_output=True, text=True, timeout=60)
        if probe.returncode == 0 and probe.stdout.strip() == "win32":
            return path
    return None


def setup() -> int:
    console.print("[bold]Setting up the Windows desktop bridge[/bold]\n")

    python = _find_python()
    if python is None:
        console.print("[red]No Windows Python found.[/red]")
        console.print(
            "Install it on the Windows side (winget install Python.Python.3.12, "
            "or python.org), then re-run this."
        )
        return 1
    console.print(f"Windows Python: [dim]{python}[/dim]")

    bridge_dir_win = config.DESKTOP_BRIDGE_DIR
    bridge_dir_wsl = Path(
        subprocess.run(["wslpath", "-u", bridge_dir_win],
                       capture_output=True, text=True).stdout.strip()
    )
    bridge_dir_wsl.mkdir(parents=True, exist_ok=True)

    venv_win = rf"{bridge_dir_win}\venv"
    venv_python_wsl = bridge_dir_wsl / "venv" / "Scripts" / "python.exe"

    if not venv_python_wsl.exists():
        console.print(f"Creating venv at [dim]{venv_win}[/dim] …")
        result = subprocess.run([python, "-m", "venv", venv_win],
                                capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            console.print(f"[red]venv creation failed:[/red] {result.stderr[-500:]}")
            return 1
    else:
        console.print("venv already exists.")

    console.print(f"Installing {', '.join(REQUIREMENTS)} …")
    result = subprocess.run(
        [str(venv_python_wsl), "-m", "pip", "install", "--upgrade", *REQUIREMENTS],
        capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        console.print(f"[red]pip install failed:[/red] {result.stdout[-800:]}")
        return 1
    console.print("[green]dependencies installed[/green]")

    # The bridge script itself lives in the repo (it is version-controlled
    # code, not config) and is read from there over the \\wsl.localhost share
    # so edits take effect with no copy step — the same reason `jarvis` is a
    # symlink into the venv rather than an installed copy.
    bridge_src_win = _win_path(str(config.REPO_ROOT / "windows" / "bridge.py"))

    launcher = bridge_dir_wsl / "run-bridge.cmd"
    launcher.write_text(
        "@echo off\r\n"
        "title Jarvis desktop bridge\r\n"
        f'"{venv_win}\\Scripts\\python.exe" "{bridge_src_win}" 127.0.0.1 '
        f"{config.DESKTOP_PORT}\r\n"
        "pause\r\n",
        encoding="utf-8",
    )
    console.print(f"Launcher written: [dim]{config.DESKTOP_BRIDGE_CMD}[/dim]")

    console.print("\n[bold green]Done.[/bold green] To start the bridge, run this "
                  "on the Windows side (not in WSL):\n")
    console.print(f"  [bold]{config.DESKTOP_BRIDGE_CMD}[/bold]\n")
    console.print(
        "[dim]It stays running and reconnects on its own. To start it at login, "
        "put a shortcut to that .cmd in shell:startup.[/dim]"
    )
    console.print("\nRegistered apps Jarvis may drive:")
    for name, spec in sorted(config.DESKTOP_APPS.items()):
        console.print(f"  [bold]{name}[/bold] — {spec.get('description', '')}")
    return 0
