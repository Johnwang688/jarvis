"""The owner-only gate's reads, against a real daemon and real Chromium
(`projects.is_owner`, 2026-10-11).

Free and hermetic. The daemon listens on three ephemeral loopback ports —
never 8402, 8403 or 8405, which the owner's daemon holds — and the browser
context refuses every request to those three. Every config path and HOME are
temp; `JARVIS_TERMINAL_SHELL` is the scripted stand-in, and no test here
opens a terminal anyway (the one owner write is a POST the route refuses as a
bad body *after* the gate, a 400 rather than a 403).

**The bug this pins.** Chromium sends no `Origin` on a same-origin GET, and
`is_owner` required one, so every `GET /terminals` from the HUD's own page
was 403 on the owner's live daemon: the panel only ever worked through its
"could not check" fallback, and opening it could never open a terminal. The
HUD suites missed it because they run against `MockDaemon`, which does not
enforce `owner_only`. So the reads are checked here with a real browser's
own headers, never hand-written ones:

  - a page loaded from the HUD listener can `GET /terminals` (fails on main);
  - a POST from that page still needs its Origin — which the browser sends;
  - a page from another ephemeral port (cross-origin) cannot read the
    listing, and gets no CORS read either;

`is_owner` itself, one clause at a time (`IsOwnerClauses`): over HTTP the
listener, Host and Origin checks that run first (`check_origin`,
`_strict_host`) refuse most of these before `is_owner` is asked, so a 403
alone cannot say which clause held; called directly, each one has to.

And by hand, the shapes no browser page can send but a refusal must cover:
a GET with no Origin and `Sec-Fetch-Site` same-site / cross-site / none /
"none"; a non-loopback Host; a POST, PATCH or DELETE with no Origin (even
marked same-origin); `Origin: null`; a WebSocket upgrade with no Origin; and
the API listener.

Run: PYTHONPATH=. .venv/bin/python tests/v2/owner_read_check.py
"""
from __future__ import annotations

from email.message import Message
import http.client
import http.server
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis import config  # noqa: E402
from jarvis.v2 import projects  # noqa: E402
from jarvis.v2.daemon import Daemon  # noqa: E402
from jarvis.v2.model import ProviderName as PN  # noqa: E402
from jarvis.v2.provider import Decision, Event, EventKind as K, SessionHandle, Usage  # noqa: E402
from jarvis.v2.stores import Stores  # noqa: E402

FAKE_SHELL = Path(__file__).resolve().parent / "terminal_fake" / "sh"
LIVE_PORTS = {8402, 8403, 8405}
LIVE = re.compile(r"^[a-z]+://[^/]*:(8402|8403|8405)(/|$)")

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - the venv has it
    sync_playwright = None

# Base points HOME at a temp folder, and Playwright looks for its browsers
# under HOME unless told where they are: pin the real cache before that.
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache" / "ms-playwright"))


