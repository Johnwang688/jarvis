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
import time
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


def fetch_redirect_checks() -> None:
    """BR-0: fetch_page judges every redirect hop *before* requesting it.

    Real httpx against loopback fixtures on ephemeral ports. On dfb1d7e httpx
    followed the hop itself, so the stand-in control listener was requested
    (a GET) before fetch_page refused where it had landed.
    """
    hits = []

    class Control(SimpleHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>control</body></html>")

        def log_message(self, *args):
            pass

    control = ThreadingHTTPServer(("127.0.0.1", 0), Control)
    cport = control.server_address[1]

    class Site(SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/to-control":
                return self._go(f"http://localhost:{cport}/approvals")
            if self.path == "/to-control-relative":
                return self._go(f"//127.0.0.1:{cport}/")
            if self.path == "/set-cookie":
                return self._go("/needs-cookie", cookie="seen=1")
            if self.path.startswith("/loop"):
                return self._go("/loop")
            if self.path == "/needs-cookie" and "seen=1" not in (self.headers.get("Cookie") or ""):
                body = b"<html><body>no cookie</body></html>"
            else:
                body = (b"<html><body><main><p>" + b"arrived with the cookie " * 12
                        + b"</p></main></body></html>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _go(self, where, cookie=None):
            self.send_response(302)
            self.send_header("Location", where)
            if cookie:
                self.send_header("Set-Cookie", cookie + "; Path=/")
            self.end_headers()

        def log_message(self, *args):
            pass

    site = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    for server in (control, site):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{site.server_address[1]}"
    real = config.DAEMON_PORT
    config.DAEMON_PORT = cport              # the stand-in is a control port now
    try:
        for path in ("/to-control", "/to-control-relative"):
            out = tools.dispatch("fetch_page", json.dumps({"url": base + path})).text
            assert "redirected to" in out and "blocked" in out, (path, out)
        assert hits == [], f"a redirect hop reached the control listener: {hits}"
        out = tools.dispatch("fetch_page", json.dumps({"url": base + "/set-cookie"})).text
        assert "arrived with the cookie" in out, out[:300]
        out = tools.dispatch("fetch_page", json.dumps({"url": base + "/loop"})).text
        assert "redirected more than" in out, out[:300]
    finally:
        config.DAEMON_PORT = real
        for server in (control, site):
            server.shutdown()
            server.server_close()
    print("ok  fetch_page: each redirect hop judged before it is requested (0 hits on the "
          "stand-in); an ordinary redirect keeps its cookie; a loop ends")

    # Typed URLs two parsers read differently are refused before any request.
    real_get = httpx.get

    def must_not_fetch(*args, **kwargs):
        raise AssertionError("fetch_page requested an ambiguous URL")

    httpx.get = must_not_fetch
    p = BrowserPolicy()
    try:
        for url in ("http:///localhost:8405/", "http://localhost:8405\\@93.184.216.34/",
                    "http://93.184.216.34\\@localhost:8405/", "http://\\localhost:8405/",
                    "http://93.184.216.34/\tx", "https:///127.0.0.1:8402/"):
            out = tools.dispatch("fetch_page", json.dumps({"url": url})).text
            assert "ambiguous" in out or "control plane" in out, (url, out)
            assert p.allows(url) is False, url
    finally:
        httpx.get = real_get
    assert p.allows("http://93.184.216.34/path\\with\\backslashes") is True, "only the authority"
    print("ok  fetch_page and the browser: an empty authority or a backslash in it is refused")


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
        assert _pumped_url(session) == "about:blank"
    finally:
        config.DAEMON_PORT = real_daemon_port
        session.stop(trace_name="policy-check-control")
        for server in (api, bouncer):
            server.shutdown()
            server.server_close()
    print("ok  browser: the API listener's HUD is refused typed, and backed out of on a redirect")


# A stand-in for the HUD's card, served by a real v2 daemon from its HUD
# listener: one button that approves whatever is pending, through the real
# owner-only route, with whatever Origin the browser sends.
STAND_IN_HUD = b"""<!doctype html><html><head><title>J.A.R.V.I.S.</title></head><body>
<div role="dialog"><p>git push origin main</p>
<button id="auth" onclick="approve()">AUTHORIZE</button><button>DENY</button></div>
<script>
async function approve() {
  const pending = await (await fetch('/approvals')).json();
  for (const r of pending)
    await fetch('/approvals/' + r.req_id, {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({decision: 'allow'})});
}
</script></body></html>"""


def _pumped_url(session: Session) -> str:
    """Where the page is, after letting Playwright process its events — on the
    browser thread, and without evaluating anything in a page that may be
    navigating (test-only, like `eval_js`)."""
    return session._submit(lambda: (session.page.wait_for_timeout(300), session.page.url)[-1])


_SAVED_CONFIG: dict = {}


def _restore_config() -> None:
    for name, value in _SAVED_CONFIG.items():
        setattr(config, name, value)
    _SAVED_CONFIG.clear()


def _daemon_with_a_card(tmp: Path):
    """A port-0 v2 daemon, its config in `tmp`, with one approval pending."""
    from jarvis.v2 import daemon as daemon_mod
    from jarvis.v2.approvals import ApprovalRequest
    from jarvis.v2.stores import Stores

    home = tmp / "home" / ".config" / "jarvis"
    home.mkdir(parents=True)
    for name, path in (("ALLOWLIST_PATH", home / "allowlist.json"),
                       ("MODELS_PATH", home / "models.json"),
                       ("PROVIDER_DEFAULTS_PATH", home / "provider_defaults.json"),
                       ("V2_DATA_DIR", tmp / "v2data"), ("V2_ALWAYS_ASK", home / "always-ask.json"),
                       ("DISCORD_GUILD_PATH", home / "discord_guild.json"),
                       ("ROUTING_PATH", home / "routing.json"),
                       ("CODEX_CATALOG_PATH", tmp / "codex-models.json")):
        _SAVED_CONFIG.setdefault(name, getattr(config, name))
        setattr(config, name, path)
    daemon = daemon_mod.Daemon(Stores(config.V2_DATA_DIR), {}, None, 0)
    daemon.start()
    answer: list = []
    asker = threading.Thread(target=lambda: answer.append(daemon.approvals.ask(ApprovalRequest(
        tool="Bash", args={"command": "git push origin main"}, command="git push origin main"))),
        daemon=True)
    asker.start()
    deadline = time.monotonic() + 5
    while not daemon.approvals.pending() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert daemon.approvals.pending(), "the card never queued"
    return daemon, answer


def _attacker(hud: str) -> ThreadingHTTPServer:
    """Pages that pass goto's check, then move themselves (or only pretend to)."""
    pages = {
        "/late": f"<p>hello</p><script>setTimeout(() => location = '{hud}', 300)</script>",
        "/replace": f"<p>hello</p><script>setTimeout(() => location.replace('{hud}'), 300)</script>",
        "/refresh": f"<head><meta http-equiv='refresh' content='0.3;url={hud}'></head><p>hello</p>",
        "/push": "<p>hello</p><button>stay</button>"
                 "<script>setTimeout(() => history.pushState({}, '', '/pushed'), 300)</script>",
    }

    class Attacker(SimpleHTTPRequestHandler):
        def do_GET(self):
            body = ("<!doctype html><html><body>" + pages.get(self.path, "<p>hello</p>")
                    + "</body></html>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Attacker)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def late_landing_checks() -> None:
    """BR-0, the high one: a page that moves itself onto the HUD listener
    *after* goto's check is never read, never clicked, and backed out of.

    On dfb1d7e (and a49423c) the next snapshot read the HUD and offered the
    card's AUTHORIZE as a ref, and clicking it approved the card: the POST
    left the HUD listener with the HUD's own Origin, so `owner_only` passed.
    Played against a port-0 daemon and a stand-in attacker on ephemeral
    ports, with `config.FACE_PORT`/`DAEMON_PORT` pointed at the daemon.
    """
    import tempfile

    from jarvis.v2 import hud_api

    huds = [("stand-in card", None)]
    if (hud_api.HUD_DIST / "index.html").is_file():
        huds.append(("the built HUD", hud_api.HUD_DIST))
    real_ports = (config.FACE_PORT, config.DAEMON_PORT)
    real_dist = hud_api.HUD_DIST
    for label, dist in huds:
        with tempfile.TemporaryDirectory(prefix="br0-late-") as tmp:
            tmp = Path(tmp)
            if dist is None:
                (tmp / "dist").mkdir()
                (tmp / "dist" / "index.html").write_bytes(STAND_IN_HUD)
                hud_api.HUD_DIST = tmp / "dist"
            try:
                daemon, answer = _daemon_with_a_card(tmp)
            except BaseException:
                _restore_config()
                hud_api.HUD_DIST = real_dist
                raise
            config.FACE_PORT, config.DAEMON_PORT = daemon.face_port, daemon.port
            hud = f"http://localhost:{daemon.face_port}/"
            attacker = _attacker(hud)
            # Same-site with the HUD (localhost, another port): a cross-site
            # navigation is already refused by the daemon (Sec-Fetch-Site).
            base = f"http://localhost:{attacker.server_address[1]}"
            session = Session(BrowserPolicy(headless=True))
            try:
                # The approve attempt, made the way an agent would make it.
                session.goto(f"{base}/late")
                time.sleep(1.5)            # Python idle; the page moves on its own
                refused = None
                try:
                    snap = session.snapshot()
                except BrowserError as exc:
                    refused = exc
                else:
                    refs = [line.split("]")[0].strip(" [") for line in snap.splitlines()
                            if "AUTHORIZE" in line and line.strip().startswith("[")]
                    for ref in refs:
                        try:
                            session.click(ref)
                        except BrowserError:
                            pass
                    time.sleep(0.5)
                assert refused is not None and "control plane" in str(refused), (
                    f"{label}: the snapshot read the HUD after a late landing")
                assert answer == [] and len(daemon.approvals.pending()) == 1, (
                    f"{label}: the card was approved from v1's browser: {answer}")
                assert _pumped_url(session) == "about:blank"

                # Each shape of moving later, through each read and act.
                for path, act in (("/late", "screenshot"), ("/replace", "snapshot"),
                                  ("/refresh", "click"), ("/replace", "type")):
                    session.goto(f"{base}{path}")
                    time.sleep(1.5)
                    try:
                        if act == "snapshot":
                            session.snapshot()
                        elif act == "screenshot":
                            session.screenshot_b64(marked=True)
                        elif act == "click":
                            session.click("e1")
                        else:
                            session.type_text("e1", "hello")
                        raise AssertionError(f"{label}: {path} then {act} was not refused")
                    except BrowserError as exc:
                        assert "control plane" in str(exc), (label, path, act, exc)
                    assert _pumped_url(session) == "about:blank", (path, act)

                # The event handler backs out on its own, before any check runs:
                # eval_js (tests only) is no guarded path, yet it lands on blank.
                session.goto(f"{base}/late")
                time.sleep(1.5)
                assert _pumped_url(session) == "about:blank", "no back-out at once"
                try:
                    session.snapshot()
                    raise AssertionError("the back-out was not reported")
                except BrowserError as exc:
                    assert "control plane" in str(exc), exc

                # A same-origin pushState is not a landing anywhere new.
                session.goto(f"{base}/push")
                time.sleep(1.0)
                snap = session.snapshot()
                assert "/pushed" in snap and "stay" in snap, snap[:300]
                stay = [line.split("]")[0].strip(" [") for line in snap.splitlines()
                        if "stay" in line and line.strip().startswith("[")]
                assert stay, snap[:300]
                session.click(stay[0])
                assert answer == [] and len(daemon.approvals.pending()) == 1, answer
            finally:
                config.FACE_PORT, config.DAEMON_PORT = real_ports
                hud_api.HUD_DIST = real_dist
                session.stop(trace_name="policy-check-late")
                attacker.shutdown()
                attacker.server_close()
                daemon.stop()
                _restore_config()
        print(f"ok  browser ({label}): a late landing on the HUD listener is never read or "
              "clicked, in four shapes; a same-origin pushState is fine")


def read_gate_checks() -> None:
    """BR-0, the two guards no timing reaches on its own.

    The scan reads and stamps only the document whose origin was judged: given
    another origin it reports `moved` and stamps no ref. And a screenshot is
    judged after the capture too: a page that moves onto a control port while
    it is taken is backed out of and the picture withheld.
    """
    from jarvis import browser as browser_mod

    class Page(SimpleHTTPRequestHandler):
        def do_GET(self):
            body = b"<!doctype html><html><body><button>press</button><p>text</p></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    site = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    control = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    for server in (site, control):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    real = config.FACE_PORT
    config.FACE_PORT = control.server_address[1]          # the stand-in is the face now
    session = Session(BrowserPolicy(headless=True))
    try:
        session.goto(f"http://localhost:{site.server_address[1]}/")
        args = {**Session._scan_args(120, text=True), "origin": "http://elsewhere.example"}
        result = session._submit(lambda: session.page.evaluate(browser_mod._SNAPSHOT_JS, args))
        assert result.get("moved", "").startswith("http://localhost:"), result
        assert result["elements"] == [] and result["text"] == "", result
        stamped = session.eval_js("document.querySelectorAll('[data-jarvis-ref]').length")
        assert stamped == 0, f"a document nobody judged got {stamped} refs"
        assert "press" in session.snapshot(), "the judged document is still read"

        hud = f"http://localhost:{config.FACE_PORT}/"

        def moving_capture(page, original):
            def capture(**kwargs):
                page.evaluate(f"location.href = '{hud}'")
                page.wait_for_timeout(500)
                return original(**kwargs)
            return capture

        session._submit(lambda: setattr(session.page, "screenshot",
                                        moving_capture(session.page, session.page.screenshot)))
        try:
            session.screenshot_b64()
            raise AssertionError("a picture taken across a move onto a control port was returned")
        except BrowserError as exc:
            assert "control plane" in str(exc), exc
    finally:
        config.FACE_PORT = real
        session.stop(trace_name="policy-check-gate")
        for server in (site, control):
            server.shutdown()
            server.server_close()
    print("ok  browser: the scan reads only the judged document; a screenshot across a move "
          "onto a control port is withheld")


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
        assert _pumped_url(session) == "about:blank"
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
    fetch_redirect_checks()
    landing_guard_checks()
    control_port_landing_checks()
    late_landing_checks()
    read_gate_checks()
    print("\nall policy checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
