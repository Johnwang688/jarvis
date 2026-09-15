"""Jarvis v2's state owner and loopback JSON/SSE API. No routing or policy.

Providers are caller-owned instances. main() alone loads the configured roster.
Threads save their Brief beside thread.json so resuming never invents a policy.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
import errno
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
import logging
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
from .bus import EventBus
from .model import (PermissionProfile, Project, ProviderName, Role, Thread,
                    from_json, to_json, utcnow)
from .provider import (Brief, BriefRefused, Decision, Event, EventKind,
                       PermissionCallback, Provider, SessionHandle, Usage, UserMessage)
from .stores import Stores, StoreError, _validate, _write_bytes
from . import worktrees

LOG = logging.getLogger(__name__)
STOP_TIMEOUT = 2.0


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


class Daemon:
    def __init__(self, stores: Stores, providers: dict[ProviderName, Provider],
                 permit_factory: Callable[[str, Brief], PermissionCallback], port: int):
        self.stores = stores
        self.providers = {ProviderName(k): v for k, v in providers.items()}
        self.permit_factory = permit_factory
        self.port = port
        self.bus = EventBus()
        self._lock = threading.RLock()
        self._worktree_lock = threading.Lock()
        self._sessions: dict[str, _Session] = {}
        self._stopping = False
        self._server = None
        self._server_thread = None
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
            server.daemon_threads = True
            self._server = server
            self.port = server.server_address[1]
            self.started_at = time.monotonic()
            self._server_thread = threading.Thread(target=lambda: server.serve_forever(0.05),
                                                   name="jarvis-v2-http", daemon=True)
            self._server_thread.start()

    def status(self) -> dict:
        providers = {}
        for name, provider in self.providers.items():
            try:
                ok, reason = provider.health()
            except Exception as exc:
                ok, reason = False, f"{type(exc).__name__}: {exc}"
            providers[name.value] = {"ok": bool(ok), "reason": str(reason)}
        with self._lock:
            return {"jarvis-daemon": True, "version": 2,
                    "uptime": time.monotonic() - self.started_at, "providers": providers,
                    "threads_open": sum(s.handle is not None for s in self._sessions.values()),
                    "turns_running": sum(s.worker is not None for s in self._sessions.values())}

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

    def make_brief(self, project, role, value):
        if isinstance(value, Brief):
            brief = value
        else:
            if not isinstance(value, dict):
                raise APIError(400, "brief must be an object")
            brief = from_json(Brief, {"cwd": project.root, "profile": project.profile,
                                      "always_ask": project.always_ask, "role": role, **value})
        _validate(brief, Brief)
        if brief.role != role or not Path(brief.cwd).is_absolute():
            raise APIError(400, "brief role must match thread role and cwd must be absolute")
        return brief

    def open_thread(self, project_id, role, provider, brief) -> Thread:
        project = self.require(self.stores.projects, project_id)
        role, provider = Role(role), ProviderName(provider)
        brief = self.make_brief(project, role, brief)
        if brief.task_id is not None:
            task = self.require(self.stores.tasks, brief.task_id)
            if task.project_id != project_id:
                raise APIError(400, "brief task belongs to another project")
        if provider not in self.providers:
            raise DaemonError(f"provider {provider.value} unavailable: not in roster")
        with self._lock:
            self._active()
            thread = self.stores.threads.create(project_id, role, provider,
                                               model=brief.model, effort=brief.effort, task_id=brief.task_id)
        self._open(thread, brief)
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
            permit = self.permit_factory(thread.id, brief)
            method = provider.resume if thread.provider_session_id else provider.start
            handle = method(thread, brief, permit)
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
        with self._lock:
            self._active()
            session = self._sessions.get(thread_id)
        if session is None:
            session = self.resume_thread(thread_id)
        with self._lock:
            self._active()
            if session.handle is None or session.closing:
                raise DaemonError("thread session is opening or closing")
            if session.worker is not None:
                raise DaemonError("a turn is already running on this thread")
            session.cancelled.clear()
            session.turn_id = uuid.uuid4().hex
            worker = threading.Thread(target=self._turn, args=(session, message),
                                      name=f"jarvis-turn-{thread_id}", daemon=True)
            session.worker = worker
            turn_id = session.turn_id
            worker.start()
            return turn_id

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
            record = {**to_json(event), "project_id": thread.project_id, "turn_id": session.turn_id}
            if event.kind != EventKind.TEXT_DELTA:
                # WP1 log() accepts text only; preserve full structured events
                # through its serialized append primitive, in the same log.
                self.stores.threads._append(thread.id, "log.jsonl", record)
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

    def _turn(self, session, message):
        terminal = None
        failed = False
        try:
            if session.cancelled.is_set():
                return
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
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server_thread.join(max(0, deadline - time.monotonic()))
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


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise APIError(400, f"{name} must be a nonempty string")
    return value


def _handler(daemon):
    class Handler(BaseHTTPRequestHandler):
        # WP12 must add same-origin/Host checks before introducing a browser
        # surface. This API currently serves loopback clients only; no CORS.
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

        def _body(self):
            if self.headers.get("Transfer-Encoding"):
                raise APIError(400, "chunked bodies are not supported")
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 16 * 1024 * 1024:
                raise APIError(400, "invalid body length (limit 16 MiB)")
            data = self.rfile.read(length)
            if len(data) != length:
                raise APIError(400, "incomplete request body")
            value = json.loads(data) if data else {}
            return _object(value, value.keys() if isinstance(value, dict) else ())

        def _dispatch(self):
            try:
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
                elif isinstance(exc, (DaemonError, BriefRefused, worktrees.WorktreeError)):
                    status = 409
                elif isinstance(exc, (ValueError, TypeError, KeyError, TimeoutError)):
                    status = 400
                else:
                    status = 500
                    LOG.exception("HTTP %s %s failed", self.command, self.path)
                try:
                    self._json(status, {"error": str(exc) or type(exc).__name__})
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
                    return 200, [to_json(p) for p in safe_list(stores.projects)]
                if method == "POST":
                    body = _object(self._body(), ("name", "root", "profile", "routing", "extra_dirs",
                                                   "always_ask", "discord_channel_id"), ("name", "root"))
                    project = from_json(Project, {"id": "00000000", **body})
                    _validate(project, Project)
                    _text(project.name, "name")
                    if not Path(project.root).is_absolute():
                        raise APIError(400, "root must be absolute")
                    values = to_json(project)
                    values.pop("id")
                    with daemon._lock:
                        daemon._active()
                        return 201, to_json(stores.projects.create(**values))
            if len(parts) == 2 and parts[0] == "projects":
                with daemon._lock:
                    project = daemon.require(stores.projects, parts[1])
                    if method == "GET":
                        return 200, to_json(project)
                    if method == "PATCH":
                        daemon._active()
                        body = _object(self._body(), ("name", "root", "profile", "routing", "extra_dirs",
                                                      "always_ask", "discord_channel_id"))
                        project = from_json(Project, {**to_json(project), **body})
                        _validate(project, Project)
                        _text(project.name, "name")
                        if not Path(project.root).is_absolute():
                            raise APIError(400, "root must be absolute")
                        if project.inbox and ("name" in body or "root" in body):
                            raise DaemonError("inbox name and root cannot change")
                        stores.projects.save(project)
                        return 200, to_json(project)
            if parts in (["threads"], ["tasks"]):
                store = stores.threads if parts[0] == "threads" else stores.tasks
                if method == "GET":
                    _object(query, ("project",))
                    filters = {}
                    if "project" in query:
                        daemon.require(stores.projects, query["project"])
                        filters["project_id"] = query["project"]
                    return 200, [to_json(obj) for obj in safe_list(store, **filters)]
                if method == "POST" and parts[0] == "threads":
                    body = _object(self._body(), ("project_id", "role", "provider", "brief"),
                                   ("project_id", "role", "provider", "brief"))
                    return 201, to_json(daemon.open_thread(**body))
                if method == "POST":
                    body = _object(self._body(), ("project_id", "brief"), ("project_id", "brief"))
                    daemon.require(stores.projects, body["project_id"])
                    _text(body["brief"], "brief")
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
                if method == "POST" and action == "send":
                    body = _object(self._body(), ("text", "images"), ("text",))
                    _text(body["text"], "text")
                    message = UserMessage(**body)
                    _validate(message, UserMessage)
                    for img in message.images:
                        _object(img, ("b64", "mime"), ("b64", "mime"))
                        _text(img["b64"], "b64")
                        _text(img["mime"], "mime")
                    return 202, {"turn_id": daemon.send(thread_id, message)}
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
            if len(parts) in (2, 3) and parts[0] == "tasks":
                task = daemon.require(stores.tasks, parts[1])
                if len(parts) == 2 and method == "GET":
                    return 200, to_json(task)
                if len(parts) == 3 and parts[2] == "worktree":
                    return self._worktree(parts[1], query)
            raise APIError(404, "route not found")

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


def placeholder_permit_factory(thread_id, brief):
    from jarvis import permissions

    def deny(tool, args):
        LOG.warning("Permission denied for thread %s: WP5 not landed", thread_id)
        return False

    gated = permissions.gate(deny)

    def permit(tool, args, callback_brief):
        return Decision.ALLOW if gated(tool, args) else Decision.DENY
    return permit


def main() -> int:
    daemon = Daemon(Stores(), load_providers(), placeholder_permit_factory, config.DAEMON_PORT)
    done = threading.Event()
    previous = signal.signal(signal.SIGTERM, lambda *_: done.set())
    try:
        daemon.start()
        LOG.info("Jarvis v2 listening on 127.0.0.1:%s", daemon.port)
        done.wait()
    except KeyboardInterrupt:
        pass
    except DaemonError as exc:
        print(f"jarvis daemon2: {exc}")
        return 1
    finally:
        daemon.stop()
        signal.signal(signal.SIGTERM, previous)
    return 0
