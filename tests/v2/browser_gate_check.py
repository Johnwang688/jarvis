"""The Browser's URL judge and SOCKS gate (BR-1a; plan §4, §11): no Chrome.

What it proves, against the real gate on an ephemeral loopback port:

- the URL judge allows http, https and about:blank only, refuses a user name
  or password, every refused scheme, an ambiguous authority, invisible
  characters and Jarvis's ports in every loopback spelling — and its
  sentences never contain the URL;
- `refused_ports` is BR-0's control ports plus the daemon's reported HUD and
  API ports, plus 8403 unless it is the daemon's workshop;
- every refusal family at the gate: Jarvis's ports (stand-in listeners on
  ephemeral ports, configured as the control ports — their accept counters
  stay 0) in every spelling Chrome or a client could send, by name and as
  IPv4 and IPv6 addresses; the gate's own port; a name that resolves to
  loopback; private addresses with the LAN switch off (and on: allowed,
  but never Jarvis's ports or a name onto loopback); multicast, reserved and
  the rest of 0/8; unresolvable names; bad names; every SOCKS ATYP, and the
  commands and methods it does not speak; the connection cap;
- **rebinding**: an injected resolver that answers a public address first
  and loopback after is asked exactly once, and the gate connects only to
  the address it judged (mutation: a second resolution);
- a dev port is piped through, both ways, with a half-close;
- the log names a refusal's class, never a host.

Hermetic: the resolver and connector are injected for every gate, so no name
reaches DNS and no address beyond loopback is ever connected to (the
connector refuses them). Never 8402, 8403 or 8405.

Run:
    PYTHONPATH=. .venv/bin/python tests/v2/browser_gate_check.py
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis import config  # noqa: E402
from jarvis.v2.browser import gate as G, policy as P  # noqa: E402

LIVE_PORTS = {8402, 8403, 8405}


# -- helpers ------------------------------------------------------------------------------

class Listener:
    """A loopback TCP listener on 127.0.0.1 and ::1 (one port) that counts
    accepts; with `echo`, it echoes every byte and closes after the peer's
    half-close."""

    def __init__(self, echo: bool = False):
        self.accepts = 0
        self.echo = echo
        self._lock = threading.Lock()
        self._socks = []
        for _ in range(40):
            s4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s4.bind(("127.0.0.1", 0))
            port = s4.getsockname()[1]
            s6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            s6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            try:
                s6.bind(("::1", port))
            except OSError:
                s4.close()
                s6.close()
                continue
            break
        else:
            raise RuntimeError("no port free on both loopbacks")
        assert port not in LIVE_PORTS
        self.port = port
        for s in (s4, s6):
            s.listen(64)
            self._socks.append(s)
            threading.Thread(target=self._accept, args=(s,), daemon=True).start()

    def _accept(self, s):
        while True:
            try:
                conn, _ = s.accept()
            except OSError:
                return
            with self._lock:
                self.accepts += 1
            if self.echo:
                threading.Thread(target=self._echo, args=(conn,), daemon=True).start()
            else:
                conn.close()

    @staticmethod
    def _echo(conn):
        with conn:
            while True:
                try:
                    data = conn.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                conn.sendall(data)

    def close(self):
        for s in self._socks:
            s.close()


def recv_exact(s, n):
    buf = b""
    while len(buf) < n:
        try:
            chunk = s.recv(n - len(buf))
        except OSError:
            return buf
        if not chunk:
            return buf
        buf += chunk
    return buf


def socks(gate_port, host, port, *, atyp=3, cmd=1, methods=b"\x00", raw_name=None):
    """One SOCKS5 exchange. Returns (reply code or the greeting answer, sock)."""
    s = socket.create_connection(("127.0.0.1", gate_port), timeout=10)
    s.sendall(b"\x05" + bytes([len(methods)]) + methods)
    hello = recv_exact(s, 2)
    if hello != b"\x05\x00":
        return hello, s
    if atyp == 1:
        addr = socket.inet_aton(host)
    elif atyp == 4:
        addr = socket.inet_pton(socket.AF_INET6, host)
    elif atyp == 3:
        name = raw_name if raw_name is not None else host.encode()
        addr = bytes([len(name)]) + name
    else:
        addr = b"\0\0\0\0"
    s.sendall(bytes([5, cmd, 0, atyp]) + addr + port.to_bytes(2, "big"))
    reply = recv_exact(s, 10)
    return (reply[1] if len(reply) == 10 else None), s


def code(gate_port, host, port, **kw):
    reply, s = socks(gate_port, host, port, **kw)
    s.close()
    return reply


class Resolver:
    """Names to address lists; anything else is unresolvable. Counts calls."""

    def __init__(self, table=None):
        self.table = dict(table or {})
        self.calls = []

    def __call__(self, name, port):
        self.calls.append(name)
        answer = self.table.get(name)
        if callable(answer):
            answer = answer(self.calls.count(name))
        if answer is None:
            raise socket.gaierror(socket.EAI_NONAME, "unknown")
        return list(answer)


class Connector:
    """Records every address the gate asks for; connects only to loopback
    (never the network)."""

    def __init__(self):
        self.seen = []

    async def __call__(self, address, port):
        self.seen.append((address, port))
        if P.address_class(__import__("ipaddress").ip_address(address)) != "loopback":
            raise ConnectionRefusedError("the suite never leaves loopback")
        return await G.default_connect(address, port)


# The spellings of "this machine" a client can send for a host.
NAMES = ["localhost", "foo.localhost", "LOCALHOST.", "a.b.localhost", "127.0.0.1", "127.1",
         "0x7f000001", "2130706433", "0177.0.0.1", "127.0.0.2", "0.0.0.0", "::1", "[::1]",
         "::ffff:127.0.0.1", "::ffff:7f00:1", "0:0:0:0:0:0:0:1"]
ADDRESSES = [(1, "127.0.0.1"), (1, "127.9.9.9"), (1, "0.0.0.0"), (4, "::1"), (4, "::ffff:127.0.0.1")]


class Base(unittest.TestCase):
    def setUp(self):
        self.lan = [False]
        self.resolver = Resolver()
        self.connector = Connector()
        self.stand_ins = {name: Listener() for name in
                          ("FACE_PORT", "DAEMON_PORT", "DESKTOP_PORT", "SPOTIFY_AUTH_PORT", "hud", "api")}
        for listener in self.stand_ins.values():
            self.addCleanup(listener.close)
        for name in ("FACE_PORT", "DAEMON_PORT", "DESKTOP_PORT", "SPOTIFY_AUTH_PORT"):
            guard = patch.object(config, name, self.stand_ins[name].port)
            guard.start()
            self.addCleanup(guard.stop)
        self.dev = Listener(echo=True)
        self.addCleanup(self.dev.close)
        self.workshop = Listener()
        self.addCleanup(self.workshop.close)
        # The loopback ports the owner opened in a pane (the host's allowance).
        self.opened = {self.dev.port, self.workshop.port}
        self.gate = self.make_gate()

    def make_gate(self, **kw):
        hud, api = self.stand_ins["hud"].port, self.stand_ins["api"].port
        kw.setdefault("loopback", lambda port: port in self.opened)
        gate = G.Gate(refused=lambda: P.refused_ports(hud_port=hud, api_port=api,
                                                     workshop_port=self.workshop.port),
                      lan=lambda: self.lan[0], resolver=self.resolver, connector=self.connector, **kw)
        port = gate.start()
        self.assertNotIn(port, LIVE_PORTS)
        self.addCleanup(gate.stop)
        return gate

    def jarvis_accepts(self):
        return {name: listener.accepts for name, listener in self.stand_ins.items() if listener.accepts}


# -- layer 1 --------------------------------------------------------------------------------

class URLJudge(unittest.TestCase):
    REFUSED = frozenset({4111, 4222})

    def test_allowed(self):
        for url in ("http://example.com/", "https://example.com/a?b#c", "about:blank", "ABOUT:BLANK",
                    "http://localhost:5173/", "http://127.0.0.1:3000/x", "http://[::1]:8080/",
                    "https://xn--nxasmq6b.com/", "  https://example.com/  "):
            with self.subTest(url=url):
                self.assertTrue(P.judge_url(url, refused=self.REFUSED))

    def test_refused_and_never_echoed(self):
        cases = {
            "scheme": ["file:///etc/passwd", "FILE:///etc/hostname", "chrome://settings",
                       "chrome-untrusted://new-tab-page/", "devtools://devtools/bundled/x.html",
                       "view-source:http://example.com/", "chrome-extension://abc/x.html",
                       "javascript:alert(1)", "JavaScript:void(0)", "data:text/html,<b>x</b>",
                       "about:config", "about:settings", "about:blank#x", "ftp://example.com/",
                       "ws://example.com/", "blob:http://example.com/1", "example.com", "//example.com"],
            "userinfo": ["http://user:pw@example.com/", "https://user@example.com/",
                         "http://a@127.0.0.1:3000/"],
            "malformed": ["http:///x", "http://a\\@b/", "http://", "http://ex\tample.com/",
                          "http://exa​mple.com/", "http://exam‮ple.com/", "http://example.com:99999/",
                          "http://x" + "a" * 20000, "", 7, None],
            P.JARVIS_PORT: [],
        }
        for port in sorted(config.control_ports() | self.REFUSED):
            for host in ("localhost", "127.0.0.1", "foo.localhost", "[::1]", "0.0.0.0", "127.1",
                         "0x7f000001", "2130706433", "[::ffff:127.0.0.1]", "localhost."):
                cases[P.JARVIS_PORT].append(f"http://{host}:{port}/")
        cases[P.JARVIS_PORT] += ["http://localhost/", "https://127.0.0.1/"] if 80 in self.REFUSED else []
        for reason, urls in cases.items():
            for url in urls:
                with self.subTest(url=url):
                    with self.assertRaises(P.URLRefused) as raised:
                        P.judge_url(url, refused=self.REFUSED)
                    self.assertEqual(raised.exception.reason, reason)
                    if isinstance(url, str) and len(url) > 8:
                        self.assertNotIn(url.strip(), str(raised.exception))
                        self.assertNotIn("example", str(raised.exception))

    def test_landed(self):
        for url in ("about:blank#top", "chrome-error://chromewebdata/", "blob:https://example.com/1-2",
                    "http://localhost:3000/"):
            with self.subTest(url=url):
                self.assertTrue(P.judge_url(url, refused=self.REFUSED, landed=True))
        for url in ("data:text/html,x", "file:///etc/passwd", "chrome://settings", "about:config",
                    "blob:file:///x", f"blob:http://localhost:{sorted(self.REFUSED)[0]}/1",
                    "http://localhost:8405/"):
            with self.subTest(url=url):
                with self.assertRaises(P.URLRefused):
                    P.judge_url(url, refused=self.REFUSED, landed=True)

    def test_refused_ports(self):
        with patch.object(config, "FACE_PORT", 9402), patch.object(config, "DAEMON_PORT", 9405):
            ports = P.refused_ports(hud_port=5001, api_port=5002, workshop_port=5003)
            self.assertTrue({8402, 8404, 8405, 8406, 9402, 9405, 5001, 5002, 8403} <= ports)
            self.assertNotIn(5003, ports)
            # The daemon's own workshop on 8403 is the one 8403 admitted.
            self.assertNotIn(8403, P.refused_ports(hud_port=8402, api_port=8405, workshop_port=8403))
            self.assertIn(8403, P.refused_ports(hud_port=8402, api_port=8405, workshop_port=0))


# -- layer 2 --------------------------------------------------------------------------------

class GateRefusals(Base):
    def test_every_spelling_of_every_jarvis_port_is_refused(self):
        sent = 0
        for listener in self.stand_ins.values():
            for name in NAMES:
                with self.subTest(name=name, port=listener.port):
                    self.assertEqual(code(self.gate.port, name, listener.port), G.REP_NOT_ALLOWED)
                    sent += 1
            for atyp, address in ADDRESSES:
                with self.subTest(address=address, port=listener.port):
                    self.assertEqual(code(self.gate.port, address, listener.port, atyp=atyp),
                                     G.REP_NOT_ALLOWED)
                    sent += 1
        self.lan[0] = True                  # the LAN switch never opens Jarvis's ports
        for listener in self.stand_ins.values():
            self.assertEqual(code(self.gate.port, "127.0.0.1", listener.port), G.REP_NOT_ALLOWED)
            sent += 1
        time.sleep(0.2)
        self.assertEqual(self.jarvis_accepts(), {}, "a stand-in for Jarvis's ports was reached")
        self.assertEqual(self.gate.stats()["refused"].get(P.JARVIS_PORT), sent)
        self.assertEqual(self.connector.seen, [], "nothing was even attempted")
        self.assertEqual(self.resolver.calls, [], "a refused port is refused before any DNS")

    def test_the_same_spellings_reach_an_ordinary_dev_port(self):
        # The sanity half: the refusal above is the port's, not the spelling's.
        # (Only 127.0.0.1 and ::1 listen: another 127/8 address is allowed but
        # finds nobody there.)
        names = [n for n in NAMES if n != "127.0.0.2"]
        addresses = [a for a in ADDRESSES if a[1] != "127.9.9.9"]
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(code(self.gate.port, name, self.dev.port), G.REP_OK)
        for atyp, address in addresses:
            with self.subTest(address=address):
                self.assertEqual(code(self.gate.port, address, self.dev.port, atyp=atyp), G.REP_OK)
        self.assertEqual(code(self.gate.port, "127.0.0.2", self.dev.port), G.REP_REFUSED)
        time.sleep(0.2)
        self.assertGreaterEqual(self.dev.accepts, len(names) + len(addresses))

    def test_loopback_is_closed_until_the_owner_opens_a_port(self):
        # The lead's decision on PR #69 (MED 2): this machine is closed like
        # the LAN. A port nobody opened is refused in every spelling, with the
        # LAN switch on or off; opening one opens that port only.
        closed, other = Listener(), Listener()
        self.addCleanup(closed.close)
        self.addCleanup(other.close)
        sent = 0
        for lan in (False, True):
            self.lan[0] = lan
            for name in ("127.0.0.1", "localhost", "foo.localhost", "::1", "0.0.0.0", "127.1"):
                with self.subTest(name=name, lan=lan):
                    self.assertEqual(code(self.gate.port, name, closed.port), G.REP_NOT_ALLOWED)
                    sent += 1
            self.assertEqual(code(self.gate.port, "::1", closed.port, atyp=4), G.REP_NOT_ALLOWED)
            sent += 1
        time.sleep(0.2)
        self.assertEqual(closed.accepts, 0)
        self.assertEqual(self.gate.stats()["refused"].get(P.LOOPBACK_CLOSED), sent)
        self.opened.add(closed.port)             # the owner opened a pane on it
        self.assertEqual(code(self.gate.port, "localhost", closed.port), G.REP_OK)
        self.assertEqual(code(self.gate.port, "127.0.0.1", other.port), G.REP_NOT_ALLOWED)
        time.sleep(0.2)
        self.assertGreater(closed.accepts, 0)
        self.assertEqual(other.accepts, 0)

    def no_cache(self):
        """Every connection asks the resolver again (the cache would answer
        the second one from the first)."""
        guard = patch.object(G, "CACHE_S", 0.0)
        guard.start()
        self.addCleanup(guard.stop)

    def test_a_name_keeps_the_address_class_it_first_resolved_to(self):
        # Rebinding with the LAN switch on: a public name later answering a
        # private address, and a private name later answering a public one.
        self.no_cache()
        self.lan[0] = True
        self.resolver.table["pin.example"] = lambda nth: ["93.184.216.34"] if nth == 1 else ["192.168.1.5"]
        self.resolver.table["lanpin.example"] = lambda nth: ["192.168.1.6"] if nth == 1 else ["93.184.216.35"]
        self.assertEqual(code(self.gate.port, "pin.example", 80), G.REP_REFUSED)       # allowed; no network
        self.assertEqual(code(self.gate.port, "pin.example", 80), G.REP_NOT_ALLOWED)   # rebound: refused
        self.assertEqual(code(self.gate.port, "PIN.example.", 80), G.REP_NOT_ALLOWED)  # any spelling
        self.assertEqual(code(self.gate.port, "lanpin.example", 80), G.REP_REFUSED)
        self.assertEqual(code(self.gate.port, "lanpin.example", 80), G.REP_NOT_ALLOWED)
        self.assertEqual(self.connector.seen, [("93.184.216.34", 80), ("192.168.1.6", 80)])
        self.assertEqual(self.gate.stats()["refused"].get(P.REBINDING), 3)

    def short_budget(self, seconds=1.5):
        guard = patch.object(G, "RESOLVE_TIMEOUT_S", seconds)
        guard.start()
        self.addCleanup(guard.stop)

    def ask_all(self, names, port=80):
        """Each name asked at once on its own connection; {name: reply}."""
        replies = {}

        def ask(name):
            replies[name] = code(self.gate.port, name, port)
        threads = [threading.Thread(target=ask, args=(n,)) for n in names]
        for t in threads:
            t.start()
        return replies, threads

    def test_a_black_holed_domain_cannot_starve_other_lookups(self):
        # A page aims lookups at a name server that never answers: one domain
        # holds at most DOMAIN_LOOKUPS resolver threads, the rest wait out
        # their budget and are refused, and another site still resolves at once.
        self.short_budget()
        release = threading.Event()
        self.addCleanup(release.set)

        def black_hole(nth):
            release.wait(30)
            return None
        names = [f"n{i}.blackhole.test" for i in range(40)]
        for name in names:
            self.resolver.table[name] = black_hole
        self.resolver.table["ok.example"] = ["93.184.216.34"]
        replies, threads = self.ask_all(names)
        time.sleep(0.5)
        started = time.monotonic()
        self.assertEqual(code(self.gate.port, "ok.example", 80), G.REP_REFUSED)
        self.assertLess(time.monotonic() - started, 1.0, "another site waited on the black hole")
        self.assertIn(("93.184.216.34", 80), self.connector.seen)
        for t in threads:
            t.join(15)
        self.assertEqual(set(replies.values()), {G.REP_HOST_UNREACHABLE})
        self.assertEqual(sum(1 for n in self.resolver.calls if n.endswith(".blackhole.test")),
                         G.DOMAIN_LOOKUPS, "the black hole held more than its domain's threads")
        self.assertGreaterEqual(self.gate.stats()["refused"].get(P.DNS_BUSY, 0), 40 - G.DOMAIN_LOOKUPS)
        release.set()
        # The same name asked twice at once is one lookup.
        slow = threading.Event()
        self.resolver.table["twice.example"] = lambda nth: (slow.wait(2), ["93.184.216.36"])[1]
        pair = [threading.Thread(target=code, args=(self.gate.port, "twice.example", 80)) for _ in range(2)]
        for t in pair:
            t.start()
        time.sleep(0.3)
        slow.set()
        for t in pair:
            t.join(10)
        self.assertEqual(self.resolver.calls.count("twice.example"), 1)

    def test_many_names_in_one_domain_wait_for_a_slot_rather_than_fail(self):
        # Delta review of PR #69: eight images on one CDN at 60–80 ms each,
        # and six of eight were refused `dns_busy` at once. Now they wait.
        busy, peak, lock = [0], [0], threading.Lock()

        def slow(nth, i):
            with lock:
                busy[0] += 1
                peak[0] = max(peak[0], busy[0])
            time.sleep(0.3)
            with lock:
                busy[0] -= 1
            return [f"93.184.216.{100 + i}"]
        names = [f"i{i}.img.test" for i in range(8)]
        for i, name in enumerate(names):
            self.resolver.table[name] = lambda nth, i=i: slow(nth, i)
        replies, threads = self.ask_all(names)
        for t in threads:
            t.join(15)
        self.assertEqual(replies, {n: G.REP_REFUSED for n in names})      # all allowed; no network
        self.assertEqual(self.gate.stats()["refused"].get(P.DNS_BUSY, 0), 0)
        self.assertEqual(peak[0], G.DOMAIN_LOOKUPS, "one domain held more threads than its slots")

    def test_a_site_under_a_shared_suffix_has_slots_of_its_own(self):
        # Every `*.co.uk` used to share two slots (the last two labels): two
        # hanging lookups under evil.co.uk refused www.bbc.co.uk at once.
        self.short_budget()
        release = threading.Event()
        self.addCleanup(release.set)
        hang = lambda nth: (release.wait(30), None)[1]  # noqa: E731
        names = [f"x{i}.evil.co.uk" for i in range(6)]
        for name in names:
            self.resolver.table[name] = hang
        self.resolver.table["www.bbc.co.uk"] = ["93.184.216.40"]
        replies, threads = self.ask_all(names)
        time.sleep(0.3)
        started = time.monotonic()
        self.assertEqual(code(self.gate.port, "www.bbc.co.uk", 80), G.REP_REFUSED)
        self.assertLess(time.monotonic() - started, 1.0, "bbc.co.uk waited on evil.co.uk")
        release.set()
        for t in threads:
            t.join(15)
        for name, key in (("www.bbc.co.uk", "bbc.co.uk"), ("a.b.evil.co.uk", "evil.co.uk"),
                          ("me.github.io", "me.github.io"), ("x.me.github.io", "me.github.io"),
                          ("a.b.example.com", "example.com"), ("example.com", "example.com"),
                          ("shop.example.de", "example.de"), ("www.foo.ne.jp", "foo.ne.jp"),
                          ("a.b.example.uk", "b.example.uk"), ("WWW.BBC.CO.UK.", "bbc.co.uk")):
            with self.subTest(name=name):
                self.assertEqual(G.domain_key(name), key)

    def test_a_cached_answer_is_judged_like_a_new_one(self):
        # A positive answer is reused for CACHE_S: one resolver call, every
        # connection still judged — the LAN switch and the pin included.
        self.lan[0] = True
        self.resolver.table["cached.example"] = ["93.184.216.41"]
        self.resolver.table["lan.example"] = ["192.168.1.8"]
        self.resolver.table["mixed.example"] = ["93.184.216.42", "192.168.1.9"]
        for _ in range(3):
            self.assertEqual(code(self.gate.port, "cached.example", 80), G.REP_REFUSED)
        self.assertEqual(self.resolver.calls.count("cached.example"), 1)
        self.assertEqual(self.connector.seen.count(("93.184.216.41", 80)), 3)
        self.assertEqual(code(self.gate.port, "lan.example", 80), G.REP_REFUSED)       # LAN on: allowed
        self.lan[0] = False
        self.assertEqual(code(self.gate.port, "lan.example", 80), G.REP_NOT_ALLOWED)   # cached, judged again
        self.lan[0] = True
        for _ in range(2):        # the pin (public) holds on the cached mixed answer too
            self.assertEqual(code(self.gate.port, "mixed.example", 80), G.REP_REFUSED)
        self.assertEqual(self.resolver.calls.count("lan.example") + self.resolver.calls.count("mixed.example"), 2)
        self.assertNotIn(("192.168.1.9", 80), self.connector.seen)
        with patch.object(G, "CACHE_S", 0.2):
            self.resolver.table["brief.example"] = ["93.184.216.43"]
            code(self.gate.port, "brief.example", 80)
            time.sleep(0.3)
            code(self.gate.port, "brief.example", 80)
        self.assertEqual(self.resolver.calls.count("brief.example"), 2, "an expired answer was reused")

    def test_a_young_pin_is_never_forgotten_to_make_room(self):
        # Delta review of PR #69: after 8,193 other names the oldest pin was
        # dropped, and a pinned-public name then reached a private address
        # with the LAN switch on.
        self.no_cache()
        self.lan[0] = True
        cap = patch.object(G, "PIN_CAP", 4)
        cap.start()
        self.addCleanup(cap.stop)
        self.resolver.table["victim.example"] = lambda nth: ["93.184.216.50"] if nth == 1 else ["192.168.1.50"]
        self.assertEqual(code(self.gate.port, "victim.example", 80), G.REP_REFUSED)    # pinned public
        for i in range(6):                                                              # fill the table
            self.resolver.table[f"f{i}.example"] = [f"93.184.216.{60 + i}"]
            self.assertEqual(code(self.gate.port, f"f{i}.example", 80), G.REP_REFUSED)
        self.assertEqual(code(self.gate.port, "victim.example", 80), G.REP_NOT_ALLOWED)  # still pinned
        self.assertEqual(self.gate.stats()["refused"].get(P.REBINDING), 1)
        # Full of young pins: an unpinned name may not reach private space.
        self.resolver.table["fresh.example"] = ["192.168.1.51"]
        self.assertEqual(code(self.gate.port, "fresh.example", 80), G.REP_NOT_ALLOWED)
        self.assertEqual(self.gate.stats()["refused"].get(P.PINS_FULL), 1)
        self.assertNotIn(("192.168.1.50", 80), self.connector.seen)
        self.assertNotIn(("192.168.1.51", 80), self.connector.seen)
        # Once pins are old enough, the oldest gives way again.
        with patch.object(G, "PIN_YOUNG_S", 0.0):
            self.assertEqual(code(self.gate.port, "fresh.example", 80), G.REP_REFUSED)
        self.assertIn(("192.168.1.51", 80), self.connector.seen)

    def test_the_workshop_is_admitted_only_as_the_reported_one(self):
        self.assertEqual(code(self.gate.port, "127.0.0.1", self.workshop.port), G.REP_OK)
        ports = P.refused_ports(hud_port=1, api_port=2, workshop_port=self.workshop.port)
        self.assertIn(8403, ports, "the live workshop's default port, never connected to here")

    def test_its_own_port(self):
        for name in ("127.0.0.1", "localhost", "::1"):
            self.assertEqual(code(self.gate.port, name, self.gate.port), G.REP_NOT_ALLOWED)
        self.assertEqual(self.gate.stats()["refused"].get(P.OWN_PORT), 3)

    def test_a_name_that_resolves_to_loopback_is_refused_whatever_the_lan_switch(self):
        self.resolver.table.update({"rebind.example": ["127.0.0.1"], "six.example": ["::1"],
                                    "mapped.example": ["::ffff:127.0.0.1"], "any.example": ["0.0.0.0"]})
        for lan in (False, True):
            self.lan[0] = lan
            for name in ("rebind.example", "six.example", "mapped.example", "any.example"):
                with self.subTest(name=name, lan=lan):
                    self.assertEqual(code(self.gate.port, name, self.dev.port), G.REP_NOT_ALLOWED)
        time.sleep(0.2)
        self.assertEqual(self.dev.accepts, 0)
        self.assertEqual(self.gate.stats()["refused"].get(P.NAME_TO_LOOPBACK), 8)

    def test_rebinding_resolves_once_and_connects_to_what_it_judged(self):
        # First answer public (allowed), every later one loopback: a gate that
        # resolved again at connect time would reach the dev listener.
        self.resolver.table["flip.example"] = lambda nth: ["93.184.216.34"] if nth == 1 else ["127.0.0.1"]
        reply = code(self.gate.port, "flip.example", self.dev.port)
        time.sleep(0.2)
        self.assertEqual(self.resolver.calls.count("flip.example"), 1, "one resolution per connection")
        self.assertEqual(self.connector.seen, [("93.184.216.34", self.dev.port)])
        self.assertEqual(self.dev.accepts, 0, "the rebound address was connected to")
        self.assertEqual(reply, G.REP_REFUSED)

    def test_a_mixed_answer_connects_only_to_the_allowed_addresses(self):
        self.resolver.table["mixed.example"] = ["127.0.0.1", "192.168.1.9", "93.184.216.34"]
        self.assertEqual(code(self.gate.port, "mixed.example", self.dev.port), G.REP_REFUSED)
        self.assertEqual(self.connector.seen, [("93.184.216.34", self.dev.port)])
        self.assertEqual(self.dev.accepts, 0)

    def test_the_lan_switch(self):
        private = [(1, "192.168.1.20"), (1, "10.0.0.1"), (1, "172.16.0.5"), (1, "169.254.169.254"),
                   (1, "100.64.0.1"), (4, "fd00::1"), (4, "fe80::1"), (3, "192.168.0.1"),
                   (3, "printer.lan")]
        self.resolver.table["printer.lan"] = ["192.168.1.5"]
        for atyp, host in private:
            with self.subTest(host=host, lan=False):
                self.assertEqual(code(self.gate.port, host, 80, atyp=atyp), G.REP_NOT_ALLOWED)
        self.assertEqual(self.connector.seen, [])
        self.assertEqual(self.gate.stats()["refused"].get(P.PRIVATE_NETWORK), len(private))
        self.lan[0] = True
        for atyp, host in private:
            with self.subTest(host=host, lan=True):
                # Allowed: the connector is asked (and, in this suite, refuses).
                self.assertEqual(code(self.gate.port, host, 80, atyp=atyp), G.REP_REFUSED)
        self.assertEqual(len(self.connector.seen), len(private))

    def test_not_a_host(self):
        for atyp, host in [(1, "224.0.0.1"), (1, "255.255.255.255"), (1, "240.0.0.1"), (1, "0.1.2.3"),
                           (4, "ff02::1"), (4, "::7f00:1"), (3, "239.1.1.1")]:
            with self.subTest(host=host):
                self.assertEqual(code(self.gate.port, host, 80, atyp=atyp), G.REP_NOT_ALLOWED)
        self.lan[0] = True
        self.assertEqual(code(self.gate.port, "224.0.0.1", 80, atyp=1), G.REP_NOT_ALLOWED)
        self.assertEqual(self.connector.seen, [])

    def test_unresolvable_and_bad_names(self):
        self.resolver.table["empty.example"] = []
        self.assertEqual(code(self.gate.port, "nowhere.invalid", 80), G.REP_HOST_UNREACHABLE)
        self.assertEqual(code(self.gate.port, "empty.example", 80), G.REP_HOST_UNREACHABLE)
        for raw in (b"exa mple.com", b"a..b", b"\xff\xfe.example", b"x/y", b"", b"[::1", b".lead"):
            with self.subTest(raw=raw):
                self.assertEqual(code(self.gate.port, "", 80, raw_name=raw), G.REP_NOT_ALLOWED)
        self.assertEqual(self.connector.seen, [])
        stats = self.gate.stats()["refused"]
        self.assertEqual(stats.get(P.UNRESOLVABLE), 2)
        self.assertEqual(stats.get(P.BAD_REQUEST), 7)

    def test_public_addresses_go_to_the_connector_in_order(self):
        self.resolver.table["example.com"] = ["93.184.216.34", "2606:2800:220:1::1"]
        self.assertEqual(code(self.gate.port, "93.184.216.34", 443, atyp=1), G.REP_REFUSED)
        self.assertEqual(code(self.gate.port, "example.com", 443), G.REP_REFUSED)
        self.assertEqual(self.connector.seen, [("93.184.216.34", 443), ("93.184.216.34", 443),
                                               ("2606:2800:220:1::1", 443)])


class Protocol(Base):
    def test_every_atyp(self):
        self.assertEqual(code(self.gate.port, "127.0.0.1", self.dev.port, atyp=1), G.REP_OK)
        self.assertEqual(code(self.gate.port, "::1", self.dev.port, atyp=4), G.REP_OK)
        self.assertEqual(code(self.gate.port, "localhost", self.dev.port, atyp=3), G.REP_OK)
        self.assertEqual(code(self.gate.port, "", self.dev.port, atyp=5), G.REP_BAD_ATYP)
        self.assertEqual(code(self.gate.port, "", self.dev.port, atyp=2), G.REP_BAD_ATYP)

    def test_connect_only(self):
        for cmd in (2, 3, 9):
            with self.subTest(cmd=cmd):
                self.assertEqual(code(self.gate.port, "127.0.0.1", self.dev.port, cmd=cmd),
                                 G.REP_BAD_COMMAND)
        time.sleep(0.2)
        self.assertEqual(self.dev.accepts, 0)

    def test_no_authentication_or_nothing(self):
        reply, s = socks(self.gate.port, "127.0.0.1", self.dev.port, methods=b"\x02")
        self.assertEqual(reply, b"\x05\xff")
        s.close()
        s = socket.create_connection(("127.0.0.1", self.gate.port), timeout=5)
        s.sendall(b"\x04\x01\x00\x50\x7f\x00\x00\x01\x00")          # SOCKS4
        self.assertEqual(recv_exact(s, 8), b"")
        s.close()

    def test_a_dev_port_is_piped_through_both_ways(self):
        payload = os.urandom(2 * 1024 * 1024)
        for atyp, host in ((3, "localhost"), (1, "127.0.0.1"), (4, "::1")):
            with self.subTest(host=host):
                reply, s = socks(self.gate.port, host, self.dev.port, atyp=atyp)
                self.assertEqual(reply, G.REP_OK)
                got = bytearray()

                def read():
                    while True:
                        chunk = s.recv(65536)
                        if not chunk:
                            return
                        got.extend(chunk)
                reader = threading.Thread(target=read)
                reader.start()
                s.sendall(payload)
                s.shutdown(socket.SHUT_WR)         # the half-close carries through
                reader.join(20)
                s.close()
                self.assertEqual(bytes(got), payload)
        self.assertEqual(self.gate.stats()["allowed"], 3)

    def test_last_lease_revokes_existing_tunnels_for_every_local_spelling(self):
        for name in ("localhost", "127.0.0.1", "0.0.0.0", "::ffff:127.0.0.1"):
            with self.subTest(name=name):
                self.opened.add(self.dev.port)
                reply, sock = socks(self.gate.port, name, self.dev.port)
                self.addCleanup(sock.close)
                self.assertEqual(reply, G.REP_OK)
                sock.sendall(b"before")
                self.assertEqual(recv_exact(sock, 6), b"before")
                self.opened.remove(self.dev.port)
                self.gate.revoke_closed_loopback()
                sock.settimeout(2)
                self.assertEqual(sock.recv(1), b"", "a revoked local tunnel stayed open")
                self.assertEqual(code(self.gate.port, name, self.dev.port), G.REP_NOT_ALLOWED)
                sock.close()

    def test_revocation_preserves_another_authorized_port(self):
        keep = Listener(echo=True)
        self.addCleanup(keep.close)
        self.opened.add(keep.port)
        reply, sock = socks(self.gate.port, "localhost", keep.port)
        self.addCleanup(sock.close)
        self.assertEqual(reply, G.REP_OK)
        self.opened.discard(self.dev.port)
        self.gate.revoke_closed_loopback()
        sock.sendall(b"keep")
        self.assertEqual(recv_exact(sock, 4), b"keep")

    def test_local_servers_override_revokes_when_turned_off(self):
        enabled=[True]
        self.opened.remove(self.dev.port)
        self.gate._loopback=lambda port: enabled[0] or port in self.opened
        reply,sock=socks(self.gate.port,"localhost",self.dev.port)
        self.addCleanup(sock.close)
        self.assertEqual(reply,G.REP_OK)
        self.gate.revoke_closed_loopback()
        sock.sendall(b"still allowed")
        self.assertEqual(recv_exact(sock,13),b"still allowed")
        enabled[0]=False
        self.gate.revoke_closed_loopback()
        sock.settimeout(2)
        self.assertEqual(sock.recv(1),b"")
        self.assertEqual(code(self.gate.port,"localhost",self.dev.port),G.REP_NOT_ALLOWED)

    def test_permission_is_rechecked_after_connect_await(self):
        entered = threading.Event()
        release = threading.Event()
        real = self.gate._connector
        async def paused(address, port):
            connected = await real(address, port)
            entered.set()
            await asyncio.to_thread(release.wait, 3)
            return connected
        self.gate._connector = paused
        result = []
        caller = threading.Thread(target=lambda: result.append(code(self.gate.port, "127.0.0.1", self.dev.port)))
        caller.start()
        try:
            self.assertTrue(entered.wait(3))
            self.opened.remove(self.dev.port)
            # No revoke callback here: catch the dispatch-time race independently.
            release.set()
            caller.join(3)
            self.assertEqual(result, [G.REP_NOT_ALLOWED])
        finally:
            release.set()
            caller.join(3)

    def test_the_connection_cap(self):
        gate = self.make_gate(max_connections=1)
        held = socket.create_connection(("127.0.0.1", gate.port), timeout=5)
        time.sleep(0.1)
        second = socket.create_connection(("127.0.0.1", gate.port), timeout=5)
        second.sendall(b"\x05\x01\x00")
        self.assertEqual(recv_exact(second, 2), b"")
        second.close()
        held.close()
        self.assertEqual(gate.stats()["refused"].get(P.BUSY), 1)

    def test_the_log_names_classes_never_hosts(self):
        records = []

        class Keep(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())
        handler = Keep(level=logging.DEBUG)
        logger = logging.getLogger("jarvis")
        old = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            self.resolver.table["secret-host-name.example"] = ["127.0.0.1"]
            code(self.gate.port, "secret-host-name.example", self.dev.port)
            code(self.gate.port, "another-secret-name.invalid", 80)
            code(self.gate.port, "10.11.12.13", 80, atyp=1)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old)
        self.assertTrue(records)
        for message in records:
            for secret in ("secret-host-name", "another-secret-name", "10.11.12.13"):
                self.assertNotIn(secret, message)


class NoToolReachesTheBrowser(unittest.TestCase):
    """No tool, MCP tool or fast-path module may reach the owner's Browser:
    only the daemon builds the host, and only the browser package itself
    names its engine or its gate (`terminal_check`'s rule, for the Browser;
    review of PR #69). `hud_api` mounts `browser.routes`, which are
    owner-only."""

    REPO = Path(__file__).resolve().parents[2]
    ALLOWED = {"jarvis/v2/daemon.py"}           # and the browser package itself
    PACKAGE = "jarvis/v2/browser/"
    NEEDLES = __import__("re").compile(
        r"browser\.(host|gate)\b|browser\s+import\s+[^\n]*\b(host|gate)\b"
        r"|\bBrowserHost\b|\bGate\(|['\"]/browser\b"
        r"|\.browser\.(open_tab|navigate|history|close_tab|tabs|forget|close_profile|loopback_open)\b")

    def test_only_the_daemon_and_the_browser_package_name_it(self):
        hits = []
        for path in sorted((self.REPO / "jarvis").rglob("*.py")):
            rel = str(path.relative_to(self.REPO))
            if rel in self.ALLOWED or rel.startswith(self.PACKAGE):
                continue
            text = path.read_text(encoding="utf-8")
            for match in self.NEEDLES.finditer(text):
                if rel == "jarvis/v2/hud_api.py" and match.group(0) in ("'/browser", '"/browser'):
                    continue
                hits.append(f"{rel}: {match.group(0)}")
        self.assertEqual(hits, [], "a module outside the daemon reaches the owner's Browser")

    def test_the_daemon_builds_it_and_hud_api_mounts_only_the_routes(self):
        daemon = (self.REPO / "jarvis/v2/daemon.py").read_text()
        self.assertIn("from .browser.host import BrowserHost", daemon)
        hud = (self.REPO / "jarvis/v2/hud_api.py").read_text()
        self.assertIn("from .browser import routes as _browser", hud)
        self.assertNotRegex(hud, r"browser\.(host|gate)|BrowserHost")


if __name__ == "__main__":
    unittest.main(verbosity=2)
