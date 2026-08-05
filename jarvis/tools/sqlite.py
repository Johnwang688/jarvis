"""Read-only SQLite queries.

Exists because reference material increasingly arrives as a .db file (the
first was a VEX game manual) and Jarvis had no way to look inside one:
sqlite3 the binary is not installed, and approving arbitrary python -c
one-liners per query is exactly the friction narrow tools exist to remove.

Read-only is enforced by SQLite itself, not by prose: the connection opens
with the `mode=ro` URI flag, so INSERT/UPDATE/CREATE fail at the engine no
matter what SQL arrives — which is what lets this stay a safe tool with no
approval gate. Protected credential files are refused by name before the
open, same rule as read_file.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated

from . import tool
from .secrets import is_protected, refusal

MAX_ROWS = 200
MAX_CHARS = 20_000


@tool
def query_sqlite(
    db_path: Annotated[str, "Absolute path to the .db / .sqlite file"],
    sql: Annotated[str, "One SELECT (or PRAGMA) to run; the database is opened read-only"],
) -> str:
    """Run a read-only query against a local SQLite database.

    The database is opened in SQLite's read-only mode, so only SELECT-style
    statements can succeed. Start with
    `SELECT name FROM sqlite_master WHERE type='table'` to see what's inside.
    Results are capped at 200 rows / 20k characters — narrow the query
    rather than paging through everything.
    """
    path = Path(db_path).expanduser()
    if is_protected(path):
        return refusal(path.name)
    if not path.is_file():
        return f"Error: no database file at {path}"
    try:
        # uri=True + mode=ro: the engine itself refuses writes.
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        return f"Error opening {path.name}: {exc}"
    try:
        cursor = conn.execute(sql)
        columns = [d[0] for d in cursor.description] if cursor.description else []
        rows = cursor.fetchmany(MAX_ROWS + 1)
    except sqlite3.Error as exc:
        return f"SQLite error: {exc}"
    finally:
        conn.close()

    truncated_rows = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    lines = [" | ".join(columns)] if columns else []
    for row in rows:
        lines.append(" | ".join("" if v is None else str(v) for v in row))
    text = "\n".join(lines) if lines else "(no rows)"
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + f"\n[truncated at {MAX_CHARS} chars]"
    if truncated_rows:
        text += f"\n[truncated at {MAX_ROWS} rows — narrow the query]"
    return text
