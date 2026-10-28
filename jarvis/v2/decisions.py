"""Owner decisions: the store and the one way an agent posts one (owner inbox
design `docs/plans/2026-10-24-owner-inbox-design.md`, IN-5, §3.2, §8, §10
questions 1 and 2; inbox package B1).

A decision is an "answer whenever" question an agent puts to the owner: its
`question`, two to six `options` (text, never actions), an optional
`default` index and `why`. The owner answers it from the inbox's owner-only
decision route (`inbox.py`), which records the answer here and delivers it
to the chat that asked as an owner message (`via="inbox"`, queued, never a
steer, no desk authority). This module holds no answering path the agents
can reach: `settle` and `delivered` are called by that route alone
(`tests/v2/owner_post_check.py` keeps the tool's module and the post service
from naming them, and `tests/v2/archive_check.py` keeps this module from
naming the inbox's routes).

**Who may post (§10 question 1; the chat-to-chat binding rules mirrored).**
`OwnerPosts.post` re-checks under the daemon lock exactly what
`chat_relay.ChatRelay._refuse_sender` checks for `chat_send`: the caller's
live session, a running worker with a handle, the exact `turn_id`, not
cancelled, closing, retired or lost, `relay_turn is None` (a turn another
chat's message or reply started never posts), an owner's chat (role CHAT, no
task) and `desk.slot["present"] is True` (a turn the owner started at the
HUD, typed or spoken). On top of that the thread must be a Claude or Codex
chat: the fast path never posts, whatever it holds. Task orchestrators are
not bound yet (they have no session server; see the control-plane note).

**Bounds (§10 question 2).** `OPEN_PER_THREAD` (5) open decisions per thread
("5 per thread" read as 5 waiting at once), `PER_THREAD_DAY` (10) posted by
one thread and `PER_DAY` (20) posted in all in any rolling 24 hours, all
counted from the files under the store's lock, so a restart forgets nothing.
Field caps: question `QUESTION_MAX` (500), each option `OPTION_MAX` (120),
why `WHY_MAX` (1000). Over a cap is refused, never cut. Every field is
scrubbed with the strongest scrub there is (`chat_reads.scrub`: invisible
characters, typed credentials, Jarvis's own credential values, private keys,
the known key and bearer shapes); a scrub that fails refuses the post. Then
it is cleaned as the owner will see it (`display_text`, the sibling of
`chat_reads.card_title`'s rule: no enclosing marks, symbols, format
characters or Hangul fillers, at most two combining marks per character),
and judged again on that (review of PR #309): a question, why or option left
empty is refused, two options alike under NFKC and case-folding are refused,
and no option may say "(suggested)", the HUD's own marker. The asking thread's
provenance (`origin`, `approvals.label`'s rule for a task, "Provider · chat
‹title› (id)" for a chat) is recorded with it.

**The files.** `decisions/<id>.json` in the data root's `inbox` folder, one per decision,
fields per §3.2 plus `origin` and `delivery`. Both folders are 0700 and every
file 0600, tightened when found wider. Every open is relative to a folder
descriptor opened with `O_NOFOLLOW | O_DIRECTORY`, so a symlinked `inbox/` or
`decisions/` is refused; files are opened `O_NOFOLLOW | O_NONBLOCK` and
refused unless `fstat` says regular (a FIFO never blocks a read); a write is
a fresh `O_EXCL` temp file, fsynced, then `os.replace`d (atomic). The tree is
protected state (`files._inbox_tree`): no agent write tool, grant or shell
line writes it unasked. At most `KEEP` (500) files: the oldest answered ones
go first, and a post past that with none to drop is refused.

SELF_PROTECTED (`jarvis/tools/files.py`): it decides what an agent may put in
front of the owner and what the owner is told was asked.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import errno
import json
import logging
import os
from pathlib import Path
import re
import secrets as pysecrets
import stat
import threading
from typing import Any

from .approvals import clean_line, label

LOG = logging.getLogger(__name__)

# -- the bounds (design §10, question 2); pinned by tests/v2/owner_post_check.py --------
OPEN_PER_THREAD = 5
PER_DAY = 20
PER_THREAD_DAY = 10         # posted by one chat in any rolling 24 hours (review of PR #309)
DAY = timedelta(hours=24)
QUESTION_MAX = 500
OPTION_MAX = 120
WHY_MAX = 1000
OPTIONS_MIN, OPTIONS_MAX = 2, 6
ANSWER_MAX = 2000           # the owner's typed answer
KEEP = 500                  # files kept; the oldest answered go first
RECORDED_WINDOW = timedelta(days=7)   # how long "recorded; the asking chat is closed" shows
_FILE_MAX = 64 * 1024       # a decision file larger than this is not one of ours

KINDS = ("decision", "check")   # "check" is IN-6 (inbox B2, `checks.py`)
# The delivered answer's last line (`inbox.decision_message`): the agent's
# question, quoted, labelled as the agent's words (review of PR #309).
QUESTION_LEAD = "(Your question, quoted; your words, not the owner's: "
SUGGESTED = "(suggested)"   # the HUD's own marker: no option may say it
_HANGUL_FILLERS = frozenset("\u115f\u1160\u3164\uffa0")
_MARKS_MAX = 2              # combining marks kept after one base character
_FIELDS = frozenset({"id", "kind", "thread_id", "task_id", "project_id", "origin", "question", "options",
                     "default", "why", "asked_at", "answer", "delivery"})
DELIVERIES = ("started", "queued", "closed", "failed")

ID_RE = re.compile(r"^d[0-9a-f]{12}$")
_NAME_RE = re.compile(r"^(d[0-9a-f]{12})\.json$")
_THREAD_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

NOT_IN_CHAT = "Error: owner_post works only inside a Jarvis HUD chat."
NOT_AT_DESK = ("Error: a decision can be posted only during a turn the owner started at the "
               "HUD in this Claude or Codex chat (not Discord, the DM, a schedule, a task, a "
               "sub-agent or a turn another chat's message started).")
NOT_FROM_A_SUB_AGENT = ("Error: a sub-agent cannot post to the owner; only the conversation "
                        "itself can.")
STOPPING = "Error: Jarvis is stopping, so nothing more is posted."
WITHHELD_REFUSAL = ("Error: the decision could not be checked for credentials, so it was not "
                    "posted.")


class CapReached(Exception):
    """A post past a bound: the sentence the tool returns."""


class Conflict(Exception):
    """A second answer to a decision (409)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value) -> datetime | None:
    try:
        out = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return out if out.tzinfo is not None else out.replace(tzinfo=timezone.utc)


