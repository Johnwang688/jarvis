"""Safe display labels and opt-in transcript tool rows. Never result text."""
from __future__ import annotations

import os
from pathlib import Path

from jarvis.credential_patterns import PATTERNS

from jarvis.tools import secrets
from .approvals import clean_line

_KEYS = ("command", "cmd", "pattern", "file_path", "notebook_path", "path", "paths",
         "url", "query", "description", "terminal", "name")
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


LABEL_SCAN = 4096


def _folder_chain(folder: Path) -> list[Path]:
    """The thread's folder and its parents up to home (or the root), as the
    scrub's own search walks the process's: a parent's `.env` counts too."""
    home = Path.home()
    chain = []
    for current in (folder, *folder.parents):
        chain.append(current)
        if current == home or len(chain) >= 16:
            break
    return chain


def label(name, args, cwd) -> str:
    """First present display argument, scrubbed before one-line cleaning.

    Only the allowlisted fields are read; patches and file contents are not
    even inspected. A present empty string is still the selected argument.
    """
    args = args if isinstance(args, dict) else {}
    text = str(name or "")
    selected_key = None
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
        selected_key = key
        break

    # A label shows 120 characters, so only the head of a long argument is
    # judged; every pass below is then bounded (delta review of PR #84: an
    # unbounded scheme match went quadratic on a 64K token and held the GIL).
    text = text[:LABEL_SCAN]
    if selected_key == "url":
        text = secrets.safe_url(text)
    # Also remove credentials from URLs embedded in commands or other fields
    # (the shared pass `redact_inline` and the logs' `for_display` run too).
    text = secrets.strip_url_credentials(text, query=True)
    text = secrets.scrub(text)
    if cwd:
        values = {value for folder in _folder_chain(Path(cwd))
                  for path in secrets.env_files_in(folder)
                  for value in secrets._values_in(path)}
        for value in sorted(values, key=len, reverse=True):
            text = text.replace(value, secrets.REDACTED)
    # A credential typed into the command (`MY_TOKEN=…`, `Authorization:
    # Bearer …`, `curl -u user:pass`): the value goes, the name stays.
    text = secrets.redact_inline(text)
    for _, pattern in PATTERNS:
        text = pattern.sub("[redacted]", text)
    return clean_line(text, 120)


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
        entry = dict(role="tool", call_id=data.get("call_id"), name=clean_line(name, 40), label=display,
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
