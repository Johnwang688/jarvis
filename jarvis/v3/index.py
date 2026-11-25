"""Private, rebuildable SQLite index for transcripts and ingested documents."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time

from jarvis import config
from jarvis.v2 import redact
from ._paths import open_directory


SCHEMA_VERSION = 1
CHUNK_CHARS = 4_000
PAGE_CHUNKS = 5
MAX_RESULTS = 20
MAX_QUERY_CHARS = 1_000
EXCERPT_BEFORE, EXCERPT_AFTER = 120, 240

_SCHEMA = (
    """CREATE TABLE docs (
        id TEXT PRIMARY KEY, source TEXT NOT NULL, ref TEXT NOT NULL,
        title TEXT NOT NULL, ts REAL NOT NULL, meta_json TEXT
    )""",
    "CREATE INDEX docs_recent ON docs(ts DESC, id)",
    """CREATE TABLE chunks (
        id INTEGER PRIMARY KEY, doc_id TEXT NOT NULL REFERENCES docs(id),
        seq INTEGER NOT NULL, part INTEGER NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL,
        ts REAL, UNIQUE(doc_id, seq, part)
    )""",
    """CREATE TABLE cursors (
        file_key TEXT PRIMARY KEY, dev INTEGER NOT NULL, ino INTEGER NOT NULL,
        offset INTEGER NOT NULL, digest BLOB NOT NULL, skipping INTEGER NOT NULL
    )""",
    """CREATE VIRTUAL TABLE chunks_fts USING fts5(
        text, content='chunks', content_rowid='id', tokenize='porter unicode61'
    )""",
    """CREATE VIRTUAL TABLE chunks_trigram USING fts5(
        text, content='chunks', content_rowid='id', tokenize='trigram'
    )""",
)

_SPACE = re.compile(r"\s")
_CODE = re.compile(r"[_/\\()[\]{}.:;=<>+*\x60@$%&|!^~#-]")
_CORRUPTION = {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}


def _private_file(directory: int, name: str, *, create: bool = False) -> None:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT
    try:
        fd = os.open(name, flags, 0o600, dir_fd=directory)
    except FileNotFoundError:
        if create:
            raise
        return
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise ValueError("Index files must be private regular files.")
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def _document_id(source: str, ref: str) -> str:
    identity = json.dumps([source, ref], ensure_ascii=True)
    return hashlib.sha256(identity.encode()).hexdigest()


def _redacted(text: str) -> str | None:
    try:
        clean = redact.for_model(text)
        # SQLite's UTF-8 interface cannot encode escaped lone surrogates.
        return clean.encode("utf-8", errors="replace").decode()
    except Exception:
        return None


def _metadata(meta) -> str | None:
    def clean(value):
        if isinstance(value, str):
            return _redacted(value)
        if isinstance(value, dict):
            return {(_redacted(str(key)) or ""): clean(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [clean(item) for item in value]
        return value

    if meta is None:
        return None
    try:
        # Scrub the original strings before JSON escaping can split a value.
        return json.dumps(clean(meta), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return None


def _chunks(text: str):
    start = 0
    while len(text) - start > CHUNK_CHARS:
        end = start + CHUNK_CHARS
        # Prefer the last whitespace in the window, retaining it for reads.
        boundary = end
        for space in _SPACE.finditer(text, start, end):
            boundary = space.end()
        yield text[start:boundary]
        start = boundary
    if start < len(text):
        yield text[start:]


def _timestamp(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _phrase(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


class Index:
    def __init__(self, path=None):
        if path is None:
            path = os.environ.get("JARVIS_INDEX_DB") or config.V3_DIR / "index.sqlite"
        self.path = Path(path).expanduser().absolute()
        self._lock = threading.RLock()
        with open_directory(self.path.parent, create=True) as directory:
            # A directory lock coordinates initial creation and recovery. SQLite
            # handles subsequent reader snapshots and writer serialization.
            fcntl.flock(directory, fcntl.LOCK_EX)
            if os.fstat(directory).st_uid != os.getuid():
                raise ValueError("The index directory must belong to its owner.")
            os.fchmod(directory, 0o700)
            names = [self.path.name + suffix for suffix in ("", "-wal", "-shm", "-journal")]
            for name in names:
                _private_file(directory, name, create=name == self.path.name)
            try:
                self._db = self._open(directory)
            except sqlite3.DatabaseError as exc:
                if getattr(exc, "sqlite_errorcode", 0) & 0xFF not in _CORRUPTION:
                    raise
                stamp = time.time_ns()
                for name in names:
                    try:
                        os.rename(name, f"{name}.corrupt-{stamp}", src_dir_fd=directory, dst_dir_fd=directory)
                    except FileNotFoundError:
                        continue
                _private_file(directory, self.path.name, create=True)
                self._db = self._open(directory)

    def _open(self, directory: int) -> sqlite3.Connection:
        db = sqlite3.connect(
            f"/proc/self/fd/{directory}/{self.path.name}",
            isolation_level=None, check_same_thread=False, timeout=30,
        )
        db.row_factory = sqlite3.Row
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError("Index schema is newer than this version of Jarvis.")
            if db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                if db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                    raise RuntimeError("The index requires SQLite WAL mode.")
            if version != SCHEMA_VERSION:
                db.execute("BEGIN IMMEDIATE")
                for table in ("chunks_trigram", "chunks_fts", "chunks", "docs", "cursors", "ingest_cursors"):
                    db.execute(f"DROP TABLE IF EXISTS {table}")
                for statement in _SCHEMA:
                    db.execute(statement)
                for table in ("chunks_fts", "chunks_trigram"):
                    db.execute(f"""CREATE TRIGGER {table}_insert AFTER INSERT ON chunks BEGIN
                        INSERT INTO {table}(rowid, text) VALUES (new.id, new.text);
                    END""")
                    db.execute(f"""CREATE TRIGGER {table}_delete AFTER DELETE ON chunks BEGIN
                        INSERT INTO {table}({table}, rowid, text) VALUES ('delete', old.id, old.text);
                    END""")
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                db.commit()
            # An additive table can be created on existing v1 indexes in place.
            db.execute("""CREATE TABLE IF NOT EXISTS ingest_cursors (
                name TEXT PRIMARY KEY, revision INTEGER NOT NULL, cursor_json TEXT NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS docs_source_date ON docs(source, ts, id)")
            return db
        except BaseException:
            db.close()
            raise

    def close(self):
        with self._lock:
            self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def _transaction(self, *, write: bool = False, wait_s: float = 30.0):
        with self._lock:
            self._db.execute(f"PRAGMA busy_timeout={int(wait_s * 1000)}")
            self._db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield self._db
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def add(self, source, ref, *, title, ts, messages, meta=None) -> int:
        """Append messages (role, text, timestamp) and return the chunk count."""
        doc_id = _document_id(source, ref)
        with self._transaction(write=True) as db:
            return self._append(db, doc_id, source, ref, title, ts, messages, meta)

    def write_transaction(self):
        """Open a write transaction for a batch of document and cursor changes."""
        return self._transaction(write=True)

    def replace(self, db, source, ref, *, title, ts, messages, meta=None) -> int:
        """Replace a document and its indexed chunks in the caller's transaction."""
        self.remove(db, source, ref)
        return self._append(db, _document_id(source, ref), source, ref, title, ts, messages, meta)

    @staticmethod
    def remove(db, source, ref) -> None:
        """Remove a document and its indexed chunks in the caller's transaction."""
        doc_id = _document_id(source, ref)
        db.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
        db.execute("DELETE FROM docs WHERE id=?", (doc_id,))

    def get_ingest_cursor(self, name):
        """Read the revision and JSON cursor together, including inside a batch."""
        with self._lock:
            row = self._db.execute("SELECT revision, cursor_json FROM ingest_cursors WHERE name=?", (name,)).fetchone()
        return (0, None) if row is None else (row["revision"], json.loads(row["cursor_json"]))

    def ingest_refs(self, source, *, after, limit):
        """Page through a source's indexed refs in (timestamp, ID) order."""
        position = (-math.inf, "") if after is None else after
        with self._lock:
            return self._db.execute(
                """SELECT ref, ts, id FROM docs
                   WHERE source=? AND (ts, id)>(?, ?)
                   ORDER BY ts, id LIMIT ?""",
                (source, *position, limit),
            ).fetchall()

    @staticmethod
    def save_ingest_cursor(db, name, cursor) -> None:
        """Advance an ingester's opaque checkpoint in the caller's transaction."""
        db.execute("""INSERT INTO ingest_cursors VALUES (?, 1, ?)
                      ON CONFLICT(name) DO UPDATE SET revision=revision+1, cursor_json=excluded.cursor_json""",
                   (name, json.dumps(cursor, allow_nan=False)))

    @staticmethod
    def _append(db, doc_id, source, ref, title, ts, messages, meta=None) -> int:
        safe_source, safe_ref, safe_title = (_redacted(value) for value in (source, ref, title))
        safe_meta = _metadata(meta)

        next_seq = db.execute(
            "SELECT coalesce(max(seq), 0) + 1 FROM chunks WHERE doc_id=?", (doc_id,),
        ).fetchone()[0]
        count = 0
        roles = {}

        def rows():
            nonlocal count, next_seq
            for role, text, stamp in messages:
                if role not in roles:
                    roles[role] = _redacted(role)
                safe_role, safe_text = roles[role], _redacted(text)
                if not safe_role or not safe_text or not safe_text.strip():
                    continue
                if role == "tool":
                    safe_text = safe_text[:2_000]
                for part, chunk in enumerate(_chunks(safe_text)):
                    count += 1
                    yield doc_id, next_seq, part, safe_role, chunk, _timestamp(stamp) if stamp is not None else None
                next_seq += 1

        db.execute("""INSERT INTO docs(id, source, ref, title, ts, meta_json)
                      VALUES (?, ?, ?, ?, ?, ?)
                      ON CONFLICT(id) DO UPDATE SET title=excluded.title,
                        ts=max(docs.ts, excluded.ts), meta_json=coalesce(excluded.meta_json, docs.meta_json)""",
                   (doc_id, safe_source or "", safe_ref or "", safe_title or "", _timestamp(ts), safe_meta))
        db.executemany("INSERT INTO chunks(doc_id, seq, part, role, text, ts) VALUES (?, ?, ?, ?, ?, ?)", rows())
        return count

    @staticmethod
    def _hits(db, table, expression, limit):
        # Rank is FTS5's default bm25. Its MATCH cursor can return the ranked
        # chunks directly. Keep the first chunk for each doc and settle ties
        # using the session timestamp.
        return db.execute(f"""
            WITH scored AS MATERIALIZED (
                SELECT c.doc_id, c.seq, c.id AS chunk_id, f.rank AS score
                  FROM {table}(?) f JOIN chunks c ON c.id=f.rowid
            ), ranked AS (
                SELECT *, row_number() OVER (
                    PARTITION BY doc_id ORDER BY score, seq, chunk_id
                ) AS position FROM scored
            )
            SELECT d.*, r.seq, r.chunk_id FROM ranked r JOIN docs d ON d.id=r.doc_id
             WHERE position=1 ORDER BY r.score, d.ts DESC, d.id LIMIT ?
        """, (expression, limit)).fetchall()

    def discover(self, query, limit=5) -> list[dict]:
        query = query.strip()
        if not query or "\0" in query or len(query) > MAX_QUERY_CHARS:
            return []
        limit = min(MAX_RESULTS, max(1, int(limit)))
        word_query = " ".join(_phrase(term) for term in query.split() if any(c.isalnum() for c in term))
        word = ("chunks_fts", word_query)
        trigram = ("chunks_trigram", _phrase(query) if len(query) >= 3 else "")
        strategies = (trigram, word) if _CODE.search(query) else (word, trigram)
        with self._transaction() as db:
            for table, expression in strategies:
                if expression and (hits := self._hits(db, table, expression, limit)):
                    return [self._discovery(db, hit, table, expression, query) for hit in hits]
        return []

    @staticmethod
    def _discovery(db, hit, table, expression, query) -> dict:
        doc_id, seq = hit["id"], hit["seq"]
        last = db.execute("SELECT max(seq) FROM chunks WHERE doc_id=?", (doc_id,)).fetchone()[0]
        sequences = sorted(set(range(1, 4)) | set(range(last - 2, last + 1)) | set(range(seq - 5, seq + 6)))
        slots = ", ".join("?" for _ in sequences)
        messages = [dict(row) for row in db.execute(f"""SELECT seq, role, text FROM chunks
            WHERE doc_id=? AND part=0 AND seq IN ({slots}) ORDER BY seq""", (doc_id, *sequences))]
        excerpt, text = db.execute(
            f"SELECT snippet({table}, 0, '', '', ' … ', 32), text FROM {table}(?) WHERE rowid=?",
            (expression, hit["chunk_id"]),
        ).fetchone()
        # A trigram snippet is a few dozen characters and can stop inside the match: cut around the words themselves.
        at = text.casefold().find(query.casefold())
        if at != -1 and len(text.casefold()) == len(text):
            start, end = max(0, at - EXCERPT_BEFORE), at + len(query) + EXCERPT_AFTER
            excerpt = ("… " if start else "") + text[start:end] + (" …" if end < len(text) else "")
        for message in messages:
            if message["seq"] == seq:
                message.update(text=text, excerpt=excerpt)
                break
        return {**hit, "messages": messages}

    def scroll(self, doc, around, limit) -> dict | None:
        radius = min(MAX_RESULTS, max(1, int(limit)))
        around = max(1, min(int(around), 2**63 - 1 - radius))
        return self._view(doc, "AND seq BETWEEN ? AND ? AND part=0", (around - radius, around + radius))

    def read(self, doc, page=1) -> dict | None:
        page = max(1, min(int(page), 2**63 // PAGE_CHUNKS))
        result = self._view(doc, "", (), "LIMIT ? OFFSET ?", (PAGE_CHUNKS + 1, (page - 1) * PAGE_CHUNKS))
        if result is not None:
            result["next_page"] = page + 1 if len(result["messages"]) > PAGE_CHUNKS else None
            result["messages"] = result["messages"][:PAGE_CHUNKS]
        return result

    def _view(self, doc, condition, args, tail="", tail_args=()) -> dict | None:
        with self._transaction() as db:
            header = db.execute("SELECT * FROM docs WHERE id=?", (doc,)).fetchone()
            if header is None:
                return None
            messages = db.execute(f"""SELECT seq, part, role, text FROM chunks WHERE doc_id=?
                                       {condition} ORDER BY seq, part {tail}""", (doc, *args, *tail_args))
            return {**header, "messages": [dict(row) for row in messages]}

    def recent(self, limit) -> list[dict]:
        limit = min(MAX_RESULTS, max(1, int(limit)))
        with self._transaction() as db:
            return [dict(row) for row in db.execute("SELECT * FROM docs ORDER BY ts DESC, id LIMIT ?", (limit,))]
