"""Open a fixed app, a registered directory, or a public HTTPS URL on Windows."""

from dataclasses import dataclass
import os
from pathlib import Path, PureWindowsPath
import re
import subprocess
from urllib.parse import urlsplit

from jarvis import config, untrusted

from .voice.installation import WSL_INTEROP_SOCKET
from .work import WorkError


class PCError(Exception):
    """A fixed reason safe to return to chat."""


@dataclass(frozen=True)
class App:
    label: str
    aliases: tuple[str, ...]
    argv: tuple[str, ...]


# cmd is used only for fixed start commands. Disable its AutoRun hooks.
_START = ("cmd.exe", "/d", "/c", "start", "")
APPS = {
    "chrome": App("Chrome", ("google chrome", "browser"), (*_START, "chrome")),
    "vscode": App("VS Code", ("vs code", "visual studio code", "code"), (*_START, "code")),
    "spotify": App("Spotify", (), (*_START, "spotify:")),
    "explorer": App("File Explorer", ("file explorer", "files"), ("explorer.exe",)),
    "terminal": App("Windows Terminal", ("windows terminal",), (*_START, "wt")),
    "t3": App("T3 Code", ("t3 code",), ("explorer.exe", r"shell:AppsFolder\com.t3tools.t3code")),
}
# A systemd user service need not inherit Windows entries in PATH.
_WINDOWS_PROGRAMS = {
    "cmd.exe": "/mnt/c/Windows/System32/cmd.exe",
    "explorer.exe": "/mnt/c/Windows/explorer.exe",
    "rundll32.exe": "/mnt/c/Windows/System32/rundll32.exe",
}
_APP_WORDS = re.compile(r"[A-Za-z0-9 .,_-]+")
_APP_PUNCTUATION = str.maketrans("", "", ".,_-")
_APP_NAMES = {alias: name for name, app in APPS.items() for alias in (name, *app.aliases)}
_DNS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_AUTHORITY = re.compile(rf"{_DNS_LABEL}(?:\.{_DNS_LABEL})+\.?(?::[0-9]+)?", re.ASCII | re.IGNORECASE)
_NUMERIC_LABEL = re.compile(r"(?:[0-9]+|0x[0-9a-f]*)")
_LOCAL_SUFFIXES = (".localhost", ".local", ".lan", ".home", ".internal", ".intranet", ".corp",
                   ".localdomain", ".arpa")
_LAUNCH_TIMEOUT = 3
_BAD_URL = "Use a public HTTPS link of at most 2,000 characters."
PHONE_REFUSAL = "PC controls are unavailable on phone-originated turns."


def app_preview(name):
    if _APP_WORDS.fullmatch(name):
        normalized = " ".join(name.lower().translate(_APP_PUNCTUATION).split())
        if normalized in _APP_NAMES:
            key = _APP_NAMES[normalized]
            return {"key": key, "app": APPS[key].label}
    raise PCError(f"Unknown app. Available apps are {', '.join(APPS)}.")


def folder_preview(work, project):
    if work is None:
        raise PCError("The work project registry is unavailable.")
    try:
        registered = work.current_project(project)
    except WorkError as exc:
        raise PCError(str(exc)) from None
    return {"project": registered.name, "path": registered.path}


def link_preview(url):
    if (len(url) > 2_000 or "%00" in url or any(
            char in '\\"' or char.isspace() or untrusted.unseen(char) for char in url)):
        raise PCError(_BAD_URL)
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise PCError(_BAD_URL) from None
    if parsed.scheme != "https" or _AUTHORITY.fullmatch(parsed.netloc) is None or port == 0:
        raise PCError(_BAD_URL)
    host = parsed.hostname.removesuffix(".")
    # Numeric final labels can turn into IPv4 literals in the browser's URL parser.
    if (len(host) > 253 or host.endswith(_LOCAL_SUFFIXES) or _NUMERIC_LABEL.fullmatch(host.rsplit(".", 1)[-1])
            or config.claims_this_machine(host) or config.is_lan_host(host)):
        raise PCError(_BAD_URL)
    return {"url": url, "host": host}


def _windows_path(path):
    try:
        result = subprocess.run(["/usr/bin/wslpath", "-w", path], check=True, capture_output=True,
                                text=True, timeout=_LAUNCH_TIMEOUT)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise PCError("Could not convert the project folder to a Windows path.") from None
    value = result.stdout.removesuffix("\n")
    if not PureWindowsPath(value).is_absolute() or any(char == '"' or untrusted.unseen(char) for char in value):
        raise PCError("Could not convert the project folder to a Windows path.")
    return value


_LAUNCH_ENV = ("PATH", "WSL_INTEROP", "WSL_DISTRO_NAME", "WSLENV", "LANG")


def _environment():
    # A small fixed environment: the daemon's own variables (keys included) stay out of the child.
    env = {name: os.environ[name] for name in _LAUNCH_ENV if name in os.environ}
    interop = env.get("WSL_INTEROP")
    if (not interop or not Path(interop).is_socket()) and WSL_INTEROP_SOCKET.is_socket():
        env["WSL_INTEROP"] = str(WSL_INTEROP_SOCKET)
    return env


def _launch(argv):
    executable, *args = argv
    subprocess.run([_WINDOWS_PROGRAMS[executable], *args], check=True, timeout=_LAUNCH_TIMEOUT,
                   shell=False, cwd="/", env=_environment(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, start_new_session=True)


def _open(argv, success, guard):
    if (refusal := guard()) is not None:
        return refusal
    try:
        _environment()  # the slow part (inspecting interop) happens before the last check
        # Checked again at the last moment: Stop may have arrived meanwhile.
        if (refusal := guard()) is not None:
            return refusal
        _launch(argv)
    except subprocess.TimeoutExpired:
        raise PCError("The Windows launcher timed out.") from None
    except subprocess.CalledProcessError:
        raise PCError("The Windows launcher reported a failure.") from None
    except OSError:
        raise PCError("Windows launch is unavailable through WSL.") from None
    return success


def open_app(preview, guard):
    app = APPS[preview["key"]]
    return _open(app.argv, f"Opened {app.label}.", guard)


def open_folder(preview, guard, *, work):
    project = preview["project"]
    if folder_preview(work, project) != preview:
        raise PCError("The registered folder changed after approval.")
    path = _windows_path(preview["path"])
    return _open(("explorer.exe", path), f"Opened the folder for {project}.", guard)


def open_link(preview, guard):
    url = preview["url"]
    host = link_preview(url)["host"]
    return _open(("rundll32.exe", "url.dll,FileProtocolHandler", url), f"Opened {host} in the browser.", guard)
