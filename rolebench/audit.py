"""Detection, not prevention: did a run reach for the network, or the world
outside its snapshot? Reported next to the score."""

from __future__ import annotations

import re
from pathlib import Path

from . import config

PATH_RE = re.compile(r"(?:/home/[^\s\"'\;|&)]+|~/[^\s\"'\;|&)]*)")


def network_flags(result) -> list[str]:
    from swecompare.bench import audit_network    # the swecompare audit, reused as is
    return audit_network(result)


def escape_flags(result, snap: Path, extra_allowed: tuple[str, ...] = ()) -> list[str]:
    """Absolute paths in tool calls that are neither inside the snapshot nor
    the interpreter's own tree."""
    py = Path(config.python_bin())
    allowed = [str(snap), str(snap.resolve()), str(py.parent.parent), "/home/johnw/.cache/ms-playwright",
               *extra_allowed]
    hits = []
    for name, args in result.tool_calls:
        for path in PATH_RE.findall(args):
            expanded = str(Path(path).expanduser()) if path.startswith("~") else path
            if not any(expanded.startswith(a) for a in allowed):
                hits.append(f"{name}: {path[:160]}")
    return hits
