"""Where an owner message lands, and what a typed yes is allowed to authorize.

Design §11.2. Everything v1 learned about this surface is carried over intact,
because every one of those rules was written after something went wrong:

* **Only the owner is ever heard, and never a bot.** v1's `should_respond` is
  still the gate; the only thing v2 changes is the *mention* requirement, and
  only inside a place Jarvis owns — a task thread or a project channel. The
  owner rule is not re-implemented here: this module asks v1's function about
  the same message with the mention supplied, so there stays exactly one copy
  of "who may trigger an agent".
* **A transcription can never resolve an authorization.** A voice note reaches
  `router.classify` with `spoken=True`, which already refuses to make a verb of
  it, and `_handle` additionally never reaches the approval parser on a spoken
  turn. A mishearing must not become a yes.
* **An answer counts only where the question was asked.** v1 scoped that to the
  DM channel; §11.2 applies it to threads. The channel an approval was posted
  to is recorded here under the request id, with the same one-shot lifetime as
  the request: a code typed in another thread, in the project channel, in a DM
  or in a guild channel Jarvis does not own resolves nothing and says so.
* **Two open questions and a bare "yes" is a guess**, so it asks for the code.
  Anything that is not a clear answer resolves nothing.

Nothing here decides permissions. The broker (§6 layer 5) owns that; this is
one surface it can ask through, and it denies on every failure path already.
"""
from __future__ import annotations

import logging
import queue
import re
import threading
import time

from jarvis import discord_gateway as v1gw
from jarvis.discord_approvals import _parse as _parse_answer
from jarvis.tools import discord as v1tools

from ..control import ControlError
from ..model import TERMINAL_STATES, ProviderName, Role
from ..provider import Decision, UserMessage
from ..router import (FastPath, Incoming, NeedsProject, NewTask, Steer, Verb,
                      classify)
from .render import _cap, approval_text, status_embed

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


class _V2Listener(v1gw.GatewayListener):
    """v1's socket (HELLO/IDENTIFY/heartbeat/4014) with v2's dispatch.

    v1 decides *whether* to dispatch inside its own loop, using its module-level
    `should_respond`; v2's decision needs a store lookup v1 cannot do, so the
    loop here hands every MESSAGE_CREATE to the router and the router gates it.
    `_serve` — including the 4014 "intents are off" stop, which must never turn
    into a retry loop — is inherited unchanged. Proposed to v1 as a `dispatch`
    hook in WP10b-notes.md, which would delete this override entirely.
    """

    def __init__(self, handler, announce=print):
        super().__init__(run_turn=lambda *_a, **_k: "", announce=announce)
        self._handler = handler

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
                self.announce(f"[discord] listening as {data['user'].get('username')} "
                              f"(replies only to the owner)")
            elif kind == "MESSAGE_CREATE":
                threading.Thread(target=self._handler,
                                 args=(data, self.bot_id, self.owner_id), daemon=True).start()


