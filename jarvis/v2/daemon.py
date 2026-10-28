"""Jarvis v2's state owner and loopback JSON/SSE API. No routing or policy.

Providers are caller-owned instances. main() alone loads the configured roster.
Threads save their Brief beside thread.json so resuming never invents a policy.
"""
from __future__ import annotations

import contextlib
from collections import deque
from dataclasses import dataclass, field, fields, replace
import errno
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
import logging
import os
import sys
from pathlib import Path
import queue
import re
import signal
import threading
import time
from typing import Callable
from urllib.parse import parse_qs, urlsplit
import uuid

from jarvis import config
from .approvals import ApprovalRequest, PendingApprovals, card_text
from .bus import EventBus
from .control import ControlError
from .permissions import PermitContext, build_permit
from .model import (PermissionProfile, Project, ProviderName, Role, Thread,
                    from_json, to_json, utcnow)
from .provider import (Brief, BriefRefused, Decision, Event, EventKind,
                       PermissionCallback, Provider, SessionHandle, SessionLost, SteerRefused,
                       Usage, UserMessage)
from .stores import ProjectArchived, Stores, StoreError, _validate, _write_bytes
from .desk import DeskState, desk_message
from . import running_cost
from . import worktrees

LOG = logging.getLogger(__name__)
STOP_TIMEOUT = 2.0
# How long `stop()` waits for the approval waiters it released to announce.
RELEASE_WAIT_S = 2.0

# A provider's APPROVAL_REQUESTED / APPROVAL_RESOLVED mean "the gate was
# consulted about this tool call": the Claude hook emits a pair for *every*
# tool use, and Codex for every escalation, before `permit` has decided
# anything. They are not questions for the owner. Only the broker
# (`PendingApprovals`, through `_approval_requested`) publishes those, and only
# when a decision really waits on a human. Forwarded under their own names,
# they became a card and a DM per tool call that vanished a moment later when
# `permit` allowed it: the auto-mode flashing and the Discord DM spam
# (2026-10-08). They stay in the thread log under these names and never reach
# the bus.
# The fields of an event's data that hold a command or a tool's arguments:
# what `log.jsonl` keeps with typed credentials withheld (owner, 2026-10-15).
LOGGED_REDACTED = ("args", "command", "cmd", "reason", "headline")


# A session's memory of its native children for card provenance (C6): the
# relay stamps of at most CHILD_STAMPS children (oldest forgotten first) and
# every sid it has seen, for the session's life. A child seen before is never
# stamped again, so one whose stamp was forgotten can only get the "running"
# line. The seen set only grows: a provider that forgot a child may announce
# it again as new (Claude past its CHILD_LIMIT), and a forgotten sid would be
# restamped from whatever turn runs then. It costs one short string per child
# ever spawned in the session (about 100 bytes; 10,000 children is ~1 MB),
# and goes with the session.
CHILD_STAMPS = 256
# The tools whose call spawns a Claude child "cl:<call id>" (SV-0): seen in
# the orchestrator's own stream, inside the turn that made it.
CLAUDE_SPAWN_TOOLS = frozenset(("Agent", "Task"))
_RELAY_PROVIDERS = ("fast", "claude", "codex")
_RELAY_THREAD_ID = re.compile(r"[0-9a-f]{8}")


def _turn_relay(session, kind: str = "started") -> dict | None:
    """Chat-to-chat C6, display only: the chat whose message (a relayed turn)
    or reply (a reply turn) started the turn `session` is running now, as
    {"title", "provider", "thread_id", "kind"}, else None — an owner's turn,
    typed, spoken or from Discord, a task's, or no turn at all. Read outside
    the lock (one attribute each; a mark that does not name the item's chat
    is taken for a turn changing hands and shows nothing). A provider or id
    of the wrong shape is left out, never the line."""
    item, mark = getattr(session, "relay_turn", None), getattr(session, "relay_mark", None)
    if item is None or not isinstance(mark, dict):
        return None
    peer = item.target if mark.get("reply") else item.sender
    if not isinstance(peer, dict) or not peer.get("thread_id") or peer.get("thread_id") != mark.get("from"):
        return None
    from .chat_reads import card_title
    provider, thread_id = peer.get("provider"), peer.get("thread_id")
    return {"title": card_title(peer.get("title")),
            "provider": provider if provider in _RELAY_PROVIDERS else "",
            "thread_id": thread_id if isinstance(thread_id, str) and _RELAY_THREAD_ID.fullmatch(thread_id) else "",
            "kind": kind}


def _relayed_from(session, child: str | None = None) -> dict | None:
    """The provenance a card shows (C6), display only; no permission layer
    reads it.

    - The orchestrator's own call (`child` None): the running turn's, kind
      "started" ("turn started by a message from chat ‹S›").
    - A native sub-agent's call (`child` its sid) whose spawning turn was a
      relayed one (`_stamp_child`): that turn's, kind "started", even after
      it ended.
    - Any other child's call, or one nobody can attribute (`child` "": an
      unknown child, the jarvis-mcp bridge): kind "running" ("while a turn
      started by a message from chat ‹S› runs") when a relayed turn is
      running now — it may be carrying that chat's words to the child — else
      nothing."""
    if child is None:
        return _turn_relay(session)
    stamps = getattr(session, "child_relay", None)
    stamp = stamps.get(child) if child and isinstance(stamps, dict) else None
    if isinstance(stamp, dict):
        return dict(stamp)
    return _turn_relay(session, "running")


def _stamp_child(session, prepared, *, running: bool = True) -> None:
    """Under the daemon's lock, from `_record_subagent` (and `_spawned`): a
    child's first record stamps it with the relay that spawned it, if any.

    - A grandchild whose parent the daemon has seen: a copy of the parent's
      stamp, or none if the parent has none.
    - A top-level child (its lifecycle `started`, no parent): the turn
      running now, when `running` (False between turns, or when the turn
      changed while the record was prepared: no stamp).
    - Anything else — a parent the daemon never saw (its `started` dropped
      past a provider's bound), or a first record that is not the lifecycle
      `started` — no stamp: who spawned it is not known, so it can only get
      the "running" line.

    Never restamped: a sid once seen stays seen for the session's life."""
    sid = prepared.get("sid")
    seen, stamps = session.child_seen, session.child_relay
    if not sid or sid in seen:
        return
    seen[sid] = True
    data = prepared.get("data") or {}
    lifecycle = prepared.get("kind") == "subagent"
    parent = data.get("parent_sid") if lifecycle else None
    if parent and parent in stamps:
        stamp = dict(stamps[parent])
    elif lifecycle and not parent and data.get("op") == "started":
        stamp = _turn_relay(session) if running else None
    else:
        stamp = None                      # an unstamped or unknown parent; not a start
    if stamp is not None:
        stamps[sid] = stamp
        while len(stamps) > CHILD_STAMPS:
            stamps.pop(next(iter(stamps)))


def _spawned(session, data, *, running: bool) -> None:
    """Under the daemon's lock, from `_record`: the orchestrator's own Agent
    call in a Claude turn names the child it spawns ("cl:" and the call's
    id, `providers.claude.child_sid`). Stamped here, from the turn that made
    the call, so a background child whose `task_started` the CLI emits only
    after the turn's result (read between turns, which stamps nothing) still
    carries its relay. A call that spawns nothing leaves one unused sid."""
    if not isinstance(data, dict) or data.get("name") not in CLAUDE_SPAWN_TOOLS:
        return
    call_id = data.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        return
    from .providers.claude import child_sid
    _stamp_child(session, {"kind": "subagent", "sid": child_sid(call_id),
                           "data": {"op": "started", "parent_sid": None}}, running=running)


def _log_fields(data) -> dict:
    """`LOGGED_REDACTED` fields of an event's data through
    `secrets.redact_value`, computed outside the daemon's lock (it reads
    credential files). A redaction that fails withholds the field."""
    out = {}
    if not isinstance(data, dict):
        return out
    from jarvis.tools import secrets as v1_secrets
    for key in LOGGED_REDACTED:
        if key in data:
            try:
                out[key] = v1_secrets.redact_value(data[key])
            except Exception:  # noqa: BLE001
                out[key] = "[withheld: could not be judged]"
    return out


def _logged(record: dict, original, fields: dict) -> dict:
    """The record as `log.jsonl` keeps it: each field still holding the
    event's own value replaced by its redacted copy (a field the daemon
    already replaced, a Browser action's empty args, stays)."""
    data = record.get("data")
    if not fields or not isinstance(data, dict) or not isinstance(original, dict):
        return record
    kept = dict(data)
    for key, value in fields.items():
        if key in kept and kept[key] == original.get(key):
            kept[key] = value
    return {**record, "data": kept}


GATE_KINDS = {EventKind.APPROVAL_REQUESTED: "gate_requested",
              EventKind.APPROVAL_RESOLVED: "gate_resolved"}
_TASK_VERBS = ("start", "steer", "cancel", "resume", "answer")
# `PATCH /projects` fields only the owner may change, from the HUD (BR-0).
_OWNER_PROJECT_FIELDS = ("profile", "always_ask", "extra_dirs", "root")

# Owner messages that wait on a running chat turn (2026-10-08, steering). One
# queue and one cap for every surface: the HUD's send and a message typed in
# the chat's Discord thread land in the same place (the mirror's O-C6 queue
# moved here). A steered message is delivered, not waiting, so it never counts.
QUEUE_MAX = 3
QUEUE_FULL = ("Three messages are already waiting on this turn; send this one again "
              "once I've answered.")
# What a provider that cannot steer is told when its turn is interrupted for
# the owner's message (`SteerRefused(fallback="interrupt")`). The owner's own
# words stay as they were everywhere they are shown; only the model sees this.
INTERRUPTED_NOTE = ("[The owner interrupted your previous turn to send this message. "
                    "Whatever that turn was doing stopped where it was.]\n")
# Between turns (SV-6, docs/plans/2026-10-13-subagent-view.md "SV-6 design").
# A provider with `watch(h)` is read between turns by a watcher thread per
# session: a sub-agent's progress and end go to its record at once, and a turn
# the CLI starts by itself (Claude, when a background task finishes) becomes a
# turn of the thread (`_begin_cli`), opened by a `cli_turn` line. Every record
# of that turn carries `self_started: true`, and the Discord mirror, speech
# and the task runner leave such a turn alone (the owner's decision,
# 2026-10-15). WATCH_JOIN_S bounds the wait for a watcher when its session
# ends.
CLI_TURN_TEXT = "A background task finished, and Claude Code carries on by itself."
WATCH_JOIN_S = 2.0
# The owner's Compact button (2026-10-19, docs/plans/2026-10-19-compact-button-
# plan.md): the provider's own compaction of a chat's conversation, as a turn
# with no message, started only by the owner's HUD route
# (`POST /threads/{id}/compact`). What `Daemon.compact` refuses with (409),
# and the system line each compaction ends with (`compacted`).
COMPACT_NOT_A_CHAT = "Tasks are not chats; only a chat compacts."
BYPASS_NOT_A_CHAT = "Only an owner's chat can bypass the classifier; a task's workers keep it."
BYPASS_ON = ("Bypass on: Claude Code's auto-mode classifier no longer judges this chat's "
             "commands and file edits in turns you send. Jarvis's own rules and cards still apply.")
BYPASS_OFF = "Bypass off: the auto-mode classifier judges this chat's calls again."
BYPASS_UNSUPPORTED = ("Only Claude chats have an auto-mode classifier to bypass: "
                      "OpenRouter and Codex chats already bring every call Jarvis does not "
                      "settle to you.")
COMPACT_RUNNING = "A turn is running; compact when it ends."
COMPACT_WAITING = "Messages are waiting to run first."
COMPACT_CLI = "Claude Code started a turn of its own; compact when it ends."
COMPACT_CARD = "Answer the card first."
COMPACT_UNSUPPORTED = "This chat's provider cannot compact its conversation."
COMPACT_OK = "Conversation compacted"
COMPACT_STOPPED = "Compaction stopped"
COMPACT_FAILED = "Compaction failed: "
COMPACT_NOT_DONE = "Not compacted: "
COMPACT_REASON_CHARS = 200


def compact_reason(text) -> str:
    """A provider's reason for a compaction that did not happen, as the chat
    shows it: scrubbed as chat reads are (`chat_reads.scrub`: the display
    redaction, then the credential scrub), one line, at most
    COMPACT_REASON_CHARS. Reads credential files: never under `_lock`."""
    from .approvals import clean_line
    from .chat_reads import WITHHELD, scrub
    try:
        line = clean_line(scrub(str(text or "")), COMPACT_REASON_CHARS)
    except Exception:  # noqa: BLE001 — a scrub that fails shows nothing of it
        line = WITHHELD
    return line or "no reason was given"


class DaemonError(RuntimeError):
    """Lifecycle conflict (HTTP 409)."""


# The owner's Stop on one native sub-agent (SV-4): its refusals, one sentence each.
SUBAGENT_NO_SESSION = "This chat's session is not open, so nothing it ran is still running."
SUBAGENT_STOP_UNSUPPORTED = "This chat's provider cannot stop one sub-agent by itself."


class SubagentStopRefused(DaemonError):
    """The owner's Stop on a sub-agent was not sent (409): `stale` when it is
    no longer running, `supported` False when its CLI cannot stop it alone,
    `unconfirmed` when it went out but the CLI did not say it took it,
    `refused` when the CLI answered it with a refusal of its own."""

    def __init__(self, message: str, *, stale: bool = False, supported: bool = True,
                 unconfirmed: bool = False, refused: bool = False):
        super().__init__(message)
        self.stale, self.supported, self.unconfirmed = stale, supported, unconfirmed
        self.refused = refused

    def body(self) -> dict:
        out: dict = {"error": str(self)}
        if self.stale:
            out["stale"] = True
        if not self.supported:
            out["supported"] = False
            out["reason"] = str(self)
        if self.unconfirmed:
            out["unconfirmed"] = True
        if self.refused:
            out["refused"] = True
        return out


class _StopPending(DaemonError):
    """A provider is still receiving the stop for this thread."""


class QueueFull(DaemonError):
    """Three messages already wait on this thread's running turn."""


class APIError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def is_running(port: int) -> bool:
    """Probe only the given loopback port, never a caller's default port."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
    try:
        conn.request("GET", "/status")
        response = conn.getresponse()
        return response.status == 200 and isinstance(json.loads(response.read()), dict)
    except Exception:
        return False
    finally:
        conn.close()


# "inbox": the owner's answer to a decision, delivered by the owner inbox
# (inbox.py, B1) through `deliver_queued`: never at the desk
# (`desk.desk_message` requires "hud"). "phone": the same answer given on the
# phone (phone design P-4, `remote_answer`): the same queue and framing, and
# no desk authority either.
VIAS = ("hud", "discord", "dm", "system", "peer", "inbox", "phone")


def user_data(message: UserMessage) -> dict:
    """The `user` log record's data and the `user_message` event's (PR C).

    `text` is what the provider received (with any file inlined); `typed` is
    the owner's own words, which is all the Discord mirror ever shows.
    `attachments` are names, never contents; `images` is a count."""
    via = message.via or ("hud" if message.origin == "owner" else "system")
    if via not in VIAS:
        raise APIError(400, "via must be one of " + ", ".join(VIAS))
    data = {"text": message.text,
            "typed": message.typed if message.typed is not None else message.text,
            "via": via, "origin": message.origin, "images": len(message.images),
            "attachments": [str(name)[:200] for name in message.attachments][:8],
            "spoken": bool(message.spoken),
            "discord_message_id": message.discord_message_id,
            "discord_channel_id": message.discord_channel_id}
    if message.skill:
        data["skill"] = message.skill
    return data


def safe_list(store, **filters):
    """Keep the strict store contract; isolate corrupt saved records here."""
    try:
        return store.list(**filters)
    except StoreError:
        unknown = filters.keys() - {f.name for f in fields(store.cls)}
        if unknown:
            raise
        result = []
        pattern = f"*/{store.filename}" if store.filename else "*.json"
        for path in sorted(store.root.glob(pattern)):
            try:
                obj = store.get(path.parent.name if store.filename else path.stem)
                if obj is not None and all(getattr(obj, k) == v for k, v in filters.items()):
                    result.append(obj)
            except StoreError as exc:
                LOG.warning("Skipping corrupt record %s: %s", path, exc)
        return result


@dataclass
class _Queued:
    """An owner message that reached a running chat turn: steered into it,
    or waiting to run as a turn of its own. `turn_id` is the turn it belongs
    to — the running one for a steer, the one minted for it for a queued
    message — so its log record and its turn's events share an id."""
    message: UserMessage
    data: dict
    message_id: str
    turn_id: str
    interrupting: bool = False


@dataclass
class _Session:
    thread: Thread
    brief: Brief
    provider: Provider
    handle: SessionHandle | None = None
    worker: threading.Thread | None = None
    # When the running turn began (ISO, with `worker`): the HUD's thinking
    # line counts from it after a reload (WP-H, H-b). Live, never stored.
    running_since: str | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)
    closing: bool = False
    retired: bool = False
    turn_id: str | None = None
    token_baseline: int = 0
    cost_baseline: float = 0.0
    # Chat threads only: the (model, effort) the provider is running now, and
    # the last one the transcript announced. A turn starts by comparing the
    # thread's effective choice with `applied` (thread_model, decisions A1/A7).
    applied: tuple | None = None
    announced: tuple | None = None
    # The thread record's (model, effort) when `applied` was set: what a
    # refused switch rolls the record back to.
    applied_record: tuple | None = None
    # The provider closed this session under us (a switch that could not even
    # reconnect the old model); it is dropped when its turn ends.
    lost: bool = False
    # Steering (2026-10-08). The owner pressed stop on the running turn (an
    # interrupt the daemon makes for a provider that cannot steer is not one);
    # the owner messages steered into it, for the provider to hand back any it
    # never delivered; and the provider questions it has open, which a steer
    # must never be mistaken for the answer to.
    owner_stopped: bool = False
    steered: list = field(default_factory=list)
    questions: set = field(default_factory=set)
    # What each open question said ({text, options, asked_at}), for the
    # owner inbox's row (inbox.py). `questions` stays the one truth for which
    # are open: an entry here whose id has left it is never shown, and is
    # pruned the next time a question is asked.
    question_detail: dict = field(default_factory=dict, repr=False)
    # The turns the owner stopped, newest last: a steer still in the
    # provider's hands when Stop landed is judged by the turn it was aimed at,
    # even after a later turn has started (Bugbot on PR #22).
    stopped_turns: deque = field(default_factory=lambda: deque(maxlen=8))
    # The §6 permit this session's provider was handed: the jarvis-mcp bridge
    # asks it about a dangerous Jarvis tool (`dangerous_tool=True`).
    permit: Callable | None = None
    # The digest of this session's jarvis-mcp caller token, while one is live
    # (`callers.CallerTokens`); never the token itself.
    caller_key: str | None = None
    origin_tag: str | None = field(default=None, repr=False)
    desk: DeskState = field(default_factory=DeskState, repr=False)
    drive_turn: object = field(default=None, repr=False)
    # Which session ran a native sub-agent (SV-1, subagents.py): when this
    # one ends, so do its children's CLI, and the record says so.
    subagent_key: str = field(default_factory=lambda: uuid.uuid4().hex)
    # C6: the relay that spawned each native child, by sid, and every sid
    # seen (`_stamp_child`).
    child_relay: dict = field(default_factory=dict, repr=False)
    child_seen: dict = field(default_factory=dict, repr=False)
    # Between turns (SV-6): the thread reading the provider's `watch`, and
    # whether the running turn is one the CLI started by itself (`_begin_cli`).
    watcher: threading.Thread | None = field(default=None, repr=False)
    self_started: bool = False
    # The watcher has passed on the provider's word that the CLI started a
    # turn (after every event it read before it), so a turn that ends may
    # take it up in that order (`_maybe_cli`).
    cli_offered: bool = False
    # Chat-to-chat (chat_relay.py, plan §5): the relay item this turn runs
    # for — a message from another chat, or that chat's reply coming back —
    # and the `relayed` field stamped on every record of it; None for any
    # other turn. Such a turn is never at the desk and never sends (no
    # chains). `relay_steered`: replies steered into the running turn, for
    # the provider to hand back any it never took (never the owner's queue).
    relay_turn: object = field(default=None, repr=False)
    relay_mark: dict | None = field(default=None, repr=False)
    relay_steered: list = field(default_factory=list, repr=False)
    # The running turn is the owner's Compact button (`compact`): its
    # records say so, it is not counted as a turn, and nothing steers into
    # it (owner messages and chat replies wait). Cleared with `worker`.
    compacting: bool = False


# `Daemon(remote_host=...)` not given: read `config.remote_host()`.
_FROM_CONFIG = object()


