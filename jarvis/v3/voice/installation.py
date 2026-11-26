"""Owner provisioning and detached Windows helper launch."""

import os
from pathlib import Path
import subprocess

from jarvis import config


WSL_INTEROP_SOCKET = Path("/run/WSL/1_interop")


class HelperUnavailable(RuntimeError):
    pass


def _convert(flag, path):
    result = subprocess.run(["wslpath", flag, str(path)], check=True, capture_output=True,
                            text=True, timeout=3)
    value = result.stdout.strip()
    if not value or any(char in value for char in '"\r\n\x00'):
        raise HelperUnavailable("Could not locate the Windows voice installation.")
    return value


def _bridge():
    from jarvis.voice_link_setup import windows_folder_closed

    bridge = Path(_convert("-u", config.DESKTOP_BRIDGE_DIR))
    if not bridge.is_absolute() or not windows_folder_closed(bridge):
        raise HelperUnavailable("The Windows bridge must be in a closed profile folder.")
    return bridge


def _script_path():
    return _convert("-w", config.REPO_ROOT / "windows" / "v3voice_main.py")


def _paired(bridge):
    return config.voice_link_key().is_file() and (bridge / "voice-link.key").is_file()


def setup():
    from jarvis import desktop_setup

    try:
        bridge = _bridge()
        if not _paired(bridge):
            raise HelperUnavailable("Pairing files are missing. Run jarvis voice-link setup.")
        python = bridge / "venv" / "Scripts" / "python.exe"
        if not python.is_file() or desktop_setup._find_python() is None:
            raise HelperUnavailable("Bridge Python is missing. Run jarvis desktop setup.")
        # Escape percent expansion even inside quoted .cmd arguments.
        args = [_convert("-w", python), _script_path(), "127.0.0.1", str(config.VOICE_LINK_PORT),
                _convert("-w", bridge / "voice-link.key")]
        exe, script, host, port, key = [arg.replace("%", "%%") for arg in args]
        subprocess.run([str(python), "-m", "pip", "install", "soundcard", "numpy", "pillow", "livekit"],
                       check=True, capture_output=True, timeout=600)
        (bridge / "run-jarvis-voice.cmd").write_bytes(
            ("@echo off\r\nsetlocal DisableDelayedExpansion\r\n"
             f'"{exe}" "{script}" {host} {port} "{key}"\r\n').encode("utf-8"))
    except HelperUnavailable as error:
        print(str(error))
        return 1
    except (OSError, subprocess.SubprocessError):
        print("Could not set up the Windows voice helper.")
        return 1
    print("Windows voice helper installed. Run jarvis voice start or open run-jarvis-voice.cmd.")
    return 0


def launch(port):
    bridge = _bridge()
    python = bridge / "venv" / "Scripts" / "pythonw.exe"
    key = bridge / "voice-link.key"
    if not _paired(bridge) or not python.is_file() or not (bridge / "run-jarvis-voice.cmd").is_file():
        raise HelperUnavailable("Windows voice setup is missing. Run jarvis voice setup.")
    env = os.environ.copy()
    interop = env.get("WSL_INTEROP")
    if not interop or not Path(interop).is_socket():
        if not WSL_INTEROP_SOCKET.is_socket():
            raise HelperUnavailable("Windows launch is unavailable. Open WSL and retry.")
        env["WSL_INTEROP"] = str(WSL_INTEROP_SOCKET)
    return subprocess.Popen(
        [str(python), _script_path(), "127.0.0.1", str(port), _convert("-w", key)],
        cwd=bridge, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True)
