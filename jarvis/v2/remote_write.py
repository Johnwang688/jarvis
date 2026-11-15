"""Writing from the phone (phone surface design §5, §6, §11 questions 1 and
2; work package P-5): a message to a chat, a new chat, and the task verbs.

Every route here is under `/m/api/` on the remote listener and is reached
only through `remote_auth.api_route`, **after** `remote_auth.owner_status`
passed (design §4 items 1 to 4: this daemon's remote listener, the exact
Host, a live session of an active passkey, and for a write the exact
Origin). The API and HUD listeners never serve `/m/` (`daemon._route`
refuses it first), and nothing here is the HUD: `projects.is_owner` is
never true on this listener.

    POST /m/api/threads/<id>/messages        {text}: a message to a chat
    POST /m/api/threads                      {project_id, provider, model?, effort?, text}
    GET  /m/api/thread-models                what the new-chat sheet offers
    POST /m/api/tasks                        {project_id, brief}: a task in INTAKE
    POST /m/api/tasks/<id>/start             {}: `TaskControl.start`
    POST /m/api/tasks/<id>/resume            {}: `TaskControl.resume`
    POST /m/api/tasks/<id>/cancel            {op}: `TaskControl.cancel` (gated, WS-5c)
    POST /m/api/working/<id>/done            {seq, op}: Done (gated, WS-5c §5.6)
    POST /m/api/working/<id>/undo            {token, op}: its Undo (gated)

(`GET /m/api/threads` and `GET /m/api/tasks` stay P-3's reads, `remote_api`.)

**A phone message is the owner's, without the desk (§11 question 2,
PH-7).** It is `UserMessage(origin="owner", via="phone", desk=False)`, built
by the HUD's own `hud_api.assemble_turn` with `paths=False` (as a message
typed in Discord: no `@path` is read from disk), and it goes to the
daemon's `deliver`, the owner queue every surface shares. Its desk is
false and `desk.desk_message` requires `via="hud"`, so a phone turn never
holds the fast path's direct edit tools (F-1..F-7), never reads terminals
with the desk's authority, and is never an owner turn for the Bypass switch
(`providers/claude.py` reads `via` too). Its log record, the chat reads
(`chat_reads`: "via the phone") and the Discord mirror ("You (phone)") say
where it came from.

**It never steers (P-5's decision).** While a turn runs, a phone message
waits in the owner's queue behind it (`daemon.NEVER_STEER`, read from the
message, so no caller can forget it): never into the running turn, never
interrupting it, and never read as the answer to a card or a question the
turn waits on. A full queue (three waiting) is 409 with the HUD's words.
Steering a turn mid-flight stays at the desk: a phone message is often
typed away from the screen the turn's work is on.

**Caps.** The text is at most `MESSAGE_CHARS` (and a brief at most
`BRIEF_CHARS`) and never blank; attachments and images are refused (400,
`NO_ATTACHMENTS`), whatever the HUD would take; any other field is a 400.
The listener's own body cap (64 KiB) holds before that. Every write here
counts against the session's rate (`RATE` per `RATE_S`, 429 past it); a
gated one (and every gated action of the other phone modules) only once it
is admitted (`Rate.take_if`).

**New chat.** A project the phone lists (not archived) and a provider the
HUD's chips offer (`fast`, `claude`, `codex`; a model and effort as
`thread_model.check` takes them), through `Daemon.open_thread` with an
empty brief, exactly as the HUD's `POST /threads`, then the first message
as above. `GET /m/api/thread-models` is `thread_model.describe()` cut to
labels, defaults and model names.

**Tasks (§11 question 1, owner 2026-10-28: yes, through the intake gate).**
`POST /m/api/tasks` is `Daemon.create_task`, the HUD's `POST /tasks`: the
task waits in INTAKE until Start. Start, resume and cancel are
`daemon.task_verb`, the function the HUD's `POST /tasks/<id>/<verb>` calls,
with the HUD's arguments (an empty body: no provider switch from the
phone). `TASK_START` gates only the phone's own explicit task making and
starting: off, those two routes are not in the table (404). It does not
stop a chat from proposing a task: a fast-path chat a phone message started
may still propose one (`task_propose`), which starts after its grace window
as it does from the HUD or Discord, and which the phone may withdraw.

**Deliberate verbs take the unlock window (WS-5c, replacing the per-cancel
assertion).** Cancel ends a task for good: `CANCELLED` is terminal in
`model.TRANSITIONS`, and resume takes only a blocked task. So a cancel from
the phone is a gated action, as Allow is
(`docs/plans/2026-11-15-ws5-phone-working-set-plan.md` §5.3), in this
order: the body and its op token's shape (400); the session's unlock window
(`RemoteAuth.require_unlocked`: 423 `remote_auth.locked_reply()` when
closed, and nothing else runs); the token's signature and session (410,
400); its admission (`Ops.admit`: 410 expired, 409 duplicate, 429 through
`Rate.take_if`, which is this module's rate, never also `_written`'s); then
the act-time checks (the task visible and not terminal) and
`TaskControl.cancel`. §4.1 is not required: a cancel grants nothing. Done
and Undo (`POST working/<id>/done|undo`) are gated the same way, bound to
the thread in the URL and that thread's drawn `seq` (§5.6); the answer
carries the Undo token and nothing else of the entry. Start and resume need
the session only: neither destroys anything, and every action a started
task takes still passes the gate. A session-only write that lands slides an
open window (`touch_unlock`) and never opens a closed one. A proposal's
withdraw (the inbox row, `remote_answer`) needs the session only, Deny's
footing. Never on the phone: permanent delete (owner HUD-only, archived
only), archive, rename, merge, steering a task, the worktree.

**Recorded as the phone's.** Every task verb from the phone, a created task
included, is a line in the task's journal (`owner_verb`, `via: "phone"`),
and an INFO log line naming the task and the verb (never a brief or a
message's text).

**Sent once (`client_id`).** A message, a new chat and a new task may
carry `client_id` (8 to 64 of `[A-Za-z0-9-]`, the phone's UUID for one
draft). A 2xx (or an uncertain answer, below) is kept per session for
`SENT_TTL_S`, with a hash of the request's body; the same id with the same
body gets that answer back and nothing is done twice, so a send whose
answer was lost (the connection dropped after the request left) can be
retried safely. The same id with another body is 409 (`DRAFT_REUSED`),
never a silent replay; one still on its way is 409 (`IN_FLIGHT`). A
session keeps at most `SENT_PER_SESSION`: past that its own oldest
answered entry goes, never one on its way (all on their way: 429), and
never another session's; `SENT_KEEP` bounds the whole (`SENT_PER_SESSION`
times `remote_auth.MAX_SESSIONS`), dropping the oldest answered entry.

**Uncertain.** An exception after the write took effect (building the
answer's view of a task made, started or cancelled, a chat opened) is
logged with its traceback and answered 409 `{"error": UNCERTAIN,
"uncertain": true}`: the phone says "may not have gone through: check",
never a definite refusal; and it is kept under the draft's id like a 2xx,
so a retry is answered, not run again. An exception the module cannot
place is answered the same way. Never a 500, never an exception's text.

**Refusals.** Malformed input 400; an unknown, archived or hidden thread,
task or project 404; a wrong state (a turn that cannot take a message, a
task that is already done, a full queue) 409; past the rate 429.

SELF_PROTECTED: it decides what the phone may do on this machine.
Tests: tests/v2/remote_write_check.py.
"""
from __future__ import annotations