class Daemon:
    def __init__(self, stores: Stores, providers: dict[ProviderName, Provider],
                 permit_factory: Callable[[str, Brief], PermissionCallback] | None,
                 port: int, approvals: PendingApprovals | None = None, *,
                 face_port: int | None = None, workshop_port: int | None = None,
                 remote_port: int | None = None, remote_host=_FROM_CONFIG,
                 dev_proc_root=None, dev_cgroup_root=None):
        self.stores = stores
        self.providers = {ProviderName(k): v for k, v in providers.items()}
        self.port = port
        self.bus = EventBus()
        # The sidebar's per-thread and per-task status (activity.py), fed by
        # every record this bus carries.
        from .activity import Activity
        self.activity = Activity(stores, self.bus.publish)
        self.bus.observe(self.activity.observe)
        # The owner inbox (inbox.py): a projection over the broker, the
        # sessions and the task store. Its observer only publishes
        # `inbox_changed` ({ids, reason}) after the source events it reads.
        from .inbox import Inbox
        self.inbox = Inbox(self)
        self.bus.observe(self.inbox.observe)
        # The daemon owns the broker (design §6 layer 5). Its two callbacks
        # publish on the same bus every surface already reads, so a HUD, a
        # Discord thread and the escape hatch all learn about a pending
        # approval the same way they learn about a tool call — and the
        # *entire* command travels with it, never a summary.
        self.approvals = approvals if approvals is not None else PendingApprovals(
            on_request=self._approval_requested, on_resolve=self._approval_resolved)
        # `permit_factory=None` means the WP5 policy over this daemon's broker.
        # An injected factory (the tests, a future strict-profile runner) wins,
        # so nothing here decides policy on its own.
        self.permit_factory = permit_factory if permit_factory is not None else self._permit
        # WP11 attaches its TaskRunner here (control.TaskControl). None means
        # this daemon serves state only and the /tasks verbs answer 409, which
        # is what every pre-WP11 test and every embedded use already expects.
        self.runner = None
        # PR A attaches its DiscordSurface here (`start_discord`); `GET /discord`
        # reads it. None means no Discord on this daemon.
        self.discord = None
        self.discord_error: str | None = None   # why start_discord failed (class name)
        # Chat titles (titles.py): attached and started by daemon2's `main()`,
        # never by `start()`, so no test or embedded daemon calls a model.
        # Stopped here by `stop()`.
        self.titles = None
        # The Diff tab's quick reviews (diff_review.py): made by the first
        # owner request for one (`diff_review.for_daemon`), never at start-up,
        # so nothing calls a model until the owner clicks. Cancelled by `stop()`.
        self.diff_reviews = None
        self._lock = threading.RLock()
        self._worktree_lock = threading.Lock()
        self._sessions: dict[str, _Session] = {}
        self._interrupting: set[str] = set()  # thread identity survives session replacement
        self._dev_hook_errors = set()
        self._dev_streams = {}
        # thread id -> owner messages waiting on its running turn (QUEUE_MAX).
        # Per thread rather than per session: a turn that ends by dropping its
        # session (a fatal error) must not take what waits behind it along.
        self._queues: dict[str, deque] = {}
        # thread id -> the owner's answers to decisions waiting to run there
        # (inbox B1, `deliver_queued`): after the owner's queue, never in it.
        self._inbox_queues: dict[str, deque] = {}
        self._stopping = False
        # Set once shutdown starts releasing the broker's waiters
        # (`release_approvals`): a "deny" that arrives after it is Jarvis
        # stopping, not the owner answering (the Discord linker keeps its ask).
        self.approvals_released = threading.Event()
        self._server = None
        self._server_thread = None
        # Port zero is an isolated embedded/test daemon on three ephemeral ports.
        self.face_port = face_port if face_port is not None else (0 if port == 0 else config.FACE_PORT)
        self.workshop_port = workshop_port if workshop_port is not None else (0 if port == 0 else config.WORKSHOP_PORT)
        # The phone's remote listener (PH-1, remote_listener.py): bound only
        # when a tailnet host is configured (`config.remote_host()`, read
        # here), never part of the single-instance lock, and skipped with a
        # log line if its port is taken. `remote_port` is the bound port
        # after `start()`, or None when there is no remote listener.
        self.remote_host = config.remote_host() if remote_host is _FROM_CONFIG else remote_host
        self._remote_want = remote_port if remote_port is not None else (0 if port == 0 else config.REMOTE_PORT)
        self.remote_port: int | None = None
        # Who the owner is on that listener (P-2, remote_auth.py): passkeys,
        # the pairing window and phone sessions, under `<data root>/remote`.
        # Made here, never touching disk until a phone or the HUD asks.
        from .remote_auth import RemoteAuth
        self.remote_auth = RemoteAuth(Path(stores.root) / "remote", self.remote_host)
        # PH-10: while a phone session was active in the last 24 h, the
        # broker waits the remote 600 s (`PendingApprovals.wait_s`).
        if getattr(self.approvals, "phone_active", False) is None:
            self.approvals.phone_active = self.remote_auth.recently_active
        # §4.1, "the network stays secured" (P-4, remote_secure.py): the
        # checks that give the phone HUD authority. Its Windows and
        # Tailscale reads are attached only by `main()`
        # (`attach_system`), so no embedded or test daemon runs them.
        from .remote_secure import SecureMonitor
        self.secure = SecureMonitor(self)
        self._listeners = []
        from .hud_api import HUDLedger
        from .router import Router
        from .schedules import Schedules
        self.router = Router(stores, self.providers, ledger=HUDLedger(stores))
        self.schedules = Schedules(self)
        # Where a permanent delete goes (decisions B2). Only Jarvis's own
        # entries in it are ever purged: `owned` is this daemon's data root.
        from .trash import Trash
        self.trash = Trash(stores.root)
        # The Browser's switches (`browser.json`, browser/settings.py): in
        # memory here; daemon2's `main()` attaches the owner's file. While
        # `auto_open` is on, a terminal's live output is read for a dev
        # server's URL (BR-8a, devurl.py), told only to that terminal's own
        # socket — never this bus, a log line or the disk.
        from .browser.settings import BrowserSettings
        self.browser_settings = BrowserSettings()
        from .browser.captures import Captures
        self.browser_captures = Captures()
        # The owner's Direct capture probe (B-DIRECT-CAPTURE): its pass is held
        # in memory only; main() attaches the report file the lead reads.
        from .browser.direct_probe import DirectProbe
        self.browser_direct_probe = DirectProbe()
        # The owner's HUD terminals (WP-C, terminals.py). Owner-only routes on
        # the HUD listener; they end with the daemon. On the bus they are one
        # lifecycle record, `terminal_attached` (an id and a time), never output.
        from .terminals import Terminals
        self.terminals = Terminals(publish=self.bus.publish,
                                   auto_open=lambda: self.devservers.enabled(),
                                   own_ports=lambda: (self.port, self.face_port, self.workshop_port),
                                   dev_announce=lambda *a, **k: self.devservers.announce_terminal(*a, **k),
                                   dev_release=lambda *a: self.devservers.terminal_delivery_failed(*a),
                                   dev_send=lambda *a: self.devservers.deliver_terminal(*a))
        # VEX V5 (VX-1, vex.py): its builds, downloads and brain terminal run
        # as program terminals above; owner-only routes; the bus carries only
        # an id-only `vex_device` record per device action.
        from .vex import Vex
        self.vex = Vex(self)
        # The owner's built-in Browser (BR-1a, browser/host.py): a
        # daemon-owned Chrome behind a SOCKS gate, started lazily by the first
        # owner-only `/browser` request that needs it, never by `start()`.
        # On the bus and in the log it is nothing but classes and counts.
        from .browser.host import BrowserHost
        self.browser = BrowserHost(self)
        # Jarvis's speech lowers the Remote Browser's sound under a short HUD
        # lease (V15, speech_duck.py). Nothing starts until the first lease;
        # `recover()` at start only reads the journal of a crashed run.
        from .speech_duck import Ducker
        self.speech_ducker = Ducker(self.browser.duck_owners, journal=self._duck_journal)
        from .browser.look import BrowserLook
        self.browser_look = BrowserLook(self)
        # The chat-to-chat service (chat_relay.py): what the chat tools read
        # through. Installed in `start()`, removed in `stop()`; no route.
        from .chat_relay import ChatRelay
        self.chat_relay = ChatRelay(self)
        # What `owner_post` reaches (decisions.py, inbox B1): the post half of
        # the inbox's decisions, over the inbox's own store. Installed in
        # `start()`, removed in `stop()`; it holds no answer path.
        from .decisions import OwnerPosts
        self.owner_posts = OwnerPosts(self, self.inbox.decisions)
        from .browser.drive import BrowserDrive
        self.browser_drive = BrowserDrive(self)
        from .devservers import DevServers
        self.devservers = DevServers(self, proc_root=dev_proc_root, cgroup_root=dev_cgroup_root)
        from .browser.panes import BrowserPanes
        self.browser_panes = BrowserPanes(self)
        # Per-session jarvis-mcp caller tokens (callers.py, plan 2026-10-10
        # §1): minted in `_run_brief` for an owner's Claude or Codex chat that
        # is offered session tools, held only as digests, revoked whenever
        # the session ends. Memory only: a restart forgets every one.
        from .callers import CallerTokens
        self.callers = CallerTokens()
        # Native sub-agents (SV-1, subagents.py): their own record, never the
        # thread's log; on the bus only ids, statuses and times.
        from .subagents import SubagentRecords
        def live_key(thread_id):
            # Lock-free on purpose: called under the record's own lock, and a
            # dict read is atomic.
            session = self._sessions.get(thread_id)
            return session.subagent_key if session is not None and not session.closing else None
        self.subagents = SubagentRecords(stores, self.bus.publish, live_key)
        self.started_at = time.monotonic()

    def start(self) -> None:
        with self._lock:
            if self._stopping or self._server is not None:
                raise DaemonError("daemon already started or stopped")
            try:
                server = ThreadingHTTPServer(("127.0.0.1", self.port), _handler(self))
            except OSError as exc:
                if exc.errno != errno.EADDRINUSE:
                    raise
                reason = "another daemon is already running" if is_running(self.port) else "port is already in use"
                raise DaemonError(f"{reason} on port {self.port}") from exc
            servers = [server]
            try:
                # Use the identical handler class on both API listeners. The
                # third server is marked preview-only before accepting requests.
                face = ThreadingHTTPServer(("127.0.0.1", self.face_port), server.RequestHandlerClass)
                servers.append(face)
                preview = ThreadingHTTPServer(("127.0.0.1", self.workshop_port), server.RequestHandlerClass)
                preview.preview_only = True
                servers.append(preview)
            except OSError as exc:
                for listener in servers:
                    listener.server_close()
                raise DaemonError("HUD or preview port is already in use") from exc
            self._server = server
            self.port, self.face_port, self.workshop_port = [s.server_address[1] for s in servers]
            # The fourth listener, after the three that are the lock: a second
            # daemon fails above and never reaches it. Never fails the start.
            from .remote_listener import bind as bind_remote
            remote = bind_remote(self.remote_host, self._remote_want, self.remote_auth, self)
            if remote is not None:
                servers.append(remote)
                self.remote_port = remote.server_address[1]
            # Only now, holding all three ports (the bind is the
            # single-instance lock) and before anything is served: a second
            # daemon started by hand on the live data folder fails the bind
            # above and never reaches this, so it cannot delete the live
            # daemon's carriers (PR #35 review).
            self._sweep_carriers()
            # Before any session opens (a session saves its own copy of a
            # thread): Claude thread costs counted before 2026-10-12 summed
            # the CLI's running totals. Once per thread, from its own log.
            running_cost.recount_threads(self.stores, ProviderName.CLAUDE, log=LOG)
            self.started_at = time.monotonic()
            for listener in servers:
                listener.daemon_threads = True
                worker = threading.Thread(target=lambda s=listener: s.serve_forever(0.05),
                                          name="jarvis-v2-http", daemon=True)
                self._listeners.append((listener, worker))
                worker.start()
            self._server_thread = self._listeners[0][1]
            # §4.1's checks: at start-up and every 5 minutes, only while the
            # phone's listener is bound (remote_secure.py).
            if self.remote_port is not None:
                self.secure.start()
            from .hud_api import connect_controls
            connect_controls(self)
            # `terminal_read` (WP-F) reads these terminals and no others.
            self.terminals.install()
            self.browser_look.install()
            self.browser_drive.install()
            self.chat_relay.install()
            self.owner_posts.install()
            # An answer still "queued" from a previous run never ran (the
            # queue is in memory): say so on its decision (inbox B1).
            self.inbox.fail_lost_answers()
            # Open PRs for the inbox (B3): a thread of its own, only once
            # `main()` attached it; nothing here waits on `gh`.
            self.inbox.prs.start()
            self.devservers.start()
            self.schedules.start()
            # A previous run's unrestored duck (V15) is repaired from its
            # journal, under the same "this is the owner's daemon" rule.
            if self._owns_data():
                self.speech_ducker.recover()

    def _duck_journal(self):
        """The duck's journal is the owner's daemon's alone: a test or embedded daemon
        on a folder of its own never reads or writes the owner's file."""
        return config.SPEECH_DUCK_PATH if self._owns_data() else None

    def _owns_data(self) -> bool:
        try:
            return Path(self.stores.root).resolve() == Path(config.V2_DATA_DIR).resolve()
        except OSError:
            return False

    def _sweep_carriers(self) -> None:
        """Before anything is served: remove jarvis-mcp token carriers a crash
        left behind (plan §1.3) — their tokens died with that process. Only
        under the data folder this daemon owns: a test or embedded daemon on a
        folder of its own never touches the owner's live one."""
        from .callers import sweep_carriers
        try:
            owned = Path(self.stores.root).resolve() == Path(config.V2_DATA_DIR).resolve()
        except OSError:
            owned = False
        if owned:
            sweep_carriers(Path(config.V2_DATA_DIR))

    def status(self) -> dict:
        dev_watch = self.devservers.status()
        providers = {}
        for name, provider in self.providers.items():
            try:
                ok, reason = provider.health()
            except Exception as exc:
                ok, reason = False, f"{type(exc).__name__}: {exc}"
            providers[name.value] = {"ok": bool(ok), "reason": str(reason)}
            cli = _cli_status(provider)
            if cli is not None:
                providers[name.value]["cli"] = cli
        from .hud_api import hud_served
        from .remote_auth import remote_status
        try:
            hud = hud_served()
        except Exception:
            hud = {"build": None, "git_head": None, "state": "failed verification"}
        # Broker approvals waiting on the owner — a turn's, and the escape
        # hatch's, which asks after its turn has finished. `jarvis restart`
        # refuses while any wait (a restart denies them). Read before
        # `self._lock`: the broker has a lock of its own.
        try:
            approvals_pending = len(self.approvals.pending())
        except Exception:
            approvals_pending = 0
        # `/status` is how `jarvis restart` checks the daemon: the Browser
        # can never make it fail. Without the host's view, the refused ports
        # are still every control port and the workshop's default.
        try:
            browser = self.browser.status()
            refused_ports = sorted(self.browser.refused_ports())
        except Exception as exc:
            from .browser.policy import refused_ports as _refused_ports
            browser = {"available": False, "running": False, "error": type(exc).__name__}
            refused_ports = sorted(_refused_ports())
        with self._lock:
            return {"jarvis-daemon": True, "version": 2,
                    "uptime": time.monotonic() - self.started_at, "providers": providers,
                    "threads_open": sum(s.handle is not None for s in self._sessions.values()),
                    "turns_running": sum(s.worker is not None for s in self._sessions.values()),
                    "approvals_pending": approvals_pending,
                    # This process: `jarvis restart` checks it is systemd's
                    # MainPID, so another daemon answering on the shell's
                    # port cannot stand in for the unit's.
                    "pid": os.getpid(),
                    # The HUD builds preview URLs from this rather than a
                    # hard-coded 8403: a HUD served by any other daemon (a
                    # test's, on an ephemeral port) must never reach the
                    # owner's live workshop origin.
                    "workshop_port": self.workshop_port,
                    # The daemon's other two listeners (WP-E): the Preview
                    # pane refuses both, so neither the HUD nor the API is
                    # ever loaded into a frame inside the HUD.
                    "hud_port": self.face_port, "api_port": self.port,
                    # The phone's listener (P-2): its port, whether it bound
                    # and whether a tailnet name is configured. Never the
                    # name, never anything of a passkey or a session.
                    "remote": remote_status(self),
                    # True once every HUD and API response refuses to be
                    # framed (the handler's `end_headers`). The HUD offers a
                    # Preview pane's "keep its own origin" switch only then
                    # (decisions W-4), so a HUD served by an older daemon
                    # never does.
                    "frame_hardened": bool(
                        self._server is not None
                        and getattr(self._server.RequestHandlerClass, "frame_hardened", False)),
                    # Which HUD `GET /` serves (decision C-1, ED-4): the
                    # installed build's id, "dev" under JARVIS_HUD_DEV=1, or
                    # null; the commit it was installed from; and why
                    # nothing is served when nothing is (review of PR #44).
                    "hud_build": hud["build"], "hud_git_head": hud.get("git_head"),
                    "hud_state": hud["state"],
                    # The newer builds passed over because they failed
                    # verification ("fallback"), newest first.
                    "hud_passed_over": hud.get("passed_over", []),
                    # The Browser (BR-1a): whether Chrome is there and
                    # running, its tabs by count, the gate's counters —
                    # never a URL, a title or a profile's label.
                    "browser": browser,
                    # Every port the Browser's gate refuses on this machine
                    # (`browser.policy.refused_ports`): BR-0's control ports,
                    # this daemon's HUD and API ports, and 8403 unless it is
                    # this daemon's workshop. The HUD's Direct judge reads it.
                    "refused_ports": refused_ports, "dev_watch": dev_watch}

    def _dev_settings_changed(self):
        # A fixed preference fact, never a server URL or a Remote navigation.
        # Publish before lapsing claims so windows invalidate queued work first.
        self.bus.publish({"kind": "dev_server_settings",
                          "data": {"auto_open": self.browser_settings.auto_open()}})
        self.devservers.settings_changed()

    def _dev_known(self):
        """Known process identities, never a command-line scan.

        SDK internals are best effort; an unavailable root is still untagged
        unless it is the agent, and the seven service ports are always refused.
        Called without taking the daemon lock: turn start already holds it.
        """
        pids = {os.getpid()}
        leaders = set()
        for terminal in list(self.terminals._terminals.values()):
            proc = terminal._proc
            if proc is not None and proc.poll() is None:
                leaders.add((proc.pid, terminal._start_time))
        for session in list(self._sessions.values()):
            native = getattr(session.handle, "native", None)
            rpc = getattr(native, "rpc", None)
            proc = getattr(rpc, "process", None)
            if proc is None:
                client = getattr(native, "client", None)
                transport = (getattr(client, "_transport", None)
                             or getattr(getattr(client, "_query", None), "transport", None))
                proc = getattr(transport, "_process", None)
            pid = getattr(proc, "pid", None)
            if isinstance(pid, int):
                pids.add(pid)
        pids.update(self._dev_browser_roots())
        gate = getattr(self.browser, "_gate", None)
        return pids, leaders, getattr(gate, "port", None)

    def _dev_hook(self, name, *args):
        """Watcher faults must not interrupt a turn or provider cleanup."""
        try:
            getattr(self.devservers, name)(*args)
        except Exception as exc:
            kind = type(exc).__name__
            with self._lock:
                if type(exc) not in self._dev_hook_errors:
                    self._dev_hook_errors.add(type(exc))
                    LOG.warning("Dev watcher hook failed (%s)", kind)
            try:
                self.devservers.note_hook_error(kind)
            except Exception:
                pass

    def _dev_stream_started(self, window):
        with self._lock:
            self._dev_streams[window] = self._dev_streams.get(window, 0) + 1
            self.browser_panes.window_started(window)

    def _dev_stream_ended(self, window):
        with self._lock:
            self.browser_panes.window_ended(window)
            remaining = self._dev_streams[window] - 1
            if remaining:
                self._dev_streams[window] = remaining
            else:
                del self._dev_streams[window]
                self._dev_hook("window_ended", window)

    def _dev_browser_roots(self):
        # Playwright's driver owns its Chrome tree. The watcher recognizes
        # descendants from stat facts, without Chrome's cmdline-based helper.
        pw = getattr(self.browser, "_pw", None)
        impl = getattr(pw, "_impl_obj", None)
        connection = getattr(impl, "_connection", None) or getattr(pw, "_connection", None)
        transport = getattr(connection, "_transport", None)
        pid = getattr(getattr(transport, "_proc", None), "pid", None)
        return {pid} if isinstance(pid, int) else set()

    def require(self, store, object_id):
        if not isinstance(object_id, str) or not re.fullmatch(r"[0-9a-f]{8}", object_id):
            raise APIError(400, "id must be eight lowercase hex characters")
        obj = store.get(object_id)
        if obj is None:
            raise APIError(404, f"{store.cls.__name__} {object_id} not found")
        return obj

    def _active(self):
        if self._stopping:
            raise DaemonError("daemon is stopping")

    # Strictness, loosest first: a caller may ask for a stricter profile than
    # its project's, never a looser one.
    _STRICTNESS = (PermissionProfile.AUTO, PermissionProfile.ASK, PermissionProfile.STRICT)

    def make_brief(self, project, role, value):
        """A thread's brief. A `Brief` comes from the runner, which built it
        from the project and task itself. A dict comes from a caller over the
        API, so the fields that bound what the thread may do are the
        project's: `cwd` is the project's folder, `profile` and `always_ask`
        are the project's or stricter, and there are no MCP servers (a project
        configures none). A caller brief that would loosen any of them is
        refused, not narrowed — a Claude or Codex chat thread is a full agent
        (decisions A1), and these fields are its fence."""
        if isinstance(value, Brief):
            brief = value
        else:
            if not isinstance(value, dict):
                raise APIError(400, "brief must be an object")
            brief = from_json(Brief, {"cwd": project.root, "profile": project.profile,
                                      "always_ask": project.always_ask, "role": role, **value})
            self._within_project(project, brief)
        _validate(brief, Brief)
        if brief.role != role or not Path(brief.cwd).is_absolute():
            raise APIError(400, "brief role must match thread role and cwd must be absolute")
        return brief

    def _within_project(self, project, brief):
        loosened = []
        if not isinstance(brief.cwd, str) or os.path.normpath(brief.cwd) != os.path.normpath(project.root):
            loosened.append(f"cwd (the project's folder is {project.root})")
        project_profile = PermissionProfile(project.profile)
        try:
            looser = (self._STRICTNESS.index(PermissionProfile(brief.profile))
                      < self._STRICTNESS.index(project_profile))
        except ValueError:
            looser = True
        if looser:
            loosened.append(f"profile (the project's is {project_profile.value})")
        if not isinstance(brief.always_ask, list) or set(project.always_ask) - set(brief.always_ask):
            loosened.append("always_ask (the project's commands must all stay)")
        if brief.mcp_servers:
            loosened.append("mcp_servers (the project configures none)")
        if loosened:
            raise APIError(400, "a brief cannot loosen the project's " + ", ".join(loosened))

    def open_thread(self, project_id, role, provider, brief) -> Thread:
        project = self.require(self.stores.projects, project_id)
        if project.archived:
            raise DaemonError(f"project {project.name} is archived; restore it from the Archive first")
        role, provider = Role(role), ProviderName(provider)
        brief = self.make_brief(project, role, brief)
        if brief.task_id is not None:
            task = self.require(self.stores.tasks, brief.task_id)
            if task.project_id != project_id:
                raise APIError(400, "brief task belongs to another project")
        if provider not in self.providers:
            raise DaemonError(f"provider {provider.value} unavailable: not in roster")
        if role == Role.CHAT and brief.task_id is None:
            brief = self._chat_brief(provider, brief)
        with self._lock:
            self._active()
            # A Claude thread opened now counts each result's own cost from
            # its first turn: born marked, the start-up recount never reads it.
            recounted = running_cost.RECOUNTED if provider == ProviderName.CLAUDE else 0
            thread = self.stores.threads.create(project_id, role, provider,
                                               model=brief.model, effort=brief.effort, task_id=brief.task_id,
                                               cwd=brief.cwd, cost_recount=recounted)
        try:
            self._open(thread, brief)
        except BaseException:
            # A thread whose provider never started has no conversation to
            # keep: leave no record behind for a surface to list.
            with self._lock:
                if thread.id not in self._sessions:
                    self.stores.threads._delete(thread.id)
            raise
        self._lifecycle("thread_opened", thread)
        return thread

    def _open(self, thread, brief):
        if brief.origin_tag is not None:
            raise BriefRefused("an origin tag belongs only to a session run brief")
        with self._lock:
            self._active()
            if thread.id in self._interrupting:
                raise _StopPending("the previous stop is still being delivered; try again shortly")
            if thread.id in self._sessions:
                raise DaemonError("thread session is already open or opening")
            provider = self.providers.get(thread.provider)
            if provider is None:
                raise DaemonError(f"provider {thread.provider.value} unavailable: not in roster")
            session = _Session(thread, brief, provider)
            self._sessions[thread.id] = session
        try:
            self._refuse_jarvis_server(thread, brief)
            # The saved brief is never rewritten; a chat thread's provider is
            # handed a copy carrying the thread's current choice instead, so a
            # resume picks up a model changed since open (decisions A1). The
            # permit is built over that same copy: one brief per session.
            # The copy is also the only place a jarvis-mcp caller token is
            # ever written (`_run_brief`): never brief.json.
            run_brief = self._run_brief(session)
            permit = self.permit_factory(thread.id, run_brief)
            session.permit = permit
            method = provider.resume if thread.provider_session_id else provider.start
            handle = method(thread, run_brief, permit)
            session.handle = handle
            usage = provider.usage(handle)
            session.token_baseline = usage.work_tokens
            session.cost_baseline = usage.cost_usd or 0
            with self._lock:
                self._active()
                thread.provider_session_id = handle.provider_session_id
                path = self.stores.threads.path(thread.id).with_name("brief.json")
                if not path.exists():
                    saved = to_json(brief)
                    saved.pop("origin_tag", None)
                    _write_bytes(path, json.dumps(saved, allow_nan=False).encode())
                self.stores.threads.save(thread)
            self._start_watcher(session)
            return session
        except BaseException:
            # A session that never opened keeps no token (plan §1.4).
            self.callers.revoke(session)
            self._dev_hook("session_closed", session.origin_tag)
            if session.handle is not None:
                self._cleanup(session)
            with self._lock:
                self._sessions.pop(thread.id, None)
            raise

    @staticmethod
    def _refuse_jarvis_server(thread, brief) -> None:
        """Only an owner's chat thread may ever have a `jarvis` MCP server,
        and only in the run copy the daemon makes (plan §1.2). A brief that
        names one for anything else — a runner change that copied a chat's
        brief, say — is refused, never run with a server it was not given."""
        from . import thread_model
        if (brief.mcp_servers and "jarvis" in brief.mcp_servers
                and not (thread_model.is_chat(thread) and brief.role == Role.CHAT
                         and brief.task_id is None)):
            raise BriefRefused("only an owner's chat thread may have a jarvis MCP server")

    # -- the model a chat thread runs on (decisions 2026-10-06, part A) ------

    def _chat_brief(self, provider, brief):
        """A chat thread's brief: its model checked before anything is
        created, and on Claude or Codex the chat role's prose. The gate is
        not here — it is the same §6 permit every thread gets in `_open`."""
        from . import roles, thread_model
        why = thread_model.refusal(provider, brief.profile, brief.always_ask)
        if why is not None:
            raise APIError(400, why)
        try:
            model, effort = thread_model.check(provider, brief.model, brief.effort)
        except thread_model.ChoiceRefused as exc:
            raise APIError(400, str(exc)) from exc
        # An effort with no model keeps the thread on the default model (A4
        # amendment): the record holds the effort on its own.
        changes = {"model": model, "effort": effort}
        if provider in (ProviderName.CLAUDE, ProviderName.CODEX) and not brief.system_append:
            changes["system_append"] = roles.brief_for(Role.CHAT).system_append
        return replace(brief, **changes)

    def _run_brief(self, session):
        """The brief a provider is started with: the saved one, plus — for an
        owner's chat thread — the thread's effective (model, effort), and on
        Claude or Codex, when the chat is offered session tools, a `jarvis`
        MCP server carrying a caller token minted for this session (plan
        §1.2). The token lives only in this copy: `_open` writes the saved
        brief, never this one, to brief.json."""
        from . import thread_model
        run_brief = session.brief
        if session.thread.provider in (ProviderName.CLAUDE, ProviderName.CODEX):
            if session.origin_tag is None:
                session.origin_tag = self.devservers.session_opened(session.thread.id, session.brief.task_id)
            run_brief = replace(run_brief, origin_tag=session.origin_tag)
        if not thread_model.is_chat(session.thread):
            return run_brief
        choice = thread_model.effective(session.thread)
        session.applied = session.announced = choice
        session.applied_record = (session.thread.model, session.thread.effort)
        # A saved brief never carries a jarvis server (`make_brief` refuses
        # MCP servers from a caller); if one somehow did, it is not passed on.
        servers = {name: server for name, server in session.brief.mcp_servers.items()
                   if name != "jarvis"}
        token = self._mint(session)
        if token is not None:
            from .bridge import server_entry
            servers["jarvis"] = server_entry(session.thread.provider, self.port, token)
        return replace(run_brief, model=choice[0], effort=choice[1], mcp_servers=servers)

    def _mint(self, session) -> str | None:
        """A caller token for an owner's chat on Claude or Codex that is
        offered at least one session tool; None for anything else (the fast
        path runs in-process and binds `runtime.caller()` itself)."""
        from . import session_tools, thread_model
        thread = session.thread
        if (not thread_model.is_chat(thread)
                or thread.provider not in (ProviderName.CLAUDE, ProviderName.CODEX)
                or not session_tools.for_provider(thread.provider)):
            return None
        return self.callers.mint(session)

    def _apply_choice(self, session):
        """At a turn's start: hand the provider a changed choice. A default
        thread follows the global picker here, every turn, and a change the
        owner did not just make in this thread is written to its log."""
        from . import thread_model
        if session.applied is None or not thread_model.is_chat(session.thread):
            return
        with self._lock:
            try:
                want = thread_model.effective(session.thread)
            except Exception:
                # A default that cannot be read right now (a broken
                # routing.json) keeps the thread on what it already runs.
                LOG.warning("Cannot resolve the model for thread %s", session.thread.id, exc_info=True)
                return
            if want == session.applied:
                return
        setter = getattr(session.provider, "set_model", None)
        try:
            if setter is None:
                raise ValueError(f"the {session.thread.provider.value} provider cannot change "
                                 "model mid-thread")
            setter(session.handle, *want)
        except Exception as exc:
            # A refused switch may already have disconnected the old CLI
            # (Claude reconnects the old model then): its children are gone.
            self._switch_ended_subagents(session)
            self._switch_refused(session, want, exc)
            return
        self._switch_ended_subagents(session)
        with self._lock:
            session.applied = want
            session.applied_record = (session.thread.model, session.thread.effort)
            if want != session.announced:
                session.announced = want
                self._model_line(session.thread, want, "follows the default")

    def _switch_ended_subagents(self, session):
        """After a model switch, refused or not: a child the provider no
        longer runs (Claude switches by reconnecting, and the old CLI takes
        its sub-agents with it) is stopped in the record (SV-1). A provider
        that cannot say which run (`live_subagents`) is left alone."""
        live = getattr(session.provider, "live_subagents", None)
        if not callable(live) or session.handle is None:
            return
        try:
            keep = frozenset(live(session.handle))
        except Exception:  # noqa: BLE001 — a closed session runs none
            keep = frozenset()
        try:
            self.subagents.end_session(session.thread, session.subagent_key,
                                       "the session restarted for a model switch", keep=keep)
        except Exception:
            LOG.warning("Cannot end the sub-agents of %s", session.thread.id, exc_info=True)

    def _switch_refused(self, session, want, exc):
        """A provider refused to move a thread onto `want`. The record goes
        back to what the provider is still running, so the next message does
        not try (and fail) the same switch again, and the transcript says so.

        A refusal the provider survived (`BriefRefused`, a `ValueError`) lets
        the turn go on, on the old model. `SessionLost` means the provider had
        to close the session: the turn ends with an error and the session is
        dropped, so the next message resumes it cleanly on the old model.
        """
        from . import thread_model
        applied = session.applied
        reason = str(exc) or type(exc).__name__
        with self._lock:
            thread = session.thread
            try:
                asked = thread_model.effective(thread)
            except Exception:
                asked = None
            # Roll back only while the record still asks for the refused
            # choice: a PATCH that landed during the switch is the owner's
            # newer word, and the next turn tries that one instead.
            rollback = asked == want
            if rollback:
                restored = session.applied_record or (None, None)
                thread.model, thread.effort = restored
                try:
                    still = thread_model.effective(thread)
                except Exception:
                    still = None
                if still != applied:
                    # A default thread whose default moved on: pin it to what
                    # it runs, or every message would retry the switch.
                    thread.model, thread.effort = applied
                thread.updated = utcnow()
                self.stores.threads.save(thread)
                session.announced = applied
                session.applied_record = (thread.model, thread.effort)
            text = (f"switch to {thread_model.label(*want)} refused: {reason}; "
                    f"still on {thread_model.label(*applied)}")
            self._system_line(thread, "model_set", text, {
                "provider": thread.provider.value, "model": thread.model, "effort": thread.effort,
                "effective_model": applied[0], "effective_effort": applied[1],
                "refused_model": want[0], "refused_effort": want[1]})
            record = self.thread_json(thread)
            self.bus.publish({"kind": "thread_updated", "thread_id": thread.id,
                              "project_id": thread.project_id,
                              "data": {**record, "thread_id": thread.id,
                                       "changed": ["model", "effort"],
                                       "effective_model": applied[0],
                                       "effective_effort": applied[1]}})
            if isinstance(exc, SessionLost):
                session.lost = True
                self._retire_drive(session)
        if isinstance(exc, SessionLost):
            raise DaemonError(f"the {thread.provider.value} session closed after a refused "
                              "model switch; send again to resume it on "
                              f"{thread_model.label(*applied)}")
        LOG.warning("Thread %s stays on %s: %s", thread.id, applied, reason)

    def _model_line(self, thread, choice, why):
        from . import thread_model
        text = f"model → {thread_model.label(*choice)} ({why})"
        self._system_line(thread, "model_set", text, {
            "provider": thread.provider.value, "model": thread.model, "effort": thread.effort,
            "effective_model": choice[0], "effective_effort": choice[1]})
        return text

    def _effort_line(self, thread, choice):
        """`effort → high (default model: X)`: an effort change on a thread
        that follows the default model, and keeps following it."""
        effort = choice[1] or "none"
        if thread.effort is None:
            effort = f"default · {effort}"
        text = f"effort → {effort} (default model: {choice[0] or 'unknown'})"
        self._system_line(thread, "model_set", text, {
            "provider": thread.provider.value, "model": thread.model, "effort": thread.effort,
            "effective_model": choice[0], "effective_effort": choice[1]})
        return text

    def _system_line(self, thread, kind, text, data):
        record = {"kind": kind, "at": utcnow(), "thread_id": thread.id,
                  "project_id": thread.project_id, "event_id": uuid.uuid4().hex,
                  "data": {**data, "text": text}}
        self.stores.threads._append(thread.id, "log.jsonl", record)
        self.bus.publish(record)

    def set_thread_model(self, thread_id, body) -> dict:
        """`PATCH /threads/{id}` `{model?, effort?}`. Applies from the next
        message, never in the middle of a turn; `brief.json` is untouched.

        A model change — a new model, or `model: null` back to the default —
        resets the effort to that model's default unless one is given with
        it; unpinning is a choice of model like any other, so it clears a
        stored effort too. An effort alone on a default thread is stored on its own and the
        thread **keeps following the default model** (A4 amendment,
        2026-10-07): `thread_model.effective` clamps it to whatever the
        default supports at each turn. Only an explicit model pins one.
        """
        from . import thread_model
        thread = self.require(self.stores.threads, thread_id)
        if not thread_model.is_chat(thread):
            raise APIError(409, "a task's threads run the model routing gave them")
        provider = self.providers.get(thread.provider)
        if provider is not None and not hasattr(provider, "set_model"):
            raise APIError(409, f"the {thread.provider.value} provider cannot change model mid-thread")
        if "model" in body:
            model = body["model"]
            effort = body.get("effort")
        else:
            model = thread.model
            effort = body["effort"]
        try:
            model, effort = thread_model.check(thread.provider, model, effort, current=thread.model)
        except thread_model.ChoiceRefused as exc:
            raise APIError(400, str(exc)) from exc
        from . import projects
        with self._lock:
            self._active()
            thread = self.require(self.stores.threads, thread_id)
            # An archived thread, or one in an archived project, is read-only
            # until it is restored: the same rule as rename and move.
            if thread.archived or projects.is_archived(self.stores, thread.project_id):
                raise APIError(409, "restore the thread before changing its model")
            before = (thread.model, thread.effort)
            thread.model, thread.effort = model, effort
            thread.updated = utcnow()
            self.stores.threads.save(thread)
            session = self._sessions.get(thread.id)
            if session is not None:
                # The live record is saved again on every usage and finish;
                # keep it in step so a turn in flight cannot write the old
                # choice back over this one.
                session.thread.model, session.thread.effort = model, effort
            choice = thread_model.effective(thread)
            if before != (model, effort):
                if session is not None:
                    session.announced = choice
                if model is None and before[0] is None:
                    # Only a default thread's effort changed: it still
                    # follows the default model, and the line names it.
                    self._effort_line(thread, choice)
                else:
                    self._model_line(thread, choice, "default, from the next message" if model is None
                                     else "from the next message")
            record = self.thread_json(thread)
            self.bus.publish({"kind": "thread_updated", "thread_id": thread.id,
                              "project_id": thread.project_id,
                              "data": {**record, "thread_id": thread.id,
                                       "changed": ["model", "effort"],
                                       "effective_model": choice[0],
                                       "effective_effort": choice[1]}})
            return record

    def thread_json(self, thread) -> dict:
        """A thread on the wire, with `cwd` filled from its saved brief for
        threads opened before the field existed. Read-only: nothing is
        rewritten, so listing threads can never change one."""
        record = to_json(thread)
        with self._lock:
            # Live state, never stored: whether a turn is running and how many
            # owner messages wait on it. The HUD reconciles its busy state
            # against `running`, so a missed `turn_finished` cannot wedge it.
            session = self._sessions.get(thread.id)
            record["running"] = session is not None and session.worker is not None
            # The running turn is a compaction (the HUD's Compact button).
            record["compacting"] = bool(record["running"] and session.compacting)
            # The owner's Bypass switch is on for this chat's open session.
            bypassed = getattr(session.provider, "bypassed", None) if session is not None else None
            record["bypass"] = bool(callable(bypassed) and session.handle is not None
                                    and bypassed(session.handle))
            record["queued"] = len(self._queues.get(thread.id) or ())
            # Other chats' messages waiting on it, and its own still out.
            record.update(self.chat_relay.counts(thread.id))
            # When that turn began (WP-H, H-b): present only while it runs, so
            # a reloaded HUD's thinking line shows the true elapsed time.
            if record["running"] and session.running_since:
                record["running_since"] = session.running_since
        if thread.surface:
            # Where the chat is on Discord (PR C): names and a link, for the
            # chat header. Never a token; the guild id is setup's.
            try:
                from .discord.mirror import describe_surface
                record["discord"] = describe_surface(self, thread)
            except Exception:
                record["discord"] = None
        if record.get("cwd") is None:
            try:
                path = self.stores.threads.path(thread.id).with_name("brief.json")
                cwd = json.loads(path.read_text()).get("cwd")
                if isinstance(cwd, str):
                    record["cwd"] = cwd
            except (OSError, ValueError, AttributeError):
                pass
        return record

    def resume_thread(self, thread_id):
        thread = self.require(self.stores.threads, thread_id)
        path = self.stores.threads.path(thread_id).with_name("brief.json")
        try:
            brief = from_json(Brief, json.loads(path.read_text()))
            _validate(brief, Brief)
        except (OSError, ValueError, TypeError) as exc:
            raise DaemonError(f"cannot resume thread: saved brief unavailable at {path}") from exc
        return self._open(thread, brief)

    def send(self, thread_id: str, message: UserMessage) -> str:
        """Start a turn with `message`, or refuse (409) while one runs.

        For callers that drive turns themselves — the task runner, the escape
        hatch — and retry on "already running". An owner's message to a chat
        goes through `deliver`, which never dead-ends while a turn runs."""
        _validate(message, UserMessage)
        data = user_data(message)
        session = self._live_session(thread_id)
        with self._lock:
            self._sendable(session)
            if (session.worker is not None or self._queues.get(thread_id)
                    or self._maybe_cli(session, eager=True)):
                # A turn the CLI started by itself (SV-6) runs first: the
                # caller retries on "already running", as for any turn, and
                # the task runner waits that one out for longer (it says so).
                own = " (the CLI's own)" if session.self_started else ""
                raise DaemonError("a turn is already running on this thread" + own)
            return self._begin(session, message, data)

    def deliver(self, thread_id: str, message: UserMessage) -> dict:
        """An owner's message to a thread (2026-10-08): never a dead end.

        No turn running: it starts one (`{"status": "started"}`). A chat turn
        running: it is **steered** into that turn at the provider's next safe
        point (`"steered"`), or — when it cannot be (nothing to steer with, a
        turn waiting on an approval or a question, messages already waiting,
        a provider that refuses) — **queued** to run as its own turn when this
        one ends (`"queued"`, up to QUEUE_MAX). A provider that cannot steer
        at all has its turn interrupted for it instead (`"steered"`, mode
        `interrupt`). A steer the provider refuses after the owner stopped
        its turn is not sent at all (`"dropped"`, its words handed back). A steer has the authority of a send and no more: it
        never answers or resolves anything the turn is waiting on. A task's
        threads are the runner's: a running one still refuses (409).
        """
        _validate(message, UserMessage)
        data = user_data(message)
        session = self._live_session(thread_id)
        drain = False
        with self._lock:
            self._sendable(session)
            waiting = self._queues.setdefault(thread_id, deque())
            if session.worker is None and not waiting:
                if self._maybe_cli(session, eager=True):
                    # The CLI started a turn by itself and it runs first, in
                    # the CLI's own order (SV-6); this message waits for it.
                    item = self._enqueue(session, waiting, message, data)
                    return self._queued_reply(session, item)
                return {"status": "started", "turn_id": self._begin(session, message, data)}
            from . import thread_model
            if not thread_model.is_chat(session.thread):
                raise DaemonError("a turn is already running on this thread")
            if (session.worker is None or waiting or session.compacting
                    or self._waiting_on_owner(session)):
                # Behind what already waits, so nothing overtakes it; never
                # into a turn blocked on the owner, where it could read as the
                # answer to the question the turn is waiting on; and never
                # into a compaction, which has no conversation to steer.
                item = self._enqueue(session, waiting, message, data)
                drain = session.worker is None
                reply = self._queued_reply(session, item)
            else:
                reply = None
                provider, handle = session.provider, session.handle
        if reply is not None:
            if drain:
                self._drain(thread_id)
            return reply
        return self._steer(session, provider, handle, message, data)

    def deliver_queued(self, thread_id: str, message: UserMessage, *,
                       decision_id: str | None = None) -> dict:
        """The owner's answer to a decision (inbox B1, design §5): a message
        that may start a turn or wait for one, and never steers (chat-to-chat
        C3's rule). No turn running and nothing of the owner's waiting: it
        starts one (`{"status": "started"}`). Otherwise it waits in a queue of
        its own (`_inbox_queues`, `"queued"`), drained after the owner's queue
        and before other chats' messages: never into a running turn, never
        interrupting it, never read as the answer to a card or a question it
        waits on. It takes none of the owner's QUEUE_MAX slots and never makes
        the owner's own message wait instead of steering (review of PR #309).
        The caller bounds it (5 open decisions per chat). `decision_id` rides
        in the logged data, so a drop marks the decision undelivered. Only a
        chat thread; what the message may do is its own `desk`/`via` (an
        inbox answer is never at the desk)."""
        _validate(message, UserMessage)
        data = user_data(message)
        if decision_id is not None:
            data["decision_id"] = decision_id
        session = self._live_session(thread_id)
        from . import thread_model
        if not thread_model.is_chat(session.thread):
            raise DaemonError("only a chat takes a queued message")
        with self._lock:
            self._sendable(session)
            answers = self._inbox_queues.setdefault(thread_id, deque())
            if (session.worker is None and not self._queues.get(thread_id) and not answers
                    and not self._maybe_cli(session, eager=True)):
                return {"status": "started", "turn_id": self._begin(session, message, data)}
            item = _Queued(message, data, uuid.uuid4().hex, uuid.uuid4().hex)
            answers.append(item)
            self._log_user(session.thread, item, queued=True)
            drain = session.worker is None
            reply = {"status": "queued", "turn_id": session.turn_id, "message_id": item.message_id,
                     "queued_turn_id": item.turn_id}
        if drain:
            self._drain(thread_id)
        return reply

    def _waiting(self, thread_id) -> bool:
        """Under the lock: anything waiting to run as a turn of this thread:
        the owner's queue, inbox answers, other chats' messages."""
        return bool(self._queues.get(thread_id) or self._inbox_queues.get(thread_id)
                    or self.chat_relay.pending(thread_id))

    def _answers_dropped(self, items) -> None:
        """Under the lock: inbox answers that will never run. Each decision is
        marked undelivered, so its row comes back saying so (review of PR
        #309: a Stop, a close or a thread that could not resume lost it)."""
        inbox = getattr(self, "inbox", None)
        for item in items:
            decision_id = item.data.get("decision_id") if isinstance(item.data, dict) else None
            if inbox is not None and decision_id:
                inbox.answer_dropped(decision_id)

    def _steer(self, session, provider, handle, message, data) -> dict:
        """Hand `message` to the provider for the running turn. Outside the
        daemon lock: a provider may wait on its own turn loop to take it, and
        that loop records events under this lock.

        The message is in the log (and on the bus) as a steer *before* the
        provider has it, so nothing it causes is ever logged ahead of it. If
        the provider refuses, the same message — same id, no second record —
        waits instead, and a `steer_queued` record says so."""
        thread_id = session.thread.id
        steer = getattr(provider, "steer", None)
        if not callable(steer):
            with self._lock:
                waiting = self._queues.setdefault(thread_id, deque())
                item = self._enqueue(session, waiting, message, data)
                drain = session.worker is None
                reply = self._queued_reply(session, item)
            if drain:
                self._drain(thread_id)
            return reply
        with self._lock:
            # Known to the turn before the provider can take it: a turn that
            # ends meanwhile hands it back (`undelivered`) by identity.
            item = _Queued(message, data, uuid.uuid4().hex, session.turn_id or "")
            session.steered.append(item)
            drive_turn = session.drive_turn
            drive_worker = session.worker
            drive_desk = session.desk.slot
            reservation = (session.desk.reserve()
                           if not desk_message(session.brief, message) else None)
            self._log_user(session.thread, item, steer=True)
        try:
            steer_confirmed = steer(handle, message)
        except SteerRefused as exc:
            fallback, reason = exc.fallback, str(exc)
            if exc.before_write and reservation is not None:
                with self._lock:
                    if self._sessions.get(thread_id) is session:
                        session.desk.refused_before_write(reservation)
        except Exception as exc:  # noqa: BLE001 — a failed steer queues, never errors
            LOG.warning("Steering thread %s failed (%s)", thread_id, type(exc).__name__)
            fallback, reason = "queue", type(exc).__name__
        else:
            with self._lock:
                if (steer_confirmed is not False and drive_turn is not None
                        and self._sessions.get(thread_id) is session
                        and session.drive_turn is drive_turn and not drive_turn.closed
                        and session.worker is drive_worker and session.turn_id == drive_turn.turn_id
                        and session.desk.slot is drive_desk and drive_desk.get("present") is True
                        and desk_message(session.brief, message) and not session.cancelled.is_set()):
                    drive_turn.accept_desk_steer(message.text)
            return {"status": "steered", "mode": "native", "turn_id": item.turn_id,
                    "message_id": item.message_id}
        finally:
            if reservation is not None:
                with self._lock:
                    session.desk.settled(reservation)
        interrupt, drain = False, False
        with self._lock:
            session.steered = [other for other in session.steered if other is not item]
            if item.turn_id and item.turn_id in session.stopped_turns:
                # The owner pressed Stop while the provider held this steer,
                # and it was not taken: stop means stop. Dropped as Stop drops
                # what waits (`queue_cleared` hands the words back) — never
                # requeued to start a turn, never an interrupt (Bugbot on PR
                # #22: it used to run as a new turn after the Stop).
                self._drop_waiting(session.thread, "stopped", [item])
                LOG.info("Thread %s: owner message not sent (the turn was stopped; %s)",
                         thread_id, reason)
                return {"status": "dropped", "reason": "stopped", "turn_id": item.turn_id,
                        "message_id": item.message_id}
            waiting = self._queues.setdefault(thread_id, deque())
            if fallback == "interrupt" and session.worker is not None and not session.closing:
                # No way to steer: stop the turn for the owner's message and
                # run it next, telling the model why its turn ended. Not the
                # owner's stop — what else waits is kept.
                item.message = replace(message, text=INTERRUPTED_NOTE + message.text)
                item.interrupting = True
                session.cancelled.set()
                session.desk.end()
                self._retire_drive(session)
                interrupt = True
            elif len(waiting) >= QUEUE_MAX:
                self._drop_waiting(session.thread, "three messages were already waiting", [item])
                raise QueueFull(QUEUE_FULL)
            else:
                drain = session.worker is None
            self._requeue(session.thread, item, waiting)
            LOG.info("Thread %s: owner message %s (%s)", thread_id,
                     "interrupts the turn" if interrupt else "queued", reason)
            reply = self._queued_reply(session, item)
        if interrupt:
            reply.update(status="steered", mode="interrupt")
            try:
                provider.interrupt(handle)
            except Exception:  # noqa: BLE001 — the turn still ends; the message still runs
                LOG.warning("Interrupting thread %s failed", thread_id, exc_info=True)
        if drain:
            self._drain(thread_id)
        return reply

    def _requeue(self, thread, item, waiting, *, front=False) -> None:
        """Under the lock: a message already logged as a steer waits instead,
        as its own turn — refused by the provider, or never delivered before
        the turn ended. Same id, a turn id of its own, and one `steer_queued`
        record (and event) rather than a second `user` record."""
        item.turn_id = uuid.uuid4().hex
        if front:
            waiting.appendleft(item)
        else:
            waiting.append(item)
        record = {"kind": "steer_queued", "at": utcnow(), "turn_id": item.turn_id,
                  "thread_id": thread.id, "project_id": thread.project_id,
                  "data": {"message_id": item.message_id, "turn_id": item.turn_id,
                           "interrupting": item.interrupting}}
        self.stores.threads._append(thread.id, "log.jsonl", record)
        self.bus.publish(dict(record))

    def _live_session(self, thread_id) -> "_Session":
        """The thread's open session, resumed if it has none."""
        with self._lock:
            self._active()
            if thread_id in self._interrupting:
                raise _StopPending("the previous stop is still being delivered; try again shortly")
            session = self._sessions.get(thread_id)
        if session is None:
            from .projects import is_archived
            stored = self.require(self.stores.threads, thread_id)
            if stored.archived or is_archived(self.stores, stored.project_id):
                raise DaemonError("this thread is archived; restore it from the Archive to continue")
            session = self.resume_thread(thread_id)
        return session

    def _sendable(self, session) -> None:
        """Under the lock: whether anything may reach this session now.
        Checked under the same lock an archive takes, so a turn cannot start
        in a thread the owner is archiving (decisions B1)."""
        self._active()
        if session.thread.id in self._interrupting:
            raise _StopPending("the previous stop is still being delivered; try again shortly")
        if session.handle is None or session.closing:
            raise DaemonError("thread session is opening or closing")
        from .projects import is_archived
        if session.thread.archived or is_archived(self.stores, session.thread.project_id):
            raise DaemonError("this thread is archived; restore it from the Archive to continue")

    def _retire_drive(self, session):
        """Under the session lock; revoke refs while active input keeps its lease."""
        turn = getattr(session, "drive_turn", None)
        if turn is not None:
            turn.closed = True
            session.drive_turn = None
            from .browser.drive import retire_turn
            retire_turn(turn.key)

    def _begin(self, session, message, data, *, queued: "_Queued | None" = None,
               relay=None) -> str:
        """Under the lock: start a turn. `queued` is a message that waited:
        its `user` record is already in the log, so its turn opens with a
        `queued_started` record instead of a second one. `relay` (a
        `chat_relay.RelayStart`) is another chat's message or its reply: the
        turn opens with the relay's own record, never `user` (P-8), and is
        never at the desk (its message is not the owner's)."""
        thread_id = session.thread.id
        session.relay_turn = relay.item if relay is not None else None
        session.relay_mark = relay.mark if relay is not None else None
        session.cancelled.clear()
        session.owner_stopped = False
        session.steered = []
        session.questions.clear()
        session.question_detail = {}
        session.turn_id = queued.turn_id if queued is not None else uuid.uuid4().hex
        session.desk.begin(session.brief, message, session.turn_id)
        self._retire_drive(session)
        if (session.thread.provider in (ProviderName.CLAUDE, ProviderName.CODEX)
                and session.brief.role == Role.CHAT and session.brief.task_id is None):
            from .browser.drive_state import DriveTurn
            session.drive_turn = DriveTurn(
                thread_id, session.turn_id, session.brief.profile == PermissionProfile.STRICT,
                session.brief.cwd or "", message.text if session.desk.slot.get("present") is True else "",
                provider=session.thread.provider.value)
        worker = threading.Thread(target=self._turn, args=(session, message),
                                  name=f"jarvis-turn-{thread_id}", daemon=True)
        turn_id = session.turn_id
        if relay is not None:
            self.chat_relay.opened(session, relay, turn_id)
        elif queued is None:
            self.stores.threads._append(thread_id, "log.jsonl",
                                        {"kind": "user", "at": utcnow(), "turn_id": turn_id,
                                         "thread_id": thread_id, "data": data})
            # The owner's message on the bus (PR C, plan §4.3), before the
            # worker starts, so it precedes every event of its turn. The
            # Discord mirror and the HUD read it; the log record above holds
            # the same fields, which is what the mirror catches up from.
            self.bus.publish({"kind": "user_message", "thread_id": thread_id,
                              "project_id": session.thread.project_id, "turn_id": turn_id,
                              "at": utcnow(), "data": dict(data)})
        else:
            record = {"kind": "queued_started", "at": utcnow(), "turn_id": turn_id,
                      "thread_id": thread_id, "project_id": session.thread.project_id,
                      "data": {"message_id": queued.message_id, "turn_id": turn_id}}
            self.stores.threads._append(thread_id, "log.jsonl", record)
            self.bus.publish(dict(record))
        session.worker = worker
        session.running_since = utcnow()
        worker.start()
        self._dev_hook("turn_started", thread_id, session.thread.provider)
        return turn_id

    # -- between turns (SV-6) -------------------------------------------------

    def _start_watcher(self, session) -> None:
        """Read the provider's `watch` for this session on a thread of its
        own, for the session's life. A provider without one is read only by
        its turns, as before."""
        watch = getattr(session.provider, "watch", None)
        if not callable(watch) or session.handle is None:
            return
        watcher = threading.Thread(target=self._watch, args=(session, watch),
                                   name=f"jarvis-watch-{session.thread.id}", daemon=True)
        session.watcher = watcher
        watcher.start()

    def _watch(self, session, watch) -> None:
        """The watcher: every event the provider reads between turns, in
        order, until the session closes (the provider's iterator ends)."""
        try:
            for event in watch(session.handle):
                try:
                    self._idle_event(session, event)
                except Exception:  # noqa: BLE001 — one bad event never ends the watch
                    LOG.exception("Cannot record an event read between turns on %s",
                                  session.thread.id)
        except Exception:  # noqa: BLE001
            LOG.exception("The between-turn reader of %s failed", session.thread.id)

    def _idle_event(self, session, event) -> None:
        """One event a provider read between turns (SV-6).

        - A sub-agent's: its record, as in a turn (`_record_subagent`).
        - The CLI started a turn by itself: taken up as a turn of the thread
          now, or when the running one ends (`_maybe_cli`).
        - A gate question: logged with no turn, for the audit trail. The
          decision was `permit`'s — the thread's own asker — and nothing here
          answers or changes it.
        - An error: a `notice` line in the chat; a fatal one also drops the
          session (the provider closed it), so the next message resumes it."""
        if event.thread_id != session.thread.id:
            LOG.warning("A between-turn event for another thread was dropped")
            return
        kind = event.kind
        if kind in (EventKind.SUBAGENT, EventKind.SUBAGENT_EVENT):
            self._record_subagent(session, event, idle=True)
        elif kind == EventKind.TURN_STARTED and (event.data or {}).get("self_started"):
            with self._lock:
                session.cli_offered = True
                self._maybe_cli(session)
        elif kind in GATE_KINDS:
            self._record(session, event, idle=True)
        elif kind == EventKind.ERROR:
            self._idle_error(session, event)

    def _idle_error(self, session, event) -> None:
        from .approvals import clean_line
        from .redact import scrub_text
        data = event.data or {}
        fatal = data.get("fatal") is True
        try:
            text = clean_line(scrub_text(str(data.get("message") or "")), 300)
        except Exception:  # noqa: BLE001 — a scrub that fails shows nothing of it
            text = "[withheld]"
        with self._lock:
            if session.retired or session.closing or self._sessions.get(session.thread.id) is not session:
                return
        # A `notice` line (the owner's, never the model's), with no turn.
        self._record(session, Event(EventKind.NOTICE, session.thread.id,
                                    {"text": f"Between turns: {text}", "fatal": fatal}), idle=True)
        if not fatal:
            return
        lost = False
        with self._lock:
            if session.retired or session.closing or self._sessions.get(session.thread.id) is not session:
                return
            if session.worker is None:
                # The provider closed the session: dropped, so the next
                # message resumes it (a running turn finds out for itself).
                session.lost = lost = True
                self._retire_drive(session)
                self._sessions.pop(session.thread.id, None)
        if lost:
            self._cleanup(session)

    def _maybe_cli(self, session, *, eager=False) -> bool:
        """Under the lock: start the turn the CLI began by itself, if the
        provider holds one and nothing else runs. -> whether it started.

        Taken up once the watcher has passed the provider's word on
        (`cli_offered`), so a sub-agent's end read before it is recorded
        first. `eager` (an owner message arriving, a queue about to run)
        asks the provider directly: the CLI runs its own turn first anyway,
        and the message then waits for it rather than reading it."""
        thread_id = session.thread.id
        if (self._stopping or session.closing or session.retired or session.lost
                or session.handle is None or session.worker is not None
                or self._sessions.get(thread_id) is not session or thread_id in self._interrupting
                or not (eager or session.cli_offered)):
            return False
        pending = getattr(session.provider, "cli_turn_pending", None)
        try:
            ready = callable(pending) and pending(session.handle) is True
        except Exception:  # noqa: BLE001 — a provider that cannot say has none
            ready = False
        if not ready:
            session.cli_offered = False         # a send read it first: nothing waits
            return False
        self._begin_cli(session)
        return True

    def _begin_cli(self, session) -> None:
        """Under the lock: a turn the CLI started by itself (SV-6), as a turn
        of the thread: a fresh `turn_id`, a `cli_turn` line where the owner's
        message would be, and the ordinary turn worker reading the provider's
        `adopt`. Every record it makes is `self_started`, so Discord, speech
        and the task runner leave it alone. Owner messages wait for it."""
        thread_id = session.thread.id
        session.cancelled.clear()
        session.owner_stopped = False
        session.steered = []
        session.questions.clear()
        session.question_detail = {}
        session.turn_id = uuid.uuid4().hex
        session.self_started = True
        session.cli_offered = False
        session.relay_turn = session.relay_mark = None
        self._retire_drive(session)
        record = {"kind": "cli_turn", "at": utcnow(), "turn_id": session.turn_id,
                  "thread_id": thread_id, "project_id": session.thread.project_id,
                  "self_started": True, "event_id": uuid.uuid4().hex,
                  "data": {"text": CLI_TURN_TEXT}}
        self.stores.threads._append(thread_id, "log.jsonl", record)
        self.bus.publish(dict(record))
        worker = threading.Thread(target=self._turn, args=(session, None),
                                  name=f"jarvis-cli-turn-{thread_id}", daemon=True)
        session.worker = worker
        session.running_since = utcnow()
        worker.start()
        self._dev_hook("turn_started", thread_id, session.thread.provider)

    # -- the owner's Compact button (2026-10-19) ------------------------------

    def compact(self, thread_id) -> dict:
        """Compact a chat's conversation with its provider's own process
        (Claude's /compact, Codex's thread compaction, the fast path's
        summary), as a turn with no message. The owner's alone: only the
        HUD's `POST /threads/{id}/compact` calls this, behind `owner_only`; no
        tool, bridge, MCP, Discord or CLI path reaches it.

        Refused (DaemonError, 409) — and the provider's `compact` never
        called — when the thread cannot take a turn now (`_sendable`), is not
        an owner's chat, runs a turn (a compaction included), has messages
        waiting (the owner's or another chat's), has a CLI turn of its own
        waiting (which starts instead), waits on the owner (a card or a
        question), or its provider has no `compact` or says why not
        (`compact_refusal`). -> {"status": "started", "turn_id"}."""
        session = self._live_session(thread_id)
        with self._lock:
            self._sendable(session)
            from . import thread_model
            if not thread_model.is_chat(session.thread) or session.brief.task_id is not None:
                raise DaemonError(COMPACT_NOT_A_CHAT)
            if session.worker is not None:
                raise DaemonError(COMPACT_RUNNING)
            if self._waiting(thread_id):
                raise DaemonError(COMPACT_WAITING)
            compact = getattr(session.provider, "compact", None)
            if not callable(compact):
                raise DaemonError(COMPACT_UNSUPPORTED)
            if self._maybe_cli(session, eager=True):
                raise DaemonError(COMPACT_CLI)
            if self._waiting_on_owner(session):
                raise DaemonError(COMPACT_CARD)
            refusal = getattr(session.provider, "compact_refusal", None)
            said = refusal(session.handle) if callable(refusal) else None
            if said:
                raise DaemonError(str(said))
            return {"status": "started", "turn_id": self._begin_compact(session)}

    def set_bypass(self, thread_id, on) -> dict:
        """The owner's Bypass switch (2026-10-21): whether Claude Code's
        auto-mode classifier judges this chat's calls. The owner's alone: only
        the HUD's `POST /threads/{id}/bypass` calls this, behind `owner_only`;
        no tool, bridge, MCP, Discord or CLI path reaches it, so no agent can
        turn off a check on itself.

        It loosens one layer only. Jarvis's gate still runs first on every
        call — DENY rules, the always-ask list, owner-only operations, cards —
        and a call it allowed then runs without the classifier being asked
        (`ClaudeProvider.set_bypass`). Never stored: it lives on the open
        session, so a restart or a reopened chat is back to auto.

        Turning it off always succeeds, open session or not. Turning it on
        resumes the chat's session if it has none and is refused (409) for a
        task's thread, an archived chat, a provider with no classifier to
        switch off, or a chat on the ask profile. -> {"bypass": bool}."""
        if not isinstance(on, bool):
            raise APIError(400, "on must be true or false")
        from . import thread_model
        thread = self.require(self.stores.threads, thread_id)
        if not thread_model.is_chat(thread):
            raise APIError(409, BYPASS_NOT_A_CHAT)
        if on:
            session = self._live_session(thread_id)
        else:
            with self._lock:
                session = self._sessions.get(thread_id)
        with self._lock:
            if session is None or session.handle is None:
                if on:
                    raise DaemonError("thread session is opening or closing")
                return {"bypass": False}
            if on:
                self._sendable(session)
            switch = getattr(session.provider, "set_bypass", None)
            bypassed = getattr(session.provider, "bypassed", None)
            if not (callable(switch) and callable(bypassed)):
                if on:
                    raise APIError(409, BYPASS_UNSUPPORTED)
                return {"bypass": False}
            if on:
                refusal = getattr(session.provider, "bypass_refusal", None)
                said = refusal(session.handle) if callable(refusal) else None
                if said:
                    raise APIError(409, str(said))
            before = bool(bypassed(session.handle))
            try:
                switch(session.handle, on)
            except BriefRefused as exc:
                # The session closed between the check and the switch.
                raise APIError(409, str(exc)) from None
            now = bool(bypassed(session.handle))
            if now != before:
                # One line in the chat, live and after a reload, so the
                # transcript says when the classifier was off.
                record = {"kind": "notice", "at": utcnow(), "thread_id": thread_id,
                          "project_id": session.thread.project_id,
                          "event_id": uuid.uuid4().hex,
                          "data": {"text": BYPASS_ON if now else BYPASS_OFF}}
                self.stores.threads._append(thread_id, "log.jsonl", record)
                self.bus.publish(dict(record))
        if now == before:
            return {"bypass": now}
        from .permissions import ApprovalRecord, log_decision
        log_decision(ApprovalRecord(
            tool="bypass", args_digest="", layer="owner-bypass", decision="on" if now else "off",
            reason="the owner's Bypass switch, from the HUD", thread_id=thread_id,
            provider=session.thread.provider.value))
        self.bus.publish({"kind": "bypass_changed", "thread_id": thread_id,
                          "project_id": session.thread.project_id, "data": {"bypass": now}})
        return {"bypass": now}

    def _begin_compact(self, session) -> str:
        """Under the lock: the compaction as a turn of the thread, modelled on
        `_begin_cli`: a fresh `turn_id`, a `compact_started` record where the
        owner's message would be, and the ordinary turn worker reading the
        provider's `compact`. No desk, no model switch, no Browser drive
        turn, no `user` record. Every record of it says `compaction`."""
        thread_id = session.thread.id
        session.cancelled.clear()
        session.owner_stopped = False
        session.steered = []
        session.questions.clear()
        session.question_detail = {}
        session.turn_id = uuid.uuid4().hex
        session.compacting = True
        session.relay_turn = session.relay_mark = None
        self._retire_drive(session)
        record = {"kind": "compact_started", "at": utcnow(), "turn_id": session.turn_id,
                  "thread_id": thread_id, "project_id": session.thread.project_id,
                  "compaction": True, "event_id": uuid.uuid4().hex, "data": {}}
        self.stores.threads._append(thread_id, "log.jsonl", record)
        self.bus.publish(dict(record))
        worker = threading.Thread(target=self._turn, args=(session, None),
                                  kwargs={"compact": True},
                                  name=f"jarvis-compact-{thread_id}", daemon=True)
        session.worker = worker
        session.running_since = utcnow()
        worker.start()
        self._dev_hook("turn_started", thread_id, session.thread.provider)
        return session.turn_id

    def _waiting_on_owner(self, session) -> bool:
        """Is the running turn blocked on the owner — a broker approval or a
        provider question? Then a message waits rather than steers."""
        if session.questions:
            return True
        try:
            pending = self.approvals.pending()
        except Exception:  # noqa: BLE001
            return False
        return any(getattr(r, "thread_id", None) == session.thread.id for r in pending)

    def _enqueue(self, session, waiting, message, data, *, cap=True, interrupting=False):
        """Under the lock: one owner message waits on this thread's turn."""
        if cap and len(waiting) >= QUEUE_MAX:
            raise QueueFull(QUEUE_FULL)
        item = _Queued(message, data, uuid.uuid4().hex, uuid.uuid4().hex, interrupting)
        waiting.append(item)
        self._log_user(session.thread, item, queued=True, interrupting=interrupting)
        return item

    def _queued_reply(self, session, item) -> dict:
        waiting = self._queues.get(session.thread.id) or ()
        position = next((i for i, other in enumerate(waiting) if other is item), len(waiting))
        return {"status": "queued", "turn_id": session.turn_id, "message_id": item.message_id,
                "position": position + 1, "queued_turn_id": item.turn_id}

    def _log_user(self, thread, item, **flags) -> None:
        """Under the lock: the owner's message in the log and on the bus,
        marked `steer` or `queued`, with the id later records refer to."""
        data = {**item.data, "message_id": item.message_id,
                **{name: True for name, on in flags.items() if on}}
        at = utcnow()
        self.stores.threads._append(thread.id, "log.jsonl",
                                    {"kind": "user", "at": at, "turn_id": item.turn_id,
                                     "thread_id": thread.id, "data": data})
        self.bus.publish({"kind": "user_message", "thread_id": thread.id,
                          "project_id": thread.project_id, "turn_id": item.turn_id,
                          "at": at, "data": dict(data)})

    def _drop_waiting(self, thread, reason, items=None) -> list:
        """Under the lock: owner messages that will not run — the owner
        stopped the turn (stop means stop, as Claude Code returns queued
        messages to the input rather than sending them), or the thread can no
        longer take a turn. Logged per message; one `queue_cleared` names
        them all, so a surface can hand the words back."""
        answers = []
        if items is None:
            waiting = self._queues.get(thread.id)
            items = list(waiting or ())
            if waiting:
                waiting.clear()
            # The owner's answers to decisions waiting here go too (inbox B1),
            # and each decision is marked undelivered.
            answers = list(self._inbox_queues.pop(thread.id, None) or ())
            items += answers
        if not items:
            return []
        for item in items:
            self.stores.threads._append(thread.id, "log.jsonl", {
                "kind": "queued_dropped", "at": utcnow(), "turn_id": item.turn_id,
                "thread_id": thread.id,
                "data": {"message_id": item.message_id, "reason": reason}})
        self.bus.publish({"kind": "queue_cleared", "thread_id": thread.id,
                          "project_id": thread.project_id, "data": {
                              "reason": reason,
                              "messages": [{"message_id": item.message_id,
                                            "typed": item.data.get("typed"),
                                            "via": item.data.get("via"),
                                            "attachments": item.data.get("attachments") or [],
                                            "images": item.data.get("images") or 0}
                                           for item in items]}})
        if answers:
            self._answers_dropped(answers)
        return items

    def _drain(self, thread_id) -> None:
        """Run the next waiting message as its own turn, if the thread is idle.
        Resumes the session if the turn before dropped it.

        The owner's queue first, always; only when it is empty, another
        chat's message waiting on this thread, then a reply held for it
        (chat_relay.py, P-4): two queues, never one."""
        with self._lock:
            if self._stopping or thread_id in self._interrupting or not self._waiting(thread_id):
                return
            session = self._sessions.get(thread_id)
            if session is not None and (session.worker is not None or session.closing
                                        or session.handle is None):
                return
        try:
            session = session or self._live_session(thread_id)
        except _StopPending:
            return  # leave queued words for the interrupt delivery's final drain
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Thread %s could not resume for its queued message (%s)",
                        thread_id, type(exc).__name__)
            reason = f"the thread could not resume ({type(exc).__name__})"
            with self._lock:
                try:
                    stored = self.stores.threads.get(thread_id)
                except StoreError:
                    stored = None
                if stored is not None:
                    self._drop_waiting(stored, reason)
                else:
                    # A deleted thread has no log to record the drop in, but
                    # what waited on it still goes, and an inbox answer's
                    # decision says it never arrived (review of PR #309).
                    self._queues.pop(thread_id, None)
                    self._answers_dropped(list(self._inbox_queues.pop(thread_id, None) or ()))
                self.chat_relay.thread_closed(thread_id, reason)
            return
        with self._lock:
            waiting = self._queues.get(thread_id)
            if (self._stopping or thread_id in self._interrupting or session.worker is not None
                    or not self._waiting(thread_id)):
                return
            try:
                self._sendable(session)
            except DaemonError as exc:
                self._drop_waiting(session.thread, str(exc))
                self.chat_relay.thread_closed(thread_id, str(exc))
                return
            if self._maybe_cli(session, eager=True):
                return      # the CLI's own turn first; its end drains the queue
            if waiting:
                item = waiting.popleft()
                self._begin(session, item.message, item.data, queued=item)
                return
            # Then the owner's answers to decisions (inbox B1), then other
            # chats' messages.
            answers = self._inbox_queues.get(thread_id)
            if answers:
                item = answers.popleft()
                self._begin(session, item.message, item.data, queued=item)
                return
            start = self.chat_relay.next_start(session)
            if start is not None:
                self._begin(session, start.message, None, relay=start)

    # -- approvals (design §6) ---------------------------------------------

    def _delegation_cancelled(self, thread_id):
        """Capture one native permission request, never a later turn on its handle."""
        with self._lock:
            session = self._sessions.get(thread_id)
            worker = session.worker if session is not None else None
            turn = session.drive_turn if session is not None else None
            turn_id = session.turn_id if session is not None else None
            slot = session.desk.slot if session is not None else None
        def cancelled():
            with self._lock:
                return (self._stopping or session is None or worker is None or turn is None
                        or self._sessions.get(thread_id) is not session
                        or session.worker is not worker or session.drive_turn is not turn
                        or session.turn_id != turn_id or turn.turn_id != turn_id or turn.closed
                        or session.desk.slot is not slot or slot.get("present") is not True
                        or session.cancelled.is_set() or session.closing or session.retired or session.lost)
        return cancelled

    def _permit(self, thread_id: str, brief: Brief) -> PermissionCallback:
        thread = self.stores.threads.get(thread_id)
        project = None
        task = None
        try:
            if thread is not None:
                project = self.stores.projects.get(thread.project_id)
            task_id = brief.task_id if brief is not None else None
            if task_id:
                task = self.stores.tasks.get(task_id)
        except StoreError:
            LOG.warning("Cannot read project/task for thread %s", thread_id, exc_info=True)
        # The session `_open` registered just before building this permit:
        # its card says which chat's message started the asking call's turn
        # (C6; a sub-agent's is its spawning turn's).
        session = self._sessions.get(thread_id)
        ctx = PermitContext(
            thread_id=thread_id, brief=brief,
            provider=thread.provider.value if thread is not None else "",
            project=project, task=task,
            delegation_cancelled=lambda: self._delegation_cancelled(thread_id),
            relayed_from=(lambda child=None: _relayed_from(session, child)) if session is not None else None)
        return build_permit(ctx, self.approvals)

    def _approval_requested(self, request: ApprovalRequest) -> None:
        self.bus.publish({"kind": "approval_requested",
                          "thread_id": request.thread_id,
                          "data": request.to_json()})

    def _approval_resolved(self, request: ApprovalRequest, decision, resolution: str) -> None:
        # Inbox IN-3: a `timeout` or `shutdown` leaves a "denied while you
        # were away" record (public `to_json` fields only), written before
        # the publish so the `inbox_changed` it causes can name the new row.
        # It never raises, and the broker has already settled the deny.
        inbox = getattr(self, "inbox", None)
        if inbox is not None:
            inbox.approval_ended(request, resolution)
        if request.private:
            self.bus.publish({"kind": "approval_resolved", "thread_id": request.thread_id,
                              "data": {"req_id": request.req_id, "code": request.code,
                                       "tool": request.tool, "hud_only": True,
                                       "decision": getattr(decision, "value", str(decision)),
                                       "resolution": resolution}})
            return
        self.bus.publish({"kind": "approval_resolved",
                          "thread_id": request.thread_id,
                          "data": {"req_id": request.req_id, "code": request.code,
                                   "tool": request.tool, "command": card_text(request.command),
                                   "origin": request.origin,
                                   "decision": getattr(decision, "value", str(decision)),
                                   "resolution": resolution}})

    def resolve_approval(self, req_id: str, decision, always: bool = False, *,
                         owner_verified: bool = False) -> dict:
        """`owner_verified` is HUD authority (PH-7): True only from the HUD's
        own answer routes and the phone's verified answer (`remote_answer`)."""
        try:
            entry = self.approvals.resolve(req_id, decision, always=always,
                                           owner_verified=owner_verified)
        except ValueError as exc:
            raise APIError(404, str(exc)) from exc
        return {"ok": True, "req_id": req_id,
                "decision": Decision(decision).value, "allowlisted": entry}

    def _record(self, session, event, *, idle=False):
        """One provider event into the thread's log, the ledger and the bus.
        `idle`: read between turns (SV-6) — it belongs to no turn, so its
        record carries no `turn_id` (a reader would otherwise take it for the
        last, finished turn's). A turn the CLI started by itself marks every
        record `self_started`."""
        if event.kind in (EventKind.SUBAGENT, EventKind.SUBAGENT_EVENT):
            self._record_subagent(session, event)
            return
        before = getattr(session, "turn_id", None)
        # Scrub reads credential files: never do this while holding _lock.
        log_fields = _log_fields(event.data)
        # A relayed turn's text for its sender's stream, scrubbed here too.
        progress = self.chat_relay.progress_text(session, event)
        tool_label = None
        if event.kind == EventKind.TOOL_STARTED:
            from .tool_calls import label
            data = event.data or {}
            try:
                tool_label = ("Browser action" if data.get("name") in ("hud_browser_act", "mcp__jarvis__hud_browser_act") else
                              label(data.get("name"), data.get("args"), session.brief.cwd))
            except Exception:
                from .approvals import clean_line
                tool_label = clean_line(data.get("name"), 40)
        with self._lock:
            if session.retired:
                return
            if event.thread_id != session.thread.id:
                raise ValueError("provider event belongs to another thread")
            thread = session.thread
            if (event.kind == EventKind.TOOL_STARTED and not idle
                    and thread.provider == ProviderName.CLAUDE):
                # C6: a spawning Agent call stamps its child (`_spawned`).
                # Display only, so it can never cost the record. The turn
                # check is defensive: TOOL_STARTED arrives only on the running
                # turn's worker, and turn_id changes only with no worker.
                try:
                    _spawned(session, event.data, running=session.turn_id == before)
                except Exception:
                    LOG.exception("C6 spawn stamp failed; the card shows no line")
            if event.kind == EventKind.USAGE:
                data = event.data
                usage = Usage(data.get("input", 0), data.get("output", 0),
                              data.get("cached", 0), data.get("cost_usd"))
                _validate(usage, Usage)
                # WP4 counters are cumulative, including saved turns. WP2 emits
                # increments. Keep this compatibility here until WP9 unifies it.
                if thread.provider == ProviderName.CODEX:
                    thread.tokens += max(0, usage.work_tokens - session.token_baseline)
                    session.token_baseline = max(session.token_baseline, usage.work_tokens)
                    if usage.cost_usd is not None:
                        thread.cost_usd += max(0, usage.cost_usd - session.cost_baseline)
                        session.cost_baseline = max(session.cost_baseline, usage.cost_usd)
                else:
                    thread.tokens += usage.work_tokens
                    thread.cost_usd += usage.cost_usd or 0
                # Claude's running total, saved so a resumed CLI (which
                # continues from its transcript's total) is measured from it.
                if "cost_total" in data:
                    running = running_cost.figure(data.get("cost_total"))
                    if running is not None:
                        thread.provider_cost_total = running
                thread.updated = utcnow()
                self.stores.threads.save(thread)
            record = {**to_json(event), "project_id": thread.project_id,
                      "turn_id": None if idle else session.turn_id,
                      "at": utcnow(), "event_id": uuid.uuid4().hex}
            if session.self_started and not idle:
                record["self_started"] = True
            if session.compacting and not idle:
                # The owner's Compact button: not a reply, and no surface
                # treats it as one (the runner, Discord, speech, unread).
                record["compaction"] = True
            if session.relay_mark is not None and not idle:
                # A turn another chat started, or its reply's: Discord,
                # speech and the task runner leave it alone (O-8).
                record["relayed"] = dict(session.relay_mark)
                self.chat_relay.on_event(session, event, progress)
            data=record.get("data") or {}
            if data.get("name") in ("hud_browser_act", "mcp__jarvis__hud_browser_act"):
                if event.kind==EventKind.TOOL_STARTED:
                    record["data"]={"call_id":data.get("call_id"),"name":"hud_browser_act","args":{}}
                elif event.kind==EventKind.TOOL_FINISHED:
                    from jarvis.tools import private_summary
                    summary=data.get("summary")
                    safe=summary if summary in ("acted","refused","asked","stopped") else private_summary("hud_browser_act",str(summary or ""))
                    record["data"]={"call_id":data.get("call_id"),"name":"hud_browser_act",
                        "ok":data.get("ok") is True,"outcome":data.get("outcome") if data.get("outcome") in ("ok","denied","failed") else "failed",
                        "summary":safe}
            if (event.kind == EventKind.APPROVAL_REQUESTED
                    and data.get("tool") in ("hud_browser_act", "mcp__jarvis__hud_browser_act")):
                record["data"] = {"req_id": data.get("req_id"), "tool": "hud_browser_act",
                                  "args": {}, "command": None}
            if (event.kind == EventKind.REVIEWER_DECLINED
                    and data.get("tool") in ("hud_browser_act", "mcp__jarvis__hud_browser_act")):
                record["data"] = {"tool": "hud_browser_act", "args": {},
                                  "command": None, "reason": "refused"}
            if tool_label is not None:
                record["data"] = {**record.get("data", {}), "label": tool_label}
            if event.kind == EventKind.QUESTION and (event.data or {}).get("req_id"):
                # The turn now waits on the owner: a message sent meanwhile
                # queues rather than steers (`_waiting_on_owner`).
                req_id = str(event.data["req_id"])
                session.questions.add(req_id)
                session.question_detail = {
                    **{k: v for k, v in session.question_detail.items() if k in session.questions},
                    req_id: {"text": event.data.get("text"), "options": event.data.get("options"),
                             "asked_at": record["at"]}}
            if event.kind == EventKind.USAGE:
                # Which model answered, per turn, in the durable record: a
                # chat thread can change model mid-conversation now (A1, A7).
                # A task thread runs what its brief named.
                brief = getattr(session, "brief", None)
                model, effort = (getattr(session, "applied", None)
                                 or (getattr(brief, "model", None), getattr(brief, "effort", None)))
                record["data"] = {**record.get("data", {}), "model": model, "effort": effort}
            if event.kind in GATE_KINDS:
                # Logged for the audit trail, never published: see GATE_KINDS.
                record["kind"] = GATE_KINDS[event.kind]
                self.stores.threads._append(thread.id, "log.jsonl", _logged(record, event.data, log_fields))
                return
            if event.kind != EventKind.TEXT_DELTA:
                # WP1 log() accepts text only; preserve full structured events
                # through its serialized append primitive, in the same log.
                # The log keeps credentials typed into a command withheld
                # (owner, 2026-10-15); the bus keeps the event as it came,
                # for the in-process consumers that act on it (the hatch
                # runs a declined command from it).
                self.stores.threads._append(thread.id, "log.jsonl", _logged(record, event.data, log_fields))
            if event.kind in (EventKind.USAGE, EventKind.ERROR):
                # Durable and synchronous, including chat. The stable id makes
                # the runner's subsequent ledger delivery idempotent.
                ledger = self.router.ledger
                before = ledger.totals(provider=thread.provider.value)
                old_quota = ledger.quota(thread.provider.value) if hasattr(ledger, "quota") else None
                partial = getattr(ledger, "partial_turns", None)
                old_partial = partial(provider=thread.provider.value) if partial else None
                rows = len(ledger._rows)
                ledger.on_event(record)
                new_quota = ledger.quota(thread.provider.value) if hasattr(ledger, "quota") else None
                # A stopped turn's usage event changes no total, only whether
                # the totals are a lower bound: the HUD must hear of that too.
                if (before != ledger.totals(provider=thread.provider.value) or old_quota != new_quota
                        or (partial and old_partial != partial(provider=thread.provider.value))
                        or (event.kind == EventKind.ERROR and len(ledger._rows) != rows)):
                    self.bus.publish({"kind": "usage_updated", "data": {"provider": thread.provider.value}})
            self.bus.publish(record)

    def _record_subagent(self, session, event, *, idle=False):
        """A native sub-agent's record (SV-1): its own file and a words-free
        bus record, never the thread's log, the ledger or the turn's text.
        Cleaned and labelled outside the lock (the scrub and a tool's label
        may read credential files), as the parent's calls are. `idle`: read
        between turns, so it belongs to no turn and stamps no relay (C6)."""
        from .subagents import SubagentRecords
        before = getattr(session, "turn_id", None)
        prepared = SubagentRecords.prepare(event, getattr(session.brief, "cwd", None))
        with self._lock:
            if session.retired:
                return
            if event.thread_id != session.thread.id:
                raise ValueError("provider event belongs to another thread")
            thread, turn_id, key = session.thread, session.turn_id, session.subagent_key
            if prepared is not None:
                # Only the turn the record was read in may stamp it: not one
                # that began while it was prepared outside the lock.
                _stamp_child(session, prepared, running=not idle and session.turn_id == before)
        if prepared is not None:
            self.subagents.record(thread, turn_id, key, prepared)

    def _finish(self, session, event):
        with self._lock:
            if session.retired:
                return
            if event.thread_id != session.thread.id:
                raise ValueError("provider event belongs to another thread")
            if not session.compacting:
                session.thread.turns += 1      # a compaction is not a turn
            session.thread.updated = utcnow()
            session.thread.provider_session_id = session.handle.provider_session_id
            self.stores.threads.save(session.thread)
            # The turn is over, so none of its questions is open any more:
            # cleared before `turn_finished` is published, so whoever reads on
            # that record (the inbox's `inbox_changed`) sees them gone. The
            # worker's `finally` clears them again and publishes nothing.
            session.questions.clear()
            session.question_detail = {}
            self._record(session, event)

    @staticmethod
    def _provider_closed(session) -> bool:
        """Whether the provider closed this session itself (Codex closes its
        handle on any transport error; Claude and the fast path mark theirs).
        Read off the native handle, never assumed open."""
        closed = getattr(getattr(session.handle, "native", None), "closed", False)
        try:
            return bool(closed.is_set()) if hasattr(closed, "is_set") else closed is True
        except Exception:
            return False

    def _desk_images(self, session, worker, turn_id, slot):
        """Pin the applied native model after a switch, for this exact turn."""
        from .router import cli_images
        def current():
            return (self._sessions.get(session.thread.id) is session
                    and session.worker is worker and session.turn_id == turn_id
                    and session.desk.slot is slot and not session.cancelled.is_set()
                    and not session.closing and not session.retired and not session.lost
                    and not self._stopping)
        with self._lock:
            if not current():
                return
            applied = session.applied
            model = applied[0] if applied else None
            provider = session.thread.provider.value
        capable = cli_images(provider, model)
        with self._lock:
            if current() and session.applied == applied:
                session.desk.images(slot, turn_id, capable)

    def _turn(self, session, message, *, compact=False):
        """A turn's worker. `message` None is a turn the CLI started by
        itself (SV-6, `_begin_cli`): read through the provider's `adopt`,
        with no model switch and no desk; everything else is the same.
        `compact` (with `message` None) is the owner's Compact button
        (`_begin_compact`): read through the provider's `compact`; its
        reason is scrubbed before anything records it, and it ends with a
        `compacted` system line."""
        # Wait for _begin to finish the start hook before this turn can end.
        with self._lock:
            terminal = None
            worker, turn_id, desk_slot = session.worker, session.turn_id, session.desk.slot
            drive_turn = session.drive_turn
            relay_item, relay_mark = session.relay_turn, session.relay_mark
        failed = False
        fatal = False
        last_error = None
        # Who is calling, on this turn's own thread (a fresh context): an
        # in-process provider reads it as the turn starts (the fast path
        # hands it to its worker), so a tool knows its thread and turn.
        from jarvis import runtime
        runtime.bind_caller(session.thread.id, session.thread.project_id,
                            session.thread.provider.value, session.turn_id)
        try:
            if session.cancelled.is_set():
                return
            if message is not None:
                self._apply_choice(session)
                self._desk_images(session, worker, turn_id, desk_slot)
                source = session.provider.send(session.handle, message)
            elif compact:
                source = session.provider.compact(session.handle)
            else:
                source = session.provider.adopt(session.handle)
            cancel_forwarded = False
            # Closed the moment the loop ends (a break, a raise), never left
            # suspended until this worker returns: the provider's `finally`
            # releases its turn slot, its reader and its steer waiters, and a
            # queued turn `_drain` starts from below must find them free
            # (review of #234, HIGH-1).
            with (contextlib.closing(source) if callable(getattr(source, "close", None))
                  else contextlib.nullcontext()):
                for event in source:
                    if event.thread_id != session.thread.id:
                        raise ValueError("provider event belongs to another thread")
                    # Providers may clear an old stop flag when send begins. Repeat
                    # cancellation after their first yield to close that start race.
                    if session.cancelled.is_set() and not cancel_forwarded:
                        session.provider.interrupt(session.handle)
                        cancel_forwarded = True
                    if event.kind == EventKind.TURN_FINISHED:
                        if compact and (event.data or {}).get("reason") is not None:
                            # Scrubbed before anything records it: neither the
                            # log nor the bus ever carries it raw.
                            event = Event(event.kind, event.thread_id, {
                                **event.data, "reason": compact_reason(event.data["reason"])})
                        terminal = event
                        break
                    if compact and event.kind == EventKind.ERROR:
                        last_error = (event.data or {}).get("message")
                    if event.kind == EventKind.ERROR:
                        with self._lock:
                            if session.drive_turn is drive_turn:
                                self._retire_drive(session)
                    self._record(session, event)
                    failed |= event.kind == EventKind.ERROR
                    fatal |= event.kind == EventKind.ERROR and bool((event.data or {}).get("fatal"))
        except Exception as exc:
            failed = True
            last_error = f"{type(exc).__name__}: {exc}"
            try:
                self._record(session, Event(EventKind.ERROR, session.thread.id,
                                           {"message": f"{type(exc).__name__}: {exc}", "fatal": False}))
            except Exception:
                LOG.exception("Cannot record turn error for %s", session.thread.id)
        finally:
            with self._lock:
                session.desk.end()
                if session.drive_turn is drive_turn:
                    self._retire_drive(session)
            # Steered messages the provider took but never delivered (the fast
            # path's final answer came first): they run next, as turns of their
            # own — unless the owner stopped this one.
            self._dev_hook("turn_ended", session.thread.id)
            returned = []
            undelivered = getattr(session.provider, "undelivered", None)
            if callable(undelivered) and session.handle is not None:
                try:
                    returned = list(undelivered(session.handle) or ())
                except Exception:  # noqa: BLE001
                    LOG.warning("Cannot read undelivered steers on %s", session.thread.id,
                                exc_info=True)
            with self._lock:
                leftover = [item for item in session.steered
                            if any(item.message is message for message in returned)]
                # How many owner messages run next: the sidebar keeps the
                # thread working across the gap rather than flashing unread.
                upcoming = 0 if session.closing or self._stopping else (
                    len(self._queues.get(session.thread.id) or ())
                    + len(self._inbox_queues.get(session.thread.id) or ())
                    + (0 if session.owner_stopped else len(leftover))
                    + self.chat_relay.pending(session.thread.id))
            stop = "end"
            try:
                stop = "interrupted" if session.cancelled.is_set() else "error" if failed else "end"
                if terminal is None:
                    terminal = Event(EventKind.TURN_FINISHED, session.thread.id, {"stop": stop})
                elif session.cancelled.is_set():
                    terminal = Event(terminal.kind, terminal.thread_id, {**terminal.data, "stop": "interrupted"})
                if upcoming:
                    terminal = Event(terminal.kind, terminal.thread_id, {**terminal.data, "next": upcoming})
                self._finish(session, terminal)
            except Exception:
                LOG.exception("Cannot finish thread %s", session.thread.id)
            try:
                # Before the worker is cleared: a relayed turn's reply goes
                # back to its sender (chat_relay.py, §5.3), and a sending
                # turn's budget ends with it.
                stop = (terminal.data or {}).get("stop", stop) if terminal is not None else stop
                self.chat_relay.turn_ended(session, turn_id, relay_item, relay_mark, stop, failed)
            except Exception:
                LOG.exception("Cannot pass on the relay of thread %s", session.thread.id)
            finally:
                line = None
                if compact:
                    # Outside the lock: the reason's scrub reads credential files.
                    try:
                        line = self._compaction_line(session, terminal, last_error)
                    except Exception:  # noqa: BLE001
                        LOG.exception("Cannot say how the compaction of %s ended",
                                      session.thread.id)
                        line = (False, COMPACT_FAILED + "no reason was given")
                with self._lock:
                    session.worker = None
                    session.running_since = None
                    session.self_started = False
                    session.compacting = False
                    session.questions.clear()
                    session.question_detail = {}
                    if line is not None and not session.retired:
                        # After the turn's turn_finished, before anything
                        # waiting runs: the chat's system line.
                        record = {"kind": "compacted", "at": utcnow(), "turn_id": turn_id,
                                  "thread_id": session.thread.id,
                                  "project_id": session.thread.project_id,
                                  "compaction": True, "event_id": uuid.uuid4().hex,
                                  "data": {"ok": line[0], "text": line[1]}}
                        self.stores.threads._append(session.thread.id, "log.jsonl", record)
                        self.bus.publish(dict(record))
                    if leftover:
                        if session.owner_stopped:
                            self._drop_waiting(session.thread, "stopped", leftover)
                        else:
                            waiting = self._queues.setdefault(session.thread.id, deque())
                            for item in reversed(leftover):
                                self._requeue(session.thread, item, waiting, front=True)
                    # Chat replies steered into this turn that it never took
                    # run as reply turns of their own; never the owner's queue.
                    self.chat_relay.handed_back(session, returned)
                    session.relay_turn = session.relay_mark = None
                    drain = self._waiting(session.thread.id) and not session.closing
                    # A fatal turn error, or a provider that closed the session
                    # under us (Codex does on any RpcError), strands the
                    # session: every later send would answer "session is
                    # closed" until a restart. Drop it, so the next message
                    # resumes it and `_run_brief` re-applies the model choice;
                    # Claude resumes its CLI session by id. A provider whose
                    # session outlives its own fatal errors says so
                    # (`keeps_session_on_error`, the fast path: its transcript
                    # is saved only when a turn returns, so a drop would cost
                    # the model the failed turn's message and completed tool
                    # steps) and is dropped only once it really closed.
                    keeps = getattr(session.provider, "keeps_session_on_error", False) is True
                    if (fatal and not keeps) or self._provider_closed(session):
                        session.lost = True
                        self._retire_drive(session)
                    if session.lost and self._sessions.get(session.thread.id) is session:
                        # The provider closed it; the next send resumes afresh.
                        self._sessions.pop(session.thread.id, None)
                    # A turn the CLI started by itself while this one ran goes
                    # next, before the owner's waiting messages: the CLI runs
                    # it first anyway (SV-6). Its own end drains the queue.
                    if self._maybe_cli(session):
                        drain = False
                if session.lost:
                    self._cleanup(session)
                if drain and not session.retired:
                    # The owner's waiting messages run next, in order — on a
                    # resumed session if this turn's was dropped.
                    try:
                        self._drain(session.thread.id)
                    except Exception:  # noqa: BLE001
                        LOG.exception("Cannot start the queued message on %s", session.thread.id)

    def _compaction_line(self, session, terminal, last_error) -> tuple[bool, str]:
        """How a compaction ended, as (ok, the system line). Its terminal's
        reason was scrubbed as it was read; an error's message is scrubbed
        here. No summary text is ever in it."""
        data = (terminal.data if terminal is not None else None) or {}
        stop = data.get("stop")
        if session.cancelled.is_set() or stop == "interrupted":
            return False, COMPACT_STOPPED
        if stop == "end" and data.get("compacted") is True:
            return True, COMPACT_OK
        reason = data.get("reason")
        if reason is None and last_error is not None:
            reason = compact_reason(last_error)
        reason = reason if isinstance(reason, str) and reason else "no reason was given"
        if stop == "end":
            return False, COMPACT_NOT_DONE + reason
        return False, COMPACT_FAILED + reason

    def _session(self, thread_id):
        self.require(self.stores.threads, thread_id)
        with self._lock:
            session = self._sessions.get(thread_id)
            if session is None or session.handle is None or session.closing:
                raise DaemonError("thread session is not open")
            return session

    def interrupt(self, thread_id, *, expected_turn_id=None):
        """Stop the current turn, or a specific acknowledged send's turn.

        A stale targeted stop changes nothing. Provider delivery runs outside
        the daemon lock, but a thread-scoped reservation prevents a successor
        from reusing its handle until delivery ends, even across a replacement
        session. Queued words are never dropped just because delivery is busy.
        """
        self.require(self.stores.threads, thread_id)
        with self._lock:
            session = self._sessions.get(thread_id)
            if expected_turn_id is not None and (session is None or session.closing
                    or session.worker is None or session.turn_id != expected_turn_id):
                return {"ok": False, "stale": True}
            if session is None or session.handle is None or session.closing:
                raise DaemonError("thread session is not open")
            if session.worker is None:
                raise DaemonError("no turn is running on this thread")
            if thread_id in self._interrupting:
                return {"ok": True}  # this turn's stop is already on its way
            session.cancelled.set()
            session.desk.end()
            self._retire_drive(session)
            session.owner_stopped = True
            if session.turn_id:
                session.stopped_turns.append(session.turn_id)
            self._drop_waiting(session.thread, "stopped")
            # Other chats' messages waiting here are dropped; this turn's own
            # still waiting elsewhere are withdrawn (chat_relay.py, §5.5).
            self.chat_relay.thread_stopped(session)
            provider, handle = session.provider, session.handle
            self._interrupting.add(thread_id)
        try:
            provider.interrupt(handle)
        finally:
            with self._lock:
                self._interrupting.discard(thread_id)
            self._drain(thread_id)
        return {"ok": True}

    # -- the owner's Stop on one native sub-agent (SV-4) ----------------------

    def _subagent_session(self, thread_id):
        """(provider, handle, key) of the thread's open session, or None."""
        with self._lock:
            session = self._sessions.get(thread_id)
            if (session is None or session.handle is None or session.closing
                    or session.retired):
                return None
            return session.provider, session.handle, session.subagent_key

    def subagent_stop_offers(self, thread_id, rows):
        """The list route's rows, each live one saying whether the owner's
        Stop can be sent to it now (`stoppable`) and, when not, why
        (`stop_unavailable`, one plain sentence). Asks the provider, which
        sends nothing; outside the daemon's lock."""
        from .approvals import clean_line
        from .subagents import LIVE
        live = self._subagent_session(thread_id)
        for row in rows:
            if row.get("status") not in LIVE:
                continue
            reason = None
            if live is None:
                reason = SUBAGENT_NO_SESSION
            else:
                provider, handle, key = live
                check = getattr(provider, "subagent_stop_refusal", None)
                if not callable(check) or not callable(getattr(provider, "stop_subagent", None)):
                    reason = SUBAGENT_STOP_UNSUPPORTED
                elif row.get("_key") is not None and row.get("_key") != key:
                    reason = SUBAGENT_NO_SESSION
                else:
                    try:
                        reason = check(handle, row["sid"])
                    except Exception:  # noqa: BLE001 — a check that fails offers nothing
                        reason = SUBAGENT_STOP_UNSUPPORTED
            row["stoppable"] = reason is None
            if reason is not None:
                row["stop_unavailable"] = clean_line(str(reason), 160)
        for row in rows:
            row.pop("_key", None)
        return rows

    def stop_subagent(self, thread_id, sid):
        """The owner's Stop on one native sub-agent (SV-4; the owner's
        2026-10-15 decisions). Only the HUD's owner route calls this: never
        the API listener, session MCP, Discord or the CLI.

        Asks the provider to stop that child alone (Claude `stop_task`, Codex
        `turn/interrupt` on the child's own thread) — never the thread's turn
        — and marks it "stopping" until the CLI's own word ends it.
        Idempotent: a Stop already on its way sends nothing more. Raises
        `SubagentStopRefused` (409, a plain sentence) when nothing can be
        sent, LookupError for an unknown child."""
        from .approvals import clean_line
        from .provider import SubagentNotLive, SubagentStopDeclined, SubagentStopUnsupported
        thread = self.require(self.stores.threads, thread_id)
        live = self._subagent_session(thread.id)
        if live is None:
            raise SubagentStopRefused(SUBAGENT_NO_SESSION, stale=True)
        provider, handle, key = live
        stopper = getattr(provider, "stop_subagent", None)
        outcome, token = self.subagents.begin_stop(thread, sid, key)
        if outcome == "unknown":
            raise LookupError("no such sub-agent on this thread")
        if outcome == "closed":
            raise SubagentStopRefused("Jarvis is stopping, so nothing more is sent to the CLI.", stale=True)
        if outcome in ("ended", "elsewhere"):
            raise SubagentStopRefused(
                "This sub-agent has already ended." if outcome == "ended" else SUBAGENT_NO_SESSION,
                stale=True)
        if outcome == "stopping":
            return {"ok": True, "stop": "stopping", "already": True}
        try:
            if not callable(stopper):
                raise SubagentStopUnsupported(SUBAGENT_STOP_UNSUPPORTED)
            stopper(handle, sid)
        except SubagentStopUnsupported as exc:
            self.subagents.stop_failed(thread, sid, token, unconfirmed=False)
            raise SubagentStopRefused(clean_line(str(exc), 200) or SUBAGENT_STOP_UNSUPPORTED,
                                      supported=False) from None
        except SubagentStopDeclined as exc:
            # The CLI's own answer: it got the request and turned it down.
            # Not "unconfirmed" — pressing Stop again would only be refused again.
            from .redact import scrub_text
            try:
                # As a child's words are (`subagents._words`): Jarvis's own
                # credential values, the known key shapes, tokens.
                reason = clean_line(scrub_text(str(exc)[:2000]), 120) or "no reason given"
            except Exception:  # noqa: BLE001 — an unscrubbable reason is not shown
                reason = "no reason given"
            self.subagents.stop_failed(thread, sid, token, refused=reason)
            raise SubagentStopRefused(f"The CLI refused the stop: {reason.rstrip('.')}.",
                                      refused=True) from None
        except SubagentNotLive as exc:
            self.subagents.stop_failed(thread, sid, token, unconfirmed=False)
            raise SubagentStopRefused(clean_line(str(exc), 200) or "This sub-agent has already ended.",
                                      stale=True) from None
        except Exception as exc:  # noqa: BLE001 — the CLI may still act on it: said so
            LOG.warning("Stopping sub-agent %s on %s: %s", sid, thread.id, type(exc).__name__)
            self.subagents.stop_failed(thread, sid, token, unconfirmed=True)
            raise SubagentStopRefused(
                "The stop was sent, but the CLI did not confirm it took it; the sub-agent "
                "may still stop. Press Stop again to retry.", unconfirmed=True) from None
        self.subagents.stop_sent(thread, sid, token)
        return {"ok": True, "stop": "stopping"}

    def answer(self, thread_id, req_id, decision, *, via: str | None = None):
        session = self._session(thread_id)
        session.provider.answer(session.handle, req_id, decision)
        with self._lock:
            session.questions.discard(str(req_id))
        # In the log too, beside the question it closes, so a surface catching
        # up from the log (the Discord mirror) knows the question is no
        # longer open. Nothing the provider sees. `via` names where it was
        # answered when that was not the HUD ("phone", P-4).
        data = {"req_id": req_id}
        if via in VIAS:
            data["via"] = via
        try:
            self.stores.threads._append(thread_id, "log.jsonl", {
                "kind": "question_answered", "at": utcnow(), "thread_id": thread_id,
                "turn_id": session.turn_id, "data": data})
        except Exception:
            LOG.warning("Cannot log the answer on thread %s", thread_id, exc_info=True)
        # Whoever answered (the HUD, Discord), every surface still showing the
        # question as open hears that it is not (PR C).
        self.bus.publish({"kind": "question_answered", "thread_id": thread_id,
                          "project_id": session.thread.project_id,
                          "data": {"req_id": req_id}})

    def _cleanup(self, session):
        # First, before the provider is touched: the session's jarvis-mcp
        # token dies with it (close, archive, delete, a lost session — plan
        # §1.4). The provider's close then removes its carrier.
        with self._lock:
            session.desk.end()
            self._retire_drive(session)
        self.callers.revoke(session)
        self._dev_hook("session_closed", session.origin_tag)
        for operation in (session.provider.interrupt, session.provider.close):
            try:
                operation(session.handle)
            except Exception:
                LOG.exception("Provider cleanup failed for %s", session.thread.id)
        # The provider's close ends its `watch` (SV-6): the watcher goes with
        # the session, bounded, never waited on by itself.
        watcher = session.watcher
        if watcher is not None and watcher is not threading.current_thread():
            watcher.join(WATCH_JOIN_S)
            if watcher.is_alive():
                LOG.warning("The between-turn reader of %s is still running", session.thread.id)
        try:
            self.subagents.end_session(session.thread, session.subagent_key)
        except Exception:
            LOG.warning("Cannot end the sub-agents of %s", session.thread.id, exc_info=True)

    def close_thread(self, thread_id):
        session = self._session(thread_id)
        with self._lock:
            session.closing = True
            session.cancelled.set()
            session.desk.end()
            self._retire_drive(session)
            worker = session.worker
        cleanup = threading.Thread(target=self._cleanup, args=(session,), daemon=True)
        cleanup.start()
        deadline = time.monotonic() + STOP_TIMEOUT
        for job in (cleanup, worker):
            if job is not None:
                job.join(max(0, deadline - time.monotonic()))
        with self._lock:
            if cleanup.is_alive() or (worker and worker.is_alive()):
                raise DaemonError("thread is still closing")
            self._sessions.pop(thread_id, None)
            self._drop_waiting(session.thread, "the thread was closed")
            self.chat_relay.thread_closed(thread_id, "the chat was closed")
        self._lifecycle("thread_closed", session.thread)

    def _lifecycle(self, kind, obj):
        record = {"kind": kind, "project_id": obj.project_id, "data": to_json(obj)}
        record["thread_id" if isinstance(obj, Thread) else "task_id"] = obj.id
        self.bus.publish(record)

    def _await_announced(self, req_ids: set, limit: float) -> None:
        """Until each of `req_ids` is in the broker's `decisions` (appended by
        `_record` after `_announce`, so after `approval_resolved` was
        published), or `limit` seconds pass. A broker without that log has
        nothing to wait for."""
        decisions = getattr(self.approvals, "decisions", None)
        if not req_ids or not isinstance(decisions, list):
            return
        until = time.monotonic() + limit
        while time.monotonic() < until:
            if req_ids <= {d.get("req_id") for d in list(decisions) if isinstance(d, dict)}:
                return
            time.sleep(0.01)
        LOG.warning("%d approval waiter(s) had not announced their denial at shutdown",
                    len(req_ids - {d.get("req_id") for d in list(decisions) if isinstance(d, dict)}))

    def release_approvals(self, limit: float = RELEASE_WAIT_S) -> int:
        """Deny every pending ask now, with resolution "shutdown", and wait
        (bounded by `limit`) until each has been announced. -> how many were
        released by this call.

        Every pending ask is denied now, not when its timer runs out or the
        process dies: each waiter returns DENY with resolution "shutdown"
        (which leaves an inbox "denied while you were away" record, IN-3) and
        its card is cleared. Outside `self._lock`: the broker's resolve
        callback publishes. Each released waiter announces on its own thread,
        so wait until every one has been recorded by the broker: its
        `approval_resolved` then reached the bus, so every window clears its
        card and the Discord surface (still running, when `main()` calls this
        first) strips the post's buttons and says "Jarvis stopped first".

        Idempotent: the broker's `shutdown` is, and a second call finds
        nothing pending. A later ask is denied at once."""
        shutdown = getattr(self.approvals, "shutdown", None)
        if not callable(shutdown):
            return 0
        self.approvals_released.set()
        try:
            released = {r.req_id for r in self.approvals.pending() if r.req_id}
        except Exception:  # noqa: BLE001 — nothing to wait for then
            released = set()
        shutdown()
        self._await_announced(released, limit)
        return len(released)

    def stop(self):
        # Before anything else closes: no jarvis-mcp call is answered for a
        # session from here on (plan §1.4).
        self.callers.revoke_all()
        # Then every pending ask is denied, before the bus closes further
        # down. `main()` has normally done this already, before Discord
        # stopped; then this finds nothing (idempotent).
        self.release_approvals()
        self.schedules.stop()
        self.inbox.prs.stop()                   # the inbox's PR poll (B3): a running gh is killed
        titles = self.titles
        if titles is not None:
            titles.stop()                       # its Codex session ends with the metadata cancel
        reviews = getattr(self, "diff_reviews", None)
        if reviews is not None:
            reviews.stop()                      # quick reviews (diff_review.py): cancelled, none starts
        deadline = time.monotonic() + STOP_TIMEOUT
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
            # Terminals end with the daemon (decision W-3): every window is
            # told and every session gets SIGHUP now; what outlives it is
            # SIGKILLed below, before the listeners close.
            self.terminals.uninstall()
            self.terminals.hangup_all()
            # No chat reads another once shutdown starts.
            self.chat_relay.stop()
            # Nor posts to the owner.
            self.owner_posts.stop()
            sessions = list(self._sessions.values())
            # The queue is in memory; what waits on a turn at shutdown says so
            # in the log rather than reading as still waiting after a restart —
            # every thread's, including one whose session a fatal turn dropped
            # while its queue waited to resume it.
            for thread_id in sorted({t for t, waiting in self._queues.items() if waiting}
                                    | {t for t, waiting in self._inbox_queues.items() if waiting}):
                try:
                    live = self._sessions.get(thread_id)
                    thread = live.thread if live is not None else self.stores.threads.get(thread_id)
                    if thread is not None:
                        self._drop_waiting(thread, "Jarvis stopped")
                except Exception:  # noqa: BLE001
                    LOG.warning("Cannot record dropped messages on %s", thread_id)
            jobs = [s.worker for s in sessions if s.worker is not None]
            for session in sessions:
                session.closing = True
                session.cancelled.set()
                session.desk.end()
                self._retire_drive(session)
                if session.handle is not None:
                    job = threading.Thread(target=self._cleanup, args=(session,), daemon=True)
                    job.start()
                    jobs.append(job)
        self._stop_codex_metadata(deadline)
        if titles is not None and not titles.join(min(3.0, max(0, deadline - time.monotonic()))):
            LOG.warning("Chat titler still running at shutdown; it writes nothing now")
        for job in jobs:
            job.join(max(0, deadline - time.monotonic()))
        with self._lock:
            for session in sessions:
                session.retired = True
            self._sessions.clear()
        self.subagents.close()
        self.terminals.finish_all(max(deadline, time.monotonic() + 0.2))
        self.devservers.stop()
        self.vex.stop()
        # The Browser's Chrome, its driver and its gate (BR-1a): after the
        # terminals, before the listeners close. Its own bound, past the
        # daemon's: a Chrome that will not close is killed by exact PID.
        # Before Chrome goes: its streams are put back to their volume while
        # they still exist (a lost one is repaired from the journal later).
        self.speech_ducker.stop()
        self.browser_drive.stop()
        self.browser_look.stop()
        self.browser_panes.stop()
        self.browser.stop()
        self.bus.close()
        for server, worker in self._listeners:
            server.shutdown()
            server.server_close()
            worker.join(max(0, deadline - time.monotonic()))
        self._listeners.clear()
        self._server = None
        self.secure.stop()
        self.remote_port = None
        self.remote_auth.server = None
        # A revocation whose write failed gets one last try (and an ERROR
        # line naming the passkey if it still cannot be written).
        try:
            self.remote_auth.final_save()
        except Exception:     # noqa: BLE001 — never why a stop fails
            LOG.exception("phone: the last save of an unsaved revocation failed")
        if any(job.is_alive() for job in jobs):
            LOG.warning("Shutdown bound reached; provider work remains on daemon threads")

    def _stop_codex_metadata(self, deadline: float) -> None:
        """Shutdown for the background Codex models/quota refresh (PR #20
        re-review): the provider's app-server in flight is cancelled, the
        ledger stops saving the catalog or swapping the table, and the worker
        is waited for within the stop bound. In that order: the ledger is
        stopped first, so a read the cancel cuts short cannot land after."""
        ledger = getattr(getattr(self, "router", None), "ledger", None)
        stop = getattr(ledger, "stop_codex", None)
        if callable(stop):
            stop(0)
        codex = self.providers.get(ProviderName.CODEX)
        cancel = getattr(codex, "cancel_metadata", None)
        if callable(cancel):
            try:
                cancel()
            except Exception as exc:
                LOG.warning("Codex metadata cancel failed (%s)", type(exc).__name__)
        wait = getattr(ledger, "wait_codex_refresh", None)
        if callable(wait):
            from .providers.codex import METADATA_DEADLINE_S
            if not wait(min(METADATA_DEADLINE_S + 2, max(0.0, deadline - time.monotonic()))):
                LOG.warning("Codex metadata refresh still running at shutdown; it saves nothing")


