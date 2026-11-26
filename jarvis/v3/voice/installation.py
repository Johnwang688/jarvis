"""Owner provisioning and detached Windows helper launch."""

import os
from pathlib import Path
import shutil
import stat
import subprocess
import time

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


def _setup_helper():
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


def _gpu_requirements():
    # The checkout and installed wheel have the same optional requirements.
    import re
    from importlib.metadata import requires

    project = config.REPO_ROOT / "pyproject.toml"
    if project.is_file():
        import tomllib

        with project.open("rb") as source:
            extras = tomllib.load(source)["project"]["optional-dependencies"]
    else:
        metadata = requires("jarvis") or ()
        extras = {
            name: [entry.split(";", 1)[0].strip() for entry in metadata
                   if re.search(rf"""\bextra\s*==\s*['"]{name}['"]""", entry)]
            for name in ("voice-gpu", "voice-gpu-worker")
        }
    # The speech stack uses only GPU ORT, even though Kokoro declares the CPU
    # wheel. Every package is pinned and installed without resolution; cuRAND's
    # build number is the one wildcard (see pyproject.toml).
    worker = extras["voice-gpu-worker"]
    cuda = [entry for entry in extras["voice-gpu"] if entry.startswith("nvidia-")
            and not entry.startswith("nvidia-curand-cu12")]
    pinned = [*worker, *cuda]
    exact = re.compile(r"[\w-]+==[\w.+-]+")
    if (not cuda or "onnxruntime-gpu==1.23.2" not in worker
            or not all(exact.fullmatch(entry) or entry == "nvidia-curand-cu12==10.3.10.*" for entry in pinned)):
        raise ValueError("GPU voice requirements must be exact pins")
    return pinned


def _setup_gpu():
    from .engines import EngineUnavailable
    from .gpu_voice import _INSTALLED, _INSTALLING, worker_python

    deadline = time.monotonic() + 1200
    root = config.V3_DIR / "voice-gpu"
    venv = root / "venv"
    python = venv / "bin" / "python"
    try:
        worker_python(python)
    except EngineUnavailable:
        pass
    else:
        print("GPU voice environment is already installed.")
        return

    smi = shutil.which("nvidia-smi")
    try:
        if smi is None:
            raise FileNotFoundError
        subprocess.run([smi], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=5, shell=False)
    except (OSError, subprocess.SubprocessError):
        print("No GPU was found for voice setup.")
        return
    uv = shutil.which("uv")
    if uv is None:
        print("GPU voice setup needs uv on PATH.")
        return

    try:
        pinned = _gpu_requirements()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("GPU voice folder must be owner-only")
        venv.mkdir(mode=0o700)
        installing = root / _INSTALLING
        try:
            installing.touch(mode=0o600, exist_ok=False)
        except BaseException:
            venv.rmdir()
            raise
        try:
            install = [uv, "pip", "install", "--python", str(python)]
            commands = (
                [uv, "venv", "--python", "3.12", str(venv)],
                [*install, "--no-deps", *pinned],
            )
            for argv in commands:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, 1200)
                subprocess.run(argv, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=remaining, shell=False)
            worker_python(python, provisioning=True)
            venv.chmod(0o700)
            installing.rename(venv / _INSTALLED)
        except BaseException:
            shutil.rmtree(venv)
            installing.unlink()
            raise
    except Exception:
        print("GPU voice setup failed.")
    else:
        print("GPU voice environment installed.")


def setup():
    result = _setup_helper()
    _setup_gpu()
    return result


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
