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
reason, the fast path's `Refusal`, the jarvis-mcp bridge's
`_unasked_refusal`, the escape hatch's message. A Codex approval's decline
carries no text, so Codex chats are told in their instructions
(`codex_config.quiet_note`) and the chat's log says why.

* **Present** (QH-3) is `Daemon.owner_present()`: the owner's input within
  `PRESENT_S` (15 minutes), the age being the larger of the wall-clock and
  the monotonic ages, stamped by `Daemon.owner_seen()` at the owner's HUD
  message (the `is_owner` test that grants the desk), the HUD's answer to
  an approval, the HUD's new VEX project, the phone's answer after a
  verified passkey assertion, and the owner's own answer here.
* **Never deferred:** a request carrying `discord_channel_id` (the owner's
  `/project new`, typed a moment ago) or `owner_initiated` (the HUD's new
  VEX project). Both are set by daemon code only.
* **Not quiet when it cannot be read.** `quiet.py` reads a malformed, linked
  or oversized `quiet.json` as its default (right for the phone's push).
  Here such a file means "not quiet" (`setting_readable`). A missing file
  is the default. Anything that raises while deciding means "ask".
* Only daemon2's `main()` attaches the desk (`Daemon.attach_quiet_desk`).

**The record (QH-5).** `<data root>/inbox/deferred.json`, protected state:
per deferral the card's public projection (`to_json`, never `owner_json`),
`request_digest` (SHA-256 of everything the broker judged: thread, task,
tool, the unredacted arguments, the command, the folder the gate judged it
in, the headline, the layer, the sub-agent and the relay), whether it may
be granted (`grant_refusal`), and how many times it was asked. An
identical waiting ask from the same thread is the same record ("asked N
times"). At most `PER_THREAD` waiting records per thread a day; past that
the asks are still denied and counted in that thread's "and N more"
record (`defer:more.<thread>`). Eviction is per thread (the thread with
most records gives up its oldest) and by size, so one chat cannot push out
another's. A deferral that cannot be written is counted in memory and
still shows as an "and N more" row. A file that is not exactly ours shows
no row and matches no grant. Rows: rank 4 for `WAITING_S`, rank 8 until
`SHOWN_S`; never markable.

**Approve and retry (QH-6) and Leave it (QH-7)**, per id
(`approve_and_retry`, `leave`; `answer` is the route on top). A grant is
offered only for a plain owner ask of a chat or a task, whose whole card
the rows show: never a Browser (HUD-only) card, a Codex request, a sandbox
widening, an owner-only operation (the merge gate's, an always-ask rule's
or a dangerous Jarvis tool's, all asked every time), an ask with no chat
or task, or a card longer than the rows show. A task that is not
`blocked` is refused ("it is running; open the task") and its row stays.
The grant is single use for `GRANT_S` (by the wall clock, and in this
process by the monotonic one too), bound to the digest and the thread,
kept in an in-memory index (loaded once, updated on every write) that the
broker takes from under its lock; the spend is then written.

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
import time

from .approvals import clean_line

LOG = logging.getLogger(__name__)

NAME = "deferred.json"
PRESENT_S = 15 * 60             # QH-3: the owner's input this recently is presence
GRANT_S = 30 * 60               # QH-6: how long an Approve and retry's grant stands
WAITING_S = 24 * 3600           # QH-5: rank 4 for a day, then rank 8 information
SHOWN_S = 7 * 24 * 3600         # rank 8 rows show this long after the deferral
KEEP = 200                      # records kept in all
PER_THREAD = 20                 # waiting records per thread a day; past it, "and N more"
RANK_WAITING = 4
RANK_INFO = 8
KIND = "deferred"
PREFIX = "defer"
MORE = "more."                  # `defer:more.<thread or "none">`: the "and N more" row
GRANTED = "granted from the inbox"
DEFERRED = "deferred"
STATES = frozenset({"waiting", "granted", "used", "left"})
# What the rows show in full (the HUD's `textBlock` caps, the phone's are
# larger): a card that is longer is shown cut and never granted (L1).
SHOWN_TEXT = 3900
SHOWN_REASON = 1000
SHOWN_HEADLINE = 400
SHOWN_LIST = 200
_CARD_MAX = 16 * 1024           # a card larger than this is kept cut
_FILE_MAX = 8 * 1024 * 1024
_WRITE_MAX = 4 * 1024 * 1024    # what one write may hold; eviction keeps under it
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_KEY = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
# What of a card's public projection a record keeps: another surface's
# handles (the Discord code and channel) and the wait are dropped.
_CARD_DROPPED = ("code", "discord_channel_id", "timeout_s")
_FIELDS = frozenset({"id", "tool", "thread_id", "task_id", "provider", "origin", "hud_only", "card",
                     "digest", "grantable", "why_not", "count", "first_at", "at", "until", "state",
                     "grant_until", "settled_at"})
_MORE_FIELDS = frozenset({"thread_id", "task_id", "count", "first_at", "at"})
# Why a record may not be granted, as codes the HUD and phone put in their own words.
WHY_NOT = frozenset({"", "browser", "codex", "hatch", "owner-only", "no-chat", "relayed", "subagent",
                     "folder", "too-large", "unreadable"})
HATCH_LAYER = "escape-hatch"     # `hatch.EscapeHatch`'s asks: never granted (lead, #326 delta review)
MORE_KEEP = 100                  # "and N more" counters kept; past it the oldest fold into ALL
ALL = "_all"                     # the one counter for every chat folded away
UNREADABLE = NAME + ".unreadable"

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

def folder_identity(path) -> dict | None:
    """The folder a call was judged in, as the filesystem names it: its real
    path and the device and inode of what that resolves to, so a link
    retargeted between night and morning is another folder. `{"missing":
    path}` when it does not exist or cannot be read (never grantable, never
    matched); None for no folder."""
    if path is None:
        return None
    try:
        real = os.path.realpath(path)
        info = os.stat(real)
        if not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError(real)
    except (OSError, ValueError, TypeError):
        return {"missing": str(path)}
    return {"path": real, "dev": info.st_dev, "ino": info.st_ino}


def request_digest(request) -> str | None:
    """SHA-256 of everything the broker judged, as one canonical JSON text
    (sorted keys, no spaces, ASCII): the thread, task, tool, the unredacted
    arguments, the command, the folder the gate judged it in
    (`folder_identity(cwd)`), the headline, the layer, the sub-agent and the
    relayed turn. None when that cannot be written exactly: such a call can
    never be granted."""
    try:
        canon = json.dumps({"v": 3, "thread_id": request.thread_id, "task_id": request.task_id,
                            "tool": request.tool, "args": request.args, "command": request.command,
                            "cwd": folder_identity(getattr(request, "cwd", None)), "headline": request.headline,
                            "layer": request.layer, "subagent": request.subagent,
                            "relayed_from": request.relayed_from, "provider": request.provider},
                           sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return None
    return hashlib.sha256(canon.encode("ascii")).hexdigest()


def _owner_only(request) -> bool:
    """An ask that is asked every time, never Always: a sandbox widening, the
    merge gate's or an always-ask rule's (layer 2), a dangerous Jarvis tool's,
    any card with a leading headline, or one the permit marked so."""
    from .permissions import L_ALWAYS_ASK, L_WIDENING
    return (getattr(request, "grantable", True) is not True or bool(request.headline)
            or request.layer in (L_WIDENING, L_ALWAYS_ASK, HATCH_LAYER))


def _fits(value, depth=0) -> bool:
    """Every string and list in a card short enough for the rows to show whole."""
    if depth > 8:
        return False
    if isinstance(value, str):
        return len(value) <= SHOWN_TEXT
    if isinstance(value, (list, tuple)):
        return len(value) <= SHOWN_LIST and all(_fits(v, depth + 1) for v in value)
    if isinstance(value, dict):
        return len(value) <= SHOWN_LIST and all(_fits(k, depth + 1) and _fits(v, depth + 1)
                                                for k, v in value.items())
    return True


def grant_refusal(request, card: dict, whole: bool) -> str:
    """Why Approve and retry is not offered for this ask, as a `WHY_NOT`
    code, or "" when it is."""
    if request.private:
        return "browser"
    if request.provider == "codex":
        return "codex"
    if request.layer == HATCH_LAYER:
        return "hatch"
    if _owner_only(request) or request.discord_channel_id is not None:
        return "owner-only"
    if not request.thread_id and not request.task_id:
        return "no-chat"
    # Each relayed turn and each native sub-agent spawn is its own: such a
    # call can never be asked again identically, so no grant is offered.
    if request.relayed_from:
        return "relayed"
    if request.subagent:
        return "subagent"
    folder = folder_identity(getattr(request, "cwd", None))
    if folder is not None and "missing" in folder:
        return "folder"
    if not whole or card.get("cut") or not _fits(card):
        return "too-large"
    for key, cap in (("command", SHOWN_TEXT), ("reason", SHOWN_REASON), ("headline", SHOWN_HEADLINE)):
        if isinstance(card.get(key), str) and len(card[key]) > cap:
            return "too-large"
    try:
        if len(json.dumps(card.get("args") or {}, indent=2, ensure_ascii=False)) > SHOWN_TEXT:
            return "too-large"
    except (TypeError, ValueError, RecursionError):
        return "unreadable"
    if request_digest(request) is None:
        return "unreadable"
    return ""


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
            f"tool and the same arguments, from this chat, in the same folder) is allowed, once, until "
            f"{stamp}; anything else asks the owner as usual.")


def public_card(request) -> tuple[dict, bool]:
    """(the card a record keeps, whether it was kept whole). The public
    projection (`to_json`; a HUD-only card's is its private one), minus
    another surface's handles. A card too large to keep is cut to its tool,
    ids and the first 2,000 characters of its command, and marked `cut`."""
    card = {k: v for k, v in request.to_json().items() if k not in _CARD_DROPPED}
    try:
        text = json.dumps(card, ensure_ascii=True, allow_nan=False, default=str)
        if len(text) <= _CARD_MAX:
            return json.loads(text), True
    except (TypeError, ValueError, RecursionError):
        pass
    command = card.get("command") if isinstance(card.get("command"), str) else ""
    return ({"req_id": card.get("req_id"), "tool": card.get("tool"), "thread_id": card.get("thread_id"),
             "task_id": card.get("task_id"), "provider": card.get("provider"),
             "command": command[:2000] + ("…" if len(command) > 2000 else ""), "cut": True}, False)


# -- the setting -------------------------------------------------------------------

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

def _count_ok(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 10**9


def _opt_id(value) -> bool:
    return value is None or (isinstance(value, str) and 0 < len(value) <= 200)


def _valid_record(key, rec) -> bool:
    if not isinstance(key, str) or not _ID.match(key) or not isinstance(rec, dict) or set(rec) != _FIELDS:
        return False
    if rec["id"] != key or rec["state"] not in STATES or not isinstance(rec["hud_only"], bool):
        return False
    if not isinstance(rec["grantable"], bool) or rec["why_not"] not in WHY_NOT or not _count_ok(rec["count"]):
        return False
    if rec["grantable"] != (rec["why_not"] == "") or (rec["grantable"] and rec["hud_only"]):
        return False
    if not isinstance(rec["tool"], str) or not isinstance(rec["origin"], str) or not isinstance(rec["card"], dict):
        return False
    if not all(_opt_id(rec[n]) for n in ("thread_id", "task_id", "provider")):
        return False
    if rec["digest"] is not None and not (isinstance(rec["digest"], str) and _DIGEST.match(rec["digest"])):
        return False
    if rec["grantable"] and rec["digest"] is None:
        return False
    if _parse(rec["at"]) is None or _parse(rec["first_at"]) is None:
        return False
    for name in ("until", "grant_until", "settled_at"):
        if rec[name] is not None and _parse(rec[name]) is None:
            return False
    if rec["state"] in ("granted", "used") and (rec["grant_until"] is None or not rec["grantable"]):
        return False
    return True


def _valid_more(key, entry) -> bool:
    return (isinstance(key, str) and bool(_KEY.match(key)) and isinstance(entry, dict)
            and set(entry) == _MORE_FIELDS and _opt_id(entry["thread_id"]) and _opt_id(entry["task_id"])
            and _count_ok(entry["count"]) and _parse(entry["at"]) is not None
            and _parse(entry["first_at"]) is not None)


def valid(data) -> tuple[dict, dict] | None:
    """(records, "and N more" counters), exactly; None for anything else
    (then no row shows and no grant matches)."""
    if not isinstance(data, dict) or set(data) != {"v", "records", "more"} or data["v"] != 2:
        return None
    records, more = data["records"], data["more"]
    if not isinstance(records, dict) or len(records) > KEEP or not isinstance(more, dict) or len(more) > KEEP:
        return None
    if not all(_valid_record(k, r) for k, r in records.items()):
        return None
    if not all(_valid_more(k, e) for k, e in more.items()):
        return None
    return records, more


def thread_key(thread_id) -> str:
    """The "and N more" counter's key for a thread (or "none")."""
    return thread_id if isinstance(thread_id, str) and _KEY.match(thread_id) and thread_id != ALL else "none"


class DeferredStore:
    """`<data root>/inbox/deferred.json`. Every write holds `_lock`, reading,
    changing and replacing the file in one step. The live grants are also
    kept in memory (`_live`, under `_index_lock`), loaded once and updated
    on every write, so the broker takes one without reading the file."""

    def __init__(self, root, *, clock=None, mono=None):
        self.root = Path(root)
        self.path = self.root / "inbox" / NAME
        self.clock = clock or _utcnow
        self.mono = mono or time.monotonic
        self._lock = threading.RLock()
        self._index_lock = threading.Lock()
        # digest -> [{id, thread_id, until (wall), mono (this process's deadline or None)}]
        self._live: dict[str, list[dict]] | None = None
        # Deferrals that could not be written: thread key -> {thread_id, task_id, count, at}.
        self._unwritten: dict[str, dict] = {}
        # Grants taken in this process: never live again here, even if
        # writing their use failed and the file still says "granted".
        self._spent: set[str] = set()

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

    # `DecisionStore._subdir` reads this off its `self`.
    _DIR = _DIR

    def _read(self, dir_fd: int) -> tuple[dict, dict] | None:
        """(records, more); empty when there is no file, None when it is not ours."""
        try:
            fd = os.open(NAME, _READ, dir_fd=dir_fd)
        except FileNotFoundError:
            return {}, {}
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
    def _encode(records: dict, more: dict) -> bytes:
        return json.dumps({"v": 2, "records": records, "more": more}, ensure_ascii=True, allow_nan=False,
                          separators=(",", ":")).encode("ascii")

    def _write(self, dir_fd: int, records: dict, more: dict) -> None:
        data = self._encode(records, more)
        if len(data) > _WRITE_MAX:
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
        """`change(records, more, now) -> result` under the lock; written when
        it returned something other than None. A file that is present but not
        ours reads as empty (an answer finds nothing, no grant stands); before
        a new deferral is written over it, it is kept aside as `UNREADABLE`
        (#326 delta review, MED 2), never silently replaced. A write that
        would leave a file `valid` refuses is never made."""
        with self._lock:
            fd = self._open(create=True)
            try:
                current = self._read(fd)
                if current is None:
                    LOG.warning("The deferred approvals file is not one of ours; it holds nothing")
                records, more = current if current is not None else ({}, {})
                result = change(records, more, self.clock())
                if result is not None:
                    if valid({"v": 2, "records": records, "more": more}) is None:
                        raise ValueError("the deferred records would not read back; nothing was written")
                    if current is None:
                        os.replace(NAME, UNREADABLE, src_dir_fd=fd, dst_dir_fd=fd)
                        LOG.warning("The unreadable deferred approvals file was kept aside as %s", UNREADABLE)
                    self._write(fd, records, more)
                    self._reindex(records)
                return result
            finally:
                os.close(fd)

    # -- the live grants (L6) ----------------------------------------------------------

    def _reindex(self, records: dict) -> None:
        """The in-memory grants after a write: each `granted` record, its
        process deadline kept when it was minted here."""
        with self._index_lock:
            old = {e["id"]: e.get("mono") for entries in (self._live or {}).values() for e in entries}
            live: dict[str, list[dict]] = {}
            for rec in records.values():
                if (rec["state"] == "granted" and rec["grantable"] and rec["digest"]
                        and rec["id"] not in self._spent):
                    live.setdefault(rec["digest"], []).append(
                        {"id": rec["id"], "thread_id": rec["thread_id"], "until": rec["grant_until"],
                         "mono": old.get(rec["id"])})
            self._live = live

    def load_index(self) -> None:
        """Read the live grants once (`attach_quiet_desk`). Never raises: a
        file that cannot be read holds no grant."""
        try:
            with self._lock:
                fd = self._open(create=False)
                current = None
                if fd is not None:
                    try:
                        current = self._read(fd)
                    finally:
                        os.close(fd)
                self._reindex(current[0] if current else {})
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Cannot read the deferral grants (%s); none stands", type(exc).__name__)
            with self._index_lock:
                self._live = {}

    def take(self, digest: str, thread_id) -> str | None:
        """Under the broker's lock: the one live grant for exactly `digest`
        and this thread, unexpired by the wall clock and (when minted here)
        the monotonic one, taken from memory; its id, or None. Never reads
        the file (`load_index` did, once); `spend` writes the use."""
        if not isinstance(digest, str) or not _DIGEST.match(digest):
            return None
        if self._live is None:
            self.load_index()
        now, mono = self.clock(), self.mono()
        with self._index_lock:
            for entry in list((self._live or {}).get(digest, ())):
                until = _parse(entry["until"])
                if entry["thread_id"] != thread_id or until is None or not now < until:
                    continue
                if entry["mono"] is not None and not mono < entry["mono"]:
                    continue
                self._live[digest].remove(entry)
                self._spent.add(entry["id"])
                return entry["id"]
        return None

    def spend(self, record_id: str) -> bool:
        """Write a taken grant's use; True when it is on disk. Never raises: in
        this process the grant is already gone from memory, and a caller that
        gets False must not use it (#326 delta review, LOW 3)."""
        def change(records, more, now):
            rec = records.get(record_id)
            if rec is None or rec["state"] != "granted":
                return None
            rec["state"] = "used"
            rec["settled_at"] = _iso(now)
            return True
        try:
            return self._change(change) is True
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Cannot write a deferral grant's use (%s); it is not used, and spent in memory",
                        type(exc).__name__)
            return False

    # -- reads -----------------------------------------------------------------------

    def snapshot(self) -> tuple[list[dict], dict]:
        """(records, more counters, the unwritten ones folded in), copied;
        empty when the file is missing or not ours. Never raises."""
        try:
            with self._lock:
                fd = self._open(create=False)
                current = None
                if fd is not None:
                    try:
                        current = self._read(fd)
                    finally:
                        os.close(fd)
                else:
                    current = ({}, {})
                unwritten = {k: dict(v) for k, v in self._unwritten.items()}
        except Exception as exc:  # noqa: BLE001 — unreadable records show nothing
            LOG.warning("Cannot read the deferred approvals (%s)", type(exc).__name__)
            current, unwritten = None, {k: dict(v) for k, v in self._unwritten.items()}
        if current is None:
            current = ({}, {})
        records, more = current
        more = {k: dict(v) for k, v in more.items()}
        for key, extra in unwritten.items():
            mine = more.get(key)
            if mine is None:
                more[key] = extra
            else:
                mine["count"] += extra["count"]
                mine["at"] = max(mine["at"], extra["at"])
        return [dict(r) for r in records.values()], more

    def all(self) -> list[dict]:
        return self.snapshot()[0]

    def get(self, record_id) -> dict | None:
        return next((r for r in self.all() if r["id"] == record_id), None)

    # -- writes ----------------------------------------------------------------------

    def add(self, request, until: datetime | None) -> str:
        """Record one deferral (`QuietDesk.deferred`): a new record, the same
        waiting record asked again, or a thread's "and N more". Returns the
        row id. A write that fails is counted in memory (still a row) and
        re-raised; the deny already stands."""
        card, whole = public_card(request)
        private = bool(request.private)
        why_not = grant_refusal(request, card, whole)
        digest = request_digest(request)
        thread_id = _ident(request.thread_id)
        task_id = None if private else _ident(request.task_id)
        key = thread_key(thread_id)

        def change(records, more, now):
            stamp = _iso(now)
            for old in [k for k, v in records.items()
                        if (_parse(v["at"]) or now) <= now - timedelta(seconds=SHOWN_S)]:
                records.pop(old)
            for old in [k for k, v in more.items()
                        if (_parse(v["at"]) or now) <= now - timedelta(seconds=SHOWN_S)]:
                more.pop(old)
            # The same call asked again while its record waits: one row, counted.
            if digest is not None:
                for rec in records.values():
                    if rec["state"] == "waiting" and rec["thread_id"] == thread_id and rec["digest"] == digest:
                        rec["count"] = min(rec["count"] + 1, 10**9)
                        rec["at"] = stamp
                        if until is not None:
                            rec["until"] = _iso(until)
                        return f"{PREFIX}:{rec['id']}"
            day = now - timedelta(seconds=WAITING_S)
            mine = [r for r in records.values() if r["thread_id"] == thread_id and r["state"] == "waiting"
                    and (_parse(r["at"]) or now) > day]
            if len(mine) >= PER_THREAD:
                entry = more.get(key)
                if entry is None:
                    more[key] = {"thread_id": thread_id, "task_id": task_id, "count": 1,
                                 "first_at": stamp, "at": stamp}
                else:
                    entry["count"] = min(entry["count"] + 1, 10**9)
                    entry["at"] = stamp
                self._evict(records, more, keep=None)
                return f"{PREFIX}:{MORE}{key}"
            records[request.req_id] = {
                "id": request.req_id, "tool": clean_line(request.tool or "", 120),
                "thread_id": thread_id, "task_id": task_id,
                "provider": None if private else _ident(request.provider),
                "origin": "" if private else clean_line(request.origin or "", 160),
                "hud_only": private, "card": card, "digest": digest,
                "grantable": why_not == "", "why_not": why_not, "count": 1, "first_at": stamp, "at": stamp,
                "until": _iso(until) if until is not None else None, "state": "waiting",
                "grant_until": None, "settled_at": None}
            self._evict(records, more, keep=request.req_id)
            return f"{PREFIX}:{request.req_id}"
        try:
            return self._change(change)
        except Exception:
            with self._lock:
                now = _iso(self.clock())
                entry = self._unwritten.get(key)
                if entry is None:
                    self._unwritten[key] = {"thread_id": thread_id, "task_id": task_id, "count": 1,
                                            "first_at": now, "at": now}
                else:
                    entry["count"] += 1
                    entry["at"] = now
            raise

    def _evict(self, records: dict, more: dict, keep) -> None:
        """Past `KEEP` records or `_WRITE_MAX` bytes: settled records first,
        oldest first; then the thread holding the most records gives up its
        oldest (never `keep`, never a live grant), so one chat cannot push
        out another's rows."""
        def over():
            return len(records) > KEEP or len(self._encode(records, more)) > _WRITE_MAX

        def oldest(candidates):
            return min(candidates, key=lambda k: (records[k]["at"], k))
        while over():
            settled = [k for k, r in records.items() if r["state"] in ("left", "used") and k != keep]
            if settled:
                records.pop(oldest(settled))
                continue
            by_thread: dict = {}
            for k, r in records.items():
                if k != keep and r["state"] != "granted":
                    by_thread.setdefault(r["thread_id"], []).append(k)
            if not by_thread:
                break
            biggest = max(by_thread.values(), key=lambda ks: (len(ks), sum(len(json.dumps(records[k]["card"]))
                                                                        for k in ks)))
            gone = oldest(biggest)
            rec = records.pop(gone)
            # Never silent: what is evicted waiting is counted in its thread's "more".
            if rec["state"] == "waiting":
                key = thread_key(rec["thread_id"])
                entry = more.get(key)
                if entry is None:
                    more[key] = {"thread_id": rec["thread_id"], "task_id": rec["task_id"], "count": rec["count"],
                                 "first_at": rec["first_at"], "at": rec["at"]}
                else:
                    entry["count"] = min(entry["count"] + rec["count"], 10**9)
        self._bound_more(more)

    @staticmethod
    def _bound_more(more: dict) -> None:
        """At most `MORE_KEEP` counters (#326 delta review, MED 2): past it the
        oldest chats' counters fold into the one `ALL` counter, so nothing is
        lost and the file a write leaves is always one `valid` reads."""
        while len(more) > MORE_KEEP:
            folded = [k for k in more if k != ALL]
            key = min(folded, key=lambda k: (more[k]["at"], k))
            gone = more.pop(key)
            entry = more.get(ALL)
            if entry is None:
                more[ALL] = {"thread_id": None, "task_id": None, "count": gone["count"],
                             "first_at": gone["first_at"], "at": gone["at"]}
            else:
                entry["count"] = min(entry["count"] + gone["count"], 10**9)
                entry["first_at"] = min(entry["first_at"], gone["first_at"])
                entry["at"] = max(entry["at"], gone["at"])

    def settle(self, record_id: str, action: str, *, check=None) -> dict:
        """The owner's Approve and retry ("retry": waiting → granted, for
        `GRANT_S`) or Leave it ("leave": waiting → left; for a `more.` key,
        the counter goes). `check(record)` may refuse first (ValueError).
        LookupError for no such record; ValueError, with a fixed sentence,
        for one this answer does not fit."""
        if action not in ("retry", "leave"):
            raise ValueError("action must be retry or leave")
        if record_id.startswith(MORE):
            key = record_id[len(MORE):]
            if action != "leave":
                raise ValueError("these asks cannot be approved from here; open the chat to ask again")
            with self._lock:
                found = self._unwritten.pop(key, None)

            def change_more(records, more, now):
                return more.pop(key, None) or found
            out = self._change(change_more)
            if out is None:
                raise LookupError(record_id)
            return {"id": record_id, "state": "left", "grant_until": None, "task_id": out.get("task_id"),
                    "thread_id": out.get("thread_id")}

        def change(records, more, now):
            rec = records.get(record_id)
            if rec is None:
                raise LookupError(record_id)
            if rec["state"] != "waiting":
                raise ValueError("this deferral was already answered")
            if action == "retry":
                if not rec["grantable"] or rec["hud_only"] or rec["digest"] is None:
                    raise ValueError("this call cannot be approved from the inbox; open its chat and "
                                     "ask again there")
                at = _parse(rec["at"])
                if at is None or now - at >= timedelta(seconds=WAITING_S):
                    raise ValueError("this deferral is over a day old; it can only be left")
                if check is not None:
                    check(rec)
                rec["state"] = "granted"
                rec["grant_until"] = _iso(now + timedelta(seconds=GRANT_S))
            else:
                rec["state"] = "left"
            rec["settled_at"] = _iso(now)
            return dict(rec)
        mono = self.mono()
        out = self._change(change)
        if action == "retry":
            with self._index_lock:
                for entry in (self._live or {}).get(out["digest"], ()):
                    if entry["id"] == record_id:
                        entry["mono"] = mono + GRANT_S
        return out


def _ident(value):
    return value if isinstance(value, str) and value and len(value) <= 200 else None


# -- the broker's hook -----------------------------------------------------------------

class QuietDesk:
    """What the broker asks at each ask (`settle`), and what it tells after a
    deferral or a grant. Attached by `Daemon.attach_quiet_desk` only."""

    def __init__(self, quiet, present, store: DeferredStore, publish):
        self.quiet = quiet
        self.present = present
        self.store = store
        self.publish = publish
        self._taken: dict[str, str] = {}
        self._taken_lock = threading.Lock()

    def settle(self, request):
        """Under the broker's lock: ("granted", text) when the owner's grant
        for exactly this call is taken now, ("deferred", text) when the ask
        is deferred, None to ask as usual. Never raises; reads no file once
        the grants are loaded."""
        try:
            if not request.private and not _owner_only(request) and request.provider != "codex":
                digest = request_digest(request)
                taken = self.store.take(digest, request.thread_id) if digest is not None else None
                if taken is not None:
                    with self._taken_lock:
                        self._taken[request.req_id] = taken
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
        if request.discord_channel_id is not None or getattr(request, "owner_initiated", False) is True:
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
            row_id = self.store.add(request, until)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Cannot record a deferred approval (%s); it is counted, and was denied all the same",
                        type(exc).__name__)
            row_id = f"{PREFIX}:{MORE}{thread_key(_ident(request.thread_id))}"
        self._changed([row_id], "deferred")

    def granted(self, request) -> bool:
        """After the broker took a grant, before it allows the call: the use
        written. False (the broker then denies) when it cannot be, so a grant
        is never used without its use on disk."""
        with self._taken_lock:
            taken = self._taken.pop(request.req_id, None)
        if taken is None:
            return False
        written = self.store.spend(taken)
        self._changed([f"{PREFIX}:{taken}"], "deferral_granted")
        return written

    def _changed(self, ids, reason) -> None:
        try:
            self.publish({"kind": "inbox_changed", "data": {"ids": ids, "reason": reason}})
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Cannot announce a deferral (%s)", type(exc).__name__)


# -- the inbox's rows (QH-5) -----------------------------------------------------------

def _who(thread_id, task_id) -> str:
    return f"task {task_id}" if task_id else f"chat {thread_id}" if thread_id else "a turn"


def rows(daemon, row) -> list[dict]:
    """The deferred rows: waiting ones at rank 4 for `WAITING_S`, then rank 8
    until `SHOWN_S`; one "and N more" row per thread past its cap. `row` is
    `inbox._row`. Titles and blocks are code's."""
    store = getattr(getattr(daemon, "inbox", None), "deferred", None)
    if store is None:
        return []
    out = []
    now = store.clock()
    records, more = store.snapshot()
    for rec in records:
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
        who = _who(thread_id, task_id)
        times = f" (asked {rec['count']} times)" if rec["count"] > 1 else ""
        if expired:
            title = f"Deferred overnight and not approved: {tool}{times}"
            blocks = f"{who} was denied it; over a day old, so it can only be left"
        else:
            title = f"{tool} is waiting for your morning approval{times}"
            blocks = f"{who} was told to carry on without it"
        detail = {"deferral_id": rec["id"], "tool": rec["tool"], "card": rec["card"],
                  "thread_id": thread_id, "task_id": task_id, "at": rec["at"], "until": rec["until"],
                  "grantable": rec["grantable"] and not expired, "why_not": rec["why_not"],
                  "expired": expired, "hud_only": rec["hud_only"], "count": rec["count"], "more": 0}
        out.append(row(KIND, RANK_INFO if expired else RANK_WAITING, f"{PREFIX}:{rec['id']}", title, blocks,
                       thread_id=thread_id, task_id=task_id, asked_at=_iso(min(at, now)),
                       origin=rec["origin"], detail=detail))
    for key, entry in more.items():
        at = _parse(entry["at"])
        if at is None or (now - at).total_seconds() >= SHOWN_S:
            continue
        expired = (now - at).total_seconds() >= WAITING_S
        thread_id, task_id = entry["thread_id"], entry["task_id"]
        who = "other chats" if key == ALL else _who(thread_id, task_id)
        detail = {"deferral_id": None, "tool": "", "card": {}, "thread_id": thread_id, "task_id": task_id,
                  "at": entry["at"], "until": None, "grantable": False, "why_not": "", "expired": expired,
                  "hud_only": False, "count": entry["count"], "more": entry["count"]}
        out.append(row(KIND, RANK_INFO if expired else RANK_WAITING, f"{PREFIX}:{MORE}{key}",
                       f"And {entry['count']} more asks from {who} were deferred overnight",
                       "denied, and too many to list one by one; ask again from its chat",
                       thread_id=thread_id, task_id=task_id, asked_at=_iso(min(at, now)), detail=detail))
    return out


# -- the owner's answer (QH-6, QH-7) -------------------------------------------------

NOTES = {"started": "Approved once for 30 minutes; the chat starts a turn with the news now.",
         "queued": "Approved once for 30 minutes; the chat hears it after the turn already running.",
         "resumed": "Approved once for 30 minutes; the blocked task was resumed.",
         "closed": "Approved once for 30 minutes; the asking chat is closed, so it was not told.",
         "failed": "Approved once for 30 minutes; the asking chat could not be told.",
         "unresumed": "Approved once for 30 minutes; the task could not be resumed: resume it from its task.",
         "left": "Left: nothing was sent."}
RUNNING = "the task is running, not blocked; open the task"


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


def _valid_id(record_id) -> bool:
    if not isinstance(record_id, str):
        return False
    if record_id.startswith(MORE):
        return bool(_KEY.match(record_id[len(MORE):]))
    return bool(_ID.match(record_id))


def _blocked_check(daemon):
    """Q2: a task's deferral is granted only while the task is `blocked`."""
    from .model import TaskState

    def check(rec):
        if not rec["task_id"]:
            return
        try:
            task = daemon.stores.tasks.get(rec["task_id"])
        except Exception:  # noqa: BLE001
            task = None
        if task is None or task.state != TaskState.BLOCKED or getattr(daemon, "runner", None) is None:
            raise ValueError(RUNNING)
    return check


def _settled(daemon, record_id, action: str, *, via: str, verified: bool) -> dict | tuple:
    """Shared checks and the store's change: the record, or a refusal."""
    if not _valid_id(record_id):
        return _refused(record_id, 400, "malformed deferral id")
    if via not in ("inbox", "phone"):
        return _refused(record_id, 400, "unknown answer path")
    if action == "retry" and via == "phone" and verified is not True:
        return _refused(record_id, 401, "Approve and retry needs the passkey")
    store = getattr(getattr(daemon, "inbox", None), "deferred", None)
    if store is None:
        return _refused(record_id, 409, "the inbox has no store here")
    try:
        record = store.settle(record_id, action, check=_blocked_check(daemon) if action == "retry" else None)
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
    """QH-7 for one deferred id (or an "and N more" counter): the row goes,
    nothing is sent. A per-item result, as `approve_and_retry`'s. Needs no
    passkey on the phone."""
    out = _settled(daemon, record_id, "leave", via=via, verified=False)
    if isinstance(out, dict):
        return out
    return {"ok": True, "id": f"{PREFIX}:{record_id}", "delivery": "left", "note": NOTES["left"]}


def _tell(daemon, record, via: str) -> str:
    """QH-6, 2: a blocked task is resumed; a chat is told through B1's
    delivery (never a steer, no desk authority). The grant stands whatever
    this says."""
    from .daemon import APIError, DaemonError
    task_id, thread_id = record["task_id"], record["thread_id"]
    if task_id:
        try:
            daemon.runner.resume(task_id)
        except Exception as exc:  # noqa: BLE001 — the grant stands; the task stays blocked
            LOG.info("A deferral's task was not resumed (%s)", type(exc).__name__)
            return "unresumed"
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