def _object(value, allowed, required=()):
    if not isinstance(value, dict):
        raise APIError(400, "body must be a JSON object")
    if value.keys() - set(allowed):
        # The count, never the names: they are the client's own text, and an
        # APIError's message is logged (review of PR #69).
        raise APIError(400, f"unknown fields ({len(value.keys() - set(allowed))})")
    if set(required) - value.keys():
        raise APIError(400, "missing fields: " + ", ".join(sorted(set(required) - value.keys())))
    return value


def _no_channel_field(body):
    """`POST`/`PATCH /projects` no longer take a channel (B1): a link is
    validated against Discord and made by the owner-only channel routes."""
    if isinstance(body, dict) and ("discord_channel_id" in body
                                   or "discord_channel_origin" in body):
        raise APIError(400, "link a channel from the project dialog")
    return body


def _routing_models(project, before=None):
    """A project's per-role model overrides name only models Jarvis knows for
    that CLI, the same rule `routing.json` and `/route` follow (400 if not).
    On a PATCH, `before` is the stored project: an entry it already holds
    unchanged is not re-judged, so a stale one round-trips."""
    from .router import check_project_models
    try:
        check_project_models(project.routing.models,
                             before.routing.models if before is not None else None)
    except ValueError as exc:
        raise APIError(400, str(exc)) from exc


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise APIError(400, f"{name} must be a nonempty string")
    return value


