"""Where an owner message lands, and what a typed yes is allowed to authorize.

Design §11.2. Everything v1 learned about this surface is carried over intact,
because every one of those rules was written after something went wrong:

* **Only the owner is ever heard, and never a bot.** v1's `should_respond` is
  still the gate; the only thing v2 changes is the *mention* requirement, and
  only inside a place Jarvis owns — a task thread or a project channel. The
  owner rule is not re-implemented here: this module asks v1's function about
  the same message with the mention supplied, so there stays exactly one copy
  of "who may trigger an agent".
* **A transcription can never resolve an authorization, or answer a
  question.** A voice note reaches `router.classify` with `spoken=True`, which
  already refuses to make a verb of it, and `_handle` additionally never
  reaches the approval parser — or `_answer` — on a spoken turn. A mishearing
  must not become a yes (decisions D7, S-2).
* **An answer counts only where the question was asked.** v1 scoped that to the
  DM channel; §11.2 applies it to threads. The channel an approval was posted
  to is recorded here under the request id, with the same one-shot lifetime as
  the request: a code typed in another thread, in the project channel, in a DM
  or in a guild channel Jarvis does not own resolves nothing and says so. The
  Approve/Deny buttons (S1) are held tighter still: the press must come from
  the very message the approval was posted as.
* **Two open questions and a bare "yes" is a guess**, so it asks for the code.
  Anything that is not a clear answer resolves nothing.

S1 adds slash commands (`interactions.py`). There is still one implementation
of every verb: each takes a `Reply` sink instead of a channel id, so the typed
keyword path (`ChannelReply`) and the slash path (`InteractionReply`) share it.
Keywords keep working for one release (decisions S-3) and every keyword reply
ends with a nudge toward its slash command.

Nothing here decides permissions. The broker (§6 layer 5) owns that; this is
one surface it can ask through, and it denies on every failure path already.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
import queue
import re
import threading
import time
from typing import Protocol

from jarvis import discord_gateway as v1gw
from jarvis import permissions as v1permissions
from jarvis.discord_approvals import _parse as _parse_answer
from jarvis.tools import discord as v1tools
from jarvis.tools.secrets import scrub

from ..approvals import v1_request
from ..commands import open_questions
from ..control import ControlError
from ..model import TERMINAL_STATES, ProviderName, Role
from ..provider import Decision, UserMessage
from ..router import (FastPath, Incoming, NeedsProject, NewTask, Steer, Verb,
                      classify)
from ..stores import StoreError, _write_bytes
from .commands import SyncResult
from .render import _cap, approval_components, approval_text, status_embed
from .reporter import thread_for
from .rest import describe

LOG = logging.getLogger(__name__)

MAX_DISCORD_CHARS = 2000
TURN_TIMEOUT_S = 900.0
APPROVAL_KINDS = frozenset({"approval_requested", "approval_resolved"})
# "cancel"/"stop" are v1 denial words *and* v2 verbs. In a task thread the verb
# is what the owner means, so those two spellings never claim an open approval;
# the approval then times out, which denies. Never the other way round.
_NOT_AN_ANSWER = frozenset({"cancel", "stop"})
_IN_PROJECT = re.compile(r"^in\s+([^:\n]{1,60}):\s*(.+)$", re.I | re.S)
_RESUME = re.compile(r"^(\S+)(?:\s+on\s+(\S+))?$", re.I)
# The slash command each typed keyword becomes (S1 transition, decisions S-3).
_SLASH = {"yes": "/yes", "allow": "/yes", "no": "/no", "deny": "/no", "always": "/always",
          "steer": "/steer", "redirect": "/steer", "cancel": "/cancel", "status": "/status",
          "tasks": "/status", "projects": "/project list", "resume": "/resume",
          "task": "/task"}


def should_respond(message: dict, bot_id: str, owner_id: str, *, owned: bool = False) -> bool:
    """v1's rule, with the mention requirement lifted inside an owned place.

    `owned` is true for a DM, a task thread and a project channel (§11.2). The
    owner-only, never-bots and never-empty halves are v1's and stay v1's: the
    message is handed to v1 as if it had carried the mention, rather than
    re-stating three rules that must not drift from the v1 surface.
    """
    if owned and "guild_id" in message:
        mentions = [*(message.get("mentions") or []), {"id": bot_id}]
        message = {**message, "mentions": mentions}
    return v1gw.should_respond(message, bot_id, owner_id)


def clean_code(code) -> str:
    """An owner-typed approval code, safe to echo: lowercase, no markup, short."""
    return re.sub(r"[^a-z0-9]", "", str(code or "").lower())[:8]


# -- reply sinks --------------------------------------------------------------


class Reply(Protocol):
    """Where a verb's answer goes. One implementation per verb, two sinks."""
    channel_id: str

    def send(self, content=None, *, embed=None, files=(), components=None,
             ephemeral=False): ...

    def refuse(self, text: str) -> None:
        """A refusal: private on an interaction, a plain post in a channel."""

    def defer(self, *, ephemeral: bool = False) -> None:
        """Called before any store write or control call (the 3-second rule)."""


