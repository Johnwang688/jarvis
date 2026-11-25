"""The loopback v3 daemon, its status endpoint, HUD and owner routes.

Stated limits (verifier r2, 2026-11-23): a request whose header block is
malformed is parsed leniently by http.server (the Host/Origin guards still
apply); a data dir reached through a symlinked *parent* is that directory and
gets 0700 (only a symlinked final component is refused). Run it as
`python -m jarvis agentd` (the unit's ExecStart, A4): that path skips the model
stack; the installed `jarvis` console script imports it (never calls it).
"""

from datetime import datetime, timezone
import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time

from jarvis import config
from . import hud, owner_routes


SERVICE_NAME = "jarvis-agentd"
_MAX_JSON_DEPTH = 128


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


def _status_handler(data_dir: Path, hud_dir: Path | None = None) -> type[BaseHTTPRequestHandler]:
    started_at = datetime.now(timezone.utc).isoformat()
    started_tick = time.monotonic()
    pid = os.getpid()
    directory = str(data_dir)
    hud_root = config.HUD3_DIR if hud_dir is None else hud_dir

    class Handler(BaseHTTPRequestHandler):
        timeout = 5

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
                status, payload = self._route.handler(body)
                if not isinstance(status, int) or not 200 <= status <= 599:
                    raise ValueError("invalid route status")
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
                body = json.loads(data.decode("utf-8"), parse_constant=_reject_json_constant)
                if not isinstance(body, dict):
                    raise ValueError("expected a JSON object")
                _check_json_depth(body)
            except (ValueError, RecursionError):
                self.send_error(400)
                return
            self._run_route(body)

        def do_GET(self):
            path = self._target.partition("?")[0]
            if self._route is not None:
                self._run_route(None)
            elif self._target == "/v3/status":
                self._json(200, {
                    "service": SERVICE_NAME,
                    "version": "v3",
                    "pid": pid,
                    "started_at": started_at,
                    "uptime_s": time.monotonic() - started_tick,
                    "data_dir": directory,
                })
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
                 hud_dir: Path | None = None):
        self._requested_port = config.V3_API_PORT if port is None else port
        self._data_dir = config.V3_DIR if data_dir is None else Path(data_dir)
        self._hud_dir = None if hud_dir is None else Path(hud_dir)
        self.address = ("127.0.0.1", self._requested_port)
        self._server: ThreadingHTTPServer | None = None
        self._lock = threading.Lock()

    @property
    def port(self) -> int:
        return self.address[1]

    def start(self) -> None:
        with self._lock:
            if self._server is not None:
                return
            _prepare_data_dir(self._data_dir)
            try:
                server = ThreadingHTTPServer(
                    ("127.0.0.1", self._requested_port), _status_handler(self._data_dir, self._hud_dir)
                )
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
            self.address = server.server_address
            self._server = server

    def stop(self) -> None:
        with self._lock:
            if self._server is None:
                return
            self._server.shutdown()
            self._server.server_close()
            self._server = None