def task_verb_body(verb: str, body) -> dict:
    """The body of `POST /tasks/{id}/<verb>` (§11.2), checked and parsed —
    400 for any other shape. `start` and `cancel` take `{}`; `steer`
    `{text, spoken?}`; `resume` `{provider?}`; `answer` exactly `{index,
    text}`, `index` being the question's place in `spec.questions`
    (`control.answer_refusal`). The route and the HUD's test mock both call
    this, so the mock cannot take a body the daemon refuses: it took anything,
    and the HUD sent `{question, answer}` to `/answer` for weeks (2026-10-11).
    A provider that is not a `ProviderName` raises ValueError, which the
    dispatcher answers 400."""
    if verb in ("start", "cancel"):
        _object(body, ())
        return {}
    if verb == "steer":
        _object(body, ("text", "spoken"), ("text",))
        spoken = body.get("spoken", False)
        if not isinstance(spoken, bool):
            raise APIError(400, "spoken must be a boolean")
        return {"text": _text(body["text"], "text"), "spoken": spoken}
    if verb == "resume":
        _object(body, ("provider",))
        provider = body.get("provider")
        return {"provider": ProviderName(provider) if provider else None}
    if verb == "answer":
        _object(body, ("index", "text"), ("index", "text"))
        index = body["index"]
        if not isinstance(index, int) or isinstance(index, bool):
            raise APIError(400, "index must be an integer")
        return {"index": index, "text": _text(body["text"], "text")}
    raise APIError(404, "route not found")


