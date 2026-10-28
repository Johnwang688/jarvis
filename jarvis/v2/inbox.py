"""The owner inbox: one ranked list of everything waiting on the owner
(design `docs/plans/2026-10-24-owner-inbox-design.md`, IN-0, IN-2 and IN-3).

**A projection first, a store second (IN-1).** Every live row is computed
from its source each time `rows()` runs: the approval broker's pending
requests, the daemon's live sessions and their open provider questions, and
the task store. None of those is written anywhere, so the inbox can never
disagree with the card, the chat or the task it shows. The one store is
below ("denied while you were away"). Resolving an item at its
source removes its row from the next read, and `inbox_changed` (published
from the same bus publish that carried the source event) tells the HUD to
read again.

**No new answering path (IN-2).** `POST /inbox/<id>/answer` finds the row
in a fresh projection and calls the function the item's own surface calls,
with the arguments that surface passes:

  * an approval: `Daemon.resolve_approval(req_id, decision, always, ...)`
    with `owner_verified` the Host test `POST /approvals/<id>` makes; the
    phone (P-4, `remote_answer`) calls `answer` with its own, True only
    after a verified per-answer assertion under §4.1;
  * a chat question: `Daemon.answer(thread_id, req_id, value)`, as
    `POST /threads/<id>/answer` does;
  * a task question: `TaskControl.answer_question(task_id, index, text)`,
    as `POST /tasks/<id>/answer` does;
  * a proposal: `TaskControl.cancel(task_id)`, the withdraw `POST
    /tasks/<id>/cancel` makes inside the grace window.

A row that is no longer in the projection (answered, timed out, its
session gone, its grace window over) is a 409, never a guess at what the
owner meant. A kind that takes no answer, an unknown kind or a malformed
body is a 4xx. Every route is owner-only (`projects.owner_only`: the HUD
listener and the HUD's own page); no tool reaches them
(`tests/v2/archive_check.py`, `tests/v2/inbox_check.py`).

**What a row may say.** `title` and `blocks` are built here, from ids,
states and tool names the source already validated, through
`approvals.clean_line`; never a thread title or anything else a model
wrote. Text an agent wrote (a question, a brief, a command) appears in
`detail`, and in `origin`: an approval's `approvals.label` attribution,
which carries a model-chosen task name (one line, `clean_line`d). The HUD
renders both as untrusted `textContent`. An approval's `detail` is exactly
`ApprovalRequest.to_json()`, so a
HUD-only (private) card carries only its private projection, and its row's
own fields carry nothing that projection does not.

**"Denied while you were away" (IN-3).** An approval whose waiter ended
with `timeout` or `shutdown` has no source left to project, so `MissedLog`
keeps one line for it in `<data root>/inbox/missed.jsonl`, written from the
daemon's broker callback (`Daemon._approval_resolved` calls
`Inbox.approval_ended` before it publishes `approval_resolved`):
`{req_id, tool, origin, thread_id, task_id, at, resolution}`, taken from
`ApprovalRequest.to_json()` only, never `owner_json`. A private (HUD-only)
card records its private projection's fields only: `origin` "" and
`task_id` null, as its live row. Each record is a `missed` row at rank 8
(information only): nothing to answer (400), no "ask again" (design §10,
question 3), a link to its chat. The file keeps the newest `MISSED_KEEP`
records; rows show the last `MISSED_WINDOW` (7 days) until dismiss (B3). A
write that fails is logged and dropped: it never touches the resolve. The
file is protected state (`jarvis/tools/files.py`), refused to agent writes.

**Decisions (B1, IN-5).** An agent's "answer whenever" question, posted by
`owner_post` into `decisions.DecisionStore` (`<data root>/inbox/decisions/`),
is a `decision` row at rank 6 ("When you have time"), oldest first. Its
`title` and `blocks` name only the asking chat's (or task's) id; the
question, options, default and why are in `detail` only, untrusted text, and
the asker's provenance is `origin`. The owner answers it at `POST
/inbox/decisions/<id>` (`{choice: index}` or `{text}`; also reachable as
`POST /inbox/dec:<id>/answer`, the same function): the answer is recorded
first (a second one is 409), then delivered to the asking chat as an owner
message with `via="inbox"` and no desk authority, in a queue of its own
behind any running turn and never steered into it (`Daemon.deliver_queued`).
The delivered text is code's frame (`decision_message`): the answer first,
with the decision's id, then the agent's question quoted on a line of its
own and labelled the agent's words, so nothing it wrote reads as the
owner's. An asking chat that is gone or archived gets nothing; the answer is
still recorded, and the row stays for `decisions.RECORDED_WINDOW` at rank 8
saying "recorded; the asking chat is closed". An answer that was queued and
then dropped (a Stop, a close, a thread that could not resume, a restart)
is marked "failed" and shows there too ("recorded; it could not reach the
asking chat"). The post and the answer each put an ids-only record on the
bus (`decision_posted`, `decision_answered`), which `Inbox.observe` turns
into `inbox_changed`. One source or record that cannot be read never takes
the projection down.

**Pull requests (B3, IN-4).** Every open, non-draft PR in each project's
GitHub repository is a `pr` row at rank 5, read from the last poll of
`inbox_prs.PrSource` (the daemon's own `gh`, read-only, every 5 minutes and
after `task_status_changed`); `rows()` never waits on `gh`. A list that
cannot be read is one `pr_unavailable` row ("PR list unavailable"), never
an empty list. Neither takes an answer (400). Details in `inbox_prs.py`.

This module is SELF_PROTECTED (`jarvis/tools/files.py`): it decides what the
owner sees as waiting.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import re
import stat
import threading

from .approvals import clean_line
from . import decisions as _decisions
from .model import TaskState

LOG = logging.getLogger(__name__)

# IN-4's bands. Code, not a setting. 5 is pull requests (B3); 7 (checks)
# belongs to a later package; 6 is decisions (B1); 8 is `failed`, "denied while away" (IN-3)
# and a decision recorded for a chat that was closed.
RANK_APPROVAL = 1
RANK_QUESTION = 2
RANK_PROPOSAL = 3
RANK_BLOCKED = 4
RANK_PR = 5            # B3: `inbox_prs.RANK_PR`, the same band
RANK_DECISION = 6
RANK_FAILED = 8
RANK_MISSED = 8
RANK_RECORDED = 8

# Row id prefixes: kind prefix + the source's own id.
_PREFIX = {"approval": "appr", "question": "q", "task_question": "tq",
           "proposal": "prop", "task": "task", "missed": "missed", "decision": "dec"}
_ANSWERABLE = {"appr", "q", "tq", "prop", "dec"}
# B3: a pull request (`pr:OWNER/REPO#N`) and "PR list unavailable" (`prx:list`).
_KNOWN = _ANSWERABLE | {"task", "missed", "pr", "prx"}

# "Denied while you were away" (IN-3). The resolutions recorded; the file's
# bound (it holds at most the newest MISSED_KEEP records: an append that
# would make one more rewrites it atomically with the newest); how long a
# record is shown until dismiss exists (B3). Code, not settings.
MISSED_RESOLUTIONS = frozenset({"timeout", "shutdown"})
MISSED_KEEP = 500
MISSED_WINDOW = timedelta(days=7)
# `approvals._new_id` is `token_urlsafe(9)`. A line whose id is not of that
# alphabet is not one of ours and is never shown.
_REQ_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ID_MAX = 200

# The bus kinds the inbox listens to (design §3.3), plus the ones that end
# a chat question without a `question_answered` (its turn ended, its chat
# closed), the question itself being asked, and a project's archive,
# restore or delete (which hide or reveal its task rows).
SOURCE_KINDS = frozenset({
    "approval_requested", "approval_resolved", "task_question", "question_answered",
    "task_status_changed", "proposal_reply",
    "project_archived", "project_restored", "project_deleted",
    "question", "turn_finished", "thread_closed", "thread_archived",
    # B1: a decision posted (`decisions.OwnerPosts.post`) or answered here.
    "decision_posted", "decision_answered",
})
_DETAIL_TEXT_CAP = 4000


def _parse_time(value):
    try:
        out = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return out if out.tzinfo is not None else out.replace(tzinfo=timezone.utc)


def _sort_key(row):
    at = _parse_time(row.get("asked_at"))
    # Ties go to the older item; an item with no time sorts after the timed
    # ones in its band, then by id so the order is stable.
    return (row["rank"], at is None, at or datetime.min.replace(tzinfo=timezone.utc), row["id"])


def _cap(value, limit=_DETAIL_TEXT_CAP):
    if not isinstance(value, str):
        return value
    return value if len(value) <= limit else value[:limit] + "…"


def _row(kind, rank, row_id, title, blocks, *, project_id=None, thread_id=None,
         task_id=None, asked_at=None, expires_at=None, origin="", detail=None):
    return {"id": row_id, "kind": kind, "rank": rank,
            "title": clean_line(title, 160), "blocks": clean_line(blocks, 160),
            "project_id": project_id, "thread_id": thread_id, "task_id": task_id,
            "asked_at": asked_at, "expires_at": expires_at,
            "origin": clean_line(origin, 120) if origin else "",
            "detail": detail if detail is not None else {}}


# -- the sources ---------------------------------------------------------------

def _approval_rows(daemon, hidden):
    out = []
    try:
        pending = daemon.approvals.pending()
    except Exception:  # noqa: BLE001 — a broker that cannot be read shows nothing, never a guess
        LOG.warning("Cannot read pending approvals for the inbox")
        return out
    for request in pending:
        card = request.to_json()
        req_id = card.get("req_id")
        if not req_id:
            continue
        tool = clean_line(card.get("tool") or "a tool", 60)
        thread_id = card.get("thread_id")
        asked_at = card.get("asked_at") or None
        expires_at = None
        start, timeout = _parse_time(asked_at), card.get("timeout_s")
        if start is not None and isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
            expires_at = (start + timedelta(seconds=float(timeout))).isoformat(timespec="seconds")
        if request.private:
            # The private projection only: no task, project, origin or
            # argument reaches the row's own fields either.
            out.append(_row("approval", RANK_APPROVAL, f"appr:{req_id}",
                            f"{tool} waits for your approval in the HUD",
                            f"chat {thread_id} waits on this" if thread_id else "a turn waits on this",
                            thread_id=thread_id, asked_at=asked_at, expires_at=expires_at,
                            detail=card))
            continue
        task_id = card.get("task_id")
        project_id = None
        if thread_id:
            try:
                thread = daemon.stores.threads.get(thread_id)
            except Exception:  # noqa: BLE001
                thread = None
            if thread is not None:
                project_id = thread.project_id
        if project_id in hidden:
            # An approval is never hidden: an archived project's live ask
            # still times out unseen otherwise. Shown without the project.
            project_id = None
        provider = clean_line(card.get("provider") or "", 20)
        who = provider.capitalize() if provider else "An agent"
        blocks = (f"task {task_id} waits on this" if task_id else
                  f"chat {thread_id} waits on this" if thread_id else "a turn waits on this")
        out.append(_row("approval", RANK_APPROVAL, f"appr:{req_id}",
                        f"{who} asks to use {tool}", blocks,
                        project_id=project_id, thread_id=thread_id, task_id=task_id,
                        asked_at=asked_at, expires_at=expires_at,
                        origin=card.get("origin") or "", detail=card))
    return out


def _question_rows(daemon, hidden):
    """Blocking provider questions on a running turn (`_Session.questions`).
    The set is the truth for what is open; `question_detail` only holds the
    text the provider sent with it."""
    out = []
    with daemon._lock:
        live = [(s.thread, sorted(s.questions), dict(getattr(s, "question_detail", {}) or {}))
                for s in daemon._sessions.values()
                if s.questions and s.worker is not None and not s.retired and not s.closing]
    for thread, req_ids, details in live:
        if thread.archived or thread.project_id in hidden:
            continue
        for req_id in req_ids:
            info = details.get(req_id) or {}
            options = info.get("options")
            who = f"task {thread.task_id}" if thread.task_id else f"chat {thread.id}"
            out.append(_row("question", RANK_QUESTION, f"q:{thread.id}:{req_id}",
                            f"{who} is stopped until you answer",
                            f"{who} waits on this",
                            project_id=thread.project_id, thread_id=thread.id,
                            task_id=thread.task_id, asked_at=info.get("asked_at"),
                            detail={"thread_id": thread.id, "req_id": req_id,
                                    "text": _cap(info.get("text") if isinstance(info.get("text"), str) else ""),
                                    "options": [_cap(o, 400) for o in options if isinstance(o, str)]
                                    if isinstance(options, list) else []}))
    return out


def _grace_until(daemon, task):
    """When this INTAKE task's proposal grace window closes, if it is still
    open — `Router.cancel_proposal`'s own test, read without acting."""
    router = getattr(daemon, "router", None)
    if router is None:
        return None
    try:
        rows = daemon.stores.tasks.read_journal(task.id)
        now = router.clock()
    except Exception:  # noqa: BLE001
        return None
    deadlines = [r.get("not_before") for r in rows if r.get("event") == "proposed_by_fastpath"]
    deadlines = [d for d in deadlines if isinstance(d, (int, float)) and not isinstance(d, bool)]
    if not deadlines or now >= max(deadlines):
        return None
    return max(deadlines)