from collections import deque
import dataclasses
import hashlib
import json
import logging
import re
import threading
import time
from urllib.parse import urlsplit

LOG = logging.getLogger(__name__)

API = "/m/api/"
# Caps, pinned by tests/v2/remote_write_check.py.
MESSAGE_CHARS = 20000
BRIEF_CHARS = 20000
RATE = 30
RATE_S = 60.0
MODELS = 100
SENT_TTL_S = 300.0
SENT_PER_SESSION = 64
SENT_KEEP = SENT_PER_SESSION * 32          # remote_auth.MAX_SESSIONS
_CLIENT_ID = re.compile(r"[A-Za-z0-9-]{8,64}")
IN_FLIGHT = "this message is still being sent; check the chat in a moment"
DRAFT_REUSED = "this draft id was already used for different text"
SENT_FULL = "too many writes from this phone are still on their way; wait a moment"
UNCERTAIN = "it may not have gone through: check before trying again"
# §11 question 1 (owner, 2026-10-28): the phone may start tasks, through the
# HUD's intake gate. False takes `POST tasks` and `tasks/<id>/start` out of
# the table: the phone's own explicit task making and starting only, never a
# chat's proposal (`task_propose`), as on the HUD and Discord.
TASK_START = True
PROVIDERS = ("fast", "claude", "codex")
NO_ATTACHMENTS = "attachments and images are not sent from the phone; attach them at the desk"
# WS-5c: Done's Undo token, as `working._TOKEN` takes it.
_UNDO_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,64}")
RATE_WORD = "too many writes from this phone; wait a minute"
NOT_A_CHAT = "only a chat takes a message from the phone"
_ID = re.compile(r"[0-9a-f]{8}")
# The reply of `Daemon.deliver` the phone is sent.
SEND_FIELDS = ("status", "turn_id", "message_id", "position", "queued_turn_id")

