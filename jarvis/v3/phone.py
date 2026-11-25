"""The loopback listener for the phone, with its own URL space."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import import_module
import json
import logging
from pathlib import Path
import re
import time

from . import hud, phone_auth
from .daemon import _HeaderReader


LOG = logging.getLogger(__name__)
BODY_CAP = 64 * 1024
DRAIN_CAP = 1024 * 1024
BODY_DEADLINE_S = 10
TRAILER_CAP = 8 * 1024
NO_STORE = "no-store"
IMMUTABLE = "private, max-age=31536000, immutable"
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; manifest-src 'self'; base-uri 'none'; "
    "form-action 'none'; object-src 'none'; frame-ancestors 'none'"
)
SECURITY_HEADERS = (
    ("Content-Security-Policy", CSP),
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
)
ERRORS = {
    400: "bad request", 401: "unauthorized", 403: "cross-origin request refused",
    404: "not found", 408: "request timeout", 413: "request body too large",
    500: "internal error",
}

_PAGE = "phone.html"
_BUILD_MANIFEST = "build-manifest.json"
_MANIFEST_CAP = 1024 * 1024
_ASSET = re.compile(r"assets/[A-Za-z0-9_][A-Za-z0-9._-]{0,199}")
_ICONS = frozenset((
    "orb-16.png", "orb-32.png", "orb-48.png", "orb-192.png", "orb-512.png",
    "orb-512-maskable.png",
))
_MANIFEST = {
    "id": "/m/", "start_url": "/m/", "scope": "/m/", "display": "standalone",
    "name": "J.A.R.V.I.S.", "short_name": "Jarvis",
    "background_color": "#0a1116", "theme_color": "#0a1116",
    "icons": [
        {"src": f"/m/icons/{name}", "sizes": size, "type": "image/png", "purpose": purpose}
        for name, size, purpose in (
            ("orb-192.png", "192x192", "any"),
            ("orb-512.png", "512x512", "any"),
            ("orb-512-maskable.png", "512x512", "maskable"),
        )
    ],
}
_NOT_BUILT = (b'<!doctype html><meta charset="utf-8"><title>Jarvis</title>'
              b'<p>The phone app is not installed yet.</p>\n')
_HTML = "text/html; charset=utf-8"


def _phone_files(built) -> frozenset[str]:
    """Follow the phone entry's Vite imports and collect its output files."""
    raw = hud.hud_install.read(built, _BUILD_MANIFEST)
    if raw is None or len(raw) > _MANIFEST_CAP or _PAGE not in built.files:
        return frozenset()
    try:
        manifest = json.loads(raw)
    except (ValueError, RecursionError):
        return frozenset()
    if not isinstance(manifest, dict):
        return frozenset()
    pending, seen, assets = [_PAGE], set(), set()
    while pending:
        key = pending.pop()
        if key in seen:
            continue
        seen.add(key)
        entry = manifest.get(key)
        if not isinstance(entry, dict):
            continue
        names = [entry.get("file")]
        for field in ("css", "assets"):
            if isinstance(entry.get(field), list):
                names.extend(entry[field])
        assets.update(name for name in names if isinstance(name, str) and _ASSET.fullmatch(name))
        for field in ("imports", "dynamicImports"):
            if isinstance(entry.get(field), list):
                pending.extend(key for key in entry[field] if isinstance(key, str))
    return frozenset(assets)


def _phone_page(root: Path) -> bytes | None:
    if hud.root_refusal(root) is not None:
        return None
    built = hud.hud_install.newest(root)
    if built is not None and _phone_files(built):
        return hud.hud_install.read(built, _PAGE)
    return None


def _phone_asset(root: Path, name: str) -> bytes | None:
    if hud.root_refusal(root) is not None:
        return None
    # A phone page loaded before an install can still request its own assets.
    for build_id in hud.hud_install.builds(root):
        built = hud.hud_install.load(root, build_id)
        if built is not None and name in _phone_files(built):
            if (data := hud.hud_install.read(built, name)) is not None:
                return data
    return None


