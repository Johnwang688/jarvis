"""The owner's snoozes and dismissals of inbox rows (owner inbox design
`docs/plans/2026-10-24-owner-inbox-design.md`, IN-8, §3.2, §4; inbox package
B4, IN-7).

**Only rows of rank 5 to 8, and only while they are (IN-8).** A live
approval, a blocking question, a proposal in its grace window or a blocked
task (ranks 1 to 4) is never hidden: hiding it is how it times out unseen.
Two rules hold that, and the second is the boundary:

  * the owner's route (`inbox.mark_row`) refuses a mark on a row whose
    **current** rank, read from a fresh projection at mark time, is below 5.
    The client never sends a rank;
  * `apply` honours a mark only while the row's current rank is 5 or more.
    So a `failed` task (rank 8) dismissed, then resumed and `blocked`
    (rank 4), shows again, whatever the file says.

The "PR list unavailable" row (inbox B3, `prx:list`, kind
`pr_unavailable`, rank 5) is never markable either, by kind and by id
prefix (`UNMARKABLE_KINDS`, `UNMARKABLE_PREFIXES`): hiding it would turn
"unavailable" into "nothing waiting".

A deferred approval (QH, `quiet_defer.py`, kind `deferred`, `defer:` ids)
is never markable either, at rank 4 or 8: "Leave it" is how it goes.

**A mark is for the row as it was (`material_key`).** Each mark keeps a
short digest of what made the row worth hiding: its kind and rank, plus,
per kind, a PR's head SHA (new commits bring it back), a task's state and
its last update, a decision's delivery, a check's done flag, nothing more
for a "denied while away" record (it never changes), and the code-built
`title` and `blocks` for any other kind. A row whose digest no longer
matches shows again, and the next write drops its mark. A check that gets a
new step result keeps its mark (its done flag has not changed).

**The file.** `<data root>/inbox/marks.json`: `{row_id: {snoozed_until,
dismissed_at, key, at}}`, exactly one of the first two set (an ISO time),
`key` the digest above and `at` when the owner marked it. The I/O is the
decisions store's: the `inbox/` folder opened `O_DIRECTORY | O_NOFOLLOW`
relative to the data root (0700, tightened), the file read `O_NOFOLLOW |
O_NONBLOCK` and refused unless regular and at most `_FILE_MAX`, written as a
fresh `O_EXCL` 0600 temp file, fsynced, then `os.replace`d. At most
`MARKS_MAX` entries; a write first drops expired snoozes and marks whose row
is gone or changed, then the oldest past the cap (a dropped mark only makes
a row show). The whole `inbox/` tree is protected state
(`files._inbox_tree`), and the file is named in `_protected_state_paths`.

**Fails closed.** A file that is missing, a symlink, not regular, too large,
not JSON, or any entry not exactly the shape above (a snooze ending more
than `SNOOZE_MAX` after its `at` included): no mark applies, so every row
shows. A mark written over such a file starts from empty. `mark` refuses a
snooze outside (now, now + `SNOOZE_MAX`] as the route does.

**A snooze that ends.** `inbox.Inbox.arm_snoozes` keeps one timer for the
earliest snooze still running and publishes `inbox_changed` (reason
`snooze_ended`, ids only) when it passes, so the row comes back without
waiting for another event.

SELF_PROTECTED (`jarvis/tools/files.py`): it decides what the owner does
not see.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import errno
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets as pysecrets
import stat
import threading

from .decisions import DecisionStore

LOG = logging.getLogger(__name__)

NAME = "marks.json"
MARKS_MAX = 500                     # entries kept; the oldest go first
SNOOZE_MAX = timedelta(days=30)     # the furthest a snooze may reach
MARKABLE_MIN_RANK = 5               # IN-8: kinds 5 to 8 only
MARKABLE_MAX_RANK = 8
# Inbox B3 (#320): "PR list unavailable" is rank 5 but never hidden.
UNMARKABLE_KINDS = frozenset({"pr_unavailable", "deferred"})
UNMARKABLE_PREFIXES = frozenset({"prx", "defer"})
_ROW_ID_MAX = 400                   # `inbox._split`'s bound on a decoded id
_KEY_RE = re.compile(r"^[0-9a-f]{16}$")
_FILE_MAX = 512 * 1024              # a larger file is not one of ours: no marks
_WRITE_MAX = _FILE_MAX // 2         # never write what a read would refuse
_FIELDS = frozenset({"snoozed_until", "dismissed_at", "key", "at"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value) -> datetime | None:
    """An ISO time with its zone, or None (a time without one is ambiguous)."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        out = datetime.fromisoformat(value)
    except ValueError:
        return None
    return out if out.tzinfo is not None else None


def _rank(row) -> int | None:
    rank = row.get("rank") if isinstance(row, dict) else None
    return rank if isinstance(rank, int) and not isinstance(rank, bool) else None


