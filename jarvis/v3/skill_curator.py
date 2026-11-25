"""Deterministic learned-skill curation.

The schedule is expressed by due(); a later PR will wire it into the daemon.
"""

from contextlib import ExitStack
from datetime import datetime, timezone
import json
import time
from uuid import uuid4

from .skill_store import (
    StoreError, _ID, _MAIN, _USAGE, _epoch, _header, _json, _name, _read, _stat, _usage,
)

DAY = 86400
STALE_DAYS = 14
ARCHIVE_DAYS = 30
WEEK_S = 7 * DAY
IDLE_S = 2 * 3600
_STATE = ".curator.json"
_DIGEST = "curator.jsonl"
_DIGEST_LIMIT = 8 * 1024 * 1024


def due(now, *, last_run, idle_s):
    return idle_s >= IDLE_S and (last_run is None or now - last_run >= WEEK_S)


def _record_id(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Invalid curator timestamp.")
    date = datetime.fromisoformat(value)
    if date.tzinfo is None:
        raise ValueError("Curator timestamps need a timezone.")
    return date.timestamp()


def _state(data):
    if data is None:
        return {"stale": {}, "last_change": None, "pending": None}
    state = json.loads(data)
    if (not isinstance(state, dict) or state.keys() != {"stale", "last_change", "pending"}
            or not isinstance(state["stale"], dict)
            or (state["last_change"] is not None and not _epoch(state["last_change"]))):
        raise ValueError("Invalid curator state.")
    for name, life in state["stale"].items():
        _name(name)
        if not _record_id(life):
            raise ValueError("Invalid stale skill state.")
    pending = state["pending"]
    if pending is not None:
        if not isinstance(pending, dict) or pending.keys() != {"line", "life", "archive", "offset"}:
            raise ValueError("Invalid curator intent.")
        line = pending["line"]
        if (not isinstance(line, dict) or line.keys() != {"ts", "skill", "change", "idle_days"}
                or line["change"] not in ("stale", "fresh", "archived")
                or type(line["idle_days"]) is not int or line["idle_days"] < 0
                or not _record_id(pending["life"]) or type(pending["offset"]) is not int
                or not 0 <= pending["offset"] <= _DIGEST_LIMIT):
            raise ValueError("Invalid curator digest entry.")
        if line["change"] == "archived":
            if not _record_id(pending["archive"]):
                raise ValueError("Missing curator archive ID.")
        elif pending["archive"] is not None:
            raise ValueError("Unexpected curator archive ID.")
        _name(line["skill"])
        _timestamp(line["ts"])
    return state


def _activity(applied):
    newest, lives = {}, {}
    for row in applied.values():
        name = row["skill"]
        ts = _timestamp(row["ts"])
        # Keep the latest timestamp even when the wall clock regresses.
        newest[name] = max(ts, newest.get(name, ts))
        if row["path"] == _MAIN and row["before"] is None:
            lives[name] = row["id"], newest[name]
    return newest, lives


class _Digest:
    """Stage one change before archiving so retries can finish it exactly once."""

    def __init__(self, op, *, dry_run):
        self.op = op
        self.dry_run = dry_run
        data, self.state_info = op.read_sidecar(_STATE)
        self.state = _state(data)
        data, self.log_info = op.read_sidecar(_DIGEST, ledger=True, limit=_DIGEST_LIMIT)
        self.log = data or b""
        if self.log and not self.log.endswith(b"\n"):
            raise StoreError("Incomplete curator digest.")

    def _save(self):
        if not self.dry_run:
            self.state_info = self.op.write_sidecar(_STATE, _json(self.state).encode(), self.state_info)

    def stage(self, line, life, archive):
        if len(self.log) + len(_json(line).encode()) + 1 > _DIGEST_LIMIT:
            raise StoreError("The curator digest exceeds its size limit.")
        self.state["pending"] = {"line": line, "life": life, "archive": archive, "offset": len(self.log)}
        self._save()

    def finish(self, *, applied=True):
        pending = self.state["pending"]
        line, offset = pending["line"], pending["offset"]
        encoded = _json(line).encode() + b"\n"
        if len(self.log) < offset or (len(self.log) != offset and
                                      (not applied or self.log[offset:] != encoded)):
            raise StoreError("The curator digest differs from its pending change.")
        if applied:
            if len(self.log) == offset:
                self.log += encoded
                if not self.dry_run:
                    self.log_info = self.op.write_sidecar(
                        _DIGEST, self.log, self.log_info, ledger=True, limit=_DIGEST_LIMIT)
            name = line["skill"]
            if line["change"] == "stale":
                self.state["stale"][name] = pending["life"]
            else:
                self.state["stale"].pop(name, None)
            self.state["last_change"] = _timestamp(line["ts"])
        self.state["pending"] = None
        self._save()
        return line if applied else None

    def recover(self, applied):
        pending = self.state["pending"]
        if pending is None:
            return None
        archive = pending["archive"]
        if archive is None:
            return self.finish()
        row = applied.get(archive)
        line = pending["line"]
        if row is not None:
            name = line["skill"]
            if (row["actor"], row["op"], row["skill"], row["ts"]) != (
                    "curator", "archive", name, line["ts"]):
                raise StoreError("The curator archive differs from its pending change.")
            if not self.dry_run and _stat(self.op.root_fd, name) is None:
                self.op._sync_links(name, live=False)
        return self.finish(applied=row is not None)


class Curator:
    def __init__(self, store, *, referenced=None):
        self.store = store
        self.referenced = referenced

    def run(self, now=None, *, dry_run=False):
        now = time.time() if now is None else now
        try:
            source = () if self.referenced is None else self.referenced()
            if isinstance(source, (str, bytes)):
                return []
            referenced = {str.casefold(name) for name in source}
        except Exception:
            return []

        lines = []
        try:
            ts = datetime.fromtimestamp(now, timezone.utc).isoformat()
            with self.store._operation(initialize=False) as op:
                data, _ = op.read_sidecar(_USAGE)
                usage = {} if data is None else _usage(data)
                applied = {row["id"]: row for row in op.records(strict=True) if op.status(row) == "applied"}
                newest, lives = _activity(applied)
                digest = _Digest(op, dry_run=dry_run)
                recovered = digest.recover(applied)
                if recovered is not None:
                    lines.append(recovered)
                last_change = digest.state["last_change"]
                if last_change is None or _timestamp(ts) >= last_change:
                    self._curate(op, digest, usage, newest, lives, referenced, now, ts, lines)
        except (OSError, ValueError, OverflowError, RecursionError):
            pass
        return lines

    @staticmethod
    def _curate(op, digest, usage, newest, lives, referenced, now, ts, lines):
        for name in sorted(lives):
            if name.casefold() in referenced or op.owner(name) is not None:
                continue
            life, since = lives[name]
            used = usage.get(name, {}).get("last_used")
            last = used if used is not None and used >= since else newest[name]
            age = now - last
            if age < 0:
                continue
            stale = digest.state["stale"].get(name) == life
            if age >= ARCHIVE_DAYS * DAY:
                change = "archived"
            elif age >= STALE_DAYS * DAY:
                if stale:
                    continue
                change = "stale"
            elif stale:
                change = "fresh"
            else:
                continue
            with ExitStack() as stack:
                try:
                    skill = op.learned(stack, name)
                    if skill is None:
                        continue
                    revision = op.revision(skill)
                    before = _read(skill, _MAIN)
                    _, flags = _header(before.decode(), name)
                    if flags["locked"] == "true" or flags["pinned"] == "true":
                        continue
                    op.verify_skill(name, skill, revision)
                except (OSError, ValueError):
                    continue
                line = {"ts": ts, "skill": name, "change": change, "idle_days": int(age // DAY)}
                archive = uuid4().hex if change == "archived" else None
                digest.stage(line, life, archive)
                if archive is not None and not digest.dry_run:
                    op.change("archive", name, _MAIN, before, None, skill, revision,
                              actor="curator", timestamp=ts, record_id=archive)
                lines.append(digest.finish())
