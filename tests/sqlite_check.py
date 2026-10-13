"""Synthetic checks for the read-only SQLite tool. Free.

The property that lets query_sqlite stay an ungated safe tool is that the
*engine* refuses writes (mode=ro), not that we asked nicely — so the check
that matters here is a write statement failing against a real file while a
read succeeds. Plus: protected names refused, caps applied, and the tool
reachable through real dispatch.

Run:  .venv/bin/python tests/sqlite_check.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis import config, runtime
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
        # ENV-1: a credential env file is refused by name, and through a
        # symlink under an innocent name (`secrets.is_credential`).
        (Path(tmp) / ".env.production").write_text("API_KEY=FAKE-SQLITE-ENV1\n")
        (Path(tmp) / "data.db").symlink_to(".env.production")
        for name in (".env.production", "data.db"):
            refused = query_sqlite(str(Path(tmp) / name), "SELECT 1")
            assert "credential" in refused.lower() and "FAKE-SQLITE" not in refused, (name, refused)

        # The HUD Browser's History is a SQLite file in a credential folder
        # (review of PR #69): refused as written and through a symlink, with
        # a temp BROWSER_DIR and HOME, never the owner's.
        profile = Path(tmp) / "browser" / "profiles" / "p-00000000" / "Default"
        profile.mkdir(parents=True)
        history = sqlite3.connect(profile / "History")
        history.execute("CREATE TABLE urls (url TEXT)")
        history.execute("INSERT INTO urls VALUES ('https://bank.example/BROWSERSECRET')")
        history.commit()
        history.close()
        (Path(tmp) / "visits.db").symlink_to(profile / "History")
        with patch.dict(os.environ, {"HOME": str(Path(tmp) / "home")}), \
                patch.object(config, "BROWSER_DIR", Path(tmp) / "browser"):
            for target in (profile / "History", Path(tmp) / "visits.db"):
                refused = query_sqlite(str(target), "SELECT url FROM urls")
                assert "credential folder" in refused and "BROWSERSECRET" not in refused, (target, refused)
            assert "300" in query_sqlite(str(db), "SELECT count(*) FROM rules")

    print("ok  sqlite: read-only enforced by engine, caps, protected names, credential folders, dispatch")
    print("\nall sqlite checks passed")
    return 0


if __name__ == "__main__":
    # dispatch() refuses when no toolset is bound (PR #34's follow-up), so
    # this suite says what it calls directly — and is held to it.
    with runtime.tools_bound({"query_sqlite"}):
        sys.exit(main())
