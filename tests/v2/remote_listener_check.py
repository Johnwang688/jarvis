"""The phone's remote listener (phone surface design PH-1, work package P-1).

Free and hermetic. Every daemon here listens on ephemeral loopback ports,
never 8402, 8403, 8405 or 8408 (the owner's daemon holds the first three and
a spike server may hold 8408); HOME, the data folder and the HUD build are
temporary. What it pins, each acceptance item of §10's P-1 row and the
package's brief:

  - **route tables** (`RouteTables`): the API, HUD and workshop listeners
    never serve `/m/*`, by any method; the remote listener never serves a
    HUD, API, inbox, approval, workshop or preview route: every route prefix
    named in `jarvis/v2`'s source, every HUD file, and the build's other
    files under `/m/` too, is a 404 there;
  - **strict Host**: anything but exactly the configured ts.net name is 400;
  - **writes**: any method but GET/HEAD without exactly
    `Origin: https://<name>` is 403;
  - `/m/api/*` is 401 with no detail;
  - the **CSP and headers** on every response, a malformed request's too;
  - **no listener** without `JARVIS_REMOTE_HOST` (or with a malformed one);
  - a **port collision** skips the listener and the daemon still starts;
  - **bound to 127.0.0.1** only;
  - `is_owner` is **false** for every request on the remote listener;
  - the shell, its manifest and icons, from the working tree's build and
    from an installed one (`hud_install`), and only the phone's own files;
  - 8408 is a **control port** in Python and in `hud/src/lib/devopen.ts`.

Run: PYTHONPATH=. .venv/bin/python tests/v2/remote_listener_check.py
"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from jarvis import config  # noqa: E402
from jarvis.v2 import hud_install, projects, remote_listener  # noqa: E402
from jarvis.v2.daemon import Daemon  # noqa: E402
from jarvis.v2.model import ProviderName as PN  # noqa: E402
from jarvis.v2.provider import Decision, SessionHandle, Usage  # noqa: E402
from jarvis.v2.stores import Stores  # noqa: E402

HOST = "jarvis-box.tail-test.ts.net"
ORIGIN = "https://" + HOST
LIVE_PORTS = {8402, 8403, 8405, 8408}
WRITES = ("POST", "PUT", "PATCH", "DELETE", "OPTIONS")
METHODS = ("GET", "HEAD") + WRITES

PAGE = ("<!doctype html><title>J.A.R.V.I.S.</title>"
        '<script type="module" crossorigin src="/m/assets/modulepreload-polyfill-PPPP.js"></script>'
        '<script type="module" crossorigin src="/m/assets/mobile-AAAA.js"></script>'
        '<link rel="stylesheet" crossorigin href="/m/assets/mobile-CCCC.css">'
        "<p>Jarvis · phone, not paired yet</p>")
BUILD_MANIFEST = {
    "_modulepreload-polyfill-PPPP.js": {"file": "assets/modulepreload-polyfill-PPPP.js"},
    "_shared-SSSS.js": {"file": "assets/shared-SSSS.js", "css": ["assets/shared-TTTT.css"]},
    "src/lazy.ts": {"file": "assets/lazy-LLLL.js", "assets": ["assets/font-FFFF.woff2"]},
    "mobile.html": {"file": "assets/mobile-AAAA.js", "src": "mobile.html", "isEntry": True,
                    "imports": ["_shared-SSSS.js"], "dynamicImports": ["src/lazy.ts"],
                    "css": ["assets/mobile-CCCC.css"]},
    "index.html": {"file": "assets/index-IIII.js", "src": "index.html", "isEntry": True,
                   "imports": ["_modulepreload-polyfill-PPPP.js", "_shared-SSSS.js"],
                   "css": ["assets/index-JJJJ.css"], "dynamicImports": ["src/editor.ts"]},
    "src/editor.ts": {"file": "assets/editor-EEEE.js"},
}
PHONE_FILES = {"assets/modulepreload-polyfill-PPPP.js", "assets/mobile-AAAA.js",
               "assets/mobile-CCCC.css", "assets/shared-SSSS.js", "assets/shared-TTTT.css",
               "assets/lazy-LLLL.js", "assets/font-FFFF.woff2"}
HUD_ONLY = {"assets/index-IIII.js", "assets/index-JJJJ.css", "assets/editor-EEEE.js"}


def make_dist(root: Path) -> Path:
    """A built HUD's shape: both pages, Vite's manifest, the HUD's own
    manifest and icons, and each listed file with recognisable bytes."""
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "icons").mkdir()
    (dist / "index.html").write_text("<!doctype html><title>J.A.R.V.I.S.</title>HUD-PAGE")
    (dist / "mobile.html").write_text(PAGE)
    (dist / "build-manifest.json").write_text(json.dumps(BUILD_MANIFEST))
    (dist / "manifest.webmanifest").write_text('{"scope": "/"}')
    for rel in PHONE_FILES | HUD_ONLY:
        (dist / rel).write_text(f"/* {rel} */")
    for name in remote_listener.ICONS:
        (dist / "icons" / name).write_bytes(b"\x89PNG-" + name.encode())
    return dist


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
        return iter(())

    def interrupt(self, handle):
        pass

    def close(self, handle):
        pass


def raw(port, method, path, headers=(), body=None, timeout=5):
    """One request with exactly these headers, in this order (no Host is
    added): returns (status, headers, body)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for name, value in headers:
            conn.putheader(name, value)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        response = conn.getresponse()
        data = response.read() if method != "HEAD" else b""
        return response.status, response.headers, data
    finally:
        conn.close()


