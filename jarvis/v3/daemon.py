"""The loopback v3 daemon and its status endpoint."""

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time

from jarvis import config


def _status_handler(data_dir: Path) -> type[BaseHTTPRequestHandler]:
    started_at = datetime.now(timezone.utc).isoformat()
    started_tick = time.monotonic()
    pid = os.getpid()
    directory = str(data_dir)

    class Handler(BaseHTTPRequestHandler):
        timeout = 5

        def log_message(self, *args):
            pass

        def end_headers(self):
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            super().end_headers()

        def send_response(self, code, message=None):
            # Even a malformed request whose version is unknown needs headers.
            if self.request_version == "HTTP/0.9":
                self.request_version = "HTTP/1.0"
            super().send_response(code, message)

        def send_error(self, code, message=None, explain=None):
            self._json(code, {"error": self.responses[code][0]})

        def _json(self, code, payload):
            body = json.dumps(payload).encode("utf-8")
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                if code == 405:
                    self.send_header("Allow", "GET, HEAD")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def parse_request(self):
            if not super().parse_request():
                return False
            port = self.server.server_port
            hosts = self.headers.get_all("Host", [])
            if len(hosts) != 1 or not config.own_host_header(hosts[0], port):
                self.send_error(421)
                return False
            origins = self.headers.get_all("Origin", [])
            allowed_origins = (f"http://127.0.0.1:{port}", f"http://localhost:{port}")
            if origins and (len(origins) != 1 or origins[0] not in allowed_origins):
                self.send_error(403)
                return False
            if self.command not in ("GET", "HEAD"):
                self.send_error(405)
                return False
            return True

        def do_GET(self):
            # http.server collapses leading slashes in self.path.
            if self.requestline.split()[1] != "/v3/status":
                self.send_error(404)
                return
            self._json(200, {
                "version": "v3",
                "pid": pid,
                "started_at": started_at,
                "uptime_s": time.monotonic() - started_tick,
                "data_dir": directory,
            })

        do_HEAD = do_GET

    return Handler


class Daemon:
    def __init__(self, port: int | None = None, data_dir: Path | None = None):
        self._requested_port = config.V3_API_PORT if port is None else port
        self._data_dir = config.V3_DIR if data_dir is None else Path(data_dir)
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
            self._data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._data_dir.chmod(0o700)
            try:
                server = ThreadingHTTPServer(
                    ("127.0.0.1", self._requested_port), _status_handler(self._data_dir)
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
