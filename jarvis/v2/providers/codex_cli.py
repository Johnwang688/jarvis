"""Which Codex CLI the provider runs, and which versions it will talk to.

The adapter speaks the app-server JSON-RPC protocol, so the version matters for
what it sends and reads — not for whether the approval gate holds: every server
request the adapter does not recognise, and every approval it cannot parse, is
refused (codex.py). So the rule is a **floor**, not a pin:

- older than ``CODEX_MIN`` is refused, because nothing below it was verified;
- ``CODEX_MIN`` up to ``CODEX_VERIFIED`` runs quietly;
- newer than ``CODEX_VERIFIED`` runs, with one warning per process naming the
  version, because the CLI auto-updates and a hard pin turned every update
  into "Could not send" (seen live 2026-10-08 on 0.161.0);
- ``config.CODEX_STRICT`` (env ``JARVIS_CODEX_STRICT=1``) restores the exact
  match: only a version in ``VERIFIED`` runs.

The protocol diff that justifies ``CODEX_VERIFIED`` is
``docs/codex-briefs/codex-0.161-protocol-notes.md``.

Resolution, first hit wins: ``config.CODEX_CLI`` (env ``JARVIS_CODEX_CLI``),
then the first ``codex`` on PATH that is not under ``/mnt/`` — the Windows npm
shim lives there and must never run — then ``~/.local/bin/codex``.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import re

from jarvis import config

CODEX_MIN = "0.153.4"
CODEX_VERIFIED = "0.161.0"
# Every version whose generated protocol was checked against the adapter.
VERIFIED = ("0.153.4", "0.161.0")

log = logging.getLogger(__name__)
_announced: set[str] = set()
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z.-]+)?")


# Where WSL mounts Windows drives. The npm shim on this machine is
# /mnt/c/Users/johnw/AppData/Roaming/npm/codex — a Windows program reached
# through interop, which would see Windows paths and a Windows CODEX_HOME.
WINDOWS_ROOTS: tuple[str, ...] = ("/mnt",)
WINDOWS_SUFFIXES = (".cmd", ".bat", ".exe", ".ps1")


def _windows(path: str | Path) -> bool:
    """A Windows binary or shim: under /mnt/ (as named *or* once symlinks are
    followed), or named .cmd/.bat/.exe/.ps1."""
    text = os.path.abspath(str(path))
    real = os.path.realpath(text)
    for candidate in (text, real):
        if any(candidate == root or candidate.startswith(root.rstrip("/") + "/") for root in WINDOWS_ROOTS):
            return True
    return any(name.lower().endswith(WINDOWS_SUFFIXES) for name in (text, real))


def _runnable(path: str | Path) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def local_bin() -> str:
    """The standalone installer's link, the last place looked."""
    return os.path.join(os.path.expanduser("~"), ".local", "bin", "codex")


def resolve() -> tuple[str | None, str]:
    """``(path, "")`` for the binary to run, or ``(None, reason)``.

    The returned path is the one found, not its realpath: the caller resolves
    it once and uses that same file for the version check and the launch, so
    an auto-update between the two cannot swap the binary underneath.
    """
    pinned = (config.CODEX_CLI or "").strip()
    if pinned:
        path = os.path.expanduser(pinned)
        if not os.path.isabs(path):
            return None, f"JARVIS_CODEX_CLI must be an absolute path, not {path!r}"
        if _windows(path):
            return None, f"JARVIS_CODEX_CLI points at a Windows binary ({path}); use the Linux codex"
        if not _runnable(path):
            return None, f"JARVIS_CODEX_CLI is not an executable file: {path}"
        return path, ""
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not os.path.isabs(directory):
            continue  # an empty or relative entry would mean "whatever is in the cwd"
        candidate = os.path.join(directory, "codex")
        if _windows(candidate) or not _runnable(candidate):
            continue
        return candidate, ""
    fallback = local_bin()
    if not _windows(fallback) and _runnable(fallback):
        return fallback, ""
    return None, "codex CLI not found (not on PATH outside /mnt/, not at ~/.local/bin/codex)"


def parse(text: str) -> tuple[int, int, int, int] | None:
    """``codex-cli 0.161.0`` → ``(0, 161, 0, 1)``; a pre-release sorts below
    its release (last element 0). Anything unparseable is None."""
    match = _VERSION.search(text or "")
    if not match:
        return None
    major, minor, patch, pre = match.groups()
    return int(major), int(minor), int(patch), 0 if pre else 1


def version_text(text: str) -> str:
    match = _VERSION.search(text or "")
    return match.group(0) if match else (text or "").strip()[:40] or "unknown"


def check(found: str, path: str) -> tuple[bool, str]:
    """Whether the provider may talk to codex ``found`` at ``path``."""
    version = version_text(found)
    parsed = parse(found)
    if parsed is None:
        return False, f"codex at {path} reported an unreadable version ({version}); refusing it"
    if config.CODEX_STRICT:
        if version not in VERIFIED:
            return False, (f"codex {version} at {path} is not a verified version "
                           f"({', '.join(VERIFIED)}) and JARVIS_CODEX_STRICT=1")
        return True, f"codex {version} ({path}, strict)"
    if parsed < parse(CODEX_MIN):
        return False, (f"codex {version} at {path} is older than {CODEX_MIN}, the oldest "
                       f"version the app-server protocol was verified against; update the Codex CLI")
    if parsed > parse(CODEX_VERIFIED):
        if version not in _announced:
            _announced.add(version)
            log.warning("codex %s at %s is newer than %s, the newest version the app-server "
                        "protocol was verified against; running it (unknown server requests "
                        "and unparseable approvals are refused)", version, path, CODEX_VERIFIED)
        return True, f"codex {version} ({path}; protocol verified up to {CODEX_VERIFIED})"
    if version not in _announced:
        _announced.add(version)
        log.info("codex %s at %s", version, path)
    return True, f"codex {version} ({path})"
