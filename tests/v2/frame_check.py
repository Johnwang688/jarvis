"""Frame hardening (WP-E; HUD workspace plan §2.5 item 3, decisions W-4).

Every response on the daemon's HUD listener and API listener must carry
`Content-Security-Policy: frame-ancestors 'none'` and `X-Frame-Options: DENY`
— the HUD page, its assets, JSON, SSE, every error and refusal, a request so
malformed the route never ran, and the terminal socket's hand-written `101`.
The preview (workshop) listener must carry neither — the HUD frames it on
purpose — but every response there carries exactly one CSP, `sandbox
allow-scripts allow-forms`, so a workshop page always runs with an opaque
origin, even in a frame its page was let keep (PR #31 review). A response's
own directives can never touch either rule: a comma, CR/LF, or a reserved
directive (`frame-ancestors`, `sandbox`, `report-*`) is refused.
`GET /status` reports `hud_port`, `api_port` and `frame_hardened`,
which is what lets the HUD offer a Preview pane's "keep its own origin"
switch at all.

Why it matters: a Preview pane may let a local dev page keep its own origin.
That is safe only while no page can turn its frame into the HUD — and the
browser enforces that only if 8402 says, on *every* response, that it may not
be framed. One route without the headers is one way back in.

Free and hermetic: three ephemeral loopback listeners (never 8402, 8403 or
8405), every config path and HOME in a temp dir, the terminal is
`terminal_fake/sh`, nothing reaches the network (the model catalog, speech
and the avatar art are stubbed).

The walk is checked against the source: every first path segment a route
module matches on must be walked (`test_the_walk_covers_every_route_family`),
so a new route family cannot be added without this suite seeing it. And the
one response written by hand is found by scanning for a raw status line.

Run:
    PYTHONPATH=. .venv/bin/python tests/v2/frame_check.py
"""
from __future__ import annotations

import base64
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis import avatars, config, models, voice  # noqa: E402
from jarvis.v2 import hud_api as H  # noqa: E402
from jarvis.v2.daemon import Daemon  # noqa: E402
from jarvis.v2.model import ProviderName as PN, Role  # noqa: E402
from jarvis.v2.provider import Brief, Decision, Event, EventKind as K, SessionHandle, Usage  # noqa: E402
from jarvis.v2.stores import Stores  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
FAKE_SHELL = Path(__file__).resolve().parent / "terminal_fake" / "sh"
LIVE_PORTS = {8402, 8403, 8405}
CSP = "Content-Security-Policy"
XFO = "X-Frame-Options"


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


def git(root, *args):
    subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)


def headers_of(raw_head: bytes) -> tuple[int, list[tuple[str, str]]]:
    """A raw response head → (status, [(name, value), ...]) with repeats kept."""
    lines = raw_head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    out = []
    for line in lines[1:]:
        if ":" in line:
            name, _, value = line.partition(":")
            out.append((name.strip(), value.strip()))
    return status, out


