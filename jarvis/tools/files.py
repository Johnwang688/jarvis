"""Filesystem reads and writes.

Writes carry a read-before-write invariant: the agent must read a file before
overwriting it, so it can never clobber content it has not seen.

Reads are **paged and line-numbered**. Both halves matter. Paging is what makes
a long file reachable at all — the old reader cut every file at 40k characters
with no way to ask for the rest, so `CLAUDE.md` (116KB) was permanently visible
only down to a third of the way, and `skills/self-improve.md`'s first
instruction ("read CLAUDE.md first") was quietly reading a truncated file.
Numbering is what makes `edit_file` reliable: the model can see where it is,
and a stated line number is checkable.

`edit_file` is the important one. Rewriting a whole file to change three lines
costs the model the entire file every time, and models drop content when they
re-emit at length — the read-before-write stamp catches *staleness*, never
*truncation*. Anchored replacement makes the cost proportional to the edit.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated

from .. import config
from . import tool
from .secrets import is_protected, refusal

_seen: dict[str, float] = {}

# Per-call ceilings. A read that hits either is not an error and not a dead end:
# the footer says exactly which lines came back and how to ask for the next
# page, so nothing is unreachable the way it used to be.
MAX_READ_CHARS = 40_000
DEFAULT_LINES = 1500

# Jarvis may improve his own code (see skills/self-improve.md), but not the
# layers that gate him: rewriting the approval broker, the permission gate,
# the secrets scrubber, dispatch, or this guard would let a bad edit (or an
# injected instruction) disarm the harness from inside. These change only by
# the owner's hand — including an *approved* run_command, which is the owner
# consenting per edit.
#
# Every write path in this module checks this, and `edit_file` is why the list
# has to be checked in more than one place now: a surgical replacement in
# permissions.py would disarm the gate exactly as thoroughly as overwriting it,
# so a new write tool that forgot this check would be a hole straight through
# the boundary rather than a smaller version of one.
SELF_PROTECTED = frozenset(
    {
        "jarvis/tools/secrets.py",
        "jarvis/tools/files.py",
        "jarvis/tools/__init__.py",
        "jarvis/face/approvals.py",
        "jarvis/permissions.py",
        # Decides which commands run with no human in the loop, and which are
        # refused outright. An agent that could edit these could widen its own
        # reach without anyone being asked — the same reasoning as the gate
        # itself, which is why they join the list rather than sitting beside it.
        "jarvis/rules.py",
        "jarvis/command_review.py",
    }
)

# `%6d\t` — the prefix read_file puts on every line. Matched here so a model
# that pastes a numbered excerpt straight back into edit_file still lands its
# edit instead of failing to match (see `_strip_numbering`).
_NUMBERED = re.compile(r"^( *\d+)\t")
# read_file right-aligns in a 6-wide field, so its prefix is always >= 6
# characters before the tab. Requiring that width is what separates "this is
# read_file output" from "this is a TSV whose first column happens to count
# 1, 2, 3" — which is the difference between a helpful strip and a corrupted
# data file.
_NUMBER_FIELD = 6


def _self_protected(target: Path) -> bool:
    try:
        rel = target.resolve().relative_to(config.REPO_ROOT)
    except ValueError:
        return False
    return str(rel) in SELF_PROTECTED


def _protect_refusal(target: Path) -> str:
    return (
        f"Error: {target.name} is part of Jarvis's safety layer and can "
        "only be changed by the owner by hand. Explain what you wanted "
        "changed and why instead."
    )


def _resolve(path: str) -> Path:
    return Path(path).expanduser().resolve()


def _strip_numbering(text: str, strict: bool = True) -> str | None:
    """`text` with read_file's line-number prefixes removed, or None.

    Returns None unless *every* non-empty line carries a prefix and the numbers
    run consecutively — anything looser would corrupt ordinary content that
    happens to start with a digit and a tab.

    `strict` additionally demands read_file's own 6-wide right-aligned field.
    write_file uses it (a wrong strip there silently mangles a saved file);
    edit_file does not, because there the strip is only accepted when it
    produces a match that was otherwise going to fail outright.
    """
    lines = text.split("\n")
    matches = [(line, _NUMBERED.match(line)) for line in lines if line.strip()]
    if not matches or not all(m for _, m in matches):
        return None
    if strict and any(len(m.group(1)) < _NUMBER_FIELD for _, m in matches):
        return None
    values = [int(m.group(1)) for _, m in matches]
    if any(b - a != 1 for a, b in zip(values, values[1:])):
        return None
    return "\n".join(_NUMBERED.sub("", line) for line in lines)


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


@tool
def read_file(
    path: Annotated[str, "Path to the file, e.g. ~/notes/todo.md"],
    offset: Annotated[int, "First line to read, 1-based. Use with limit to page through a long file."] = 1,
    limit: Annotated[int, "How many lines to read (default 1500)"] = DEFAULT_LINES,
) -> str:
    """Read a UTF-8 text file. Output is line-numbered, `<number>\\ttext`.

    Long files come back one page at a time. If the footer says lines were left
    over, call this again with `offset` set past the last line you got — the
    rest of the file is always reachable that way.

    The line numbers are display only. When passing text to edit_file, use the
    file's own content without the number-and-tab prefix.
    """
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
    if not text:
        return "[file is empty]"

    lines = text.splitlines()
    total = len(lines)
    start = max(1, int(offset or 1))
    if start > total:
        return f"Error: {target} has {total} lines; offset {start} is past the end."
    count = max(1, int(limit or DEFAULT_LINES))
    window = lines[start - 1 : start - 1 + count]

    rendered: list[str] = []
    used = 0
    for i, line in enumerate(window):
        entry = f"{start + i:6d}\t{line}"
        # The character ceiling is a backstop against a file of very long lines;
        # it stops the page early rather than cutting mid-line, so the footer's
        # "resume at" number stays truthful.
        if used + len(entry) > MAX_READ_CHARS and rendered:
            break
        rendered.append(entry)
        used += len(entry) + 1

    last = start + len(rendered) - 1
    body = "\n".join(rendered)
    if start > 1 or last < total:
        body += (
            f"\n\n[lines {start}-{last} of {total}."
            + (
                f" Continue with read_file(path, offset={last + 1})]"
                if last < total
                else " End of file.]"
            )
        )
    return body


@tool
def write_file(
    path: Annotated[str, "Path to write"],
    content: Annotated[str, "Full new contents of the file"],
) -> str:
    """Write a text file whole, creating parent directories as needed.

    An existing file must be read with read_file first. To change part of a
    file that already exists, prefer edit_file — it costs a fraction as much
    and cannot drop the parts you were not changing.
    """
    target = _resolve(path)
    if _self_protected(target):
        return _protect_refusal(target)
    if is_protected(path) or is_protected(target):
        return refusal(target.name)
    if target.exists():
        stamp = _seen.get(str(target))
        current = target.stat().st_mtime
        if stamp is None:
            return f"Error: {target} already exists. Read it first, then write."
        if stamp != current:
            return f"Error: {target} changed on disk since you read it. Read it again."

    # read_file numbers its output, so a whole-file rewrite of something just
    # read is one copy-paste away from baking the line numbers into the file.
    # `_strip_numbering` only fires on a consecutively-numbered block, so this
    # cannot misfire on real content — and the note makes the save visible
    # rather than silent.
    note = ""
    stripped = _strip_numbering(content)
    if stripped is not None and content.strip():
        content = stripped
        note = " (stripped read_file's line numbers from the content)"

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _seen[str(target)] = target.stat().st_mtime
    return f"Wrote {len(content)} characters to {target}.{note}"


@tool
def edit_file(
    path: Annotated[str, "Path to the file to change"],
    old_string: Annotated[
        str,
        "The exact text to replace, copied from the file. Include enough "
        "surrounding lines to make it unique. Do not include read_file's "
        "line-number prefix.",
    ],
    new_string: Annotated[str, "What to put in its place. Empty string deletes it."],
    replace_all: Annotated[
        bool, "Replace every occurrence instead of requiring exactly one"
    ] = False,
) -> str:
    """Replace an exact stretch of text in a file, leaving the rest untouched.

    This is the right way to change a file that already exists. `old_string`
    must match the file byte for byte and must appear exactly once, unless
    replace_all is set — if the match is ambiguous, add more surrounding lines
    rather than guessing.

    The file must have been read with read_file first, the same rule write_file
    follows.
    """
    target = _resolve(path)
    if _self_protected(target):
        return _protect_refusal(target)
    if is_protected(path) or is_protected(target):
        return refusal(target.name)
    if not target.exists():
        return f"Error: {target} does not exist. Use write_file to create it."
    if target.is_dir():
        return f"Error: {target} is a directory."
    if not old_string:
        return "Error: old_string is empty. Use write_file to create or replace a whole file."
    if old_string == new_string:
        return "Error: old_string and new_string are identical — nothing to change."

    stamp = _seen.get(str(target))
    current = target.stat().st_mtime
    if stamp is None:
        return f"Error: {target} has not been read this session. Read it first, then edit."
    if stamp != current:
        return f"Error: {target} changed on disk since you read it. Read it again."

    text = target.read_text(encoding="utf-8", errors="replace")

    hits = text.count(old_string)
    salvaged = ""
    if hits == 0:
        # A model quoting an excerpt back from read_file keeps the numbering
        # more often than not. Retry once without it rather than making the
        # display format a trap.
        stripped = _strip_numbering(old_string, strict=False)
        if stripped is not None and text.count(stripped) > 0:
            old_string = stripped
            hits = text.count(old_string)
            salvaged = " (ignored the line numbers you included)"
        else:
            return (
                f"Error: that text does not appear in {target.name}. It must match "
                "byte for byte, including indentation. Read the file again and "
                "copy the exact lines."
            )
    if hits > 1 and not replace_all:
        first = _line_of(text, text.index(old_string))
        return (
            f"Error: that text appears {hits} times in {target.name} (first at line "
            f"{first}). Include more surrounding lines to identify the one you "
            "mean, or pass replace_all=true to change all of them."
        )

    line = _line_of(text, text.index(old_string))
    target.write_text(text.replace(old_string, new_string), encoding="utf-8")
    _seen[str(target)] = target.stat().st_mtime

    where = f"line {line}" if hits == 1 else f"{hits} places, first at line {line}"
    delta = len(new_string) - len(old_string)
    return f"Edited {target} at {where} ({delta:+d} characters).{salvaged}"


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