clock = time.monotonic


class _Refused(Exception):
    def __init__(self, status: int, word: str, extra: dict | None = None):
        super().__init__(word)
        self.status = status
        self.word = word
        self.extra = extra or {}


def _word(text) -> str:
    """A refusal's words from the daemon (code-written), scrubbed and capped
    as every phone string is."""
    from .remote_api import _scrub
    return _scrub(str(text))[:300]


def _call(fn):
    """`fn()`, with the daemon's refusals mapped as its own handler maps them
    (`daemon._handler._dispatch`): an `APIError`'s status, a lifecycle
    conflict 409, an unknown object 404, a bad value 400."""
    from .control import ControlError
    from .daemon import APIError, DaemonError
    from .provider import BriefRefused
    from .stores import ProjectArchived
    from .worktrees import WorktreeError
    try:
        return fn()
    except _Refused:
        raise
    except APIError as exc:
        raise _Refused(exc.status if 400 <= exc.status < 500 else 409, _word(exc)) from None
    except (DaemonError, BriefRefused, ControlError, ProjectArchived, WorktreeError) as exc:
        raise _Refused(409, _word(exc)) from None
    except FileNotFoundError:
        raise _Refused(404, "not found") from None
    except PermissionError:
        raise _Refused(403, "refused") from None
    except LookupError as exc:
        if isinstance(exc, KeyError):
            raise _Refused(400, "bad request") from None
        raise _Refused(404, "not found") from None
    except (ValueError, TypeError, TimeoutError):
        raise _Refused(400, "bad request") from None
    except Exception as exc:  # noqa: BLE001 — a provider's or the OS's: 409, never its text
        LOG.exception("phone: a write failed (%s)", type(exc).__name__)
        raise _Refused(409, "request failed (" + type(exc).__name__ + ")") from None


# -- the session's rate -----------------------------------------------------------


class Rate:
    """Each session's writes in the last `RATE_S` (monotonic): at most `RATE`."""

    def __init__(self):
        self._lock = threading.Lock()
        self._seen: dict[str, deque] = {}

    def take(self, digest: str) -> bool:
        now = clock()
        with self._lock:
            for key in [k for k, q in self._seen.items() if not q or now - q[-1] >= RATE_S]:
                del self._seen[key]
            seen = self._seen.setdefault(digest, deque())
            while seen and now - seen[0] >= RATE_S:
                seen.popleft()
            if len(seen) >= RATE:
                return False
            seen.append(now)
            return True

    def take_if(self, digest: str, now: float, ok) -> str:
        """WS-5c §5.3 step 4: under this lock, the window first ("rate" when
        full), then `ok()` (the `Ops` capacity, called with the Ops lock
        already held: "full" when it refuses), and the write is recorded only
        when both pass ("go"). So a refusal spends no rate token. The lock
        order is always Ops, then this."""
        with self._lock:
            for key in [k for k, q in self._seen.items() if not q or now - q[-1] >= RATE_S]:
                del self._seen[key]
            seen = self._seen.get(digest) or deque()
            while seen and now - seen[0] >= RATE_S:
                seen.popleft()
            if len(seen) >= RATE:
                return "rate"
            if not ok():
                return "full"
            seen.append(now)
            self._seen[digest] = seen
            return "go"