class ChannelReply:
    """The message path: every answer is an ordinary bot post in the channel."""

    def __init__(self, surface, channel_id):
        self.surface = surface
        self.channel_id = str(channel_id)

    def send(self, content=None, *, embed=None, files=(), components=None, ephemeral=False):
        return self.surface._post(self.channel_id, content, files=files, embed=embed,
                                  components=components)

    def refuse(self, text):
        self.send(text)

    def defer(self, *, ephemeral=False):
        pass

    def finish(self):
        pass


class NudgingReply:
    """A keyword reply that ends with its slash command (decisions S-3)."""

    def __init__(self, inner, slash: str):
        self.inner = inner
        self.channel_id = inner.channel_id
        self.note = f"(next time: `{slash}`)"

    def _nudge(self, content):
        if content is None:
            return self.note
        return f"{content}\n{self.note}"

    def send(self, content=None, *, embed=None, files=(), components=None, ephemeral=False):
        if content is None and files and embed is None:
            return self.inner.send(None, files=files, components=components)
        return self.inner.send(self._nudge(content), embed=embed, files=files,
                               components=components, ephemeral=ephemeral)

    def refuse(self, text):
        self.inner.refuse(self._nudge(text))

    def defer(self, *, ephemeral=False):
        self.inner.defer(ephemeral=ephemeral)

    def finish(self):
        getattr(self.inner, "finish", lambda: None)()


# -- the socket ---------------------------------------------------------------


class _V2Listener(v1gw.GatewayListener):
    """v1's socket (HELLO/IDENTIFY/heartbeat/4014) with v2's dispatch.

    v1 decides *whether* to dispatch inside its own loop, using its module-level
    `should_respond`; v2's decision needs a store lookup v1 cannot do, so the
    loop here hands every MESSAGE_CREATE to the router and the router gates it.
    `_serve` — including the 4014 "intents are off" stop, which must never turn
    into a retry loop — is inherited unchanged. Proposed to v1 as a `dispatch`
    hook in WP10b-notes.md, which would delete this override entirely.

    S1: READY also carries the application id (the command sync and the
    interaction gate both need it), and INTERACTION_CREATE — which needs no
    intent — goes to a worker thread at once, because an interaction has three
    seconds to be answered and the socket loop must never wait on one. v1's
    listener ignores INTERACTION_CREATE, so a stray v1 process cannot
    double-handle a command.
    """

    def __init__(self, handler, announce=print, *, on_interaction=None, on_ready=None):
        super().__init__(run_turn=lambda *_a, **_k: "", announce=announce)
        self._handler = handler
        self.application_id = ""
        self.on_interaction = on_interaction
        self.on_ready = on_ready

    def _session(self, ws) -> None:
        import websocket

        hello = v1gw._recv_json(ws)
        beat_every = hello["d"]["heartbeat_interval"] / 1000.0
        seq = None
        ws.send(v1gw.json.dumps({
            "op": 2,
            "d": {"token": self._token, "intents": v1gw.INTENTS,
                  "properties": {"os": "linux", "browser": "jarvis", "device": "jarvis"}},
        }))
        ws.settimeout(1.0)
        next_beat = time.monotonic() + beat_every
        while not self._stop.is_set():
            if time.monotonic() >= next_beat:
                ws.send(v1gw.json.dumps({"op": 1, "d": seq}))
                next_beat = time.monotonic() + beat_every
            try:
                frame = v1gw._recv_json(ws)
            except websocket.WebSocketTimeoutException:
                continue
            if frame.get("s") is not None:
                seq = frame["s"]
            op, kind, data = frame.get("op"), frame.get("t"), frame.get("d")
            if op == 1:
                ws.send(v1gw.json.dumps({"op": 1, "d": seq}))
            elif op in (7, 9):
                ws.close()
                return
            elif kind == "READY":
                self.bot_id = str(data["user"]["id"])
                application = (data.get("application") or {}).get("id")
                if application:
                    self.application_id = str(application)
                self.announce(f"[discord] listening as {data['user'].get('username')} "
                              f"(replies only to the owner)")
                if self.on_ready is not None:
                    threading.Thread(target=self.on_ready, args=(self,), daemon=True,
                                     name="jarvis-discord-ready").start()
            elif kind == "MESSAGE_CREATE":
                threading.Thread(target=self._handler,
                                 args=(data, self.bot_id, self.owner_id), daemon=True).start()
            elif kind == "INTERACTION_CREATE" and self.on_interaction is not None:
                threading.Thread(target=self.on_interaction, args=(data,), daemon=True,
                                 name="jarvis-discord-interaction").start()


