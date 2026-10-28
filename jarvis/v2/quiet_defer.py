"""Approvals asked during quiet hours, deferred to the owner's morning
(work package QH, `docs/plans/2026-10-28-quiet-hours-deferral.md`; the
owner's words of 2026-10-28: "defer and keep working").

**The deferral (QH-1 to QH-3).** The approval broker (`approvals.
PendingApprovals.ask`) asks `QuietDesk.settle` once per ask, under its own
lock, before any card exists. While `quiet.QuietHours.is_quiet()` and the
owner is not present, the ask settles at once as DENY with resolution
`deferred`: no card, no Discord post, no wait. The asking agent is told,
in code's words (`denial_text`), that the owner is asleep until the end of
quiet hours, that this was not approved, not to retry it tonight, and to
carry on with work that does not need it. The words reach the model
through the permit's verdict (`permissions.L_DEFERRED`): Claude's hook
reason, the fast path's `Refusal`, and the jarvis-mcp bridge's
`_unasked_refusal`. A Codex approval's decline carries no text (as CG-1
found); its chat log says why instead.

* **Present** (QH-3) is `Daemon.owner_present()`: the owner's input within
  `PRESENT_S` (15 minutes) by the daemon's monotonic clock, stamped by
  `Daemon.owner_seen()` at the owner's HUD message (the same `is_owner`
  test that grants the desk), the HUD's answer to an approval (`POST
  /approvals/<id>` and the inbox's answer route), the phone's answer after
  a verified passkey assertion, and the owner's own Approve and retry or
  Leave it here. The desk's own presence (`desk.DeskState`) is per turn and
  keeps no time, so it is not the signal.
* **A Discord command's own ask** (a request carrying `discord_channel_id`,
  set by daemon code only for the owner's `/project new`) is never
  deferred: the owner typed it a moment ago in that channel.
* **Not quiet when it cannot be read.** `quiet.py` reads a malformed, linked
  or oversized `quiet.json` as its default (on, 23:00 to 07:00), which is
  right for the phone's push. Here such a file means "not quiet": every ask
  is asked normally (`setting_readable`). A missing file is the owner's
  default setting, and it applies. Anything that raises while deciding
  means "ask normally".
* Only daemon2's `main()` attaches the desk (`Daemon.attach_quiet_desk`):
  an embedded or test daemon's broker never consults the wall clock.

**The record (QH-5).** Each deferral is one entry in
`<data root>/inbox/deferred.json`, protected state (the inbox tree, and the
file named in `files._protected_state_paths`): the card's public projection
(`ApprovalRequest.to_json`, never `owner_json`; a HUD-only card is its
private projection), and `request_digest`, a SHA-256 of the full request
the broker judged (thread, task, tool, the unredacted arguments and the
command). The arguments themselves are not kept. The file's I/O is the
inbox stores' own (`inbox_marks.MarksStore`'s: `O_NOFOLLOW` folder and file,
0600, a fresh `O_EXCL` temp file, fsync, `os.replace`); a file that is not
exactly ours shows no row and matches no grant. A record is a `deferred`
row (`rows`) at rank 4 for `WAITING_S` (24 h), then rank 8 information for
`SHOWN_S`; never markable (`inbox_marks.UNMARKABLE_KINDS`).

**Approve and retry (QH-6) and Leave it (QH-7)** are `answer`, reached only
from the owner's inbox route (`inbox.answer`) and the phone's answer route
after a verified passkey assertion bound to this deferral
(`remote_answer`). Approve and retry turns the record into a single-use
grant for `GRANT_S` (30 minutes), bound to its digest; then the asking chat
is told through inbox B1's delivery (`Daemon.deliver_queued`, `via="inbox"`
or "phone", no desk authority, queued behind a running turn, never a
steer), or a task that is `blocked` is resumed (`TaskControl.resume`). The
next ask whose digest is identical, within the 30 minutes, is allowed once
with no card and logged as "granted from the inbox" (`settle`, under the
broker's lock, so two identical asks cannot both take it). Anything that
differs asks normally. Never Always. A HUD-only (Browser) card is never
granted: its row offers Leave it and its chat only. Leave it marks the
record left; nothing is sent.

SELF_PROTECTED (`jarvis/tools/files.py`): it decides which asks are answered
without the owner at the desk. Tests: tests/v2/quiet_defer_check.py.
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

from .approvals import clean_line

LOG = logging.getLogger(__name__)

NAME = "deferred.json"
PRESENT_S = 15 * 60             # QH-3: the owner's input this recently is presence
GRANT_S = 30 * 60               # QH-6: how long an Approve and retry's grant stands
WAITING_S = 24 * 3600           # QH-5: rank 4 for a day, then rank 8 information
SHOWN_S = 7 * 24 * 3600         # rank 8 rows show this long after the deferral
KEEP = 200                      # records kept; the oldest go first
RANK_WAITING = 4
RANK_INFO = 8
KIND = "deferred"
PREFIX = "defer"
GRANTED = "granted from the inbox"
DEFERRED = "deferred"
STATES = frozenset({"waiting", "granted", "used", "left"})
_CARD_MAX = 32 * 1024           # a card larger than this is kept cut, and never granted
_FILE_MAX = 8 * 1024 * 1024
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
# What of a card's public projection a record keeps: another surface's
# handles (the Discord code and channel) and the wait are dropped.
_CARD_DROPPED = ("code", "discord_channel_id", "timeout_s")
_FIELDS = frozenset({"id", "tool", "thread_id", "task_id", "provider", "origin", "hud_only", "card",
                     "digest", "at", "until", "state", "grant_until", "settled_at"})

_DIR = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0))
_READ = (os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
         | getattr(os, "O_CLOEXEC", 0))
_WRITE = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
          | getattr(os, "O_CLOEXEC", 0))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(value) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        out = datetime.fromisoformat(value)
    except ValueError:
        return None
    return out if out.tzinfo is not None else None


# -- the request -------------------------------------------------------------------

def request_digest(request) -> str | None:
    """SHA-256 of the full request the broker judged: its thread, task, tool,
    the unredacted arguments and the command, as one canonical JSON text
    (sorted keys, no spaces, ASCII). None when that cannot be written
    exactly (a value JSON cannot hold): such a call can never be granted."""
    try:
        canon = json.dumps({"v": 1, "thread_id": request.thread_id, "task_id": request.task_id,
                            "tool": request.tool, "args": request.args, "command": request.command},
                           sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return None
    return hashlib.sha256(canon.encode("ascii")).hexdigest()


def _clock_words(when: datetime | None) -> str:
    return when.strftime("%H:%M") if isinstance(when, datetime) else "the morning"


def denial_text(until: datetime | None) -> str:
    """What the asking agent reads (QH-2): code's words only."""
    return (f"Deferred: the owner is asleep until {_clock_words(until)} (quiet hours), so nobody was "
            "asked. This call was NOT approved and did not run. Do not retry it tonight. Carry on with "
            "work that does not need it; the owner will see it in their inbox in the morning and may "
            "approve it then.")