def _ident(value) -> str | None:
    return value if isinstance(value, str) and _THREAD_RE.match(value) else None


def _int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def valid(record: Any, name: str | None = None) -> dict | None:
    """`record` if it has exactly the shape `create` writes (§3.2 plus
    `kind`, `origin` and `delivery`), else None: anything else on disk is
    never a row, never counted and never answered (review of PR #309: a file
    without `answer` used to take `GET /inbox` down, and one of another kind
    showed as a decision). `name` is the file it came from, which must be its
    id's."""
    if not isinstance(record, dict) or set(record) != _FIELDS:
        return None
    rid = record["id"]
    if not isinstance(rid, str) or not ID_RE.match(rid) or (name is not None and name != f"{rid}.json"):
        return None
    if record["kind"] != "decision":
        return None
    question, options, default, why = record["question"], record["options"], record["default"], record["why"]
    if not isinstance(question, str) or not isinstance(why, str) or not isinstance(record["origin"], str):
        return None
    if (not isinstance(options, list) or not OPTIONS_MIN <= len(options) <= OPTIONS_MAX
            or not all(isinstance(o, str) and o for o in options)):
        return None
    if default is not None and not (_int(default) and 0 <= default < len(options)):
        return None
    if _ident(record["thread_id"]) is None or _parse_time(record["asked_at"]) is None:
        return None
    for key in ("task_id", "project_id"):
        if record[key] is not None and _ident(record[key]) is None:
            return None
    answer = record["answer"]
    if answer is not None:
        if not isinstance(answer, dict) or set(answer) != {"choice", "text", "at"}:
            return None
        if _parse_time(answer["at"]) is None:
            return None
        choice, text = answer["choice"], answer["text"]
        if choice is not None and not (_int(choice) and 0 <= choice < len(options)):
            return None
        if text is not None and not isinstance(text, str):
            return None
        if (choice is None) == (text is None):
            return None
    if record["delivery"] not in (None, *DELIVERIES):
        return None
    if answer is None and record["delivery"] is not None:
        return None
    return record