class _BodyError(Exception):
    def __init__(self, status: int):
        super().__init__(status)
        self.status = status


class _Wire:
    """Read the body within a single deadline and a wire-byte budget."""

    def __init__(self, handler):
        self.stream = handler.rfile
        self.connection = handler.connection
        self.deadline = time.monotonic() + BODY_DEADLINE_S
        self.budget = DRAIN_CAP

    def _wait(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError
        self.connection.settimeout(left)

    def read(self, length: int, *, keep: bool = True) -> bytes:
        data = bytearray()
        oversized = length > self.budget
        remaining = min(length, self.budget)
        while remaining:
            self._wait()
            chunk = self.stream.read1(min(remaining, BODY_CAP))
            if not chunk:
                raise _BodyError(413 if oversized else 400)
            remaining -= len(chunk)
            self.budget -= len(chunk)
            if keep:
                data.extend(chunk)
        if oversized:
            raise _BodyError(413)
        return bytes(data)

    def line(self, limit: int) -> bytes:
        data = bytearray()
        while len(data) < limit:
            if not self.budget:
                raise _BodyError(413)
            self._wait()
            queued = self.stream.peek(1)[:min(limit - len(data), self.budget)]
            if not queued:
                break
            end = queued.find(b"\n") + 1
            chunk = self.stream.read(end or len(queued))
            self.budget -= len(chunk)
            data.extend(chunk)
            if end:
                return bytes(data)
        raise _BodyError(400)


def _read_chunked(wire: _Wire) -> bytes:
    data = bytearray()
    total = 0
    while True:
        line = wire.line(1026)
        size_text = line.split(b";", 1)[0].strip()
        if not re.fullmatch(rb"[0-9A-Fa-f]{1,8}", size_text):
            raise _BodyError(400)
        size = int(size_text, 16)
        if not size:
            break
        total += size
        data.extend(wire.read(size, keep=total <= BODY_CAP))
        if wire.line(3) not in (b"\r\n", b"\n"):
            raise _BodyError(400)
    trailers_left = TRAILER_CAP
    while (trailer := wire.line(trailers_left + 2)) not in (b"\r\n", b"\n"):
        trailers_left -= len(trailer)
        if trailers_left < 0:
            raise _BodyError(400)
    if total > BODY_CAP:
        raise _BodyError(413)
    return bytes(data)


def _read_body(handler, folded: set) -> bytes:
    if not hasattr(handler.headers, "get_all"):
        return b""
    lengths = handler.headers.get_all("Content-Length", [])
    encodings = handler.headers.get_all("Transfer-Encoding", [])
    if folded & {b"content-length", b"transfer-encoding"}:
        raise _BodyError(400)
    if encodings:
        if lengths or [value.strip().lower() for value in encodings] != ["chunked"]:
            raise _BodyError(400)
        return _read_chunked(_Wire(handler))
    if not lengths:
        return b""
    if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,12}", lengths[0].strip()):
        raise _BodyError(400)
    length = int(lengths[0])
    wire = _Wire(handler)
    if length > BODY_CAP:
        try:
            wire.read(length, keep=False)
        except _BodyError:
            pass
        raise _BodyError(413)
    return wire.read(length)