GRANTED_TEXT = ("granted from the inbox: the owner's Approve and retry for exactly this call, once, "
                "after it was deferred overnight")


def retry_message(record: dict) -> str:
    """The message an Approve and retry sends the asking chat (QH-6, 2): code's
    words, the tool's name quoted as text."""
    tool = json.dumps(clean_line(record.get("tool") or "a tool", 80), ensure_ascii=False)
    until = _parse(record.get("grant_until"))
    stamp = until.astimezone().strftime("%H:%M") if until is not None else "30 minutes from now"
    return (f"The owner approved the {tool} call that was deferred overnight (deferral "
            f"{record['id']}). Retry it now if it is still needed. Only the identical call (the same "
            f"tool and the same arguments, from this chat) is allowed, once, until {stamp}; anything "
            "else asks the owner as usual.")


def public_card(request) -> tuple[dict, bool]:
    """(the card a record keeps, whether it was kept whole). The public
    projection (`to_json`; a HUD-only card's is its private one), minus
    another surface's handles. A card too large to keep is cut to its tool,
    ids and the first 2,000 characters of its command: it is shown, never
    granted (the owner never authorizes what they cannot fully see)."""
    card = {k: v for k, v in request.to_json().items() if k not in _CARD_DROPPED}
    try:
        size = len(json.dumps(card, ensure_ascii=True, allow_nan=False, default=str))
    except (TypeError, ValueError, RecursionError):
        size = _CARD_MAX + 1
    if size <= _CARD_MAX:
        try:
            return json.loads(json.dumps(card, ensure_ascii=True, allow_nan=False, default=str)), True
        except (TypeError, ValueError, RecursionError):
            pass
    command = card.get("command") if isinstance(card.get("command"), str) else ""
    return ({"req_id": card.get("req_id"), "tool": card.get("tool"), "thread_id": card.get("thread_id"),
             "task_id": card.get("task_id"), "provider": card.get("provider"),
             "command": command[:2000] + ("…" if len(command) > 2000 else ""), "cut": True}, False)


