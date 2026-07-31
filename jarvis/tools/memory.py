"""File-backed long-term memory.

One fact per file, plain markdown, stored under config.MEMORY_DIR. Plain files
mean you can read, grep, edit, and version-control what the agent believes
about you — no database, no embeddings.
"""

from __future__ import annotations

import re
from typing import Annotated

from .. import config
from . import tool

SLUG = re.compile(r"[^a-z0-9-]+")


def _path(name: str):
    slug = SLUG.sub("-", name.lower().strip()).strip("-") or "note"
    return config.MEMORY_DIR / f"{slug}.md"


@tool
def memory_list() -> str:
    """List everything in long-term memory, with a one-line summary of each entry."""
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(config.MEMORY_DIR.glob("*.md"))
    if not files:
        return "Memory is empty."

    lines = []
    for item in files:
        first = ""
        for line in item.read_text(encoding="utf-8").splitlines():
            if line.strip():
                first = line.strip().lstrip("# ")
                break
        lines.append(f"{item.stem}: {first[:100]}")
    return "\n".join(lines)


@tool
def memory_read(
    name: Annotated[str, "Entry name, as shown by memory_list"],
) -> str:
    """Read one memory entry in full."""
    target = _path(name)
    if not target.exists():
        return f"No memory entry named {target.stem!r}. Use memory_list to see what exists."
    return target.read_text(encoding="utf-8")


@tool
def memory_write(
    name: Annotated[str, "Short kebab-case name, e.g. 'coffee-preferences'"],
    content: Annotated[str, "The fact to remember, in markdown"],
) -> str:
    """Save or replace a long-term memory entry.

    Use for durable facts: preferences, ongoing projects, corrections the user
    has given. Do not store secrets, passwords, or API keys.
    """
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    target = _path(name)
    existed = target.exists()
    target.write_text(content.rstrip() + "\n", encoding="utf-8")
    return f"{'Updated' if existed else 'Saved'} memory entry {target.stem!r}."


@tool
def memory_search(
    query: Annotated[str, "Text to search for across all memory entries"],
) -> str:
    """Search long-term memory for a word or phrase."""
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    needle = query.lower()
    hits = []
    for item in sorted(config.MEMORY_DIR.glob("*.md")):
        text = item.read_text(encoding="utf-8")
        if needle in text.lower() or needle in item.stem.lower():
            hits.append(f"--- {item.stem} ---\n{text.strip()}")
    return "\n\n".join(hits) or f"Nothing in memory matches {query!r}."


@tool
def memory_delete(
    name: Annotated[str, "Entry name to delete"],
) -> str:
    """Delete a memory entry that is wrong or no longer relevant."""
    target = _path(name)
    if not target.exists():
        return f"No memory entry named {target.stem!r}."
    target.unlink()
    return f"Deleted memory entry {target.stem!r}."
