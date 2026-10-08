"""Every chat is a Discord thread (plan §4, decisions C1, O-C1…O-C7).

`ChatMirror` is the one place a chat thread meets Discord, both ways:

* **Out.** A `Role.CHAT` thread (never a task's worker thread: a task has its
  own thread, from the Reporter) gets a Discord thread in its project's
  channel at its first owner message — Inbox chats in #ungrouped — and from
  then on the mirror posts what the HUD shows: "You (HUD): …" with the owner's
  *typed* words (never an inlined file; attachments by name, images as a
  note), Jarvis's settled replies split at 2000 characters (never a delta,
  never thinking), one tool footer per turn and a "turn failed" or
  "interrupted" line. Everything is scrubbed (`secrets.scrub`) and silent
  (flag 4096); the only pings are the D1 lines after a provider question
  (here) and an approval (the gateway's `_post_approval`).
* **In.** A message typed in that Discord thread runs a turn in the same chat
  as `UserMessage(via="discord")` — never echoed back, labelled "via Discord"
  in the HUD. While a turn runs, up to three wait their turn (O-C6). A
  provider question open in the chat is answered by the next typed message.

**Progress is the log, not the bus.** A bus event only says "bring chat X up
to the end of its `log.jsonl`"; how far a chat was posted is
`mirrored_through` in its sidecar `threads/<id>/discord.json` (with `surface`,
`retired`, `moved_to` and `name`), advanced only once a post was delivered.
A dropped event, an outage or a restart therefore costs at worst a late post:
on start, a chat ≤6 messages behind gets them, one further behind gets one
"(N messages while Discord was unavailable, see the HUD)" line.

**Echo prevention.** Bot authors are never heard (v1 `should_respond`, in the
gateway); `via` discord/dm messages are never re-posted; the last 500 Discord
message ids are remembered so a duplicate delivery runs once; and every post
is the bot's own, never a webhook's.

**Rate limits.** Each Discord thread has its own outbox, sent at most 4 posts
per 5 s; above 12 pending, the rest collapse into "(N more messages, open the
HUD)" with the text attached as `reply.txt`. A 429 or 5xx is retried with
back-off; 10003 (the thread was deleted on Discord) unlinks the chat, which
gets a fresh thread at its next message.

**Lifecycle (§4.4).** A move opens a new thread in the new channel ("Continued
from <#old>"), says "Moved to …" in the old one and renames it
`↪ moved to <project> · <name>` (O-C5); the old thread keeps working and its
replies point at the new one. Archive posts a note and archives the Discord
thread as its creator (a 403 is reported, O-P1); restore says "Restored.";
a permanent delete posts a note and unlinks. **Nothing is ever deleted.**

Nothing here logs a URL, a token or a message body: failures are the
operation, the HTTP status and Discord's code (`rest.describe`).
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import math
import queue
import re
import threading
import time

from jarvis.tools.secrets import scrub

from ..model import ProviderName, Role, utcnow
from ..stores import StoreError, _lock as store_lock, _write_bytes
from .render import _cap
from .rest import (MISSING_ACCESS, MISSING_PERMISSIONS, THREAD_ARCHIVED, UNKNOWN_CHANNEL,
                   describe)

LOG = logging.getLogger(__name__)

MAX_CHARS = 2000
NAME_MAX = 100
EXCERPT = 50
RATE_POSTS = 4                 # per Discord thread ...
RATE_WINDOW_S = 5.0            # ... per this many seconds
COLLAPSE_AT = 12               # pending posts in one thread before the rest collapse
CATCH_UP_MAX = 6               # missing messages a restart posts one by one
QUEUE_MAX = 3                  # owner messages waiting on a running turn (O-C6)
SEEN_MAX = 500                 # Discord message ids remembered (echo prevention)
SURFACE_WAIT_S = 3.0           # an approval waits this long for its chat's thread
RETRY_S = 5.0
RETRY_MAX_S = 60.0
DRAIN_POLL_S = 1.0
DOWN_AFTER = 3
FORBIDDEN = frozenset({MISSING_ACCESS, MISSING_PERMISSIONS})
KINDS = frozenset({"user_message", "turn_finished", "question", "question_answered",
                   "thread_updated",
                   "thread_moved", "thread_archived", "thread_restored", "thread_deleted",
                   "proposal_reply"})
# Records of a turn the walk composes into posts; anything else is skipped.
# A provider's gate consultations are logged as gate_requested/gate_resolved
# (daemon.GATE_KINDS, 2026-10-08); they belong to the turn but post nothing.
TURN_KINDS = frozenset({"turn_started", "text", "thinking", "tool_started", "tool_finished",
                        "usage", "error", "question", "question_answered", "plan_updated",
                        "gate_requested", "gate_resolved", "reviewer_declined",
                        "turn_finished"})
MESSAGE_KINDS = frozenset({"user", "text"})
QUEUED_TEXT = "I'll take this next."
FULL_TEXT = ("Three messages are already waiting on this turn; send this one again once "
             "I've answered.")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,60}$")


# -- names and lines ------------------------------------------------------------

def chat_name(thread, first_text: str = "") -> str:
    """The chat's title, else `<id> · <first 50 characters>`, ≤100, scrubbed.
    The id alone is unreadable on a phone (plan §4.3)."""
    title = " ".join((thread.title or "").split())
    if title:
        name = title
    else:
        excerpt = " ".join((first_text or "").split())[:EXCERPT].rstrip()
        name = f"{thread.id} · {excerpt}" if excerpt else thread.id
    return _cap(scrub(name), NAME_MAX)


def moved_name(project_name: str, name: str) -> str:
    """The old thread's name after a move (O-C5, owner addition)."""
    return _cap(f"↪ moved to {project_name} · {name}", NAME_MAX)


def user_line(data: dict) -> str | None:
    """What the HUD's message looks like on Discord. The typed words only."""
    via = data.get("via") or "hud"
    if via in ("discord", "dm"):
        return None                                     # never echoed
    typed = data.get("typed")
    if not isinstance(typed, str):
        typed = data.get("text") if isinstance(data.get("text"), str) else ""
    if isinstance(data.get("skill"), str):
        typed = f"/skill {data['skill']} {typed}".rstrip()
    if via == "system":
        # The first line only: an escape-hatch result is "[owner ran: <cmd>]"
        # followed by the command's output, and file contents are never
        # mirrored (plan §6) — `cat diary.txt` must not reach Discord.
        lines = typed.strip().splitlines() or [""]
        more = " (output in the HUD)" if len(lines) > 1 else ""
        head = f"· system: {_cap(' '.join(lines[0].split()), 300)}{more}"
        return scrub(head)
    elif via == "peer":
        head = f"↪ Request from another thread: {_cap(' '.join(typed.split()), 400)}"
    else:
        who = "You (HUD, voice)" if data.get("spoken") else "You (HUD)"
        head = f"{who}: {typed}"
    lines = [head]
    names = [str(n) for n in (data.get("attachments") or []) if isinstance(n, str)]
    if names:
        lines.append("[attached: " + ", ".join(names) + "]")
    images = data.get("images")
    if isinstance(images, int) and not isinstance(images, bool) and images > 0:
        lines.append(f"[{images} image{'s' if images != 1 else ''}, in the HUD]")
    return scrub("\n".join(lines))


