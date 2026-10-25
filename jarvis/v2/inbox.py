"""The owner inbox: one ranked list of everything waiting on the owner
(design `docs/plans/2026-10-24-owner-inbox-design.md`, IN-0 and IN-2).

**A projection, never a store (IN-1).** Every row is computed from its
source each time `rows()` runs: the approval broker's pending requests, the
daemon's live sessions and their open provider questions, and the task
store. Nothing here is written anywhere, so the inbox can never disagree
with the card, the chat or the task it shows. Resolving an item at its
source removes its row from the next read, and `inbox_changed` (published
from the same bus publish that carried the source event) tells the HUD to
read again.

**No new answering path (IN-2).** `POST /inbox/<id>/answer` finds the row
in a fresh projection and calls the function the item's own surface calls,
with the arguments that surface passes:

  * an approval: `Daemon.resolve_approval(req_id, decision, always,
    hud=<the Host test POST /approvals/<id> makes>)`;
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

This module is SELF_PROTECTED (`jarvis/tools/files.py`): it decides what the
owner sees as waiting.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import threading

from .approvals import clean_line
from .model import TaskState

LOG = logging.getLogger(__name__)

# IN-4's bands. Code, not a setting. 5 to 7 (PRs, decisions, checks) belong
# to later packages; 8 here is `failed` only ("denied while away" is IN-3).
RANK_APPROVAL = 1
RANK_QUESTION = 2
RANK_PROPOSAL = 3
RANK_BLOCKED = 4
RANK_FAILED = 8

# Row id prefixes: kind prefix + the source's own id.
_PREFIX = {"approval": "appr", "question": "q", "task_question": "tq",
           "proposal": "prop", "task": "task"}
_ANSWERABLE = {"appr", "q", "tq", "prop"}
_KNOWN = _ANSWERABLE | {"task"}

# The bus kinds the inbox listens to (design §3.3), plus the ones that end
# a chat question without a `question_answered` (its turn ended, its chat
# closed), the question itself being asked, and a project's archive,
# restore or delete (which hide or reveal its task rows).
SOURCE_KINDS = frozenset({
    "approval_requested", "approval_resolved", "task_question", "question_answered",
    "task_status_changed", "proposal_reply",
    "project_archived", "project_restored", "project_deleted",
    "question", "turn_finished", "thread_closed", "thread_archived",
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


def rows(daemon) -> list[dict]:
    """Every row, ranked (IN-4; ties to the older item). Pure: reads only."""
    from .projects import archived_project_ids
    try:
        hidden = archived_project_ids(daemon.stores)
    except Exception:  # noqa: BLE001
        hidden = set()
    out = _approval_rows(daemon, hidden) + _question_rows(daemon, hidden) + _task_rows(daemon, hidden)
    return sorted(out, key=_sort_key)


# -- inbox_changed ---------------------------------------------------------------

class Inbox:
    """Publishes `inbox_changed` ({ids, reason}, never text) after each
    source event, from the bus's synchronous observer, so it follows the
    record that caused it within the same publish. `_asked` remembers which
    chat questions were announced per thread, only so that a turn that ends
    without answering one can name the rows it removed. It is not a row
    store: `rows()` never reads it."""

    def __init__(self, daemon):
        self.daemon = daemon
        self._lock = threading.Lock()
        self._asked: dict[str, set[str]] = {}

    def observe(self, record) -> None:
        if not isinstance(record, dict):
            return
        kind = record.get("kind")
        if kind not in SOURCE_KINDS:
            return
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
            return [f"appr:{req_id}"] if isinstance(req_id, str) and req_id else []
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


def answer(daemon, row_id, body, *, hud: bool):
    """Dispatch one answer to its source's own function (IN-2). The caller
    has already enforced `owner_only`; `hud` is the Host test the approvals
    route makes, passed through unchanged."""
    from .daemon import APIError, _object, _text
    from .provider import Decision
    prefix, _, row_id = _split(row_id)
    if prefix not in _ANSWERABLE:
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
            # `POST /approvals/<id>`'s own call, `hud` and `always` included.
            return daemon.resolve_approval(req_id, decision, always, hud=hud)
        except APIError:
            if not any(r.req_id == req_id for r in daemon.approvals.pending()):
                raise _gone() from None
            raise
    if prefix == "q":
        thread_id, req_id = row["detail"]["thread_id"], row["detail"]["req_id"]
        try:
            # `POST /threads/<id>/answer`'s own call.
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


def route(handler, daemon, parts, query):
    """`GET /inbox` and `POST /inbox/<id>/answer`; None for any other path.
    `owner_only` runs before anything else on every `/inbox` path, so the API
    listener and a non-owner page learn nothing, not even which ids exist."""
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
        # The Host test `POST /approvals/<id>` makes, made the same way.
        hud = config.own_host_header(handler.headers.get("Host", ""), daemon.face_port)
        return 200, answer(daemon, parts[1], handler._body(), hud=hud)
    raise APIError(404, "route not found")
