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
    GET    /browser/settings                 {lan, local_servers}
    PATCH  /browser/settings                 {lan?, local_servers?}  (browser.json, protected)
    GET    /browser/tabs                     [{tab_id, profile, ctx, url, title}]
    POST   /browser/tabs                     {profile?, ctx?, url?} -> 201 {tab_id, ...}
    DELETE /browser/tabs/<id>
    POST   /browser/tabs/<id>/navigate       {url}  (the URL judge first)
    POST   /browser/tabs/<id>/history        {action: back | forward | reload}
    POST   /browser/profiles/<id>/forget     {}  ("Sign out of everything")

A URL the owner opens on this machine (`localhost`, `127.0.0.1` …) with
`POST /browser/tabs` or `…/navigate` opens that loopback port in the gate for
this host's lifetime: loopback is closed like the LAN until then (review of
PR #69). History, a page's own moves and any agent never open one, and
neither does auto-open (amendment 2, L2): it loads only in a Direct frame, and
a loopback port opens to the Remote browser only through the owner's own
navigation.

The stream, input, dialogs, downloads and the tab strip are BR-1b's; profile
management is BR-1c's.
"""

from __future__ import annotations

from .host import BrowserRefused, load_settings, save_settings
from .policy import URLRefused


def _fail(status: int, text: str):
    from ..daemon import APIError
    raise APIError(status, text)


def _strict_host(handler, daemon) -> None:
    """The Host must name the HUD listener on loopback exactly (as
    `/terminals` and `/vex` require): a rebound name never gets this far."""
    port = daemon.face_port
    if handler.headers.get("Host", "") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
        _fail(403, "untrusted Host")


def route(handler, daemon, parts, query):
    """`/browser…`, or None."""
    if not parts or parts[0] != "browser":
        return None
    from ..daemon import _object
    from ..projects import owner_only
    owner_only(handler, daemon)
    _strict_host(handler, daemon)
    _object(query, ())
    host = daemon.browser
    method = handler.command
    try:
        if parts == ["browser"] and method == "GET":
            return 200, {**host.status(), "profiles": host.profiles(), "settings": load_settings()}
        if parts == ["browser", "settings"]:
            if method == "GET":
                return 200, load_settings()
            if method == "PATCH":
                body = _object(handler._body(), ("lan", "local_servers"))
                if not body:
                    _fail(400, "missing fields: lan or local_servers")
                return 200, save_settings(body)
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
            if parts[3] == "history":
                body = _object(handler._body(), ("action",), ("action",))
                return 200, host.history(parts[2], body["action"])
        if (len(parts) == 4 and parts[1] == "profiles" and parts[3] == "forget"
                and method == "POST"):
            _object(handler._body(), ())
            return 200, host.forget(parts[2])
    except URLRefused as refusal:
        _fail(400, str(refusal))
    except BrowserRefused as refusal:
        _fail(refusal.status, str(refusal))
    _fail(404, "route not found")