_RATE_LOCK = threading.Lock()


def rate_of(server) -> Rate:
    with _RATE_LOCK:
        found = getattr(server, "phone_rate", None)
        if not isinstance(found, Rate):
            found = Rate()
            server.phone_rate = found
        return found


class Sent:
    """Each session's answered writes by `client_id`, for `SENT_TTL_S`
    (monotonic): (session, route, object, id) -> [time, body hash, answer or
    None while it is on its way]."""

    def __init__(self):
        self._lock = threading.Lock()
        self._seen: dict[tuple, list] = {}

    def _evict(self, keys) -> bool:
        """The oldest answered entry among `keys` goes; False when every one
        is still on its way."""
        done = [k for k in keys if self._seen[k][2] is not None]
        if not done:
            return False
        del self._seen[min(done, key=lambda k: self._seen[k][0])]
        return True

    def begin(self, key, digest: str):
        """("go", None): go ahead (the key is now on its way); ("answer", a)
        the earlier answer; ("busy"|"reused"|"full", None) a refusal."""
        now = clock()
        with self._lock:
            self._seen = {k: v for k, v in self._seen.items()
                          if v[2] is None or now - v[0] < SENT_TTL_S}
            found = self._seen.get(key)
            if found is not None:
                if found[1] != digest:
                    return "reused", None
                return ("busy", None) if found[2] is None else ("answer", found[2])
            mine = [k for k in self._seen if k[0] == key[0]]
            if len(mine) >= SENT_PER_SESSION and not self._evict(mine):
                return "full", None
            if len(self._seen) >= SENT_KEEP and not self._evict(list(self._seen)):
                return "full", None
            self._seen[key] = [now, digest, None]
            return "go", None

    def end(self, key, answer: tuple | None) -> None:
        """Keep an answer; forget the key when None (a retry may then go)."""
        with self._lock:
            if answer is None:
                self._seen.pop(key, None)
            elif key in self._seen:
                self._seen[key] = [clock(), self._seen[key][1], answer]


def sent_of(server) -> Sent:
    with _RATE_LOCK:
        found = getattr(server, "phone_sent", None)
        if not isinstance(found, Sent):
            found = Sent()
            server.phone_sent = found
        return found


def _after(fn):
    """`fn()` once the write took effect (its answer's view): an exception
    here is uncertain, never a definite refusal."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        LOG.exception("phone: a write took effect, its answer failed (%s)", type(exc).__name__)
        raise _Refused(409, UNCERTAIN, {"uncertain": True}) from None


def _client_id(value: dict):
    """The body's `client_id` taken out (None when absent), or a 400."""
    if not isinstance(value, dict) or "client_id" not in value:
        return None
    found = value.pop("client_id")
    if not isinstance(found, str) or not _CLIENT_ID.fullmatch(found):
        raise _Refused(400, "client_id must be 8 to 64 letters, digits or dashes")
    return found


# -- what is recorded -------------------------------------------------------------


def record(daemon, task_id: str, verb: str) -> None:
    """A task verb from the phone, in the task's journal and the log. A
    journal that cannot be written loses the line, never the verb."""
    LOG.info("phone: task %s %s", task_id, verb)
    try:
        daemon.stores.tasks.journal(task_id, "owner_verb", verb=verb, via="phone")
    except Exception:  # noqa: BLE001 — the verb already happened
        LOG.warning("phone: the journal line for task %s %s was not written", task_id, verb)


# -- checks ------------------------------------------------------------------------


def _fields(body, allowed: tuple[str, ...], required: tuple[str, ...] = ()) -> dict:
    if not isinstance(body, dict):
        raise _Refused(400, "bad request")
    if "images" in body or "attachments" in body:
        raise _Refused(400, NO_ATTACHMENTS)
    if set(body) - set(allowed) or set(required) - set(body):
        raise _Refused(400, "bad request")
    return body


