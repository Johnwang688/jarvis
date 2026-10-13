"""`/browser…` (BR-1a): the owner's Browser, owner-only on the HUD listener.

Every route is `projects.owner_only` (the HUD listener, and the HUD's own page
asking: its Origin, or a same-origin GET) plus a strict loopback `Host`,
checked before anything else — before a body is read, a tab is looked up or
Chrome is started. The API listener every tool's HTTP client uses gets 403,
whatever it sends: the Browser holds the owner's logins, and a page in it
could otherwise be driven by any local client. Nothing here takes a query
string; URLs travel only in JSON bodies, and nothing a route answers with a
refusal ever contains one.

    GET    /browser                          state: running, profiles, tabs, settings
    GET    /browser/settings                 {lan, local_servers, auto_open}
    PATCH  /browser/settings                 {lan?, local_servers?, auto_open?}  (browser.json, protected)
    GET    /browser/tabs                     [{tab_id, profile, ctx, url, title}]
    POST   /browser/tabs                     {profile?, ctx?, url?} -> 201 {tab_id, ...}
    DELETE /browser/tabs/<id>
    POST   /browser/tabs/<id>/navigate       {url}  (the URL judge first)
    POST   /browser/tabs/<id>/history        {action: back | forward | reload}
    POST   /browser/profiles/<id>/forget     {}  ("Sign out of everything")
    GET    /browser/profiles                 [{id, label, created}]
    POST   /browser/profiles                 {label} -> 201 profile row
    PATCH  /browser/profiles/<id>            {label} -> profile row
    DELETE /browser/profiles/<id>            closes and deletes only this profile
    POST   /browser/profiles/<id>/signin     {} -> 201 exact owner settings entry
    DELETE /browser/contexts/<ctx>           closes a Private context and its data

BR-1b's (stream.py, files.py):

    POST   /browser/tabs/<id>/ticket         {} -> {ticket, expires_in}  (single use, 30 s)
    GET    /browser/tabs/<id>/attach?ticket= the tab's socket (101); the only query
    POST   /browser/tabs/<id>/activate       {}  (switch: brought to the front)
    POST   /browser/tabs/<id>/files          {chooser, files: [{project, path}]}
    GET    /browser/downloads                {downloads: [...], in_progress}
    GET    /browser/downloads/<id>           the file, as an attachment
    DELETE /browser/downloads/<id>

While any approval card is waiting, nothing reaches a page: `navigate`,
`history`, a new tab with a URL and `files` answer 409, and the socket holds
every input (stream.py).

A URL the owner opens on this machine (`localhost`, `127.0.0.1` …) with
`POST /browser/tabs` or `…/navigate` opens that loopback port in the gate for
as long as an authorizing tab remains on the port; its last departure also
revokes established local tunnels (BR-1c; review of PR #69). History, a page's own moves and any agent never open one, and
neither does auto-open (amendment 2, L2): it loads only in a Direct frame, and
a loopback port opens to the Remote browser only through the owner's own
navigation.

The settings are the daemon's `BrowserSettings` (`settings.py`, the one
reader and writer of `browser.json`): any non-empty subset of the switches,
each a boolean; an unknown field is counted, never echoed.

Profile management is BR-1c's.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import quote

from . import files as _files
from .files import FileRefused
from .host import BrowserRefused
from .panes import PaneRefused
from .policy import URLRefused
from .settings import DEFAULTS as SETTINGS
from .stream import SOCKET_PENDING

LOG = logging.getLogger(__name__)

# A download runs nothing, whatever it is (narrowing the HUD's one policy).
DOWNLOAD_CSP = "default-src 'none'"
# A download is fetched or opened as a document, never embedded: a script,
# style, worker, frame or image request for one is refused.
DOWNLOAD_DESTS = ("", "document", "empty")
DOWNLOAD_CHUNK = 1024 * 1024


def _fail(status: int, text: str):
    from ..daemon import APIError
    raise APIError(status, text)


def _strict_host(handler, daemon) -> None:
    """The Host must name the HUD listener on loopback exactly (as
    `/terminals` and `/vex` require): a rebound name never gets this far."""
    port = daemon.face_port
    if handler.headers.get("Host", "") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
        _fail(403, "untrusted Host")


def _disposition(name: str) -> str:
    """`attachment`, with an ASCII fallback and the UTF-8 name (RFC 6266);
    `name` is already cleaned (no control, quote or path character)."""
    ascii_name = "".join(c if c.isascii() and c.isprintable() and c not in '"\\%;' else "_"
                         for c in name) or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


def _serve_download(handler, download_id):
    """A saved download, to the owner's window only, as an attachment that
    runs nothing (files.py)."""
    dest = handler.headers.get("Sec-Fetch-Dest", "")
    if dest not in DOWNLOAD_DESTS:
        _fail(403, "a download is opened or fetched, never embedded")
    fd, name, size = _files.open_download(download_id)
    try:
        from ..hud_api import frame_headers
        frame_headers(handler, DOWNLOAD_CSP)        # checked before a byte is written
        handler.send_response(200)
        handler.send_header("Content-Type", "application/octet-stream")
        handler.send_header("Content-Length", str(size))
        handler.send_header("Content-Disposition", _disposition(name))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler._csp = DOWNLOAD_CSP
        handler.end_headers()
        handler._streaming = True
        handler.connection.settimeout(30)
        sent = 0
        while sent < size:
            chunk = os.read(fd, min(DOWNLOAD_CHUNK, size - sent))
            if not chunk:
                break
            handler.wfile.write(chunk)
            sent += len(chunk)
    finally:
        os.close(fd)
    return 200, None


def route(handler, daemon, parts, query):
    """`/browser…`, or None."""
    if not parts or parts[0] != "browser":
        return None
    from ..daemon import _object
    from ..projects import owner_only
    owner_only(handler, daemon)
    _strict_host(handler, daemon)
    method = handler.command
    attach = len(parts) == 4 and parts[1] == "tabs" and parts[3] == "attach" and method == "GET"
    # The socket's ticket is the one query any route takes (it is never logged:
    # `/browser` paths are logged without their query).
    _object(query, ("ticket",) if attach else ())
    host = daemon.browser
    settings = daemon.browser_settings
    try:
        if parts == ["browser", "front"] and method == "POST":
            return 200, daemon.browser_panes.report(handler._body())
        if len(parts) >= 3 and parts[1] == "panes":
            if parts[2] not in tuple(str(n) for n in range(1, 9)):
                _fail(400, "choose a Browser pane number")
            pane_id = int(parts[2])
            if len(parts) == 3 and method == "PATCH":
                return 200, daemon.browser_panes.permissions(pane_id, handler._body())
            if len(parts) == 6 and parts[3] == "tabs" and parts[5] == "address" and method == "POST":
                return 200, daemon.browser_panes.address(pane_id, parts[4], handler._body())
        if attach:
            from .. import ws
            from ..hud_api import frame_headers
            try:
                key = ws.handshake_key(handler.headers)
            except ws.HandshakeError as exc:
                _fail(400, str(exc))
            tab = host.require_tab(parts[2])
            if not host.tickets.redeem(query.get("ticket"), tab.id):
                _fail(403, "a fresh ticket for this tab is required")
            # The 101 is written by hand, past `end_headers`: it carries the
            # listener's frame headers explicitly (WP-E), as the terminal's does.
            sock = ws.upgrade(handler, key, headers=frame_headers(handler), max_pending=SOCKET_PENDING)
            host.serve_socket(tab.id, sock)
            return 200, None
        if parts == ["browser", "downloads"] and method == "GET":
            return 200, host.downloads()
        if len(parts) == 3 and parts[1] == "downloads":
            if method == "GET":
                return _serve_download(handler, parts[2])
            if method == "DELETE":
                return 200, _files.delete(parts[2])
        if parts == ["browser"] and method == "GET":
            return 200, {**host.status(), "profiles": host.profiles(), "settings": settings.snapshot()}
        if parts == ["browser", "settings"]:
            if method == "GET":
                return 200, settings.snapshot()
            if method == "PATCH":
                body = _object(handler._body(), tuple(SETTINGS))
                if not body:
                    _fail(400, "missing fields: " + " or ".join(SETTINGS))
                try:
                    values = settings.update(**body)
                except ValueError as refusal:
                    _fail(400, str(refusal))
                except OSError as exc:
                    LOG.warning("browser settings not saved (%s)", type(exc).__name__)
                    _fail(500, "the Browser settings could not be saved")
                LOG.info("browser settings changed by the owner: %s",
                         ", ".join(f"{k} {'on' if v else 'off'}" for k, v in sorted(body.items())))
                daemon._dev_settings_changed()
                host.settings_changed()
                return 200, values
        if parts == ["browser", "profiles"]:
            if method == "GET":
                return 200, host.list_profiles()
            if method == "POST":
                body = _object(handler._body(), ("label",), ("label",))
                return 201, host.create_profile(body["label"])
        if len(parts) == 3 and parts[1] == "profiles":
            if method == "PATCH":
                body = _object(handler._body(), ("label",), ("label",))
                return 200, host.rename_profile(parts[2], body["label"])
            if method == "DELETE":
                return 200, host.delete_profile(parts[2])
        if len(parts) == 3 and parts[1] == "contexts" and method == "DELETE":
            return 200, host.close_context(parts[2])
        if (len(parts) == 4 and parts[1] == "profiles" and parts[3] == "signin"
                and method == "POST"):
            _object(handler._body(), ())
            return 201, host.signin(parts[2])
        # `owner=True` below is the owner's own navigation (this route is the
        # owner's only): it opens the URL's loopback port in the gate, as
        # nothing a page or an agent does ever can (MED 2 of PR #69's review).
        if parts == ["browser", "tabs"]:
            if method == "GET":
                return 200, host.tabs()
            if method == "POST":
                body = _object(handler._body(), ("profile", "ctx", "url"))
                return 201, host.open_tab(body.get("profile"), body.get("url"), body.get("ctx"),
                                          owner=True)
        if len(parts) == 3 and parts[1] == "tabs" and method == "DELETE":
            return 200, host.close_tab(parts[2])
        if len(parts) == 4 and parts[1] == "tabs" and method == "POST":
            if parts[3] == "navigate":
                body = _object(handler._body(), ("url",), ("url",))
                return 200, host.navigate(parts[2], body["url"], owner=True)
            if parts[3] == "restore":
                # A stored Remote page is restored lazily, without manufacturing
                # a new owner navigation gesture or opening a loopback lease.
                body = _object(handler._body(), ("url",), ("url",))
                return 200, host.navigate(parts[2], body["url"], owner=False)
            if parts[3] == "history":
                body = _object(handler._body(), ("action",), ("action",))
                return 200, host.history(parts[2], body["action"])
            if parts[3] == "ticket":
                _object(handler._body(), ())
                return 200, host.ticket(parts[2])
            if parts[3] == "activate":
                _object(handler._body(), ())
                return 200, host.activate(parts[2])
            if parts[3] == "files":
                # The owner's answer to a page's file chooser: files the owner
                # chose, by project and path, judged by H8 (files.py).
                body = _object(handler._body(), ("chooser", "files"), ("chooser", "files"))
                host.require_tab(parts[2])
                if host.input_held():
                    _fail(409, "an approval card is waiting for you; nothing reaches the Browser "
                               "until it is answered")
                payloads = _files.upload_payloads(daemon, body["files"])
                return 200, host.set_files(parts[2], body["chooser"], payloads)
        if (len(parts) == 4 and parts[1] == "profiles" and parts[3] == "forget"
                and method == "POST"):
            _object(handler._body(), ())
            return 200, host.forget(parts[2])
    except URLRefused as refusal:
        _fail(400, str(refusal))
    except (BrowserRefused, FileRefused, PaneRefused) as refusal:
        _fail(refusal.status, str(refusal))
    _fail(404, "route not found")