def send_bytes(port, data: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(data)
        out = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                return out
            out += chunk


def route_prefixes() -> set[str]:
    """Every first path segment a route in `jarvis/v2` compares against."""
    found: set[str] = set()
    for path in (ROOT / "jarvis" / "v2").rglob("*.py"):
        if path.name == "remote_listener.py":
            continue
        text = path.read_text(encoding="utf-8")
        found.update(re.findall(r'parts\[0\]\s*[!=]=\s*"([A-Za-z0-9_.-]+)"', text))
        for group in re.findall(r'"/"\.join\(parts\)\s*in\s*\(([^)]*)\)', text):
            found.update(n.split("/")[0] for n in re.findall(r'"([A-Za-z0-9_./-]+)"', group))
        found.update(re.findall(r'parts\s*==\s*\["([A-Za-z0-9_.-]+)"', text))
        found.update(re.findall(r'parts\[:\d\]\s*==\s*\["([A-Za-z0-9_.-]+)"', text))
        for group in re.findall(r'parts\[0\]\s*(?:not\s+)?in\s*\(([^)]*)\)', text):
            found.update(re.findall(r'"([A-Za-z0-9_.-]+)"', group))
    found.discard("m")
    return found


class Base(unittest.TestCase):
    remote_host: str | None = HOST

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        home = self.root / "home"
        home.mkdir()
        env = {k: v for k, v in os.environ.items() if k != "JARVIS_REMOTE_HOST"}
        env.update({"HOME": str(home), "XDG_DATA_HOME": str(self.root / "xdg"),
                    "JARVIS_CLAUDE_CREDENTIALS": str(self.root / "none.json")})
        guard = patch.dict(os.environ, env, clear=True)
        guard.start()
        self.addCleanup(guard.stop)
        self.dist = make_dist(self.root)
        for name, value in dict(V2_DATA_DIR=self.root / "data", ALLOWLIST_PATH=self.root / "allow.json",
                                MODELS_PATH=self.root / "models.json",
                                PROVIDER_DEFAULTS_PATH=self.root / "provider_defaults.json",
                                DISCORD_GUILD_PATH=self.root / "guild.json",
                                HUD_DEV=True, HUD_DIST=self.dist).items():
            guard = patch.object(config, name, value)
            guard.start()
            self.addCleanup(guard.stop)

    def daemon(self, **kw):
        kw.setdefault("remote_host", self.remote_host)
        daemon = Daemon(Stores(), {name: Fake(name) for name in PN},
                        lambda *_: lambda *_: Decision.DENY, 0, **kw)
        daemon.start()
        self.addCleanup(daemon.stop)
        ports = {daemon.port, daemon.face_port, daemon.workshop_port, daemon.remote_port}
        self.assertFalse(ports & LIVE_PORTS, "a test daemon must never hold a live port")
        return daemon

    def get(self, path, *, method="GET", host=HOST, origin=None, extra=()):
        headers = [("Host", host)] if host is not None else []
        if origin is not None:
            headers.append(("Origin", origin))
        return raw(self.port, method, path, headers + list(extra))


class Remote(Base):
    def setUp(self):
        super().setUp()
        self.d = self.daemon()
        self.port = self.d.remote_port
        self.assertIsInstance(self.port, int)


class Config(unittest.TestCase):
    def test_remote_host_names(self):
        good = ["box.tailnet.ts.net", "my-pc.tail1234.ts.net", "a.b.c.ts.net", HOST]
        bad = ["", "localhost", "127.0.0.1", "box.ts.net", "ts.net", "Box.tailnet.ts.net",
               "box.tailnet.ts.net.", "box.tailnet.ts.net:443", " box.tailnet.ts.net",
               "box.tailnet.ts.net ", "box.tailnet.ts.net.evil.com", "box.tailnet.evil.net",
               "-box.tailnet.ts.net", "box..ts.net", "box.tailnet.ts.net/", "box_x.tailnet.ts.net",
               "box.tailnet.ts.net\n", "localhost.ts.net", "*.tailnet.ts.net"]
        for name in good:
            self.assertEqual(config.remote_host({"JARVIS_REMOTE_HOST": name}), name, name)
        for name in bad:
            self.assertIsNone(config.remote_host({"JARVIS_REMOTE_HOST": name}), repr(name))
        self.assertIsNone(config.remote_host({}))
        with patch.dict(os.environ, {"JARVIS_REMOTE_HOST": "box.tailnet.ts.net"}):
            self.assertEqual(config.remote_host(), "box.tailnet.ts.net")
        print("ok  config: only a well-formed <machine>.<tailnet>.ts.net name is a remote host")

    def test_control_ports(self):
        if "JARVIS_REMOTE_PORT" not in os.environ:
            self.assertEqual(config.REMOTE_PORT, 8408)
        self.assertIn(8408, config.DEFAULT_CONTROL_PORTS)
        self.assertIn(8408, config.control_ports())
        with patch.object(config, "REMOTE_PORT", 18408):
            self.assertTrue({8408, 18408} <= config.control_ports())
        # devopen.ts mirrors DEFAULT_CONTROL_PORTS (FACE_PORT 8402, API_PORT 8405).
        ts = (ROOT / "hud" / "src" / "lib" / "devopen.ts").read_text(encoding="utf-8")
        match = re.search(r"export const CONTROL_PORTS: readonly number\[\] = \[([^\]]*)\];", ts)
        self.assertIsNotNone(match)
        names = {"FACE_PORT": 8402, "API_PORT": 8405}
        listed = {names[t] if t in names else int(t) for t in (x.strip() for x in match.group(1).split(","))}
        self.assertEqual(listed, set(config.DEFAULT_CONTROL_PORTS))
        from jarvis.v2.browser.policy import refused_ports
        self.assertIn(8408, set(refused_ports()))
        print("ok  ports: 8408 is a control port in config, control_ports(), the Browser gate and devopen.ts")


class Binding(Base):
    def test_no_listener_without_a_host(self):
        d = self.daemon(remote_host=None)
        self.assertIsNone(d.remote_port)
        self.assertEqual(len(d._listeners), 3)
        self.assertFalse(any(getattr(s, "remote_only", False) for s, _ in d._listeners))
        print("ok  no remote listener when no host is given")

    def test_host_comes_from_the_environment(self):
        d = Daemon(Stores(), {name: Fake(name) for name in PN}, lambda *_: lambda *_: Decision.DENY, 0)
        self.assertIsNone(d.remote_host)            # setUp removed JARVIS_REMOTE_HOST
        d.start()
        self.addCleanup(d.stop)
        self.assertIsNone(d.remote_port)
        self.assertEqual(len(d._listeners), 3)
        for value in ("localhost", "127.0.0.1", "box.tailnet.ts.net:443", ""):
            with patch.dict(os.environ, {"JARVIS_REMOTE_HOST": value}):
                d2 = Daemon(Stores(), {}, None, 0)
                self.assertIsNone(d2.remote_host, value)
        with patch.dict(os.environ, {"JARVIS_REMOTE_HOST": HOST}):
            d3 = Daemon(Stores(), {name: Fake(name) for name in PN},
                        lambda *_: lambda *_: Decision.DENY, 0)
            self.assertEqual(d3.remote_host, HOST)
            d3.start()
            self.addCleanup(d3.stop)
            self.assertIsInstance(d3.remote_port, int)
            self.assertNotIn(d3.remote_port, LIVE_PORTS)
            self.assertEqual(len(d3._listeners), 4)
        print("ok  JARVIS_REMOTE_HOST: unset or malformed binds nothing; a ts.net name binds")

    def test_port_collision_skips_and_the_daemon_runs(self):
        taken = socket.socket()
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        self.addCleanup(taken.close)
        port = taken.getsockname()[1]
        self.assertNotIn(port, LIVE_PORTS)
        with self.assertLogs("jarvis.v2.remote_listener", "WARNING") as logs:
            d = self.daemon(remote_port=port)
        self.assertIsNone(d.remote_port)
        self.assertEqual(len(d._listeners), 3)
        self.assertTrue(any("not started" in line for line in logs.output), logs.output)
        self.assertNotIn(HOST, "\n".join(logs.output))
        status, _, body = raw(d.port, "GET", "/status", [("Host", f"127.0.0.1:{d.port}")])
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["jarvis-daemon"])
        # The port's owner still answers, not us.
        self.assertEqual(taken.getsockname()[1], port)
        print("ok  a taken remote port is logged and skipped; the daemon starts and serves")

    def test_bound_to_loopback_only(self):
        d = self.daemon()
        server = next(s for s, _ in d._listeners if getattr(s, "remote_only", False))
        self.assertEqual(server.server_address[0], "127.0.0.1")
        self.assertEqual(server.socket.getsockname()[0], "127.0.0.1")
        self.assertEqual(server.socket.family, socket.AF_INET)
        other = None
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("192.0.2.1", 9))        # TEST-NET: no packet is sent
            other = probe.getsockname()[0]
            probe.close()
        except OSError:
            pass
        if other and not other.startswith("127."):
            with self.assertRaises(OSError):
                socket.create_connection((other, d.remote_port), timeout=2).close()
            print(f"ok  bound to 127.0.0.1 only (refused on {other})")
        else:
            print("ok  bound to 127.0.0.1 only (no other interface to probe)")