def _text(value, cap: int, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _Refused(400, f"{name} must be a nonempty string")
    if len(value) > cap:
        raise _Refused(400, f"{name} is at most {cap} characters from the phone")
    return value


def _project(daemon, project_id):
    from .remote_api import _get
    found = _get(daemon.stores.projects, project_id)
    if found is None or found.archived:
        raise _Refused(404, "not found")
    return found


def _chat(daemon, thread_id):
    """A thread the phone lists, and an owner's chat (never a task's)."""
    from .model import Role
    from .remote_api import _Refused as ReadRefused, _visible_thread
    try:
        found = _visible_thread(daemon, thread_id)
    except ReadRefused as exc:
        raise _Refused(exc.status, exc.word) from None
    if found.role != Role.CHAT or found.task_id is not None:
        raise _Refused(409, NOT_A_CHAT)
    return found


def _task(daemon, task_id):
    from .remote_api import _Refused as ReadRefused, _visible_task
    try:
        return _visible_task(daemon, task_id)
    except ReadRefused as exc:
        raise _Refused(exc.status, exc.word) from None


def phone_message(project, text: str):
    """The owner's typed words as the HUD's send builds them
    (`assemble_turn`, `paths=False`), from the phone: `via="phone"`, no
    desk, nothing attached."""
    from .hud_api import assemble_turn
    message = assemble_turn(project, {"text": text}, paths=False, owner=False, captures=None)
    return dataclasses.replace(message, origin="owner", via="phone", desk=False, images=[], attachments=[],
                               spoken=False)


def _delivered(daemon, thread_id: str, message) -> dict:
    reply = _call(lambda: daemon.deliver(thread_id, message))
    return {k: reply[k] for k in SEND_FIELDS if isinstance(reply, dict) and k in reply}


# -- the routes --------------------------------------------------------------------


def send(daemon, thread_id: str, body) -> dict:
    """`POST threads/<id>/messages`: one message to a chat (queues behind a
    running turn; never steers)."""
    text = _text(_fields(body, ("text",), ("text",))["text"], MESSAGE_CHARS, "text")
    thread = _chat(daemon, thread_id)
    project = _project(daemon, thread.project_id)
    out = _delivered(daemon, thread.id, phone_message(project, text))
    LOG.info("phone: a message to chat %s (%s)", thread.id, out.get("status"))
    return out


def new_chat(daemon, body, live=lambda: True) -> dict:
    """`POST threads`: a chat in a project on a provider, and its first
    message. Opening a provider can take seconds: the session must still
    hold (`live`) before the first message is sent."""
    from .remote_api import thread as thread_view
    body = _fields(body, ("project_id", "provider", "model", "effort", "text"),
                   ("project_id", "provider", "text"))
    text = _text(body["text"], MESSAGE_CHARS, "text")
    provider = body["provider"]
    if provider not in PROVIDERS:
        raise _Refused(400, "provider must be one of " + ", ".join(PROVIDERS))
    brief = {}
    for key in ("model", "effort"):
        value = body.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > 200:
            raise _Refused(400, f"{key} must be a string")
        if value.strip():
            brief[key] = value
    project = _project(daemon, body["project_id"])
    # The HUD's `POST /threads` for a chat: an empty brief and the choice,
    # the fence fields the project's own (`Daemon.make_brief`).
    thread = _call(lambda: daemon.open_thread(project.id, "chat", provider, brief))
    LOG.info("phone: new %s chat %s", provider, thread.id)
    shown = _after(lambda: thread_view(daemon.thread_json(thread)))
    if not live():
        raise _Refused(401, "unauthorized", {"thread": shown})
    try:
        sent = _delivered(daemon, thread.id, phone_message(project, text))
    except _Refused as exc:
        raise _Refused(exc.status, exc.word, {"thread": shown}) from None
    return {"thread": shown, "send": sent}


def models(daemon) -> dict:
    """`GET thread-models`: the HUD's chips (`thread_model.describe`), cut to
    what the new-chat sheet shows; `available` when the provider is in the
    daemon's roster."""
    from . import thread_model
    from .model import ProviderName
    described = _call(thread_model.describe)
    out = {}
    for name in PROVIDERS:
        entry = described.get("providers", {}).get(name)
        if not isinstance(entry, dict):
            continue
        rows = []
        for row in (entry.get("models") or [])[:MODELS]:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                continue
            efforts = row.get("efforts")
            rows.append({"id": row["id"][:200], "name": str(row.get("name") or row["id"])[:200],
                         "efforts": [e for e in efforts if isinstance(e, str)][:12] if isinstance(efforts, list) else []})
        out[name] = {"label": str(entry.get("label") or name), "available": ProviderName(name) in daemon.providers,
                     "default": entry.get("default") if isinstance(entry.get("default"), str) else None,
                     "default_effort": (entry.get("default_effort")
                                        if isinstance(entry.get("default_effort"), str) else None),
                     "models": rows}
    return {"providers": out}


def new_task(daemon, body) -> dict:
    """`POST tasks`: the HUD's `POST /tasks` (`Daemon.create_task`): INTAKE,
    waiting for Start."""
    from .model import to_json
    from .remote_api import task as task_view
    body = _fields(body, ("project_id", "brief"), ("project_id", "brief"))
    brief = _text(body["brief"], BRIEF_CHARS, "brief")
    project = _project(daemon, body["project_id"])
    task = _call(lambda: daemon.create_task(project.id, brief))
    record(daemon, task.id, "create")
    return {"task": _after(lambda: task_view(to_json(task)))}


def verb(daemon, task_id: str, name: str, body) -> dict:
    """`POST tasks/<id>/start|resume`: `daemon.task_verb`, the HUD's own call,
    with the HUD's (empty) body."""
    from .daemon import task_verb
    from .remote_api import task as task_view
    _fields(body, ())
    task = _task(daemon, task_id)
    out = _call(lambda: task_verb(daemon, task.id, name, {}))
    record(daemon, task.id, name)
    return {"task": _after(lambda: task_view(out))}


def _catch(fn):
    """`fn()`'s answer, with a refusal answered as its status (the act-time
    half of a gated action: nothing here raises past the op's settling)."""
    try:
        return fn()
    except _Refused as exc:
        return exc.status, {"error": exc.word, **exc.extra}, []
    except Exception as exc:  # noqa: BLE001 — may have taken effect: uncertain
        LOG.exception("phone: a write failed (%s)", type(exc).__name__)
        return 409, {"error": UNCERTAIN, "uncertain": True}, []


def cancel(handler, daemon, session, path: str, task_id: str, body) -> tuple:
    """`POST tasks/<id>/cancel {op}`: a gated action (WS-5c §5.3). The body
    and the token's shape (400), then the unlock window (423), the token
    and its admission (`remote_auth.gated`), then the act-time checks
    (the task visible and not terminal: 404, 409) and `daemon.task_verb`,
    the HUD's own call."""
    from .remote_auth import gated, parse_op
    body = _fields(body, ("op",), ("op",))
    if not isinstance(task_id, str) or not _ID.fullmatch(task_id):
        raise _Refused(400, "bad request")
    op = parse_op(body["op"])
    return gated(handler, session, path, body, op, "task",
                 lambda ticket: _catch(lambda: _cancel(daemon, task_id, ticket)))


def _cancel(daemon, task_id: str, ticket) -> tuple:
    from .daemon import task_verb
    from .model import TERMINAL_STATES
    from .remote_api import task as task_view
    task = _task(daemon, task_id)
    if task.state in TERMINAL_STATES:
        raise _Refused(409, f"this task is already {task.state.value}")
    ticket.mutating()
    out = _call(lambda: task_verb(daemon, task.id, "cancel", {}))
    ticket.returned()
    ticket.result = "cancelled"
    record(daemon, task.id, "cancel")
    return 200, {"task": _after(lambda: task_view(out))}, []


# -- Done and Undo from the phone (WS-5c §5.6) ------------------------------------------


def _working(daemon):
    found = getattr(daemon, "working", None)
    if found is None:
        raise _Refused(503, "the In progress list is not running")
    return found


def done(handler, daemon, session, path: str, thread_id: str, body) -> tuple:
    """`POST working/<id>/done {seq, op}`: Done on the thread in the URL, as
    drawn (its own `seq`, captured at the tap). 400 for a malformed id or
    `seq`; then the window, the token, the thread still visible, and
    `WorkingSet.done` (409 stale, out, a linked row or a running turn, with
    the daemon's words). The answer is `{thread_id, done, token, undo_s}`
    and nothing else of the entry."""
    from .remote_auth import gated, parse_op
    body = _fields(body, ("seq", "op"), ("seq", "op"))
    if not isinstance(thread_id, str) or not _ID.fullmatch(thread_id):
        raise _Refused(400, "id must be eight lowercase hex characters")
    seq = body["seq"]
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise _Refused(400, "seq must be a whole number")
    op = parse_op(body["op"])

    def act(ticket):
        def run():
            working = _working(daemon)
            _visible(daemon, thread_id)
            ticket.mutating()
            status, payload = working.done(thread_id, seq)
            ticket.returned()
            if status != 200:
                return status, {"error": _word(payload.get("error") if isinstance(payload, dict) else "")}, []
            ticket.result = "done"
            LOG.info("phone: Done from the phone on chat %s", thread_id)
            return 200, {"thread_id": thread_id, "done": True, "token": payload["token"],
                         "undo_s": payload["undo_s"]}, []
        return _catch(run)
    return gated(handler, session, path, body, op, "working", act)


def undo(handler, daemon, session, path: str, thread_id: str, body) -> tuple:
    """`POST working/<id>/undo {token, op}`: Done's own token, within its
    window (`WorkingSet.undo`: 409 for a wrong token, too late, or after an
    archive)."""
    from .remote_auth import gated, parse_op
    body = _fields(body, ("token", "op"), ("token", "op"))
    if not isinstance(thread_id, str) or not _ID.fullmatch(thread_id):
        raise _Refused(400, "id must be eight lowercase hex characters")
    token = body["token"]
    if not isinstance(token, str) or not _UNDO_TOKEN.fullmatch(token):
        raise _Refused(400, "token must be the one Done returned")
    op = parse_op(body["op"])

    def act(ticket):
        def run():
            working = _working(daemon)
            ticket.mutating()
            status, payload = working.undo(thread_id, token)
            ticket.returned()
            if status != 200:
                return status, {"error": _word(payload.get("error") if isinstance(payload, dict) else "")}, []
            ticket.result = "undone"
            LOG.info("phone: Undo from the phone on chat %s", thread_id)
            return 200, {"thread_id": thread_id, "done": False}, []
        return _catch(run)
    return gated(handler, session, path, body, op, "working", act)


def _visible(daemon, thread_id):
    from .remote_api import _Refused as ReadRefused, _visible_thread
    try:
        return _visible_thread(daemon, thread_id)
    except ReadRefused as exc:
        raise _Refused(exc.status, exc.word) from None


def _match(parts: list[str]):
    """(name, id) of a route here, or None. `threads` and `tasks` alone are
    here for POST only (their GET is P-3's)."""
    if len(parts) == 3 and parts[0] == "threads" and parts[2] == "messages":
        return "send", parts[1]
    if parts == ["threads"]:
        return "new_chat", None
    if parts == ["thread-models"]:
        return "models", None
    if TASK_START and parts == ["tasks"]:
        return "new_task", None
    if len(parts) == 3 and parts[0] == "tasks" and parts[2] in (("start", "resume") if TASK_START else ("resume",)):
        return parts[2], parts[1]
    if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "cancel":
        return "cancel", parts[1]
    if len(parts) == 3 and parts[0] == "working" and parts[2] in ("done", "undo"):
        return parts[2], parts[1]
    return None


# The two collections whose GET stays P-3's read (`remote_api`).
_SHARED = ("new_chat", "new_task")
# WS-5c's gated writes: the unlock window, an op token, their own rate
# (`Rate.take_if` at admission), never `_written`'s.
GATED = {"cancel": cancel, "done": done, "undo": undo}


def route(handler, path: str, session: dict, body: bytes):
    """(status, payload, extra headers) for one of this module's paths, or
    None for any other path (P-3's reads take it)."""
    if not path.startswith(API):
        return None
    matched = _match(path[len(API):].split("/"))
    if matched is None:
        return None
    name, object_id = matched
    method = handler.command
    if name in _SHARED and method in ("GET", "HEAD"):
        return None
    want = "GET" if name == "models" else "POST"
    if method != want:
        allow = "GET, POST" if name in _SHARED else want
        return 405, {"error": "method not allowed"}, [("Allow", allow)]
    daemon = getattr(handler, "daemon", None)
    auth = getattr(handler, "remote_auth", None)
    if daemon is None or auth is None:
        return 404, {"error": "not found"}, []
    from .remote_auth import AuthError, Locked, auth_reply, locked_reply, _json_body
    if urlsplit(getattr(handler, "path", "") or "").query:
        return 400, {"error": "bad request"}, []
    try:
        if name == "models":
            return 200, models(daemon), []
        value = _json_body(body)
        if name in GATED:
            return GATED[name](handler, daemon, session, path, object_id, value)
        if name in ("send", "new_chat", "new_task"):
            client = _client_id(value)
            if client is not None:
                key = (session.get("token_sha256") or "", name, object_id, client)
                digest = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                                   default=str).encode()).hexdigest()
                sent = sent_of(handler.server)
                state, found = sent.begin(key, digest)
                if state == "reused":
                    return 409, {"error": DRAFT_REUSED}, []
                if state == "busy":
                    return 409, {"error": IN_FLIGHT}, []
                if state == "full":
                    return 429, {"error": SENT_FULL}, []
                if state == "answer":
                    return found[0], {**found[1], "repeat": True}, []
                answer = None
                try:
                    answer = _written(handler, daemon, session, name, object_id, value, auth)
                    return answer
                finally:
                    keep = answer is not None and (200 <= answer[0] < 300 or answer[1].get("uncertain") is True)
                    sent.end(key, answer[:2] if keep else None)
        return _written(handler, daemon, session, name, object_id, value, auth)
    except _Refused as exc:
        return exc.status, {"error": exc.word, **exc.extra}, []
    except Locked:
        return locked_reply()
    except AuthError as exc:
        return auth_reply(exc)
    except Exception as exc:  # noqa: BLE001 — never a 500 with an exception's text
        LOG.exception("phone: a write failed (%s)", type(exc).__name__)
        if name == "models":
            return 409, {"error": "request failed (" + type(exc).__name__ + ")"}, []
        # Where it failed is not known here: it may have taken effect.
        return 409, {"error": UNCERTAIN, "uncertain": True}, []