def _error_class(message) -> str:
    head = str(message or "").split(":", 1)[0].strip()
    return head if _IDENT.match(head) else "error"


def footer(tools: list[str], stop: str | None, error: str | None) -> str:
    """`· 4 tools: read_file, grep_files +2`, then `· turn failed (<class>)` or
    `· interrupted`, on one line. Never an error message: a class only."""
    parts = []
    if tools:
        names = list(dict.fromkeys(tools))
        shown = ", ".join(names[:2])
        more = f" +{len(names) - 2}" if len(names) > 2 else ""
        parts.append(f"{len(tools)} tool{'s' if len(tools) != 1 else ''}: {shown}{more}")
    if stop == "error":
        parts.append(f"turn failed ({error or 'error'})")
    elif stop in ("interrupted", "max_turns"):
        parts.append("interrupted" if stop == "interrupted" else "stopped at the turn limit")
    return ("· " + " · ".join(parts)) if parts else ""


def _iso(value) -> float | None:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def _transient(exc) -> bool:
    status = getattr(exc, "status", None)
    return status is None or status == 429 or status >= 500


def sidecar_path(stores, thread_id):
    return stores.threads.path(thread_id).with_name("discord.json")


def read_sidecar(stores, thread_id) -> dict:
    try:
        data = json.loads(sidecar_path(stores, thread_id).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError, UnicodeError):
        return {}
    return data if isinstance(data, dict) else {}


def surface_id(surface) -> str | None:
    """`discord:<id>` -> id; anything else -> None."""
    if isinstance(surface, str) and surface.startswith("discord:"):
        return surface.split(":", 1)[1]
    return None


def describe_surface(daemon, thread) -> dict | None:
    """For the HUD (`GET /threads`): where this chat is on Discord. Names and
    a link only; the guild id is the one setup wrote."""
    surface = getattr(thread, "surface", None)
    if not surface or surface == "dm:retired":
        return None
    if surface == "dm":
        return {"kind": "dm", "channel": "DM", "name": "your DM with Jarvis", "url": None}
    thread_id = surface_id(surface)
    if not thread_id:
        return None
    from . import guild as guildmod
    from .linker import slug
    side = read_sidecar(daemon.stores, thread.id)
    channel = None
    try:
        project = daemon.stores.projects.get(thread.project_id)
    except StoreError:
        project = None
    linker = getattr(getattr(daemon, "discord", None), "linker", None)
    cached = getattr(linker, "_views", {}).get(thread.project_id) if linker else None
    if cached and isinstance(cached[1], dict) and cached[1].get("name"):
        channel = str(cached[1]["name"])
    elif project is not None:
        channel = "ungrouped" if project.inbox else slug(project.name, project.id)
    cfg = guildmod.load()
    return {"kind": "thread", "channel": channel, "name": str(side.get("name") or thread.id),
            "url": (f"https://discord.com/channels/{cfg.guild_id}/{thread_id}"
                    if cfg is not None else None)}


# -- the outbox -------------------------------------------------------------------

@dataclass
class _Post:
    chat_id: str
    channel: str
    action: str = "post"            # post | speak | ping | archive | rename | marker
    content: str | None = None
    files: tuple = ()
    silent: bool = True
    through: int | None = None      # mirrored_through once this one is delivered
    name: str | None = None         # rename
    tries: int = 0
    keep: bool = False              # never collapsed (a question)


