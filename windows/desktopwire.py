"""Shared desktop registry and pairing for the Windows and daemon halves."""
from __future__ import annotations

import hmac
import os
from pathlib import Path
import stat

if __package__:
    from . import voicewire as wire
else:
    import voicewire as wire


def private_key(path: Path) -> bytes:
    if os.name == "nt":
        raw = wire.load_key(path)
    else:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
                raise wire.KeyFileError()
            raw = wire.parse_key(stream.read(66))
    return hmac.digest(raw, b"jarvis-desktop-bridge/2", "sha256")


def _hex(value):
    return isinstance(value, str) and wire.HEX64.fullmatch(value) is not None


def prove_daemon(stream, hello, key):
    a = hello.get("nonce")
    if not _hex(a):
        raise wire.LinkError("Invalid desktop bridge hello.")
    d = wire.new_nonce()
    wire.send_json(stream, {"nonce": d, "proof": wire.daemon_proof(key, a, d)})
    answer = wire.read_json(stream, 4096).get("proof")
    if not _hex(answer) or not hmac.compare_digest(wire.agent_proof(key, d, a), answer):
        raise wire.LinkError("Desktop bridge pairing failed.")


def prove_windows(stream, key):
    a = wire.new_nonce()
    wire.send_json(stream, {"hello": "jarvis-desktop-bridge", "version": "2", "nonce": a})
    answer = wire.read_json(stream, 4096)
    d, proof = answer.get("nonce"), answer.get("proof")
    if not _hex(d) or not _hex(proof) or not hmac.compare_digest(wire.daemon_proof(key, a, d), proof):
        raise wire.LinkError("Desktop daemon pairing failed.")
    wire.send_json(stream, {"proof": wire.agent_proof(key, d, a)})


# Both peers resolve the owner's registry locally. A wire request never
# supplies a launch command, executable identity, accessibility backend or URL.
APPS = {
    "settings": {
        "description": "Windows Settings (system, network, personalization, updates)",
        "title": "Settings", "class": "ApplicationFrameWindow", "backend": "uia",
        "processes": ("applicationframehost.exe", "systemsettings.exe"), "maximize": True,
        "launch": {"kind": "uri", "target": "ms-settings:"},
    },
    "claude": {
        "description": "Claude Desktop", "title": "Claude", "class": "Chrome_WidgetWin_1",
        "backend": "msaa", "processes": ("claude.exe",), "maximize": False,
        "launch": {"kind": "aumid", "target": "Claude_pzs8sxrjxfjjc!Claude",
                   "args": "--force-renderer-accessibility"},
    },
}

def launch_name(value):
    """A Start display name only; registered aliases are resolved by the Windows peer."""
    if not isinstance(value, str) or not (0 < len(value.strip()) <= 160):
        return None
    if any(ord(char) < 32 or char in '\\/:"<>|*?' for char in value):
        return None
    name = value.strip().casefold()
    # An explicitly privileged shortcut is outside the user-session launcher.
    words = set("".join(ch if ch.isalnum() else " " for ch in name).split())
    if words & {"administrator", "administrators", "admin", "elevated", "runas"}:
        return None
    return name

NAV_KEYS = {"enter": "{Enter}", "escape": "{Esc}", "tab": "{Tab}", "up": "{Up}",
            "down": "{Down}", "left": "{Left}", "right": "{Right}", "home": "{Home}", "end": "{End}"}
