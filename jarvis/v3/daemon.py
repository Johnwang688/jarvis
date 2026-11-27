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
import re
import threading
import time
from typing import Callable

from jarvis import config
from windows.v3voice_problems import PROBLEMS, UNKNOWN
from . import hud, owner_routes
from .approvals import ApprovalBroker
from .browser.desk import BrowserDesk
from .browser.service import BrowserService
from .chat import ChatService
from .executor import Executor, Worker as ExecutorWorker
from .jobs import Job, Scheduler, default_jobs, idle_seconds
from .logins import Store
from .voice import settings as voice_settings
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
                    phone_status=None, get_feed=None, voice_status=None,
                    voice_helper=None, voice_panel=None, browser_status=None) -> type[BaseHTTPRequestHandler]:
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
            "voice": {"brain": "claude", "state": "resting", "turns": 0} if voice_status is None else voice_status(),
            "browser": {"chrome": "closed", "edge": "closed"} if browser_status is None else browser_status(),
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
                                                  get_proposals=get_proposals, voice_helper=voice_helper,
                                                  voice_panel=voice_panel)
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
                 phone_port: int | None = None, phone_host=_CONFIGURED_PHONE_HOST, voice_link_port=None,
                 browser: BrowserService | None = None):
        self._requested_port = config.V3_API_PORT if port is None else port
        self._phone_host = config.remote_host() if phone_host is _CONFIGURED_PHONE_HOST else phone_host
        self._requested_phone_port = phone_port
        self._requested_voice_link_port = voice_link_port
        self._data_dir = Path(config.V3_DIR if data_dir is None else data_dir)
        self._hud_dir = None if hud_dir is None else Path(hud_dir)
        self._work = work
        self._proposals = proposals
        self._chats_root = self._data_dir / "chats"
        self._owns_chat = chat is None
        self._owns_browser = browser is None
        self._replace_browser = False
        self.browser = BrowserService() if browser is None else browser
        self._chat = self._new_chat() if chat is None else chat
        self._voice_settings_lock = threading.Lock()
        self._voice_epoch_offset = time.time() - time.monotonic()
        self._voice_settings = voice_settings.load(self._data_dir)
        self._voice_brain, self._voice_turns, self._voice_session = self._new_voice()
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
                                executor=self._sweep_executor, executor_activity=self._executor_activity,
                                announce=self._announce_work if self._voice_session is not None else None)
        browser_reap = Job("browser-reap", lambda: self.browser.reap_async(), every_s=60)
        self._scheduler = Scheduler((browser_reap, *jobs), state=self._data_dir / "jobs/state.json",
                                    idle=self._idle_seconds)
        self.address = ("127.0.0.1", self._requested_port)
        self._server: ThreadingHTTPServer | None = None
        self._phone_server = None
        self._voice_link = None
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
        from .chat import calendar_available
        from .feed import Feed
        from .policy import Policy
        from .proposals import ProposalStore
        from .vault import Vault
        from .work import Ledger, Registry

        def calendar_factory(*, approve):
            if not calendar_available():
                return None
            from .tools.calendar import CalendarTools

            return CalendarTools(approve=approve)

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
                vault = Vault(os.environ.get("JARVIS_VAULT") or self._data_dir / "vault")
                self._executor = (feed, Executor(ProposalStore(self._data_dir / "proposals"),
                                                 feed, work, Policy(), calendar_factory, vault,
                                                 stop=self._executor_stop))
            return self._executor

    def _executor_activity(self, since, until):
        return self._initiative()[1].activity_counts(since, until)

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
        desk = BrowserDesk(self.browser, store_factory=Store.system, feed_post=self._post_browser_feed)
        return ChatService(self._chats_root, approvals=ApprovalBroker(log=self._data_dir / "approvals.jsonl"),
                           work=WorkService(registry=Registry(root / "projects.json"),
                                            ledger=Ledger(root / "threads.jsonl")), browser_desk=desk)

    def _post_browser_feed(self, *, actor, **fields):
        return self._get_feed().post(fields, actor=actor)

    def _announce_work(self, notice):
        session = self._voice_session
        if session is not None:
            session.announce(notice)

    def _new_voice(self):
        from .voice.approving import VoiceApproving
        from .voice.claude_brain import ClaudeVoiceBrain
        from .voice.engines import EngineUnavailable, KokoroVoice, cuda_voice_available, pick_transcriber
        from .voice.gpu_voice import KokoroWorkerVoice
        from .voice.session import VoiceSession
        from .voice.turns import VoiceTurns

        brain = ClaudeVoiceBrain(self._data_dir, approvals=self._chat.approvals, work=self._chat.work,
                                 calendar=self._chat._calendar)
        discard = lambda *_args, **_fields: None
        turns = VoiceTurns(brain, on_event=discard)
        work = self._chat.work
        names = lambda: () if work is None else (project.name for project in work.projects())
        setting = config.V3_VOICE
        speed = self._voice_settings.speed
        fast = KokoroVoice(device="cpu", speed=speed) if setting in ("auto", "kokoro-gpu", "kokoro-cpu") else None
        best = None
        if setting in ("auto", "kokoro-gpu"):
            if cuda_voice_available():
                best = KokoroVoice(device="cuda", speed=speed)
            else:
                try:
                    best = KokoroWorkerVoice(speed=speed, data_dir=self._data_dir)
                except EngineUnavailable:
                    pass
        approving = VoiceApproving(self._chat.approvals, clock=time.monotonic)
        session = VoiceSession(turns, transcriber=pick_transcriber, voices=(fast, best), names=names,
                               approving=approving, follow_up_s=self._voice_settings.follow_up_s,
                               end_silence_ms=self._voice_settings.end_silence_ms)
        return brain, turns, session

    def _voice_status(self):
        # A status request may already be in flight as the daemon shuts down.
        brain, session, link = self._voice_brain, self._voice_session, self._voice_link
        status = {"brain": "claude", "state": "idle", "turns": 0} if brain is None else brain.status()
        status["brain_state"] = status.pop("state")
        status.update({"state": "resting", "problem": None} if session is None else session.status())
        status["link"] = (link.status() if link is not None else
                          {"listening": False, "connected": False, "reason": "not_started"})
        return status

    def _voice_panel(self, body):
        if body is not None:
            with self._voice_settings_lock:
                session = self._voice_session
                if session is None:
                    return 503, {"message": "Voice is unavailable."}
                try:
                    settings = self._voice_settings.patched(body)
                except ValueError as error:
                    return 400, {"message": str(error)}
                if body:
                    try:
                        voice_settings.save(self._data_dir, settings)
                    except OSError:
                        return 503, {"message": "Voice settings could not be saved."}
                    session.apply_settings(settings)
                    self._voice_settings = settings
                return 200, {"settings": settings.snapshot()}

        with self._voice_settings_lock:
            settings = self._voice_settings.snapshot()
            brain, turns, session, link = (self._voice_brain, self._voice_turns,
                                          self._voice_session, self._voice_link)
        if session is None or brain is None or turns is None:
            return 503, {"message": "Voice is unavailable."}
        link_status = {} if link is None else link.status(include_canceller=True)
        helper = {name: link_status.get(name, False) for name in ("connected", "listening")}
        version = link_status.get("helper_version")
        # HELLO version is peer-supplied. Accept a short release tag only.
        if version and len(version) <= 32 and re.fullmatch(r"(?:[a-zA-Z]+-)?[0-9]+(?:\.[0-9]+){0,3}", version):
            helper["version"] = version
        for name in ("canceller", "level"):
            if name in link_status:
                helper[name] = link_status[name]
        brain_status = brain.status()
        status = session.status()
        panel = {name: status[name] for name in ("state", "reason", "transcriber", "voice",
                                                 "detector", "gpu_warm", "counters") if name in status}
        problem = status.get("problem")
        panel["problem"] = None
        if problem is not None:
            _, line, hint = PROBLEMS.get(problem["code"], UNKNOWN)
            panel["problem"] = {"text": line, "hint": hint, "at": problem["at"]}
        rows = []
        for row in turns.trace()[:20]:
            rows.append({
                "heard_at": self._voice_epoch_offset + row["heard_at"],
                "text": row["text"][:300],
                "tools": [{"name": tool["name"], "ok": tool["outcome"] == "ok"} for tool in row["tools"]],
                "approval_asked": row["approval_asked"],
                "sentence_count": row["sentence_count"],
                **{name: row[name] for name in ("silence_ms", "decode_ms", "first_audio_ms", "first_say_s", "end_s")},
            })
        return 200, {**panel, "helper": helper, "settings": settings, "turns": rows,
                     "brain": {"name": brain_status["brain"], "state": brain_status["state"],
                               "turns": brain_status["turns"]}}

    def _new_voice_session(self, *, on_event, on_audio):
        session = self._voice_session
        session.attach(on_event, on_audio)
        return session

    def _start_voice_link(self):
        from .voice.link import VoiceLink

        port = self._requested_voice_link_port
        if port is None and self._requested_port == 0:
            port = 0
        link = self._voice_link = VoiceLink(self._new_voice_session, port=port)
        link.start()

    def _voice_helper(self, action):
        from .voice.installation import HelperUnavailable

        link = self._voice_link
        if link is None:
            return 503, {"message": "The voice link is unavailable."}
        if action == "stop":
            if link.request_quit():
                return 200, {"message": "Asked the Windows voice helper to quit."}
            return 200, {"message": "No Windows voice helper is connected."}
        try:
            started = link.launch_helper()
        except HelperUnavailable as error:
            return 409, {"message": str(error)}
        message = "Started the Windows voice helper." if started else "The Windows voice helper is already starting or connected."
        return 200, {"message": message}

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
            if self._replace_browser:
                self.browser = BrowserService()
                self._replace_browser = False
            if self._chat is None:
                self._chat = self._new_chat()
            if self._voice_brain is None:
                with self._voice_settings_lock:
                    self._voice_settings = voice_settings.load(self._data_dir)
                    self._voice_brain, self._voice_turns, self._voice_session = self._new_voice()
            handler = _status_handler(self._data_dir, self._hud_dir, self._scheduler, self._chat,
                                      work=self._work, proposals=self._proposals, phone_status=self._phone_status,
                                      get_feed=self._get_feed, voice_status=self._voice_status,
                                      voice_helper=self._voice_helper, voice_panel=self._voice_panel,
                                      browser_status=self.browser.status)
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
            self._start_voice_link()

    def stop(self) -> None:
        with self._lock:
            if self._voice_link is not None:
                try:
                    self._voice_link.close()
                except Exception:
                    LOG.warning("Could not close the voice link at stop.")
                self._voice_link = None
            with self._voice_settings_lock:
                components = (("voice session", self._voice_session), ("voice turns", self._voice_turns),
                              ("voice brain", self._voice_brain))
                self._voice_session = self._voice_turns = self._voice_brain = None
            for label, component in components:
                if component is not None:
                    try:
                        component.close()
                    except Exception:
                        LOG.warning("Could not close %s at stop.", label)
            try:
                self.browser.close()
            except Exception:
                LOG.warning("Could not close the browser service at stop.")
            self._replace_browser = self._owns_browser
            if self._server is None:
                if self._owns_chat and self._chat is not None:
                    self._chat.close()
                    self._chat.approvals.shutdown()
                    self._chat = None
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