def markable(row) -> bool:
    """Whether the owner may snooze or dismiss this row now, and whether a
    mark on it applies: rank 5 to 8, never the "PR list unavailable" row."""
    rank = _rank(row)
    if rank is None or not MARKABLE_MIN_RANK <= rank <= MARKABLE_MAX_RANK:
        return False
    row_id = row.get("id")
    if not isinstance(row_id, str) or ":" not in row_id:
        return False
    return row.get("kind") not in UNMARKABLE_KINDS and row_id.split(":", 1)[0] not in UNMARKABLE_PREFIXES


def material_key(row) -> str:
    """The digest a mark keeps: what about the row, changed, brings it back."""
    kind = row.get("kind")
    detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
    if kind == "pr":
        basis = [detail.get("head")]
    elif kind == "task":
        basis = [detail.get("state"), row.get("asked_at")]
    elif kind == "decision":
        basis = [detail.get("delivery")]
    elif kind == "check":
        basis = [detail.get("done")]
    elif kind == "missed":
        basis = []
    else:
        basis = [row.get("title"), row.get("blocks")]
    raw = json.dumps([kind, _rank(row), *basis], ensure_ascii=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("ascii")).hexdigest()[:16]


def _valid_entry(row_id, entry) -> bool:
    if not isinstance(row_id, str) or not row_id or len(row_id) > _ROW_ID_MAX or ":" not in row_id:
        return False
    if not isinstance(entry, dict) or set(entry) != _FIELDS:
        return False
    snoozed, dismissed = entry["snoozed_until"], entry["dismissed_at"]
    if (snoozed is None) == (dismissed is None):
        return False
    if snoozed is not None and parse_time(snoozed) is None:
        return False
    marked = parse_time(entry["at"])
    # The 30-day bound holds on read too: a snooze longer than SNOOZE_MAX
    # from when it was made is not one the route could have written.
    if snoozed is not None and (marked is None or parse_time(snoozed) - marked > SNOOZE_MAX):
        return False
    if dismissed is not None and parse_time(dismissed) is None:
        return False
    return (isinstance(entry["key"], str) and bool(_KEY_RE.match(entry["key"]))
            and parse_time(entry["at"]) is not None)


def valid(data) -> dict | None:
    """The marks, exactly; None for anything else (then no mark applies)."""
    if not isinstance(data, dict) or len(data) > MARKS_MAX:
        return None
    for row_id, entry in data.items():
        if not _valid_entry(row_id, entry):
            return None
    return data