def _task_rows(daemon, hidden):
    out = []
    try:
        tasks = daemon.stores.tasks.list()
    except Exception:  # noqa: BLE001
        LOG.warning("Cannot list tasks for the inbox")
        return out
    for task in tasks:
        if task.project_id in hidden:
            continue
        tid, state = task.id, task.state
        brief = _cap(task.brief, 400)
        if state == TaskState.CLARIFYING:
            for index, question in enumerate(task.spec.questions):
                if not question.blocking or question.answer is not None:
                    continue
                out.append(_row("task_question", RANK_QUESTION, f"tq:{tid}:{index}",
                                f"task {tid} is stopped until you answer",
                                f"task {tid} waits on this",
                                project_id=task.project_id, task_id=tid, asked_at=task.updated,
                                detail={"task_id": tid, "index": index,
                                        "text": _cap(question.text),
                                        "options": [_cap(o, 400) for o in question.options
                                                    if isinstance(o, str)]}))
        elif state == TaskState.INTAKE:
            until = _grace_until(daemon, task)
            if until is None:
                continue
            withdraw_until = datetime.fromtimestamp(until, timezone.utc).isoformat(timespec="seconds")
            out.append(_row("proposal", RANK_PROPOSAL, f"prop:{tid}",
                            f"task {tid} starts on its own unless you withdraw it",
                            "nothing waits yet; it starts when its window closes",
                            project_id=task.project_id, task_id=tid, asked_at=task.created,
                            detail={"task_id": tid, "brief": brief,
                                    "withdraw_until": withdraw_until}))
        elif state in (TaskState.BLOCKED, TaskState.FAILED):
            blocked = state == TaskState.BLOCKED
            out.append(_row("task", RANK_BLOCKED if blocked else RANK_FAILED, f"task:{tid}",
                            f"task {tid} is blocked" if blocked else f"task {tid} failed",
                            f"task {tid} waits for you to resume or cancel it" if blocked
                            else "for your information",
                            project_id=task.project_id, task_id=tid, asked_at=task.updated,
                            detail={"task_id": tid, "state": state.value, "brief": brief}))
    return out


