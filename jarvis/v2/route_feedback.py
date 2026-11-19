"""Local review of Laya voice routes.

Use `python -m jarvis.v2.route_feedback recent`, `correct ID ROUTE`, or
`export` (JSONL on stdout). The default root is the daemon's JARVIS_V2_DATA.
The owner-only database keeps the newest 500 turns, including reviewed turns.
Overlong transcripts are skipped rather than clipped. The caller supplies the
original assistant or wake transcript and the router decision before execution.
Only known route names, numeric scores and a checkpoint hash are taken from that
decision. Keep audio, payloads, selection contents and desktop context upstream.

Each record call represents a new utterance, even if its words match a prior
turn. Keep the returned ID when retrying outcome writes. Outcome status is
observational, and only an explicit correction or review creates a label.
Set JARVIS_ROUTE_FEEDBACK=0 in the daemon environment to pause collection.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sqlite3
import stat
import time
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from windows.voicetype import JARVIS_ADDRESS, POLITE_PREFIX, WAKE_REQUEST, sanitize

from .laya_routing import CHOICES, _literal_payload

LOG = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_MAX_TURNS = 500
_MAX_TEXT = 2000  # Matches the router's grounding limit.
_STATUSES = frozenset({"confirmed", "failed", "clarified", "answered", "cancelled", "unavailable"})
_DECISIONS = (*CHOICES, "unavailable")
_TYPING_KINDS = ("dictate", "dictation", "literal")
# Also reject the legacy bare typing frame, even if a caller reports a
# different route. Use the sanitized form only for the privacy check.
_GREETING = re.compile(r"^(?:hey|hi|hello|yo|okay|ok)[,\s]+", re.IGNORECASE)
_TYPING_FRAME = re.compile(r"^(?:(?:just|now)\s+)*(?:type|dictate|insert)\b", re.IGNORECASE)
_REVISION = re.compile(r"[0-9a-f]{40}")


def _typing_request(text: str) -> bool:
    body = _GREETING.sub("", sanitize(text), count=1)
    body = JARVIS_ADDRESS.sub("", body, count=1)
    body = WAKE_REQUEST.sub("", body, count=1)
    body = POLITE_PREFIX.sub("", body, count=1)
    return bool(_literal_payload(text) or _TYPING_FRAME.match(body))


def _route_fields(decision: Mapping) -> tuple[str, str | None, dict, str | None] | None:
    kind = decision.get("kind")
    proposal = decision.get("proposal")
    if isinstance(proposal, Mapping):
        proposal = proposal.get("kind")
    if (kind not in _DECISIONS or proposal in _TYPING_KINDS
            or decision.get("target") == "literal"):
        return None
    raw = decision.get("raw_scores", decision.get("scores"))
    scores = {}
    if isinstance(raw, Mapping):
        for choice in CHOICES:
            score = raw.get(choice)
            if type(score) in (int, float) and 0 <= score <= 1 and math.isfinite(score):
                scores[choice] = score
    revision = decision.get("revision")
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        revision = None
    return kind, proposal if proposal in CHOICES else None, scores, revision


class RouteFeedback:
    """A concurrency-safe journal under the owner daemon's Stores.root."""

    def __init__(self, root: Path):
        self.path = Path(root) / "route-feedback" / "feedback.sqlite3"

    @contextmanager
    def _db(self, *, create: bool = False) -> Iterator[sqlite3.Connection | None]:
        if not create and not self.path.exists():
            yield None
            return

        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = self.path.parent.lstat()
        if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
                or directory.st_mode & 0o077):
            raise PermissionError("route feedback directory must be private to the owner")

        # Create with private permissions before SQLite can write the first row.
        # A rollback journal lives in the same private directory.
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if create:
            flags |= os.O_CREAT
        with os.fdopen(os.open(self.path, flags, 0o600), "rb") as file:
            info = os.fstat(file.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise PermissionError("route feedback database must be private to the owner")

        with closing(sqlite3.connect(self.path, timeout=0.25)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA secure_delete = ON")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                if not create:
                    yield None
                    return
                db.execute("BEGIN IMMEDIATE")
                db.execute("""
                    CREATE TABLE IF NOT EXISTS turns (
                        id TEXT PRIMARY KEY,
                        created_at INTEGER NOT NULL,
                        mode TEXT NOT NULL,
                        text TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        proposal TEXT,
                        scores TEXT NOT NULL,
                        revision TEXT,
                        schema_version INTEGER NOT NULL,
                        label TEXT
                    )
                """)
                db.execute("""
                    CREATE TABLE IF NOT EXISTS outcomes (
                        turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
                        status TEXT NOT NULL,
                        observed_at INTEGER NOT NULL,
                        PRIMARY KEY (turn_id, status)
                    )
                """)
                db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            elif version != _SCHEMA_VERSION:
                raise sqlite3.DatabaseError("unsupported route feedback database version")
            yield db

    def record(self, mode: str, text: str, decision: Mapping) -> str | None:
        """Record an eligible transcript and return its full, stable turn ID."""
        if (mode not in ("assistant", "wake") or os.environ.get("JARVIS_ROUTE_FEEDBACK") == "0"
                or not isinstance(text, str) or not 0 < len(text) <= _MAX_TEXT
                or not text.strip() or _typing_request(text)
                or not isinstance(decision, Mapping)):
            return None
        fields = _route_fields(decision)
        if fields is None:
            return None
        kind, proposal, scores, revision = fields
        token = uuid4().hex
        try:
            with self._db(create=True) as db:
                db.execute("""
                    INSERT INTO turns (id, created_at, mode, text, kind, proposal, scores,
                                       revision, schema_version)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (token, time.time_ns(), mode, text, kind, proposal, json.dumps(scores),
                      revision, _SCHEMA_VERSION))
                db.execute("""
                    DELETE FROM turns WHERE rowid <= (
                        SELECT rowid FROM turns ORDER BY rowid DESC LIMIT 1 OFFSET ?
                    )
                """, (_MAX_TURNS,))
        except (OSError, sqlite3.Error, UnicodeError):
            LOG.warning("Could not record local route feedback")
            return None
        return token

    def outcome(self, token: str | None, status: str) -> None:
        """Append each supported status at most once for a retained turn."""
        if not token or status not in _STATUSES:
            return
        try:
            with self._db() as db:
                if db is not None:
                    db.execute("""
                        INSERT INTO outcomes (turn_id, status, observed_at)
                        SELECT id, ?, ? FROM turns WHERE id = ?
                        ON CONFLICT (turn_id, status) DO NOTHING
                    """, (status, time.time_ns(), token))
        except (OSError, sqlite3.Error):
            LOG.warning("Could not save local route outcome")

    def correct(self, token: str, kind: str) -> bool:
        """Review the turn with an existing route label (including its original route)."""
        if kind not in CHOICES:
            raise ValueError("choose a route from " + ", ".join(CHOICES))
        with self._db() as db:
            if db is None:
                return False
            changed = db.execute("UPDATE turns SET label = ? WHERE id = ?", (kind, token))
            return changed.rowcount == 1

    def recent(self, limit: int = 20) -> list[dict]:
        """Read newest turns and their outcomes from a single review snapshot."""
        with self._db() as db:
            if db is None:
                return []
            db.execute("BEGIN")
            turns = [dict(row) for row in db.execute(
                "SELECT * FROM turns ORDER BY rowid DESC LIMIT ?", (max(0, limit),)
            )]
            for turn in turns:
                turn["scores"] = json.loads(turn["scores"])
                turn["outcomes"] = [row["status"] for row in db.execute("""
                    SELECT status FROM outcomes WHERE turn_id = ? ORDER BY rowid
                """, (turn["id"],))]
            return turns

    def examples(self) -> Iterator[dict]:
        """Iterate a snapshot of explicit labels without execution metadata."""
        with self._db() as db:
            rows = [] if db is None else db.execute("""
                SELECT id, text, label, mode, schema_version FROM turns
                WHERE label IS NOT NULL ORDER BY rowid
            """).fetchall()
        return (dict(row) for row in rows)


def main(argv: list[str] | None = None) -> int:
    from jarvis import config

    parser = argparse.ArgumentParser(description="Review local Jarvis voice routing feedback.")
    parser.add_argument("--root", type=Path, default=config.V2_DATA_DIR,
                        help="use a different daemon data root")
    commands = parser.add_subparsers(dest="command", required=True)
    recent = commands.add_parser("recent", help="list recent turns and their IDs")
    recent.add_argument("--limit", type=int, default=20, help="number of turns to list (default 20)")
    correct = commands.add_parser("correct", help="assign or replace the reviewed route for a turn")
    correct.add_argument("id", help="full turn ID from recent")
    correct.add_argument("route", choices=CHOICES, help="route the speaker intended")
    commands.add_parser("export", help="print only explicitly labeled examples as JSONL")
    args = parser.parse_args(argv)

    feedback = RouteFeedback(args.root)
    try:
        if args.command == "correct":
            if not feedback.correct(args.id, args.route):
                parser.exit(1, "Turn ID not found. Use recent to find a retained turn.\n")
            print(f"Labeled {args.id} as {args.route}.")
        elif args.command == "export":
            for item in feedback.examples():
                print(json.dumps(item))
        else:
            if args.limit < 1:
                recent.error("--limit must be at least 1")
            turns = feedback.recent(args.limit)
            if not turns:
                print("No voice routing turns recorded.")
            for turn in turns:
                stamp = datetime.fromtimestamp(
                    turn["created_at"] / 1_000_000_000, timezone.utc
                ).astimezone().isoformat(timespec="seconds")
                label = turn["label"] or "unreviewed"
                outcomes = ", ".join(turn["outcomes"]) or "none"
                print(f"{turn['id']}  {stamp}  {turn['mode']}  {turn['kind']}  [{label}]")
                print(f"  Transcript  {turn['text']!r}")
                print(f"  Outcomes {outcomes}  Scores {json.dumps(turn['scores'])}")
                if turn["proposal"]:
                    print(f"  Classifier proposal {turn['proposal']}")
    except (OSError, sqlite3.Error):
        parser.exit(1, "Could not read or update the local route feedback database.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
