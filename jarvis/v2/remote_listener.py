"""The phone's listener (phone surface design PH-1, work package P-1).

v2's fourth listener: `config.REMOTE_PORT` (8408), bound to 127.0.0.1 only,
and reached from the owner's tailnet through `tailscale serve` on Windows,
which terminates HTTPS at `https://<machine>.<tailnet>.ts.net` and proxies to
`127.0.0.1:8408` (PH-2). It binds only when `config.remote_host()` names that
host; with no name configured there is no fourth port at all, which is the
default. A port that cannot be bound is logged and skipped: the daemon never
fails for it (`bind`).

**Its own route table, never a filter on the HUD's.** The handler here is a
separate class from the daemon's (`daemon._handler`); it never reaches
`hud_api.route`, `inbox.route`, `bridge.route`, the preview routes or any
other daemon route, so the HUD, the API, `/approvals`, `/inbox`, the
workshop and the preview pages are absent from it by construction, not by
an exclusion list that a new route could slip past. What it serves, and
nothing else (`ROUTES`, `ASSETS`, `ICONS`, `API_PREFIX`):

  - `GET /m/`: the mobile shell, `mobile.html` of the HUD build (PH-9);
  - `GET /m/pair`: the same shell, where a pairing QR code lands
    (`/m/pair#<code>`; the code is in the fragment, never sent here);
  - `GET /m/assets/<file>`: only the files Vite's build manifest lists for
    the `mobile.html` entry and what it imports (`mobile_files`), never
    the rest of the HUD's bundle;
  - `GET /m/icons/<orb icon>`: the orb icons the manifest names;
  - `GET /m/manifest.webmanifest`: the installed app's manifest, scope `/m/`;
  - `/m/api/...`: `remote_auth.api_route` (P-2). Four routes need no
    session (`POST /m/api/pair/options`, `pair/verify`, `login/options`,
    `login/verify`); `POST /m/api/logout` and `GET /m/api/me` need one;
    anything else is 401 without a session and 404 with one. With no
    `remote_auth` attached, every `/m/api` request is 401.

Everything else is 404. HEAD answers like GET without the body.

**Every request body is read before the answer** (`_read_body`), up to
`BODY_CAP` (64 KiB; above it 413, the excess drained up to `DRAIN_CAP` first,
chunked bodies decoded the same way). An answer sent over an unread body
closes a socket with data still in it, which the kernel answers with a
reset, and behind `tailscale serve`'s Go proxy that turns a 401 or a 403
into a 502 (review of PR #304).

**Posture**, on every response including a malformed request's:

  - the `Host` must be exactly the configured name, else 400 (the same
    strictness as `hud_api.check_origin`, for one name and no port);
  - any method but GET and HEAD must carry `Origin: https://<name>`
    exactly, else 403 (design §4, item 4);
  - `REMOTE_POLICY`'s Content-Security-Policy with `frame-ancestors 'none'`,
    `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
    `Referrer-Policy: no-referrer`, `Cross-Origin-Opener-Policy` and
    `Cross-Origin-Resource-Policy: same-origin`, and `Cache-Control:
    no-store` except on the content-hashed files under `/m/assets/`.

**Never the owner.** `projects.is_owner` is false for every request here:
it requires the HUD listener's port, and this server is also marked
`remote_only`, which `is_owner` refuses on its own (PH-1). The owner test
for this listener is P-2's passkey and session, `remote_auth.is_remote_owner`
(design §4), and nothing else.

SELF_PROTECTED: this module decides what the tailnet can reach on this
machine. Tests: tests/v2/remote_listener_check.py.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
import stat

from jarvis import config
from . import remote_auth

LOG = logging.getLogger(__name__)

PREFIX = "/m/"
API_PREFIX = "/m/api/"
ASSETS_PREFIX = "/m/assets/"
ICONS_PREFIX = "/m/icons/"
SHELL = "mobile.html"
# Vite's build manifest (`build.manifest` in hud/vite.config.ts), at the
# build's root under a name `hud_install` accepts and installs with the rest.
BUILD_MANIFEST = "build-manifest.json"

# The orb icons, the HUD's own files (`hud/public/icons`); the only icons served.
ICONS = ("orb-16.png", "orb-32.png", "orb-48.png", "orb-192.png", "orb-512.png",
         "orb-512-maskable.png")

MANIFEST = {
    "id": "/m/",
    "start_url": "/m/",
    "scope": "/m/",
    "display": "standalone",
    "name": "J.A.R.V.I.S.",
    "short_name": "Jarvis",
    "background_color": "#141413",
    "theme_color": "#141413",
    "icons": [
        {"src": "/m/icons/orb-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/m/icons/orb-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/m/icons/orb-512-maskable.png", "sizes": "512x512", "type": "image/png",
         "purpose": "maskable"},
    ],
}

# The phone's Content-Security-Policy: the HUD's posture (`hud_api.HUD_POLICY`)
# narrowed to what the shell uses. Only the bundle's own script runs (no
# inline script, no `eval`, no WebAssembly); styles come only from the
# bundle's stylesheet (the HUD's `'unsafe-inline'` is for xterm and Monaco,
# which the phone does not load); nothing is framed, framed by, posted to or
# based anywhere. Later packages widen it here, one directive at a time.
REMOTE_POLICY = "; ".join((
    "default-src 'none'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self'",
    "connect-src 'self'",
    "manifest-src 'self'",
    "base-uri 'none'",
    "form-action 'none'",
    "object-src 'none'",
    "frame-ancestors 'none'",
))
SECURITY_HEADERS = (
    ("Content-Security-Policy", REMOTE_POLICY),
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
)
NO_STORE = "no-store"
# `/m/assets/` names carry Vite's content hash: a changed file is a new name.
IMMUTABLE = "private, max-age=31536000, immutable"

_FILE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,199}")
# A script or stylesheet tag of the built page naming one of its own files.
_PAGE_REF = re.compile(r'\s(?:src|href)="/m/(assets/[A-Za-z0-9_][A-Za-z0-9._-]{0,199})"')
_MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml",
         ".woff2": "font/woff2", ".woff": "font/woff", ".json": "application/json"}
_FILE_CAP = 16 * 1024 * 1024
# A request body: at most this much is accepted, and up to DRAIN_CAP of a
# larger one is read and dropped before the 413, so the proxy sees an answer.
BODY_CAP = 64 * 1024
DRAIN_CAP = 1024 * 1024
_MANIFEST_CAP = 1024 * 1024

_NOT_BUILT = (b"<!doctype html><meta charset=\"utf-8\"><title>J.A.R.V.I.S.</title>"
              b"<p>The phone app is not installed on this machine yet.</p>\n")


# -- what the build holds ------------------------------------------------------------


def _dev_read(rel: str) -> bytes | None:
    """`hud/dist` as it is, under JARVIS_HUD_DEV=1 (the headless suites): no
    link followed, nothing outside it. The same rule as `hud_api.hud_files`."""
    root = Path(config.HUD_DIST)
    path = root / rel
    try:
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            return None
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _FILE_CAP:
            return None
        return path.read_bytes()
    except OSError:
        return None


def _readers():
    """Each build to read from, newest first, as a `read(rel)` function: the
    working tree's `hud/dist` under JARVIS_HUD_DEV=1, else every installed
    build (`hud_install`), each file verified against its build's manifest
    on every read."""
    if config.HUD_DEV:
        yield _dev_read
        return
    from . import hud_install
    root = config.HUD_DIST
    try:
        if hud_install.root_refusal(root):
            return
        names = hud_install.builds(root)
    except Exception:     # noqa: BLE001 — a bad install is a 404, never a 500
        return
    for name in names:
        built = hud_install.load(root, name)
        if built is not None:
            yield lambda rel, built=built: hud_install.read(built, rel)


def _served_reader():
    """The build `GET /m/` reads its page from: the one the HUD serves
    (`hud_install.state`, the newest intact build), or None."""
    if config.HUD_DEV:
        return _dev_read
    from . import hud_install
    built, _state = hud_install.state(config.HUD_DIST)
    if built is None:
        return None
    return lambda rel: hud_install.read(built, rel)


def mobile_files(read) -> frozenset[str]:
    """The files of one build that belong to the phone: those the built
    `mobile.html` itself names under `/m/assets/`, and from Vite's build
    manifest the `mobile.html` entry and every chunk the page names, with
    their CSS and assets and everything they import, statically or
    dynamically, transitively. (Vite lists a script the page loads, such as
    its module-preload polyfill, under no entry when the entry's own chunk is
    empty, so the page's own tags are a root too.) Only `assets/<name>` paths
    with a plain file name count. A missing page or a missing or malformed
    manifest is the empty set: nothing is served, rather than guessing."""
    try:
        page = read(SHELL)
        raw = read(BUILD_MANIFEST)
        if page is None or raw is None or len(raw) > _MANIFEST_CAP:
            return frozenset()
        manifest = json.loads(raw)
        named = {m.group(1) for m in _PAGE_REF.finditer(page.decode("utf-8", "replace"))}
    except Exception:     # noqa: BLE001
        return frozenset()
    if not isinstance(manifest, dict) or not isinstance(manifest.get(SHELL), dict):
        return frozenset()
    out: set[str] = set(named)
    seen: set[str] = set()
    todo = [SHELL] + [key for key, chunk in manifest.items()
                      if isinstance(chunk, dict) and chunk.get("file") in named]
    while todo:
        key = todo.pop()
        if key in seen or len(seen) > 2000:
            continue
        seen.add(key)
        chunk = manifest.get(key)
        if not isinstance(chunk, dict):
            continue
        files = [chunk.get("file")]
        for field in ("css", "assets"):
            listed = chunk.get(field)
            if isinstance(listed, list):
                files.extend(listed)
        for name in files:
            if (isinstance(name, str) and name.startswith("assets/")
                    and _FILE.fullmatch(name[len("assets/"):])):
                out.add(name)
        for field in ("imports", "dynamicImports"):
            listed = chunk.get(field)
            if isinstance(listed, list):
                todo.extend(k for k in listed if isinstance(k, str))
    return frozenset(out)


def _asset(rel: str) -> bytes | None:
    """`assets/<name>` from the newest build whose phone files list it (so a
    page loaded before an install still finds its own files)."""
    for read in _readers():
        if rel in mobile_files(read):
            data = read(rel)
            if data is not None:
                return data
    return None


def _icon(name: str) -> bytes | None:
    for read in _readers():
        data = read("icons/" + name)
        if data is not None:
            return data
    return None


# -- the route table -----------------------------------------------------------------


def _shell(handler):
    read = _served_reader()
    data = read(SHELL) if read is not None else None
    if data is None:
        return handler._send(503, _NOT_BUILT, _MIME[".html"])
    return handler._send(200, data, _MIME[".html"])


def _manifest(handler):
    data = json.dumps(MANIFEST, indent=1).encode()
    return handler._send(200, data, "application/manifest+json")


# Exact paths, GET and HEAD only. Everything else under `/m/` is one of the
# prefixes below or a 404.
ROUTES = {
    "/m/": _shell,
    "/m/pair": _shell,
    "/m/manifest.webmanifest": _manifest,
}


def _route(handler, path: str):
    if path == API_PREFIX[:-1] or path.startswith(API_PREFIX):
        status, payload, extra = remote_auth.api_route(handler, path, handler._body_bytes)
        return handler._send(status, json.dumps(payload).encode(), "application/json", extra=extra)
    if handler.command not in ("GET", "HEAD"):
        return handler._error(404, "not found")
    exact = ROUTES.get(path)
    if exact is not None:
        return exact(handler)
    if path.startswith(ASSETS_PREFIX):
        name = path[len(ASSETS_PREFIX):]
        if _FILE.fullmatch(name):
            data = _asset("assets/" + name)
            if data is not None:
                return handler._send(200, data, _mime(name), cache=IMMUTABLE)
    elif path.startswith(ICONS_PREFIX):
        name = path[len(ICONS_PREFIX):]
        if name in ICONS:
            data = _icon(name)
            if data is not None:
                return handler._send(200, data, "image/png")
    return handler._error(404, "not found")


def _mime(name: str) -> str:
    return _MIME.get(Path(name).suffix.lower(), "application/octet-stream")


# -- the listener --------------------------------------------------------------------


def _header(headers, name: str) -> list[str] | None:
    """Every value of the header `name`, or None when the request carries no
    parsed headers at all: an HTTP/0.9 request line (`GET /m/`) leaves
    http.server's `headers` a plain dict, with no `get_all` (review of PR
    #304). None is never the trusted Host or Origin, so it is refused."""
    get_all = getattr(headers, "get_all", None)
    if not callable(get_all):
        return None
    return get_all(name)


class _BodyError(Exception):
    def __init__(self, status: int):
        super().__init__(status)
        self.status = status


def _read_chunked(rfile, cap: int, drain: int) -> bytes:
    """A chunked body, decoded: at most `cap` bytes kept; past it, read on
    and dropped up to `drain`, then a 413."""
    kept, total = [], 0
    while True:
        line = rfile.readline(1026)
        if not line.endswith(b"\n"):
            raise _BodyError(400)
        size_text = line.split(b";", 1)[0].strip()
        if not size_text or len(size_text) > 8 or any(c not in b"0123456789abcdefABCDEF" for c in size_text):
            raise _BodyError(400)
        size = int(size_text, 16)
        if size == 0:
            while True:                       # trailers, up to the blank line
                trailer = rfile.readline(8194)
                if trailer in (b"\r\n", b"\n"):
                    break
                if not trailer.endswith(b"\n"):
                    raise _BodyError(400)
            break
        if total + size > drain:
            raise _BodyError(413)
        data = rfile.read(size)
        if len(data) != size or rfile.readline(3) not in (b"\r\n", b"\n"):
            raise _BodyError(400)
        total += size
        if total <= cap:
            kept.append(data)
    if total > cap:
        raise _BodyError(413)
    return b"".join(kept)


def _read_body(handler) -> bytes:
    """The request's body, read whole before any answer (module note): at
    most BODY_CAP bytes, else 413 once up to DRAIN_CAP of it is read. A
    malformed length is 400. Neither length nor chunked: no body."""
    headers = handler.headers
    encodings = _header(headers, "Transfer-Encoding") or []
    lengths = _header(headers, "Content-Length") or []
    if encodings:
        if lengths or [e.strip().lower() for e in encodings] != ["chunked"]:
            raise _BodyError(400)
        return _read_chunked(handler.rfile, BODY_CAP, DRAIN_CAP)
    if not lengths:
        return b""
    if len(lengths) != 1 or not lengths[0].strip().isdigit() or len(lengths[0].strip()) > 12:
        raise _BodyError(400)
    length = int(lengths[0].strip())
    if length > BODY_CAP:
        left = min(length, DRAIN_CAP)
        while left:
            chunk = handler.rfile.read(min(left, 65536))
            if not chunk:
                break
            left -= len(chunk)
        raise _BodyError(413)
    data = handler.rfile.read(length) if length else b""
    if len(data) != length:
        raise _BodyError(400)
    return data


def handler_class(host: str, auth: remote_auth.RemoteAuth | None = None):
    """The remote listener's handler, for exactly one trusted `host`;
    `auth` holds its passkeys and sessions (None: every `/m/api` is 401)."""
    origin = "https://" + host

    class RemoteHandler(BaseHTTPRequestHandler):
        # HTTP/1.0 (the default): one request per connection, so a body this
        # handler never reads cannot be taken for the next request.
        server_version = "jarvis"
        sys_version = ""

        def log_message(self, *args):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(2)
            self._cache = NO_STORE
            self._body_bytes = b""

        def send_response(self, code, message=None):
            # As the daemon's handler does: a request too malformed for a
            # version is answered as HTTP/1.0, so even it carries the headers.
            if self.request_version == "HTTP/0.9":
                self.request_version = "HTTP/1.0"
            super().send_response(code, message)

        def end_headers(self):
            # The one place every response passes through, a malformed
            # request's `send_error` included: no route can forget these.
            for name, value in SECURITY_HEADERS:
                self.send_header(name, value)
            self.send_header("Cache-Control", self._cache)
            super().end_headers()

        def send_error(self, code, message=None, explain=None):
            self.close_connection = True
            self._error(code, self.responses.get(code, ("error",))[0].lower())

        def _send(self, status, data: bytes, mime: str, *, cache: str = NO_STORE, extra=()):
            self._cache = cache
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            for name, value in extra:
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _error(self, status: int, word: str):
            # A fixed word per status: never the path, a header or a reason.
            self._send(status, json.dumps({"error": word}).encode(), "application/json")

        def _dispatch(self):
            self.close_connection = True
            try:
                # The body first, whatever the answer will be (module note).
                body_status = None
                try:
                    self._body_bytes = _read_body(self)
                except _BodyError as exc:
                    body_status = exc.status
                except (OSError, ValueError):
                    body_status = 400
                if _header(self.headers, "Host") != [host]:
                    return self._error(400, "untrusted host")
                if self.command not in ("GET", "HEAD") and _header(self.headers, "Origin") != [origin]:
                    return self._error(403, "cross-origin request refused")
                if body_status == 413:
                    return self._error(413, "request body too large")
                if body_status is not None:
                    return self._error(400, "bad request")
                path = self.path.split("?", 1)[0].split("#", 1)[0]
                if not path.startswith(PREFIX):
                    return self._error(404, "not found")
                return _route(self, path)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:     # noqa: BLE001
                LOG.warning("remote listener: %s %s", self.command, type(exc).__name__)
                try:
                    self._error(500, "internal error")
                except OSError:
                    pass

        do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _dispatch

    RemoteHandler.remote_host = host
    RemoteHandler.remote_auth = auth
    return RemoteHandler


def bind(host: str | None, port: int,
         auth: remote_auth.RemoteAuth | None = None) -> ThreadingHTTPServer | None:
    """The remote listener, bound to 127.0.0.1:`port` and not yet serving, or
    None: no host configured (the default: nothing is bound and nothing is
    logged), or the bind failed (logged; the daemon goes on without it).
    `auth` learns the bound server (`RemoteAuth.server`), which is how
    `remote_auth.is_remote_owner` knows a request arrived on it."""
    if not host:
        return None
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), handler_class(host, auth))
    except Exception as exc:     # noqa: BLE001 — never why the daemon fails
        LOG.warning("Remote listener not started: 127.0.0.1:%s unavailable (%s)", port,
                    type(exc).__name__)
        return None
    server.remote_only = True
    server.daemon_threads = True
    if auth is not None:
        auth.server = server
    LOG.info("Remote listener on 127.0.0.1:%d for the configured tailnet host",
             server.server_address[1])
    return server
