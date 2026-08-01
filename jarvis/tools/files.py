"""Filesystem reads and writes.

Writes carry a read-before-write invariant: the agent must read a file before
overwriting it, so it can never clobber content it has not seen.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from .. import config
from . import tool
from .secrets import is_protected, refusal

_seen: dict[str, float] = {}
MAX_READ_CHARS = 40_000

# Jarvis may improve his own code (see skills/self-improve.md), but not the
# layers that gate him: rewriting the approval broker, the permission gate,
# the secrets scrubber, dispatch, or this guard would let a bad edit (or an
# injected instruction) disarm the harness from inside. These change only by
# the owner's hand — including an *approved* run_command, which is the owner
# consenting per edit.
SELF_PROTECTED = frozenset(
    {
        "jarvis/tools/secrets.py",
        "jarvis/tools/files.py",
        "jarvis/tools/__init__.py",
        "jarvis/face/approvals.py",
        "jarvis/permissions.py",
    }
)


def _self_protected(target: Path) -> bool:
    try:
        rel = target.resolve().relative_to(config.REPO_ROOT)
    except ValueError:
        return False
    return str(rel) in SELF_PROTECTED


def _resolve(path: str) -> Path:
    return Path(path).expanduser().resolve()


@tool
def read_file(
    path: Annotated[str, "Path to the file, e.g. ~/notes/todo.md"],
) -> str:
    """Read a UTF-8 text file from disk."""
    target = _resolve(path)
    # Checked before .exists() so the refusal is the same whether or not the
    # file is there, and against both spellings so a symlink cannot launder it.
    if is_protected(path) or is_protected(target):
        return refusal(target.name)
    if not target.exists():
        return f"Error: {target} does not exist."
    if target.is_dir():
        return f"Error: {target} is a directory. Use list_dir instead."

    text = target.read_text(encoding="utf-8", errors="replace")
    _seen[str(target)] = target.stat().st_mtime
    if len(text) > MAX_READ_CHARS:
        return text[:MAX_READ_CHARS] + f"\n\n[truncated at {MAX_READ_CHARS} characters]"
    return text or "[file is empty]"


@tool
def write_file(
    path: Annotated[str, "Path to write"],
    content: Annotated[str, "Full new contents of the file"],
) -> str:
    """Write a text file, creating parent directories as needed.

    An existing file must be read with read_file first.
    """
    target = _resolve(path)
    if _self_protected(target):
        return (
            f"Error: {target.name} is part of Jarvis's safety layer and can "
            "only be changed by the owner by hand. Explain what you wanted "
            "changed and why instead."
        )
    if target.exists():
        stamp = _seen.get(str(target))
        current = target.stat().st_mtime
        if stamp is None:
            return f"Error: {target} already exists. Read it first, then write."
        if stamp != current:
            return f"Error: {target} changed on disk since you read it. Read it again."

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _seen[str(target)] = target.stat().st_mtime
    return f"Wrote {len(content)} characters to {target}."


@tool
def list_dir(
    path: Annotated[str, "Directory to list"] = ".",
) -> str:
    """List the entries in a directory."""
    target = _resolve(path)
    if not target.is_dir():
        return f"Error: {target} is not a directory."

    entries = []
    for item in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if item.name.startswith("."):
            continue
        entries.append(f"{item.name}/" if item.is_dir() else f"{item.name}  ({item.stat().st_size}B)")
    return "\n".join(entries) or "[empty directory]"


@tool
def find_files(
    pattern: Annotated[str, "Glob pattern, e.g. **/*.py"],
    path: Annotated[str, "Directory to search from"] = ".",
) -> str:
    """Find files matching a glob pattern."""
    root = _resolve(path)
    if not root.is_dir():
        return f"Error: {root} is not a directory."

    hits = []
    for match in root.glob(pattern):
        if any(part.startswith(".") for part in match.parts):
            continue
        hits.append(os.path.relpath(match, root))
        if len(hits) >= 200:
            hits.append("[...more matches truncated]")
            break
    return "\n".join(hits) or f"No files matching {pattern!r} under {root}."
