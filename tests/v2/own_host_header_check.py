"""`config.own_host_header`, the one DNS-rebinding Host check (CL-3, 2026-10-18).

Free and hermetic: the daemon listens on ephemeral loopback ports (never 8402,
8403 or 8405) with temp config, fake providers and a temp HOME.

Two parts.

  - `Helper`: the unit table. Exactly two spellings pass, `127.0.0.1:<port>` and
    `localhost:<port>`, compared on the raw header value. Everything else is
    refused: other ports, a rebound name, `127.0.0.1.nip.io`, a case change, a
    trailing dot, no port, `[::1]:<port>`, a missing header, a non-string.

  - `Sites`: the listeners whose own Host check no other test reaches. On the
    real handler `check_origin` (hud_api) refuses a forged Host first, so these
    checks are a second line of defence that an ordinary request never gets
    through to. The tests take the first line out (`check_origin` patched to a
    no-op) and send a forged Host *with a matching Origin*, which the owner gate
    (`projects.is_owner`) accepts by itself. The refusal that remains can only
    be the site's own Host check:
      - `/browser/…`   (`browser/routes._strict_host`)
      - `/vex/…`       (`vex._owner`)
      - `/dev-servers` (`devservers.route`)
      - `/terminals`   (`terminals._strict_host`)
      - `POST /approvals/<id>`  (`daemon`'s `owner_verified=` flag: a hud-only approval
        answered from a forged Host must not count as answered in the HUD)

The other sites (`hud_api.check_origin`, `projects.is_owner`) have refusal
tests in hud_backend_check and owner_read_check.

Run: PYTHONPATH=. .venv/bin/python tests/v2/own_host_header_check.py
"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis import config  # noqa: E402
from jarvis.v2.approvals import ApprovalRequest  # noqa: E402
from jarvis.v2.daemon import Daemon  # noqa: E402
from jarvis.v2.model import ProviderName as PN  # noqa: E402
from jarvis.v2.provider import Decision, SessionHandle, Usage  # noqa: E402
from jarvis.v2.stores import Stores  # noqa: E402

LIVE_PORTS = {8402, 8403, 8405}


class Helper(unittest.TestCase):
    PORT = 41234

    def test_the_two_exact_spellings_pass(self):
        p = self.PORT
        self.assertIs(config.own_host_header(f"127.0.0.1:{p}", p), True)
        self.assertIs(config.own_host_header(f"localhost:{p}", p), True)

    def test_every_other_value_is_refused(self):
        p = self.PORT
        table = {
            "other port, ip": f"127.0.0.1:{p + 1}",
            "other port, name": f"localhost:{p + 1}",
            "evil name": f"evil.example:{p}",
            "rebinding suffix": f"127.0.0.1.nip.io:{p}",
            "rebinding prefix": f"localhost.evil.example:{p}",
            "subdomain of localhost": f"foo.localhost:{p}",
            "uppercase name": f"LOCALHOST:{p}",
            "mixed case name": f"Localhost:{p}",
            "trailing dot": f"localhost.:{p}",
            "trailing dot, ip": f"127.0.0.1.:{p}",
            "no port, ip": "127.0.0.1",
            "no port, name": "localhost",
            "empty port": "127.0.0.1:",
            "ipv6 loopback": f"[::1]:{p}",
            "other loopback address": f"127.0.0.2:{p}",
            "leading space": f" 127.0.0.1:{p}",
            "trailing space": f"127.0.0.1:{p} ",
            "port with a leading zero": f"127.0.0.1:0{p}",
            "userinfo": f"user@127.0.0.1:{p}",
            "scheme": f"http://127.0.0.1:{p}",
            "two hosts": f"127.0.0.1:{p},evil.example:{p}",
            "missing header": "",
            "none": None,
        }
        for label, value in table.items():
            with self.subTest(label):
                self.assertIs(config.own_host_header(value, p), False, value)

    def test_the_port_argument_is_what_is_compared(self):
        self.assertIs(config.own_host_header("127.0.0.1:8402", 8402), True)
        self.assertIs(config.own_host_header("127.0.0.1:8402", 8403), False)
        self.assertIs(config.own_host_header("localhost:8402", 8403), False)

    def test_exactly_two_spellings_among_a_wide_sweep(self):
        p = self.PORT
        names = ["127.0.0.1", "localhost", "LOCALHOST", "127.0.0.1.", "localhost.", "[::1]", "::1",
                 "0.0.0.0", "127.1", "2130706433", "0x7f000001", "127.0.0.1.nip.io", "evil.example", ""]
        passed = {f"{n}:{p}" for n in names if config.own_host_header(f"{n}:{p}", p)}
        self.assertEqual(passed, {f"127.0.0.1:{p}", f"localhost:{p}"})


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

    def interrupt(self, handle):
        pass

    def close(self, handle):
        pass


class Sites(unittest.TestCase):
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
                                DISCORD_GUILD_PATH=self.root / "guild.json").items():
            guard = patch.object(config, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        self.daemon = Daemon(Stores(), {name: Fake(name) for name in PN},
                             lambda *_: lambda *_: Decision.DENY, 0)
        self.daemon.start()
        self.addCleanup(self.daemon.stop)
        self.face = self.daemon.face_port
        self.assertFalse({self.daemon.port, self.face, self.daemon.workshop_port} & LIVE_PORTS,
                         "a test daemon must never hold a live port")

    def raw(self, method, path, host, *, origin=True, body=None):
        """One request to the HUD listener with exactly this Host. `origin=True`
        sends the Origin a browser page on that Host would (`http://<host>`)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.face, timeout=10)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", host)
            if origin:
                conn.putheader("Origin", f"http://{host}")
            data = json.dumps(body).encode() if body is not None else b""
            if body is not None or method in ("POST", "PATCH", "PUT"):
                conn.putheader("Content-Type", "application/json")
                conn.putheader("Content-Length", str(len(data)))
            conn.endheaders(data or None)
            response = conn.getresponse()
            return response.status, response.read().decode("utf-8", "replace")
        finally:
            conn.close()

    def forged(self):
        return (f"evil.example:{self.face}", f"127.0.0.1.nip.io:{self.face}", f"LOCALHOST:{self.face}",
                f"localhost.:{self.face}", f"[::1]:{self.face}", "127.0.0.1", f"127.0.0.1:{self.face + 1}")

    def test_the_first_line_still_refuses_a_forged_host_on_the_real_handler(self):
        for host in self.forged():
            with self.subTest(host=host):
                status, text = self.raw("GET", "/vex/status", host)
                self.assertEqual((status, "untrusted Host" in text), (403, True), text[:200])

    def test_browser_routes_check_the_host_themselves(self):
        with patch("jarvis.v2.hud_api.check_origin", lambda *a, **k: None):
            own = f"127.0.0.1:{self.face}"
            status, text = self.raw("GET", "/browser/settings", own)
            self.assertEqual(status, 200, text[:200])           # the same request, a real Host
            self.assertEqual(self.raw("GET", "/browser/settings", f"localhost:{self.face}")[0], 200)
            for host in self.forged():
                with self.subTest(host=host):
                    status, text = self.raw("GET", "/browser/settings", host)
                    self.assertEqual(status, 403, text[:200])
                    self.assertIn("untrusted Host", text)

    def test_vex_routes_check_the_host_themselves(self):
        with patch("jarvis.v2.hud_api.check_origin", lambda *a, **k: None):
            own = f"127.0.0.1:{self.face}"
            status, text = self.raw("GET", "/vex/status", own)
            self.assertEqual(status, 200, text[:200])
            self.assertEqual(self.raw("GET", "/vex/status", f"localhost:{self.face}")[0], 200)
            for host in self.forged():
                with self.subTest(host=host):
                    status, text = self.raw("GET", "/vex/status", host)
                    self.assertEqual(status, 403, text[:200])

    def test_dev_servers_routes_check_the_host_themselves(self):
        with patch("jarvis.v2.hud_api.check_origin", lambda *a, **k: None):
            status, text = self.raw("GET", "/dev-servers", f"127.0.0.1:{self.face}")
            self.assertEqual(status, 200, text[:200])
            self.assertEqual(self.raw("GET", "/dev-servers", f"localhost:{self.face}")[0], 200)
            for host in self.forged():
                with self.subTest(host=host):
                    status, text = self.raw("GET", "/dev-servers", host)
                    self.assertEqual(status, 403, text[:200])
                    self.assertIn("untrusted Host", text)

    def test_terminal_routes_check_the_host_themselves(self):
        with patch("jarvis.v2.hud_api.check_origin", lambda *a, **k: None):
            status, text = self.raw("GET", "/terminals", f"127.0.0.1:{self.face}")
            self.assertEqual((status, json.loads(text)), (200, []))
            self.assertEqual(self.raw("GET", "/terminals", f"localhost:{self.face}")[0], 200)
            for host in self.forged():
                with self.subTest(host=host):
                    status, text = self.raw("GET", "/terminals", host)
                    self.assertEqual(status, 403, text[:200])
                    self.assertIn("untrusted Host", text)

    def hud_only_approval(self):
        answers = []
        asked = threading.Event()
        broker = self.daemon.approvals
        previous = broker._on_request

        def on_request(request):
            if previous is not None:
                previous(request)
            asked.set()

        broker._on_request = on_request
        self.addCleanup(setattr, broker, "_on_request", previous)
        request = ApprovalRequest("hud_browser_act", hud_only=True)
        worker = threading.Thread(target=lambda: answers.append(broker.ask(request)), daemon=True)
        worker.start()
        self.addCleanup(worker.join, 5)
        self.addCleanup(lambda: [broker.resolve(r.req_id, Decision.DENY) for r in broker.pending()])
        self.assertTrue(asked.wait(5))
        return request, worker, answers

    def test_a_hud_only_approval_is_answered_only_from_a_real_host(self):
        request, worker, answers = self.hud_only_approval()
        path = f"/approvals/{request.req_id}"
        with patch("jarvis.v2.hud_api.check_origin", lambda *a, **k: None):
            for host in self.forged():
                with self.subTest(host=host):
                    status, text = self.raw("POST", path, host, body={"decision": "allow"})
                    self.assertEqual(status, 404, text[:200])
                    self.assertIn("owner HUD", text)
                    self.assertEqual([r.req_id for r in self.daemon.approvals.pending()], [request.req_id])
            status, text = self.raw("POST", path, f"127.0.0.1:{self.face}", body={"decision": "allow"})
            self.assertEqual(status, 200, text[:200])
        worker.join(5)
        self.assertEqual(answers, [Decision.ALLOW])


if __name__ == "__main__":
    unittest.main(verbosity=2)
