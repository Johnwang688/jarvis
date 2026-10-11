"""Synthetic checks for the internet-access policy. Free — no API calls.

Covers the 2026-07-31 opening of the browser to the public internet:
'*' in allowed_hosts means public addresses only, private/LAN space and the
face's origin stay blocked, named hosts always pass, fetch_page applies the
same rule (including after redirects), and the browser backs out of a
redirect that lands on a blocked host. Since BR-0, every port in
`config.control_ports()` is refused like the face's (v2's API listener
serves the same HUD page), the workshop's 8403 stays open, and a real
headless session cannot load a stand-in API listener's page.

IP-literal cases need no DNS; name cases monkeypatch the resolver, so the
whole file runs offline except one headless-Chromium launch at the end.

Run:  .venv/bin/python tests/browser/policy_check.py
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import types
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx

from jarvis import config, tools
from jarvis.browser import BrowserError, BrowserPolicy, Session

PORT = 8431


def policy_checks() -> None:
    p = BrowserPolicy()  # the default: localhost, 127.0.0.1, '*'

    # Public IP space is open now.
    assert p.allows("http://93.184.216.34/") is True
    assert p.allows("https://8.8.8.8/") is True

    # Private/LAN space is not, in every flavor.
    assert p.allows("http://192.168.1.1/") is False, "RFC1918 (router)"
    assert p.allows("http://10.0.0.5/") is False, "RFC1918"
    assert p.allows("http://172.16.0.1/") is False, "RFC1918"
    assert p.allows("http://169.254.169.254/") is False, "link-local / metadata"
    assert p.allows("http://100.64.0.1/") is False, "CGNAT"

    # Named local hosts still pass (they are listed, not wildcarded).
    assert p.allows("http://localhost:8000/") is True
    assert p.allows("http://127.0.0.1:8000/") is True

    # The face stays refused ahead of everything.
    assert p.allows(f"http://localhost:{config.FACE_PORT}/") is False

    # An explicitly named LAN host passes without resolving — the owner's
    # allowlist outranks the private-space block.
    trusting = BrowserPolicy(allowed_hosts=["192.168.1.50", "*"])
    assert trusting.allows("http://192.168.1.50/") is True
    assert trusting.allows("http://192.168.1.51/") is False

    # Dropping '*' pins the session (what benches do).
    pinned = BrowserPolicy(allowed_hosts=["localhost", "127.0.0.1"])
    assert pinned.allows("http://93.184.216.34/") is False
    assert pinned.allows("http://localhost:8000/") is True
    print("ok  policy: public open, LAN blocked, named hosts pass, no-'*' pins")


def resolver_checks() -> None:
    real = socket.getaddrinfo

    def fake(host, *args, **kwargs):
        table = {
            "public.example": "93.184.216.34",
            "sneaky.example": "127.0.0.1",  # public name, loopback answer
            "internal.example": "192.168.1.7",
        }
        if host in table:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], 0))]
        raise socket.gaierror(f"unresolvable in test: {host}")

    socket.getaddrinfo = fake
    try:
        p = BrowserPolicy()
        assert p.allows("http://public.example/") is True
        assert p.allows("http://sneaky.example/") is False, (
            "a name resolving to loopback must not be a route to local services"
        )
        assert p.allows(f"http://sneaky.example:{config.FACE_PORT}/") is False, (
            "…especially not to the face"
        )
        assert p.allows("http://internal.example/") is False
        assert p.allows("http://no-such.example/") is False, "unresolvable ⇒ refused"
    finally:
        socket.getaddrinfo = real
    print("ok  resolver: names judged by what they resolve to; unresolvable refused")


def fetch_page_checks() -> None:
    real_get = httpx.get

    def must_not_fetch(*args, **kwargs):
        raise AssertionError("fetch_page hit the network for a blocked URL")

    httpx.get = must_not_fetch
    try:
        out = tools.dispatch("fetch_page", json.dumps({"url": "http://192.168.1.1/"}))
        assert "private network" in out.text, out.text
        out = tools.dispatch(
            "fetch_page", json.dumps({"url": f"http://localhost:{config.FACE_PORT}/x"})
        )
        assert "control plane" in out.text, out.text
    finally:
        httpx.get = real_get
    print("ok  fetch_page: LAN and face refused before any bytes move")

    # A public URL whose redirect chain ends somewhere private is dropped.
    def fake_get(*args, **kwargs):
        return types.SimpleNamespace(
            url=httpx.URL("http://192.168.1.1/admin"),
            status_code=200,
            headers={"content-type": "text/html"},
            text="<html><body>router admin</body></html>",
        )

    httpx.get = fake_get
    try:
        out = tools.dispatch("fetch_page", json.dumps({"url": "http://93.184.216.34/"}))
        assert "redirected to" in out.text and "blocked" in out.text, out.text
    finally:
        httpx.get = real_get
    print("ok  fetch_page: redirect to a private host is refused after the fetch")


def control_port_policy_checks() -> None:
    """BR-0: every Jarvis control port is refused, not only the face's.

    v2's API listener (8405) serves the very same HUD page as the face, and on
    a49423c the browser and `fetch_page` refused only the face's port. Every
    assertion here about a port other than 8402 fails there.
    """
    p = BrowserPolicy()
    for port in (8402, 8404, 8405, 8406):
        for host in ("localhost", "127.0.0.1", "[::1]", "foo.localhost", "0.0.0.0", "127.0.0.2"):
            assert p.allows(f"http://{host}:{port}/") is False, (host, port)
    assert p.allows("http://localhost:8403/") is True, "the workshop stays open (v1's designer)"
    assert p.allows("http://localhost:5173/") is True, "a dev server stays open"

    # A listener moved by an environment variable is refused where it is
    # configured *and* at its default (a daemon elsewhere may sit there).
    real = (config.FACE_PORT, config.DESKTOP_PORT, config.DAEMON_PORT, config.SPOTIFY_AUTH_PORT)
    config.FACE_PORT, config.DESKTOP_PORT, config.DAEMON_PORT, config.SPOTIFY_AUTH_PORT = (
        18402, 18404, 18405, 18406)
    try:
        ports = config.control_ports()
        assert {18402, 18404, 18405, 18406, 8402, 8404, 8405, 8406} <= ports, ports
        for port in (18402, 18404, 18405, 18406, 8405):
            assert p.allows(f"http://localhost:{port}/") is False, port
    finally:
        config.FACE_PORT, config.DESKTOP_PORT, config.DAEMON_PORT, config.SPOTIFY_AUTH_PORT = real
    assert p.allows("http://localhost:18405/") is True, "and only while it is configured there"
    print("ok  control ports: 8402/8404/8405/8406 and their configured ports refused; 8403 open")

    real_get = httpx.get

    def must_not_fetch(*args, **kwargs):
        raise AssertionError("fetch_page hit a control-plane port")

    httpx.get = must_not_fetch
    try:
        out = tools.dispatch("fetch_page", json.dumps({"url": "http://localhost:8405/"}))
        assert "control plane" in out.text, out.text
    finally:
        httpx.get = real_get

    def lands_on_the_api(*args, **kwargs):
        return types.SimpleNamespace(
            url=httpx.URL("http://localhost:8405/"),
            status_code=200,
            headers={"content-type": "text/html"},
            text="<html><head><title>J.A.R.V.I.S.</title></head><body>hud</body></html>",
        )

    httpx.get = lands_on_the_api
    try:
        out = tools.dispatch("fetch_page", json.dumps({"url": "http://93.184.216.34/"}))
        assert "redirected to" in out.text and "blocked" in out.text, out.text
        assert "hud" not in out.text, out.text
    finally:
        httpx.get = real_get
    print("ok  fetch_page: 8405 refused before the request, and as a redirect's landing")


def control_port_landing_checks() -> None:
    """A real headless browser cannot load the HUD page from the API listener.

    Played against a stand-in API listener on an ephemeral port (never 8405):
    `config.DAEMON_PORT` points at it for the duration. Typed, it is refused
    before Chromium connects (the stand-in counts zero requests); reached by a
    redirect from an allowed page, the session backs out to about:blank. On
    a49423c the typed load succeeds and the agent reads the HUD.
    """
    hits = []

    class FakeApi(SimpleHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            body = b"<html><head><title>J.A.R.V.I.S.</title></head><body>AUTHORIZE</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    api = ThreadingHTTPServer(("127.0.0.1", 0), FakeApi)
    api_port = api.server_address[1]

    class Bouncer(SimpleHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{api_port}/")
            self.end_headers()

        def log_message(self, *args):
            pass

    bouncer = ThreadingHTTPServer(("127.0.0.1", 0), Bouncer)
    for server in (api, bouncer):
        threading.Thread(target=server.serve_forever, daemon=True).start()

    real_daemon_port = config.DAEMON_PORT
    config.DAEMON_PORT = api_port
    session = Session(BrowserPolicy(headless=True))
    try:
        try:
            out = session.goto(f"http://localhost:{api_port}/")
            raise AssertionError(f"the API listener's HUD page loaded: {out[:80]!r}")
        except BrowserError as exc:
            assert "control plane" in str(exc), exc
        assert hits == [], f"Chromium connected to the API listener: {hits}"
        try:
            session.goto(f"http://127.0.0.1:{bouncer.server_address[1]}/go")
            raise AssertionError("a redirect onto the API listener was not caught")
        except BrowserError as exc:
            assert "backed out" in str(exc), exc
        assert session.eval_js("location.href") == "about:blank"
    finally:
        config.DAEMON_PORT = real_daemon_port
        session.stop(trace_name="policy-check-control")
        for server in (api, bouncer):
            server.shutdown()
            server.server_close()
    print("ok  browser: the API listener's HUD is refused typed, and backed out of on a redirect")


def landing_guard_checks() -> None:
    """A pinned session that gets redirected off-list backs out to about:blank."""

    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/bounce":
                # 127.0.0.1 is on the pinned list below; localhost is not —
                # a stand-in for "public page redirects somewhere blocked"
                # that needs no live DNS.
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{PORT}/landed")
                self.end_headers()
            else:
                body = b"<html><body>landed</body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    policy = BrowserPolicy(allowed_hosts=["127.0.0.1"], headless=True)
    session = Session(policy)
    try:
        try:
            session.goto(f"http://127.0.0.1:{PORT}/bounce")
            raise AssertionError("redirect off the allowlist was not caught")
        except BrowserError as exc:
            assert "backed out" in str(exc), exc
        assert session.eval_js("location.href") == "about:blank"
    finally:
        session.stop(trace_name="policy-check")
        server.shutdown()
        server.server_close()
    print("ok  browser: redirect to a blocked host backs out to about:blank")


def main() -> int:
    policy_checks()
    resolver_checks()
    fetch_page_checks()
    control_port_policy_checks()
    landing_guard_checks()
    control_port_landing_checks()
    print("\nall policy checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
