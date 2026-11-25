"""Index the schoolwork CLI's next school days, overdue and undated work,
plus changes from the last 48 hours. Grades are state only, with no score.
"""

from datetime import datetime, timezone
import hashlib
import json
import math
import re
import time
from uuid import uuid4

from . import Doc
from .. import signals


SW_ARGV = (("sw", "prep", "--json"), ("sw", "review", "--json"))
_SIGN_IN = "Schoolwork needs a new MySchoolApp sign-in. Run `sw login-msa` on Windows."
_TEXT_FIELDS = ("title", "course", "state", "type", "source", "lockNote", "description")


class SchoolworkError(Exception):
    pass


def _object(value):
    return value if isinstance(value, dict) else {}


def _rows(obj, key):
    if not isinstance(obj, dict) or not isinstance(obj.get(key), list):
        raise SchoolworkError("Incomplete schoolwork response.")
    return obj[key]


def _due(value):
    if value is None:
        return None
    if isinstance(value, str) and len(value) <= 40:
        try:
            instant = datetime.fromisoformat(value)
            if instant.tzinfo is not None:
                return instant.astimezone(timezone.utc)
        except (ValueError, OverflowError):
            pass
    raise SchoolworkError("Invalid schoolwork due date.")


def _assignment(row):
    if not isinstance(row, dict) or type(row.get("id")) not in (str, int):
        raise SchoolworkError("Invalid schoolwork assignment.")
    ref = signals.scrub(str(row["id"]))
    points = row.get("points")
    if (not 0 < len(ref) <= 4096
            or any(row.get(key) is not None and not isinstance(row[key], str) for key in _TEXT_FIELDS)
            or any(row.get(key) is not None and type(row[key]) is not bool for key in ("done", "locked"))
            or (points is not None and type(points) is not int
                and (type(points) is not float or not math.isfinite(points)))):
        raise SchoolworkError("Invalid schoolwork assignment.")
    return ref, _due(row.get("dueAt"))


def _document(ref, row, due, now):
    points = row.get("points")
    title = signals.scrub(row.get("title") or "")
    meta = signals.scrub({
        "untrusted": 1, "course": row.get("course"),
        "due": due.isoformat(timespec="seconds").replace("+00:00", "Z") if due else None,
        "state": row.get("state"), "done": int(row.get("done") is True),
        "locked": int(row.get("locked") is True), "points": points,
        "type": row.get("type"), "system": row.get("source"),
    })
    body = signals.scrub("\n".join((title, f"Course: {meta['course'] or ''}", f"Due: {meta['due'] or ''}",
                                   f"State: {meta['state'] or ''}", f"Points: {points if points is not None else ''}",
                                   f"Type: {meta['type'] or ''}", row.get("lockNote") or "",
                                   row.get("description") or "")))
    ts = due.timestamp() if due else now
    item = {key: meta[key] for key in ("course", "due", "state", "done", "locked")}
    item.update(id=ref, title=title)
    digest = hashlib.sha256(json.dumps([title, meta, body], sort_keys=True).encode()).hexdigest()
    return Doc(ref, title, ts, [("assignment", body, ts)], meta), item, digest


class SchoolworkIngester:
    name = "schoolwork"

    def __init__(self, run, *, post, signals_dir=None, clock=time.time):
        self._run = run
        self._post = post
        self._signals_dir = signals_dir
        self._clock = clock

    def poll(self, cursor):
        try:
            return self._poll(cursor or {})
        except SchoolworkError:
            raise
        except Exception:
            raise SchoolworkError("Schoolwork poll failed.") from None

    def _poll(self, cursor):
        prep = self._read(SW_ARGV[0], (0,))
        review = self._read(SW_ARGV[1], (0, 1))
        health = set()
        for finding in _rows(review, "health"):
            code = _object(finding).get("code")
            if not isinstance(code, str) or not code.strip():
                raise SchoolworkError("Invalid schoolwork health finding.")
            health.add(code)
        needs_sign_in = "needs_login" in health
        # An expired login also reports a failed sync. That case is the
        # sign-in episode (one brief, a blocked snapshot), not a job error.
        if "last_sync_failed" in health and not needs_sign_in:
            raise SchoolworkError("Schoolwork data is stale.")
        rows = _rows(review, "recentChanges") + _rows(review, "undated") + _rows(prep, "overdue")
        for day in _rows(prep, "days"):
            rows.extend(_rows(day, "due"))
        assignments = {}
        for row in rows:
            ref, due = _assignment(row)
            assignments[ref] = row, due

        known = _object(cursor.get("digests")).copy()
        changes, items = [], []
        now = self._clock()
        for ref, (row, due) in assignments.items():
            doc, item, digest = _document(ref, row, due, now)
            if known.get(ref) != digest:
                changes.append(doc)
            known[ref] = digest
            items.append(item)
        signals.write(self.name, items, directory=self._signals_dir, needs_sign_in=needs_sign_in)
        episode = cursor.get("sign_in_episode")
        if not isinstance(episode, str) or not re.fullmatch(r"[0-9a-f]{32}", episode):
            episode = "initial"
        if needs_sign_in and cursor.get("needs_sign_in") is not True:
            # Keep the same key when the Feed post succeeds but the index
            # rolls back. A committed recovery opens the next episode.
            try:
                self._post({"kind": "news", "rank": 3, "ask": _SIGN_IN,
                            "idempotency_key": f"schoolwork-sign-in:{episode}"})
            except Exception:
                raise SchoolworkError("Schoolwork sign-in brief failed.") from None
        elif not needs_sign_in and (cursor.get("needs_sign_in") is not False or episode == "initial"):
            episode = uuid4().hex
        return changes, {"digests": known, "needs_sign_in": needs_sign_in, "sign_in_episode": episode}

    def _read(self, argv, statuses):
        try:
            status, text = self._run(argv)
        except Exception:
            raise SchoolworkError("Schoolwork command failed.") from None
        if status not in statuses:
            raise SchoolworkError("Schoolwork command failed.")
        result = json.loads(text)
        if not isinstance(result, dict):
            raise SchoolworkError("Invalid schoolwork response.")
        if status == 1 and not _rows(result, "health"):
            raise SchoolworkError("Incomplete schoolwork health response.")
        return result
