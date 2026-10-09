"""Jarvis v2's state owner and loopback JSON/SSE API. No routing or policy.

Providers are caller-owned instances. main() alone loads the configured roster.
Threads save their Brief beside thread.json so resuming never invents a policy.
"""
from __future__ import annotations

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
from .approvals import ApprovalRequest, PendingApprovals
from .bus import EventBus
from .control import ControlError
from .permissions import PermitContext, build_permit
from .model import (PermissionProfile, Project, ProviderName, Role, Thread,
                    from_json, to_json, utcnow)
from .provider import (Brief, BriefRefused, Decision, Event, EventKind,
                       PermissionCallback, Provider, SessionHandle, SessionLost, Usage,
                       UserMessage)
from .stores import ProjectArchived, Stores, StoreError, _validate, _write_bytes
from . import worktrees

LOG = logging.getLogger(__name__)
STOP_TIMEOUT = 2.0

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
GATE_KINDS = {EventKind.APPROVAL_REQUESTED: "gate_requested",
              EventKind.APPROVAL_RESOLVED: "gate_resolved"}
_TASK_VERBS = ("start", "steer", "cancel", "resume", "answer")


class DaemonError(RuntimeError):
    """Lifecycle conflict (HTTP 409)."""


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


VIAS = ("hud", "discord", "dm", "system", "peer")


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
class _Session:
    thread: Thread
    brief: Brief
    provider: Provider
    handle: SessionHandle | None = None
    worker: threading.Thread | None = None
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