class HostAndOrigin(Remote):
    def test_strict_host(self):
        status, headers, body = self.get("/m/")
        self.assertEqual(status, 200)
        bad = [f"127.0.0.1:{self.port}", f"localhost:{self.port}", HOST.upper(), HOST + ":443",
               HOST + ".", "evil.tail-test.ts.net", "x" + HOST, HOST + ".evil.com", "",
               HOST + "\\", HOST + "/", "[::1]", "127.0.0.1"]
        # (Leading and trailing blanks are not part of a header's value in
        # HTTP, and http.server strips them, so " <name>" is the name.)
        for host in bad:
            status, headers, body = self.get("/m/", host=host)
            self.assertEqual(status, 400, host)
            self.assertEqual(json.loads(body), {"error": "untrusted host"})
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        status, _, _ = self.get("/m/", host=None)
        self.assertEqual(status, 400)
        status, _, _ = raw(self.port, "GET", "/m/", [("Host", HOST), ("Host", HOST)])
        self.assertEqual(status, 400)
        status, _, _ = raw(self.port, "GET", "/m/api/inbox", [("Host", "localhost")])
        self.assertEqual(status, 400)
        print("ok  strict Host: exactly the configured name, once; everything else 400")

    def test_bare_address_redirects_to_the_app(self):
        for method in ("GET", "HEAD"):
            status, headers, body = self.get("/", method=method)
            self.assertEqual(status, 302, method)
            self.assertEqual(headers.get_all("Location"), ["/m/"])
            self.assertEqual(body, b"")
            self.assertEqual(headers["Content-Length"], "0")
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            # Only after the strict Host check: a wrong or missing Host is 400.
            for host in ("localhost", f"127.0.0.1:{self.port}", HOST.upper(), HOST + ":443", None):
                status, headers, body = self.get("/", method=method, host=host)
                self.assertEqual(status, 400, (method, host))
                self.assertNotIn("Location", headers)
        status, _, _ = raw(self.port, "GET", "/", [("Host", HOST), ("Host", HOST)])
        self.assertEqual(status, 400)
        # No other path gains it: the exact target "/" only.
        for path in ("/x", "/index.html", "/?x=1", "/#a", "/m", "/M/", "/m/nothing", "/.", "/%2f"):
            for method in ("GET", "HEAD"):
                status, headers, _ = self.get(path, method=method)
                self.assertNotIn("Location", headers, (method, path))
                self.assertNotEqual(status, 302, (method, path))
        # (http.server itself collapses a leading "//" to "/" before any of
        # our code runs: the same relative redirect, to the same place.)
        self.assertEqual(self.get("//")[0], 302)
        self.assertEqual(self.get("/x")[0], 404)
        status, headers, body = self.get("/x")
        self.assertEqual(json.loads(body), {"error": "not found"})
        # Writes are unchanged: no Origin 403; with it, 404 and no Location.
        for method in WRITES:
            self.assertEqual(self.get("/", method=method)[0], 403, method)
            status, headers, _ = self.get("/", method=method, origin=ORIGIN)
            self.assertEqual(status, 404, method)
            self.assertNotIn("Location", headers)
        status, headers, _ = raw(self.port, "POST", "/", [("Host", HOST), ("Origin", ORIGIN)], body=b"{}")
        self.assertEqual(status, 404)
        self.assertNotIn("Location", headers)
        print("ok  GET/HEAD / is 302 /m/ (relative, no body) behind the strict Host; "
              "other paths and writes unchanged")

    def test_writes_need_the_exact_origin(self):
        bad = [None, "http://" + HOST, ORIGIN + ":443", ORIGIN + "/", "null", "https://evil.ts.net",
               f"http://127.0.0.1:{self.port}", "", ORIGIN.upper()]
        for method in WRITES:
            for path in ("/m/", "/m/api/inbox", "/m/manifest.webmanifest", "/status"):
                for origin in bad:
                    status, headers, _ = self.get(path, method=method, origin=origin)
                    self.assertEqual(status, 403, (method, path, origin))
                    self.assertEqual(headers["X-Frame-Options"], "DENY")
                status, _, _ = raw(self.port, method, path,
                                   [("Host", HOST), ("Origin", ORIGIN), ("Origin", ORIGIN)])
                self.assertEqual(status, 403, (method, path, "two Origins"))
            status, _, _ = self.get("/m/api/inbox", method=method, origin=ORIGIN)
            self.assertEqual(status, 401, method)
            status, _, _ = self.get("/m/", method=method, origin=ORIGIN)
            self.assertEqual(status, 404, method)
        # The body is read first (tests/v2/remote_auth_check.py `Bodies`): still the same answers.
        status, _, _ = raw(self.port, "POST", "/m/api/inbox", [("Host", HOST)], body=b"{}")
        self.assertEqual(status, 403)
        status, _, _ = raw(self.port, "POST", "/m/api/inbox", [("Host", HOST), ("Origin", ORIGIN)],
                           body=b'{"answer": "allow"}')
        self.assertEqual(status, 401)
        print("ok  writes: without exactly Origin: https://<name> 403; with it, still nothing to write")

    def test_api_is_401_with_no_detail(self):
        for path in ("/m/api", "/m/api/", "/m/api/inbox", "/m/api/inbox/x/answer",
                     "/m/api/threads?project=p", "/m/api/../api/x", "/m/api/%2e%2e/"):
            for method in ("GET", "HEAD"):
                status, headers, body = self.get(path, method=method)
                self.assertEqual(status, 401, (method, path))
                if method == "GET":
                    self.assertEqual(json.loads(body), {"error": "unauthorized"})
                self.assertNotIn("Set-Cookie", headers)
                self.assertEqual(headers["Cache-Control"], "no-store")
        print("ok  /m/api/*: 401 and nothing more, every method")