# -- presence and the setting --------------------------------------------------------

def setting_readable(quiet) -> bool:
    """Whether the quiet-hours file is one the setting can be read from: no
    file (the default applies), or a regular, small file of exactly the
    setting's shape. A link, a folder, a malformed or oversized file, or any
    error: False, so nothing is deferred."""
    from . import quiet as _quiet
    try:
        info = os.lstat(quiet.path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
        return False
    try:
        fd = os.open(quiet.path, _READ)
    except OSError:
        return False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        data = os.read(fd, 4097)
    except OSError:
        return False
    finally:
        os.close(fd)
    try:
        return _quiet.valid(json.loads(data.decode("utf-8"))) is not None
    except (ValueError, UnicodeError, RecursionError):
        return False


# -- the store -----------------------------------------------------------------------

def _valid_record(key, rec) -> bool:
    if not isinstance(key, str) or not _ID.match(key) or not isinstance(rec, dict) or set(rec) != _FIELDS:
        return False
    if rec["id"] != key or rec["state"] not in STATES or not isinstance(rec["hud_only"], bool):
        return False
    if not isinstance(rec["tool"], str) or not isinstance(rec["origin"], str) or not isinstance(rec["card"], dict):
        return False
    for name in ("thread_id", "task_id", "provider"):
        if rec[name] is not None and not (isinstance(rec[name], str) and len(rec[name]) <= 200):
            return False
    if rec["digest"] is not None and not (isinstance(rec["digest"], str) and _DIGEST.match(rec["digest"])):
        return False
    if _parse(rec["at"]) is None:
        return False
    for name in ("until", "grant_until", "settled_at"):
        if rec[name] is not None and _parse(rec[name]) is None:
            return False
    if rec["state"] in ("granted", "used") and (rec["grant_until"] is None or rec["digest"] is None
                                                 or rec["hud_only"]):
        return False
    return True


def valid(data) -> dict | None:
    """The records, exactly; None for anything else (then no row shows and
    no grant matches)."""
    if not isinstance(data, dict) or set(data) != {"v", "records"} or data["v"] != 1:
        return None
    records = data["records"]
    if not isinstance(records, dict) or len(records) > KEEP:
        return None
    for key, rec in records.items():
        if not _valid_record(key, rec):
            return None
    return records


class DeferredStore:
    """`<data root>/inbox/deferred.json`. Every write holds `_lock`, reading,
    changing and replacing the file in one step, so a grant is taken once."""

    def __init__(self, root, *, clock=None):
        self.root = Path(root)
        self.path = self.root / "inbox" / NAME
        self.clock = clock or _utcnow
        self._lock = threading.RLock()

    # -- the folder and the file (the inbox stores' I/O) -----------------------------

    def _open(self, create: bool) -> int | None:
        from .decisions import DecisionStore
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
            return DecisionStore._subdir(self, root_fd, "inbox", create)
        finally:
            os.close(root_fd)

    # `DecisionStore._subdir` reads these off its `self`.
    _DIR = _DIR

    def _read(self, dir_fd: int) -> dict | None:
        """The records, `{}` when there is no file, None when it is not ours."""
        try:
            fd = os.open(NAME, _READ, dir_fd=dir_fd)
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

    def _write(self, dir_fd: int, records: dict) -> None:
        data = json.dumps({"v": 1, "records": records}, ensure_ascii=True, allow_nan=False,
                          separators=(",", ":")).encode("ascii")
        if len(data) > _FILE_MAX // 2:
            raise ValueError("the deferred records would be larger than the file may be")
        tmp = f".deferred.{pysecrets.token_hex(4)}.tmp"
        fd = os.open(tmp, _WRITE, 0o600, dir_fd=dir_fd)
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

    def _change(self, change):
        """`change(records, now) -> result` under the lock; the records are
        written when it returned something other than None. A file that is
        not ours reads as no records: a grant or an answer finds nothing,
        and only a new deferral starts the file again."""
        with self._lock:
            fd = self._open(create=True)
            try:
                current = self._read(fd)
                if current is None:
                    LOG.warning("The deferred approvals file is not one of ours; it holds nothing")
                records = current if current is not None else {}
                result = change(records, self.clock())
                if result is not None:
                    self._write(fd, records)
                return result
            finally:
                os.close(fd)

    # -- reads -----------------------------------------------------------------------

    def all(self) -> list[dict]:
        """Every record, copied; `[]` when the file is missing or not ours.
        Never raises."""
        try:
            with self._lock:
                fd = self._open(create=False)
                if fd is None:
                    return []
                try:
                    records = self._read(fd)
                finally:
                    os.close(fd)
        except Exception as exc:  # noqa: BLE001 — unreadable records show nothing
            LOG.warning("Cannot read the deferred approvals (%s)", type(exc).__name__)
            return []
        if records is None:
            LOG.warning("The deferred approvals file is not one of ours; nothing shows or matches")
            return []
        return [dict(r) for r in records.values()]

    def get(self, record_id) -> dict | None:
        return next((r for r in self.all() if r["id"] == record_id), None)

    # -- writes ----------------------------------------------------------------------

    def add(self, request, until: datetime | None) -> dict:
        """Record one deferral (`QuietDesk.deferred`). Raises on a write
        that fails; the caller logs it, and the deny already stands."""
        card, whole = public_card(request)
        private = bool(request.private)
        digest = None if private or not whole else request_digest(request)

        def change(records, now):
            for old in [k for k, v in records.items()
                        if (_parse(v["at"]) or now) <= now - timedelta(seconds=SHOWN_S)]:
                records.pop(old)
            entry = {"id": request.req_id, "tool": clean_line(request.tool or "", 120),
                     "thread_id": _ident(request.thread_id),
                     "task_id": None if private else _ident(request.task_id),
                     "provider": None if private else _ident(request.provider),
                     "origin": "" if private else clean_line(request.origin or "", 160),
                     "hud_only": private, "card": card, "digest": digest,
                     "at": _iso(now), "until": _iso(until) if until is not None else None,
                     "state": "waiting", "grant_until": None, "settled_at": None}
            records[request.req_id] = entry
            for old in sorted(records, key=lambda k: (records[k]["at"], k)):
                if len(records) <= KEEP:
                    break
                if old != request.req_id:
                    records.pop(old)
            return dict(entry)
        return self._change(change)

    def settle(self, record_id: str, action: str) -> dict:
        """The owner's Approve and retry ("retry": waiting → granted, for
        `GRANT_S`) or Leave it ("leave": waiting → left). LookupError for no
        such record; ValueError, with a fixed sentence, for one this answer
        does not fit."""
        if action not in ("retry", "leave"):
            raise ValueError("action must be retry or leave")

        def change(records, now):
            rec = records.get(record_id)
            if rec is None:
                raise LookupError(record_id)
            if rec["state"] != "waiting":
                raise ValueError("this deferral was already answered")
            if action == "retry":
                if rec["hud_only"] or rec["digest"] is None:
                    raise ValueError("this call cannot be approved from the inbox; open its chat and "
                                     "ask again there")
                at = _parse(rec["at"])
                if at is None or now - at >= timedelta(seconds=WAITING_S):
                    raise ValueError("this deferral is over a day old; it can only be left")
                rec["state"] = "granted"
                rec["grant_until"] = _iso(now + timedelta(seconds=GRANT_S))
            else:
                rec["state"] = "left"
            rec["settled_at"] = _iso(now)
            return dict(rec)
        return self._change(change)

    def take(self, digest: str, thread_id) -> str | None:
        """The one grant whose digest is `digest`, for this thread, unused and
        unexpired: marked used and its id returned; None otherwise (and
        nothing is written). Never raises: a grant that cannot be read or
        written matches nothing."""
        if not isinstance(digest, str) or not _DIGEST.match(digest):
            return None

        def change(records, now):
            for rec in records.values():
                if (rec["state"] == "granted" and rec["digest"] == digest and not rec["hud_only"]
                        and rec["thread_id"] == thread_id):
                    until = _parse(rec["grant_until"])
                    if until is None or not now < until:
                        continue
                    rec["state"] = "used"
                    rec["settled_at"] = _iso(now)
                    return rec["id"]
            return None
        try:
            return self._change(change)
        except Exception as exc:  # noqa: BLE001 — a grant that cannot be taken is not taken
            LOG.warning("Cannot read or spend a deferral grant (%s); asking as usual", type(exc).__name__)
            return None


def _ident(value):
    return value if isinstance(value, str) and value and len(value) <= 200 else None


# -- the broker's hook -----------------------------------------------------------------

class QuietDesk:
    """What the broker asks at each ask (`settle`), and what it tells after a
    deferral or a grant. Attached by `Daemon.attach_quiet_desk` only.

    `quiet` is the daemon's `QuietHours`; `present()` is `Daemon.
    owner_present`; `store` the inbox's `DeferredStore`; `publish` the bus's
    publish (ids only)."""

    def __init__(self, quiet, present, store: DeferredStore, publish):
        self.quiet = quiet
        self.present = present
        self.store = store
        self.publish = publish

    def settle(self, request):
        """Under the broker's lock: ("granted", text) when the owner's grant
        for exactly this call is taken now, ("deferred", text) when the ask
        is deferred, None to ask as usual. Never raises."""
        try:
            if not request.private and request.discord_channel_id is None:
                digest = request_digest(request)
                if digest is not None and self.store.take(digest, request.thread_id) is not None:
                    return ("granted", GRANTED_TEXT)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Cannot check for an inbox grant (%s); asking as usual", type(exc).__name__)
        try:
            text = self.deferral(request)
        except Exception as exc:  # noqa: BLE001 — unknown means not quiet: ask as usual
            LOG.warning("Cannot judge quiet hours (%s); asking as usual", type(exc).__name__)
            return None
        return ("deferred", text) if text else None

    def deferral(self, request) -> str | None:
        """The agent's words when this ask is deferred, else None."""
        if request.discord_channel_id is not None:
            return None
        now = self.quiet.clock()
        if not self.quiet.is_quiet(now):
            return None
        if not setting_readable(self.quiet):
            return None
        try:
            if self.present() is not False:
                return None
        except Exception:  # noqa: BLE001 — presence unknown: ask as usual
            return None
        return denial_text(self.quiet.ends_at(now))

    def deferred(self, request) -> None:
        """After the broker settled a deferral: the record, then `inbox_changed`
        with its id. Never raises; the deny already stands."""
        try:
            until = self.quiet.ends_at()
        except Exception:  # noqa: BLE001
            until = None
        try:
            self.store.add(request, until)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Cannot record a deferred approval (%s); it was denied all the same",
                        type(exc).__name__)
            return
        self._changed([f"{PREFIX}:{request.req_id}"], "deferred")

    def granted(self, request) -> None:
        self._changed([], "deferral_granted")

    def _changed(self, ids, reason) -> None:
        try:
            self.publish({"kind": "inbox_changed", "data": {"ids": ids, "reason": reason}})
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Cannot announce a deferral (%s)", type(exc).__name__)


