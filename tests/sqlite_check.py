"""Synthetic checks for the read-only SQLite tool. Free.

The property that lets query_sqlite stay an ungated safe tool is that the
*engine* refuses writes (mode=ro), not that we asked nicely — so the check
that matters here is a write statement failing against a real file while a
read succeeds. Plus: protected names refused, caps applied, and the tool
reachable through real dispatch.

Run:  .venv/bin/python tests/sqlite_check.py
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis import tools
from jarvis.tools.sqlite import query_sqlite


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "game.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE rules (num TEXT, text TEXT)")
        conn.executemany(
            "INSERT INTO rules VALUES (?, ?)",
            [(f"SG{i}", f"rule text {i}") for i in range(300)],
        )
        conn.commit()
        conn.close()

        # Reads work, with columns, through real dispatch.
        out = tools.dispatch(
            "query_sqlite",
            f'{{"db_path": "{db}", "sql": "SELECT num FROM rules WHERE num=\'SG7\'"}}',
        ).text
        assert "SG7" in out and "num" in out, out

        # Writes fail at the engine, and the file is untouched.
        denied = query_sqlite(str(db), "INSERT INTO rules VALUES ('X', 'y')")
        assert "SQLite error" in denied and "readonly" in denied.lower(), denied
        assert "300" in query_sqlite(str(db), "SELECT count(*) FROM rules")

        # DDL fails the same way.
        assert "SQLite error" in query_sqlite(str(db), "DROP TABLE rules")

        # Row cap announces itself.
        capped = query_sqlite(str(db), "SELECT * FROM rules")
        assert "truncated at 200 rows" in capped

        # Protected names are refused before any open; missing files are clear.
        protected = query_sqlite(str(Path(tmp) / "google_token.json"), "SELECT 1")
        assert "refus" in protected.lower() or "credential" in protected.lower(), protected
        assert "no database file" in query_sqlite(str(Path(tmp) / "nope.db"), "SELECT 1")

    print("ok  sqlite: read-only enforced by engine, caps, protected names, dispatch")
    print("\nall sqlite checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
