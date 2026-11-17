"""Explicit voice-agent operations from WSL. Never used at daemon startup."""
from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.request import urlopen

from . import config, desktop_setup

PID_FILE = "voice-agent.pid"
STOP_FILE = "voice-agent.stop"
LOG_FILE = Path("logs") / "voice-agent.log"


def _query(pid):
    try:
        result = subprocess.run([
            "powershell.exe", "-NoProfile", "-Command",
            f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
            capture_output=True, text=True, timeout=3)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def running(bridge, *, query=_query) -> int | None:
    try:
        pid = int((Path(bridge) / PID_FILE).read_text().strip())
        line = query(pid) if pid > 0 else None
        return pid if isinstance(line, str) and "voice_agent.py" in line else None
    except (OSError, ValueError):
        return None


def _tail(path, n):
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as f:
            return list(deque(f, maxlen=max(0, n)))
    except OSError:
        return []


def start(bridge, *, popen=subprocess.Popen, query=_query, wait=5.0, echo=print) -> int:
    bridge = Path(bridge)
    python = bridge / "venv" / "Scripts" / "pythonw.exe"
    key = bridge / "voice-link.key"
    if not key.is_file() or not python.is_file():
        echo("Voice agent is not set up; run jarvis voice-link setup.")
        return 1
    pid = running(bridge, query=query)
    if pid is not None:
        echo(f"Voice agent already running (pid {pid}). Log: {bridge / LOG_FILE}")
        return 0
    try:
        (bridge / STOP_FILE).unlink(missing_ok=True)
        win = desktop_setup._win_path
        popen(["cmd.exe", "/c", "start", "", "/b", win(str(python)),
               win(str(config.REPO_ROOT / "windows" / "voice_agent.py")),
               "127.0.0.1", str(config.VOICE_LINK_PORT), win(str(key))],
              cwd="/mnt/c", stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        echo(f"Could not launch voice agent. Log: {bridge / LOG_FILE}")
        for line in _tail(bridge / LOG_FILE, 20):
            echo(line.rstrip("\n"))
        return 1
    deadline = time.monotonic() + max(0, wait)
    while True:
        pid = running(bridge, query=query)
        if pid is not None:
            echo(f"Voice agent running (pid {pid}). Log: {bridge / LOG_FILE}")
            return 0
        if time.monotonic() >= deadline:
            break
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    echo(f"Voice agent did not start. Log: {bridge / LOG_FILE}")
    for line in _tail(bridge / LOG_FILE, 20):
        echo(line.rstrip("\n"))
    return 1


def _kill(pid):
    subprocess.run(["taskkill.exe", "/F", "/PID", str(pid)],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=5, check=True)


def stop(bridge, *, query=_query, kill=_kill, wait=5.0, sleep=time.sleep, echo=print) -> int:
    bridge = Path(bridge)
    pid = running(bridge, query=query)
    if pid is None:
        echo("Voice agent is not running.")
        return 0
    try:
        (bridge / STOP_FILE).write_text("stop\n", encoding="ascii")
        deadline = time.monotonic() + max(0, wait)
        while running(bridge, query=query) == pid:
            if time.monotonic() >= deadline:
                # Re-query the ORIGINAL pid immediately before taskkill. Never
                # kill a replacement process, or trust a stale pid file alone.
                line = query(pid)
                if isinstance(line, str) and "voice_agent.py" in line:
                    kill(pid)
                    echo(f"Voice agent forced to stop (pid {pid}).")
                return 0
            sleep(min(0.1, max(0, deadline - time.monotonic())))
    except (OSError, subprocess.SubprocessError):
        echo("Could not stop voice agent.")
        return 1
    echo(f"Voice agent stopped (pid {pid}).")
    return 0


def _daemon_status():
    try:
        with urlopen("http://127.0.0.1:8405/status", timeout=1) as response:
            value = json.load(response)
        return value.get("voice_link") if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def status(bridge, *, query=_query, echo=print, daemon_status=_daemon_status) -> int:
    bridge = Path(bridge)
    pid = running(bridge, query=query)
    echo(f"Voice agent running (pid {pid})." if pid else "Voice agent is not running.")
    echo(f"Log: {bridge / LOG_FILE}")
    recent = deque(maxlen=5)
    try:
        with (bridge / LOG_FILE).open(encoding="utf-8", errors="replace") as f:
            for line in f:
                if " ERROR " in line or " WARNING " in line:
                    recent.append(line.rstrip("\n"))
    except OSError:
        pass
    for line in recent:
        echo(line)
    value = daemon_status()
    if value is not None:
        echo(f"Daemon voice-link: {json.dumps(value, ensure_ascii=False)}")
    return 0


def _journal(n):
    try:
        result = subprocess.run(["journalctl", "--user", "-u", "jarvis-daemon2", "--no-pager",
                                 "-o", "short-iso", "--grep=voice", "--case-sensitive=no", "-n", str(n)],
                                capture_output=True, text=True, timeout=5)
        return result.stdout.splitlines() if result.returncode == 0 else []
    except (OSError, subprocess.SubprocessError):
        return []


def logs(bridge, n=50, follow=False, *, echo=print, journal=_journal) -> int:
    path = Path(bridge) / LOG_FILE
    echo(f"Agent log: {path}")
    # Keep the same open descriptor between the initial tail and follow, so a
    # line appended while journalctl runs is not skipped.
    stream = None
    try:
        try:
            stream = path.open(encoding="utf-8", errors="replace")
            for line in deque(stream, maxlen=max(0, n)):
                echo(line.rstrip("\n"))
        except OSError:
            echo("No agent log yet.")
        echo("Daemon voice-link journal:")
        for line in journal(max(0, n)):
            echo(line.rstrip("\n"))
        if follow:
            echo("Following agent log (Ctrl+C to stop).")
            while True:
                if stream is None and path.exists():
                    stream = path.open(encoding="utf-8", errors="replace")
                line = stream.readline() if stream else ""
                if line:
                    echo(line.rstrip("\n"))
                else:
                    if stream and (not path.exists() or path.stat().st_ino != os.fstat(stream.fileno()).st_ino
                                   or path.stat().st_size < stream.tell()):
                        stream.close()
                        stream = None
                    time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        if stream:
            stream.close()
    return 0
