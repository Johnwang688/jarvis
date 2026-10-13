"""Safe display labels and opt-in transcript tool rows. Never result text."""
from __future__ import annotations

import os
from pathlib import Path

from jarvis.tools import secrets
from .approvals import clean_line

_KEYS = ("command", "cmd", "file_path", "notebook_path", "path", "paths",
         "pattern", "url", "query", "description", "terminal", "name")
_PATH_KEYS = frozenset(("file_path", "notebook_path", "path", "paths"))


def _path(value: str, cwd: str) -> str:
    # Lexical paths only: displaying a label must not follow symlinks or
    # depend on whether a model-supplied filename exists.
    if not cwd or not os.path.isabs(value):
        return value
    try:
        return str(Path(os.path.normpath(value)).relative_to(os.path.abspath(cwd)))
    except ValueError:
        return value


def label(name, args, cwd) -> str:
    """First present display argument, scrubbed before one-line cleaning.

    Only the allowlisted fields are read; patches and file contents are not
    even inspected. A present empty string is still the selected argument.
    """
    args = args if isinstance(args, dict) else {}
    text = str(name or "")
    for key in _KEYS:
        if key not in args:
            continue
        value = args[key]
        if isinstance(value, str):
            text = _path(value, cwd) if key in _PATH_KEYS else value
        elif key == "paths" and isinstance(value, (list, tuple)):
            text = ", ".join(_path(p, cwd) for p in value if isinstance(p, str))
        else:
            continue
        if key == "pattern" and isinstance(args.get("path"), str) and args["path"]:
            text += " in " + _path(args["path"], cwd)
        break
    return clean_line(secrets.scrub(text), 120)


def transcript_tools(rows, cwd, running_turn):
    """Index tool entries by start-row position, pairing within their turn.

    Labels saved by the daemon are reused. Only the newest 500 starts may
    compute a missing label, bounding the credential-file reads per request.
    """
    starts = [i for i, row in enumerate(rows) if row.get("kind") == "tool_started"]
    recent = set(starts[-500:])
    finishes = {(row.get("turn_id"), (row.get("data") or {}).get("call_id")): row
                for row in rows if row.get("kind") == "tool_finished"}
    entries = {}
    for i in starts:
        row = rows[i]
        data = row.get("data") or {}
        turn = row.get("turn_id")
        finish = finishes.get((turn, data.get("call_id")))
        name = data.get("name", "")
        display = data.get("label")
        if not isinstance(display, str):
            display = label(name, data.get("args"), cwd) if i in recent else clean_line(name, 40)
        entry = dict(role="tool", call_id=data.get("call_id"), name=name, label=display,
                     outcome="running" if running_turn is not None and turn == running_turn else "stopped",
                     turn_id=turn, at=row.get("at", row.get("t")))
        if finish is not None:
            result = finish.get("data") or {}
            outcome = result.get("outcome")
            entry["outcome"] = outcome if outcome in ("ok", "failed", "denied") else (
                "ok" if result.get("ok") else "failed")
            entry["finished_at"] = finish.get("at", finish.get("t"))
        entries[i] = entry
    return entries