class Daemon:
    def __init__(self, stores: Stores, providers: dict[ProviderName, Provider],
                 permit_factory: Callable[[str, Brief], PermissionCallback] | None,
                 port: int, approvals: PendingApprovals | None = None, *,
                 face_port: int | None = None, workshop_port: int | None = None):
        self.stores = stores
        self.providers = {ProviderName(k): v for k, v in providers.items()}
        self.port = port
        self.bus = EventBus()
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
        self._lock = threading.RLock()
        self._worktree_lock = threading.Lock()
        self._sessions: dict[str, _Session] = {}
        self._stopping = False
        self._server = None
        self._server_thread = None
        # Port zero is an isolated embedded/test daemon on three ephemeral ports.
        self.face_port = face_port if face_port is not None else (0 if port == 0 else config.FACE_PORT)
        self.workshop_port = workshop_port if workshop_port is not None else (0 if port == 0 else config.WORKSHOP_PORT)
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
            self.started_at = time.monotonic()
            for listener in servers:
                listener.daemon_threads = True
                worker = threading.Thread(target=lambda s=listener: s.serve_forever(0.05),
                                          name="jarvis-v2-http", daemon=True)
                self._listeners.append((listener, worker))
                worker.start()
            self._server_thread = self._listeners[0][1]
            from .hud_api import connect_controls
            connect_controls(self)
            self.schedules.start()

    def status(self) -> dict:
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
        with self._lock:
            return {"jarvis-daemon": True, "version": 2,
                    "uptime": time.monotonic() - self.started_at, "providers": providers,
                    "threads_open": sum(s.handle is not None for s in self._sessions.values()),
                    "turns_running": sum(s.worker is not None for s in self._sessions.values()),
                    # The HUD builds preview URLs from this rather than a
                    # hard-coded 8403: a HUD served by any other daemon (a
                    # test's, on an ephemeral port) must never reach the
                    # owner's live workshop origin.
                    "workshop_port": self.workshop_port}

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
            thread = self.stores.threads.create(project_id, role, provider,
                                               model=brief.model, effort=brief.effort, task_id=brief.task_id,
                                               cwd=brief.cwd)
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
        with self._lock:
            self._active()
            if thread.id in self._sessions:
                raise DaemonError("thread session is already open or opening")
            provider = self.providers.get(thread.provider)
            if provider is None:
                raise DaemonError(f"provider {thread.provider.value} unavailable: not in roster")
            session = _Session(thread, brief, provider)
            self._sessions[thread.id] = session
        try:
            # The saved brief is never rewritten; a chat thread's provider is
            # handed a copy carrying the thread's current choice instead, so a
            # resume picks up a model changed since open (decisions A1). The
            # permit is built over that same copy: one brief per session.
            run_brief = self._run_brief(session)
            permit = self.permit_factory(thread.id, run_brief)
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
                _write_bytes(path, json.dumps(to_json(brief), allow_nan=False).encode())
                self.stores.threads.save(thread)
            return session
        except BaseException:
            if session.handle is not None:
                self._cleanup(session)
            with self._lock:
                self._sessions.pop(thread.id, None)
            raise

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
        owner's chat thread — the thread's effective (model, effort)."""
        from . import thread_model
        if not thread_model.is_chat(session.thread):
            return session.brief
        choice = thread_model.effective(session.thread)
        session.applied = session.announced = choice
        session.applied_record = (session.thread.model, session.thread.effort)
        return replace(session.brief, model=choice[0], effort=choice[1])

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
            self._switch_refused(session, want, exc)
            return
        with self._lock:
            session.applied = want
            session.applied_record = (session.thread.model, session.thread.effort)
            if want != session.announced:
                session.announced = want
                self._model_line(session.thread, want, "follows the default")

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
        _validate(message, UserMessage)
        data = user_data(message)
        with self._lock:
            self._active()
            session = self._sessions.get(thread_id)
        if session is None:
            from .projects import is_archived
            stored = self.require(self.stores.threads, thread_id)
            if stored.archived or is_archived(self.stores, stored.project_id):
                raise DaemonError("this thread is archived; restore it from the Archive to continue")
            session = self.resume_thread(thread_id)
        with self._lock:
            self._active()
            if session.handle is None or session.closing:
                raise DaemonError("thread session is opening or closing")
            if session.worker is not None:
                raise DaemonError("a turn is already running on this thread")
            # Checked under the same lock an archive takes, so a turn cannot
            # start in a thread the owner is archiving (decisions B1).
            from .projects import is_archived
            if session.thread.archived or is_archived(self.stores, session.thread.project_id):
                raise DaemonError("this thread is archived; restore it from the Archive to continue")
            session.cancelled.clear()
            session.turn_id = uuid.uuid4().hex
            worker = threading.Thread(target=self._turn, args=(session, message),
                                      name=f"jarvis-turn-{thread_id}", daemon=True)
            turn_id = session.turn_id
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
            session.worker = worker
            worker.start()
            return turn_id

    # -- approvals (design §6) ---------------------------------------------

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
        ctx = PermitContext(
            thread_id=thread_id, brief=brief,
            provider=thread.provider.value if thread is not None else "",
            project=project, task=task)
        return build_permit(ctx, self.approvals)

    def _approval_requested(self, request: ApprovalRequest) -> None:
        self.bus.publish({"kind": "approval_requested",
                          "thread_id": request.thread_id,
                          "data": request.to_json()})

    def _approval_resolved(self, request: ApprovalRequest, decision, resolution: str) -> None:
        self.bus.publish({"kind": "approval_resolved",
                          "thread_id": request.thread_id,
                          "data": {"req_id": request.req_id, "code": request.code,
                                   "tool": request.tool, "command": request.command,
                                   "origin": request.origin,
                                   "decision": getattr(decision, "value", str(decision)),
                                   "resolution": resolution}})

    def resolve_approval(self, req_id: str, decision, always: bool = False) -> dict:
        try:
            entry = self.approvals.resolve(req_id, decision, always=always)
        except ValueError as exc:
            raise APIError(404, str(exc)) from exc
        return {"ok": True, "req_id": req_id,
                "decision": Decision(decision).value, "allowlisted": entry}

    def _record(self, session, event):
        with self._lock:
            if session.retired:
                return
            if event.thread_id != session.thread.id:
                raise ValueError("provider event belongs to another thread")
            thread = session.thread
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
                thread.updated = utcnow()
                self.stores.threads.save(thread)
            record = {**to_json(event), "project_id": thread.project_id, "turn_id": session.turn_id,
                      "at": utcnow(), "event_id": uuid.uuid4().hex}
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
                self.stores.threads._append(thread.id, "log.jsonl", record)
                return
            if event.kind != EventKind.TEXT_DELTA:
                # WP1 log() accepts text only; preserve full structured events
                # through its serialized append primitive, in the same log.
                self.stores.threads._append(thread.id, "log.jsonl", record)
            if event.kind in (EventKind.USAGE, EventKind.ERROR):
                # Durable and synchronous, including chat. The stable id makes
                # the runner's subsequent ledger delivery idempotent.
                ledger = self.router.ledger
                before = ledger.totals(provider=thread.provider.value)
                old_quota = ledger.quota(thread.provider.value) if hasattr(ledger, "quota") else None
                rows = len(ledger._rows)
                ledger.on_event(record)
                new_quota = ledger.quota(thread.provider.value) if hasattr(ledger, "quota") else None
                if (before != ledger.totals(provider=thread.provider.value) or old_quota != new_quota
                        or (event.kind == EventKind.ERROR and len(ledger._rows) != rows)):
                    self.bus.publish({"kind": "usage_updated", "data": {"provider": thread.provider.value}})
            self.bus.publish(record)

    def _finish(self, session, event):
        with self._lock:
            if session.retired:
                return
            if event.thread_id != session.thread.id:
                raise ValueError("provider event belongs to another thread")
            session.thread.turns += 1
            session.thread.updated = utcnow()
            session.thread.provider_session_id = session.handle.provider_session_id
            self.stores.threads.save(session.thread)
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

    def _turn(self, session, message):
        terminal = None
        failed = False
        fatal = False
        try:
            if session.cancelled.is_set():
                return
            self._apply_choice(session)
            cancel_forwarded = False
            for event in session.provider.send(session.handle, message):
                if event.thread_id != session.thread.id:
                    raise ValueError("provider event belongs to another thread")
                # Providers may clear an old stop flag when send begins. Repeat
                # cancellation after their first yield to close that start race.
                if session.cancelled.is_set() and not cancel_forwarded:
                    session.provider.interrupt(session.handle)
                    cancel_forwarded = True
                if event.kind == EventKind.TURN_FINISHED:
                    terminal = event
                    break
                self._record(session, event)
                failed |= event.kind == EventKind.ERROR
                fatal |= event.kind == EventKind.ERROR and bool((event.data or {}).get("fatal"))
        except Exception as exc:
            failed = True
            try:
                self._record(session, Event(EventKind.ERROR, session.thread.id,
                                           {"message": f"{type(exc).__name__}: {exc}", "fatal": False}))
            except Exception:
                LOG.exception("Cannot record turn error for %s", session.thread.id)
        finally:
            try:
                stop = "interrupted" if session.cancelled.is_set() else "error" if failed else "end"
                if terminal is None:
                    terminal = Event(EventKind.TURN_FINISHED, session.thread.id, {"stop": stop})
                elif session.cancelled.is_set():
                    terminal = Event(terminal.kind, terminal.thread_id, {**terminal.data, "stop": "interrupted"})
                self._finish(session, terminal)
            except Exception:
                LOG.exception("Cannot finish thread %s", session.thread.id)
            finally:
                with self._lock:
                    session.worker = None
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
                    if session.lost and self._sessions.get(session.thread.id) is session:
                        # The provider closed it; the next send resumes afresh.
                        self._sessions.pop(session.thread.id, None)
                if session.lost:
                    self._cleanup(session)

    def _session(self, thread_id):
        self.require(self.stores.threads, thread_id)
        with self._lock:
            session = self._sessions.get(thread_id)
            if session is None or session.handle is None or session.closing:
                raise DaemonError("thread session is not open")
            return session

    def interrupt(self, thread_id):
        session = self._session(thread_id)
        with self._lock:
            if session.worker is None:
                raise DaemonError("no turn is running on this thread")
            session.cancelled.set()
        session.provider.interrupt(session.handle)

    def answer(self, thread_id, req_id, decision):
        session = self._session(thread_id)
        session.provider.answer(session.handle, req_id, decision)
        # In the log too, beside the question it closes, so a surface catching
        # up from the log (the Discord mirror) knows the question is no
        # longer open. Nothing the provider sees.
        try:
            self.stores.threads._append(thread_id, "log.jsonl", {
                "kind": "question_answered", "at": utcnow(), "thread_id": thread_id,
                "turn_id": session.turn_id, "data": {"req_id": req_id}})
        except Exception:
            LOG.warning("Cannot log the answer on thread %s", thread_id, exc_info=True)
        # Whoever answered (the HUD, Discord), every surface still showing the
        # question as open hears that it is not (PR C).
        self.bus.publish({"kind": "question_answered", "thread_id": thread_id,
                          "project_id": session.thread.project_id,
                          "data": {"req_id": req_id}})

    def _cleanup(self, session):
        for operation in (session.provider.interrupt, session.provider.close):
            try:
                operation(session.handle)
            except Exception:
                LOG.exception("Provider cleanup failed for %s", session.thread.id)

    def close_thread(self, thread_id):
        session = self._session(thread_id)
        with self._lock:
            session.closing = True
            session.cancelled.set()
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
        self._lifecycle("thread_closed", session.thread)

    def _lifecycle(self, kind, obj):
        record = {"kind": kind, "project_id": obj.project_id, "data": to_json(obj)}
        record["thread_id" if isinstance(obj, Thread) else "task_id"] = obj.id
        self.bus.publish(record)

    def stop(self):
        self.schedules.stop()
        deadline = time.monotonic() + STOP_TIMEOUT
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
            sessions = list(self._sessions.values())
            jobs = [s.worker for s in sessions if s.worker is not None]
            for session in sessions:
                session.closing = True
                session.cancelled.set()
                if session.handle is not None:
                    job = threading.Thread(target=self._cleanup, args=(session,), daemon=True)
                    job.start()
                    jobs.append(job)
        for job in jobs:
            job.join(max(0, deadline - time.monotonic()))
        with self._lock:
            for session in sessions:
                session.retired = True
            self._sessions.clear()
        self.bus.close()
        for server, worker in self._listeners:
            server.shutdown()
            server.server_close()
            worker.join(max(0, deadline - time.monotonic()))
        self._listeners.clear()
        self._server = None
        if any(job.is_alive() for job in jobs):
            LOG.warning("Shutdown bound reached; provider work remains on daemon threads")


def _object(value, allowed, required=()):
    if not isinstance(value, dict):
        raise APIError(400, "body must be a JSON object")
    if value.keys() - set(allowed):
        raise APIError(400, "unknown fields: " + ", ".join(sorted(value.keys() - set(allowed))))
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


def _routing_models(project):
    """A project's per-role model overrides name only models Jarvis knows for
    that CLI, the same rule `routing.json` and `/route` follow (400 if not)."""
    from .router import check_project_models
    try:
        check_project_models(project.routing.models)
    except ValueError as exc:
        raise APIError(400, str(exc)) from exc


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise APIError(400, f"{name} must be a nonempty string")
    return value


def _handler(daemon):
    class Handler(BaseHTTPRequestHandler):
        # No CORS: the preview origin must never invoke this approval surface.
        def log_message(self, *args):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(2)
            self._streaming = False

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
                    LOG.warning("%s %s -> %d %s", self.command, self.path, status, exc)
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
                    LOG.exception("HTTP %s %s failed", self.command, self.path)
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
            if getattr(self.server, "preview_only", False):
                return hud_api.preview_route(self, daemon, parts, query)
            mounted = hud_api.route(self, daemon, parts, query)
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
                _object(query, ("thread", "project"))
                filters = {}
                for name, store in (("thread", stores.threads), ("project", stores.projects)):
                    if name in query:
                        daemon.require(store, query[name])
                        filters[name + "_id"] = query[name]
                self._events(filters)
                return 200, None
            if parts == ["projects"]:
                if method == "GET":
                    # Archived projects are hidden from every list (decisions B1);
                    # the HUD's Archive view reads them from /archive.
                    return 200, [to_json(p) for p in safe_list(stores.projects) if not p.archived]
                if method == "POST":
                    body = _no_channel_field(self._body())
                    body = _object(body, ("name", "root", "profile", "routing", "extra_dirs",
                                          "always_ask"), ("name", "root"))
                    # One create path for the HUD and Discord's `/project new`
                    # (B2): a colliding name is numbered, never refused.
                    from .projects import create_project
                    values = dict(body)
                    created = create_project(daemon, values.pop("name"), values.pop("root"),
                                             **values)
                    return 201, to_json(created)
            if len(parts) == 2 and parts[0] == "projects":
                with daemon._lock:
                    project = daemon.require(stores.projects, parts[1])
                    if method == "GET":
                        return 200, to_json(project)
                    if method == "PATCH":
                        daemon._active()
                        body = _no_channel_field(self._body())
                        body = _object(body, ("name", "root", "profile", "routing", "extra_dirs",
                                              "always_ask"))
                        before = project
                        project = from_json(Project, {**to_json(project), **body})
                        _validate(project, Project)
                        if "routing" in body:
                            # Only when written: a project saved before the
                            # check must stay renameable.
                            _routing_models(project)
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
                        return 200, to_json(project)
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
                    return 201, to_json(daemon.open_thread(**body))
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
                if method == "POST" and action == "interrupt":
                    _object(self._body(), ())
                    daemon.interrupt(thread_id)
                    return 200, {"ok": True}
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
                body = _object(self._body(), ("decision", "always"), ("decision",))
                always = body.get("always", False)
                if not isinstance(always, bool):
                    raise APIError(400, "always must be a boolean")
                return 200, daemon.resolve_approval(
                    parts[1], Decision(_text(body["decision"], "decision")), always)
            if len(parts) in (2, 3) and parts[0] == "tasks":
                task = daemon.require(stores.tasks, parts[1])
                if len(parts) == 2 and method == "GET":
                    return 200, to_json(task)
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
            body = self._body()
            try:
                if verb == "start":
                    _object(body, ())
                    return 200, to_json(runner.start(task_id))
                if verb == "steer":
                    _object(body, ("text", "spoken"), ("text",))
                    spoken = body.get("spoken", False)
                    if not isinstance(spoken, bool):
                        raise APIError(400, "spoken must be a boolean")
                    runner.steer(task_id, _text(body["text"], "text"), spoken=spoken)
                    return 200, {"ok": True}
                if verb == "cancel":
                    _object(body, ())
                    return 200, to_json(runner.cancel(task_id))
                if verb == "resume":
                    _object(body, ("provider",))
                    provider = body.get("provider")
                    return 200, to_json(runner.resume(
                        task_id, provider=ProviderName(provider) if provider else None))
                _object(body, ("index", "text"), ("index", "text"))
                index = body["index"]
                if not isinstance(index, int) or isinstance(index, bool):
                    raise APIError(400, "index must be an integer")
                return 200, to_json(runner.answer_question(task_id, index,
                                                           _text(body["text"], "text")))
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

        def _events(self, filters):
            q = daemon.bus.subscribe(filters)
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


def main() -> int:
    # Imported here, not at module scope: the hatch reads the daemon it is
    # given and nothing in the daemon needs it, so keeping the edge one-way
    # keeps `import jarvis.v2.daemon` free of the subprocess machinery.
    from .hatch import EscapeHatch
    from .runner import TaskRunner

    configure_logging()
    # The Codex models the account offered last time, before anything reads
    # routing: an entry only that catalog offers must not degrade on restart.
    from .router import load_codex_catalog
    load_codex_catalog()

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
    daemon = Daemon(Stores(), load_providers(), None, config.DAEMON_PORT, approvals)
    if approvals is not None:
        holder["daemon"] = daemon
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
        purger.start()
        LOG.info("Jarvis v2 listening on 127.0.0.1:%s", daemon.port)
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
        # The gateway first, so no verb arrives at a runner that is stopping;
        # then the runner, so its workers see interrupted turns rather than a
        # dead daemon; then the Reporter, flushing within 2 s (all three inside
        # `discord.stop()`). Nothing is cancelled — a task left RUNNING is
        # re-admitted by `serve()`'s recovery next boot.
        if discord is not None:
            discord.stop()
        daemon.runner.stop()                    # idempotent if Discord stopped it
        hatch.stop()
        daemon.stop()
        signal.signal(signal.SIGTERM, previous)
    return 0