# -- the inbox's rows (QH-5) -----------------------------------------------------------

def _row_detail(rec, *, expired: bool) -> dict:
    grantable = not rec["hud_only"] and rec["digest"] is not None and not expired
    return {"deferral_id": rec["id"], "tool": rec["tool"], "card": rec["card"],
            "thread_id": rec["thread_id"], "task_id": rec["task_id"], "at": rec["at"],
            "until": rec["until"], "grantable": grantable, "expired": expired,
            "hud_only": rec["hud_only"]}


def rows(daemon, row) -> list[dict]:
    """The deferred rows: waiting ones at rank 4 for `WAITING_S`, then rank 8
    until `SHOWN_S`. `row` is `inbox._row`. Titles and blocks are code's."""
    store = getattr(getattr(daemon, "inbox", None), "deferred", None)
    if store is None:
        return []
    out = []
    now = store.clock()
    for rec in store.all():
        if rec["state"] != "waiting":
            continue
        at = _parse(rec["at"])
        if at is None:
            continue
        age = (now - at).total_seconds()
        if age >= SHOWN_S:
            continue
        expired = age >= WAITING_S
        tool = clean_line(rec["tool"] or "a tool", 60)
        thread_id, task_id = rec["thread_id"], rec["task_id"]
        who = f"task {task_id}" if task_id else f"chat {thread_id}" if thread_id else "a turn"
        if expired:
            title = f"Deferred overnight and not approved: {tool}"
            blocks = f"{who} was denied it; over a day old, so it can only be left"
        else:
            title = f"{tool} is waiting for your morning approval"
            blocks = (f"{who} was told to carry on without it" if not rec["hud_only"]
                      else f"{who} was told to carry on without it; a Browser action is approved only live")
        out.append(row(KIND, RANK_INFO if expired else RANK_WAITING, f"{PREFIX}:{rec['id']}", title, blocks,
                       thread_id=thread_id, task_id=task_id, asked_at=_iso(min(at, now)),
                       origin=rec["origin"], detail=_row_detail(rec, expired=expired)))
    return out