class Fake:
    def __init__(self, name):
        self.name = name

    def health(self):
        return True, "fake"

    def start(self, thread, brief, permit):
        return SessionHandle(thread.id, self.name, "fake:" + thread.id, SimpleNamespace(brief=brief))

    resume = start

    def usage(self, handle):
        return Usage()

    def send(self, handle, message):
        yield Event(K.TEXT, handle.thread_id, {"text": "ok"})
        yield Event(K.TURN_FINISHED, handle.thread_id, {"stop": "end"})

    def interrupt(self, handle):
        pass

    def close(self, handle):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        home = self.root / "home"
        home.mkdir()
        env = patch.dict(os.environ, {"HOME": str(home), "XDG_DATA_HOME": str(self.root / "xdg"),
                                      "JARVIS_CLAUDE_CREDENTIALS": str(self.root / "none.json")})
        env.start()
        self.addCleanup(env.stop)
        for name, value in dict(V2_DATA_DIR=self.root / "data", ALLOWLIST_PATH=self.root / "allow.json",
                                MODELS_PATH=self.root / "models.json",
                                PROVIDER_DEFAULTS_PATH=self.root / "provider_defaults.json",
                                DISCORD_GUILD_PATH=self.root / "guild.json",
                                TERMINAL_SHELL=str(FAKE_SHELL)).items():
            guard = patch.object(config, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        self.daemon = Daemon(Stores(), {name: Fake(name) for name in PN},
                             lambda *_: lambda *_: Decision.DENY, 0)
        self.daemon.start()
        self.addCleanup(self.daemon.stop)
        ports = {self.daemon.port, self.daemon.face_port, self.daemon.workshop_port}
        self.assertFalse(ports & LIVE_PORTS, "a test daemon must never hold a live port")
        self.face = self.daemon.face_port

    def raw(self, method, path, headers, *, port=None, body=None):
        """One request with exactly these headers (http.client adds Host only if absent)."""
        port = port or self.face
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.putrequest(method, path, skip_host="Host" in headers, skip_accept_encoding=True)
            for k, v in headers.items():
                conn.putheader(k, v)
            data = json.dumps(body).encode() if body is not None else b""
            if body is not None or method in ("POST", "PATCH", "PUT"):
                conn.putheader("Content-Type", "application/json")
                conn.putheader("Content-Length", str(len(data)))
            conn.endheaders(data or None)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()


FACE = 41000


def handler(method="GET", *, port=FACE, host=f"127.0.0.1:{FACE}", origin=None, site="same-origin",
            upgrade=None):
    """A stand-in for the request handler `is_owner` reads: its listener's
    port, its method and its headers (case-insensitive, as http.server's)."""
    headers = Message()
    if host is not None:
        headers["Host"] = host
    if origin is not None:
        headers["Origin"] = origin
    if site is not None:
        headers["Sec-Fetch-Site"] = site
    if upgrade is not None:
        headers["Upgrade"] = upgrade
    return SimpleNamespace(command=method, headers=headers,
                           server=SimpleNamespace(server_address=("127.0.0.1", port)))


class IsOwnerClauses(unittest.TestCase):
    """Each clause of `is_owner` held on its own: from a request it accepts,
    change one thing and it must refuse."""

    daemon = SimpleNamespace(face_port=FACE)

    def owner(self, **kw) -> bool:
        return projects.is_owner(handler(**kw), self.daemon)

    def test_a_same_origin_read_on_the_hud_listener_is_the_owners(self):
        self.assertTrue(self.owner())
        self.assertTrue(self.owner(method="HEAD"))
        self.assertTrue(self.owner(host=f"localhost:{FACE}"))

    def test_the_hud_listener_is_required(self):
        self.assertFalse(self.owner(port=FACE + 1, host=f"127.0.0.1:{FACE + 1}"))
        self.assertFalse(self.owner(port=FACE + 1, host=f"127.0.0.1:{FACE + 1}", origin=f"http://127.0.0.1:{FACE + 1}"))
        self.assertFalse(self.owner(port=FACE + 1))

    def test_a_read_with_no_origin_needs_a_loopback_host(self):
        for host in (f"evil.example:{FACE}", f"127.0.0.1.nip.io:{FACE}", "127.0.0.1", f"127.0.0.1:{FACE + 1}",
                     f"[::1]:{FACE}", "", None):
            with self.subTest(host=host):
                self.assertFalse(self.owner(host=host))

    def test_a_read_with_no_origin_needs_same_origin_fetch_metadata(self):
        for site in ("same-site", "cross-site", "none", "", "Same-Origin", None):
            with self.subTest(site=site):
                self.assertFalse(self.owner(site=site))

    def test_only_a_plain_read_may_come_without_an_origin(self):
        for method in ("POST", "PATCH", "PUT", "DELETE", "OPTIONS"):
            with self.subTest(method=method):
                self.assertFalse(self.owner(method=method))
        self.assertFalse(self.owner(upgrade="websocket"))
        self.assertFalse(self.owner(upgrade=""))

    def test_an_origin_present_must_be_the_huds_own(self):
        hud = f"http://127.0.0.1:{FACE}"
        for method in ("GET", "POST", "PATCH", "DELETE"):
            with self.subTest(method=method):
                self.assertTrue(self.owner(method=method, origin=hud, site=None))
                self.assertTrue(self.owner(method=method, origin=hud, upgrade="websocket"))
        for origin in ("null", "", f"http://localhost:{FACE}", f"http://127.0.0.1:{FACE + 1}", "http://evil.example"):
            with self.subTest(origin=origin):
                # A wrong Origin is refused even with same-origin fetch metadata beside it.
                self.assertFalse(self.owner(origin=origin))


class ByHand(Base):
    """What a browser page cannot send, but the gate must still refuse."""

    def test_a_same_origin_read_is_the_owners(self):
        status, body = self.raw("GET", "/terminals", {"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 200, body[:200])
        self.assertEqual(json.loads(body), [])
        self.assertEqual(self.raw("GET", "/terminals", {"Host": f"localhost:{self.face}",
                                                        "Sec-Fetch-Site": "same-origin"})[0], 200)
        self.assertEqual(self.raw("GET", "/terminals", {"Origin": f"http://127.0.0.1:{self.face}"})[0], 200)

    def test_a_read_with_no_origin_and_any_other_fetch_site_is_refused(self):
        for site in ("same-site", "cross-site", "none", None, "", "Same-Origin", "same-origin, cross-site"):
            with self.subTest(site=site):
                headers = {} if site is None else {"Sec-Fetch-Site": site}
                status, body = self.raw("GET", "/terminals", headers)
                self.assertEqual(status, 403, body[:200])

    def test_a_non_loopback_host_is_refused(self):
        for host in (f"evil.example:{self.face}", f"127.0.0.1.nip.io:{self.face}", "127.0.0.1",
                     f"127.0.0.1:{self.face + 1}", f"[::1]:{self.face}"):
            with self.subTest(host=host):
                status, body = self.raw("GET", "/terminals", {"Host": host, "Sec-Fetch-Site": "same-origin"})
                self.assertEqual(status, 403, body[:200])

    def test_a_write_with_no_origin_is_refused_whatever_it_is_marked(self):
        for method, path, body in (("POST", "/terminals", {"in": "nowhere"}),
                                   ("POST", "/terminals/0a0b0c0d/ticket", {}),
                                   ("PATCH", "/terminals/0a0b0c0d", {"readable": False}),
                                   ("DELETE", "/terminals/0a0b0c0d", None)):
            for site in ("same-origin", None):
                with self.subTest(method=method, path=path, site=site):
                    headers = {} if site is None else {"Sec-Fetch-Site": site}
                    status, raw = self.raw(method, path, headers, body=body)
                    self.assertEqual(status, 403, raw[:200])
        # The same POST with the HUD's Origin passes the gate (and is then a bad body).
        status, raw = self.raw("POST", "/terminals", {"Origin": f"http://127.0.0.1:{self.face}"},
                               body={"in": "nowhere"})
        self.assertEqual(status, 400, raw[:200])

    def test_origin_null_or_empty_is_never_the_owner(self):
        for origin in ("null", ""):
            with self.subTest(origin=origin):
                status, body = self.raw("GET", "/terminals", {"Origin": origin, "Sec-Fetch-Site": "same-origin"})
                self.assertEqual(status, 403, body[:200])

    def test_a_socket_with_no_origin_is_refused_even_marked_same_origin(self):
        headers = {"Upgrade": "websocket", "Connection": "Upgrade", "Sec-WebSocket-Version": "13",
                   "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==", "Sec-Fetch-Site": "same-origin"}
        status, body = self.raw("GET", "/terminals/0a0b0c0d/attach?ticket=x", headers)
        self.assertEqual(status, 403, body[:200])

    def test_the_api_listener_is_never_the_owner(self):
        api = self.daemon.port
        for headers in ({"Sec-Fetch-Site": "same-origin"}, {"Origin": f"http://127.0.0.1:{api}"}):
            with self.subTest(headers=headers):
                status, body = self.raw("GET", "/terminals", headers, port=api)
                self.assertEqual(status, 403, body[:200])


class Page(http.server.BaseHTTPRequestHandler):
    """A page on another ephemeral loopback port: same site, another origin."""

    def do_GET(self):
        data = b"<!doctype html><title>elsewhere</title><p>another origin</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@unittest.skipIf(sync_playwright is None, "Playwright is not installed: the browser half did not run")
class InChromium(Base):
    """What a real browser sends, read where the daemon receives it: every
    request passes `hud_api.check_origin` first, and a recording wrapper
    there keeps the headers exactly as they arrived (Playwright's own view of
    a request's headers leaves out the ones the network stack adds, the
    `Sec-Fetch-*` among them)."""

    def setUp(self):
        super().setUp()
        from jarvis.v2 import hud_api
        real = hud_api.check_origin
        self.received: list[tuple[str, str, dict]] = []

        def recording(handler, **kw):
            self.received.append((handler.command, handler.path,
                                  {k.lower(): v for k, v in handler.headers.items()}))
            return real(handler, **kw)
        guard = patch.object(hud_api, "check_origin", recording)
        guard.start()
        self.addCleanup(guard.stop)
        self.pw = sync_playwright().start()
        self.addCleanup(self.pw.stop)
        self.browser = self.pw.chromium.launch(headless=True)
        self.addCleanup(self.browser.close)
        self.ctx = self.browser.new_context()
        self.addCleanup(self.ctx.close)
        self.live_hits: list[str] = []

        def refuse(route):
            self.live_hits.append(route.request.url)
            route.abort()
        self.ctx.route(LIVE, refuse)
        self.addCleanup(lambda: self.assertEqual(self.live_hits, [], "a request reached a live port"))
        self.page = self.ctx.new_page()

    def requests_to(self, path: str, method: str = "GET") -> list[dict]:
        """The headers of each request for `path` the daemon received, as they arrived."""
        return [h for (m, p, h) in self.received if m == method and p.split("?", 1)[0] == path]

    def test_a_page_loaded_from_the_hud_listener_reads_the_listing(self):
        self.page.goto(f"http://127.0.0.1:{self.face}/status")
        got = self.page.evaluate("""async () => {
          const r = await fetch('/terminals');
          return {status: r.status, body: await r.text()};
        }""")
        sent = self.requests_to("/terminals")
        self.assertTrue(sent, "the browser sent no listing request")
        self.assertNotIn("origin", sent[-1], "setup: Chromium sent an Origin on a same-origin GET after all")
        self.assertEqual(sent[-1].get("sec-fetch-site"), "same-origin", str(sent[-1]))
        self.assertEqual(got["status"], 200, got["body"][:200])
        self.assertEqual(json.loads(got["body"]), [])

    def test_a_post_from_it_still_needs_its_origin_which_the_browser_sends(self):
        self.page.goto(f"http://127.0.0.1:{self.face}/status")
        got = self.page.evaluate("""async () => {
          const r = await fetch('/terminals', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                               body: JSON.stringify({in: 'nowhere'})});
          return {status: r.status, body: await r.text()};
        }""")
        sent = self.requests_to("/terminals", "POST")
        self.assertTrue(sent, "the browser sent no POST")
        self.assertEqual(sent[-1].get("origin"), f"http://127.0.0.1:{self.face}", str(sent[-1]))
        # Past the owner gate (a 403 would say it refused), refused as a bad body.
        self.assertEqual(got["status"], 400, got["body"][:200])

    def test_the_huds_own_voice_requests_pass_the_gate(self):
        """Voice follow-ups, V2 row: `/stt`, `/say`, `/voices`, `/voice` and `/mute` are owner-only.
        The HUD's real requests, as Chromium sends them from its own page, still pass; the same
        requests from a page on another port reach no voice at all."""
        from jarvis import speaker, voice
        from jarvis.tools import voicectl
        listed = [{"name": "test", "backend": "fake", "kind": "builtin"}]
        heard, said, muted = [], [], []
        fakes = [patch.object(voice, "stt_result", lambda data, **kw: heard.append(data) or
                              voice.SttResult("dictated", "cloud", "m/x")),
                 patch.object(speaker, "verify", lambda data, mime: "skipped_unenrolled"),
                 patch.object(voice, "tts", lambda text, **kw: said.append(text) or b"RIFFsaid"),
                 patch.object(voice, "catalog", lambda: listed),
                 patch.object(voice, "_known", lambda name: True),
                 patch.object(voicectl, "set_muted", lambda on: muted.append(on))]
        for fake in fakes:
            fake.start()
            self.addCleanup(fake.stop)
        script = """async () => {
          const out = {};
          const ask = async (name, path, init) => {
            try { const r = await fetch(path, init); out[name] = {status: r.status, body: await r.text()}; }
            catch (e) { out[name] = {error: e.name}; }
          };
          const json = (b) => ({method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(b)});
          await ask('voices', BASE + '/voices');
          await ask('say', BASE + '/say', json({text: 'hello', local_only: true}));
          await ask('voice', BASE + '/voice', json({voice: 'test'}));
          await ask('mute', BASE + '/mute', json({muted: false}));
          await ask('stt', BASE + '/stt?seq=1', {method: 'POST', headers: {'Content-Type': 'audio/wav'},
                                                  body: new Blob([new Uint8Array([82, 73, 70, 70, 1, 2])])});
          return out;
        }"""
        self.page.goto(f"http://127.0.0.1:{self.face}/status")
        got = self.page.evaluate(script.replace("BASE", "''"))
        for name in ("voices", "say", "voice", "mute", "stt"):
            with self.subTest(name=name):
                self.assertEqual(got[name].get("status"), 200, str(got[name])[:300])
        self.assertEqual(json.loads(got["voices"]["body"])["voices"], listed)
        self.assertEqual(json.loads(got["stt"]["body"])["text"], "dictated")
        self.assertNotIn("origin", self.requests_to("/voices")[-1], "the HUD's read carried no Origin")
        self.assertEqual(self.requests_to("/voices")[-1].get("sec-fetch-site"), "same-origin")
        self.assertEqual((len(heard), said, muted), (1, ["hello"], [False]))

        other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
        other.daemon_threads = True
        self.assertNotIn(other.server_address[1], LIVE_PORTS)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        self.addCleanup(other.server_close)
        self.addCleanup(other.shutdown)
        self.page.goto(f"http://127.0.0.1:{other.server_address[1]}/")
        got = self.page.evaluate(script.replace("BASE", repr(f"http://127.0.0.1:{self.face}")))
        for name in ("voices", "say", "voice", "mute", "stt"):
            with self.subTest(other=name):
                self.assertEqual(got[name], {"error": "TypeError"}, str(got[name])[:300])
        self.assertEqual((len(heard), said, muted), (1, ["hello"], [False]), "a foreign page reached the voice")

    def test_a_page_on_another_port_cannot_read_the_listing(self):
        other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
        other.daemon_threads = True
        self.assertNotIn(other.server_address[1], LIVE_PORTS)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        self.addCleanup(other.server_close)
        self.addCleanup(other.shutdown)
        self.page.goto(f"http://127.0.0.1:{other.server_address[1]}/")
        statuses: list[int] = []
        self.page.on("response", lambda r: statuses.append(r.status) if "/terminals" in r.url else None)
        url = f"http://127.0.0.1:{self.face}/terminals"
        got = self.page.evaluate("""async (url) => {
          const out = {};
          try { const r = await fetch(url); out.cors = {status: r.status, body: await r.text()}; }
          catch (e) { out.cors = {error: e.name}; }
          try { const r = await fetch(url, {mode: 'no-cors'}); out.opaque = {type: r.type, status: r.status,
                                                                             body: await r.text()}; }
          catch (e) { out.opaque = {error: e.name}; }
          return out;
        }""", url)
        self.assertEqual(got["cors"], {"error": "TypeError"}, str(got))
        self.assertEqual(got["opaque"], {"type": "opaque", "status": 0, "body": ""}, str(got))
        sent = self.requests_to("/terminals")
        self.assertTrue(sent, "the browser sent no request to the HUD listener")
        self.assertIn(sent[0].get("sec-fetch-site"), ("same-site", "cross-site"), str(sent[0]))
        self.assertTrue(statuses and all(s == 403 for s in statuses), str(statuses))


if __name__ == "__main__":
    unittest.main(verbosity=2)