class DiscordRouter:
    """One Discord surface over the v2 daemon, stores, router and broker."""

    def __init__(self, daemon, stores, router, approvals, control, rest,
                 listener_factory=None, *, announce=None, dm_channel=None,
                 turn_timeout_s: float = TURN_TIMEOUT_S):
        self.daemon = daemon
        self.stores = stores
        self.router = router
        self.approvals = approvals
        self.control = control
        self.rest = rest
        self.bot_id = ""
        self.owner_id = ""
        self.turn_timeout_s = turn_timeout_s
        self._announce = announce or (lambda text: LOG.info("%s", text))
        self._dm_channel = dm_channel or v1tools.owner_dm_channel
        self._dm_cache = None
        self._lock = threading.RLock()
        self._approval_channels: dict[str, str] = {}
        self._answered_here: set[str] = set()
        self._chat_threads: dict[str, str] = {}
        factory = listener_factory or (lambda handler: _V2Listener(handler, self._announce))
        self.listener = factory(self.handle)
        self._stop = threading.Event()
        self._subscription = daemon.bus.subscribe(lambda r: r.get("kind") in APPROVAL_KINDS)
        self._worker = threading.Thread(target=self._watch, name="jarvis-discord-approvals",
                                        daemon=True)
        self._worker.start()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self.listener.start()
        self.owner_id = str(getattr(self.listener, "owner_id", "") or self.owner_id)

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

    # -- placement (§11.1) -------------------------------------------------

    def _locate(self, message, channel_id):
        """-> (where, project, task). `where` is dm / task / project / other."""
        if "guild_id" not in message:
            return "dm", None, None
        tasks = self.stores.tasks.list(discord_thread_id=channel_id)
        if tasks:
            task = tasks[0]
            return "task", self.stores.projects.get(task.project_id), task
        projects = self.stores.projects.list(discord_channel_id=channel_id)
        if projects:
            return "project", projects[0], None
        return "other", None, None

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
        if not should_respond(message, bot_id, owner_id, owned=where in ("dm", "task", "project")):
            return
        if where == "dm":
            with self._lock:
                self._dm_cache = channel_id
        text = v1gw.strip_mention(message.get("content") or "", bot_id)
        spoken = False
        note = v1gw.voice_attachment(message)
        if note is not None:
            try:
                text, spoken = self._hear(note), True
            except Exception as exc:
                self._post(channel_id, f"I couldn't make out that voice message ({exc}).")
                return
        if not text.strip():
            return
        # Typed only: a transcription is one mishearing away from "yes".
        if not spoken and self._approval_answer(channel_id, text):
            return
        incoming = Incoming(
            text=text,
            surface=(project.discord_channel_id if project and project.discord_channel_id
                     else channel_id),
            project_id=project.id if project else None,
            task_id=task.id if task else None,
            spoken=spoken)
        destination = classify(incoming)
        if isinstance(destination, Verb):
            self._verb(destination, channel_id, project, task)
        elif isinstance(destination, NewTask):
            self._intake(destination.text, channel_id, project)
        elif isinstance(destination, Steer):
            self._steer(destination.task_id, text, channel_id, spoken)
        elif isinstance(destination, FastPath):
            self._chat(channel_id, where, project, text, spoken, incoming)

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

    def _approval_answer(self, channel_id: str, text: str) -> bool:
        """True when this message was an answer (or a refusal to guess at one)."""
        verdict, code = _parse_answer(text)
        if verdict is None or text.strip().split()[0].lower() in _NOT_AN_ANSWER:
            return False
        with self._lock:
            channels = dict(self._approval_channels)
        open_requests = list(self.approvals.pending())
        if code:
            match = next((r for r in open_requests if r.code == code), None)
            if match is None:
                self._post(channel_id, f"No open authorization with code `{code}`.")
                return True
            if channels.get(match.req_id) != channel_id:
                self._post(channel_id, f"That code (`{code}`) was asked elsewhere — "
                                       f"answer it where it was posted. Nothing ran.")
                return True
            target = match
        else:
            mine = [r for r in open_requests if channels.get(r.req_id) == channel_id]
            if not mine:
                return False
            if len(mine) > 1:
                codes = ", ".join(f"`{r.code}` ({r.tool})" for r in mine)
                self._post(channel_id, f"Which one? Reply with the code: {codes}.")
                return True
            target = mine[0]
        decision = Decision.DENY if verdict == "deny" else Decision.ALLOW
        try:
            with self._lock:
                self._answered_here.add(target.req_id)
            self.approvals.resolve(target.req_id, decision, always=(verdict == "always"))
        except ValueError:
            self._post(channel_id, f"That one (`{target.code}`) already expired — nothing ran.")
            return True
        if decision is Decision.DENY:
            self._post(channel_id, f"Denied. `{target.tool}` did not run.")
        elif verdict == "always":
            self._post(channel_id, f"Authorized `{target.tool}`, and I will stop asking "
                                   f"about this one.")
        else:
            self._post(channel_id, f"Authorized. Running `{target.tool}` now.")
        return True

    def _watch(self) -> None:
        while not self._stop.is_set():
            try:
                record = self._subscription.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if record.get("kind") == "shutdown":
                    break
                if record["kind"] == "approval_requested":
                    self._post_approval(record.get("data") or {})
                else:
                    self._post_resolution(record.get("data") or {})
            except Exception as exc:
                LOG.warning("Discord approval delivery failed (%s)", type(exc).__name__)
            finally:
                self._subscription.task_done()

    def _post_approval(self, data: dict) -> None:
        req_id = str(data.get("req_id") or "")
        channel = None
        task_id = data.get("task_id")
        if task_id:
            task = self.stores.tasks.get(task_id)
            channel = task.discord_thread_id if task else None
        channel = channel or self._owner_dm()
        if not channel or not req_id:
            # The broker denies on timeout; it does not need rescuing here.
            self._announce("[approval] Discord could not deliver this request")
            return
        args = dict(data.get("args") or {})
        command = data.get("command")
        if isinstance(command, str) and not isinstance(args.get("command"), str):
            # The owner must see the entire command, never a digest of it.
            args["command"] = command
        body = approval_text(str(data.get("tool") or ""), args,
                             str(data.get("code") or ""), str(data.get("origin") or ""))
        with self._lock:
            self._approval_channels[req_id] = str(channel)
        try:
            # Never split or cap: DiscordRest attaches an over-long approval whole.
            self.rest.post(channel, content=body)
        except Exception:
            with self._lock:
                self._approval_channels.pop(req_id, None)
            raise

    def _post_resolution(self, data: dict) -> None:
        req_id = str(data.get("req_id") or "")
        with self._lock:
            channel = self._approval_channels.pop(req_id, None)
            here = req_id in self._answered_here
            self._answered_here.discard(req_id)
        if not channel:
            return
        where = "answered here" if here else "answered on another surface"
        self._post(channel, f"Approval `{data.get('code', '')}` for "
                            f"`{data.get('tool', '')}`: {data.get('resolution', '')} ({where}).")

    # -- verbs -------------------------------------------------------------

    def _verb(self, verb: Verb, channel_id, project, task) -> None:
        name, argument = verb.name, verb.argument.strip()
        try:
            if name in ("yes", "no", "always"):
                # Only reachable with no open request in this channel: the
                # answer parser above claims every message that answers one.
                self._post(channel_id, f"No open authorization with code `{argument}`.")
            elif name in ("steer", "redirect"):
                self._steer(task.id if task else None, argument, channel_id, False)
            elif name == "cancel":
                self._cancel(argument or (task.id if task else ""), channel_id)
            elif name == "status":
                self._status(channel_id, project, task)
            elif name == "projects":
                self._list_projects(channel_id)
            elif name == "tasks":
                self._list_tasks(channel_id, project)
            elif name == "resume":
                self._resume(argument, channel_id)
        except ControlError as exc:
            self._post(channel_id, str(exc))

    def _steer(self, task_id, text, channel_id, spoken) -> None:
        if not task_id:
            self._post(channel_id, "There is no task here to steer — say `task: …` to open one.")
            return
        self.control.steer(task_id, text, spoken=spoken)
        self._post(channel_id, f"Noted — steering task {task_id} at its next step.")

    def _cancel(self, task_id, channel_id) -> None:
        if not task_id:
            self._post(channel_id, "Cancel what? Reply `cancel <task id>`.")
            return
        # A proposal still inside its grace window is withdrawn before any CLI
        # session starts; anything else is cancelled at the next step boundary.
        if self.router.cancel_proposal(task_id):
            self._post(channel_id, f"Withdrew task {task_id} before it started.")
            return
        task = self.control.cancel(task_id)
        self._post(channel_id, f"Cancelling task {task_id}; it stops at the next step boundary "
                               f"(now {task.state.value}).")

    def _resume(self, argument, channel_id) -> None:
        match = _RESUME.match(argument)
        if not match:
            self._post(channel_id, "Reply `resume <task id>` or `resume <task id> on <provider>`.")
            return
        task_id, provider = match.group(1), match.group(2)
        try:
            provider = ProviderName(provider.lower()) if provider else None
        except ValueError:
            self._post(channel_id, "Provider must be claude, codex or fast.")
            return
        task = self.control.resume(task_id, provider=provider)
        where = f" on {provider.value}" if provider else ""
        self._post(channel_id, f"Resumed task {task.id}{where} ({task.state.value}).")

    def _status(self, channel_id, project, task) -> None:
        if task is None:
            self._list_tasks(channel_id, project)
            return
        try:
            current = self.control.status(task.id) or task
        except ControlError:
            current = task
        project = project or self.stores.projects.get(current.project_id)
        if project is None:
            self._post(channel_id, f"Task {current.id} has no project on disk.")
            return
        try:
            self.rest.post(channel_id, embed=status_embed(current, project))
        except Exception as exc:
            LOG.warning("Discord status post failed (%s)", type(exc).__name__)

    def _list_projects(self, channel_id) -> None:
        projects = self.stores.projects.list()
        self._post(channel_id, _listing(
            "Projects", [f"`{p.id}` {p.name} — {p.root}" for p in projects]))

    def _list_tasks(self, channel_id, project) -> None:
        tasks = [t for t in self.stores.tasks.list() if t.state not in TERMINAL_STATES]
        if project is not None:
            tasks = [t for t in tasks if t.project_id == project.id]
        self._post(channel_id, _listing("Active tasks", [
            f"`{t.id}` {t.state.value} — {_cap(' '.join(t.brief.split()), 80)}" for t in tasks]))

    # -- intake and chat ---------------------------------------------------

    def _intake(self, brief, channel_id, project) -> None:
        brief = brief.strip()
        named = _IN_PROJECT.match(brief)
        target = project
        if named:
            target = self.router.place(named.group(1).strip())
            brief = named.group(2).strip()
            if target is None:
                self._post(channel_id, f"I don't know a project called "
                                       f"`{_cap(named.group(1).strip(), 60)}`.")
                return
        if not brief:
            self._post(channel_id, "A task needs a brief: `task: <what to do>`.")
            return
        if target is None:
            target = self.stores.projects.inbox()
        task = self.stores.tasks.create(target.id, brief)
        self.stores.tasks.save(task)
        summary = " ".join(task.brief.split()).rstrip(".")
        try:
            self.control.start(task.id)
        except ControlError as exc:
            self._post(channel_id, f"Opened task {task.id} in {target.name}, but it cannot "
                                   f"start yet: {exc}")
            return
        self._post(channel_id, _cap(f"Opened task {task.id} in {target.name}: {summary}. "
                                    f"It will ask if anything is unclear.", MAX_DISCORD_CHARS))

    def _chat_thread(self, project, key) -> str:
        with self._lock:
            thread_id = self._chat_threads.get(key)
        if thread_id and self.stores.threads.get(thread_id) is not None:
            return thread_id
        thread = self.daemon.open_thread(project.id, Role.CHAT, ProviderName.FAST, {})
        with self._lock:
            self._chat_threads[key] = thread.id
        return thread.id

    def _chat(self, channel_id, where, project, text, spoken, incoming) -> None:
        if where != "project" or project is None:
            project, key = self.stores.projects.inbox(), "dm"
        else:
            key = project.id
        thread_id = self._chat_thread(project, key)
        reply, finished = self._turn(thread_id, text)
        if reply.strip():
            for chunk in _split(reply.strip()):
                self._post(channel_id, chunk)
            if spoken:
                self._speak(channel_id, reply.strip())
        if finished is not None and (finished.get("data") or {}).get("proposal"):
            # Replay-protected by turn id inside the router, so a runner that
            # also consumes this event cannot open the task a second time.
            outcome = self.router.on_turn_finished(
                finished, Incoming(text=text, surface=incoming.surface,
                                   project_id=project.id, thread_id=thread_id))
            if isinstance(outcome, NeedsProject):
                self._post(channel_id, outcome.reply)
            elif outcome:
                self._post(channel_id, _cap(str(outcome), MAX_DISCORD_CHARS))

    def _turn(self, thread_id, text):
        subscription = self.daemon.bus.subscribe(lambda r: r.get("thread_id") == thread_id)
        reply, finished = "", None
        try:
            turn_id = self.daemon.send(thread_id, UserMessage(text=text))
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

    def _speak(self, channel_id, reply) -> None:
        from jarvis import voice

        try:
            audio = voice.tts(reply)
        except Exception as exc:
            self._announce(f"[discord] tts failed ({type(exc).__name__}); text-only reply")
            return
        name, _mime = v1gw._audio_filename(audio)
        self._post(channel_id, None, files=((name, audio),))

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

    def _post(self, channel_id, content, files=()):
        try:
            return self.rest.post(channel_id, content=content, files=files)
        except Exception as exc:
            LOG.warning("Discord post failed (%s)", type(exc).__name__)
            return None


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
        if len(body) + 1 + len(line) + len(tail) > limit:
            return body + tail
        body += "\n" + line
    return body