# -- the owner's answer (QH-6, QH-7) -------------------------------------------------

NOTES = {"started": "Approved once for 30 minutes; the chat starts a turn with the news now.",
         "queued": "Approved once for 30 minutes; the chat hears it after the turn already running.",
         "resumed": "Approved once for 30 minutes; the blocked task was resumed.",
         "granted": "Approved once for 30 minutes; the task was not blocked, so it was not told.",
         "closed": "Approved once for 30 minutes; the asking chat is closed, so it was not told.",
         "failed": "Approved once for 30 minutes; the asking chat could not be told.",
         "left": "Left: nothing was sent."}


def answer(daemon, record_id, body, *, via: str = "inbox", verified: bool = False) -> dict:
    """The route's half (one row, one body): `{action: "retry"}` (Approve and
    retry) or `{action: "leave"}` (Leave it), through `approve_and_retry` or
    `leave`, a refused result raised as its status. Callers: `inbox.answer`
    (the HUD's owner-only route, `via="inbox"`) and `remote_answer` (the
    phone, `via="phone"`, `verified` true only after a passkey assertion
    bound to this deferral). 400 for a malformed body."""
    from .daemon import APIError, _object
    body = _object(body, ("action",), ("action",))
    action = body["action"]
    if action not in ("retry", "leave"):
        raise APIError(400, "action must be retry or leave")
    result = (approve_and_retry(daemon, record_id, via=via, verified=verified) if action == "retry"
              else leave(daemon, record_id, via=via))
    if not result["ok"]:
        raise APIError(result["status"], result["error"])
    return result