# -- "denied while you were away" (IN-3) -------------------------------------------

_READ_CAP = 4 << 20         # far above 500 records; a larger file is read from its tail
_WRITTEN_CAP = 1024         # `Inbox._written`'s bound


def _ident(value):
    return value if isinstance(value, str) and value and len(value) <= _ID_MAX else None


def missed_entry(request, resolution: str, at: str) -> dict:
    """One record, from the card's public projection (`to_json`), never
    `owner_json`. A private card's projection carries no `origin` or
    `task_id`; both are also emptied here by name, so a private card can
    never record one whatever its projection later grows."""
    card = request.to_json()
    private = bool(card.get("hud_only")) or bool(getattr(request, "private", False))
    return {"req_id": _ident(card.get("req_id")),
            "tool": clean_line(card.get("tool") or "", 120),
            "origin": "" if private else clean_line(card.get("origin") or "", 160),
            "thread_id": _ident(card.get("thread_id")),
            "task_id": None if private else _ident(card.get("task_id")),
            "at": at, "resolution": resolution}


class MissedLog:
    """`<data root>/inbox/missed.jsonl`: one JSON line per approval whose
    waiter ended with `timeout` or `shutdown` (design §3.2).

    * **Atomic appends.** One `os.write` of one whole line to an `O_APPEND`
      descriptor, under `_lock` (a shutdown's waiters all record at once).
      A reader takes no lock: a last line without its newline is a write in
      progress (or torn by a crash) and is left out; the next record
      rewrites the file without it rather than gluing onto it.
    * **Bounded.** At most `MISSED_KEEP` records: an append that would make
      one more rewrites the file with the newest, by `stores._write_bytes`
      (a sibling temp file, mode 600, then `os.replace`).
    * **Private.** The file is 0600 and `inbox/` 0700, both tightened if
      found wider; a symlink or a non-regular file there is refused.
    * **Never in the way.** `record` never raises: a failure is one warning
      line naming the error's class only, and the approval it describes was
      already resolved by the broker.
    """

    def __init__(self, root, *, clock=None):
        self.dir = Path(root) / "inbox"
        self.path = self.dir / "missed.jsonl"
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()

    def record(self, request, resolution: str) -> bool:
        """Append one record; True when it was written. Never raises."""
        try:
            if resolution not in MISSED_RESOLUTIONS:
                return False
            entry = missed_entry(request, resolution, self.clock().isoformat(timespec="seconds"))
            if not entry["req_id"] or not _REQ_ID.match(entry["req_id"]):
                return False        # never admitted (a closed broker): no waiter ended
            line = json.dumps(entry, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n"
            with self._lock:
                self._prepare_dir()
                data = self._read()
                lines = self._complete(data)
                if len(lines) >= MISSED_KEEP or (data and not data.endswith(b"\n")):
                    from .stores import _write_bytes
                    keep = lines[len(lines) - MISSED_KEEP + 1:] if len(lines) >= MISSED_KEEP else lines
                    _write_bytes(self.path, b"".join(keep) + line)
                else:
                    self._append(line)
            return True
        except Exception as exc:  # noqa: BLE001 — a record is never worth a failed resolve
            LOG.warning("Cannot record a missed approval (%s); the approval itself is unaffected",
                        type(exc).__name__)
            return False

    def recent(self) -> list[dict]:
        """The records of the last `MISSED_WINDOW` by `clock`, one per
        `req_id` (the last line wins), malformed lines skipped. Never raises."""
        try:
            lines = self._complete(self._read())
            now = self.clock()
            since = now - MISSED_WINDOW
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Cannot read missed approvals (%s)", type(exc).__name__)
            return []
        out: dict[str, dict] = {}
        for raw in lines:
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            req_id, at = rec.get("req_id"), _parse_time(rec.get("at"))
            if not isinstance(req_id, str) or not _REQ_ID.match(req_id) or at is None or at < since:
                continue
            # A time after now (the clock stepped back, a hand-edited line) is
            # taken as now: shown, never claiming a future time or sorting
            # ahead of what really came later.
            at = min(at, now)
            resolution = rec.get("resolution")
            if resolution not in MISSED_RESOLUTIONS:
                continue
            out.pop(req_id, None)
            out[req_id] = {"req_id": req_id,
                           "tool": clean_line(rec.get("tool") if isinstance(rec.get("tool"), str) else "", 120),
                           "origin": clean_line(rec.get("origin") if isinstance(rec.get("origin"), str) else "", 160),
                           "thread_id": _ident(rec.get("thread_id")), "task_id": _ident(rec.get("task_id")),
                           "at": at.isoformat(timespec="seconds"), "resolution": resolution}
        return list(out.values())

    # -- the file -------------------------------------------------------------

    def _prepare_dir(self) -> None:
        self.dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.mkdir(self.dir, 0o700)
        except FileExistsError:
            pass
        info = os.lstat(self.dir)
        if not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError("the inbox folder is not a plain directory")
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.chmod(self.dir, 0o700)

    # O_NONBLOCK on both opens (review of #307, MED): a FIFO at the path
    # would block a plain open before the S_ISREG check could refuse it,
    # stalling every later waiter (under `_lock`) and every `GET /inbox`.
    # With it a FIFO read opens at once and is refused by `fstat`, and a
    # FIFO write with no reader is ENXIO. A regular file ignores the flag.
    _NONBLOCK = getattr(os, "O_NONBLOCK", 0)

    def _read(self) -> bytes:
        flags = (os.O_RDONLY | self._NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_CLOEXEC", 0))
        try:
            fd = os.open(self.path, flags)
        except FileNotFoundError:
            return b""
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("the missed-approvals file is not a regular file")
            tail = info.st_size > _READ_CAP
            if tail:
                os.lseek(fd, info.st_size - _READ_CAP, os.SEEK_SET)
            chunks, left = [], _READ_CAP
            while left > 0 and (chunk := os.read(fd, min(1 << 16, left))):
                chunks.append(chunk)
                left -= len(chunk)
            data = b"".join(chunks)
            # Read from the middle of a line: that first piece is not a record.
            return data.split(b"\n", 1)[1] if tail and b"\n" in data else data
        finally:
            os.close(fd)

    @staticmethod
    def _complete(data: bytes) -> list[bytes]:
        """The whole lines, each with its newline; a last piece without one is not a record yet."""
        return [part + b"\n" for part in data.split(b"\n")[:-1] if part.strip()]

    def _append(self, line: bytes) -> None:
        flags = (os.O_WRONLY | os.O_APPEND | os.O_CREAT | self._NONBLOCK
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        fd = os.open(self.path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("the missed-approvals file is not a regular file")
            if stat.S_IMODE(info.st_mode) != 0o600:
                os.fchmod(fd, 0o600)
            view = memoryview(line)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)


def _missed_rows(daemon):
    """One information-only row per recent record. Its own fields name ids
    and the tool only. It carries no project (a private card's row has
    none, and the record does not say which it was), so a project's
    archive does not hide it: like the approval it was, it stays until the
    window passes it."""
    log = getattr(getattr(daemon, "inbox", None), "missed", None)
    if log is None:
        return []
    out = []
    for rec in log.recent():
        thread_id, task_id = rec["thread_id"], rec["task_id"]
        tool = clean_line(rec["tool"] or "a tool", 60)
        who = f"task {task_id}" if task_id else f"chat {thread_id}" if thread_id else "a turn"
        why = ("nobody answered in time" if rec["resolution"] == "timeout"
               else "Jarvis stopped before you answered")
        out.append(_row("missed", RANK_MISSED, f"missed:{rec['req_id']}",
                        f"Denied while you were away: {tool}", f"{who} was denied it: {why}",
                        thread_id=thread_id, task_id=task_id, asked_at=rec["at"],
                        origin=rec["origin"], detail=rec))
    return out


# -- decisions (B1, IN-5) ------------------------------------------------------------

def _asker_closed(daemon, thread_id, hidden) -> bool:
    """The asking chat is gone, or it or its project is archived: an answer
    is recorded only. Unreadable counts as closed (nothing is delivered on a
    guess)."""
    try:
        thread = daemon.stores.threads.get(thread_id)
    except Exception:  # noqa: BLE001
        return True
    return thread is None or bool(thread.archived) or thread.project_id in hidden


def _decision_detail(rec) -> dict:
    """What the HUD shows of a decision: the agent's words, as untrusted text."""
    answer = rec["answer"]
    return {"decision_id": rec["id"], "question": _cap(rec["question"], 1000),
            "options": [_cap(o, 200) for o in rec["options"]], "default": rec["default"],
            "why": _cap(rec["why"], 2000),
            "answer": dict(answer) if isinstance(answer, dict) else None,
            "delivery": rec.get("delivery")}


def _decision_rows(daemon, hidden):
    """Open decisions at rank 6; an answer recorded for a closed chat, or one
    that could not be delivered, at rank 8 for `RECORDED_WINDOW`. `title` and
    `blocks` name the asker's id only (the A1 sentinel rule)."""
    store = getattr(getattr(daemon, "inbox", None), "decisions", None)
    if store is None:
        return []
    out = []
    now = store.clock()
    for rec in store.all():
        try:
            out += _decision_row(daemon, hidden, rec, now)
        except Exception as exc:  # noqa: BLE001 — one record never hides the rest
            LOG.warning("Cannot show a decision (%s)", type(exc).__name__)
    return out


def _decision_row(daemon, hidden, rec, now) -> list:
    """One decision's row, if it has one (`_decision_rows`)."""
    out: list = []
    thread_id, task_id = rec["thread_id"], rec.get("task_id")
    who = f"task {task_id}" if task_id else f"chat {thread_id}"
    project_id = rec.get("project_id")
    if project_id in hidden:
        project_id = None       # shown without its project, as an approval is
    answer = rec["answer"]
    if answer is None:
        closed = _asker_closed(daemon, thread_id, hidden)
        out.append(_row("decision", RANK_DECISION, f"dec:{rec['id']}",
                        f"{who} asks for your decision",
                        f"{who} is closed; an answer is recorded only" if closed
                        else f"nothing waits; {who} hears your answer as a message",
                        project_id=project_id, thread_id=thread_id, task_id=task_id,
                        asked_at=rec["asked_at"], origin=rec.get("origin") or "",
                        detail=_decision_detail(rec)))
        return out
    delivery = rec.get("delivery")
    at = _parse_time(answer.get("at"))
    if delivery not in ("closed", "failed") or at is None or at < now - _decisions.RECORDED_WINDOW:
        return out
    out.append(_row("decision", RANK_RECORDED, f"dec:{rec['id']}",
                    f"Your decision for {who}",
                    "recorded; the asking chat is closed" if delivery == "closed"
                    else "recorded; it could not reach the asking chat",
                    project_id=project_id, thread_id=thread_id, task_id=task_id,
                    asked_at=min(at, now).isoformat(timespec="seconds"),
                    origin=rec.get("origin") or "", detail=_decision_detail(rec)))
    return out


def _pr_rows(daemon, hidden):
    """B3 (IN-4): the last PR poll's rows. A read of a snapshot under the
    source's own lock, which no `gh` call holds: never waits on `gh`."""
    source = getattr(getattr(daemon, "inbox", None), "prs", None)
    return source.rows(hidden) if source is not None else []


def rows(daemon) -> list[dict]:
    """Every row, ranked (IN-4; ties to the older item). Pure: reads only."""
    from .projects import archived_project_ids
    try:
        hidden = archived_project_ids(daemon.stores)
    except Exception:  # noqa: BLE001
        hidden = set()
    out = []
    # One source that fails (a decision file that is not one, review of PR
    # #309) shows nothing of its own and never takes the others down.
    for name, source in (("approvals", lambda: _approval_rows(daemon, hidden)),
                         ("questions", lambda: _question_rows(daemon, hidden)),
                         ("tasks", lambda: _task_rows(daemon, hidden)),
                         ("missed", lambda: _missed_rows(daemon)),
                         ("decisions", lambda: _decision_rows(daemon, hidden)),
                         ("pull requests", lambda: _pr_rows(daemon, hidden))):
        try:
            out += source()
        except Exception as exc:  # noqa: BLE001 — never a 500 for the whole inbox
            LOG.warning("Cannot read the inbox's %s (%s)", name, type(exc).__name__)
    return sorted(out, key=_sort_key)


# -- inbox_changed ---------------------------------------------------------------

class Inbox:
    """Publishes `inbox_changed` ({ids, reason}, never text) after each
    source event, from the bus's synchronous observer, so it follows the
    record that caused it within the same publish. `_asked` remembers which
    chat questions were announced per thread, only so that a turn that ends
    without answering one can name the rows it removed. It is not a row
    store: `rows()` never reads it.

    `missed` is the one store (IN-3), under the daemon's data root.
    `approval_ended` writes to it from the broker's resolve callback, just
    before that callback publishes `approval_resolved`; that publish's
    `inbox_changed` then names the new `missed:` row beside the `appr:` row
    it replaces (`_written` says which were written), so the HUD's one
    re-read sees the swap."""

    def __init__(self, daemon):
        self.daemon = daemon
        self._lock = threading.Lock()
        self._asked: dict[str, set[str]] = {}
        self._written: set[str] = set()
        root = getattr(getattr(daemon, "stores", None), "root", None)
        self.missed = MissedLog(root) if root is not None else None
        # B1: the decisions `owner_post` writes (through the daemon's
        # `decisions.OwnerPosts`, which shares this store) and the owner answers here.
        self.decisions = _decisions.DecisionStore(root) if root is not None else None
        # B3 (IN-4): open PRs, polled off the daemon's lock by `gh`, only once
        # daemon2's `main()` attaches it (`inbox_prs.PrSource.attach_system`).
        from .inbox_prs import PrSource
        self.prs = PrSource(daemon)

    def answer_dropped(self, decision_id) -> None:
        """An answer queued for its chat will never run (the owner's Stop, a
        close, a thread that could not resume, shutdown): its decision says
        "failed", so the rank-8 "could not reach the asking chat" row shows,
        and `decision_answered` re-reads the inbox. Never raises."""
        try:
            if self.decisions is None or not _decisions.ID_RE.match(str(decision_id)):
                return
            record = self.decisions.delivered(decision_id, "failed")
            if record is not None:
                from .model import utcnow
                self.daemon.bus.publish({"kind": "decision_answered", "thread_id": record["thread_id"],
                                         "project_id": record.get("project_id"), "at": utcnow(),
                                         "data": {"decision_id": decision_id, "delivery": "failed"}})
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Cannot mark a dropped answer (%s)", type(exc).__name__)

    def fail_lost_answers(self) -> int:
        """At start: an answer a previous run left "queued" never ran (the
        queue lives in memory), and one recorded with no delivery noted at
        all was cut off between the two (a crash after `settle`); both are
        "failed". Returns how many."""
        if self.decisions is None:
            return 0
        lost = 0
        for record in self.decisions.all():
            if record["answer"] is not None and record.get("delivery") in ("queued", None):
                if self.decisions.delivered(record["id"], "failed") is not None:
                    lost += 1
        return lost

    def approval_ended(self, request, resolution) -> None:
        """A waiter ended (the broker's `on_resolve`): record it when it was a
        `timeout` or a `shutdown`. Never raises."""
        try:
            if self.missed is None or resolution not in MISSED_RESOLUTIONS:
                return
            if self.missed.record(request, resolution):
                with self._lock:
                    # Each id leaves on its own publish; the cap only bounds
                    # one whose publish never came (it then names nothing).
                    if len(self._written) >= _WRITTEN_CAP:
                        self._written.clear()
                    self._written.add(request.req_id)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Cannot record a missed approval (%s)", type(exc).__name__)

    def observe(self, record) -> None:
        if not isinstance(record, dict):
            return
        kind = record.get("kind")
        if kind not in SOURCE_KINDS:
            return
        if kind == "task_status_changed" and getattr(self, "prs", None) is not None:
            self.prs.poke()          # B3: poll PRs soon (debounced); only sets an event
        ids = self._ids(kind, record)
        if ids is None:
            return
        self.daemon.bus.publish({"kind": "inbox_changed",
                                 "data": {"ids": sorted(set(ids)), "reason": kind}})

    def _ids(self, kind, record):
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        thread_id = record.get("thread_id")
        if kind in ("approval_requested", "approval_resolved"):
            req_id = data.get("req_id")
            if not isinstance(req_id, str) or not req_id:
                return []
            ids = [f"appr:{req_id}"]
            if kind == "approval_resolved":
                with self._lock:
                    written = req_id in self._written
                    self._written.discard(req_id)
                if written:
                    ids.append(f"missed:{req_id}")
            return ids
        if kind in ("question", "question_answered"):
            req_id = data.get("req_id")
            if not isinstance(thread_id, str) or req_id is None:
                return None if kind == "question" else []
            req_id = str(req_id)
            with self._lock:
                if kind == "question":
                    self._asked.setdefault(thread_id, set()).add(req_id)
                else:
                    asked = self._asked.get(thread_id)
                    if asked is not None:
                        asked.discard(req_id)
                        if not asked:
                            self._asked.pop(thread_id, None)
            return [f"q:{thread_id}:{req_id}"]
        if kind in ("turn_finished", "thread_closed", "thread_archived"):
            if not isinstance(thread_id, str):
                return None
            with self._lock:
                left = self._asked.pop(thread_id, None)
            # Nothing was open on this thread: no row changed, nothing to say.
            return [f"q:{thread_id}:{r}" for r in left] if left else None
        if kind in ("project_archived", "project_restored", "project_deleted"):
            # Its task rows hide or return; which ones is a re-read's job.
            return []
        if kind in ("decision_posted", "decision_answered"):
            decision_id = data.get("decision_id")
            if not isinstance(decision_id, str) or not _decisions.ID_RE.match(decision_id):
                return []
            return [f"dec:{decision_id}"]
        task_id = record.get("task_id") or data.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return []
        ids = [f"task:{task_id}", f"prop:{task_id}"]
        task = data.get("task") if kind == "task_question" else data
        spec = task.get("spec") if isinstance(task, dict) else None
        questions = spec.get("questions") if isinstance(spec, dict) else None
        if isinstance(questions, list):
            ids += [f"tq:{task_id}:{i}" for i, q in enumerate(questions)
                    if isinstance(q, dict) and q.get("blocking")]
        if kind == "proposal_reply":
            ids = [f"prop:{task_id}"]
        return ids


# -- the routes (owner-only) -------------------------------------------------------

def _split(row_id):
    """(prefix, rest) of a row id, or a 400 for one that cannot be ours."""
    from urllib.parse import unquote
    from .daemon import APIError
    if not isinstance(row_id, str) or len(row_id) > 1200:
        raise APIError(400, "malformed inbox id")
    # The HUD sends path ids through `encodeURIComponent`, so `appr:X`
    # arrives as `appr%3AX`. Decoded once; the row is then matched exactly
    # against the projection, so no spelling reaches a row it does not name.
    row_id = unquote(row_id, errors="strict")
    if ":" not in row_id or len(row_id) > 400:
        raise APIError(400, "malformed inbox id")
    prefix, rest = row_id.split(":", 1)
    if prefix not in _KNOWN:
        raise APIError(400, "unknown inbox kind")
    if not rest:
        raise APIError(400, "malformed inbox id")
    return prefix, rest, row_id


def _gone():
    from .daemon import APIError
    return APIError(409, "this item is no longer waiting; it was answered or its source has gone")


def answer(daemon, row_id, body, *, owner_verified: bool, via: str = "inbox"):
    """Dispatch one answer to its source's own function (IN-2). The caller
    has already enforced its owner test: `owner_only` here (`route`), or the
    phone's session (`remote_answer`). `owner_verified` is HUD authority
    (PH-7), passed through unchanged: the Host test the approvals route
    makes here, the phone's verified assertion under §4.1 there. `via` is
    where the owner answered ("inbox", or "phone"): a decision's delivered
    message carries it, and a chat question's log record names "phone"."""
    from .daemon import APIError, _object, _text
    from .provider import Decision
    prefix, rest, row_id = _split(row_id)
    if prefix == "dec":
        # `POST /inbox/decisions/<id>`'s own function. A decision that is
        # not there, or no longer open, is "no longer waiting" here as for
        # every other row.
        try:
            return answer_decision(daemon, rest, body, via=via)
        except APIError as exc:
            if exc.status == 404:
                raise _gone() from None
            raise
    if prefix not in _ANSWERABLE:
        if prefix == "missed":
            # Information only (IN-3); no "ask again" (design §10, question 3).
            raise APIError(400, "this item takes no answer; open its chat to see what was denied")
        if prefix in ("pr", "prx"):
            # B3 (IN-4): merging stays the owner's own gh, or a card under the merge gate.
            raise APIError(400, "this item takes no answer; open the pull request to review or merge it")
        raise APIError(400, "this item takes no answer; open its task to resume or cancel it")
    # Shape first, so a malformed body is a 400 whatever the row's state.
    if prefix == "appr":
        body = _object(body, ("decision", "always"), ("decision",))
        always = body.get("always", False)
        if not isinstance(always, bool):
            raise APIError(400, "always must be a boolean")
        decision = Decision(_text(body["decision"], "decision"))
    elif prefix == "q":
        # A provider question takes text (Codex refuses anything else); the
        # thread route's `decision` is for withdrawing a provider approval,
        # which is never a question row.
        body = _object(body, ("text",), ("text",))
        value = _text(body["text"], "text")
    elif prefix == "tq":
        body = _object(body, ("text",), ("text",))
        text = _text(body["text"], "text")
    else:
        body = _object(body, ("withdraw",), ("withdraw",))
        if body["withdraw"] is not True:
            raise APIError(400, "withdraw must be true")
    row = next((r for r in rows(daemon) if r["id"] == row_id), None)
    if row is None:
        raise _gone()
    if prefix == "appr":
        req_id = row["detail"]["req_id"]
        try:
            # `POST /approvals/<id>`'s own call, `owner_verified` and `always` included.
            return daemon.resolve_approval(req_id, decision, always, owner_verified=owner_verified)
        except APIError:
            if not any(r.req_id == req_id for r in daemon.approvals.pending()):
                raise _gone() from None
            raise
    if prefix == "q":
        thread_id, req_id = row["detail"]["thread_id"], row["detail"]["req_id"]
        try:
            # `POST /threads/<id>/answer`'s own call (the phone's is named in its log record).
            if via == "phone":
                daemon.answer(thread_id, req_id, value, via="phone")
            else:
                daemon.answer(thread_id, req_id, value)
        except ValueError:
            # The provider no longer holds it (answered meanwhile, its turn
            # ended): the value was checked above, so this is a lost race.
            raise _gone() from None
        return {"ok": True}
    from .control import ControlError
    from .daemon import DaemonError
    runner = daemon.runner
    if runner is None:
        raise DaemonError("no task runner is attached to this daemon")
    from .model import to_json
    try:
        if prefix == "tq":
            # `POST /tasks/<id>/answer`'s own call.
            return to_json(runner.answer_question(row["detail"]["task_id"], row["detail"]["index"], text))
        # A proposal's withdraw is `POST /tasks/<id>/cancel`'s own call.
        return to_json(runner.cancel(row["detail"]["task_id"]))
    except ControlError as exc:
        raise APIError(409, str(exc)) from exc


NOTES = {"started": "Sent to the asking chat; it starts a turn with your answer now.",
         "queued": "Sent to the asking chat; it runs your answer after the turn already running.",
         "closed": "Recorded; the asking chat is closed, so it was not told.",
         "failed": "Recorded; it could not reach the asking chat."}


# Characters that look like a double quote (review of PR #309): an agent's
# text could close the JSON quote to the eye while JSON escapes only `"`.
# Each becomes `'`, as `chat_reads.card_title` neutralizes look-alike brackets.
_QUOTE_LIKE = frozenset("\u00ab\u00bb\u02ba\u02bb\u02bc\u02bd\u02ee\u02dd\u05f3\u05f4\u2018\u2019\u201a"
                        "\u201b\u201c\u201d\u201e\u201f\u2032\u2033\u2034\u2035\u2036\u2037\u2039"
                        "\u203a\u275b\u275c\u275d\u275e\u301d\u301e\u301f\u3003\uff02\uff07")


def _unquote_like(text: str) -> str:
    import unicodedata
    return "".join("'" if ch in _QUOTE_LIKE or unicodedata.category(ch) in ("Pi", "Pf") else ch
                   for ch in text)


def _quoted(text: str) -> str:
    """Agent-written text as one JSON-quoted line: whitespace, line breaks
    and invisible characters collapsed (`clean_line`), look-alike quotes made
    `'`, fence phrases annotated, every quote and backslash escaped, so
    nothing in it can close the quote, to the eye or to a parser, or start a
    line of its own."""
    from jarvis import untrusted
    return json.dumps(untrusted.neutralize_markers(_unquote_like(clean_line(text, 1000))),
                      ensure_ascii=False)


def decision_message(record) -> str:
    """The owner message a decision's answer becomes (design §5; reframed on
    the review of PR #309). Code's words first, with the decision's id and
    the owner's answer: an option the owner picked is the agent's own text,
    so it is quoted as such; the owner's typed words are theirs and stand as
    typed. Then, on a last line of its own, the agent's question, quoted and
    labelled as the agent's: it can never read as the owner's words."""
    answer = record["answer"]
    did = record["id"]
    if answer.get("choice") is not None:
        choice = answer["choice"]
        head = (f"The owner answered your decision {did}: they picked option {choice + 1} of the options "
                f"you gave, {_quoted(record['options'][choice])}.")
    else:
        head = f"The owner answered your decision {did} in their own words: {answer['text']}"
    return f"{head}\n{_decisions.QUESTION_LEAD}{_quoted(record['question'])})"


def _deliver_decision(daemon, record, via: str = "inbox") -> str:
    """Hand the answer to the asking chat (design §5): an owner message,
    `via="inbox"` (or "phone", answered on the phone, P-4), `desk=False`, through `Daemon.deliver_queued` (a turn when
    the chat is idle, else queued behind the running one; never a steer).
    "closed" when the chat is gone or archived, "failed" when it cannot take
    it; the answer is recorded either way."""
    from .daemon import APIError, DaemonError
    from .projects import is_archived
    from .provider import UserMessage
    thread_id = record["thread_id"]

    def closed() -> bool:
        try:
            thread = daemon.stores.threads.get(thread_id)
            return thread is None or bool(thread.archived) or is_archived(daemon.stores, thread.project_id)
        except Exception:  # noqa: BLE001
            return True
    if closed():
        return "closed"
    text = decision_message(record)
    message = UserMessage(text, origin="owner", via="phone" if via == "phone" else "inbox",
                          desk=False, typed=text)
    try:
        out = daemon.deliver_queued(thread_id, message, decision_id=record["id"])
    except (DaemonError, APIError) as exc:
        LOG.info("A decision's answer was not delivered (%s)", type(exc).__name__)
        return "closed" if closed() else "failed"
    except Exception:  # noqa: BLE001 — the answer is recorded; say it did not arrive
        LOG.exception("Delivering a decision's answer failed")
        return "closed" if closed() else "failed"
    status = out.get("status") if isinstance(out, dict) else None
    return status if status in ("started", "queued") else "failed"


def answer_decision(daemon, decision_id, body, *, via: str = "inbox") -> dict:
    """`POST /inbox/decisions/<id>` (owner-only, checked by `route`): record
    the owner's answer, then deliver it (design §4, §5). `{choice: index}` or
    `{text}`, exactly one. 400 for a malformed id or body, 404 for an unknown
    decision, 409 for one already answered."""
    from .daemon import APIError, _object
    store = getattr(getattr(daemon, "inbox", None), "decisions", None)
    if not isinstance(decision_id, str) or not _decisions.ID_RE.match(decision_id):
        raise APIError(400, "malformed decision id")
    body = _object(body, ("choice", "text"))
    if ("choice" in body) == ("text" in body):
        raise APIError(400, "give exactly one of choice or text")
    choice = text = None
    if "choice" in body:
        choice = body["choice"]
        if not isinstance(choice, int) or isinstance(choice, bool) or choice < 0:
            raise APIError(400, "choice must be an option's index")
    else:
        text = body["text"]
        if not isinstance(text, str) or not text.strip():
            raise APIError(400, "text must be a nonempty string")
        if len(text) > _decisions.ANSWER_MAX:
            raise APIError(400, f"text is longer than {_decisions.ANSWER_MAX} characters")
        text = text.strip()
    record = store.get(decision_id) if store is not None else None
    if record is None:
        raise APIError(404, "no such decision")
    if record["answer"] is not None:
        raise APIError(409, "this decision was already answered")
    if choice is not None and choice >= len(record["options"]):
        raise APIError(400, "choice is not one of the options")
    try:
        record = store.settle(decision_id, choice=choice, text=text)
    except _decisions.Conflict:
        raise APIError(409, "this decision was already answered") from None
    except LookupError:
        raise APIError(404, "no such decision") from None
    except ValueError as exc:
        raise APIError(400, str(exc)) from None
    delivery = _deliver_decision(daemon, record, via)
    # A drop between the queueing and here (the owner's Stop) already said
    # "failed": that stands.
    noted = store.delivered(decision_id, delivery, keep_failed=True)
    if noted is not None:
        delivery = noted.get("delivery") or delivery
    from .model import utcnow
    daemon.bus.publish({"kind": "decision_answered", "thread_id": record["thread_id"],
                        "project_id": record.get("project_id"), "at": utcnow(),
                        "data": {"decision_id": decision_id, "delivery": delivery}})
    return {"ok": True, "decision_id": decision_id, "delivery": delivery, "note": NOTES[delivery]}


def route(handler, daemon, parts, query):
    """`GET /inbox`, `POST /inbox/<id>/answer` and `POST
    /inbox/decisions/<id>`; None for any other path. `owner_only` runs before
    anything else on every `/inbox` path, so the API listener and a non-owner
    page learn nothing, not even which ids exist."""
    if not parts or parts[0] != "inbox":
        return None
    from jarvis import config
    from .daemon import APIError, _object
    from .projects import owner_only
    owner_only(handler, daemon)
    method = handler.command
    if parts == ["inbox"] and method == "GET":
        _object(query, ())
        return 200, rows(daemon)
    if len(parts) == 3 and parts[2] == "answer" and method == "POST":
        _object(query, ())
        # The Host test `POST /approvals/<id>` makes, made the same way: the
        # HUD route's HUD authority (PH-7).
        owner_verified = config.own_host_header(handler.headers.get("Host", ""), daemon.face_port)
        return 200, answer(daemon, parts[1], handler._body(), owner_verified=owner_verified)
    if len(parts) == 3 and parts[1] == "decisions" and method == "POST":
        _object(query, ())
        return 200, answer_decision(daemon, parts[2], handler._body())
    raise APIError(404, "route not found")