class Headers(Remote):
    def check(self, headers, cache="no-store"):
        csp = headers["Content-Security-Policy"]
        self.assertEqual(csp, remote_listener.REMOTE_POLICY)
        directives = {d.split()[0]: d.split()[1:] for d in csp.split("; ")}
        self.assertEqual(directives["frame-ancestors"], ["'none'"])
        self.assertEqual(directives["default-src"], ["'none'"])
        self.assertEqual(directives["script-src"], ["'self'"])
        self.assertEqual(directives["object-src"], ["'none'"])
        self.assertEqual(directives["base-uri"], ["'none'"])
        self.assertNotIn("unsafe", csp)
        self.assertNotIn(",", csp)
        self.assertEqual(headers.get_all("Content-Security-Policy"), [csp])
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["Cross-Origin-Opener-Policy"], "same-origin")
        self.assertEqual(headers["Cross-Origin-Resource-Policy"], "same-origin")
        self.assertEqual(headers.get_all("Cache-Control"), [cache])
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_every_response_carries_them(self):
        for path, want, cache in (("/m/", 200, "no-store"), ("/m/manifest.webmanifest", 200, "no-store"),
                                  ("/m/icons/orb-192.png", 200, "no-store"),
                                  ("/m/assets/mobile-AAAA.js", 200, remote_listener.IMMUTABLE),
                                  ("/m/nothing", 404, "no-store"), ("/", 302, "no-store"),
                                  ("/m/api/inbox", 401, "no-store")):
            for method in ("GET", "HEAD"):
                status, headers, _ = self.get(path, method=method)
                self.assertEqual(status, want, path)
                self.check(headers, cache)
        status, headers, _ = self.get("/m/", host="localhost")
        self.assertEqual(status, 400)
        self.check(headers)
        status, headers, _ = self.get("/m/", method="POST")
        self.assertEqual(status, 403)
        self.check(headers)
        # A request line http.server cannot parse still gets every header.
        for line in (b"GARBAGE\r\n\r\n", b"GET /m/\r\n\r\n", b"GET /m/ HTTP/9.9\r\nHost: " + HOST.encode() + b"\r\n\r\n",
                     b"BREW /m/ HTTP/1.1\r\nHost: " + HOST.encode() + b"\r\n\r\n"):
            answer = send_bytes(self.port, line)
            head = answer.split(b"\r\n\r\n", 1)[0].decode("latin-1")
            self.assertTrue(head.startswith("HTTP/1."), head[:40])
            self.assertIn("Content-Security-Policy: " + remote_listener.REMOTE_POLICY, head)
            self.assertIn("X-Frame-Options: DENY", head)
            self.assertIn("X-Content-Type-Options: nosniff", head)
        # An HTTP/0.9 request line carries no Host: 400, never a 500 or a
        # warning (review of PR #304).
        with self.assertNoLogs("jarvis.v2.remote_listener", "WARNING"):
            answer = send_bytes(self.port, b"GET /m/\r\n\r\n")
        self.assertRegex(answer.split(b"\r\n", 1)[0].decode("latin-1"), r"^HTTP/1\.\d 400 ")
        self.assertNotIn(b"not paired", answer)
        print("ok  headers: the CSP (frame-ancestors 'none'), XFO, nosniff, no-referrer and "
              "Cache-Control on every response, errors and malformed requests included")

    def test_content_types(self):
        expect = {"/m/": "text/html; charset=utf-8", "/m/manifest.webmanifest": "application/manifest+json",
                  "/m/icons/orb-512.png": "image/png", "/m/assets/mobile-AAAA.js": "text/javascript; charset=utf-8",
                  "/m/assets/mobile-CCCC.css": "text/css; charset=utf-8", "/m/assets/font-FFFF.woff2": "font/woff2",
                  "/m/api/x": "application/json", "/m/x": "application/json"}
        for path, mime in expect.items():
            _, headers, _ = self.get(path)
            self.assertEqual(headers["Content-Type"], mime, path)
        print("ok  content types are explicit")