def _refused(record_id, status: int, error: str) -> dict:
    return {"ok": False, "id": f"{PREFIX}:{record_id}", "status": status, "error": error}


def _settled(daemon, record_id, action: str, *, via: str, verified: bool) -> dict | tuple:
    """Shared checks and the store's change: the record, or a refusal."""
    if not isinstance(record_id, str) or not _ID.match(record_id):
        return _refused(record_id, 400, "malformed deferral id")
    if via not in ("inbox", "phone"):
        return _refused(record_id, 400, "unknown answer path")
    if action == "retry" and via == "phone" and verified is not True:
        return _refused(record_id, 401, "Approve and retry needs the passkey")
    store = getattr(getattr(daemon, "inbox", None), "deferred", None)
    if store is None:
        return _refused(record_id, 409, "the inbox has no store here")
    try:
        record = store.settle(record_id, action)
    except LookupError:
        return _refused(record_id, 409, "this item is no longer waiting; it was answered or its source has gone")
    except ValueError as exc:
        return _refused(record_id, 409, str(exc))
    except OSError as exc:
        LOG.warning("Cannot write a deferral's answer (%s)", type(exc).__name__)
        return _refused(record_id, 500, "the answer could not be saved")
    seen = getattr(daemon, "owner_seen", None)
    if callable(seen) and (via == "inbox" or verified is True):
        seen()                      # QH-3: the HUD's answer, or the phone's verified one
    try:
        daemon.bus.publish({"kind": "inbox_changed",
                            "data": {"ids": [f"{PREFIX}:{record_id}"], "reason": "deferral_answered"}})
    except Exception as exc:  # noqa: BLE001
        LOG.warning("Cannot announce a deferral's answer (%s)", type(exc).__name__)
    return (record,)