class ChatMirror:
    """Construct, then `start()` (subscribes and starts the worker); `close()`."""

    def __init__(self, daemon, rest, *, dm_channel=None, speak=None, router=None,
                 clock=time.monotonic, wall=time.time, rate_posts=None, rate_window_s=None):
        self.daemon = daemon
        self.stores = daemon.stores
        self.rest = rest
        self._dm_channel = dm_channel
        self._speak = speak
        self.router = router
        self._clock, self._wall = clock, wall
        self.rate_posts = rate_posts or RATE_POSTS
        self.rate_window_s = rate_window_s or RATE_WINDOW_S
        self.started_at = utcnow()
        self._lock = threading.RLock()
        self._surface_cv = threading.Condition(self._lock)
        self._dm_lock = threading.Lock()
        self._create_lock = threading.Lock()
        # chat id -> {surface, retired, moved_to, name}; Discord thread id ->
        # (chat id, retired?); the DM conversation's chat id.
        self._chats: dict[str, dict] = {}
        self._places: dict[str, tuple[str, bool]] = {}
        self._dm: str | None = None
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._dirty: set[str] = set()
        self._enqueued: dict[str, int] = {}
        self._outbox: dict[str, deque] = {}
        self._sent: dict[str, deque] = {}
        self._retry_at: dict[str, float] = {}
        self._backoff: dict[str, float] = {}
        # chat id -> (next create attempt, current delay): thread creation
        # backs off like posts do, honouring Discord's retry_after.
        self._create_retry: dict[str, tuple[float, float]] = {}
        self._questions: dict[str, str] = {}
        # chat id -> the req id whose question was put on Discord; and when a
        # question with nowhere to go yet (no DM, a thread still being made)
        # is looked at again.
        self._asked: dict[str, str] = {}
        self._question_retry: dict[str, float] = {}
        self._inbound: dict[str, deque] = {}
        self._drain_at: dict[str, float] = {}
        self._failures = 0
        self.last_error: dict | None = None
        self.forbidden: dict | None = None      # archive/rename refused (O-P1)
        self.counters: dict[str, int] = {}
        self._indexed = threading.Event()
        self._stop = threading.Event()
        self._closing = False
        self._dropped_seen = 0
        self.subscription = None
        self._worker = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "ChatMirror":
        with self._lock:
            if self.subscription is not None:
                return self
            self.subscription = self.daemon.bus.subscribe(
                lambda r: r.get("kind") in KINDS)
        self._index()
        self._worker = threading.Thread(target=self._run, name="jarvis-discord-mirror",
                                        daemon=True)
        self._worker.start()
        return self

    def close(self, flush: bool = True) -> None:
        with self._lock:
            if self._closing:
                return
            self._closing = True
        if self.subscription is not None:
            self.daemon.bus.unsubscribe(self.subscription)
            self.subscription.offer({"kind": "shutdown", "flush": flush})
        if self._worker is not None:
            self._worker.join(timeout=2.5)
        self._stop.set()

    def _kick(self) -> None:
        if self.subscription is not None:
            self.subscription.offer({"kind": "_wake"})

    def _count(self, key, n=1):
        with self._lock:
            self.counters[key] = self.counters.get(key, 0) + n

    # -- the index -----------------------------------------------------------

    def _index(self) -> None:
        """Every chat with a surface (or a retired Discord thread), from disk."""
        try:
            ids = self.stores.threads.ids()
        except (StoreError, OSError):
            ids = []
        dms = []
        for chat_id in ids:
            try:
                thread = self.stores.threads.get(chat_id)
            except StoreError:
                continue
            if thread is None or not _is_chat(thread):
                continue
            side = read_sidecar(self.stores, chat_id)
            if not thread.surface and not side.get("retired"):
                continue
            self._remember(chat_id, thread.surface, side)
            if thread.surface == "dm":
                dms.append(thread)
        if dms:
            dms.sort(key=lambda t: t.created)
            self._dm = dms[-1].id
        self._indexed.set()

    def _remember(self, chat_id, surface, side) -> None:
        with self._surface_cv:
            state = self._chats.setdefault(chat_id, {"surface": None, "retired": [],
                                                     "moved_to": {}, "name": ""})
            state["surface"] = surface
            state["retired"] = [str(r) for r in side.get("retired") or [] if isinstance(r, str)]
            moved = side.get("moved_to")
            state["moved_to"] = dict(moved) if isinstance(moved, dict) else {}
            state["name"] = str(side.get("name") or state.get("name") or "")
            for place in state["retired"]:
                self._places[place] = (chat_id, True)
            current = surface_id(surface)
            if current:
                self._places[current] = (chat_id, False)
            if surface == "dm":
                self._dm = chat_id
            self._surface_cv.notify_all()

    def locate(self, channel_id) -> tuple[str, bool] | None:
        """A Discord thread -> (chat id, retired?), or None."""
        self._indexed.wait(2.0)
        with self._lock:
            return self._places.get(str(channel_id))

    def current_place(self, chat_id) -> str | None:
        with self._lock:
            return surface_id((self._chats.get(chat_id) or {}).get("surface"))

    # -- sidecar -------------------------------------------------------------

    def _save(self, chat_id, side) -> None:
        """Best-effort, and never for a thread that is gone: writing the
        sidecar of a deleted chat would recreate its directory."""
        path = sidecar_path(self.stores, chat_id)
        try:
            with store_lock:
                if not path.with_name("thread.json").exists():
                    return
                _write_bytes(path, json.dumps(side, sort_keys=True).encode("utf-8"))
        except (StoreError, OSError, TypeError, ValueError) as exc:
            self._count("sidecar_errors")
            LOG.warning("Discord chat sidecar for %s not saved (%s)", chat_id,
                        type(exc).__name__)

    def _advance(self, chat_id, through) -> None:
        if through is None:
            return
        side = read_sidecar(self.stores, chat_id)
        if through > int(side.get("mirrored_through") or 0):
            side["mirrored_through"] = through
            self._save(chat_id, side)

    def attach(self, chat_id, surface, name="", *, through=None) -> None:
        """Give a chat its surface: the store first (write-once against stale
        saves), then the sidecar, then the index, then the HUD hears it.
        `through` is where mirroring starts (default: the end of the log)."""
        # The index first, so a message typed in the new thread the moment it
        # exists is already placed; undone if the store refuses.
        place = surface_id(surface)
        if place:
            with self._lock:
                self._places[place] = (chat_id, False)
        try:
            self.stores.threads.set_surface(chat_id, surface)
        except BaseException:
            with self._lock:
                if place and self._places.get(place) == (chat_id, False):
                    self._places.pop(place, None)
            raise
        if through is None:
            try:
                through = len(self.stores.threads.read_log(chat_id))
            except StoreError:
                through = 0
        side = read_sidecar(self.stores, chat_id)
        side.update(surface=surface, name=name or side.get("name") or "",
                    retired=list(side.get("retired") or []), mirrored_through=through)
        self._save(chat_id, side)
        with self._lock:
            self._enqueued[chat_id] = through
        self._remember(chat_id, surface, side)
        self._publish_surface(chat_id)

    def _publish_surface(self, chat_id) -> None:
        try:
            thread = self.stores.threads.get(chat_id)
        except StoreError:
            return
        if thread is None:
            return
        self.daemon.bus.publish({"kind": "thread_updated", "thread_id": chat_id,
                                 "project_id": thread.project_id,
                                 "data": {"thread_id": chat_id, "surface": thread.surface,
                                          "changed": ["surface"]}})

    # -- inbound: the gateway's side ------------------------------------------

    def seen(self, message_id) -> bool:
        """True when this Discord message id already ran (a duplicate)."""
        if not message_id:
            return False
        with self._lock:
            key = str(message_id)
            if key in self._seen:
                return True
            self._seen[key] = None
            while len(self._seen) > SEEN_MAX:
                self._seen.popitem(last=False)
            return False

    def _busy(self, chat_id) -> bool:
        daemon = self.daemon
        lock = getattr(daemon, "_lock", None)
        sessions = getattr(daemon, "_sessions", {})
        if lock is None:
            return False
        with lock:
            session = sessions.get(chat_id)
            return session is not None and session.worker is not None

    def submit(self, chat_id, message) -> str:
        """Run an owner message from Discord in this chat: "sent", "queued"
        (a turn is running; it goes next, O-C6), or "full" (three wait
        already). Anything else the daemon refuses is raised."""
        from ..daemon import DaemonError
        with self._lock:
            waiting = self._inbound.setdefault(chat_id, deque())
            if waiting or self._busy(chat_id):
                if len(waiting) >= QUEUE_MAX:
                    return "full"
                waiting.append(message)
                self._drain_at[chat_id] = min(self._drain_at.get(chat_id, math.inf),
                                              self._clock() + DRAIN_POLL_S)
                self._kick()
                return "queued"
        try:
            self.daemon.send(chat_id, message)
            return "sent"
        except DaemonError as exc:
            if "already running" not in str(exc):
                raise
        with self._lock:
            waiting = self._inbound.setdefault(chat_id, deque())
            if len(waiting) >= QUEUE_MAX:
                return "full"
            waiting.append(message)
            self._drain_at[chat_id] = self._clock() + DRAIN_POLL_S
        self._kick()
        return "queued"

    def pending_question(self, chat_id) -> str | None:
        with self._lock:
            return self._questions.get(chat_id)

    def answer(self, chat_id, text) -> bool:
        """The owner's next typed message answers an open provider question
        (Codex `requestUserInput`) through the existing answer path."""
        with self._lock:
            req_id = self._questions.pop(chat_id, None)
        if not req_id:
            return False
        self.daemon.answer(chat_id, req_id, text)
        return True

    def target(self, chat_id) -> str | None:
        """Where a chat's posts go now: its Discord thread, or the DM."""
        with self._lock:
            surface = (self._chats.get(chat_id) or {}).get("surface")
        if surface == "dm":
            return self._dm_id()
        return surface_id(surface)

    def channel_for(self, chat_id, timeout: float = SURFACE_WAIT_S) -> str | None:
        """For an approval raised in a chat: the chat's place on Discord. A
        chat whose first message is being mirrored right now is waited for
        (bounded), so its approval lands in its thread rather than the DM."""
        deadline = self._clock() + timeout
        with self._surface_cv:
            while True:
                found = self.target(chat_id)
                if found or not self._may_get_surface(chat_id):
                    return found
                left = deadline - self._clock()
                if left <= 0:
                    return None
                self._surface_cv.wait(min(left, 0.1))

    def _may_get_surface(self, chat_id) -> bool:
        try:
            thread = self.stores.threads.get(chat_id)
        except StoreError:
            return False
        if thread is None or not _is_chat(thread):
            return False
        if thread.surface:
            # Stored, and the index is a moment behind: wait for it.
            return thread.surface != "dm:retired"
        project = self.stores.projects.get(thread.project_id)
        return bool(project and project.discord_channel_id and not project.archived)

    def ensure_dm_chat(self) -> str:
        """The DM conversation: one Inbox chat with `surface="dm"`, persisted.
        An archived one is retired (`dm:retired`) and a fresh one opened, so
        the DM never stops answering."""
        from ..projects import is_archived
        with self._dm_lock:
            self._indexed.wait(2.0)
            chat_id = self._dm
            thread = None
            if chat_id:
                try:
                    thread = self.stores.threads.get(chat_id)
                except StoreError:
                    thread = None
            if thread is not None and not thread.archived \
                    and not is_archived(self.stores, thread.project_id):
                return chat_id
            if thread is not None:
                self.stores.threads.set_surface(chat_id, "dm:retired")
                with self._lock:
                    (self._chats.get(chat_id) or {})["surface"] = "dm:retired"
                    self._dm = None
            inbox = self.stores.projects.inbox()
            opened = self.daemon.open_thread(inbox.id, Role.CHAT, ProviderName.FAST, {})
            self.attach(opened.id, "dm", "DM")
            return opened.id

    def start_chat(self, project, channel_id, text, *, message_id=None) -> tuple[str, str]:
        """A new chat from a top-level owner message in a project channel (or
        #ungrouped): Start Thread from Message, or a plain thread for a slash
        command, which has no message. -> (chat id, Discord thread id)."""
        opened = self.daemon.open_thread(project.id, Role.CHAT, ProviderName.FAST, {})
        name = chat_name(opened, text)
        try:
            if message_id:
                place = self.rest.start_thread_from_message(channel_id, message_id, name)
            else:
                place = self.rest.create_thread(channel_id, name)
        except Exception as exc:
            # No Discord thread, so no chat: an empty one left in the HUD
            # would be a conversation nobody had.
            self._failed("create_thread", exc)
            self._discard(opened)
            raise
        if not message_id:
            try:
                self.rest.add_owner(place)
            except Exception as exc:
                self._failed("add_owner", exc)
        self.attach(opened.id, f"discord:{place}", name)
        self._count("threads")
        return opened.id, str(place)

    def _discard(self, thread) -> None:
        """Undo an `open_thread` whose chat never started (nothing was said)."""
        try:
            self.daemon.close_thread(thread.id)
        except Exception:
            pass
        try:
            self.stores.threads.delete(thread.id)
        except Exception as exc:
            LOG.warning("An unstarted chat was not removed (%s)", type(exc).__name__)
            return
        self.daemon.bus.publish({"kind": "thread_deleted", "thread_id": thread.id,
                                 "project_id": thread.project_id,
                                 "data": {"thread_id": thread.id,
                                          "project_id": thread.project_id}})

    # -- the worker ------------------------------------------------------------

    def _run(self) -> None:
        try:
            self._reconcile()
            while not self._stop.is_set():
                try:
                    record = self.subscription.get(timeout=self._timeout())
                except queue.Empty:
                    record = None
                if record is not None:
                    try:
                        if record.get("kind") == "shutdown":
                            if record.get("flush", True):
                                self._final_flush()
                            break
                        dropped = self.subscription.dropped
                        if dropped > self._dropped_seen:
                            # Events were evicted: their effect is in the logs.
                            self._dropped_seen = dropped
                            with self._lock:
                                self._dirty.update(self._chats)
                        self._handle(record)
                    except Exception as exc:
                        self._count("errors")
                        LOG.warning("Discord mirror could not handle an event (%s)",
                                    type(exc).__name__)
                    finally:
                        self.subscription.task_done()
                self._process_dirty()
                self._drain_inbound()
                self._flush()
        finally:
            if self.subscription is not None:
                self.daemon.bus.unsubscribe(self.subscription)

    def _final_flush(self, budget_s: float = 2.0) -> None:
        deadline = self._clock() + budget_s
        self._process_dirty()
        while self._clock() < deadline and self._pending_count():
            if not self._flush():
                break

    def _timeout(self) -> float:
        now = self._clock()
        times = []
        with self._lock:
            for channel, box in self._outbox.items():
                if not box:
                    continue
                times.append(self._retry_at.get(channel, now))
                sent = self._sent.get(channel)
                if sent and len(sent) >= self.rate_posts:
                    times.append(sent[0] + self.rate_window_s)
            times += [at for chat, at in self._drain_at.items() if self._inbound.get(chat)]
            times += [at for at, _delay in self._create_retry.values()]
            times += list(self._question_retry.values())
        if not times:
            return 0.5
        return min(0.5, max(0.0, min(times) - now))

    def _handle(self, record) -> None:
        kind = record.get("kind")
        chat_id = record.get("thread_id") or (record.get("data") or {}).get("thread_id")
        data = record.get("data") or {}
        if kind == "_wake" or not chat_id:
            return
        if kind == "thread_deleted":
            self._deleted(chat_id)
            return
        try:
            thread = self.stores.threads.get(chat_id)
        except StoreError:
            return
        if thread is None or not _is_chat(thread):
            return
        if kind == "user_message":
            with self._lock:
                self._dirty.add(chat_id)
        elif kind == "turn_finished":
            with self._lock:
                self._dirty.add(chat_id)
                self._questions.pop(chat_id, None)
                self._asked.pop(chat_id, None)
                self._question_retry.pop(chat_id, None)
                if self._inbound.get(chat_id):
                    self._drain_at[chat_id] = self._clock()
            if data.get("proposal") and getattr(self.daemon, "runner", None) is None:
                self._proposal_without_runner(thread, record)
        elif kind == "question":
            self._question(thread, data)
        elif kind == "question_answered":
            # Answered in the HUD (or here): the next Discord message is a
            # turn again, not an answer to a question nobody is waiting on.
            with self._lock:
                if self._questions.get(chat_id) == str(data.get("req_id")):
                    self._questions.pop(chat_id, None)
        elif kind == "proposal_reply":
            self._say(chat_id, data.get("reply"),
                      silent=self._quiet(chat_id, data.get("turn_id")))
        elif kind == "thread_updated":
            if "title" in (data.get("changed") or ()):
                self._renamed(thread)
        elif kind == "thread_moved":
            self._moved(thread, data)
        elif kind == "thread_archived":
            self._archived(thread)
        elif kind == "thread_restored":
            self._say(chat_id, "Restored.")

    def _process_dirty(self) -> None:
        with self._lock:
            now = self._clock()
            for chat_id, at in list(self._question_retry.items()):
                if at <= now:
                    self._question_retry.pop(chat_id, None)
                    self._dirty.add(chat_id)
            dirty, self._dirty = self._dirty, set()
        for chat_id in dirty:
            try:
                self._sync(chat_id)
            except Exception as exc:
                self._count("errors")
                LOG.warning("Discord mirror could not bring a chat up to date (%s)",
                            type(exc).__name__)

    # -- restart reconcile -------------------------------------------------------

    def _reconcile(self) -> None:
        """Before the first event: chats created before this mirror started and
        never seen by it are seeded silently (no backfill); a chat with a
        surface that is ≤6 messages behind is caught up, one further behind
        gets one catch-up line."""
        self._count("reconciles")
        started = _iso(self.started_at)
        try:
            ids = self.stores.threads.ids()
        except (StoreError, OSError):
            return
        for chat_id in ids:
            if self._stop.is_set():
                return
            try:
                thread = self.stores.threads.get(chat_id)
                if thread is None or not _is_chat(thread):
                    continue
                side = read_sidecar(self.stores, chat_id)
                log = self.stores.threads.read_log(chat_id)
            except StoreError:
                continue
            if "mirrored_through" not in side:
                created = _iso(thread.created)
                if thread.surface is None and (created is None or started is None
                                               or created < started):
                    side["mirrored_through"] = len(log)
                    self._save(chat_id, side)
                    continue
            if not thread.surface or thread.surface == "dm:retired":
                continue
            through = int(side.get("mirrored_through") or 0)
            missing = sum(1 for row in log[through:] if row.get("kind") in MESSAGE_KINDS)
            if missing > CATCH_UP_MAX:
                target = self.target(chat_id)
                if target:
                    self._enqueue([_Post(chat_id, target, content=(
                        f"({missing} messages while Discord was unavailable, see the HUD)"),
                        through=len(log))])
                    with self._lock:
                        self._enqueued[chat_id] = len(log)
                    self._count("catch_up_lines")
                continue
            with self._lock:
                self._dirty.add(chat_id)

    # -- bringing one chat up to the end of its log -------------------------------

    def _sync(self, chat_id) -> None:
        thread = self.stores.threads.get(chat_id)
        if thread is None or not _is_chat(thread):
            return
        side = read_sidecar(self.stores, chat_id)
        log = self.stores.threads.read_log(chat_id)
        with self._lock:
            start = max(int(side.get("mirrored_through") or 0), self._enqueued.get(chat_id, 0))
        if start >= len(log):
            return
        surface = thread.surface
        if surface == "dm:retired":
            self._mark(chat_id, len(log), side)
            return
        if surface is None:
            first = next((i for i in range(start, len(log)) if log[i].get("kind") == "user"),
                         None)
            if first is None:
                self._mark(chat_id, len(log), side)
                return
            created = self._create_for(thread, log[first], first)
            if created is None:
                return                          # retried later, or settled
            if created is False:
                self._mark(chat_id, len(log), side)
                return
            surface, start = created, first
        else:
            self._remember(chat_id, surface, side)
        target = self.target(chat_id)
        if not target:
            self._question_later(chat_id)
            return
        posts, cursor, running = [], start, None
        while cursor < len(log):
            row = log[cursor]
            kind = row.get("kind")
            if kind == "user":
                line = user_line(row.get("data") or {})
                posts.append(_Post(chat_id, target, content=line,
                                   action="post" if line else "marker", through=cursor + 1))
                cursor += 1
                continue
            turn = row.get("turn_id")
            if not turn or kind not in TURN_KINDS:
                posts.append(_Post(chat_id, target, action="marker", through=cursor + 1))
                cursor += 1
                continue
            end, cut = None, False
            for j in range(cursor, len(log)):
                other = log[j]
                if other.get("kind") == "turn_finished" and other.get("turn_id") == turn:
                    end = j
                    break
                if other.get("kind") == "user" and other.get("turn_id") != turn:
                    end, cut = j - 1, True           # never finished: interrupted
                    break
            if end is None:
                running = turn
                break                                # the turn is still running
            segment = [r for r in log[cursor:end + 1] if r.get("turn_id") == turn]
            posts += self._turn_posts(chat_id, target, log, segment, turn, end + 1, cut)
            cursor = end + 1
        if posts:
            self._enqueue(posts)
        with self._lock:
            self._enqueued[chat_id] = max(self._enqueued.get(chat_id, 0), cursor)
        if running is not None:
            self._open_question(chat_id, log[cursor:], running)

    def _open_question(self, chat_id, rows, turn) -> None:
        """A question its bus event never put on Discord (the event was
        dropped, or there was nowhere to post it yet): found in the log of the
        turn still running — asked, with no later `question_answered` — and
        posted once, with its ping. Only for the turn this process is running:
        a provider question dies with the daemon, so one from before a
        restart is never resurrected."""
        asked = None
        for row in rows:
            if row.get("turn_id") != turn:
                continue
            kind, data = row.get("kind"), row.get("data") or {}
            if kind == "question" and data.get("req_id"):
                asked = data
            elif (kind in ("question_answered", "turn_finished") and asked is not None
                  and (kind == "turn_finished"
                       or str(data.get("req_id")) == str(asked.get("req_id")))):
                asked = None
        if asked is None or not self._live_turn(chat_id, turn):
            return
        req_id = str(asked["req_id"])
        with self._lock:
            if self._asked.get(chat_id) == req_id:
                return
            self._questions[chat_id] = req_id
        self._count("questions_recovered")
        self._post_question(chat_id, asked)

    def _live_turn(self, chat_id, turn) -> bool:
        daemon = self.daemon
        lock = getattr(daemon, "_lock", None)
        sessions = getattr(daemon, "_sessions", None)
        if lock is None or not isinstance(sessions, dict):
            return False
        with lock:
            session = sessions.get(chat_id)
            return (session is not None and session.worker is not None
                    and getattr(session, "turn_id", None) == turn)

    def _question_later(self, chat_id) -> None:
        """A question is open and was never posted: come back to it."""
        with self._lock:
            req_id = self._questions.get(chat_id)
            if req_id and self._asked.get(chat_id) != req_id:
                self._question_retry.setdefault(chat_id, self._clock() + RETRY_S)

    def _mark(self, chat_id, through, side) -> None:
        side["mirrored_through"] = max(int(side.get("mirrored_through") or 0), through)
        self._save(chat_id, side)
        with self._lock:
            self._enqueued[chat_id] = max(self._enqueued.get(chat_id, 0), through)

    def _create_for(self, thread, first_user, first):
        """A HUD chat's Discord thread, at its first owner message. -> the new
        surface; None to retry later; False when it will not get one (no
        channel, archived) and the message is passed over (no backfill)."""
        from ..projects import is_archived
        data = first_user.get("data") or {}
        if thread.archived or is_archived(self.stores, thread.project_id):
            return False
        project = self.stores.projects.get(thread.project_id)
        channel = project.discord_channel_id if project is not None else None
        if not channel:
            return False
        if data.get("via") == "dm":
            return False
        name = chat_name(thread, data.get("typed") or data.get("text") or "")
        with self._lock:
            retry = self._create_retry.get(thread.id)
        if retry is not None and self._clock() < retry[0]:
            with self._lock:
                self._dirty.add(thread.id)           # not yet: the worker comes back
            return None
        with self._create_lock:
            fresh = self.stores.threads.get(thread.id)
            if fresh is not None and fresh.surface:
                return fresh.surface                 # the gateway got there first
            try:
                place = self.rest.create_thread(channel, name)
            except Exception as exc:
                self._failed("create_thread", exc)
                if _transient(exc):
                    # Back off like a post: 5 s doubling to 60 s, never sooner
                    # than Discord's own retry_after.
                    delay = retry[1] * 2 if retry is not None else RETRY_S
                    delay = min(delay, RETRY_MAX_S)
                    wait = max(delay, float(getattr(exc, "retry_after", None) or 0))
                    with self._lock:
                        self._create_retry[thread.id] = (self._clock() + wait, delay)
                        self._dirty.add(thread.id)   # the worker comes back to it
                    return None
                with self._lock:
                    self._create_retry.pop(thread.id, None)
                return False
            with self._lock:
                self._create_retry.pop(thread.id, None)
            try:
                self.rest.add_owner(place)
            except Exception as exc:
                self._failed("add_owner", exc)
            self._count("threads")
            # Mirroring starts at the message that made the thread.
            self.attach(thread.id, f"discord:{place}", name, through=first)
        return f"discord:{place}"

    def _turn_posts(self, chat_id, target, log, segment, turn, through, cut) -> list:
        texts, tools, stop, error = [], [], None, None
        for row in segment:
            kind, data = row.get("kind"), row.get("data") or {}
            if kind == "text" and isinstance(data.get("text"), str):
                texts.append(data["text"])
            elif kind == "tool_started":
                tools.append(str(data.get("name") or "tool")[:40])
            elif kind == "error":
                error = _error_class(data.get("message"))
            elif kind == "turn_finished":
                stop = data.get("stop")
        if cut and stop is None:
            stop = "interrupted"
        asked = next((r.get("data") or {} for r in log
                      if r.get("kind") == "user" and r.get("turn_id") == turn), {})
        via = asked.get("via") or "hud"
        reply_to, pointer = target, ""
        origin = str(asked.get("discord_channel_id") or "")
        if via == "discord" and origin and origin != target:
            with self._lock:
                place = self._places.get(origin)
            if place and place[0] == chat_id:
                # Typed in the old thread after a move: answered there, with
                # the way to the new one (O-C5).
                reply_to = origin
                pointer = f"↪ this chat continues in <#{target}>"
        silent = not (via == "dm" and reply_to == self._dm_id())
        reply = "\n\n".join(t.strip() for t in texts if t.strip())
        from .gateway import _split
        chunks = _split(scrub(reply)) if reply else []
        tail = "\n".join(x for x in (footer(tools, stop, error), pointer) if x)
        if tail:
            if chunks and len(chunks[-1]) + 1 + len(tail) <= MAX_CHARS:
                chunks[-1] = chunks[-1] + "\n" + tail
            else:
                chunks.append(tail)
        posts = [_Post(chat_id, reply_to, content=chunk, silent=silent) for chunk in chunks]
        if reply and asked.get("spoken") and via in ("discord", "dm") and self._speak:
            # Spoken from the scrubbed text, like every other post: a value
            # the scrub redacts must not come back as audio.
            posts.append(_Post(chat_id, reply_to, action="speak", content=scrub(reply),
                               silent=silent))
        if posts:
            posts[-1].through = through
        else:
            posts = [_Post(chat_id, reply_to, action="marker", through=through)]
        return posts

    # -- out-of-band posts ---------------------------------------------------------

    def _quiet(self, chat_id, turn_id) -> bool:
        """Silent, except in the DM for a turn the owner sent from the DM
        (O-C3): a reply to a Discord DM notifies, as DMs always have."""
        if self.target(chat_id) != self._dm_id() or not turn_id:
            return True
        try:
            log = self.stores.threads.read_log(chat_id)
        except StoreError:
            return True
        asked = next((r.get("data") or {} for r in reversed(log)
                      if r.get("kind") == "user" and r.get("turn_id") == turn_id), {})
        return asked.get("via") != "dm"

    def _say(self, chat_id, text, *, silent=True) -> None:
        target = self.target(chat_id)
        if not target or not isinstance(text, str) or not text.strip():
            return
        self._enqueue([_Post(chat_id, target, content=_cap(scrub(text), MAX_CHARS),
                             silent=silent)])

    def _question(self, thread, data) -> None:
        """A provider question (Codex `requestUserInput`): posted at once, the
        turn is waiting on it, then the D1 ping line (not in a DM).

        Recorded first, whether or not it can be posted now: with no place yet
        (the thread still being made, the DM unavailable) the owner's next
        typed message must still answer it, and the sync posts it once the
        place exists."""
        req_id = data.get("req_id")
        if not req_id:
            return
        with self._lock:
            self._questions[thread.id] = str(req_id)
            if self._asked.get(thread.id) == str(req_id):
                # The daemon logs the question before it publishes it, so a
                # sync can recover and post it from the log first: posted once,
                # one ping.
                return
        self._post_question(thread.id, data)

    def _post_question(self, chat_id, data) -> bool:
        target = self.target(chat_id)
        if not target:
            self._question_later(chat_id)
            with self._lock:
                self._dirty.add(chat_id)         # a thread being made: the sync goes on
            return False
        options = [str(o) for o in data.get("options") or [] if isinstance(o, (str, int))]
        lines = [f"Question: {data.get('text') or ''}"]
        lines += [f"{i}. {_cap(o, 200)}" for i, o in enumerate(options[:10], 1)]
        lines.append("Answer by typing here.")
        posts = [_Post(chat_id, target, content=_cap(scrub("\n".join(lines)), MAX_CHARS),
                       keep=True)]
        if target != self._dm_id():
            posts.append(_Post(chat_id, target, action="ping"))
        with self._lock:
            self._asked[chat_id] = str(data.get("req_id"))
            self._question_retry.pop(chat_id, None)
        self._enqueue(posts)
        return True

    def _proposal_without_runner(self, thread, record) -> None:
        """No task runner on this daemon (an embedded or test daemon): the
        mirror hands a fast-path proposal to the router itself, which is
        idempotent per turn id, and posts its sentence."""
        if self.router is None or not self.target(thread.id):
            return
        from ..router import Incoming, NeedsProject
        try:
            outcome = self.router.on_turn_finished(
                record, Incoming(text="", surface="discord", project_id=thread.project_id,
                                 thread_id=thread.id))
        except Exception as exc:
            LOG.warning("Fast-path proposal not handled (%s)", type(exc).__name__)
            return
        text = outcome.reply if isinstance(outcome, NeedsProject) else outcome
        if isinstance(text, str):
            self._say(thread.id, text, silent=self._quiet(thread.id, record.get("turn_id")))

    # -- lifecycle (§4.4) -------------------------------------------------------------

    def _renamed(self, thread) -> None:
        """The owner's HUD rename. The current thread takes the new name; each
        old one keeps its "↪ moved to" header with the new name after it."""
        with self._lock:
            state = self._chats.get(thread.id)
        if not state:
            return
        name = chat_name(thread)
        side = read_sidecar(self.stores, thread.id)
        side["name"] = name
        self._save(thread.id, side)
        with self._lock:
            state["name"] = name
        posts = []
        current = surface_id(state.get("surface"))
        if current:
            posts.append(_Post(thread.id, current, action="rename", name=name))
        for old in state.get("retired") or []:
            project = (state.get("moved_to") or {}).get(old)
            if project:
                posts.append(_Post(thread.id, old, action="rename",
                                   name=moved_name(project, name)))
        self._enqueue(posts)

    def _moved(self, thread, data) -> None:
        with self._lock:
            state = self._chats.get(thread.id)
        old = surface_id((state or {}).get("surface"))
        if not old:
            return                       # not on Discord, or the DM conversation
        project = self.stores.projects.get(data.get("to_project_id") or thread.project_id)
        if project is None:
            return
        side = read_sidecar(self.stores, thread.id)
        name = str(side.get("name") or (state or {}).get("name") or chat_name(thread))
        channel = project.discord_channel_id if not project.archived else None
        new = None
        if channel:
            try:
                new = str(self.rest.create_thread(channel, name))
                self._count("threads")
            except Exception as exc:
                self._failed("create_thread", exc)
            if new:
                try:
                    self.rest.add_owner(new)
                except Exception as exc:
                    self._failed("add_owner", exc)
        label = _cap(scrub(project.name), 80)
        retired = [r for r in side.get("retired") or [] if isinstance(r, str)]
        if old not in retired:
            retired.append(old)
        moved_to = dict(side.get("moved_to") or {})
        moved_to[old] = label
        surface = f"discord:{new}" if new else None
        self.stores.threads.set_surface(thread.id, surface)
        side.update(surface=surface, retired=retired, moved_to=moved_to, name=name)
        self._save(thread.id, side)
        self._remember(thread.id, surface, side)
        posts = [_Post(thread.id, old, content=(
            f"Moved to {label} → <#{new}>" if new else
            f"Moved to {label}, which has no Discord channel yet; this chat continues in the HUD."))]
        posts.append(_Post(thread.id, old, action="rename", name=moved_name(label, name)))
        if new:
            posts.append(_Post(thread.id, new, content=f"Continued from <#{old}>"))
        self._enqueue(posts)
        self._count("moves")
        self._publish_surface(thread.id)

    def _archived(self, thread) -> None:
        target = self.target(thread.id)
        if not target:
            return
        posts = [_Post(thread.id, target, content=(
            "Archived in the HUD. Restore it there to carry on here."))]
        with self._lock:
            place = surface_id((self._chats.get(thread.id) or {}).get("surface"))
        if place:
            posts.append(_Post(thread.id, place, action="archive"))
        self._enqueue(posts)

    def _deleted(self, chat_id) -> None:
        """A note, then unlinked. The Discord thread is kept — never deleted."""
        with self._lock:
            state = self._chats.pop(chat_id, None)
            if not state:
                return
            for place, (owner, _retired) in list(self._places.items()):
                if owner == chat_id:
                    self._places.pop(place, None)
            if self._dm == chat_id:
                self._dm = None
            self._questions.pop(chat_id, None)
            self._asked.pop(chat_id, None)
            self._question_retry.pop(chat_id, None)
            self._inbound.pop(chat_id, None)
        surface = state.get("surface")
        target = self._dm_id() if surface == "dm" else surface_id(surface)
        if target:
            self._enqueue([_Post(chat_id, target, content=(
                "Deleted in the HUD. This Discord thread is kept, but it no longer runs "
                "a chat."))])

    # -- the outbox ----------------------------------------------------------------

    def _dm_id(self) -> str | None:
        if self._dm_channel is None:
            return None
        try:
            channel = self._dm_channel()
        except Exception:
            return None
        return str(channel) if channel else None

    def _enqueue(self, posts) -> None:
        with self._lock:
            for post in posts:
                self._outbox.setdefault(post.channel, deque()).append(post)
            for channel in {p.channel for p in posts}:
                self._collapse(channel)

    def _collapse(self, channel) -> None:
        """Above 12 pending, the rest become one line with the text attached."""
        box = self._outbox.get(channel)
        if not box or len(box) <= COLLAPSE_AT:
            return
        items = list(box)
        keep, rest = items[:COLLAPSE_AT - 1], items[COLLAPSE_AT - 1:]
        # Never folded into the line: a question and its D1 ping (the turn
        # waits on the owner), and thread renames and archives.
        actions = [p for p in rest if p.keep or p.action in ("archive", "rename", "ping")]
        held = {id(p) for p in actions}
        folded = [p for p in rest if id(p) not in held]
        if not folded:
            return
        bodies = [p.content for p in folded if p.action == "post" and p.content]
        through = max((p.through for p in folded if p.through is not None), default=None)
        chat_id = folded[-1].chat_id
        collapsed = []
        if bodies:
            collapsed.append(_Post(chat_id, channel, content=(
                f"({len(bodies)} more messages, open the HUD)"),
                files=(("reply.txt", "\n\n".join(bodies)),),
                silent=all(p.silent for p in folded if p.action == "post"), through=through))
        elif through is not None:
            collapsed.append(_Post(chat_id, channel, action="marker", through=through))
        self._outbox[channel] = deque(keep + collapsed + actions)
        self._count("collapsed", len(bodies))

    def _pending_count(self) -> int:
        with self._lock:
            return sum(len(box) for box in self._outbox.values())

    def _flush(self) -> bool:
        """Send what each thread's window allows. -> anything sent or settled."""
        progressed = False
        now = self._clock()
        with self._lock:
            channels = [c for c, box in self._outbox.items() if box]
        for channel in channels:
            if self._retry_at.get(channel, 0) > now:
                continue
            sent = self._sent.setdefault(channel, deque())
            while sent and now - sent[0] >= self.rate_window_s:
                sent.popleft()
            while True:
                with self._lock:
                    box = self._outbox.get(channel)
                    item = box[0] if box else None
                if item is None:
                    break
                if item.action == "marker":
                    self._pop(channel, item)
                    progressed = True
                    continue
                if len(sent) >= self.rate_posts:
                    break
                outcome = self._send(item)
                if outcome == "retry":
                    delay = self._backoff.get(channel, RETRY_S)
                    self._retry_at[channel] = self._clock() + delay
                    self._backoff[channel] = min(delay * 2, RETRY_MAX_S)
                    break
                self._backoff.pop(channel, None)
                self._retry_at.pop(channel, None)
                if outcome == "gone":
                    self._gone(channel)
                    progressed = True
                    break
                if outcome == "sent":
                    sent.append(self._clock())
                self._pop(channel, item)
                progressed = True
        return progressed

    def _pop(self, channel, item) -> None:
        with self._lock:
            box = self._outbox.get(channel)
            if box and box[0] is item:
                box.popleft()
            if box is not None and not box:
                self._outbox.pop(channel, None)
        self._advance(item.chat_id, item.through)

    def _send(self, item) -> str:
        """-> "sent", "retry" (transient), "gone" (10003) or "dropped"."""
        op = {"post": "post", "speak": "post_audio", "ping": "ping_owner",
              "archive": "archive_thread", "rename": "rename_thread"}[item.action]
        try:
            if item.action == "post":
                self.rest.post(item.channel, content=item.content, files=item.files,
                               silent=item.silent)
            elif item.action == "speak":
                spoken = self._speak(item.content) if self._speak else None
                if spoken:
                    self.rest.post(item.channel, files=(spoken,), silent=item.silent)
            elif item.action == "ping":
                self.rest.ping_owner(item.channel)
            elif item.action == "archive":
                self.rest.archive_thread(item.channel)
            elif item.action == "rename":
                self.rest.rename_thread(item.channel, item.name)
        except Exception as exc:
            code, status = getattr(exc, "code", None), getattr(exc, "status", None)
            if (code == THREAD_ARCHIVED and item.action in ("post", "speak", "ping", "rename")
                    and item.tries == 0):
                item.tries += 1
                try:
                    self.rest.unarchive(item.channel)
                except Exception as again:
                    self._failed("unarchive", again)
                    return "retry" if _transient(again) else "dropped"
                return self._send(item)
            refused = status == 403 or code in FORBIDDEN
            if item.action in ("archive", "rename") and refused:
                # O-P1: the bot cannot do this to its own thread. Reported on
                # the light (set before `_failed` publishes the status);
                # Manage Threads would be the fix.
                self.forbidden = {"op": op, "status": status, "code": code, "at": self._wall()}
                self._failed(op, exc)
                return "dropped"
            self._failed(op, exc)
            if item.action in ("post", "speak") and refused:
                # The thread is closed to the bot: the DM is the safety net
                # (C1, O1), as the Reporter's is for tasks.
                self._to_dm(item)
                return "dropped"
            if _transient(exc):
                return "retry"
            if code == UNKNOWN_CHANNEL:
                return "gone"
            return "dropped"
        self._ok()
        if item.action in ("archive", "rename") and self.forbidden:
            self.forbidden = None                # it works now: the light clears
            self._publish_status()
        self._count({"post": "posts", "speak": "voice", "ping": "pings",
                     "archive": "archives", "rename": "renames"}[item.action])
        return "sent"

    def _to_dm(self, item) -> None:
        """Re-send one refused post to the owner's DM, prefixed with where it
        belonged. Notifies: the owner learns the thread is closed to the bot."""
        dm = self._dm_id()
        if not dm or str(item.channel) == dm:
            return
        try:
            thread = self.stores.threads.get(item.chat_id)
            project = self.stores.projects.get(thread.project_id) if thread else None
        except StoreError:
            project = None
        label = _cap(scrub(project.name), 60) if project is not None else "Jarvis"
        prefix = f"[{label} · chat {item.chat_id}] "
        if item.action == "speak":
            moved = _Post(item.chat_id, dm, action="speak", content=item.content,
                          silent=False, through=item.through)
        else:
            moved = _Post(item.chat_id, dm, content=prefix + (item.content or ""),
                          files=item.files, silent=False, through=item.through)
        self._enqueue([moved])
        self._count("dm_fallback")

    def _gone(self, channel) -> None:
        """Discord says this thread no longer exists (deleted by hand). Its
        posts are settled; a current surface is unlinked, so the chat gets a
        fresh thread at its next message."""
        with self._lock:
            box = self._outbox.pop(channel, deque())
            place = self._places.pop(str(channel), None)
        for item in box:
            self._advance(item.chat_id, item.through)
        self._count("threads_gone")
        if place is None or place[1]:
            return
        chat_id = place[0]
        try:
            self.stores.threads.set_surface(chat_id, None)
        except StoreError:
            return
        side = read_sidecar(self.stores, chat_id)
        side["surface"] = None
        self._save(chat_id, side)
        self._remember(chat_id, None, side)
        self._publish_surface(chat_id)

    # -- inbound drain ----------------------------------------------------------------

    def _drain_inbound(self) -> None:
        from ..daemon import DaemonError
        now = self._clock()
        with self._lock:
            due = [c for c, box in self._inbound.items()
                   if box and self._drain_at.get(c, math.inf) <= now]
        for chat_id in due:
            if self._busy(chat_id):
                with self._lock:
                    self._drain_at[chat_id] = now + DRAIN_POLL_S
                continue
            with self._lock:
                box = self._inbound.get(chat_id)
                message = box[0] if box else None
            if message is None:
                continue
            try:
                self.daemon.send(chat_id, message)
            except DaemonError as exc:
                if "already running" in str(exc):
                    with self._lock:
                        self._drain_at[chat_id] = now + DRAIN_POLL_S
                    continue
                with self._lock:
                    box.popleft()
                channel = message.discord_channel_id or self.target(chat_id)
                if channel:
                    self._enqueue([_Post(chat_id, str(channel), content=_cap(
                        f"I couldn't take your queued message: {exc}", MAX_CHARS))])
                continue
            except Exception as exc:
                LOG.warning("Queued Discord message not sent (%s)", type(exc).__name__)
                with self._lock:
                    box.popleft()
                continue
            with self._lock:
                box.popleft()
                # The next one waits for this turn to finish (or the poll).
                self._drain_at[chat_id] = now + DRAIN_POLL_S

    # -- health --------------------------------------------------------------------

    def _failed(self, op, exc) -> None:
        facts = describe(exc)
        self.last_error = {"op": op, "status": facts["status"], "code": facts["code"],
                           "at": self._wall()}
        self._count("errors")
        LOG.warning("Discord %s failed (HTTP %s, code %s, %s)", op, facts["status"],
                    facts["code"], facts["error"])
        if _transient(exc):
            self._failures += 1
        self._publish_status()

    def _ok(self) -> None:
        if self._failures:
            self._failures = 0
            self._publish_status()

    def _publish_status(self) -> None:
        try:
            self.daemon.bus.publish({"kind": "discord_status", "data": {"mirror": self.status()}})
        except Exception:
            pass

    def status(self) -> dict:
        """For `GET /discord`: a state, a sentence, the number of posts still
        to send, and the last failure as operation, status and code."""
        queued = self._pending_count()
        error = dict(self.last_error) if self.last_error else None
        if self._failures >= DOWN_AFTER:
            state = "down"
            reason = (f"Discord is failing ({error['op']}: HTTP {error['status']}, code "
                      f"{error['code']}); {queued} chat post(s) waiting") if error else "failing"
        elif self.forbidden and self._wall() - self.forbidden.get("at", 0) < 24 * 3600:
            state = "degraded"
            verb = {"archive_thread": "archive", "rename_thread": "rename"}.get(
                self.forbidden.get("op"), "change")
            reason = (f"the bot cannot {verb} its own chat threads (HTTP "
                      f"{self.forbidden['status']}); add Manage Threads")
        elif error and self._wall() - error.get("at", 0) < 600:
            state = "degraded"
            reason = (f"last chat {error['op']} failed (HTTP {error['status']}, code "
                      f"{error['code']})")
        else:
            state, reason = "ok", ""
        return {"state": state, "reason": reason, "queued": queued, "last_error": error}


def _is_chat(thread) -> bool:
    return thread.role == Role.CHAT and thread.task_id is None