class MarksStore:
    """`inbox/marks.json` under the data root. Thread-safe: every write
    holds `_lock`, reading, pruning and replacing the file in one step."""

    # The decisions store's folder I/O, shared (no symlinked `inbox/`, 0700).
    _DIR = DecisionStore._DIR
    _READ = DecisionStore._READ
    _WRITE = DecisionStore._WRITE
    _subdir = DecisionStore._subdir

    def __init__(self, root, *, clock=None):
        self.root = Path(root)
        self.path = self.root / "inbox" / NAME
        self.clock = clock or _now
        self._lock = threading.RLock()

    # -- the folder and the file ---------------------------------------------------

    def _open(self, create: bool) -> int | None:
        """The `inbox/` folder's descriptor (the caller closes it), or None."""
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        try:
            root_fd = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                              | getattr(os, "O_CLOEXEC", 0))
        except FileNotFoundError:
            if create:
                raise
            return None
        try:
            return self._subdir(root_fd, "inbox", create)
        finally:
            os.close(root_fd)

    def _read(self, dir_fd: int) -> dict | None:
        """The marks, `{}` when there is no file, None when the file is not
        one of ours (a symlink, not regular, too large, malformed)."""
        try:
            fd = os.open(NAME, self._READ, dir_fd=dir_fd)
        except FileNotFoundError:
            return {}
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                return None
            raise
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > _FILE_MAX:
                return None
            if stat.S_IMODE(info.st_mode) != 0o600:
                os.fchmod(fd, 0o600)
            chunks, left = [], _FILE_MAX + 1
            while left > 0 and (chunk := os.read(fd, min(1 << 16, left))):
                chunks.append(chunk)
                left -= len(chunk)
            data = b"".join(chunks)
        finally:
            os.close(fd)
        if len(data) > _FILE_MAX:
            return None
        try:
            return valid(json.loads(data.decode("utf-8")))
        except (ValueError, UnicodeError, RecursionError):
            return None

    @staticmethod
    def _encode(marks: dict) -> bytes:
        return json.dumps(marks, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")

    def _size(self, marks: dict) -> int:
        return len(self._encode(marks))

    def _write(self, dir_fd: int, marks: dict) -> None:
        data = self._encode(marks)
        if len(data) > _WRITE_MAX:
            raise ValueError("the marks would be larger than the file may be")
        tmp = f".marks.{pysecrets.token_hex(4)}.tmp"
        fd = os.open(tmp, self._WRITE, 0o600, dir_fd=dir_fd)
        try:
            try:
                os.fchmod(fd, 0o600)
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, NAME, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dir_fd)
            except OSError:
                pass
            raise

    # -- reads -----------------------------------------------------------------------

    def read(self) -> dict:
        """The marks; `{}` (no mark applies) when the file is missing or not
        one of ours. Never raises."""
        try:
            with self._lock:
                fd = self._open(create=False)
                if fd is None:
                    return {}
                try:
                    marks = self._read(fd)
                finally:
                    os.close(fd)
        except Exception as exc:  # noqa: BLE001 — unreadable marks hide nothing
            LOG.warning("Cannot read the inbox marks (%s); every row shows", type(exc).__name__)
            return {}
        if marks is None:
            LOG.warning("The inbox marks file is not one of ours; every row shows")
            return {}
        return marks

    def hidden(self, row, mark, now) -> dict | None:
        """What hides `row` now (`{snoozed_until, dismissed_at}`), or None."""
        if not mark or not markable(row) or mark.get("key") != material_key(row):
            return None
        if mark.get("dismissed_at") is not None:
            return {"snoozed_until": None, "dismissed_at": mark["dismissed_at"]}
        until = parse_time(mark.get("snoozed_until"))
        if until is not None and until > now:
            return {"snoozed_until": mark["snoozed_until"], "dismissed_at": None}
        return None

    def apply(self, rows: list[dict], *, flag: bool) -> list[dict]:
        """`rows` with the marks applied: a hidden row is left out, or, with
        `flag`, kept with `hidden` set. With `flag` every row also says
        whether it is `markable`. Rows are copied, never changed."""
        marks = self.read()
        now = self.clock()
        out = []
        for row in rows:
            why = self.hidden(row, marks.get(row.get("id")), now) if marks else None
            if not flag:
                if why is None:
                    out.append(row)
                continue
            extra = {"markable": markable(row)}
            if why is not None:
                extra["hidden"] = why
            out.append({**row, **extra})
        return out

    # -- writes ----------------------------------------------------------------------

    def _prune(self, marks: dict, live: dict, now) -> dict:
        """Keep a mark only while its row is here, unchanged and still markable,
        and a snooze only until it ends."""
        kept = {}
        for row_id, mark in marks.items():
            row = live.get(row_id)
            if row is None or not markable(row) or mark["key"] != material_key(row):
                continue
            if mark["dismissed_at"] is None:
                until = parse_time(mark["snoozed_until"])
                if until is None or until <= now:
                    continue
            kept[row_id] = mark
        return kept

    def mark(self, row: dict, live: list[dict], *, snooze_until: datetime | None = None) -> dict:
        """Snooze `row` until `snooze_until`, or dismiss it (None). `live` is
        the fresh projection (marks ignored) the caller found `row` in; the
        caller has judged `markable(row)` and the time. Returns the entry."""
        if not markable(row):
            raise ValueError("this row cannot be snoozed or dismissed")
        now = self.clock()
        if snooze_until is not None and not now < snooze_until <= now + SNOOZE_MAX:
            raise ValueError("a snooze must end in the future, at most SNOOZE_MAX ahead")
        at = now.isoformat(timespec="seconds")
        entry = {"snoozed_until": snooze_until.astimezone(timezone.utc).isoformat(timespec="seconds")
                 if snooze_until is not None else None,
                 "dismissed_at": None if snooze_until is not None else at,
                 "key": material_key(row), "at": at}
        with self._lock:
            fd = self._open(create=True)
            try:
                current = self._read(fd)
                if current is None:
                    LOG.warning("The inbox marks file was not one of ours; it starts again")
                    current = {}
                marks = self._prune(current, {r["id"]: r for r in live if isinstance(r.get("id"), str)}, now)
                marks.pop(row["id"], None)
                marks[row["id"]] = entry
                # Past the cap, or past what one write may hold, the oldest
                # go first (never the new one): a dropped mark only shows a row.
                oldest = [k for k in sorted(marks, key=lambda k: (parse_time(marks[k]["at"]), k))
                          if k != row["id"]]
                while len(marks) > MARKS_MAX or (oldest and self._size(marks) > _WRITE_MAX):
                    marks.pop(oldest.pop(0))
                self._write(fd, marks)
            finally:
                os.close(fd)
        return entry

    def clear(self, row_id: str) -> bool:
        """Undo: drop `row_id`'s mark. True when there was one."""
        return bool(self.forget([row_id]))

    def forget(self, row_ids) -> list[str]:
        """Drop the marks of `row_ids` in one write (an Undo, or rows that are
        known to have left: a PR closed or merged). Returns the ids dropped."""
        with self._lock:
            fd = self._open(create=False)
            if fd is None:
                return []
            try:
                current = self._read(fd)
                gone = [i for i in row_ids if current and i in current]
                if not gone:
                    return []
                kept = {k: v for k, v in current.items() if k not in gone}
                self._write(fd, kept)
                return gone
            finally:
                os.close(fd)