def display_text(text: str, *, lines: bool) -> str:
    """Agent text as the owner is shown it (review of PR #309): NFC, then
    `chat_reads.card_title`'s sibling rule per character: enclosing marks,
    symbols (So), format and private-use characters and the Hangul fillers
    go; at most `_MARKS_MAX` combining marks stay after a base character
    (no Zalgo towers). `lines` keeps line breaks (a question, a why), each
    line's runs of white space one space; else the whole is one line."""
    import unicodedata
    text = unicodedata.normalize("NFC", text)
    out, marks = [], 0
    for ch in text:
        if ch == "\n" and lines:
            out.append(ch)
            marks = 0
            continue
        category = unicodedata.category(ch)
        if ch in _HANGUL_FILLERS or category in ("Me", "So", "Cf", "Co", "Cs", "Cn"):
            continue
        if category == "Mn":
            marks += 1
            if marks > _MARKS_MAX:
                continue
        else:
            marks = 0
        out.append(ch)
    text = "".join(out)
    if not lines:
        return clean_line(text, 10_000)
    kept = [clean_line(line, 10_000) for line in text.split("\n")]
    while kept and not kept[0]:
        kept.pop(0)
    while kept and not kept[-1]:
        kept.pop()
    return "\n".join(kept)


# A small confusables skeleton (review of PR #309): the Cyrillic and Greek
# letters that look like Latin ones, so `Yes` and `Y\u0435s` (Cyrillic e) are
# the same option. Not Unicode's whole confusables table (no dependency):
# the common look-alikes only; others are left to the owner's eye.
_SKELETON = str.maketrans({
    # Cyrillic lower and upper
    "\u0430": "a", "\u0432": "b", "\u0435": "e", "\u0456": "i", "\u0458": "j", "\u043a": "k",
    "\u043c": "m", "\u043d": "h", "\u043e": "o", "\u0440": "p", "\u0441": "c", "\u0442": "t",
    "\u0443": "y", "\u0445": "x", "\u0455": "s", "\u0501": "d", "\u051b": "q", "\u051d": "w",
    "\u04bb": "h", "\u0457": "i", "\u0491": "r",
    # Greek lower
    "\u03b1": "a", "\u03b2": "b", "\u03b5": "e", "\u03b9": "i", "\u03ba": "k", "\u03bd": "v",
    "\u03bf": "o", "\u03c1": "p", "\u03c4": "t", "\u03c5": "u", "\u03c7": "x", "\u03b3": "y",
    # Latin look-alikes
    "\u0131": "i", "\u0261": "g", "\u1d0f": "o", "\u0251": "a",
})


def _same(text: str) -> str:
    """What two options are compared as: NFKC, case-folded, the common
    Cyrillic and Greek look-alikes mapped to Latin, spaces collapsed."""
    import unicodedata
    folded = unicodedata.normalize("NFKC", text).casefold().translate(_SKELETON)
    return " ".join(folded.split())


