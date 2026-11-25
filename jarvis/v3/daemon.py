"""The loopback v3 daemon, its status endpoint, HUD and owner routes.

Stated limits (verifier r2, 2026-11-23): a request whose header block is
malformed is parsed leniently by http.server (the Host/Origin guards still
apply); a data dir reached through a symlinked *parent* is that directory and
gets 0700 (only a symlinked final component is refused). Run it as
`python -m jarvis agentd` (the unit's ExecStart, A4): that path skips the model
stack; the installed `jarvis` console script imports it (never calls it).
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import errno
from functools import cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Callable

from jarvis import config
from . import hud, owner_routes
from .approvals import ApprovalBroker
from .chat import ChatService
from .executor import Executor, Worker as ExecutorWorker
from .jobs import Scheduler, default_jobs, idle_seconds
from .work import WorkService


SERVICE_NAME = "jarvis-agentd"
_MAX_JSON_DEPTH = 128
_CONFIGURED_PHONE_HOST = object()
_EXECUTOR_STOP_GRACE_S = 3.0
LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PhoneServices:
    feed: Callable
    chat: ChatService | None
    data_dir: Path
    work: Callable
    proposals: Callable
    status: Callable


class _HeaderReader:
    """Track folded fields before the HTTP parser can strip a leading fold."""

    def __init__(self, stream):
        self.stream = stream
        self.folded = set()
        self._field = b""

    def readline(self, size=-1):
        line = self.stream.readline(size)
        if line.startswith((b" ", b"\t")):
            self.folded.add(self._field)
        else:
            self._field = line.partition(b":")[0].lower()
        return line


def _reject_json_constant(value):
    raise ValueError(value)


def _unique_json_keys(pairs):
    body = dict(pairs)
    if len(body) != len(pairs):
        raise ValueError("Duplicate JSON keys")
    return body


def _check_json_depth(value, depth=1):
    if isinstance(value, dict):
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return
    if depth > _MAX_JSON_DEPTH:
        raise ValueError("JSON nesting exceeds the limit")
    for child in children:
        _check_json_depth(child, depth + 1)


def _decode_route_body(data: bytes) -> dict:
    body = json.loads(data.decode("utf-8"), parse_constant=_reject_json_constant,
                      object_pairs_hook=_unique_json_keys)
    if not isinstance(body, dict):
        raise ValueError("expected a JSON object")
    _check_json_depth(body)
    return body


def _status_handler(data_dir: Path, hud_dir: Path | None = None,
                    scheduler: Scheduler | None = None,
                    chat: ChatService | None = None, *, work=None, proposals=None,
                    phone_status=None, get_feed=None) -> type[BaseHTTPRequestHandler]:
    started_at = datetime.now(timezone.utc).isoformat()
    started_tick = time.monotonic()
    pid = os.getpid()
    directory = str(data_dir)
    hud_root = config.HUD3_DIR if hud_dir is None else hud_dir

    @cache
    def default_feed():
        from .feed import Feed

        return Feed(data_dir / "feed", lock_timeout=owner_routes.FEED_LOCK_TIMEOUT)

    def default_work():
        from .work import Ledger, Registry

        root = data_dir / "work"
        return Registry(root / "projects.json"), Ledger(root / "threads.jsonl")

    get_work = default_work if work is None else work
    if get_feed is None:
        get_feed = default_feed

    def default_proposals():
        from .proposals import ProposalStore

        return ProposalStore(data_dir / "proposals")

    get_proposals = default_proposals if proposals is None else proposals

    def status():
        return {
            "service": SERVICE_NAME,
            "version": "v3",
            "pid": pid,
            "started_at": started_at,
            "uptime_s": time.monotonic() - started_tick,
            "data_dir": directory,
            "jobs": {} if scheduler is None else scheduler.status(),
            "phone": {"bound": False, "host_configured": False} if phone_status is None else phone_status(),
        }

    class Handler(BaseHTTPRequestHandler):
        timeout = 5
        phone_services = _PhoneServices(get_feed, chat, data_dir, get_work, get_proposals, status)

        def log_message(self, *args):
            pass

        def end_headers(self):
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", hud.CSP)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            super().end_headers()

        def send_response(self, code, message=None):
            # Even a malformed request whose version is unknown needs headers.
            if self.request_version == "HTTP/0.9":
                self.request_version = "HTTP/1.0"
            super().send_response(code, message)

        def send_error(self, code, message=None, explain=None):
            # A refusal can leave unread bytes on the connection.
            self.close_connection = True
            self._json(code, {"error": self.responses[code][0]})

        def _json(self, code, payload):
            self._reply(code, "application/json", json.dumps(payload).encode("utf-8"))

        def _reply(self, code, kind, body):
            try:
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                if self.close_connection:
                    self.send_header("Connection", "close")
                if code == 405:
                    methods = []
                    if ("GET", self._target) in owner_routes.ROUTES:
                        methods.extend(("GET", "HEAD"))
                    if ("POST", self._target) in owner_routes.ROUTES:
                        methods.append("POST")
                    self.send_header("Allow", ", ".join(methods) or "GET, HEAD")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def parse_request(self):
            reader = _HeaderReader(self.rfile)
            self.rfile = reader
            try:
                if not super().parse_request():
                    return False
            finally:
                self.rfile = reader.stream
            self.folded_headers = reader.folded
            # HTTP/0.9 has no headers. Some Python versions store a plain dict.
            if self.request_version == "HTTP/0.9":
                self.send_error(421)
                return False
            port = self.server.server_port
            hosts = self.headers.get_all("Host", [])
            if b"host" in self.folded_headers or len(hosts) != 1 or not config.own_host_header(hosts[0], port):
                self.send_error(421)
                return False
            if "cross-site" in self.headers.get_all("Sec-Fetch-Site", []):
                self.send_error(403)
                return False
            # http.server collapses leading slashes in self.path.
            self._target = self.requestline.split()[1]
            if self.command not in ("GET", "HEAD", "POST"):
                self.send_error(405)
                return False
            method = "GET" if self.command == "HEAD" else self.command
            self._route = owner_routes.ROUTES.get((method, self._target))
            if self._route is not None and self._route.owner:
                if not owner_routes.is_owner(self):
                    self.send_error(403)
                    return False
            else:
                # Keep the existing Origin guard for the public HTTP surface,
                # including the refusal of cross-origin POSTs to unlisted paths.
                origins = self.headers.get_all("Origin", [])
                allowed = (f"http://127.0.0.1:{port}", f"http://localhost:{port}")
                if b"origin" in self.folded_headers or (origins and (len(origins) != 1 or origins[0] not in allowed)):
                    self.send_error(403)
                    return False
                if self._route is None and method == "POST":
                    self.send_error(405)
                    return False
            return True

        def _run_route(self, body):
            try:
                status, payload = self._route.run(body, get_feed, chat, data_dir=data_dir, get_work=get_work,
                                                  get_proposals=get_proposals)
                if not isinstance(status, int) or not 200 <= status <= 599:
                    raise ValueError("invalid route status")
                if isinstance(payload, dict) and "error" in payload:
                    # Only send fixed public error text across the HTTP boundary.
                    message = self.responses.get(status, ("Request failed",))[0]
                    payload = {"error": message}
                encoded = json.dumps(payload, allow_nan=False).encode("utf-8")
            except Exception:
                self.send_error(500)
                return
            self._reply(status, "application/json", encoded)

        def _read_body(self, length):
            # read1 makes at most one socket read. A timeout on read(length)
            # alone would restart on each new fragment of a slow body.
            deadline = time.monotonic() + self.timeout
            body = bytearray()
            try:
                while len(body) < length:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    self.connection.settimeout(remaining)
                    chunk = self.rfile.read1(length - len(body))
                    if not chunk:
                        break
                    body.extend(chunk)
            finally:
                self.connection.settimeout(self.timeout)
            return body

        def do_POST(self):
            types = self.headers.get_all("Content-Type", [])
            if (len(types) != 1 or types[0].partition(";")[0] != "application/json"
                    or b"content-type" in self.folded_headers or "\r" in types[0] or "\n" in types[0]):
                self.send_error(415)
                return
            if "Transfer-Encoding" in self.headers:
                self.send_error(400)
                return
            lengths = self.headers.get_all("Content-Length", [])
            if not lengths:
                self.send_error(411)
                return
            if (b"content-length" in self.folded_headers or len(lengths) != 1
                    or not lengths[0].isascii() or not lengths[0].isdecimal()):
                self.send_error(400)
                return
            digits = lengths[0].lstrip("0") or "0"
            if len(digits) > len(str(owner_routes.MAX_BODY)):
                self.send_error(413)
                return
            length = int(digits)
            if length > owner_routes.MAX_BODY:
                self.send_error(413)
                return
            try:
                data = self._read_body(length)
            except TimeoutError:
                self.send_error(408)
                return
            except OSError:
                self.send_error(400)
                return
            try:
                if len(data) != length:
                    raise ValueError("incomplete body")
                body = _decode_route_body(data)
            except (ValueError, RecursionError):
                self.send_error(400)
                return
            self._run_route(body)

        def do_GET(self):
            path = self._target.partition("?")[0]
            if self._route is not None:
                self._run_route(None)
            elif self._target == "/v3/status":
                self._json(200, status())
            elif (asset := hud.static_file(hud_root, path)) is not None:
                self._reply(200, *asset)
            elif path == "/":
                self._json(503, {"error": hud.MISSING_HUD})
            else:
                self.send_error(404)

        do_HEAD = do_GET

    return Handler


def _prepare_data_dir(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except FileExistsError:
        # Check the existing entry below, including dangling symlinks.
        pass
    try:
        fd = os.open(path, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno in (errno.ENOTDIR, errno.ELOOP):
            raise RuntimeError(f"v3 data directory must be a directory without a final symlink: {path}") from exc
        raise
    try:
        # O_PATH works even with mode 000. The procfs path pins chmod to the
        # opened directory if the original path is replaced after open().
        os.chmod(f"/proc/self/fd/{fd}", 0o700)
    finally:
        os.close(fd)


class Daemon:
    def __init__(self, port: int | None = None, data_dir: Path | None = None,
                 hud_dir: Path | None = None, *, jobs=None, chat=None, work=None, proposals=None,
                 phone_port: int | None = None, phone_host=_CONFIGURED_PHONE_HOST):
        self._requested_port = config.V3_API_PORT if port is None else port
        self._phone_host = config.remote_host() if phone_host is _CONFIGURED_PHONE_HOST else phone_host
        self._requested_phone_port = phone_port
        self._data_dir = Path(config.V3_DIR if data_dir is None else data_dir)
        self._hud_dir = None if hud_dir is None else Path(hud_dir)
        self._work = work
        self._proposals = proposals
        self._chats_root = self._data_dir / "chats"
        self._owns_chat = chat is None
        self._chat = self._new_chat() if chat is None else chat
        self._executor = None
        self._executor_lock = threading.Lock()
        self._executor_stop = threading.Event()
        self._executor_stop.set()
        self._executor_worker = ExecutorWorker(self._sweep_executor, stop=self._executor_stop)
        self._executor_thread = None
        self._executor_condition = threading.Condition()
        self._executor_sweep = None
        if jobs is None:
            jobs = default_jobs(feed_root=self._data_dir / "feed", chats_root=self._chats_root,
                                index_path=os.environ.get("JARVIS_INDEX_DB") or self._data_dir / "index.sqlite",
                                executor=self._sweep_executor)
        self._scheduler = Scheduler(jobs, state=self._data_dir / "jobs/state.json",
                                    idle=self._idle_seconds)
        self.address = ("127.0.0.1", self._requested_port)
        self._server: ThreadingHTTPServer | None = None
        self._phone_server = None
        self._lock = threading.Lock()

    @property
    def port(self) -> int:
        return self.address[1]

    @property
    def phone_port(self) -> int | None:
        server = self._phone_server
        return None if server is None else server.server_port

    def _phone_status(self) -> dict:
        return {"bound": self.phone_port is not None, "host_configured": bool(self._phone_host)}

    def _start_phone(self, services) -> None:
        if not self._phone_host:
            return
        from jarvis.v2.remote_auth import RemoteAuth
        from . import phone

        port = self._requested_phone_port
        if port is None:
            port = 0 if self._requested_port == 0 else config.REMOTE_PORT
        auth = RemoteAuth(config.remote_dir(), self._phone_host)
        root = config.HUD3_DIR if self._hud_dir is None else self._hud_dir
        server = phone.bind(self._phone_host, port, auth=auth, services=services, hud_dir=root)
        if server is None:
            return
        try:
            threading.Thread(target=server.serve_forever, name="jarvis-phone", daemon=True).start()
        except RuntimeError:
            server.server_close()
            auth.server = None
            LOG.warning("Phone listener could not start.")
            return
        self._phone_server = server

    def _idle_seconds(self):
        chat_idle = self._chat.idle_seconds()
        return min(chat_idle, idle_seconds(chats_root=self._chats_root)) if chat_idle else 0.0

    def _initiative(self):
        from .feed import Feed
        from .policy import Policy
        from .proposals import ProposalStore
        from .work import Ledger, Registry

        with self._executor_lock:
            if self._executor is None:
                feed = Feed(self._data_dir / "feed", lock_timeout=owner_routes.FEED_LOCK_TIMEOUT,
                            execute=self._executor_worker.wake)
                if self._work is None:
                    root = self._data_dir / "work"
                    registry, ledger = Registry(root / "projects.json"), Ledger(root / "threads.jsonl")
                else:
                    registry, ledger = self._work()
                work = WorkService(registry=registry, ledger=ledger)
                self._executor = (feed, Executor(ProposalStore(self._data_dir / "proposals"),
                                                 feed, work, Policy(), stop=self._executor_stop))
            return self._executor

    def _get_feed(self):
        return self._initiative()[0]

    def _sweep_executor(self):
        finished = failed = False

        def run():
            nonlocal finished, failed
            try:
                # A fresh data directory needs no optional proposal modules.
                if (self._data_dir / "proposals" / "proposals.jsonl").exists():
                    self._initiative()[1].sweep()
            except Exception:
                failed = True
            finally:
                with self._executor_condition:
                    finished = True
                    self._executor_condition.notify_all()

        with self._executor_condition:
            previous = self._executor_sweep
            if self._executor_stop.is_set() or (previous is not None and previous.is_alive()):
                return
            # A slow action may outlast the shutdown grace. This thread owns
            # its durable result and can finish even after both triggers exit.
            active = threading.Thread(target=run, name="jarvis-v3-proposal-sweep", daemon=False)
            self._executor_sweep = active
            try:
                active.start()
            except RuntimeError:
                self._executor_sweep = None
                raise
            self._executor_condition.wait_for(lambda: self._executor_stop.is_set() or finished)
        if finished:
            active.join()
        if failed:
            raise RuntimeError("Proposal sweep could not finish.")

    def _stop_jobs(self):
        self._executor_stop.set()
        with self._executor_condition:
            active = self._executor_sweep
            self._executor_condition.notify_all()
        self._executor_worker.wake()
        self._scheduler.stop()
        if active is not None:
            active.join(_EXECUTOR_STOP_GRACE_S)
        if self._executor_thread is not None:
            self._executor_thread.join()
            self._executor_thread = None

    def _new_chat(self):
        # The chat and the work route read the same registry and ledger: this daemon's data folder.
        from .work import Ledger, Registry

        root = self._data_dir / "work"
        return ChatService(self._chats_root, approvals=ApprovalBroker(log=self._data_dir / "approvals.jsonl"),
                           work=WorkService(registry=Registry(root / "projects.json"),
                                            ledger=Ledger(root / "threads.jsonl")))

    def start(self) -> None:
        with self._lock:
            if self._server is not None:
                return
            with self._executor_condition:
                active = self._executor_sweep
                if active is not None and active.is_alive():
                    # A restart must never clear a stop that an older sweep still uses.
                    raise RuntimeError("The previous proposal sweep is still finishing.")
            _prepare_data_dir(self._data_dir)
            if self._chat is None:
                self._chat = self._new_chat()
            handler = _status_handler(self._data_dir, self._hud_dir, self._scheduler, self._chat,
                                      work=self._work, proposals=self._proposals, phone_status=self._phone_status,
                                      get_feed=self._get_feed)
            try:
                server = ThreadingHTTPServer(("127.0.0.1", self._requested_port), handler)
            except OSError as exc:
                raise RuntimeError(
                    f"Could not bind v3 daemon on 127.0.0.1:{self._requested_port}. "
                    f"Another daemon may hold the port. {exc}"
                ) from exc
            try:
                threading.Thread(target=server.serve_forever, name="jarvis-agentd", daemon=True).start()
            except RuntimeError:
                server.server_close()
                raise
            try:
                self._executor_stop.clear()
                worker = threading.Thread(target=self._executor_worker.run,
                                          name="jarvis-v3-executor", daemon=True)
                worker.start()
                self._executor_thread = worker
                self._scheduler.start()
            except RuntimeError:
                self._stop_jobs()
                server.shutdown()
                server.server_close()
                raise
            self.address = server.server_address
            self._server = server
            self._start_phone(handler.phone_services)

    def stop(self) -> None:
        with self._lock:
            if self._server is None:
                return
            self._stop_jobs()
            if self._phone_server is not None:
                phone = self._phone_server
                phone.shutdown()
                phone.server_close()
                phone.auth.server = None
                self._phone_server = None
                try:
                    phone.auth.final_save()
                except Exception:
                    # A failed save of phone state must not keep the window's
                    # listener, the chat or the jobs from stopping.
                    LOG.warning("Phone state could not be saved at stop.")
            self._chat.close()
            if self._owns_chat:
                self._chat.approvals.shutdown()
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            if self._owns_chat:
                self._chat = None