class PhoneHandler(BaseHTTPRequestHandler):
    timeout = BODY_DEADLINE_S
    server_version = "jarvis"
    sys_version = ""

    @property
    def remote_auth(self):
        return self.server.auth

    def log_message(self, *args):
        pass

    def end_headers(self):
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        super().end_headers()

    def send_error(self, code, message=None, explain=None):
        self.close_connection = True
        self._error(code)

    def _error(self, code, word=None):
        self._json(code, {"error": word or ERRORS.get(code, self.responses[code][0].lower())})

    def _json(self, code, payload, extra=(), *, kind="application/json"):
        self._reply(code, kind, json.dumps(payload, allow_nan=False).encode(), extra=extra)

    def _reply(self, code, kind, data, *, extra=(), cache=NO_STORE):
        # Even an unparseable request gets a framed HTTP response.
        if self.request_version == "HTTP/0.9":
            self.request_version = "HTTP/1.0"
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", cache)
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def parse_request(self):
        reader = _HeaderReader(self.rfile)
        self.rfile = reader
        try:
            parsed = super().parse_request()
        finally:
            self.rfile = reader.stream
        if parsed:
            self.close_connection = True
            self._dispatch(reader.folded)
        # Dispatch all methods here, before http.server's method lookup.
        return False

    def _dispatch(self, folded):
        try:
            try:
                body, body_error = _read_body(self, folded), None
            except _BodyError as exc:
                body_error = exc.status
            except TimeoutError:
                body_error = 408
            except OSError:
                body_error = 400
            finally:
                self.connection.settimeout(self.timeout)
            host = self.server.host
            if (b"host" in folded or not hasattr(self.headers, "get_all")
                    or self.headers.get_all("Host") != [host]):
                return self._error(400, "untrusted host")
            if self.command not in ("GET", "HEAD") and (
                b"origin" in folded or self.headers.get_all("Origin") != ["https://" + host]
            ):
                return self._error(403)
            if body_error is not None or self.headers.defects:
                return self._error(body_error or 400)
            target = self.requestline.split()[1]
            if target == "/" and self.command == "GET":
                return self._reply(302, "text/plain", b"", extra=(("Location", "/m/"),))
            path = target.split("?", 1)[0].split("#", 1)[0]
            if not path.startswith("/m/"):
                return self._error(404)
            if path == "/m/api" or path.startswith("/m/api/"):
                return self._api(path, body)
            if self.command in ("GET", "HEAD"):
                return self._static(path)
            return self._error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            LOG.warning("Phone request failed (%s)", type(exc).__name__)
            try:
                self._error(500)
            except OSError:
                pass

    def _api(self, path, body):
        # Keep the bounded wire body intact for both route tables. Each
        # route checks its session and decodes its own request shape.
        answer = phone_auth.auth_route(self, path, body)
        if answer is None:
            if phone_auth.session_for(self) is None:
                return self._error(401)
            try:
                routes = import_module("jarvis.v3.phone_routes")
            except ModuleNotFoundError as exc:
                if exc.name != "jarvis.v3.phone_routes":
                    raise
            else:
                answer = routes.data_route(self, self.command, path, body)
        if answer is None:
            return self._error(404)
        return self._json(*answer)

    def _static(self, path):
        root = self.server.hud_dir
        if path in ("/m/", "/m/pair"):
            page = _phone_page(root)
            return self._reply(503, _HTML, _NOT_BUILT) if page is None else self._reply(200, _HTML, page)
        if path == "/m/manifest.webmanifest":
            return self._json(200, _MANIFEST, kind="application/manifest+json")
        name = path.removeprefix("/m/")
        if _ASSET.fullmatch(name):
            data = _phone_asset(root, name)
            if data is not None:
                mime = hud._CONTENT_TYPES.get(Path(name).suffix, "application/octet-stream")
                return self._reply(200, mime, data, cache=IMMUTABLE)
        elif name.startswith("icons/") and name.removeprefix("icons/") in _ICONS:
            asset = hud.static_file(root, "/" + name)
            if asset is not None:
                return self._reply(200, *asset)
        return self._error(404)


class PhoneServer(ThreadingHTTPServer):
    remote_only = True

    def __init__(self, host, port, *, auth, services, hud_dir):
        self.host = host
        self.auth = auth
        self.services = services
        self.hud_dir = Path(hud_dir)
        super().__init__(("127.0.0.1", port), PhoneHandler)


def bind(host: str | None, port: int, *, auth, services, hud_dir) -> PhoneServer | None:
    """Bind the phone listener. The daemon starts serving it after the window."""
    if not host:
        return None
    try:
        server = PhoneServer(host, port, auth=auth, services=services, hud_dir=hud_dir)
    except OSError:
        LOG.warning("Phone listener could not bind its port.")
        return None
    if auth is not None:
        auth.server = server
    return server