class DecisionStore:
    """`decisions/` in `<root>`'s `inbox` folder. Thread-safe; every method that writes holds
    `_lock`, so the caps are counted and the file written in one step.

    The folder's safe I/O (dir fds, O_NOFOLLOW, O_NONBLOCK reads, O_EXCL temp
    then fsync then replace, 0700/0600) is shared with the live checks'
    store (`checks.py`, inbox B2), which names its own folder, file names
    and record shape through the four class attributes below."""

    _SUB = "decisions"          # the folder under `inbox/`
    _NAMES = _NAME_RE           # the file names that are records
    _ID = ID_RE                 # a record's id
    _valid = staticmethod(valid)
    _MAX = _FILE_MAX            # a record file larger than this is not one of ours

    @staticmethod
    def _order(record):
        return (_parse_time(record["asked_at"]), record["id"])

    _DIR = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    _READ = (os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_CLOEXEC", 0))
    _WRITE = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
              | getattr(os, "O_CLOEXEC", 0))

    def __init__(self, root, *, clock=None):
        self.root = Path(root)
        self.dir = self.root / "inbox" / self._SUB
        self.clock = clock or _now
        self._lock = threading.RLock()

    # -- the folder ------------------------------------------------------------------

    def _subdir(self, parent_fd: int, name: str, create: bool) -> int | None:
        """A descriptor for `name` under `parent_fd`: never through a symlink
        (O_NOFOLLOW), a plain directory, 0700. None when it does not exist and
        `create` is False."""
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
        try:
            fd = os.open(name, self._DIR, dir_fd=parent_fd)
        except FileNotFoundError:
            if create:
                raise
            return None
        try:
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode):
                raise NotADirectoryError(f"the {name} folder is not a plain directory")
            if stat.S_IMODE(info.st_mode) != 0o700:
                os.fchmod(fd, 0o700)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _open(self, create: bool) -> int | None:
        """The decisions folder's descriptor (the caller closes it), or None
        when nothing was ever stored and `create` is False."""
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
            inbox_fd = self._subdir(root_fd, "inbox", create)
        finally:
            os.close(root_fd)
        if inbox_fd is None:
            return None
        try:
            return self._subdir(inbox_fd, self._SUB, create)
        finally:
            os.close(inbox_fd)

    # -- files -----------------------------------------------------------------------

    def _read(self, dir_fd: int, name: str) -> dict | None:
        try:
            fd = os.open(name, self._READ, dir_fd=dir_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                return None         # a symlink there (O_NOFOLLOW): never one of ours
            raise
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > self._MAX:
                return None
            if stat.S_IMODE(info.st_mode) != 0o600:
                os.fchmod(fd, 0o600)
            chunks, left = [], self._MAX + 1
            while left > 0 and (chunk := os.read(fd, min(1 << 16, left))):
                chunks.append(chunk)
                left -= len(chunk)
            data = b"".join(chunks)
        finally:
            os.close(fd)
        if len(data) > self._MAX:
            return None
        try:
            return self._valid(json.loads(data.decode("utf-8")), name)
        except (ValueError, UnicodeError):
            return None

    def _write(self, dir_fd: int, record: dict) -> None:
        """Atomic: a fresh 0600 temp file beside it, fsynced, then replaced."""
        name = f"{record['id']}.json"
        data = json.dumps(record, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
        tmp = f".{record['id']}.{pysecrets.token_hex(4)}.tmp"
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
            os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dir_fd)
            except OSError:
                pass
            raise

    def _all(self, dir_fd: int) -> list[dict]:
        out = []
        for name in os.listdir(dir_fd):
            if not self._NAMES.match(name):
                continue
            try:
                record = self._read(dir_fd, name)
            except OSError as exc:
                LOG.warning("Cannot read a record in %s (%s)", self._SUB, type(exc).__name__)
                continue
            if record is not None:
                out.append(record)
        out.sort(key=self._order)
        return out

    # -- reads -----------------------------------------------------------------------

    def all(self) -> list[dict]:
        """Every well-formed decision, oldest first. Never raises."""
        try:
            with self._lock:
                fd = self._open(create=False)
                if fd is None:
                    return []
                try:
                    return self._all(fd)
                finally:
                    os.close(fd)
        except Exception as exc:  # noqa: BLE001 — an unreadable store shows nothing, never a guess
            LOG.warning("Cannot read the decisions (%s)", type(exc).__name__)
            return []

    def get(self, decision_id: str) -> dict | None:
        if not isinstance(decision_id, str) or not self._ID.match(decision_id):
            return None
        try:
            with self._lock:
                fd = self._open(create=False)
                if fd is None:
                    return None
                try:
                    return self._read(fd, f"{decision_id}.json")
                finally:
                    os.close(fd)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Cannot read a decision (%s)", type(exc).__name__)
            return None

    # -- writes ----------------------------------------------------------------------

    def create(self, *, thread_id: str, project_id: str | None, task_id: str | None, origin: str,
               question: str, options: list[str], default: int | None, why: str) -> dict:
        """Write one new decision, or raise `CapReached` (nothing written).
        The caller has already validated and scrubbed every field."""
        with self._lock:
            fd = self._open(create=True)
            try:
                existing = self._all(fd)
                now = self.clock()
                open_here = sum(1 for r in existing if r["answer"] is None and r["thread_id"] == thread_id)
                if open_here >= OPEN_PER_THREAD:
                    raise CapReached(
                        f"Error: this chat already has {OPEN_PER_THREAD} decisions waiting on the "
                        "owner, the most one chat may; wait for an answer before posting another.")
                since = now - DAY
                mine = sum(1 for r in existing if r["thread_id"] == thread_id
                           and (_parse_time(r["asked_at"]) or now) > since)
                if mine >= PER_THREAD_DAY:
                    raise CapReached(
                        f"Error: this chat has posted {PER_THREAD_DAY} decisions in the last 24 hours, "
                        "the most one chat may; try again later.")
                today = sum(1 for r in existing if (_parse_time(r["asked_at"]) or now) > since)
                if today >= PER_DAY:
                    raise CapReached(
                        f"Error: {PER_DAY} decisions have been posted to the owner in the last 24 "
                        "hours, the most Jarvis allows; try again later.")
                files = sum(1 for name in os.listdir(fd) if _NAME_RE.match(name))
                if files >= KEEP:
                    answered = sorted((r for r in existing if r["answer"] is not None),
                                      key=lambda r: (_parse_time(r["answer"]["at"]), r["id"]))
                    for old in answered[:files - KEEP + 1]:
                        try:
                            os.unlink(f"{old['id']}.json", dir_fd=fd)
                        except FileNotFoundError:
                            pass
                    files = sum(1 for name in os.listdir(fd) if _NAME_RE.match(name))
                    if files >= KEEP:
                        raise CapReached(
                            f"Error: {KEEP} decisions wait on the owner, the most Jarvis keeps; "
                            "nothing more is posted until some are answered.")
                taken = {r["id"] for r in existing}
                rid = "d" + pysecrets.token_hex(6)
                while rid in taken:
                    rid = "d" + pysecrets.token_hex(6)
                record = {"id": rid, "kind": "decision", "thread_id": thread_id, "task_id": task_id,
                          "project_id": project_id, "origin": origin, "question": question,
                          "options": list(options), "default": default, "why": why,
                          "asked_at": now.isoformat(timespec="seconds"), "answer": None,
                          "delivery": None}
                if valid(record) is None:
                    raise ValueError("a decision that would not read back was refused")
                self._write(fd, record)
                return record
            finally:
                os.close(fd)

    def settle(self, decision_id: str, *, choice: int | None, text: str | None) -> dict:
        """Record the owner's answer (the inbox's owner-only route only).
        LookupError for an unknown id, `Conflict` for one already answered."""
        with self._lock:
            fd = self._open(create=False)
            if fd is None:
                raise LookupError("no such decision")
            try:
                record = self._read(fd, f"{decision_id}.json") if ID_RE.match(decision_id) else None
                if record is None:
                    raise LookupError("no such decision")
                if record["answer"] is not None:
                    raise Conflict("this decision was already answered")
                if choice is not None and not (_int(choice) and 0 <= choice < len(record["options"])):
                    raise ValueError("choice is not one of the options")
                record = dict(record)
                record["answer"] = {"choice": choice, "text": text,
                                    "at": self.clock().isoformat(timespec="seconds")}
                self._write(fd, record)
                return record
            finally:
                os.close(fd)

    def delivered(self, decision_id: str, delivery: str, *, keep_failed: bool = False) -> dict | None:
        """Note what became of an answer (the inbox's route and the daemon's
        drop only). `keep_failed`: a "failed" a drop already wrote stands.
        Returns the record as it now is. Never raises."""
        if delivery not in DELIVERIES:
            return None
        try:
            with self._lock:
                fd = self._open(create=False)
                if fd is None:
                    return None
                try:
                    record = self._read(fd, f"{decision_id}.json")
                    if record is None or record["answer"] is None:
                        return None
                    if keep_failed and record["delivery"] == "failed":
                        return record
                    record = dict(record, delivery=delivery)
                    self._write(fd, record)
                    return record
                finally:
                    os.close(fd)
        except Exception as exc:  # noqa: BLE001 — the answer is recorded already
            LOG.warning("Cannot note a decision's delivery (%s)", type(exc).__name__)
            return None


# -- the post (IN-5) ---------------------------------------------------------------------

def _refuse_fields(kind, question, options, default, why) -> str | None:
    """Why these arguments are refused, or None. Fails closed: an unknown
    kind, a wrong type, a missing or empty field, a count or length out of
    bounds. Never cuts."""
    if not isinstance(kind, str) or kind not in KINDS:
        return 'Error: kind must be "decision" or "check".'
    if not isinstance(question, str) or not question.strip():
        return "Error: question must be a nonempty string."
    if len(question) > QUESTION_MAX:
        return (f"Error: the question is {len(question):,} characters and the most is "
                f"{QUESTION_MAX}; shorten it (nothing is cut).")
    if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
        return "Error: options must be a list of strings."
    if not OPTIONS_MIN <= len(options) <= OPTIONS_MAX:
        return f"Error: give between {OPTIONS_MIN} and {OPTIONS_MAX} options (got {len(options)})."
    for index, option in enumerate(options):
        if not option.strip():
            return f"Error: option {index} is empty."
        if len(option) > OPTION_MAX:
            return (f"Error: option {index} is {len(option):,} characters and the most is "
                    f"{OPTION_MAX}; shorten it (nothing is cut).")
    if len({" ".join(o.split()).casefold() for o in options}) != len(options):
        return "Error: two options say the same thing; make each one distinct."
    if default is not None and not (_int(default) and 0 <= default < len(options)):
        return f"Error: default must be the index of an option (0 to {len(options) - 1}) or null."
    if not isinstance(why, str) or not why.strip():
        return "Error: why must be a nonempty string: say what the answer changes."
    if len(why) > WHY_MAX:
        return (f"Error: why is {len(why):,} characters and the most is {WHY_MAX}; shorten it "
                "(nothing is cut).")
    return None


def provenance(thread, task=None) -> str:
    """Who asked, as one clean line: `approvals.label`'s attribution for a
    task, "Provider · chat ‹title› (id)" for a chat (the title cleaned and
    scrubbed as an approval card's relay line is)."""
    from . import chat_reads
    task_id = getattr(thread, "task_id", None)
    if task_id:
        return clean_line(label(task_id, getattr(task, "brief", "") if task is not None else ""), 160)
    title = chat_reads.card_title(chat_reads.clean_title(getattr(thread, "title", "")))
    return clean_line(f"{chat_reads.provider_word(getattr(thread, 'provider', ''))} · chat ‹{title}› "
                      f"({thread.id})", 160)


def _clean_field(text: str, lines: bool) -> str:
    """Scrubbed, cleaned as the owner will see it (`display_text`), then
    scrubbed again, outside any lock (the scrub reads credential files): a
    token split by a character the cleaning drops (a symbol, a filler, a
    private-use or unassigned code point, an enclosing mark) is whole again
    after it, so the second scrub is the one that counts (review of PR #309).
    `chat_reads.WITHHELD` when a scrub failed: the post is refused."""
    from . import chat_reads
    once = chat_reads.scrub(text)
    if once == chat_reads.WITHHELD:
        return once
    return chat_reads.scrub(display_text(once, lines=lines))


class OwnerPosts:
    """The service `owner_post` reaches (`installed()`): bound checks under the
    daemon lock, then the store. It holds no answer path."""

    def __init__(self, daemon: Any, store: DecisionStore | None, checks: Any = None):
        self.daemon = daemon
        self.store = store
        self.checks = checks        # the live checks' store (inbox B2, `checks.py`), or None
        self.closed = False

    def _refuse_caller(self, caller) -> tuple[str | None, Any]:
        """Under the daemon lock: (why this caller may not post now, None) or
        (None, the session). `chat_relay._refuse_sender`'s checks, plus the
        provider."""
        from .model import ProviderName, Role
        daemon = self.daemon
        if self.closed or daemon._stopping:
            return STOPPING, None
        session = daemon._sessions.get(caller["thread_id"])
        if (session is None or session.worker is None or session.handle is None
                or session.turn_id != caller["turn_id"]):
            return NOT_AT_DESK, None
        if getattr(session, "relay_turn", None) is not None:
            return NOT_AT_DESK, None
        if session.cancelled.is_set() or session.closing or session.retired or session.lost:
            return "Error: this turn was stopped, so it posts nothing more.", None
        if session.brief.role != Role.CHAT or session.brief.task_id is not None:
            return NOT_AT_DESK, None
        if session.thread.provider not in (ProviderName.CLAUDE, ProviderName.CODEX):
            return NOT_AT_DESK, None
        if session.desk.slot.get("present") is not True:
            return NOT_AT_DESK, None
        return None, session

    def post(self, caller: dict | None, kind, question, options, default, why, *,
             title=None, steps=None, prs=None) -> str:
        """Post one decision, or one check (`kind="check"`, inbox B2: `title`,
        `steps`, `prs`, through `_post_check`). Returns "Posted decision d…"
        or "Posted check c…", or a sentence starting "Error: " (and then
        nothing was written). A kind's arguments are its own: a decision
        given a check's, or a check a decision's, is refused."""
        from . import chat_reads
        if (not isinstance(caller, dict) or not isinstance(caller.get("thread_id"), str)
                or not isinstance(caller.get("turn_id"), str)):
            return NOT_IN_CHAT
        if kind == "check":
            if any(v is not None for v in (question, options, default, why)):
                return ('Error: a check takes title, steps and prs; question, options, why and '
                        'default are for kind "decision".')
            return self._post_check(caller, title, steps, prs)
        if self.closed or self.store is None:
            return STOPPING if self.closed else NOT_IN_CHAT
        if any(v is not None for v in (title, steps, prs)):
            return ('Error: title, steps and prs are for kind "check"; a decision takes question, '
                    'options, why and default.')
        refusal = _refuse_fields(kind, question, options, default, why)
        if refusal is not None:
            return refusal
        clean = _clean_field
        clean_question = clean(question.strip(), True)
        clean_why = clean(why.strip(), True)
        clean_options = [clean(" ".join(o.split()), False) for o in options]
        if chat_reads.WITHHELD in (clean_question, clean_why, *clean_options):
            return WITHHELD_REFUSAL
        # Judged again on what is shown: nothing empty, nothing alike, and no
        # option that forges the HUD's own "(suggested)" marker.
        clean_question = display_text(clean_question, lines=True)
        clean_why = display_text(clean_why, lines=True)
        clean_options = [clean_line(display_text(o, lines=False), OPTION_MAX + 40) for o in clean_options]
        if not clean_question:
            return "Error: the question has nothing left to show once cleaned."
        if not clean_why:
            return "Error: why has nothing left to show once cleaned."
        for index, option in enumerate(clean_options):
            if not option:
                return f"Error: option {index} has nothing left to show once cleaned."
            if SUGGESTED in _same(option):
                return f'Error: option {index} says "{SUGGESTED}"; the owner\'s list marks your default itself.'
        if len({_same(o) for o in clean_options}) != len(clean_options):
            return "Error: two options say the same thing once cleaned; make each one distinct."
        with self.daemon._lock:
            why_not, session = self._refuse_caller(caller)
            if why_not is not None:
                return why_not
            thread = session.thread
            thread_id, project_id = thread.id, thread.project_id
        origin = provenance(thread)
        try:
            record = self.store.create(thread_id=thread_id, project_id=project_id, task_id=None,
                                       origin=origin, question=clean_question, options=clean_options,
                                       default=default, why=clean_why)
        except CapReached as exc:
            return str(exc)
        except Exception as exc:  # noqa: BLE001 — a store that cannot write posts nothing
            LOG.warning("Cannot store a decision (%s)", type(exc).__name__)
            return "Error: the decision could not be stored, so it was not posted."
        # Ids only on the bus: the inbox's observer turns this into
        # `inbox_changed`, which carries no text either.
        from .model import utcnow
        self.daemon.bus.publish({"kind": "decision_posted", "thread_id": thread_id,
                                 "project_id": project_id, "at": utcnow(),
                                 "data": {"decision_id": record["id"]}})
        return (f"Posted decision {record['id']} to the owner. They answer when they have time; "
                "the answer comes back to this chat as a message from the owner, as its own turn "
                "after any running one. Do not wait for it: go on with what does not depend on it.")

    def _post_check(self, caller: dict, title, steps, prs) -> str:
        """Post one check (inbox B2, IN-5/IN-6): the decision's binding, caps,
        scrub, cleaning and provenance, with the check's own fields and caps
        (`checks.refuse_fields`). A step is text only: nothing here runs it,
        and only the owner records a result (the inbox's owner-only route,
        which this service never names)."""
        from . import chat_reads
        from . import checks as _checks
        if self.closed or self.checks is None:
            return STOPPING if self.closed else NOT_IN_CHAT
        refusal = _checks.refuse_fields(title, steps, prs)
        if refusal is not None:
            return refusal
        clean_title = _clean_field(" ".join(title.split()), False)
        clean_steps = [{"do": _clean_field(s["do"].strip(), True), "expect": _clean_field(s["expect"].strip(), True)}
                       for s in steps]
        texts = [clean_title, *(v for s in clean_steps for v in s.values())]
        if chat_reads.WITHHELD in texts:
            return WITHHELD_REFUSAL.replace("decision", "check")
        clean_title = clean_line(display_text(clean_title, lines=False), _checks.TITLE_MAX + 40)
        if not clean_title:
            return "Error: the title has nothing left to show once cleaned."
        for index, step in enumerate(clean_steps):
            for key in ("do", "expect"):
                step[key] = display_text(step[key], lines=True)
                if not step[key]:
                    return f"Error: step {index}'s {key} has nothing left to show once cleaned."
        with self.daemon._lock:
            why_not, session = self._refuse_caller(caller)
            if why_not is not None:
                return why_not.replace("A decision can", "A check can").replace("a decision can", "a check can")
            thread = session.thread
            thread_id, project_id = thread.id, thread.project_id
        origin = provenance(thread)
        try:
            record = self.checks.create(thread_id=thread_id, project_id=project_id, task_id=None,
                                        created_by=origin, title=clean_title, prs=list(prs or ()),
                                        steps=clean_steps)
        except CapReached as exc:
            return str(exc)
        except Exception as exc:  # noqa: BLE001 — a store that cannot write posts nothing
            LOG.warning("Cannot store a check (%s)", type(exc).__name__)
            return "Error: the check could not be stored, so it was not posted."
        from .model import utcnow
        self.daemon.bus.publish({"kind": "check_posted", "thread_id": thread_id,
                                 "project_id": project_id, "at": utcnow(),
                                 "data": {"check_id": record["id"]}})
        return (f"Posted check {record['id']} to the owner. They run its steps when they have time "
                "and record each result themselves; nothing is sent back to this chat. Do not wait "
                "for it.")

    # -- install / stop (as chat_relay) -------------------------------------------------

    def install(self) -> None:
        global _installed
        with _installed_lock:
            self.closed = False
            _installed = self

    def stop(self) -> None:
        global _installed
        self.closed = True
        with _installed_lock:
            if _installed is self:
                _installed = None


_installed_lock = threading.Lock()
_installed: OwnerPosts | None = None


def installed() -> OwnerPosts | None:
    """The running daemon's service, or None (no daemon, or it stopped)."""
    with _installed_lock:
        service = _installed
    return service if service is not None and not service.closed else None