class Shell(Remote):
    def test_shell_manifest_and_icons(self):
        status, headers, body = self.get("/m/")
        self.assertEqual(status, 200)
        self.assertIn("Jarvis · phone, not paired yet", body.decode())
        # `/m/pair` is the same page: where a pairing QR code lands (P-2).
        status, _, pair = self.get("/m/pair")
        self.assertEqual((status, pair), (200, body))
        self.assertEqual(self.get("/m/pair/")[0], 404)
        status, headers, body = self.get("/m/", method="HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(int(headers["Content-Length"]), len(PAGE.encode()))
        status, _, body = self.get("/m/?source=pwa")
        self.assertEqual(status, 200)
        status, _, body = self.get("/m/manifest.webmanifest")
        self.assertEqual(status, 200)
        manifest = json.loads(body)
        self.assertEqual((manifest["scope"], manifest["start_url"], manifest["id"]), ("/m/", "/m/", "/m/"))
        self.assertEqual(manifest["display"], "standalone")
        self.assertTrue(manifest["icons"])
        for icon in manifest["icons"]:
            self.assertTrue(icon["src"].startswith("/m/icons/orb-"), icon)
            status, headers, data = self.get(icon["src"])
            self.assertEqual((status, headers["Content-Type"]), (200, "image/png"), icon)
            self.assertTrue(data.startswith(b"\x89PNG"))
        print("ok  shell: GET /m/ is the placeholder; the manifest is scoped to /m/ with the orb icons")

    def test_only_the_phone_files(self):
        for rel in sorted(PHONE_FILES):
            status, _, body = self.get("/m/" + rel)
            self.assertEqual(status, 200, rel)
            self.assertEqual(body.decode(), f"/* {rel} */")
        for rel in sorted(HUD_ONLY):
            status, _, _ = self.get("/m/" + rel)
            self.assertEqual(status, 404, rel)
        self.assertEqual(remote_listener.mobile_files(remote_listener._dev_read), frozenset(PHONE_FILES))
        print("ok  /m/assets/: the page, its entry and their imports only; the HUD's own chunks 404")

    def test_no_page_or_manifest_serves_nothing(self):
        (self.dist / "build-manifest.json").unlink()
        status, _, _ = self.get("/m/assets/mobile-AAAA.js")
        self.assertEqual(status, 404)
        (self.dist / "build-manifest.json").write_text("[not json")
        status, _, _ = self.get("/m/assets/mobile-AAAA.js")
        self.assertEqual(status, 404)
        (self.dist / "mobile.html").unlink()
        status, _, body = self.get("/m/")
        self.assertEqual(status, 503)
        self.assertNotIn(b"HUD-PAGE", body)
        print("ok  without the page or Vite's manifest: no phone file is served")


class Installed(Base):
    """Under the owner's daemon (no JARVIS_HUD_DEV) files come from an
    installed build, verified against its manifest on every read."""

    def test_installed_build(self):
        hud_root = self.root / "installed"
        with patch.object(config, "HUD_DEV", False), patch.object(config, "HUD_DIST", hud_root):
            d = self.daemon()
            self.port = d.remote_port
            status, _, _ = raw(self.port, "GET", "/m/", [("Host", HOST)])
            self.assertEqual(status, 503)
            built = hud_install.install(self.dist, hud_root)
            for path, want in (("/m/", 200), ("/m/assets/mobile-AAAA.js", 200),
                               ("/m/icons/orb-192.png", 200), ("/m/assets/index-IIII.js", 404),
                               ("/m/build-manifest.json", 404), ("/m/mobile.html", 404)):
                status, _, _ = raw(self.port, "GET", path, [("Host", HOST)])
                self.assertEqual(status, want, path)
            target = built.path / "assets" / "mobile-AAAA.js"
            os.chmod(built.path / "assets", 0o755)
            os.chmod(target, 0o644)
            target.write_text("/* tampered */")
            with self.assertLogs("jarvis.v2.hud_install", "WARNING"):
                status, _, _ = raw(self.port, "GET", "/m/assets/mobile-AAAA.js", [("Host", HOST)])
            self.assertEqual(status, 404)
        print("ok  installed build: served from hud_install, a tampered file refused")


class RouteTables(Remote):
    def test_remote_serves_no_hud_or_api_route(self):
        prefixes = route_prefixes()
        for known in ("projects", "threads", "tasks", "approvals", "inbox", "browser", "terminals",
                      "status", "events", "mcp", "route", "assets", "icons", "p", "dev-servers",
                      "speaker", "speech", "stt", "say", "avatar.svg", "models", "vex", "fs",
                      "archive", "trash", "discord", "schedules"):
            self.assertIn(known, prefixes, known)
        self.assertGreater(len(prefixes), 30)
        calls = []
        real = projects.is_owner

        def spy(handler, daemon):
            result = real(handler, daemon)
            calls.append((handler.server, result))
            return result

        paths = {"/index.html", "/mobile.html", "/manifest.webmanifest", "/build-manifest.json",
                 "/icons/orb-192.png", "/assets/index-IIII.js", "/assets/mobile-AAAA.js",
                 "/m", "/m/index.html", "/m/mobile.html", "/m/build-manifest.json",
                 "/m/assets/", "/m/assets/index-IIII.js", "/m/assets/index-JJJJ.css",
                 "/m/assets/editor-EEEE.js", "/m/assets/../index.html", "/m/assets/%2e%2e/index.html",
                 "/m/icons/", "/m/icons/../index.html", "/m/icons/favicon.ico", "/m/p/x/index.html",
                 "/M/", "/m//", "/m/./", "/m/assets//mobile-AAAA.js", "/p/x/index.html",
                 "/m/manifest.webmanifest/x", "/m/status", "/m/approvals", "/m/inbox"}
        for prefix in prefixes:
            paths.update({f"/{prefix}", f"/{prefix}/", f"/{prefix}/x", f"/m/{prefix}"})
        with patch.object(projects, "is_owner", spy):
            for path in sorted(paths):
                for method in METHODS:
                    status, _, body = self.get(path, method=method,
                                               origin=ORIGIN if method in WRITES else None)
                    want = 401 if path.startswith("/m/api") else 404
                    self.assertEqual(status, want, (method, path, body[:80]))
                    self.assertNotIn(b"HUD-PAGE", body)
        self.assertFalse([c for c in calls if getattr(c[0], "remote_only", False)],
                         "the remote listener asked is_owner")
        print(f"ok  remote route table: {len(paths)} HUD, API, inbox, workshop and preview paths "
              f"x {len(METHODS)} methods are 404 there; is_owner never asked")

    def test_other_listeners_never_serve_m(self):
        phone = ["/m/", "/m", "/m/manifest.webmanifest", "/m/assets/mobile-AAAA.js",
                 "/m/assets/modulepreload-polyfill-PPPP.js", "/m/icons/orb-192.png", "/m/api/inbox",
                 "/m/api/", "/m/x"]
        for port in (self.d.port, self.d.face_port, self.d.workshop_port):
            for host in (f"127.0.0.1:{port}", f"localhost:{port}"):
                for path in phone:
                    for method in METHODS:
                        headers = [("Host", host)]
                        if method in WRITES:
                            headers += [("Origin", f"http://{host}"), ("Content-Type", "application/json")]
                        status, _, body = raw(port, method, path, headers,
                                              body=b"{}" if method in ("POST", "PUT", "PATCH") else None)
                        self.assertEqual(status, 404, (port, method, path, body[:80]))
                        self.assertNotIn(b"not paired", body)
            # The trusted remote Host does not open them either: that is the
            # HUD's strict Host refusing, before any route.
            status, _, _ = raw(port, "GET", "/m/", [("Host", HOST)])
            self.assertEqual(status, 403)
        # The daemon's own `/m` guard, not the route modules' absence of a
        # match: with every route module answering anything, `/m/*` is still
        # 404 there (review of PR #304; deleting the guard fails this).
        from jarvis.v2 import bridge, hud_api
        leak = lambda *a, **k: (200, {"leak": 1})  # noqa: E731
        with patch.object(hud_api, "route", leak), patch.object(hud_api, "preview_route", leak), \
                patch.object(bridge, "route", leak):
            status, _, body = raw(self.d.port, "GET", "/status", [("Host", f"127.0.0.1:{self.d.port}")])
            self.assertEqual((status, json.loads(body)), (200, {"leak": 1}), "the catch-all is live")
            for port in (self.d.port, self.d.face_port, self.d.workshop_port):
                host = f"127.0.0.1:{port}"
                for path in phone:
                    for method in METHODS:
                        headers = [("Host", host)]
                        if method in WRITES:
                            headers += [("Origin", f"http://{host}"), ("Content-Type", "application/json")]
                        status, _, body = raw(port, method, path, headers,
                                              body=b"{}" if method in ("POST", "PUT", "PATCH") else None)
                        self.assertEqual(status, 404, ("guard", port, method, path, body[:80]))
                        self.assertNotIn(b"leak", body)
        # The bare address redirect is the remote listener's alone.
        for port in (self.d.port, self.d.face_port, self.d.workshop_port):
            for method in ("GET", "HEAD"):
                status, headers, _ = raw(port, method, "/", [("Host", f"127.0.0.1:{port}")])
                self.assertNotEqual(status, 302, (port, method))
                self.assertNotIn("Location", headers, (port, method))
        # Proof the sweep could see a page: the HUD listener does serve its own.
        status, _, body = raw(self.d.face_port, "GET", "/", [("Host", f"127.0.0.1:{self.d.face_port}")])
        self.assertEqual(status, 200)
        self.assertIn(b"HUD-PAGE", body)
        status, _, body = raw(self.d.face_port, "GET", "/mobile.html",
                              [("Host", f"127.0.0.1:{self.d.face_port}")])
        self.assertNotEqual(status, 200)
        print("ok  the API, HUD and workshop listeners answer /m/* with 404, every method")


class Owner(Remote):
    def test_is_owner_false_on_the_remote_listener(self):
        remote = next(s for s, _ in self.d._listeners if getattr(s, "remote_only", False))
        face = next(s for s, _ in self.d._listeners if s.server_address[1] == self.d.face_port)

        def handler(server, method, headers):
            from email.message import Message
            msg = Message()
            for name, value in headers:
                msg[name] = value
            return SimpleNamespace(server=server, command=method, headers=msg)

        for host in (HOST, f"127.0.0.1:{remote.server_address[1]}"):
            shapes = [("POST", [("Host", host), ("Origin", f"http://{host}")]),
                      ("POST", [("Host", host), ("Origin", f"https://{host}")]),
                      ("GET", [("Host", host), ("Sec-Fetch-Site", "same-origin")]),
                      ("GET", [("Host", host)])]
            for method, headers in shapes:
                self.assertFalse(projects.is_owner(handler(remote, method, headers), self.d))
                # Even were the remote port the HUD's, its mark refuses it.
                with patch.object(self.d, "face_port", remote.server_address[1]):
                    self.assertFalse(projects.is_owner(handler(remote, method, headers), self.d))
        # The same owner-shaped request on the HUD listener is the owner's,
        # so the shapes above are the ones that matter.
        host = f"127.0.0.1:{self.d.face_port}"
        self.assertTrue(projects.is_owner(handler(face, "POST", [("Host", host), ("Origin", f"http://{host}")]),
                                          self.d))
        self.assertTrue(remote.remote_only)
        self.assertFalse(getattr(face, "remote_only", False))
        print("ok  is_owner: false for every request shape on the remote listener, by port and by mark")


class RealBuild(Base):
    """The working tree's own build, when it has one (`npm run build`): the
    page's tags all resolve on the remote listener, and no phone file
    points outside /m/."""

    def test_built_shell(self):
        dist = ROOT / "hud" / "dist"
        if not (dist / "mobile.html").is_file() or not (dist / "build-manifest.json").is_file():
            print("skip  real build: hud/dist has no phone shell (run npm run build in hud/)")
            return
        with patch.object(config, "HUD_DIST", dist):
            d = self.daemon()
            port = d.remote_port
            status, _, page = raw(port, "GET", "/m/", [("Host", HOST)])
            self.assertEqual(status, 200)
            text = page.decode()
            self.assertIn("Jarvis · phone", text)
            self.assertIn('id="app"', text)
            refs = re.findall(r'\s(?:src|href)="([^"]+)"', text)
            self.assertTrue(any(r.startswith("/m/assets/") for r in refs), refs)
            for ref in refs:
                self.assertTrue(ref.startswith("/m/"), ref)
                status, _, _ = raw(port, "GET", ref, [("Host", HOST)])
                self.assertEqual(status, 200, ref)
            files = remote_listener.mobile_files(remote_listener._dev_read)
            for rel in files:
                if rel.endswith((".js", ".css")):
                    body = (dist / rel).read_text(encoding="utf-8", errors="replace")
                    self.assertNotRegex(body, r"""["'(]/assets/""", rel)
            manifest = json.loads((dist / "build-manifest.json").read_text())
            hud_entry = manifest["index.html"]["file"]
            self.assertNotIn(hud_entry, files)
            status, _, _ = raw(port, "GET", "/m/" + hud_entry, [("Host", HOST)])
            self.assertEqual(status, 404)
            for icon in json.loads(raw(port, "GET", "/m/manifest.webmanifest", [("Host", HOST)])[2])["icons"]:
                self.assertEqual(raw(port, "GET", icon["src"], [("Host", HOST)])[0], 200, icon)
        print(f"ok  real build: every tag of the built page resolves under /m/ ({len(files)} phone files); "
              "the HUD's entry chunk is not among them")


class Source(unittest.TestCase):
    def test_separate_route_table(self):
        text = (ROOT / "jarvis" / "v2" / "remote_listener.py").read_text(encoding="utf-8")
        for name in ("hud_api", "inbox", "bridge", "projects", "daemon", "approvals"):
            self.assertNotRegex(text, rf"(?m)^\s*(from \.+ import .*\b{name}\b|from \.{name} import|import .*\b{name}\b)",
                                name)
        self.assertTrue(all(k.startswith("/m/") for k in remote_listener.ROUTES))
        from jarvis.tools.files import SELF_PROTECTED
        self.assertIn("jarvis/v2/remote_listener.py", SELF_PROTECTED)
        print("ok  the remote route table imports no daemon route module, and is SELF_PROTECTED")


if __name__ == "__main__":
    unittest.main(verbosity=1)