def _log_path(path: str) -> str:
    """A request path for the daemon log. A terminal attach carries its
    ticket in the query string, so `/terminals` paths are logged without one;
    so are `/mcp` paths, whose credential is a header the relay never moves
    into a query — but a confused client could (PR #35 review) — and
    `/browser` paths, which take no query but must never log a URL that a
    confused client put in one (BR-1a)."""
    url = urlsplit(path)
    if url.path.startswith("/dev-servers"):
        return "/dev-servers"               # even malformed ids and queries are client text
    if url.path == "/events":
        return "/events"                   # window nonce belongs only to memory
    return url.path if url.path.startswith(("/terminals", "/mcp", "/browser")) else path


def _handler(daemon):
    class Handler(BaseHTTPRequestHandler):
        # No CORS: the preview origin must never invoke this approval surface.
        # And no framing (WP-E): every response on the HUD and API listeners
        # carries `frame-ancestors 'none'` and `X-Frame-Options: DENY`, added
        # here in `end_headers` — the one place every status line we send
        # passes through (JSON, files, SSE, errors, a malformed request's
        # 400), so no route can forget them. `/status` says so (`frame_hardened`).
        frame_hardened = True

        def log_message(self, *args):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(2)
            self._streaming = False
            self._csp = None
            self._wasm = False

        def send_response(self, code, message=None):
            self._csp = None        # a response's own CSP directives (hud_api.binary)
            self._wasm = False      # a worker script's `'wasm-unsafe-eval'` (hud_api.binary)
            # A request too malformed for http.server to learn its version
            # (a garbage line, HTTP/9.9) — or a real HTTP/0.9 one, which the
            # Host check refuses — is answered HTTP/0.9-style: a body with no
            # status line and so no headers at all. Answer it as 1.0, so even
            # that refusal carries the frame headers.
            if self.request_version == "HTTP/0.9":
                self.request_version = "HTTP/1.0"
            super().send_response(code, message)

        def end_headers(self):
            from .hud_api import frame_headers
            for name, value in frame_headers(self, self._csp, wasm=self._wasm):
                self.send_header(name, value)
            super().end_headers()

        def send_error(self, code, message=None, explain=None):
            self._json(code, {"error": message or self.responses.get(code, ("HTTP error",))[0]})

        def _json(self, status, payload):
            data = json.dumps(payload, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _raw_body(self):
            if self.headers.get("Transfer-Encoding"):
                raise APIError(400, "chunked bodies are not supported")
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0:
                raise APIError(400, "invalid body length")
            if length > 48 * 1024 * 1024:
                raise APIError(413, "request body exceeds 48 MiB")
            data = self.rfile.read(length)
            if len(data) != length:
                raise APIError(400, "incomplete request body")
            return data

        def _body(self):
            data = self._raw_body()
            value = json.loads(data) if data else {}
            return _object(value, value.keys() if isinstance(value, dict) else ())

        def _dispatch(self):
            try:
                from .hud_api import check_origin
                check_origin(self, preview=getattr(self.server, "preview_only", False))
                status, payload = self._route()
                if not self._streaming:
                    self._json(status, payload)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:
                if self._streaming:
                    return
                if isinstance(exc, APIError):
                    status = exc.status
                    LOG.warning("%s %s -> %d %s", self.command, _log_path(self.path), status, exc)
                elif isinstance(exc, (DaemonError, BriefRefused, worktrees.WorktreeError, ProjectArchived)):
                    status = 409
                elif isinstance(exc, (FileNotFoundError, LookupError)) and not isinstance(exc, KeyError):
                    status = 404
                elif isinstance(exc, PermissionError):
                    status = 403
                elif isinstance(exc, (ValueError, TypeError, KeyError, TimeoutError)):
                    status = 400
                else:
                    status = 409
                    LOG.exception("HTTP %s %s failed", self.command, _log_path(self.path))
                try:
                    from jarvis.tools.secrets import scrub
                    # Only intentional, bounded API errors are reflected. JSON
                    # decoder/provider exceptions may contain request secrets.
                    message = str(exc) if isinstance(exc, (APIError, DaemonError, BriefRefused, ControlError, ProjectArchived)) else "request failed (" + type(exc).__name__ + ")"
                    self._json(status, {"error": scrub(message)})
                except OSError:
                    pass

        do_GET = do_POST = do_PATCH = do_DELETE = do_PUT = do_OPTIONS = do_HEAD = _dispatch

        def _route(self):
            url = urlsplit(self.path)
            parts = url.path.strip("/").split("/")
            query = parse_qs(url.query, keep_blank_values=True)
            if any(len(v) != 1 for v in query.values()):
                raise APIError(400, "duplicate query parameter")
            query = {k: v[0] for k, v in query.items()}
            method = self.command
            stores = daemon.stores
            from . import hud_api
            # The phone's paths belong to the remote listener alone
            # (remote_listener.py, PH-1): never the HUD, API or preview one.
            if parts[0] == "m":
                raise APIError(404, "route not found")
            if getattr(self.server, "preview_only", False):
                return hud_api.preview_route(self, daemon, parts, query)
            # jarvis-mcp for chat sessions (bridge.py): the API listener only,
            # a caller token only; 404 on the HUD listener.
            from . import bridge
            mounted = bridge.route(self, daemon, parts, query)
            if mounted is not None:
                return mounted
            mounted = hud_api.route(self, daemon, parts, query)
            if mounted is not None:
                return mounted
            # The owner inbox: owner-only on every path (inbox.route).
            from . import inbox as _inbox
            mounted = _inbox.route(self, daemon, parts, query)
            if mounted is not None:
                return mounted
            # The phone's pairing window and paired passkeys: owner-only on
            # every path (remote_auth.hud_route, phone design PH-6).
            from . import remote_auth as _remote_auth
            mounted = _remote_auth.hud_route(self, daemon, parts, query)
            if mounted is not None:
                return mounted
            if parts == ["route"] and method in ("GET", "POST"):
                from .router import daemon_router
                router = daemon_router(daemon)
                if method == "GET":
                    _object(query, ("project",))
                    return 200, router.view(query.get("project"))
                _object(query, ())
                with daemon._lock:
                    daemon._active()
                    return 200, router.configure(self._body())
            if method == "GET" and parts == ["status"]:
                return 200, daemon.status()
            if method == "GET" and parts == ["events"]:
                _object(query, ("thread", "project", "window"))
                filters = {}
                for name, store in (("thread", stores.threads), ("project", stores.projects)):
                    if name in query:
                        daemon.require(store, query[name])
                        filters[name + "_id"] = query[name]
                from .projects import is_owner
                from .devservers import valid_window
                window = query.get("window") if is_owner(self, daemon) else None
                if window is not None and not valid_window(window):
                    raise APIError(400, "invalid window")
                self._events(filters, window)
                return 200, None
            if parts == ["projects"]:
                if method == "GET":
                    # Archived projects are hidden from every list (decisions B1);
                    # the HUD's Archive view reads them from /archive.
                    # `root_real` beside each root: where it resolves, which a
                    # File pane's tree finds a file's `abs` under (#50).
                    from .projects import project_view
                    return 200, [project_view(p) for p in safe_list(stores.projects) if not p.archived]
                if method == "POST":
                    body = _no_channel_field(self._body())
                    body = _object(body, ("name", "root", "profile", "routing", "extra_dirs",
                                          "always_ask"), ("name", "root"))
                    # One create path for the HUD and Discord's `/project new`
                    # (B2): a colliding name is numbered, never refused.
                    from .projects import create_project, project_view
                    values = dict(body)
                    created = create_project(daemon, values.pop("name"), values.pop("root"),
                                             **values)
                    return 201, project_view(created)
            if len(parts) == 2 and parts[0] == "projects":
                with daemon._lock:
                    project = daemon.require(stores.projects, parts[1])
                    if method == "GET":
                        from .projects import project_view
                        return 200, project_view(project)
                    if method == "PATCH":
                        daemon._active()
                        body = _no_channel_field(self._body())
                        body = _object(body, ("name", "root", "profile", "routing", "extra_dirs",
                                              "always_ask"))
                        before = project
                        project = from_json(Project, {**to_json(project), **body})
                        # What a project's threads may do and where (BR-0): a
                        # change to its permission profile, always-ask list,
                        # extra folders or root is the owner's, from the HUD
                        # window — never any client of the API listener, which
                        # could otherwise loosen what asks it. A field sent
                        # back unchanged changes nothing and needs no owner.
                        guarded = [k for k in _OWNER_PROJECT_FIELDS
                                   if k in body and to_json(project).get(k) != to_json(before).get(k)]
                        if guarded:
                            from .projects import owner_only
                            owner_only(self, daemon)
                        _validate(project, Project)
                        if "routing" in body:
                            # Only when written: a project saved before the
                            # check must stay renameable. Only what changed
                            # is a new choice: an unchanged entry the Codex
                            # catalog has since dropped round-trips.
                            _routing_models(project, before)
                        _text(project.name, "name")
                        if not Path(project.root).is_absolute():
                            raise APIError(400, "root must be absolute")
                        if project.inbox and ("name" in body or "root" in body):
                            raise DaemonError("inbox name and root cannot change")
                        from . import projects as edits
                        edits.refuse_archived_project(before)
                        if edits.name_key(project.name) != edits.name_key(before.name):
                            # Numbered only when the name really changes, so the
                            # owner's existing duplicates stay editable (B4).
                            project.name = edits.unique_name(
                                project.name, edits.project_names_taken(stores, but=project.id))
                        else:
                            project.name = project.name.strip() or before.name
                        if project.root != before.root:
                            if not Path(project.root).is_dir():
                                raise APIError(400, "root must be an existing directory")
                            # Tasks already under way keep the root they were
                            # started under (B5): pin it before it changes.
                            from .stores import _lock as store_lock
                            for listed in safe_list(stores.tasks, project_id=project.id):
                                with store_lock:            # a fresh read, never a stale copy
                                    task = stores.tasks.get(listed.id)
                                    if task is not None and task.root is None and task.worktree:
                                        task.root = before.root
                                        stores.tasks.save(task)
                        changed = sorted(k for k, v in to_json(project).items()
                                         if to_json(before).get(k) != v)
                        if changed:
                            stores.projects.save(project)
                            # `by` (B1, decisions D4): the owner's own edit
                            # moves the Discord channel at once; anything else
                            # (`api`) asks through the approval gate first.
                            by = "owner" if edits.is_owner(self, daemon) else "api"
                            previous = {k: to_json(before).get(k) for k in changed}
                            daemon.bus.publish({"kind": "project_updated", "project_id": project.id,
                                                "data": {"project_id": project.id, "changed": changed,
                                                         "project": to_json(project), "by": by,
                                                         "previous": previous}})
                        return 200, edits.project_view(project)
            if parts in (["threads"], ["tasks"]):
                store = stores.threads if parts[0] == "threads" else stores.tasks
                if method == "GET":
                    _object(query, ("project",))
                    filters = {}
                    if "project" in query:
                        daemon.require(stores.projects, query["project"])
                        filters["project_id"] = query["project"]
                    # Archived threads, and everything in an archived project,
                    # are hidden here too (decisions B1).
                    from .projects import archived_project_ids
                    hidden = archived_project_ids(stores)
                    if parts[0] == "threads":
                        return 200, [daemon.thread_json(t) for t in safe_list(store, **filters)
                                     if not t.archived and t.project_id not in hidden]
                    return 200, [to_json(obj) for obj in safe_list(store, **filters)
                                 if obj.project_id not in hidden]
                if method == "POST" and parts[0] == "threads":
                    # provider and brief are optional: a chat thread defaults to
                    # the fast path with an empty brief (the HUD's New Thread
                    # sent only project_id + role and got a silent 400 — found
                    # live 2026-09-16). Other roles still need a provider.
                    body = _object(self._body(), ("project_id", "role", "provider", "brief"),
                                   ("project_id", "role"))
                    body.setdefault("brief", {})
                    if "provider" not in body:
                        if body["role"] != "chat":
                            raise APIError(400, "missing fields: provider")
                        body["provider"] = "fast"
                    return 201, daemon.thread_json(daemon.open_thread(**body))
                if method == "POST":
                    body = _object(self._body(), ("project_id", "brief"), ("project_id", "brief"))
                    daemon.require(stores.projects, body["project_id"])
                    _text(body["brief"], "brief")
                    # An archived project is refused inside the store, under
                    # its lock, atomically with the create (ProjectArchived -> 409).
                    with daemon._lock:
                        daemon._active()
                        task = stores.tasks.create(**body)
                        stores.tasks.save(task)
                        daemon._lifecycle("task_created", task)
                    return 201, to_json(task)
            if len(parts) == 3 and parts[0] == "threads":
                thread_id, action = parts[1:]
                daemon.require(stores.threads, thread_id)
                if action == "log" and method == "GET":
                    _object(query, ("after",))
                    after = int(query.get("after", "0"))
                    if after < 0:
                        raise APIError(400, "after must be nonnegative")
                    # after=N skips N records (zero-based resume offset).
                    return 200, stores.threads.read_log(thread_id)[after:]
                if method == "POST" and action == "seen":
                    # The owner opened it in the HUD: no longer unread or failed.
                    _object(self._body(), ())
                    return 200, {"status": daemon.activity.seen_thread(thread_id)}
                if method == "POST" and action == "interrupt":
                    body = _object(self._body(), ("expected_turn_id",))
                    expected = body.get("expected_turn_id")
                    if "expected_turn_id" in body and (not isinstance(expected, str)
                            or len(expected) != 32 or any(c not in "0123456789abcdef" for c in expected)):
                        raise APIError(400, "expected_turn_id must be 32 lowercase hexadecimal characters")
                    return 200, daemon.interrupt(thread_id, expected_turn_id=expected)
                if method == "POST" and action == "answer":
                    body = _object(self._body(), ("req_id", "decision", "text"), ("req_id",))
                    _text(body["req_id"], "req_id")
                    if ("decision" in body) == ("text" in body):
                        raise APIError(400, "provide exactly one of decision or text")
                    value = Decision(body["decision"]) if "decision" in body else _text(body["text"], "text")
                    daemon.answer(thread_id, body["req_id"], value)
                    return 200, {"ok": True}
            if method == "GET" and parts == ["approvals"]:
                _object(query, ())
                return 200, [r.to_json() for r in daemon.approvals.pending()]
            if method == "POST" and len(parts) == 2 and parts[0] == "approvals":
                # Owner-only (BR-0): the HUD's listener and the HUD's Origin.
                # The API listener serves the same HUD page, so a page loaded
                # there passed `check_origin` (its Origin equals its Host) and
                # could approve — v1's agent browser could load it. Discord
                # and the escape hatch resolve in-process and never come here.
                from .projects import owner_only
                owner_only(self, daemon)
                body = _object(self._body(), ("decision", "always"), ("decision",))
                always = body.get("always", False)
                if not isinstance(always, bool):
                    raise APIError(400, "always must be a boolean")
                # The HUD route: HUD authority (PH-7) only with the HUD
                # listener's own Host, after `owner_only` above.
                return 200, daemon.resolve_approval(
                    parts[1], Decision(_text(body["decision"], "decision")), always,
                    owner_verified=config.own_host_header(self.headers.get("Host", ""), daemon.face_port))
            if len(parts) in (2, 3) and parts[0] == "tasks":
                task = daemon.require(stores.tasks, parts[1])
                if len(parts) == 2 and method == "GET":
                    return 200, to_json(task)
                if len(parts) == 3 and parts[2] == "seen" and method == "POST":
                    _object(self._body(), ())
                    return 200, {"status": daemon.activity.seen_task(task)}
                if len(parts) == 3 and parts[2] == "worktree":
                    return self._worktree(parts[1], query)
                if len(parts) == 3 and method == "POST" and parts[2] in _TASK_VERBS:
                    return self._task_verb(task.id, parts[2])
            raise APIError(404, "route not found")

        def _task_verb(self, task_id, verb):
            """The TaskControl verbs (§11.2). Every one is synchronous and quick:
            a steer queues, a cancel lands at the next boundary, a resume
            requeues — none of them waits on a model."""
            runner = daemon.runner
            if runner is None:
                raise DaemonError("no task runner is attached to this daemon")
            args = task_verb_body(verb, self._body())
            try:
                if verb == "start":
                    return 200, to_json(runner.start(task_id))
                if verb == "steer":
                    runner.steer(task_id, args["text"], spoken=args["spoken"])
                    return 200, {"ok": True}
                if verb == "cancel":
                    return 200, to_json(runner.cancel(task_id))
                if verb == "resume":
                    return 200, to_json(runner.resume(task_id, provider=args["provider"]))
                return 200, to_json(runner.answer_question(task_id, args["index"], args["text"]))
            except ControlError as exc:
                raise APIError(409, str(exc)) from exc

        def _worktree(self, task_id, query):
            # Include the read in the lock: two HTTP requests must not operate
            # on detached task snapshots from before the other's ensure/remove.
            if self.command == "POST":
                _object(self._body(), ())
            _object(query, ("force",) if self.command == "DELETE" else ())
            force = query.get("force", "false").lower()
            if force not in ("true", "false", "1", "0"):
                raise APIError(400, "force must be true or false")
            with daemon._worktree_lock:
                task = daemon.require(daemon.stores.tasks, task_id)
                if self.command == "GET":
                    return 200, to_json(worktrees.status(task, daemon.stores))
                daemon._active()
                if self.command == "POST":
                    project = daemon.require(daemon.stores.projects, task.project_id)
                    task = worktrees.ensure(task, project, daemon.stores)
                    daemon._lifecycle("task_worktree_ensured", task)
                elif self.command == "DELETE":
                    worktrees.remove(task, daemon.stores, force=force in ("true", "1"))
                    daemon._lifecycle("task_worktree_removed", task)
                else:
                    raise APIError(404, "route not found")
                return 200, to_json(task)

        def _events(self, filters, window=None):
            q = daemon.bus.subscribe(filters)
            if window is not None:
                daemon._dev_stream_started(window)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self._streaming = True
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    try:
                        record = q.get(timeout=0.5)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    else:
                        self.wfile.write(("data: " + json.dumps(record) + "\n\n").encode())
                        if record["kind"] == "shutdown":
                            self.wfile.flush()
                            return
                    self.wfile.flush()
            except OSError:
                pass
            finally:
                daemon.bus.unsubscribe(q)
                if window is not None:
                    daemon._dev_stream_ended(window)
    return Handler


class _Unavailable:
    def __init__(self, name, reason):
        self.name, self.reason = name, reason

    def health(self):
        return False, self.reason

    def start(self, *args):
        raise BriefRefused(self.reason)

    resume = start


def load_providers(roster=None):
    """Import only roster members; a missing SDK is a provider health failure."""
    modules = {ProviderName.FAST: ("fastpath", "FastPathProvider"),
               ProviderName.CODEX: ("codex", "CodexProvider"),
               ProviderName.CLAUDE: ("claude", "ClaudeProvider")}
    providers = {}
    for name in (list(ProviderName) if roster is None else roster):
        name = ProviderName(name)
        module, cls = modules[name]
        try:
            providers[name] = getattr(importlib.import_module(f"jarvis.v2.providers.{module}"), cls)()
        except Exception as exc:
            providers[name] = _Unavailable(name, f"unavailable: {type(exc).__name__}: {exc}")
    return providers


# -- the Discord surface (WP10b) -------------------------------------------
# Startup only. Everything about placement, verbs and thread-scoped approvals
# lives in discord/gateway.py; nothing below decides policy.


def discord_connected() -> bool:
    """True once `jarvis auth discord` has been run. The token value is never
    read out of the bundle here, logged, or passed anywhere by this function."""
    try:
        from jarvis.tools.discord import _load_bundle

        return bool(_load_bundle().get("bot_token"))
    except Exception:
        return False


class _NoControl:
    """TaskControl before WP11's runner exists: every verb answers, none acts.

    A surface that raises AttributeError at the owner is worse than one that
    says what it cannot do yet, and `ControlError` is the sentence a surface is
    already required to show.
    """

    _MESSAGE = "The task runner is not running yet, so I cannot do that."

    def _refuse(self, *_args, **_kwargs):
        from .control import ControlError

        raise ControlError(self._MESSAGE)

    start = steer = answer_question = cancel = resume = status = _refuse

    def list_tasks(self, project_id=None, *, active_only=True):
        return []


def start_discord(daemon, control=None, **surface_kwargs):
    """Start the Discord surface and return it (setting `daemon.discord`), or
    None if it is not connected/cannot start.

    The surface owns the order (discord/surface.py): the Reporter subscribes,
    then `daemon.runner.serve()`, then the gateway — so call this after
    `daemon.start()` and before anything else serves the runner. A second call
    returns the surface already attached.

    A 4014 close (Message Content Intent off) is v1's job and stays v1's: the
    listener prints the explanation and stops rather than retry-looping.
    """
    from .discord.surface import DiscordSurface
    from .router import daemon_router

    existing = getattr(daemon, "discord", None)
    if existing is not None:
        return existing
    surface = None
    try:
        router = getattr(daemon, "router", None) or daemon_router(daemon)
        control = control or getattr(daemon, "runner", None) or _NoControl()
        surface = DiscordSurface(daemon, control=control, router=router, **surface_kwargs)
        # `GET /discord` reads its status (connection, commands, reporter) here.
        daemon.discord = surface
        surface.start()
        return surface
    except Exception as exc:
        # Class only: this path is one frame away from the credential bundle.
        LOG.warning("Discord surface not started (%s)", type(exc).__name__)
        daemon.discord = None
        # The class only, for `GET /discord`: a start that failed is red on
        # the HUD, not an endless "pending".
        daemon.discord_error = type(exc).__name__
        if surface is not None:
            try:
                surface.stop(runner=False)     # the daemon still needs its runner
            except Exception:
                pass
        return None


def _purge_trash(daemon, done, interval=6 * 3600):
    """Startup and every six hours: finish any delete a crash left staged, then
    purge Jarvis's trash entries past the retention period (decisions B2)."""
    from .projects import recover_staging
    while True:
        try:
            recover_staging(daemon)
            daemon.trash.purge()
        except Exception:
            LOG.warning("Trash purge failed", exc_info=True)
        if done.wait(interval):
            return


def _cli_status(provider):
    """`{path, version}` of the CLI a provider spawns, for `/status` — the path
    and the version string only. None for a provider that reports no CLI (the
    fast path, a fake, an unavailable one) or whose report fails."""
    info = getattr(provider, "cli_info", None)
    if not callable(info):
        return None
    try:
        got = info()
        if not isinstance(got, dict):
            return None
        return {key: got[key] if isinstance(got.get(key), str) else None
                for key in ("path", "version")}
    except Exception:  # noqa: BLE001 — /status must answer whatever a provider does
        return None


# Libraries that log a request's full URL at INFO (httpx: `HTTP Request: POST
# https://…`) or DEBUG. A Discord interaction reply's URL *is* a credential —
# `/webhooks/<app>/<interaction token>/…` — so at INFO every slash-command
# reply wrote a live token into the daemon log (found on the live daemon,
# 2026-10-07). Our own code logs operations, never URLs; these are held to
# WARNING so a library cannot do it for us.
QUIET_LOGGERS = ("httpx", "httpcore", "urllib3", "websocket", "anthropic", "openai")


def configure_logging() -> None:
    """The daemon's logging: INFO to stderr, URL-logging libraries at WARNING."""
    # A daemon that logs nowhere is one whose failures are invisible: the
    # first owner message that silently did nothing (2026-09-16) left no trace.
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def load_codex_catalog_at_start() -> bool:
    """Install the saved Codex catalog, or keep the built-in table. Nothing
    about that file may stop the daemon from starting (PR #20 re-review):
    under systemd a crash here is a crash loop, so every failure — a
    MemoryError included — is one log line and the fallback."""
    from .router import load_codex_catalog
    try:
        loaded = load_codex_catalog()
    except Exception as exc:
        LOG.warning("Codex model catalog not loaded (%s); using the built-in table",
                    type(exc).__name__)
        return False
    if loaded:
        LOG.info("Codex model catalog loaded from %s", config.CODEX_CATALOG_PATH)
    return bool(loaded)


def _log_hud_served() -> None:
    """Say at start-up which HUD is served, and warn when it is not today's
    (review of PR #44, L6): dev mode, nothing installed or intact, or a
    build installed from another commit than the checkout's — a forgotten
    `jarvis hud install` reads as a warning, never as a silently old window.
    Never raises: a bad install is not why the daemon fails to start (L2)."""
    try:
        from . import hud_install
        from .hud_api import hud_served
        served = hud_served()
        if served["state"] == "dev":
            LOG.warning("JARVIS_HUD_DEV=1: serving %s directly, not an installed build — "
                        "the agent-writable build, for tests only", config.HUD_DIST)
        elif served["build"] is None:
            LOG.warning("No HUD is served from %s (%s): the window will say so. "
                        "Run `jarvis hud install`.", config.HUD_DIR, served["state"])
        else:
            LOG.info("HUD build served: %s (from %s)", served["build"], served["git_head"] or "an unknown commit")
            if served["state"] == "fallback":
                LOG.warning("The newest HUD build (%s) failed verification; serving %s instead. "
                            "Run `jarvis hud install`.", ", ".join(served["passed_over"]), served["build"])
            # Only hud/ matters: a Python-only merge leaves the HUD current.
            changed = hud_install.hud_changed_since(served["git_head"])
            if changed:
                LOG.warning("hud/ changed since the HUD served (%s) was installed from %s: "
                            "run `jarvis hud install`.", served["build"], served["git_head"][:12])
            elif changed is None:
                LOG.warning("Cannot tell whether hud/ changed since the HUD served (%s) was "
                            "installed (from %s).", served["build"], served["git_head"] or "an unknown commit")
            if served.get("dirty"):
                LOG.warning("The HUD served (%s) was installed with uncommitted changes in hud/.",
                            served["build"])
    except Exception:     # noqa: BLE001
        LOG.warning("could not tell which HUD is served", exc_info=True)


def main() -> int:
    # Imported here, not at module scope: the hatch reads the daemon it is
    # given and nothing in the daemon needs it, so keeping the edge one-way
    # keeps `import jarvis.v2.daemon` free of the subprocess machinery.
    from .hatch import EscapeHatch
    from .runner import TaskRunner

    configure_logging()
    # The Codex models the account offered last time, before anything reads
    # routing (PR #20 review): a restart, or a Discord-only daemon no HUD ever
    # reads, routes and offers against them rather than the built-in fallback.
    # Missing or corrupt leaves the fallback. Here and not in `Daemon.start`,
    # so no embedded or test daemon reads the owner's file.
    load_codex_catalog_at_start()

    remote = discord_connected()
    approvals = None
    if remote:
        # A DM takes longer to answer than a card in front of you, so an
        # attached remote surface gets WP5's long timeout (v1's ten minutes).
        # The callbacks are the daemon's own, bound once it exists.
        holder = {}
        approvals = PendingApprovals(
            remote=True,
            on_request=lambda request: holder["daemon"]._approval_requested(request),
            on_resolve=lambda request, decision, resolution:
                holder["daemon"]._approval_resolved(request, decision, resolution))
    daemon = Daemon(Stores(), load_providers(), None, config.DAEMON_PORT, approvals,
                    dev_proc_root="/proc", dev_cgroup_root="/sys/fs/cgroup")
    if approvals is not None:
        holder["daemon"] = daemon
    # The owner's Browser switches (`browser.json`): here, not in the
    # constructor or `start`, so no embedded or test daemon reads the owner's
    # file. Nothing about it may stop the daemon: a failure keeps the defaults.
    try:
        daemon.browser_settings.attach(config.BROWSER_SETTINGS_PATH)
    except Exception as exc:     # noqa: BLE001
        LOG.warning("browser settings not attached (%s); using the defaults", type(exc).__name__)
    daemon.browser_direct_probe.attach(config.DIRECT_CAPTURE_PROBE_PATH)
    # The owner's keyring (`browser/password_store.py`): the daemon's own
    # session bus, attached here for the same reason — no embedded or test
    # daemon ever reads the owner's keyring state or hands Chrome the bus.
    try:
        daemon.browser.keyring.attach()
    except Exception as exc:     # noqa: BLE001
        LOG.warning("browser keyring not attached (%s); the basic store is used", type(exc).__name__)
    # The phone's §4.1 checks read Tailscale and Windows (remote_secure.py,
    # read-only, time-limited): attached here, so no embedded or test daemon
    # runs a Windows command. Without it the phone stays limited.
    daemon.secure.attach_system()
    # The inbox's open PRs (B3, inbox_prs.py): the owner's `gh`, read-only,
    # attached here for the same reason.
    daemon.inbox.prs.attach_system()
    hatch = EscapeHatch(daemon, daemon.approvals)
    discord = None
    daemon.runner = TaskRunner(daemon, daemon.router)
    done = threading.Event()
    purger = threading.Thread(target=_purge_trash, args=(daemon, done), name="jarvis-trash", daemon=True)
    previous = signal.signal(signal.SIGTERM, lambda *_: done.set())
    try:
        daemon.start()
        hatch.start()
        if remote:
            # Reporter subscribes, then the runner serves, then the gateway.
            discord = start_discord(daemon)
        daemon.runner.serve()                   # idempotent if Discord served it
        # Chat titles: after the surfaces, so the Discord mirror hears every
        # `thread_updated` a title publishes (titles.py).
        from .titles import Titler
        daemon.titles = Titler(daemon)
        daemon.titles.start()
        purger.start()
        LOG.info("Jarvis v2 listening on 127.0.0.1:%s", daemon.port)
        _log_hud_served()
        claude_cli = _cli_status(daemon.providers.get(ProviderName.CLAUDE))
        if claude_cli is not None:
            # Once, at start-up: which `claude` every Claude session spawns.
            LOG.info("claude CLI in use: %s, version %s",
                     claude_cli["path"] or "SDK bundled", claude_cli["version"] or "unknown")
        done.wait()
    except KeyboardInterrupt:
        pass
    except DaemonError as exc:
        print(f"jarvis daemon2: {exc}")
        return 1
    finally:
        shut_down(daemon, discord, hatch)
        signal.signal(signal.SIGTERM, previous)
    return 0


def shut_down(daemon, discord, hatch) -> None:
    """`main()`'s shutdown order, here so a check can run it with fakes.

    Pending approvals are released first, while every surface still listens:
    each is denied with resolution "shutdown" and announced (bounded by
    RELEASE_WAIT_S), and `discord.stop()` lets the gateway finish the post
    edits those announcements queued (bounded by gateway.SETTLE_S) before it
    closes, so no approval post keeps live buttons after Jarvis stops.

    Then the gateway, so no verb arrives at a runner that is stopping; then
    the runner, so its workers see interrupted turns rather than a dead
    daemon; then the Reporter, flushing within 2 s (all three inside
    `discord.stop()`). Nothing is cancelled — a task left RUNNING is
    re-admitted by `serve()`'s recovery next boot. `daemon.stop()` releases
    again, which finds nothing (idempotent). Before any of it the runner is
    quiesced (no task admitted, no further model turn), so a denial cannot
    move a task on. Each step is bounded; one that raises is logged and does
    not skip the rest, and `daemon.stop()` runs even when a KeyboardInterrupt
    cuts the others short."""
    runner = getattr(daemon, "runner", None)
    steps = [
        # No task starts another model turn on the strength of a denial.
        ("runner.quiesce", getattr(runner, "quiesce", None)),
        ("release_approvals", daemon.release_approvals),
        ("discord.stop", getattr(discord, "stop", None)),
        ("runner.stop", getattr(runner, "stop", None)),   # idempotent if Discord stopped it
        ("hatch.stop", getattr(hatch, "stop", None)),
    ]
    try:
        for name, step in steps:
            if step is None:
                continue
            try:
                step()
            except Exception:  # noqa: BLE001 — every later step still runs
                LOG.warning("shutdown step %s failed", name, exc_info=True)
    finally:
        # Always, even when a second Ctrl-C (a BaseException) cuts a step short.
        try:
            daemon.stop()
        except Exception:  # noqa: BLE001
            LOG.warning("shutdown step daemon.stop failed", exc_info=True)
