"""Detection, not prevention: did a run reach for the network, or the world
outside its snapshot? Reported next to the score.

Best-effort pattern matching over the tool calls' text. The real control is
the sandbox (`rolebench.sandbox`): what these flags catch is an attempt, not
an escape.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import sandbox

# Markers beyond swecompare's: the GitHub CLI and API host.
EXTRA_NETWORK = ("gh ", "api.github")
# An absolute path (not the tail of a relative one, not part of a URL), a
# home-relative one, or a relative one that climbs.
ABS_RE = re.compile(r"(?<![\w.~$}/:-])/(?:[\w.@+=,%-]+/?)+")
HOME_RE = re.compile(r"(?:\$\{HOME\}|\$HOME|(?<![\w/])~)(?:/[\w.@+=,%-]*)*")
UP_RE = re.compile(r"(?<![\w/])(?:[\w.@+-]+/)*\.\.(?:/[\w.@+=,%-]*)*")
ALLOWED_ABS = ("/usr", "/bin", "/lib", "/lib64", "/dev/null", "/dev/stdout", "/dev/stderr", "/dev/stdin",
               "/proc/self", "/tmp", str(sandbox.OPT))


def network_flags(result) -> list[str]:
    from swecompare.bench import audit_network    # the swecompare audit, reused as is
    hits = audit_network(result)
    seen = set(hits)
    for name, args in result.tool_calls:
        blob = args.lower()
        if any(marker in blob for marker in EXTRA_NETWORK):
            hit = f"{name}: {args[:160]}"
            if hit not in seen:
                hits.append(hit)
                seen.add(hit)
    return hits


# File bodies are not paths: an Edit's `new_string` with `import "../x"` is not
# a climb out of the snapshot.
BODY_KEYS = {"content", "new_string", "old_string", "text", "new_source", "edits", "todos", "plan", "items"}


def scan_text(args: str) -> str:
    """The parts of one tool call's arguments that name paths or run commands."""
    try:
        doc = json.loads(args)
    except (json.JSONDecodeError, TypeError):
        return args
    if not isinstance(doc, dict):
        return args
    return "\n".join(str(v) for k, v in doc.items() if k not in BODY_KEYS and isinstance(v, (str, int, float)))


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def escape_flags(result, snap: Path, extra_allowed: tuple[str, ...] = ()) -> list[str]:
    """Paths in tool calls that point outside the snapshot: absolute ones not
    under the snapshot or the system/interpreter trees, anything under `~` or
    `$HOME`, and relative ones whose `..` climbs out of the snapshot (taken
    relative to the snapshot root)."""
    snap_s = os.path.normpath(str(snap))
    roots = [snap_s, os.path.normpath(os.path.realpath(snap)), *map(os.path.normpath, extra_allowed)]
    allowed = roots + list(ALLOWED_ABS)
    hits = []
    for name, args in result.tool_calls:
        args = scan_text(args)
        for raw in HOME_RE.findall(args):
            hits.append(f"{name}: {raw[:160]}")
        for raw in ABS_RE.findall(args):
            path = os.path.normpath(raw)
            if path.startswith("/tmp/claude-") or not any(_within(path, a) for a in allowed):
                hits.append(f"{name}: {raw[:160]}")
        for raw in UP_RE.findall(args):
            path = os.path.normpath(os.path.join(snap_s, raw))
            if not any(_within(path, r) for r in roots):
                hits.append(f"{name}: {raw[:160]}")
    return hits