class DiscordRouter:
    """One Discord surface over the v2 daemon, stores, router and broker."""

    def __init__(self, daemon, stores, router, approvals, control, rest,
                 listener_factory=None, *, announce=None, dm_channel=None,
                 turn_timeout_s: float = TURN_TIMEOUT_S, sync_commands: bool = True,
                 clock=time.monotonic, approval_posts_path=None):
        from .interactions import InteractionRouter

        self.daemon = daemon
        self.stores = stores
        self.router = router
        self.approvals = approvals
        self.control = control
        self.rest = rest
        self.bot_id = ""
        self.owner_id = ""
        self.application_id = ""
        # The configured guild (B1) is read from `jarvis auth discord-guild`'s
        # file whenever it is needed (`guild_id` below); a test may pin it.
        self._guild_pin: str | None = None
        self._guild_pinned = False
        self.turn_timeout_s = turn_timeout_s
        self.sync_commands = sync_commands
        self.commands = SyncResult()
        self._synced = False
        self._announce = announce or (lambda text: LOG.info("%s", text))
        self._dm_channel = dm_channel or v1tools.owner_dm_channel
        self._dm_cache = None
        self._lock = threading.RLock()
        self._approval_channels: dict[str, str] = {}
        # req_id -> (channel, message id, code) of the post carrying the buttons.
        self._approval_messages: dict[str, tuple[str, str, str]] = {}
        # The same map on disk, so a restart can strip the buttons of posts
        # whose requests died with the previous process (the broker denies
        # them all at shutdown). Beside the stores, never the owner's real
        # data dir unless the stores are.
        self._posts_path = Path(approval_posts_path) if approval_posts_path else \
            Path(stores.root) / "discord" / "approval-posts.json"
        self._stale_worker = None
        self._answered_here: set[str] = set()
        self._chat_threads: dict[str, str] = {}
        self.interactions = InteractionRouter(self, clock=clock)
        factory = listener_factory or (lambda handler: _V2Listener(handler, self._announce))
        self.listener = factory(self.handle)
        # Attributes rather than constructor arguments, so any listener
        # (including a test's) is wired the same way.
        self.listener.on_interaction = self.handle_interaction
        self.listener.on_ready = self._ready
        self._stop = threading.Event()
        self._subscription = daemon.bus.subscribe(lambda r: r.get("kind") in APPROVAL_KINDS)
        self._worker = threading.Thread(target=self._watch, name="jarvis-discord-approvals",
                                        daemon=True)
        self._worker.start()

    # -- the configured guild (B1) ----------------------------------------

    @property
    def guild_id(self) -> str | None:
        """The server Jarvis lives in, or None before `jarvis auth
        discord-guild`. Re-read on every use (cached on the file's mtime), so
        setup takes effect without a restart. Once set, a guild message or
        interaction from any other server is placed nowhere."""
        if self._guild_pinned:
            return self._guild_pin
        from . import guild
        cfg = guild.load()
        return cfg.guild_id if cfg is not None else None

    @guild_id.setter
    def guild_id(self, value) -> None:
        self._guild_pinned, self._guild_pin = True, (str(value) if value else None)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self.listener.start()
        self.owner_id = str(getattr(self.listener, "owner_id", "") or self.owner_id)
        self._stale_worker = threading.Thread(target=self._strip_stale, daemon=True,
                                              name="jarvis-discord-stale-buttons")
        self._stale_worker.start()

    def _strip_stale(self) -> None:
        """Every approval post the last process left open loses its buttons.
        Their requests cannot be answered any more (a press is refused either
        way); this just stops a dead button from looking live."""
        try:
            stale = json.loads(self._posts_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            LOG.warning("Discord approval-post map unreadable (%s)", type(exc).__name__)
            stale = {}
        if not isinstance(stale, dict):
            stale = {}
        for req_id, row in stale.items():
            with self._lock:
                if req_id in self._approval_messages:   # one of ours, posted since
                    continue
            if isinstance(row, list) and len(row) == 3 and all(isinstance(x, str) for x in row):
                self._strip_buttons(row[0], row[1])
        self._save_posts()

    def _save_posts(self) -> None:
        with self._lock:
            rows = {req_id: list(row) for req_id, row in self._approval_messages.items()}
            try:
                _write_bytes(self._posts_path, json.dumps(rows).encode("utf-8"))
            except (StoreError, OSError) as exc:
                LOG.warning("Discord approval-post map not saved (%s)", type(exc).__name__)

    def stop(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        self.daemon.bus.unsubscribe(self._subscription)
        self._subscription.offer({"kind": "shutdown"})
        try:
            self.listener.stop()
        except Exception:
            LOG.warning("Discord listener did not stop cleanly")
        self._worker.join(timeout=2)
        if self._stale_worker is not None:
            self._stale_worker.join(timeout=2)

    def _ready(self, listener) -> None:
        """First READY: remember who we are, then sync the commands once."""
        self.bot_id = str(getattr(listener, "bot_id", "") or self.bot_id)
        self.application_id = str(getattr(listener, "application_id", "") or self.application_id)
        self.owner_id = str(getattr(listener, "owner_id", "") or self.owner_id)
        with self._lock:
            if self._synced or not self.sync_commands or not self.application_id:
                return
            self._synced = True
        from . import commands

        self.commands = commands.sync(self.rest, self.application_id)
        self._announce(self.commands.line())

    def status(self) -> dict:
        """For `GET /discord`. Holds no token, no path and no payload."""
        return {"connected": bool(self.application_id) and not self._stop.is_set(),
                "commands": self.commands.to_json()}

    def handle_interaction(self, interaction) -> None:
        self.interactions.handle(interaction)

    # -- placement (§11.1) -------------------------------------------------

    def _locate(self, message, channel_id):
        """-> (where, project, task). `where` is dm / task / project / archived / other.

        An archived project's channel, and the old task threads under it, are
        `archived` (decisions B1): nothing is placed, opened, started or steered
        there; the owner is told why and pointed at the HUD.
        """
        if "guild_id" not in message:
            return "dm", None, None
        configured = self.guild_id
        if configured and str(message.get("guild_id")) != str(configured):
            # Another server (B1): nothing there is a Jarvis place, whatever
            # its channel id happens to match.
            return "other", None, None
        tasks = self.stores.tasks.list(discord_thread_id=channel_id)
        if tasks:
            task = tasks[0]
            project = self.stores.projects.get(task.project_id)
            if project is not None and project.archived:
                return "archived", project, task
            return "task", project, task
        projects = self.stores.projects.list(discord_channel_id=channel_id)
        live = [p for p in projects if not p.archived]
        if live:
            return "project", live[0], None
        if projects:
            return "archived", projects[0], None
        return "other", None, None

    def _remember_dm(self, channel_id) -> None:
        with self._lock:
            self._dm_cache = str(channel_id)

    @staticmethod
    def archived_text(project) -> str:
        return _cap(f"Project {project.name} is archived, so I won't open, start or steer "
                    "anything here. Restore it from the HUD's Archive view to work in it "
                    "again.", MAX_DISCORD_CHARS)

    # -- the message path --------------------------------------------------

    def handle(self, message, bot_id=None, owner_id=None) -> None:
        try:
            self._handle(message, bot_id, owner_id)
        except Exception as exc:
            # Never log the message of an exception raised near credentials or
            # injected transports; the class is enough to find it in the code.
            LOG.warning("Discord message handling failed (%s)", type(exc).__name__)

    def _handle(self, message, bot_id=None, owner_id=None) -> None:
        bot_id = str(self.bot_id if bot_id is None else bot_id)
        owner_id = str(self.owner_id if owner_id is None else owner_id)
        self.bot_id, self.owner_id = bot_id or self.bot_id, owner_id or self.owner_id
        channel_id = str(message.get("channel_id") or "")
        where, project, task = self._locate(message, channel_id)
        if not should_respond(message, bot_id, owner_id,
                              owned=where in ("dm", "task", "project", "archived")):
            return
        reply = ChannelReply(self, channel_id)
        if where == "archived":
            # Before the approval parser, the classifier and the fast path: an
            # archived project takes no work from here, whatever the message says.
            reply.send(self.archived_text(project))
            return
        if where == "dm":
            self._remember_dm(channel_id)
        text = v1gw.strip_mention(message.get("content") or "", bot_id)
        spoken = False
        note = v1gw.voice_attachment(message)
        if note is not None:
            try:
                text, spoken = self._hear(note), True
            except Exception as exc:
                reply.send(f"I couldn't make out that voice message ({exc}).")
                return
        if not text.strip():
            return
        # Typed only: a transcription is one mishearing away from "yes".
        if not spoken and self._approval_answer(channel_id, text, reply):
            return
        incoming = self._incoming(text, channel_id, project, task, spoken)
        destination = classify(incoming)
        if isinstance(destination, Verb):
            self._verb(destination, reply, project, task)
        elif isinstance(destination, NewTask):
            nudged = NudgingReply(reply, _SLASH["task"])
            try:
                self._intake(destination.text, nudged, project)
            except ControlError as exc:
                nudged.send(str(exc))
        elif isinstance(destination, Steer):
            self._steer_or_answer(destination.task_id, text, reply, spoken)
        elif isinstance(destination, FastPath):
            self._chat(reply, where, project, text, spoken, incoming)

    @staticmethod
    def _incoming(text, channel_id, project, task, spoken=False) -> Incoming:
        return Incoming(
            text=text,
            surface=(project.discord_channel_id if project and project.discord_channel_id
                     else channel_id),
            project_id=project.id if project else None,
            task_id=task.id if task else None,
            spoken=spoken)

    def _hear(self, attachment) -> str:
        """Voice note -> transcript, v1's rules (size cap, empty is an error)."""
        from jarvis import voice

        size = int(attachment.get("size") or 0)
        if size > v1gw.MAX_VOICE_BYTES:
            raise ValueError(f"it is too large — {size // (1024 * 1024)}MB")
        audio = v1gw._download(attachment["url"])
        mime = (attachment.get("content_type") or "audio/ogg").split(";")[0]
        text = voice.stt(audio, mime=mime).strip()
        if not text:
            raise ValueError("the transcription came back empty")
        return text

    # -- approvals ---------------------------------------------------------

    def _approval_answer(self, channel_id: str, text: str, reply) -> bool:
        """True when this typed message was an answer (or a refusal to guess at one)."""
        verdict, code = _parse_answer(text)
        if verdict is None or text.strip().split()[0].lower() in _NOT_AN_ANSWER:
            return False
        target, refusal = self.approval_target(verdict, code, channel_id,
                                               bare_falls_through=True)
        if target is None and refusal is None:
            return False
        nudged = NudgingReply(reply, _SLASH[{"allow": "yes", "deny": "no"}.get(verdict, verdict)])
        if refusal is not None:
            nudged.refuse(refusal)
            return True
        self.decide(target, verdict, nudged)
        return True

    def pending_here(self, channel_id: str) -> list:
        """The open requests that were asked in this channel, and only those."""
        with self._lock:
            channels = dict(self._approval_channels)
        return [r for r in self.approvals.pending() if channels.get(r.req_id) == str(channel_id)]

    @staticmethod
    def allowlistable(request) -> tuple[bool, str]:
        """Whether `always` could mint a standing rule for this request."""
        if not getattr(request, "allowlistable", True):
            return False, "it came through a path that never makes standing rules"
        try:
            v1permissions.entry_for(*v1_request(request.tool, dict(request.args or {})))
        except v1permissions.NotAllowlistable as exc:
            return False, str(exc)
        except Exception as exc:
            return False, type(exc).__name__
        return True, ""

    def approval_target(self, verdict, code, channel_id, *, bare_falls_through: bool):
        """-> (request, None) to resolve, (None, sentence) to refuse, or
        (None, None) when a bare word claims nothing and may be ordinary text.

        Reads only: the slash path calls this before it defers."""
        code = clean_code(code)
        with self._lock:
            channels = dict(self._approval_channels)
        open_requests = list(self.approvals.pending())
        if code:
            match = next((r for r in open_requests if r.code == code), None)
            if match is None:
                return None, f"No open authorization with code `{code}`."
            if channels.get(match.req_id) != str(channel_id):
                return None, (f"That code (`{code}`) was asked elsewhere — answer it where "
                              f"it was posted. Nothing ran.")
            target = match
        else:
            mine = [r for r in open_requests if channels.get(r.req_id) == str(channel_id)]
            if not mine:
                if bare_falls_through:
                    return None, None
                return None, "No open authorization was asked in this chat."
            if len(mine) > 1:
                codes = ", ".join(f"`{r.code}` ({r.tool})" for r in mine)
                return None, f"Which one? Answer with the code: {codes}."
            target = mine[0]
        if verdict == "always":
            ok, why = self.allowlistable(target)
            if not ok:
                return None, (f"`{target.code}` can't become a standing rule ({_cap(why, 160)}). "
                              f"Answer it once with `/yes {target.code}` or `/no {target.code}`. "
                              "Nothing ran.")
        return target, None

    def decide(self, target, verdict, reply) -> None:
        """Resolve one request the caller has already checked, and say so."""
        decision = Decision.DENY if verdict == "deny" else Decision.ALLOW
        reply.defer()
        try:
            with self._lock:
                self._answered_here.add(target.req_id)
            self.approvals.resolve(target.req_id, decision, always=(verdict == "always"))
        except ValueError:
            reply.send(f"That one (`{target.code}`) was already answered or expired — "
                       "nothing ran.")
            return
        if decision is Decision.DENY:
            reply.send(f"Denied. `{target.tool}` did not run.")
        elif verdict == "always":
            reply.send(f"Authorized `{target.tool}`, and I will stop asking about this one.")
        else:
            reply.send(f"Authorized. Running `{target.tool}` now.")

    def button_target(self, message_id, channel_id, code):
        """The open request a button press is for, or (None, sentence).

        The press must come from the message the approval was posted as, in
        the channel it was asked in, with the code that message carries. The
        custom id is untrusted: it is only allowed to agree with the lookup."""
        with self._lock:
            found = [(req_id, row) for req_id, row in self._approval_messages.items()
                     if row[1] == str(message_id)]
        if not found:
            return None, "That approval was already answered — nothing ran."
        req_id, (asked_in, _message, posted_code) = found[0]
        if asked_in != str(channel_id) or posted_code != clean_code(code):
            return None, "That button does not match its approval. Nothing ran."
        request = next((r for r in self.approvals.pending() if r.req_id == req_id), None)
        if request is None:
            return None, "That approval was already answered — nothing ran."
        return request, None

    def _watch(self) -> None:
        while not self._stop.is_set():
            try:
                record = self._subscription.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if record.get("kind") == "shutdown":
                    break
                if not (record.get("data") or {}).get("code"):
                    # Only the broker asks the owner, and every broker question
                    # carries a code. Anything else (a provider's "the gate was
                    # consulted") is never a DM, nor a resolution post.
                    continue
                if record["kind"] == "approval_requested":
                    self._post_approval(record.get("data") or {})
                else:
                    self._post_resolution(record.get("data") or {})
            except Exception as exc:
                LOG.warning("Discord approval delivery failed (%s)", type(exc).__name__)
            finally:
                self._subscription.task_done()

    def _post_approval(self, data: dict) -> None:
        """Ask in the task's thread when it has one, else the owner's DM.

        A thread post that fails is asked again in the DM, and the DM is then
        recorded as where it was asked — so the answer counts there and only
        there, exactly as if it had been asked there first. In a guild the post
        is silent and followed by the D1 ping line; a DM notifies by itself."""
        req_id = str(data.get("req_id") or "")
        code = str(data.get("code") or "")
        channel = None
        task_id = data.get("task_id")
        if task_id:
            try:
                task = self.stores.tasks.get(task_id)
            except StoreError:                  # not a task id: ask in the DM
                task = None
            channel = thread_for(self.stores, task) if task else None
        if not req_id:
            self._announce("[approval] Discord could not deliver this request")
            return
        args = dict(data.get("args") or {})
        command = data.get("command")
        if isinstance(command, str) and not isinstance(args.get("command"), str):
            # The owner must see the entire command, never a digest of it.
            args["command"] = command
        tool = str(data.get("tool") or "")
        request = _Asked(tool=tool, args=args,
                         allowlistable=data.get("allowlistable", True) is not False)
        # The whole command is shown, minus any secret value pasted into it:
        # the transcript scrub runs over the text Discord receives.
        body = scrub(approval_text(tool, args, code, str(data.get("origin") or ""),
                                   allowlistable=self.allowlistable(request)[0]))
        posted = None
        if channel:
            posted = self._ask_in(req_id, str(channel), body, code)
        if posted is None:
            dm = self._owner_dm()
            if not dm:
                # The broker denies on timeout; it does not need rescuing here.
                self._announce("[approval] Discord could not deliver this request")
                return
            posted = self._ask_in(req_id, dm, body, code, raise_errors=True)
        channel, message_id = posted
        with self._lock:
            pending = req_id in self._approval_channels     # not resolved meanwhile
            if pending:
                self._approval_messages[req_id] = (channel, str(message_id), code)
        if not pending:
            self._strip_buttons(channel, str(message_id))
            return
        # S1's persisted map: a restart strips these buttons if the request
        # died with this process — the DM fallback's post included.
        self._save_posts()
        if not self._is_dm(channel):
            # D1: the approval itself is silent; this line is what notifies.
            try:
                self.rest.ping_owner(channel)
            except Exception as exc:
                LOG.warning("Discord ping_owner failed (HTTP %s, code %s, %s)",
                            *_facts(exc))

    def _ask_in(self, req_id, channel, body, code, *, raise_errors=False):
        """Post one approval and record where it was asked. -> (channel, message
        id), or None when the post failed and the caller may try elsewhere."""
        with self._lock:
            self._approval_channels[req_id] = channel
        try:
            # Never split or cap: DiscordRest attaches an over-long approval whole.
            message_id = self.rest.post(channel, content=body,
                                        components=approval_components(code),
                                        silent=not self._is_dm(channel))
        except Exception as exc:
            with self._lock:
                self._approval_channels.pop(req_id, None)
            LOG.warning("Discord approval post failed (HTTP %s, code %s, %s)", *_facts(exc))
            if raise_errors:
                raise
            return None
        return channel, message_id

    def _is_dm(self, channel) -> bool:
        dm = self._owner_dm()
        return bool(dm) and str(channel) == str(dm)

    def _strip_buttons(self, channel, message_id) -> None:
        try:
            self.rest.edit(channel, message_id, components=[])
        except Exception as exc:
            LOG.warning("Discord approval buttons not removed (%s)", type(exc).__name__)

    def _post_resolution(self, data: dict) -> None:
        req_id = str(data.get("req_id") or "")
        with self._lock:
            channel = self._approval_channels.pop(req_id, None)
            posted = self._approval_messages.pop(req_id, None)
            here = req_id in self._answered_here
            self._answered_here.discard(req_id)
        if posted is not None:
            self._save_posts()
            # Answered anywhere — HUD, timeout, slash, a button — the buttons go,
            # so a stale tap cannot reach a later request.
            self._strip_buttons(posted[0], posted[1])
        if not channel:
            return
        where = "answered here" if here else "answered on another surface"
        self._post(channel, f"Approval `{data.get('code', '')}` for "
                            f"`{data.get('tool', '')}`: {data.get('resolution', '')} ({where}).")

    # -- verbs -------------------------------------------------------------
    # Each verb takes a `Reply`. Refusals that need only reads come first, then
    # `reply.defer()`, then the first store write or control call.

    def _verb(self, verb: Verb, reply, project, task) -> None:
        name, argument = verb.name, verb.argument.strip()
        reply = NudgingReply(reply, _SLASH.get(name, "/" + name))
        try:
            if name in ("yes", "no", "always"):
                # Only reachable with no open request in this channel: the
                # answer parser above claims every message that answers one.
                reply.refuse(f"No open authorization with code `{clean_code(argument)}`.")
            elif name in ("steer", "redirect"):
                self._steer(task.id if task else None, argument, reply, False)
            elif name == "cancel":
                self._cancel(argument or (task.id if task else ""), reply)
            elif name == "status":
                self._status(reply, project, task)
            elif name == "projects":
                self._list_projects(reply)
            elif name == "tasks":
                self._list_tasks(reply, project)
            elif name == "resume":
                match = _RESUME.match(argument)
                if not match:
                    reply.refuse("Say `resume <task id>` or `resume <task id> on <provider>`.")
                    return
                try:
                    provider = ProviderName(match.group(2).lower()) if match.group(2) else None
                except ValueError:
                    reply.refuse("Provider must be claude, codex or fast.")
                    return
                self._resume(match.group(1), provider, reply)
        except ControlError as exc:
            reply.send(str(exc))
        except StoreError:
            # A typed id that is not the store's id shape (`cancel foo`).
            typed = " ".join(argument.split()[:1]).replace("`", "")
            reply.send(f"I don't know a task `{_cap(typed, 40)}`.")

    def _steer(self, task_id, text, reply, spoken) -> None:
        if not task_id:
            reply.refuse("There is no task here to steer — open one with `/task`, or name "
                         "one with `/steer task:`.")
            return
        reply.defer()
        self.control.steer(task_id, text, spoken=spoken)
        reply.send(f"Noted — steering task {task_id} at its next step.")

    def _steer_or_answer(self, task_id, text, reply, spoken) -> None:
        """Plain text in a task thread: the open question's answer if one is
        waiting, otherwise a steer (plan §4.3, bug 2). Never an answer when spoken."""
        task = self.stores.tasks.get(task_id) if task_id else None
        waiting = open_questions(task)
        try:
            if waiting and spoken:
                reply.refuse("I can't take a voice note as an answer to the open question — "
                             "type it here, or use `/answer`. Nothing was answered.")
            elif waiting:
                self._answer(task, waiting[0][0], text, reply)
            else:
                self._steer(task_id, text, reply, spoken)
        except ControlError as exc:
            reply.send(str(exc))

    def _answer(self, task, index: int, text: str, reply) -> None:
        """The one answer path, shared by `/answer` and plain typed text."""
        reply.defer()
        updated = self.control.answer_question(task.id, index, text)
        remaining = open_questions(updated) if updated is not None else []
        tail = (f" Next question: {_cap(remaining[0][1].text, 300)}" if remaining
                else " That was the last open question; the task carries on.")
        reply.send(_cap(f"Answered task {task.id}'s question {index + 1}: "
                        f"{_cap(' '.join(text.split()), 300)}.{tail}", MAX_DISCORD_CHARS))

    def _cancel(self, task_id, reply) -> None:
        if not task_id:
            reply.refuse("Cancel what? Use `/cancel task:<id>`.")
            return
        reply.defer()
        # A proposal still inside its grace window is withdrawn before any CLI
        # session starts; anything else is cancelled at the next step boundary.
        if self.router.cancel_proposal(task_id):
            reply.send(f"Withdrew task {task_id} before it started.")
            return
        task = self.control.cancel(task_id)
        reply.send(f"Cancelling task {task_id}; it stops at the next step boundary "
                   f"(now {task.state.value}).")

    def _resume(self, task_id, provider, reply) -> None:
        reply.defer()
        task = self.control.resume(task_id, provider=provider)
        where = f" on {provider.value}" if provider else ""
        reply.send(f"Resumed task {task.id}{where} ({task.state.value}).")

    def _status(self, reply, project, task) -> None:
        if task is None:
            self._list_tasks(reply, project)
            return
        reply.defer(ephemeral=True)
        try:
            current = self.control.status(task.id) or task
        except ControlError:
            current = task
        project = project or self.stores.projects.get(current.project_id)
        if project is None:
            reply.send(f"Task {current.id} has no project on disk.")
            return
        reply.send(embed=status_embed(current, project))

    def _list_projects(self, reply) -> None:
        reply.defer(ephemeral=True)
        projects = [p for p in self.stores.projects.list() if not p.archived]
        reply.send(_listing("Projects", [f"`{p.id}` {p.name} — {p.root}" for p in projects]))

    def _list_tasks(self, reply, project) -> None:
        reply.defer(ephemeral=True)
        tasks = [t for t in self.stores.tasks.list() if t.state not in TERMINAL_STATES]
        if project is not None:
            tasks = [t for t in tasks if t.project_id == project.id]
        reply.send(_listing("Active tasks", [
            f"`{t.id}` {t.state.value} — {_cap(' '.join(t.brief.split()), 80)}" for t in tasks]))

    # -- intake and chat ---------------------------------------------------

    def _intake(self, brief, reply, project, *, provider: str | None = None,
                parse_named: bool = True) -> None:
        brief = brief.strip()
        named = _IN_PROJECT.match(brief) if parse_named else None
        target = project
        if named:
            target = self.router.place(named.group(1).strip())
            brief = named.group(2).strip()
            if target is None:
                reply.refuse(f"I don't know a project called "
                             f"`{_cap(named.group(1).strip(), 60)}`.")
                return
        if not brief:
            reply.refuse("A task needs a brief: `/task brief:<what to do>`.")
            return
        reply.defer()
        if target is None:
            target = self.stores.projects.inbox()
        try:
            extra = {"provider_override": provider} if provider else {}
            task = self.stores.tasks.create(target.id, brief, **extra)
            self.stores.tasks.save(task)
        except StoreError as exc:                 # ProjectArchived: archived meanwhile
            reply.send(_cap(f"I opened nothing: {exc}.", MAX_DISCORD_CHARS))
            return
        summary = " ".join(task.brief.split()).rstrip(".")
        try:
            self.control.start(task.id)
        except ControlError as exc:
            reply.send(f"Opened task {task.id} in {target.name}, but it cannot "
                       f"start yet: {exc}")
            return
        reply.send(_cap(f"Opened task {task.id} in {target.name}: {summary}. "
                        f"It will ask if anything is unclear.", MAX_DISCORD_CHARS))

    def _chat_thread(self, project, key) -> str:
        with self._lock:
            thread_id = self._chat_threads.get(key)
        cached = self.stores.threads.get(thread_id) if thread_id else None
        if cached is not None and not cached.archived:
            return thread_id
        thread = self.daemon.open_thread(project.id, Role.CHAT, ProviderName.FAST, {})
        with self._lock:
            self._chat_threads[key] = thread.id
        return thread.id

    def _chat(self, reply, where, project, text, spoken, incoming, *, skill=None) -> None:
        reply.defer()
        if where != "project" or project is None:
            project, key = self.stores.projects.inbox(), "dm"
        else:
            key = project.id
        thread_id = self._chat_thread(project, key)
        answer, finished = self._turn(thread_id, text, skill=skill)
        if answer.strip():
            for chunk in _split(answer.strip()):
                reply.send(chunk)
            if spoken:
                self._speak(reply, answer.strip())
        if finished is not None and (finished.get("data") or {}).get("proposal"):
            # Replay-protected by turn id inside the router, so a runner that
            # also consumes this event cannot open the task a second time.
            outcome = self.router.on_turn_finished(
                finished, Incoming(text=text, surface=incoming.surface,
                                   project_id=project.id, thread_id=thread_id))
            if isinstance(outcome, NeedsProject):
                reply.send(outcome.reply)
            elif outcome:
                reply.send(_cap(str(outcome), MAX_DISCORD_CHARS))

    def _turn(self, thread_id, text, *, skill=None):
        subscription = self.daemon.bus.subscribe(lambda r: r.get("thread_id") == thread_id)
        reply, finished = "", None
        try:
            turn_id = self.daemon.send(thread_id, UserMessage(text=text, skill=skill))
            deadline = time.monotonic() + self.turn_timeout_s
            while not self._stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    record = subscription.get(timeout=min(0.2, remaining))
                except queue.Empty:
                    continue
                try:
                    if record.get("kind") == "shutdown":
                        break
                    if record.get("turn_id") not in (None, turn_id):
                        continue
                    if record["kind"] == "text":
                        reply += (record.get("data") or {}).get("text", "")
                    elif record["kind"] == "turn_finished":
                        finished = record
                        break
                finally:
                    subscription.task_done()
        except Exception as exc:
            LOG.warning("Fast-path turn failed (%s)", type(exc).__name__)
            reply = reply or f"That chat thread could not run a turn ({type(exc).__name__})."
        finally:
            self.daemon.bus.unsubscribe(subscription)
        return reply, finished

    def _speak(self, reply, text) -> None:
        from jarvis import voice

        try:
            audio = voice.tts(text)
        except Exception as exc:
            self._announce(f"[discord] tts failed ({type(exc).__name__}); text-only reply")
            return
        name, _mime = v1gw._audio_filename(audio)
        reply.send(None, files=((name, audio),))

    # -- posting -----------------------------------------------------------

    def _owner_dm(self):
        with self._lock:
            if self._dm_cache:
                return self._dm_cache
        try:
            channel = str(self._dm_channel())
        except Exception as exc:
            LOG.warning("Discord DM channel unavailable (%s)", type(exc).__name__)
            return None
        with self._lock:
            self._dm_cache = channel
        return channel

    def _post(self, channel_id, content, files=(), *, embed=None, components=None):
        """A plain bot post. In a guild it is silent (decisions D1: nothing but
        the ping line notifies); in the DM it notifies as a DM always has."""
        try:
            return self.rest.post(channel_id, content=content, embed=embed, files=files,
                                  components=components, silent=not self._is_dm(channel_id))
        except Exception as exc:
            LOG.warning("Discord post failed (HTTP %s, code %s, %s)", *_facts(exc))
            return None


def _facts(exc) -> tuple:
    facts = describe(exc)
    return facts["status"], facts["code"], facts["error"]


class _Asked:
    """The parts of an approval request `allowlistable` reads, from bus data."""

    def __init__(self, tool, args, allowlistable=True):
        self.tool, self.args, self.allowlistable = tool, args, allowlistable


def _split(text: str, limit: int = MAX_DISCORD_CHARS) -> list[str]:
    """Chat text only. An approval is attached whole, never split (§11.3)."""
    chunks, rest = [], text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut <= 0:
            cut = rest.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    chunks.append(rest)
    return [chunk for chunk in chunks if chunk.strip()] or [text[:limit]]


def _listing(title: str, lines: list[str], limit: int = MAX_DISCORD_CHARS) -> str:
    """One message, capped, saying how many rows it could not show."""
    if not lines:
        return f"{title}: none."
    body = f"{title}:"
    for index, line in enumerate(lines):
        more = len(lines) - index
        tail = f"\n… and {more} more."
        if len(body) + 1 + len(line) + len(tail) > limit - 40:
            return body + tail
        body += "\n" + line
    return body
