"""SEC-1 case 7: the v2 Browser gate refuses `127.<name>` on Jarvis's ports.

Task card: `docs/plans/2026-10-18-security-fix-specs.md`, section SEC-1,
with the lead's tighten-only amendment (2026-10-18): no URL refused today
may become allowed. After SEC-1 `127.rebind.example` is no longer a
spelling of this machine. The SOCKS gate is the boundary, and it must still
refuse the connection: Jarvis's ports are refused on every address, and a
name that resolves to loopback is DNS rebinding whatever the port. The URL
judge must keep refusing such a name on every refused port too (7g), and
the name must no longer count as the owner opening a local server (7d).

Reuses `browser_gate_check.Base`: the real gate on an ephemeral loopback
port, an injected resolver (no DNS) and connector (never beyond loopback),
and stand-in listeners configured as the control ports whose accept
counters must stay 0. Never 8402, 8403 or 8405.

Run:
    PYTHONPATH=. .venv/bin/python tests/v2/loopback_names_gate_check.py
"""
from __future__ import annotations

from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis import config  # noqa: E402
from jarvis.v2.browser import gate as G, policy as P  # noqa: E402
from browser_gate_check import Base, code  # noqa: E402

NAMES_127 = ("127.rebind.example", "127.0.0.1.nip.io")


class LoopbackNamesAtTheGate(Base):
    def setUp(self):
        super().setUp()
        for name in NAMES_127:
            self.resolver.table[name] = ["127.0.0.1"]

    def test_case7a_127_name_to_every_control_port_is_refused(self):
        sent = 0
        for lan in (False, True):
            self.lan[0] = lan
            for label, listener in self.stand_ins.items():
                # Even a port the owner had opened: Jarvis's ports come first.
                self.opened.add(listener.port)
                for name in NAMES_127:
                    with self.subTest(name=name, port=label, lan=lan):
                        self.assertEqual(code(self.gate.port, name, listener.port),
                                         G.REP_NOT_ALLOWED)
                        sent += 1
        time.sleep(0.2)
        self.assertEqual(self.jarvis_accepts(), {}, "a stand-in for Jarvis's ports was reached")
        self.assertEqual(self.connector.seen, [], "nothing was even attempted")
        refused = self.gate.stats()["refused"]
        self.assertEqual(refused.get(P.JARVIS_PORT, 0) + refused.get(P.NAME_TO_LOOPBACK, 0), sent)

    def test_case7b_127_name_to_an_opened_dev_port_is_rebinding(self):
        # The owner opened the dev port (by a real loopback spelling); a name
        # that resolves to loopback still does not reach it.
        sent = 0
        for lan in (False, True):
            self.lan[0] = lan
            for name in NAMES_127:
                with self.subTest(name=name, lan=lan):
                    self.assertEqual(code(self.gate.port, name, self.dev.port), G.REP_NOT_ALLOWED)
                    sent += 1
        time.sleep(0.2)
        self.assertEqual(self.dev.accepts, 0)
        self.assertEqual(self.connector.seen, [])
        self.assertEqual(self.gate.stats()["refused"].get(P.NAME_TO_LOOPBACK), sent)
        for name in NAMES_127:
            self.assertIn(name, self.resolver.calls, "the gate resolves a 127.<name>")

    def test_case7c_judged_url_then_gate_refuses_end_to_end(self):
        # Whatever the URL judge says about the typed address, what Chrome
        # would then send the gate is refused, and no control stand-in is
        # reached (the gate does not lean on the judge).
        for label, listener in self.stand_ins.items():
            url = f"http://127.rebind.example:{listener.port}/"
            refused = P.refused_ports(hud_port=self.stand_ins["hud"].port,
                                      api_port=self.stand_ins["api"].port,
                                      workshop_port=self.workshop.port)
            try:
                P.judge_url(url, refused=refused)
            except P.URLRefused as exc:
                self.assertEqual(exc.reason, P.JARVIS_PORT)
                continue
            with self.subTest(port=label):
                self.assertEqual(code(self.gate.port, "127.rebind.example", listener.port),
                                 G.REP_NOT_ALLOWED)
        time.sleep(0.2)
        self.assertEqual(self.jarvis_accepts(), {})


    def test_case7g_url_judge_still_refuses_127_names_on_every_refused_port(self):
        # Tighten-only: on main the judge refuses `127.<name>` on every port in
        # `refused` (BR-0's control ports and this daemon's reported HUD, API
        # and workshop ports), not only on `config.control_ports()`.
        refused = P.refused_ports(hud_port=self.stand_ins["hud"].port,
                                  api_port=self.stand_ins["api"].port,
                                  workshop_port=self.workshop.port)
        self.assertIn(P.DEFAULT_WORKSHOP_PORT, refused)
        for port in sorted(refused):
            for name in NAMES_127:
                with self.subTest(name=name, port=port):
                    with self.assertRaises(P.URLRefused) as raised:
                        P.judge_url(f"http://{name}:{port}/", refused=refused)
                    self.assertEqual(raised.exception.reason, P.JARVIS_PORT)


class LoopbackNamesOwnerOpening(unittest.TestCase):
    """The card's behaviour change at `policy.py:272`: an owner typing
    `127.rebind.example:<port>` no longer opens that loopback port; the real
    loopback spellings still do."""

    def test_case7d_127_name_does_not_open_a_loopback_port(self):
        for url in ("http://127.rebind.example:3000/", "http://127.0.0.1.nip.io:3000/",
                    "https://127.rebind.example/"):
            with self.subTest(url=url):
                self.assertIsNone(P.loopback_port(url))

    def test_case7e_literal_spellings_still_open_their_port(self):
        for url, port in (("http://127.0.0.1:3000/", 3000), ("http://127.5.6.7:3000/", 3000),
                          ("http://127.1:3000/", 3000), ("http://0x7f000001:3000/", 3000),
                          ("http://[::1]:3000/", 3000), ("http://localhost:3000/", 3000),
                          ("http://localhost.:3000/", 3000), ("http://foo.localhost:3000/", 3000),
                          ("https://127.0.0.1/", 443)):
            with self.subTest(url=url):
                self.assertEqual(P.loopback_port(url), port)

    def test_case7f_url_judge_still_refuses_literal_control_ports(self):
        for port in sorted(config.control_ports()):
            for host in ("127.0.0.1", "127.1", "0x7f000001", "[::1]", "localhost", "localhost."):
                with self.subTest(host=host, port=port):
                    with self.assertRaises(P.URLRefused) as raised:
                        P.judge_url(f"http://{host}:{port}/")
                    self.assertEqual(raised.exception.reason, P.JARVIS_PORT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