def approve_and_retry(daemon, record_id, *, via: str = "inbox", verified: bool = False) -> dict:
    """QH-6 for one deferred id: the grant, then the asking chat told or its
    blocked task resumed. Never raises for a refusal: returns a per-item
    result, `{ok: True, id, delivery, note, grant_until}` or `{ok: False,
    id, status, error}`, so a caller answering several ids under one owner
    confirm (the morning review) gets one result per id. `via="phone"`
    needs `verified` true (a passkey assertion that covered this id)."""
    out = _settled(daemon, record_id, "retry", via=via, verified=verified)
    if isinstance(out, dict):
        return out
    record = out[0]
    delivery = _tell(daemon, record, via)
    return {"ok": True, "id": f"{PREFIX}:{record_id}", "delivery": delivery, "note": NOTES[delivery],
            "grant_until": record["grant_until"]}


def leave(daemon, record_id, *, via: str = "inbox") -> dict:
    """QH-7 for one deferred id: the row goes, nothing is sent. A per-item
    result, as `approve_and_retry`'s. Needs no passkey on the phone."""
    out = _settled(daemon, record_id, "leave", via=via, verified=False)
    if isinstance(out, dict):
        return out
    return {"ok": True, "id": f"{PREFIX}:{record_id}", "delivery": "left", "note": NOTES["left"]}


def _tell(daemon, record, via: str) -> str:
    """QH-6, 2: a blocked task is resumed; a chat is told through B1's
    delivery (never a steer, no desk authority). The grant stands whatever
    this says."""
    from .daemon import APIError, DaemonError
    from .model import TaskState
    task_id, thread_id = record["task_id"], record["thread_id"]
    if task_id:
        runner = getattr(daemon, "runner", None)
        try:
            task = daemon.stores.tasks.get(task_id)
        except Exception:  # noqa: BLE001
            task = None
        if task is None or runner is None or task.state != TaskState.BLOCKED:
            return "granted"
        try:
            runner.resume(task_id)
        except Exception as exc:  # noqa: BLE001 — the grant stands; the task stays blocked
            LOG.info("A deferral's task was not resumed (%s)", type(exc).__name__)
            return "granted"
        return "resumed"
    if not thread_id:
        return "closed"
    from .projects import is_archived
    from .provider import UserMessage

    def closed() -> bool:
        try:
            thread = daemon.stores.threads.get(thread_id)
            return thread is None or bool(thread.archived) or is_archived(daemon.stores, thread.project_id)
        except Exception:  # noqa: BLE001
            return True
    if closed():
        return "closed"
    text = retry_message(record)
    message = UserMessage(text, origin="owner", via="phone" if via == "phone" else "inbox",
                          desk=False, typed=text)
    try:
        out = daemon.deliver_queued(thread_id, message)
    except (DaemonError, APIError) as exc:
        LOG.info("A deferral's news was not delivered (%s)", type(exc).__name__)
        return "closed" if closed() else "failed"
    except Exception:  # noqa: BLE001
        LOG.exception("Delivering a deferral's news failed")
        return "closed" if closed() else "failed"
    status = out.get("status") if isinstance(out, dict) else None
    return status if status in ("started", "queued") else "failed"