def _written(handler, daemon, session, name, object_id, value, auth):
    """One session-only write, past the body, the id and before the rate.
    A write that lands slides an open unlock window (`touch_unlock`), never
    opening a closed one (WS-5c §5.1)."""
    from .remote_auth import AuthError, Locked, auth_reply, locked_reply
    try:
        if not rate_of(handler.server).take(session.get("token_sha256") or ""):
            return 429, {"error": RATE_WORD}, [("Retry-After", str(int(RATE_S)))]
        if name == "send":
            answer = 202, send(daemon, object_id, value), []
        elif name == "new_chat":
            answer = 201, new_chat(daemon, value, lambda: auth.session_live(session.get("token_sha256"))), []
        elif name == "new_task":
            answer = 201, new_task(daemon, value), []
        else:
            answer = 200, verb(daemon, object_id, name, value), []
        auth.touch_unlock(session)
        return answer
    except _Refused as exc:
        return exc.status, {"error": exc.word, **exc.extra}, []
    except Locked:
        return locked_reply()
    except AuthError as exc:
        return auth_reply(exc)
    except Exception as exc:  # noqa: BLE001 — may have taken effect: uncertain, kept under the id
        LOG.exception("phone: a write failed (%s)", type(exc).__name__)
        return 409, {"error": UNCERTAIN, "uncertain": True}, []