def raw_exchange(port: int, data: bytes, timeout=5.0) -> bytes:
    """Send bytes, read until the head is complete (or the socket closes)."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(data)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        return buf.partition(b"\r\n\r\n")[0]


def raw_all(port: int, data: bytes, timeout=5.0) -> bytes:
    """Send bytes and read everything the server writes until it closes."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(data)
        buf = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                return buf
            buf += chunk


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        (self.home / ".hushlogin").write_text("")
        env = patch.dict(os.environ, {"HOME": str(self.home), "XDG_DATA_HOME": str(self.root / "xdg"),
                                      "TMPDIR": str(self.root),
                                      "JARVIS_CLAUDE_CREDENTIALS": str(self.root / "none.json")})
        env.start()
        self.addCleanup(env.stop)
        tempfile.tempdir = None
        self.addCleanup(setattr, tempfile, "tempdir", None)
        for name, value in dict(V2_DATA_DIR=self.root / "data", ALLOWLIST_PATH=self.root / "allow.json",
                                MODELS_PATH=self.root / "models.json",
                                MODEL_CACHE_PATH=self.root / "catalog.json",
                                PROVIDER_DEFAULTS_PATH=self.root / "provider_defaults.json",
                                ROUTING_PATH=self.root / "routing.json",
                                CODEX_CATALOG_PATH=self.root / "codex-models.json",
                                V2_ALWAYS_ASK=self.root / "always-ask.json",
                                AVATAR_STATE_PATH=self.root / "avatar.json", AVATARS_DIR=self.root / "avatars",
                                AVATAR_ENV="",
                                DISCORD_GUILD_PATH=self.root / "guild.json",
                                DISCORD_TOKEN_PATH=self.root / "discord_token.json",
                                TERMINAL_SHELL=str(FAKE_SHELL)).items():
            guard = patch.object(config, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        # Nothing here reaches the network or a speech engine.
        catalog = [models.normalize(dict(id="test/model", name="Test", context_length=10000,
                                         supported_parameters=["tools"],
                                         architecture={"input_modalities": ["text"], "output_modalities": ["text"]},
                                         pricing={"prompt": "0", "completion": "0"}))]
        for target, name, kw in ((models, "catalog", {"return_value": catalog}),
                                 (models, "cached_info", {"return_value": None}),
                                 (voice, "tts", {"return_value": b"RIFFfake"}),
                                 (voice, "catalog", {"return_value": []}),
                                 (avatars, "svg", {"return_value": '<svg xmlns="http://www.w3.org/2000/svg"/>'})):
            stub = patch.object(target, name, **kw)
            stub.start()
            self.addCleanup(stub.stop)
        from jarvis.v2 import router as _router
        codex_table = _router.CLI_MODELS["codex"]
        self.addCleanup(lambda: _router.CLI_MODELS.__setitem__("codex", codex_table))

        self.work = self.root / "work"
        self.work.mkdir()
        (self.work / "hello.txt").write_text("hello\n")
        (self.work / "index.html").write_text("<h1>preview</h1>")
        (self.work / ".env").write_text("PLACEHOLDER=not-a-secret\n")
        git(self.work, "init", "-q")
        git(self.work, "config", "user.email", "test@example.invalid")
        git(self.work, "config", "user.name", "Test")
        git(self.work, "add", "hello.txt", "index.html")
        git(self.work, "commit", "-qm", "base")

        self.stores = Stores()
        self.daemon = Daemon(self.stores, {name: Fake(name) for name in PN},
                             lambda *_: lambda *_: Decision.DENY, 0)
        self.daemon.start()
        self.addCleanup(self.daemon.stop)
        self.ports = {"api": self.daemon.port, "hud": self.daemon.face_port,
                      "preview": self.daemon.workshop_port}
        self.assertFalse(set(self.ports.values()) & LIVE_PORTS, "a test daemon must never hold a live port")
        self.assertEqual(len(set(self.ports.values())), 3)
        self.project = self.stores.projects.create("Calc", str(self.work))
        self.thread = self.daemon.open_thread(self.project.id, Role.CHAT, PN.FAST,
                                              Brief(Role.CHAT, str(self.work)))
        self.task = self.stores.tasks.create(project_id=self.project.id, brief="a task")
        self.stores.tasks.save(self.task)
        self.seen: list[tuple[str, str, str, int]] = []   # (listener, method, path, status)

    # -- requests -----------------------------------------------------------------

    def ask(self, listener, method, path, body=None, *, origin=True, headers=None, raw=None,
            read_body=True):
        """One request; returns (status, [(name, value)...], body). Records it."""
        port = self.ports[listener]
        h = {}
        if body is not None:
            h["Content-Type"] = "application/json"
        if origin and listener == "hud":
            h["Origin"] = f"http://127.0.0.1:{port}"
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            data = json.dumps(body).encode() if body is not None else raw
            conn.request(method, path, data, h)
            response = conn.getresponse()
            got = response.getheaders()
            content = response.read() if read_body else b""
            status = response.status
        finally:
            conn.close()
        self.seen.append((listener, method, path, status))
        return status, got, content

    def assert_hardened(self, got, what):
        csp = [v for n, v in got if n.lower() == CSP.lower()]
        xfo = [v for n, v in got if n.lower() == XFO.lower()]
        self.assertEqual(len(csp), 1, f"{what}: exactly one CSP header, got {csp}")
        self.assertIn("frame-ancestors 'none'", csp[0], what)
        self.assertEqual(xfo, ["DENY"], f"{what}: X-Frame-Options")

    def assert_workshop(self, got, what):
        """The preview listener: no frame rule, and exactly one CSP — the sandbox."""
        csp = [v for n, v in got if n.lower() == CSP.lower()]
        self.assertEqual(csp, ["sandbox allow-scripts allow-forms"], f"{what}: the workshop's one CSP")
        self.assertFalse([v for n, v in got if n.lower() == XFO.lower()], f"{what}: X-Frame-Options")

    # -- a terminal (owner-only: the HUD listener and its Origin) -------------------

    def close_quietly(self, tid):
        try:
            self.daemon.terminals.close(tid)
        except Exception:
            pass                    # already closed by the test

    def ticket(self, tid):
        status, _, content = self.ask("hud", "POST", f"/terminals/{tid}/ticket", {})
        self.assertEqual(status, 200, content)
        return json.loads(content)["ticket"]

    def upgrade_head(self, tid, ticket, *, listener="hud", upgrade=True):
        port = self.ports[listener]
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        request = (f"GET /terminals/{tid}/attach?ticket={ticket} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                   + (f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                      f"Sec-WebSocket-Version: 13\r\n" if upgrade else "")
                   + f"Origin: http://127.0.0.1:{self.ports['hud']}\r\n\r\n")
        status, got = headers_of(raw_exchange(port, request.encode()))
        self.seen.append((listener, "GET", f"/terminals/{tid}/attach", status))
        return status, got


# Every route family the daemon serves, with the ids filled in at run time.
# (method, path, body) — the body is JSON, or None. Statuses vary on purpose:
# successes, refusals, missing ids, bad bodies. Each is asked on both the HUD
# and the API listener.
def routes(t) -> list[tuple[str, str, object]]:
    p, th, tk = t.project.id, t.thread.id, t.task.id
    return [
        ("GET", "/", None), ("GET", "/assets/missing.js", None), ("GET", "/assets/%2e%2e/index.html", None),
        ("HEAD", "/", None), ("OPTIONS", "/status", None),
        ("GET", "/status", None), ("GET", "/events", None), ("GET", "/nope", None), ("GET", "/projects/zzzz", None),
        ("GET", "/projects/deadbeef", None),
        ("GET", "/projects", None), ("POST", "/projects", {}), ("GET", f"/projects/{p}", None),
        ("PATCH", f"/projects/{p}", {"name": "Calc"}), ("PATCH", f"/projects/{p}", {"bogus": 1}),
        ("GET", f"/projects/{p}/tree", None), ("GET", f"/projects/{p}/file?path=hello.txt", None),
        ("GET", f"/projects/{p}/file?path=.env", None), ("PUT", f"/projects/{p}/file?path=x.txt", {"bogus": 1}),
        ("GET", f"/projects/{p}/platform", None), ("GET", f"/projects/{p}/impact", None),
        ("POST", f"/projects/{p}/archive", {}), ("POST", f"/projects/{p}/restore", {}),
        ("DELETE", f"/projects/{p}", None), ("GET", f"/projects/{p}/discord", None),
        ("POST", f"/projects/{p}/discord", {"bogus": 1}),
        ("GET", "/threads", None), ("POST", "/threads", {"project_id": p, "role": "chat"}),
        ("POST", "/threads", {"project_id": p}),
        ("GET", f"/threads/{th}/log", None), ("GET", f"/threads/{th}/transcript", None),
        ("PATCH", f"/threads/{th}", {"bogus": 1}), ("POST", f"/threads/{th}/seen", {}),
        ("POST", f"/threads/{th}/interrupt", {}), ("POST", f"/threads/{th}/answer", {"req_id": "x"}),
        ("POST", f"/threads/{th}/send", {"bogus": 1}), ("POST", f"/threads/{th}/archive", {"bogus": 1}),
        ("DELETE", f"/threads/{th}", None),
        ("GET", "/tasks", None), ("POST", "/tasks", {"project_id": p}), ("GET", f"/tasks/{tk}", None),
        ("POST", f"/tasks/{tk}/seen", {}), ("POST", f"/tasks/{tk}/start", {}),
        ("GET", f"/tasks/{tk}/journal", None), ("GET", f"/tasks/{tk}/threads", None),
        ("GET", f"/tasks/{tk}/diff", None), ("GET", f"/tasks/{tk}/diff/file?path=x", None),
        ("GET", f"/tasks/{tk}/worktree", None),
        ("GET", "/approvals", None), ("POST", "/approvals/nothere", {"decision": "deny"}),
        ("GET", "/activity", None), ("GET", "/usage", None), ("GET", "/discord", None),
        ("POST", "/discord/backfill", {}),
        ("GET", "/fs/dirs", None),
        ("GET", "/schedules", None), ("POST", "/schedules", {"bogus": 1}),
        ("POST", "/schedules/preview", {"when": "every day at 9"}), ("PATCH", "/schedules/nothere", {}),
        ("DELETE", "/schedules/nothere", None), ("POST", "/schedules/nothere/run-now", {}),
        ("GET", "/route", None), ("POST", "/route", {"bogus": 1}),
        ("GET", "/thread-models", None), ("POST", "/thread-models", {"bogus": 1}),
        ("GET", "/archive", None), ("GET", "/trash", None), ("POST", "/trash/empty", {"bogus": 1}),
        ("GET", "/avatars", None), ("GET", "/avatar.svg", None), ("POST", "/avatar", {"slug": "missing"}),
        ("GET", "/voices", None), ("POST", "/voice", {"bogus": 1}),
        ("GET", "/models", None), ("GET", "/models/catalog", None), ("POST", "/model", {"bogus": 1}),
        ("POST", "/models", {"bogus": 1}), ("POST", "/mute", {"muted": "yes"}),
        ("POST", "/say", {}), ("POST", "/say", {"text": "hello"}), ("POST", "/stt", {"bogus": 1}),
        ("GET", "/terminals", None), ("POST", "/terminals", {"bogus": 1}),
        ("GET", "/terminals/deadbeef/attach", None), ("DELETE", "/terminals/deadbeef", None),
    ]


class FrameHeaders(Base):
    def test_every_hud_and_api_response_refuses_to_be_framed(self):
        statuses = set()
        for listener in ("hud", "api"):
            for method, path, body in routes(self):
                with self.subTest(listener=listener, method=method, path=path):
                    # The event stream never ends: its head is all there is to read.
                    status, got, _ = self.ask(listener, method, path, body,
                                              read_body=not path.startswith("/events"))
                    statuses.add(status)
                    self.assert_hardened(got, f"{listener} {method} {path} -> {status}")
        # The walk really spans successes, refusals and errors.
        for status in (200, 201, 400, 403, 404, 409):
            self.assertIn(status, statuses, f"the walk never produced a {status}")

    def test_refusals_before_any_route_runs(self):
        for listener in ("hud", "api"):
            port = self.ports[listener]
            cases = [
                ("untrusted Host", dict(headers={"Host": "attacker.example"})),
                ("cross-origin Origin", dict(headers={"Origin": "http://evil.example"})),
                ("cross-site fetch", dict(headers={"Sec-Fetch-Site": "cross-site"})),
                ("the preview origin", dict(headers={"Origin": f"http://127.0.0.1:{self.ports['preview']}"})),
            ]
            for what, kw in cases:
                with self.subTest(listener=listener, what=what):
                    status, got, _ = self.ask(listener, "GET", "/status", origin=False, **kw)
                    self.assertEqual(status, 403, what)
                    self.assert_hardened(got, f"{listener} {what}")
            with self.subTest(listener=listener, what="a form body"):
                status, got, _ = self.ask(listener, "POST", "/threads", raw=b"a=b",
                                          headers={"Content-Type": "application/x-www-form-urlencoded"})
                self.assertEqual(status, 400)
                self.assert_hardened(got, f"{listener} form body")
            with self.subTest(listener=listener, what="an oversize body, refused unread"):
                status, got, _ = self.ask(listener, "POST", "/threads", raw=b"",
                                          headers={"Content-Type": "application/json",
                                                   "Content-Length": str(49 * 1024 * 1024)})
                self.assertEqual(status, 413)
                self.assert_hardened(got, f"{listener} 413")
            # Requests the route never sees: http.server's own refusals go
            # through the handler's `send_error`, so they carry them too.
            for what, data, want in (
                    ("a garbage request line", b"GARBAGE\r\n\r\n", 400),
                    ("an unknown method", b"BREW / HTTP/1.0\r\nHost: x\r\n\r\n", 501),
                    ("a bad HTTP version", b"GET / HTTP/9.9\r\n\r\n", 505),
                    ("a malformed HTTP version", b"GET / HTTP/x.y\r\n\r\n", 400),
                    # HTTP/0.9 has no Host, so the Host check refuses it — and
                    # answers as 1.0, since a 0.9 answer could carry no header.
                    ("an HTTP/0.9 request", b"GET /\r\n", 403),
                    ("a request line too long", b"GET /" + b"a" * 70000 + b" HTTP/1.0\r\n\r\n", 414),
                    ("too many headers", b"GET / HTTP/1.0\r\n" + b"X-A: b\r\n" * 120 + b"\r\n", 431)):
                with self.subTest(listener=listener, what=what):
                    status, got = headers_of(raw_exchange(port, data))
                    self.seen.append((listener, "RAW", what, status))
                    self.assertEqual(status, want, what)
                    self.assert_hardened(got, f"{listener} {what}")

    def test_the_event_stream_refuses_to_be_framed(self):
        for listener in ("hud", "api"):
            with self.subTest(listener=listener):
                status, got, _ = self.ask(listener, "GET", "/events", read_body=False)
                self.assertEqual(status, 200)
                self.assertIn(("Content-Type", "text/event-stream"), got)
                self.assert_hardened(got, f"{listener} SSE")
                status, got, _ = self.ask(listener, "GET", f"/events?thread={self.thread.id}", read_body=False)
                self.assert_hardened(got, f"{listener} SSE filtered")

    def test_the_hud_page_and_its_assets_refuse_to_be_framed(self):
        dist = self.root / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<!doctype html><title>J.A.R.V.I.S.</title>")
        (dist / "assets" / "app.js").write_text("console.log('HUD')")
        with patch.object(H, "HUD_DIST", dist):
            for listener in ("hud", "api"):
                for path in ("/", "/assets/app.js"):
                    with self.subTest(listener=listener, path=path):
                        status, got, content = self.ask(listener, "GET", path)
                        self.assertEqual(status, 200)
                        self.assertTrue(content)
                        self.assert_hardened(got, f"{listener} {path}")

    def test_a_response_with_its_own_policy_keeps_both_halves_in_one_header(self):
        # The avatar's SVG locks itself down further; the frame rule rides in
        # the same header (two policies would both apply, but one builder
        # keeps the next change from dropping the frame rule).
        status, got, content = self.ask("hud", "GET", "/avatar.svg")
        self.assertEqual(status, 200, content)
        self.assert_hardened(got, "avatar.svg")
        csp = next(v for n, v in got if n == CSP)
        self.assertIn("default-src 'none'", csp)
        self.assertIn("style-src 'unsafe-inline'", csp)
        self.assertEqual(H.content_security_policy(), "frame-ancestors 'none'")
        self.assertEqual(H.content_security_policy("img-src 'self'"), "img-src 'self'; frame-ancestors 'none'")

    def test_a_response_cannot_change_the_frame_rule_through_its_own_directives(self):
        # PR #31 review, shown in Chromium: a comma starts a second policy, so
        # `img-src 'self', frame-ancestors *` became `img-src 'self'` and
        # `frame-ancestors *; frame-ancestors 'none'` (the first wins), and a
        # browser that sees frame-ancestors ignores X-Frame-Options: a
        # keep-origin frame landed same-origin on the HUD and read the parent.
        for bad in ("img-src 'self', frame-ancestors *",          # a second policy
                    "img-src 'self',frame-ancestors *",
                    "default-src 'none'; frame-ancestors *",       # an override
                    "FRAME-ANCESTORS http://localhost:5173",
                    "sandbox allow-same-origin allow-scripts",     # reserved
                    "report-uri http://localhost:5173/r",
                    "img-src 'self'; report-to x",
                    "img-src 'self'\r\nX-Injected: 1",            # a header split
                    "img-src 'self'\nX-Injected: 1",
                    "img-src 'self'\0",
                    "img-src 'self'\u2028",                       # not ASCII (strip() ate it silently)
                    "img-src\u2028'self'",                        # not latin-1: failed after the status line
                    "img-src 'self' https://exampl\u00e9.com",     # not ASCII
                    "img-src\t'self'"):                            # not printable
            for builder in (H.content_security_policy, H.workshop_security_policy):
                with self.subTest(bad=bad, builder=builder.__name__), self.assertRaises(ValueError):
                    builder(bad)
            with self.subTest(bad=bad, where="frame_headers"), self.assertRaises(ValueError):
                H.frame_headers(SimpleNamespace(server=SimpleNamespace()), bad)

        class Recorder:
            server = SimpleNamespace()

            def __getattr__(self, name):
                raise AssertionError(f"binary wrote something ({name}) before refusing its policy")

        for bad in ("img-src 'self', frame-ancestors *", "img-src 'self'\u2028"):
            with self.subTest(bad=bad, where="binary"), self.assertRaises(ValueError):
                H.binary(Recorder(), b"x", "text/plain", csp=bad)

    def test_a_bad_policy_is_one_400_never_two_status_lines(self):
        # PR #31 re-review: a U+2028 in a response's own directives passed the
        # check, then failed inside end_headers, after send_response — so the
        # dispatcher's error answer went out as a second status line on the
        # same response. It is refused before a byte is written now, and the
        # dispatcher's answer to a ValueError is a 400 (docs/hud-api.md).
        for bad in ("img-src\u2028'self'", "img-src 'self'\u2028", "img-src 'self', frame-ancestors *"):
            def answer(handler, *_args, bad=bad):
                return H.binary(handler, b"<svg/>", "image/svg+xml", csp=bad)
            with self.subTest(bad=bad), patch.object(H, "pickers", side_effect=answer):
                port = self.ports["hud"]
                data = raw_all(port, f"GET /avatar.svg HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
                self.assertEqual(data.count(b"HTTP/1."), 1, data[:300])
                head, _, body = data.partition(b"\r\n\r\n")
                status, got = headers_of(head)
                self.assertEqual(status, 400, head)
                self.assert_hardened(got, f"the refusal of {bad!r}")
                self.assertEqual(json.loads(body), {"error": "request failed (ValueError)"})

    def test_the_terminal_socket_and_its_refusals_refuse_to_be_framed(self):
        status, _, content = self.ask("hud", "POST", "/terminals", {"in": {"project": self.project.id}})
        self.assertEqual(status, 201, content)
        tid = json.loads(content)["id"]
        self.addCleanup(self.close_quietly, tid)
        # The 101 is written by hand (HTTP/1.1, past `end_headers`).
        status, got = self.upgrade_head(tid, self.ticket(tid))
        self.assertEqual(status, 101)
        self.assert_hardened(got, "the 101")
        # A spent ticket, a plain GET that is not an upgrade, the API listener.
        status, got = self.upgrade_head(tid, "spent")
        self.assertEqual(status, 403)
        self.assert_hardened(got, "403 bad ticket")
        status, got = self.upgrade_head(tid, self.ticket(tid), upgrade=False)
        self.assertEqual(status, 400)
        self.assert_hardened(got, "400 not an upgrade")
        status, got = self.upgrade_head(tid, "x", listener="api")
        self.assertEqual(status, 403)
        self.assert_hardened(got, "the API listener's refusal")
        status, got, _ = self.ask("hud", "DELETE", f"/terminals/{tid}")
        self.assertEqual(status, 200)
        self.assert_hardened(got, "terminal close")

    def test_the_upgrade_refuses_a_header_that_could_split_the_response(self):
        from jarvis.v2 import ws as W

        class Sink:
            def __init__(self):
                self.wfile = self

            def write(self, data):
                raise AssertionError("nothing may be written")

        with self.assertRaises(ValueError):
            W.upgrade(Sink(), "dGhlIHNhbXBsZSBub25jZQ==", headers=[("X-A", "b\r\nX-Injected: 1")])


class PreviewOrigin(Base):
    def test_the_workshop_is_framed_but_always_sandboxed(self):
        prefix = f"/p/{self.project.id}/"
        for method, path, want in (("GET", prefix + "hello.txt", 200), ("GET", prefix, 200),
                                   ("GET", prefix + ".env", 403), ("GET", "/status", 404),
                                   ("GET", "/", 404), ("PUT", prefix + "hello.txt", 404),
                                   ("GET", "/p/deadbeef/x", 404)):
            with self.subTest(path=path):
                status, got, _ = self.ask("preview", method, path, {} if method == "PUT" else None)
                self.assertEqual(status, want)
                self.assert_workshop(got, f"preview {method} {path}")
        status, got, _ = self.ask("preview", "GET", prefix + "hello.txt", headers={"Host": "attacker.example"})
        self.assertEqual(status, 403)
        self.assert_workshop(got, "preview bad Host")
        status, got = headers_of(raw_exchange(self.ports["preview"], b"GARBAGE\r\n\r\n"))
        self.assertEqual(status, 400)
        self.assert_workshop(got, "preview garbage")
        preview = SimpleNamespace(server=SimpleNamespace(preview_only=True))
        self.assertEqual(H.frame_headers(preview),
                         [("Content-Security-Policy", "sandbox allow-scripts allow-forms")])
        self.assertEqual(H.frame_headers(preview, "img-src 'self'"),
                         [("Content-Security-Policy", "img-src 'self'; sandbox allow-scripts allow-forms")],
                         "a response's own directives merge into the one header")


class Status(Base):
    def test_status_reports_the_three_ports_and_frame_hardening(self):
        for listener in ("hud", "api"):
            with self.subTest(listener=listener):
                status, _, content = self.ask(listener, "GET", "/status")
                self.assertEqual(status, 200)
                body = json.loads(content)
                self.assertEqual(body["hud_port"], self.daemon.face_port)
                self.assertEqual(body["api_port"], self.daemon.port)
                self.assertEqual(body["workshop_port"], self.daemon.workshop_port)
                self.assertIs(body["frame_hardened"], True)
                self.assertFalse({body["hud_port"], body["api_port"], body["workshop_port"]} & {0})

    def test_frame_hardened_comes_from_the_handler_that_adds_the_headers(self):
        # A daemon not started has no listener, so nothing is hardened yet;
        # and the flag is the handler class's own, next to `end_headers`.
        idle = Daemon(Stores(self.root / "idle"), {}, lambda *_: None, 0)
        self.assertIs(idle.status()["frame_hardened"], False)
        handler = self.daemon._server.RequestHandlerClass
        self.assertIs(handler.frame_hardened, True)
        self.assertIn("end_headers", vars(handler), "the hook that adds the headers")


class Coverage(Base):
    ROUTE_FILES = ("jarvis/v2/daemon.py", "jarvis/v2/hud_api.py", "jarvis/v2/projects.py",
                   "jarvis/v2/terminals.py", "jarvis/v2/discord/routes.py")

    def families(self) -> set[str]:
        """Every first path segment a route module matches on."""
        found: set[str] = set()
        for name in self.ROUTE_FILES:
            text = (REPO / name).read_text()
            found |= set(re.findall(r'parts\[0\] [!=]= "([\w.-]+)"', text))
            found |= set(re.findall(r'parts == \["([\w.-]+)"', text))
            for pair in re.findall(r'parts in \(\["([\w.-]+)"\], \["([\w.-]+)"\]\)', text):
                found |= set(pair)
            for group in re.findall(r'"/"\.join\(parts\) in \(([^)]*)\)', text):
                found |= {s.split("/")[0] for s in re.findall(r'"([\w./-]+)"', group)}
        found.discard("p")          # the preview listener's only route (PreviewOrigin walks it)
        return found

    def test_the_walk_covers_every_route_family(self):
        walked = {path.split("?")[0].strip("/").split("/")[0] for _, path, _ in routes(self)}
        missing = self.families() - walked
        self.assertFalse(missing, f"route families no frame check walks: {sorted(missing)}")
        self.assertGreater(len(self.families()), 20, "the scan found the route modules")

    def test_the_only_status_line_written_by_hand_is_the_upgrade(self):
        # `end_headers` is the hook; a status line written straight to the
        # socket would bypass it. The only one is ws.upgrade's 101, which
        # the terminal test above proves carries the headers.
        hits = []
        for path in sorted((REPO / "jarvis" / "v2").rglob("*.py")):
            for i, line in enumerate(path.read_text().splitlines(), 1):
                if re.search(r'b?"HTTP/1\.[01] \d{3}', line):
                    hits.append(f"{path.relative_to(REPO)}:{i}")
        self.assertEqual([h.split(":")[0] for h in hits], ["jarvis/v2/ws.py"], hits)


if __name__ == "__main__":
    unittest.main(verbosity=2)
