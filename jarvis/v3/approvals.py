"""In-memory owner approvals with an append-only transition log."""

from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
import fcntl
import hashlib
from hmac import compare_digest
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import stat
from threading import Condition
import time
from typing import Callable

from ._paths import open_directory


_FINISHED_LIMIT = 1024
_POLL_SECONDS = 0.1
AUTO_PER_DAY = 30


class ApprovalConflict(Exception):
    """The approval is no longer pending or its digest differs."""


def digest(id, session_id, tool, args, preview):
    canonical = json.dumps(
        {"v": 1, "id": id, "session_id": session_id, "tool": tool,
         "args_raw": args, "preview": preview},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


@dataclass
class _Request:
    item: dict
    turn_id: str
    deadline: float
    cancelled: Callable[[], bool] | None
    state: str = "pending"


class ApprovalBroker:
    def __init__(self, *, clock=time.monotonic, timeout_s=120.0, new_id=token_urlsafe, log=None):
        self._clock = clock
        self._timeout = timeout_s
        self._new_id = new_id
        self._log = Path(log) if log is not None else None
        self._changed = Condition()
        self._pending = {}
        self._finished = OrderedDict()
        self._closed = False

    @contextmanager
    def _open_log(self, access):
        with open_directory(self._log.parent, create=True) as parent:
            info = os.fstat(parent)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise OSError("Approval log directory must be private.")
            flags = access | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
            fd = os.open(self._log.name, flags, 0o600, dir_fd=parent)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                    raise OSError("Approval log must be a private regular file.")
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield parent, fd
            finally:
                os.close(fd)

    @staticmethod
    def _write_row(parent, fd, item, state, stamp):
        row = {key: item[key] for key in ("id", "tool", "digest", "level", "why")}
        row.update(state=state, t=stamp)
        if state == "auto":
            row["decided_by"] = "policy"
        line = (json.dumps(row, allow_nan=False) + "\n").encode("ascii")
        if os.write(fd, line) != len(line):
            raise OSError("Incomplete approval log write.")
        os.fsync(fd)
        os.fsync(parent)

    def _append(self, request, state, stamp):
        if self._log is not None:
            with self._open_log(os.O_WRONLY) as (parent, fd):
                self._write_row(parent, fd, request.item, state, stamp)

    def _new_item(self, tool, args, session_id, verdict, preview):
        id = self._new_id()
        if not id or id in self._pending or id in self._finished:
            raise ApprovalConflict("Approval id is unavailable.")
        return {"id": id, "tool": tool, "digest": digest(id, session_id, tool, args, preview),
                "level": verdict["level"], "why": verdict["why"]}

    def auto(self, tool, args, *, session_id, verdict, preview=None, cancelled=None):
        """Record authorization and return its receipt, or None at the daily cap."""
        with self._changed:
            if self._closed or self._cancelled(cancelled) or self._log is None:
                raise ApprovalConflict("Automatic approval is unavailable.")
            # Hold the file lock across the count and append, including across brokers.
            with self._open_log(os.O_RDWR) as (parent, fd):
                stamp = time.time()
                today = date.fromtimestamp(stamp)
                count = 0
                with os.fdopen(os.dup(fd), encoding="ascii") as stream:
                    for line in stream:
                        if not line.endswith("\n"):
                            raise ValueError("Incomplete approval log row.")
                        row = json.loads(line)
                        if row["state"] == "auto" and date.fromtimestamp(row["t"]) == today:
                            count += 1
                            if count == AUTO_PER_DAY:
                                return None
                if self._cancelled(cancelled):
                    raise ApprovalConflict("Automatic approval was cancelled.")
                item = self._new_item(tool, args, session_id, verdict, preview)
                self._write_row(parent, fd, item, "auto", stamp)
            self._remember(item["id"])
            return item["id"], item["digest"]

    def _remember(self, id):
        self._finished[id] = None
        if len(self._finished) > _FINISHED_LIMIT:
            self._finished.popitem(last=False)

    def _finish(self, request, state):
        try:
            self._append(request, state, time.time())
        except Exception:
            # A missing durable decision cannot grant authority.
            state = "cancelled"
        request.state = state
        id = request.item["id"]
        del self._pending[id]
        self._remember(id)
        self._changed.notify_all()

    @staticmethod
    def _cancelled(check):
        try:
            return check is not None and bool(check())
        except Exception:
            return True

    def _advance(self, request, now):
        if self._cancelled(request.cancelled):
            self._finish(request, "cancelled")
        elif now >= request.deadline:
            self._finish(request, "expired")

    def ask(self, tool, args, card, *, session_id, turn_id, verdict, preview=None, cancelled=None,
            on_approved=None, on_pending=None, display_why=None):
        request = None
        try:
            with self._changed:
                if self._closed or self._cancelled(cancelled):
                    return False
                item = self._new_item(tool, args, session_id, verdict, preview)
                id, signature = item["id"], item["digest"]
                asked_at = time.time()
                deadline = self._clock() + self._timeout
                item["asked_at"] = asked_at
                item["card"] = deepcopy(card)
                item["display_why"] = display_why
                request = _Request(item, turn_id, deadline, cancelled)
                self._append(request, "pending", asked_at)
                self._pending[id] = request
            if on_pending is not None:
                on_pending()
            with self._changed:
                while request.state == "pending":
                    now = self._clock()
                    self._advance(request, now)
                    if request.state == "pending":
                        self._changed.wait(min(_POLL_SECONDS, deadline - now))
                if self._closed or request.state != "approved" or self._cancelled(cancelled):
                    return False
                if on_approved is not None:
                    on_approved(id, signature)
                return True
        except Exception:
            with self._changed:
                if request is not None and self._pending.get(request.item["id"]) is request:
                    self._finish(request, "cancelled")
            return False

    def pending(self, *, turn_id=None):
        with self._changed:
            now = self._clock()
            requests = [request for request in self._pending.values()
                        if turn_id is None or request.turn_id == turn_id]
            for request in requests:
                self._advance(request, now)
            items = []
            for request in requests:
                if request.state == "pending":
                    item = dict(deepcopy(request.item), expires_in=max(0, request.deadline - now))
                    display_why = item.pop("display_why")
                    if display_why is not None:
                        item["why"] = display_why
                    items.append(item)
            return items

    def answer(self, id, digest, decision):
        if decision not in ("approve", "deny"):
            raise ValueError("Expected approve or deny.")
        with self._changed:
            request = self._pending.get(id)
            if request is None:
                if id in self._finished:
                    raise ApprovalConflict("Approval is no longer pending.")
                raise KeyError(id)
            self._advance(request, self._clock())
            if request.state != "pending":
                raise ApprovalConflict("Approval is no longer pending.")
            expected = request.item["digest"].encode("ascii")
            if not isinstance(digest, str) or not compare_digest(digest.encode("utf-8", "surrogatepass"), expected):
                raise ApprovalConflict("Approval digest differs.")
            state = "approved" if decision == "approve" else "denied"
            self._finish(request, state)
            if request.state != state:
                raise ApprovalConflict("Approval could not be recorded.")
            return state

    def _cancel(self, turn_id):
        for request in list(self._pending.values()):
            if turn_id is None or request.turn_id == turn_id:
                self._finish(request, "cancelled")

    def cancel(self, turn_id=None, reason="stopped"):
        with self._changed:
            self._cancel(turn_id)

    def shutdown(self):
        with self._changed:
            self._closed = True
            self._cancel(None)
