"""The owner's Browser with real headless Google Chrome (BR-1a; plan §11).

One port-0 daemon, its Browser driven through the owner-only `/browser`
routes on the HUD listener (and, for the attacks, Playwright calls submitted
to the Browser's own thread). What it proves:

- **twelve ways into the HUD**, each with Jarvis's ports stood in for by
  listeners of the suite's own on ephemeral ports (configured as the control
  ports through `config`): typed in every spelling, a 302, a meta refresh,
  `location=`, an iframe, fetch, XHR, a WebSocket, a service worker's fetch,
  `<img>`, a form POST and `window.open`, from Main and from a Private
  context. **Every stand-in's accept counter stays 0**, while the same pages
  aimed at an ordinary listener reach it by every one of the twelve ways
  (the sanity half: the attacks really fire, through the gate);
- **loopback is closed until the owner opens a port** (review of PR #69,
  MED 2): a "public" page (`pub.test`, which the suite's resolver answers
  with a public address and its connector carries back to the fixture)
  cannot reach a CORS-permissive loopback server, nor can the attacks; once
  the owner opens `http://localhost:<p>/` in a tab, that port works and
  another loopback port is still refused;
- **no UDP leaves Chrome** (review of PR #69, HIGH): WebRTC gathers no ICE
  candidate and a loopback UDP sink hears nothing (STUN, TURN over UDP, a
  remote host candidate, WebTransport). Run inside a private network
  namespace with `JARVIS_NETNS_SNIFF=1`, an AF_PACKET sniffer also proves
  that no packet at all (mDNS, IGMP, DNS) leaves on an interface but `lo`;
- the refused schemes and a user name in the address, typed; the landing
  guard replacing a page that lands where the judge refuses; pop-ups landing
  on their own allowed page (MED 1), and a pop-up sent to Jarvis's port
  still replaced;
- every route owner-only and on a strict Host: the API listener, a write
  with no Origin, a foreign Origin;
- the folder: 0700 throughout, `profiles.json` 0600, on every credential
  list; a symlinked or open folder refuses to start; `browser.json` 0600 and
  protected state;
- persistence across a Chrome restart, and "Sign out of everything";
- the curated launch flags, the sandbox (a renderer under seccomp), no
  DevTools port, and a clean environment;
- no Chrome left after `stop()`; v1's `SESSION` never started; no URL or
  title on the bus or in the log; the Chrome tree's memory under 1.5 GiB.

**Hard rules** (the brief's): one Chrome instance at a time, at most 3 tabs
(`BROWSER_TAB_CAP=3`), `BROWSER_DIR` a temp folder, local fixture servers on
ephemeral ports only, and never 8402, 8403 or 8405. The gate's resolver is
injected and answers only `pub.test` (a public address the connector carries
back to loopback), and its connector refuses every other address but
loopback: nothing reaches DNS or the network, Chrome's own background
traffic included. Run it under the memory cap:

    systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 \\
        env PYTHONPATH=. .venv/bin/python tests/v2/owner_browser_check.py

and, for the sniffer, inside a user + network + mount namespace with only
`lo` and a dummy interface (the review's `netns_setup.sh`):

    systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 \\
        unshare --user --net --mount --keep-caps sh netns_setup.sh <dir> \\
        env JARVIS_NETNS_SNIFF=1 PYTHONPATH=. .venv/bin/python tests/v2/owner_browser_check.py
"""
from __future__ import annotations

import asyncio
import html
import http.client
import json
import logging
import os
from pathlib import Path
import secrets
import signal
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis import config, runtime, tools  # noqa: E402
from jarvis.tools import files as v1_files  # noqa: E402
from jarvis.v2 import editor_policy, folders, permissions  # noqa: E402
from jarvis.v2.browser import gate as G, host as H, policy as P  # noqa: E402
from jarvis.v2.daemon import Daemon  # noqa: E402
from jarvis.v2.model import ProviderName as PN  # noqa: E402
from jarvis.v2.provider import Event, EventKind as K, SessionHandle, Usage  # noqa: E402
from jarvis.v2.stores import Stores  # noqa: E402

from browser_gate_check import Listener  # noqa: E402

LIVE_PORTS = {8402, 8403, 8405}
MARK = "MARKERz7q"          # in every fixture title and path: must never reach a log or the bus
PUB = "93.184.216.34"       # what the suite's resolver says `pub.test` is (never dialled)
SNIFF = os.environ.get("JARVIS_NETNS_SNIFF") == "1"
PSS_LIMIT = 1.5 * 1024 ** 3
WAYS = ("iframe", "img", "fetch", "xhr", "ws", "form", "sw", "open")
NAV_WAYS = ("302", "meta", "loc")
ENV_ALLOWED = {"HOME", "PATH", "USER", "LOGNAME", "LANG", "LANGUAGE", "TZ", "FONTCONFIG_FILE"}


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
        yield Event(K.TURN_FINISHED, handle.thread_id, {"stop": "end"})

    def interrupt(self, handle):
        pass

    def close(self, handle):
        pass


class UdpSink:
    """A UDP socket on 127.0.0.1 and ::1 (one port) that counts datagrams."""

    def __init__(self):
        self.datagrams = 0
        self._lock = threading.Lock()
        self._socks = []
        for _ in range(40):
            s4 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s4.bind(("127.0.0.1", 0))
            port = s4.getsockname()[1]
            s6 = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            s6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            try:
                s6.bind(("::1", port))
            except OSError:
                s4.close()
                s6.close()
                continue
            break
        else:
            raise RuntimeError("no UDP port free on both loopbacks")
        self.port = port
        for s in (s4, s6):
            self._socks.append(s)
            threading.Thread(target=self._read, args=(s,), daemon=True).start()

    def _read(self, s):
        while True:
            try:
                s.recvfrom(65535)
            except OSError:
                return
            with self._lock:
                self.datagrams += 1

    def close(self):
        for s in self._socks:
            try:
                s.close()
            except OSError:
                pass


class Sniffer:
    """Every packet sent on any interface (AF_PACKET, PACKET_OUTGOING): only
    inside the private network namespace, where the suite holds CAP_NET_RAW.
    Ambient capabilities are dropped first, so Chrome starts with none."""

    def __init__(self):
        import ctypes
        self.packets: list[tuple] = []
        self.label = "setup"                  # the test running when a packet was sent
        self._lock = threading.Lock()
        self._sock = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM, socket.htons(3))
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(47, 4, 0, 0, 0) != 0:          # PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL
            raise OSError(ctypes.get_errno(), "could not clear ambient capabilities")
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        import ipaddress
        while True:
            try:
                data, addr = self._sock.recvfrom(65535)
            except OSError:
                return
            if addr[2] != 4:                          # PACKET_OUTGOING only
                continue
            proto, dst, dport = None, None, None
            if addr[1] == 0x0800 and len(data) >= 20:
                off, proto, dst = (data[0] & 0xF) * 4, data[9], str(ipaddress.IPv4Address(data[16:20]))
            elif addr[1] == 0x86DD and len(data) >= 40:
                off, proto, dst = 40, data[6], str(ipaddress.IPv6Address(data[24:40]))
            else:
                continue
            if proto in (6, 17) and len(data) >= off + 4:
                dport = int.from_bytes(data[off + 2:off + 4], "big")
            elif proto == 2 and len(data) >= off + 16 and data[off] == 0x22:
                # An IGMPv3 report: name the first group it joins or leaves.
                dport = str(ipaddress.IPv4Address(data[off + 12:off + 16]))
            with self._lock:
                self.packets.append((addr[0], proto, dst, dport, self.label))

    def snapshot(self):
        with self._lock:
            return list(self.packets)

    def close(self):
        self._sock.close()


class Sanity(Listener):
    """An ordinary local server: answers every request with a tiny 200 that
    any origin may read (CORS and Private/Local Network Access preflights
    included) and records which way (`?w=`) each one came by."""

    def __init__(self):
        self.ways: set[str] = set()
        super().__init__()

    def _accept(self, s):
        while True:
            try:
                conn, _ = s.accept()
            except OSError:
                return
            with self._lock:
                self.accepts += 1
            threading.Thread(target=self._answer, args=(conn,), daemon=True).start()

    def _answer(self, conn):
        with conn:
            conn.settimeout(3)
            data = b""
            try:
                while b"\r\n\r\n" not in data and len(data) < 16384:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                line = data.split(b"\r\n", 1)[0].decode("latin-1")
                target = line.split(" ")[1] if line.count(" ") >= 2 else ""
                way = parse_qs(urlsplit(target).query).get("w", [""])[0]
                if way:
                    with self._lock:
                        self.ways.add(way)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                             b"Access-Control-Allow-Origin: *\r\n"
                             b"Access-Control-Allow-Methods: *\r\nAccess-Control-Allow-Headers: *\r\n"
                             b"Access-Control-Allow-Private-Network: true\r\n"
                             b"Access-Control-Allow-Local-Network: true\r\n"
                             b"Content-Length: 2\r\nConnection: close\r\n\r\nok")
            except OSError:
                pass


# A page reading a loopback server it was not served from: a CORS read and a
# no-cors POST (the shapes Local Network Access is about).
LNA_PAGE = """<!doctype html><title>lna</title><script>
const T = __T__;
window.__r = {};
const settle = () => { if ('cors' in __r && 'post' in __r) __r.done = true; };
fetch(`http://${T}/hit?w=lna-cors`).then(r => r.text())
  .then(t => { __r.cors = 'read:' + t; settle(); }).catch(() => { __r.cors = 'err'; settle(); });
fetch(`http://${T}/hit?w=lna-post`, {method: 'POST', mode: 'no-cors', body: 'x'})
  .then(() => { __r.post = 'sent'; settle(); }).catch(() => { __r.post = 'err'; settle(); });
</script>"""

# Every UDP way out of a page the review found or thought of: STUN and TURN
# over UDP to a loopback sink, a remote host candidate there (connectivity
# checks), a local peer pair with a data channel (host candidates, which
# Chrome announces over mDNS), TURN over TCP to a Jarvis stand-in and to a
# closed loopback port, and WebTransport (QUIC).
RTC_PAGE = """<!doctype html><title>rtc</title><script>
const U = __U__, S = __S__, C = __C__;
window.__r = {cands: [], states: [], err: null, wt: [], done: false};
(async () => {
 try {
  const ice = [
   {urls: `stun:127.0.0.1:${U}`}, {urls: `stun:[::1]:${U}`},
   {urls: `turn:127.0.0.1:${U}?transport=udp`, username: 'u', credential: 'p'},
   {urls: `turn:127.0.0.1:${S}?transport=tcp`, username: 'u', credential: 'p'},
   {urls: `turn:127.0.0.1:${C}?transport=tcp`, username: 'u', credential: 'p'},
  ];
  const a = new RTCPeerConnection({iceServers: ice});
  const b = new RTCPeerConnection();
  a.onicecandidate = e => { if (e.candidate && e.candidate.candidate) { __r.cands.push(e.candidate.candidate); b.addIceCandidate(e.candidate).catch(() => {}); } };
  b.onicecandidate = e => { if (e.candidate && e.candidate.candidate) { __r.cands.push(e.candidate.candidate); a.addIceCandidate(e.candidate).catch(() => {}); } };
  a.oniceconnectionstatechange = () => __r.states.push(a.iceConnectionState);
  a.createDataChannel('x');
  await a.setLocalDescription(await a.createOffer());
  await b.setRemoteDescription(a.localDescription);
  await b.setLocalDescription(await b.createAnswer());
  await a.setRemoteDescription(b.localDescription);
  for (const ip of ['127.0.0.1', '::1']) {
   try { await a.addIceCandidate({candidate: `candidate:9 1 udp 2122260223 ${ip} ${U} typ host`, sdpMid: '0', sdpMLineIndex: 0}); }
   catch (e) { __r.err = 'add'; }
  }
 } catch (e) { __r.err = String(e).slice(0, 80); }
 for (const h of ['127.0.0.1', '[::1]']) {
  try { const w = new WebTransport(`https://${h}:${U}/`); w.ready.catch(() => __r.wt.push(h)); w.closed.catch(() => {}); }
  catch (e) { __r.wt.push(h + ' threw'); }
 }
 setTimeout(() => { __r.done = true; }, 6000);
})();
</script>"""


def fixture_handler(state):
    class Fixture(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, body, ctype="text/html", headers=()):
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            self._send(200, "ok")

        def do_GET(self):
            url = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            target = q.get("t", "")
            hit = lambda way: f"http://{target}/hit?w={way}"  # noqa: E731
            path = url.path
            if path == f"/{MARK}/page":
                return self._send(200, f"<title>{MARK} page</title><p>{MARK}")
            if path == "/multi":
                n = q.get("n", "0")
                js = json.dumps(target)
                return self._send(200, f"""<!doctype html><title>{MARK} multi</title>
<iframe name="sink" style="display:none"></iframe>
<button id="open" onclick='window.open({json.dumps(hit("open"))})'>open</button>
<script>
const T = {js};
const u = w => `http://${{T}}/hit?w=${{w}}`;
const f = document.createElement('iframe'); f.src = u('iframe'); document.body.appendChild(f);
const i = new Image(); i.src = u('img');
fetch(u('fetch'), {{mode: 'no-cors'}}).catch(() => {{}});
const x = new XMLHttpRequest(); x.open('GET', u('xhr')); x.onerror = () => {{}}; x.send();
try {{ const s = new WebSocket(`ws://${{T}}/hit?w=ws`); s.onerror = () => {{}}; }} catch (e) {{}}
const form = document.createElement('form'); form.method = 'post'; form.action = u('form');
form.target = 'sink'; document.body.appendChild(form); form.submit();
navigator.serviceWorker.register('/sw/{n}/sw.js?t=' + encodeURIComponent(T), {{scope: '/sw/{n}/'}})
  .catch(() => {{}});
window.__done = true;
</script>""")
            if path.startswith("/sw/") and path.endswith("/sw.js"):
                return self._send(200, f"""self.addEventListener('install', e => {{
  e.waitUntil(fetch({json.dumps(hit('sw'))}, {{mode: 'no-cors'}}).catch(() => {{}}));
  self.skipWaiting();
}});""", "application/javascript")
            if path == "/lna":
                return self._send(200, LNA_PAGE.replace("__T__", json.dumps(target)))
            if path == "/popups":
                u = q.get("u", "")
                return self._send(200, f"""<!doctype html><title>{MARK} popups</title>
<button id=b1 onclick='window.open({json.dumps(u)})'>open</button>
<a id=b2 target=_blank href="{html.escape(u, quote=True)}">blank</a>
<a id=b3 target=_blank rel=noopener href="{html.escape(u, quote=True)}">noopener</a>""")
            if path == "/rtc":
                page = RTC_PAGE
                for key in ("u", "s", "c"):
                    page = page.replace(f"__{key.upper()}__", str(int(q.get(key, "0"))))
                return self._send(200, page)
            if path == "/redirect":
                return self._send(302, "", headers=[("Location", hit("302"))])
            if path == "/meta":
                return self._send(200, f'<title>{MARK}</title><meta http-equiv="refresh" '
                                       f'content="0;url={hit("meta")}">')
            if path == "/loc":
                return self._send(200, f"<title>{MARK}</title><script>location = "
                                       f"{json.dumps(hit('loc'))}</script>")
            if path == "/setcookie":
                state["cookie"] = f"keep-{secrets.token_hex(4)}"
                return self._send(200, "<title>set</title>", headers=[
                    ("Set-Cookie", f"probe={state['cookie']}; Max-Age=86400; Path=/")])
            if path == "/echo":
                cookie = self.headers.get("Cookie", "")
                return self._send(200, f"<title>cookie={cookie}</title>")
            return self._send(404, "<title>no</title>")
    return Fixture


def chrome_tree(folder: Path) -> list[int]:
    return H.chrome_pids(folder)


def proc_argv(pid: int) -> list[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            return [a.decode("utf-8", "replace") for a in handle.read().split(b"\0") if a]
    except OSError:
        return []


def pss_bytes(pids) -> int:
    total = 0
    for pid in pids:
        try:
            with open(f"/proc/{pid}/smaps_rollup") as handle:
                for line in handle:
                    if line.startswith("Pss:"):
                        total += int(line.split()[1]) * 1024
        except OSError:
            pass
    return total


def rss_bytes(pids) -> int:
    total = 0
    for pid in pids:
        try:
            with open(f"/proc/{pid}/status") as handle:
                for line in handle:
                    if line.startswith("VmRSS:"):
                        total += int(line.split()[1]) * 1024
        except OSError:
            pass
    return total


def deny_every_name(name, port):
    raise socket.gaierror(socket.EAI_NONAME, "the suite resolves nothing")


def suite_resolve(name, port):
    """`pub.test` is a public address; nothing else resolves."""
    if name == "pub.test":
        return [PUB]
    raise socket.gaierror(socket.EAI_NONAME, "the suite resolves nothing else")


async def loopback_only(address, port):
    """Loopback as asked; the suite's public address carried back to the
    same port on 127.0.0.1 (so a "public" page is the fixture, served from
    an address the gate judges public); anything else refused."""
    import ipaddress
    if address == PUB:
        return await G.default_connect("127.0.0.1", port)
    if P.address_class(ipaddress.ip_address(address)) != "loopback":
        raise ConnectionRefusedError("the suite never leaves loopback")
    return await G.default_connect(address, port)


class OwnerBrowser(unittest.TestCase):
    """Ordered sections sharing one daemon; at most one Chrome at a time."""

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory(prefix="obc-")
        cls.tmp = tmp
        cls.root = Path(tmp.name)
        cls.home = cls.root / "home"
        cls.home.mkdir()
        cls.patches = []

        def keep(p):
            p.start()
            cls.patches.append(p)

        keep(patch.dict(os.environ, {"HOME": str(cls.home), "XDG_DATA_HOME": str(cls.root / "xdg"),
                                     "JARVIS_CLAUDE_CREDENTIALS": str(cls.root / "none.json"),
                                     "OPENROUTER_API_KEY": "sk-dummy-not-a-key",
                                     "ANTHROPIC_API_KEY": "dummy-not-a-key",
                                     "JARVIS_SUITE_SECRET": "must-not-reach-chrome",
                                     "DISPLAY": ":99", "PULSE_SERVER": "unix:/nonexistent",
                                     "LC_ALL": "C.UTF-8"}))
        cls.stand_ins = {name: Listener() for name in
                         ("FACE_PORT", "DAEMON_PORT", "DESKTOP_PORT", "SPOTIFY_AUTH_PORT")}
        cls.sanity = Sanity()            # opened by the owner in test_08
        cls.closed = Sanity()            # a loopback port nobody opens
        cls.udp = UdpSink()
        cls.sniffer = Sniffer() if SNIFF else None
        values = dict(V2_DATA_DIR=cls.root / "data", ALLOWLIST_PATH=cls.root / "cfg" / "allow.json",
                      MODELS_PATH=cls.root / "cfg" / "models.json",
                      PROVIDER_DEFAULTS_PATH=cls.root / "cfg" / "provider_defaults.json",
                      ROUTING_PATH=cls.root / "cfg" / "routing.json",
                      CODEX_CATALOG_PATH=cls.root / "cfg" / "codex-models.json",
                      V2_ALWAYS_ASK=cls.root / "cfg" / "always-ask.json",
                      DISCORD_GUILD_PATH=cls.root / "cfg" / "guild.json",
                      DISCORD_TOKEN_PATH=cls.root / "cfg" / "discord_token.json",
                      HUD_DIR=cls.root / "hud", HUD_DIST=cls.root / "hud", HUD_DEV=False,
                      BROWSER_DIR=cls.root / "browser",
                      BROWSER_SETTINGS_PATH=cls.root / "cfg" / "browser.json",
                      BROWSER_TAB_CAP=3,
                      **{name: listener.port for name, listener in cls.stand_ins.items()})
        for name, value in values.items():
            keep(patch.object(config, name, value))
        cls.fixture_state = {}
        cls.fixture = ThreadingHTTPServer(("127.0.0.1", 0), fixture_handler(cls.fixture_state))
        threading.Thread(target=cls.fixture.serve_forever, daemon=True).start()
        cls.fx = cls.fixture.server_address[1]
        cls.records: list[dict] = []
        cls.logs: list[str] = []

        class Keep(logging.Handler):
            def emit(self, record):
                try:
                    cls.logs.append(record.getMessage())
                except Exception:  # noqa: BLE001
                    cls.logs.append(str(record.msg))
        cls.log_handler = Keep(level=logging.DEBUG)
        root_logger = logging.getLogger()
        cls.old_level = root_logger.level
        root_logger.addHandler(cls.log_handler)
        root_logger.setLevel(logging.DEBUG)

        cls.daemon = Daemon(Stores(), {name: Fake(name) for name in PN},
                            lambda *_: (lambda *_: None), 0)
        # `browser.json` is read and written only by the daemon's
        # `BrowserSettings` (settings.py), in memory unless attached: this
        # suite attaches its temp file, as daemon2's `main()` attaches the
        # owner's.
        cls.daemon.browser_settings.attach(config.BROWSER_SETTINGS_PATH)
        cls.daemon.browser = H.BrowserHost(cls.daemon, resolver=suite_resolve, connector=loopback_only)
        cls.daemon.bus.observe(cls.records.append)
        cls.daemon.start()
        cls.ports = {"api": cls.daemon.port, "hud": cls.daemon.face_port}
        assert not set(cls.ports.values()) & LIVE_PORTS
        cls.host = cls.daemon.browser
        cls.main_tab = None
        cls.sw = 0
        cls.measured: dict[str, float] = {}

    @classmethod
    def tearDownClass(cls):
        try:
            cls.daemon.stop()
        finally:
            logging.getLogger().removeHandler(cls.log_handler)
            logging.getLogger().setLevel(cls.old_level)
            left = chrome_tree(cls.root / "browser")
            for pid in left:            # by exact PID, ours only
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            cls.fixture.shutdown()
            for listener in (*cls.stand_ins.values(), cls.sanity, cls.closed, cls.udp):
                listener.close()
            if cls.sniffer is not None:
                cls.sniffer.close()
            for p in reversed(cls.patches):
                p.stop()
            cls.tmp.cleanup()
            print("\nmeasured:", json.dumps(cls.measured), file=sys.stderr)
            if left:
                raise AssertionError(f"{len(left)} Chrome processes were left running")

    def setUp(self):
        if self.sniffer is not None:
            self.sniffer.label = self._testMethodName

    # -- requests -----------------------------------------------------------------

    def ask(self, method, path, body=None, *, listener="hud", origin=True, headers=None):
        port = self.ports[listener]
        h = {"Content-Type": "application/json"} if body is not None else {}
        if origin:
            h["Origin"] = f"http://127.0.0.1:{port}"
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=90)
        try:
            conn.request(method, path, json.dumps(body).encode() if body is not None else None, h)
            response = conn.getresponse()
            raw = response.read()
        finally:
            conn.close()
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = raw
        return response.status, payload

    def owner(self, method, path, body=None):
        status, payload = self.ask(method, path, body)
        self.assertLess(status, 300, f"{method} {path}: {status} {payload}")
        return payload

    def run_on(self, coro, timeout=30):
        return self.host._call(coro, timeout)

    def page(self, tab_id):
        return self.host._tabs[tab_id].page

    def settled(self, page, expression):
        """`expression` in the page, or None while it is still navigating
        (the routes return at commit, so a context may be torn down under us)."""
        try:
            return self.run_on(page.evaluate(expression))
        except H.BrowserRefused:
            return None

    def stand_in_accepts(self):
        return {name: l.accepts for name, l in self.stand_ins.items() if l.accepts}

    def other_tabs(self, keep):
        return [t["tab_id"] for t in self.owner("GET", "/browser/tabs") if t["tab_id"] != keep]

    def close_others(self, keep):
        for tab_id in self.other_tabs(keep):
            self.owner("DELETE", f"/browser/tabs/{tab_id}")

    def wait(self, predicate, timeout=15.0, step=0.1):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(step)
        return predicate()

    def attack(self, tab_id, target, *, keep=()):
        """Every page-initiated way, aimed at `target` (host:port)."""
        type(self).sw += 1
        self.owner("POST", f"/browser/tabs/{tab_id}/navigate",
                   {"url": f"http://127.0.0.1:{self.fx}/multi?n={self.sw}&t={target}"})
        page = self.page(tab_id)
        self.assertTrue(self.wait(lambda: self.settled(page, "window.__done === true"), 10))
        self.run_on(page.click("#open"))           # a user gesture, so no pop-up blocker
        time.sleep(2.0)
        for other in self.other_tabs(tab_id):
            if other not in keep:
                self.owner("DELETE", f"/browser/tabs/{other}")
        for way in ("redirect", "meta", "loc"):
            self.owner("POST", f"/browser/tabs/{tab_id}/navigate",
                       {"url": f"http://127.0.0.1:{self.fx}/{way}?t={target}"})
            time.sleep(1.0)

    def measure(self, name):
        pids = chrome_tree(config.BROWSER_DIR)
        pss = pss_bytes(pids)
        self.measured[f"pss_mb_{name}"] = round(pss / 2 ** 20, 1)
        # RSS summed over the tree counts shared pages once per process: an
        # upper bound, reported beside the proportional figure.
        self.measured[f"rss_sum_mb_{name}"] = round(rss_bytes(pids) / 2 ** 20, 1)
        self.measured[f"processes_{name}"] = len(pids)
        self.assertLess(pss, PSS_LIMIT, f"the Chrome tree uses {pss / 2 ** 20:.0f} MiB")

    # -- 1: start, the folder, the launch ----------------------------------------------

    def test_01_nothing_starts_with_the_daemon(self):
        self.assertFalse(config.BROWSER_DIR.exists())
        status = self.ask("GET", "/status", listener="api")[1]
        self.assertFalse(status["browser"]["running"])
        self.assertTrue(status["browser"]["available"])
        refused = set(status["refused_ports"])
        self.assertTrue({l.port for l in self.stand_ins.values()} | {8402, 8404, 8405, 8406, 8403}
                        | set(self.ports.values()) <= refused)
        self.assertNotIn(self.daemon.workshop_port, refused)
        self.assertEqual(self.owner("GET", "/browser/tabs"), [])
        self.assertFalse(config.BROWSER_DIR.exists(), "a read started nothing")

    def test_02_first_tab_starts_main(self):
        # Two first tabs at once: one start, one Chrome, both tabs.
        rows, errors = [], []

        def first(path):
            try:
                rows.append(self.owner("POST", "/browser/tabs",
                                       {"url": f"http://127.0.0.1:{self.fx}/{MARK}/{path}"}))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        started = time.monotonic()
        racers = [threading.Thread(target=first, args=(p,)) for p in ("page", "other")]
        for racer in racers:
            racer.start()
        for racer in racers:
            racer.join(60)
        self.measured["first_tab_s"] = round(time.monotonic() - started, 2)
        self.measured["chrome_launch_s"] = self.host.timings.get("launch_s")
        self.assertEqual(errors, [])
        self.assertEqual(len({r["profile"] for r in rows}), 1)
        browsers = [pid for pid in chrome_tree(config.BROWSER_DIR)
                    if "--remote-debugging-pipe" in proc_argv(pid)]
        self.assertEqual(len(browsers), 1, "one Chrome for one profile")
        names = [t.name for t in threading.enumerate()]
        self.assertEqual(names.count("jarvis-owner-browser"), 1, "one Browser thread")
        self.assertEqual(names.count("jarvis-browser-gate"), 1, "one gate")
        row = next(r for r in rows if self.page(r["tab_id"]).url.endswith("/page"))
        self.owner("DELETE", f"/browser/tabs/{next(r for r in rows if r is not row)['tab_id']}")
        type(self).main_tab = row["tab_id"]
        self.assertTrue(row["loaded"], row)
        self.assertRegex(row["profile"], r"^p-[0-9a-f]{8}$")
        tabs = self.owner("GET", "/browser/tabs")
        self.assertEqual([t["title"] for t in tabs], [f"{MARK} page"])
        state = self.owner("GET", "/browser")
        self.assertEqual([p["label"] for p in state["profiles"]], ["Main"])
        self.assertTrue(state["running"])
        self.measure("main_one_tab")

    def test_03_the_folder_is_private_and_a_credential_folder(self):
        root = config.BROWSER_DIR
        for folder in (root, root / "profiles", root / "downloads", root / "home", root / "artifacts",
                       root / "private", *(root / "profiles").iterdir()):
            with self.subTest(folder=folder.name):
                self.assertEqual(oct(folder.lstat().st_mode & 0o777), "0o700")
        self.assertEqual(oct((root / "profiles.json").stat().st_mode & 0o777), "0o600")
        resolved = root.resolve()
        self.assertIn(str(root), config.credential_dirs())
        self.assertIn(resolved, permissions._credential_dirs())
        self.assertIn(resolved, editor_policy.credential_dirs())
        self.assertIn(Path(os.path.normpath(root)), folders._protected())
        self.assertTrue(permissions._under_credential_dir(str(root / "profiles" / "x" / "Cookies")))
        self.assertIsNotNone(permissions.denied_grant(str(root / "profiles"), "read"))

    def test_04_the_launch_is_curated(self):
        profile = next((config.BROWSER_DIR / "profiles").iterdir())
        pids = chrome_tree(profile)
        self.assertTrue(pids)
        argvs = {pid: proc_argv(pid) for pid in pids}
        browser = [pid for pid, argv in argvs.items()
                   if "--remote-debugging-pipe" in argv and not any(a.startswith("--type=") for a in argv)]
        self.assertEqual(len(browser), 1, "one browser process")
        argv = argvs[browser[0]]
        self.assertEqual(argv[1:], H.launch_args(profile, self.host._gate.port, private=False))
        # What the flags *do* is test_04b's (no UDP leaves Chrome): a flag
        # Chrome does not know is ignored without a word, which is how the
        # first round shipped `--force-webrtc-ip-handling-policy`.
        for flag in ("--headless", f"--user-data-dir={profile}", "--proxy-bypass-list=<-loopback>",
                     f"--proxy-server=socks5://127.0.0.1:{self.host._gate.port}", "--disable-extensions",
                     "--deny-permission-prompts", "--mute-audio"):
            self.assertIn(flag, argv)
        for every in argvs.values():
            for refused in ("--enable-automation", "--no-sandbox", "--disable-sync",
                            "--disable-background-networking", "--disable-component-update"):
                self.assertNotIn(refused, every)
            self.assertFalse(any(a.startswith("--remote-debugging-port") for a in every))
        self.assertFalse((profile / "DevToolsActivePort").exists(), "no DevTools port")
        # The sandbox: a renderer runs under seccomp-bpf.
        sandboxed = []
        for pid, a in argvs.items():
            if "--type=renderer" in " ".join(a).split():
                status = Path(f"/proc/{pid}/status").read_text()
                sandboxed.append("Seccomp:\t2" in status)
        self.assertTrue(sandboxed and all(sandboxed), sandboxed)
        # A clean environment, built from nothing.
        env = dict(item.split("=", 1) for item in
                   Path(f"/proc/{browser[0]}/environ").read_bytes().decode().split("\0") if "=" in item)
        self.assertEqual(env["HOME"], str(config.BROWSER_DIR / "home"))
        self.assertLessEqual({k for k in env if not k.startswith("LC_")}, ENV_ALLOWED, sorted(env))
        blob = "\0".join(f"{k}={v}" for k, v in env.items())
        for secret in ("sk-dummy-not-a-key", "dummy-not-a-key", "must-not-reach-chrome", "PULSE_SERVER",
                       "DISPLAY", "JARVIS_"):
            self.assertNotIn(secret, blob)
        self.assertEqual(env.get("LC_ALL"), "C.UTF-8", "the locale passes")
        if H._WINDOWS_FONTS.is_dir():
            self.assertEqual(env.get("FONTCONFIG_FILE"), str(config.BROWSER_DIR / "home" / "fontconfig" / "fonts.conf"))

    def test_04b_no_udp_leaves_chrome(self):
        """WebRTC and WebTransport, effect not flags (review of PR #69, HIGH:
        `--force-webrtc-ip-handling-policy` does not exist, and with it every
        ICE candidate was gathered and STUN reached a loopback sink). With
        `--webrtc-ip-handling-policy=disable_non_proxied_udp` behind a SOCKS
        proxy nothing is gathered and nothing is sent; the sniffer (in a
        private network namespace) adds that no mDNS or IGMP left either."""
        tab = self.main_tab
        stand_in = self.stand_ins["FACE_PORT"].port
        before = (self.udp.datagrams, self.closed.accepts)
        self.owner("POST", f"/browser/tabs/{tab}/navigate", {
            "url": f"http://127.0.0.1:{self.fx}/rtc?u={self.udp.port}&s={stand_in}&c={self.closed.port}"})
        page = self.page(tab)
        self.assertTrue(self.wait(lambda: self.settled(page, "window.__r && window.__r.done"), 20))
        result = json.loads(self.run_on(page.evaluate("JSON.stringify(window.__r)")))
        time.sleep(1.0)
        self.measured["webrtc_candidates"] = len(result["cands"])
        self.measured["udp_datagrams"] = self.udp.datagrams - before[0]
        self.assertIsNone(result["err"], result)
        self.assertEqual(result["cands"], [], "WebRTC gathered ICE candidates")
        self.assertEqual(self.udp.datagrams, before[0], "a datagram reached the loopback UDP sink")
        self.assertEqual(sorted(result["wt"]), ["127.0.0.1", "[::1]"], "WebTransport failed both ways")
        self.assertEqual(self.stand_in_accepts(), {})
        self.assertEqual(self.closed.accepts, before[1])

    # -- 2: typed, owner-only, refused schemes -------------------------------------------

    def test_05_typed_jarvis_ports_in_every_spelling(self):
        tab = self.main_tab
        before = self.page(tab).url
        spellings = ("localhost", "127.0.0.1", "foo.localhost", "[::1]", "0.0.0.0", "127.1", "0x7f000001",
                     "2130706433", "[::ffff:127.0.0.1]", "localhost.", "0177.0.0.1", "LOCALHOST")
        targets = [l.port for l in self.stand_ins.values()] + list(self.ports.values())
        for port in targets:
            for host in spellings:
                url = f"http://{host}:{port}/hit?w=typed"
                with self.subTest(url=url):
                    status, payload = self.ask("POST", f"/browser/tabs/{tab}/navigate", {"url": url})
                    self.assertEqual(status, 400, payload)
                    self.assertEqual(payload["error"], P.JARVIS_PORT_SENTENCE)
        status, payload = self.ask("POST", "/browser/tabs",
                                   {"url": f"http://localhost:{targets[0]}/"})
        self.assertEqual(status, 400, payload)
        self.assertEqual(self.page(tab).url, before)
        self.assertEqual(self.stand_in_accepts(), {})

    def test_06_typed_refused_schemes(self):
        tab = self.main_tab
        before = self.page(tab).url
        for url in ("file:///etc/hostname", f"file://{config.BROWSER_DIR}/profiles.json",
                    "chrome://settings", "chrome://version", "chrome-untrusted://new-tab-page/",
                    "devtools://devtools/bundled/inspector.html", f"view-source:http://127.0.0.1:{self.fx}/",
                    "chrome-extension://aaaa/x.html", "javascript:document.title='x'",
                    "data:text/html,<title>x</title>", "about:config", "about:version",
                    f"http://user:pw@127.0.0.1:{self.fx}/", "ftp://127.0.0.1/"):
            with self.subTest(url=url):
                status, payload = self.ask("POST", f"/browser/tabs/{tab}/navigate", {"url": url})
                self.assertEqual(status, 400, payload)
                self.assertNotIn(url, json.dumps(payload))
        self.assertEqual(self.page(tab).url, before, "nothing was loaded")

    def test_07_every_route_is_the_owners(self):
        tab = self.main_tab
        url = {"url": f"http://127.0.0.1:{self.fx}/{MARK}/page"}
        for method, path, body in (("GET", "/browser", None), ("GET", "/browser/tabs", None),
                                   ("POST", "/browser/tabs", url),
                                   ("POST", f"/browser/tabs/{tab}/navigate", url),
                                   ("POST", f"/browser/tabs/{tab}/history", {"action": "reload"}),
                                   ("PATCH", "/browser/settings", {"lan": True}),
                                   ("DELETE", f"/browser/tabs/{tab}", None),
                                   ("POST", "/browser/profiles/p-00000000/forget", {})):
            with self.subTest(path=path, method=method):
                # The API listener every tool's HTTP client uses.
                self.assertEqual(self.ask(method, path, body, listener="api", origin=False)[0], 403)
                self.assertEqual(self.ask(method, path, body, listener="api")[0], 403)
                # A foreign page, or a write with no Origin, on the HUD listener.
                self.assertEqual(self.ask(method, path, body, origin=False,
                                          headers={"Origin": "http://127.0.0.1:1"})[0], 403)
                if method != "GET":
                    self.assertEqual(self.ask(method, path, body, origin=False)[0], 403)
                # A rebound name.
                self.assertEqual(self.ask(method, path, body, headers={
                    "Host": f"evil.example:{self.ports['hud']}"})[0], 403)
        # The owner's own same-origin read (Chromium sends no Origin on it).
        status, _ = self.ask("GET", "/browser", origin=False, headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 200)
        self.assertFalse(self.daemon.browser_settings.value("lan"), "a refused PATCH changed nothing")
        self.assertIn(self.main_tab, [t["tab_id"] for t in self.owner("GET", "/browser/tabs")])

    # -- 3: the twelve ways (typed above) ----------------------------------------------

    def lna(self, tab_id, target):
        """A "public" page (`pub.test`, a public address to the gate) reading
        `target`; what it got."""
        self.owner("POST", f"/browser/tabs/{tab_id}/navigate",
                   {"url": f"http://pub.test:{self.fx}/lna?t={target}"})
        page = self.page(tab_id)
        self.assertTrue(self.wait(lambda: self.settled(page, "window.__r && window.__r.done"), 10))
        return json.loads(self.run_on(page.evaluate("JSON.stringify(window.__r)")))

    def test_08_loopback_is_closed_until_the_owner_opens_it(self):
        """Review of PR #69, MED 2 (the lead's decision): a page from the
        internet read a CORS-permissive service on this machine through the
        gate. Loopback is closed like the LAN until the owner opens a port."""
        tab = self.main_tab
        gate = self.host._gate
        closed_before = gate.stats()["refused"].get(P.LOOPBACK_CLOSED, 0)
        self.assertEqual(self.host.status()["loopback_opened"], 1, "the fixture's port, from test_02")
        # Not yet opened: neither a public page nor every way from a loopback page reaches it.
        self.assertEqual(self.lna(tab, f"127.0.0.1:{self.sanity.port}"), {"cors": "err", "post": "err",
                                                                         "done": True})
        self.assertEqual(self.lna(tab, f"localhost:{self.sanity.port}")["cors"], "err")
        self.attack(tab, f"127.0.0.1:{self.sanity.port}")
        time.sleep(0.5)
        self.assertEqual(self.sanity.accepts, 0, "a page reached a loopback port nobody opened")
        self.assertGreater(gate.stats()["refused"].get(P.LOOPBACK_CLOSED, 0), closed_before + 5)
        # A page's own move to it does not open it either (only the owner's routes do).
        self.owner("POST", f"/browser/tabs/{tab}/navigate",
                   {"url": f"http://127.0.0.1:{self.fx}/loc?t=127.0.0.1:{self.sanity.port}"})
        time.sleep(1.0)
        self.assertEqual(self.sanity.accepts, 0)
        # Nor does a navigation that is not the owner's (BR-2's tools will
        # call `navigate` without `owner=True`).
        self.host.navigate(tab, f"http://127.0.0.1:{self.sanity.port}/hit?w=agent")
        self.assertEqual(self.sanity.accepts, 0)
        self.assertEqual(self.host.status()["loopback_opened"], 0, "the owner left the fixture port")
        # Keep the authorizing pane open while a different page exercises the
        # shared gate; navigating that pane away now revokes its permission.
        keeper = self.owner("POST", "/browser/tabs", {"url": f"http://localhost:{self.sanity.port}/"})["tab_id"]
        self.assertGreaterEqual(self.sanity.accepts, 1)

        body = "document.body ? document.body.innerText : ''"
        self.assertTrue(self.wait(lambda: self.settled(self.page(keeper), body) == "ok", 10))
        self.assertEqual(self.host.status()["loopback_opened"], 1)
        self.assertEqual(self.lna(tab, f"127.0.0.1:{self.sanity.port}"),
                         {"cors": "read:ok", "post": "sent", "done": True})
        # Another loopback port is still refused, by every way.
        self.assertEqual(self.lna(tab, f"127.0.0.1:{self.closed.port}")["cors"], "err")
        self.attack(tab, f"localhost:{self.closed.port}", keep=(keeper,))
        time.sleep(0.5)
        self.assertEqual(self.closed.accepts, 0, "a page reached a loopback port nobody opened")
        self.owner("DELETE", f"/browser/tabs/{keeper}")
        self.assertFalse(self.host.loopback_open(self.sanity.port), "closing the last owner revokes the port")

    def test_08b_the_ways_reach_an_ordinary_server(self):
        # The sanity half: every way really fires, through the gate, to the
        # loopback port with a currently open owner-authorizing pane.
        keeper = self.owner("POST", "/browser/tabs", {"url": f"http://127.0.0.1:{self.sanity.port}/"})["tab_id"]
        allowed_before = self.host._gate.stats()["allowed"]
        try:
            for spelling in ("127.0.0.1", "localhost"):
                self.sanity.ways.clear()
                self.attack(self.main_tab, f"{spelling}:{self.sanity.port}", keep=(keeper,))
                self.assertTrue(self.wait(lambda: set(WAYS) | set(NAV_WAYS) <= self.sanity.ways, 10),
                                f"{spelling}: only {sorted(self.sanity.ways)}")
            self.assertGreater(self.host._gate.stats()["allowed"], allowed_before)
        finally:
            self.owner("DELETE", f"/browser/tabs/{keeper}")

    def test_09_no_way_reaches_jarvis(self):
        refused_before = self.host._gate.stats()["refused"].get(P.JARVIS_PORT, 0)
        ports = [l.port for l in self.stand_ins.values()]
        spellings = ("127.0.0.1", "localhost", "[::1]", "foo.localhost", "127.1", "[::ffff:127.0.0.1]",
                     "0.0.0.0")
        for i, spelling in enumerate(spellings):
            with self.subTest(spelling=spelling):
                self.attack(self.main_tab, f"{spelling}:{ports[i % len(ports)]}")
        # And the daemon's own reported API port.
        self.attack(self.main_tab, f"127.0.0.1:{self.ports['api']}")
        time.sleep(0.5)
        self.assertEqual(self.stand_in_accepts(), {}, "a page reached a stand-in for Jarvis's ports")
        self.assertGreater(self.host._gate.stats()["refused"].get(P.JARVIS_PORT, 0), refused_before + 20)
        self.measure("main_after_attacks")

    def test_10_the_landing_guard(self):
        tab = self.main_tab
        before = self.host.status()["landing_guard"]
        try:      # past the judge, straight to Chrome: only the guard is left
            self.run_on(self.page(tab).goto(f"data:text/html,<title>{MARK} landed</title>"))
        except Exception:  # noqa: BLE001 - the guard may interrupt the navigation
            pass
        self.assertTrue(self.wait(lambda: self.host.status()["landing_guard"] > before, 10))
        def explained():
            try:
                return "does not open this kind of address" in self.run_on(self.page(tab).content())
            except H.BrowserRefused:          # caught mid-navigation: ask again
                return False
        self.assertTrue(self.wait(explained, 10))
        self.assertEqual(self.page(tab).url, "about:blank")

    def popup(self, tab, url, button):
        """Click `button` on a page that opens `url` in a new tab; the new
        tab's row, after it settled."""
        from urllib.parse import quote
        self.owner("POST", f"/browser/tabs/{tab}/navigate",
                   {"url": f"http://127.0.0.1:{self.fx}/popups?u={quote(url, safe='')}"})
        self.run_on(self.page(tab).click(f"#{button}"))      # a user gesture
        self.assertTrue(self.wait(lambda: self.other_tabs(tab), 10), "no pop-up was opened")
        time.sleep(2.0)
        rows = [r for r in self.owner("GET", "/browser/tabs") if r["tab_id"] != tab]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def test_10b_popups_land_where_they_were_sent(self):
        """Review of PR #69, MED 1: a pop-up is handed over before its first
        load, with an empty URL, and the guard judged "" and blanked every
        one. Its first real landing is what is judged."""
        tab = self.main_tab
        self.close_others(tab)
        target = f"http://127.0.0.1:{self.fx}/{MARK}/page"
        for button in ("b1", "b2", "b3"):        # window.open, target=_blank, rel=noopener
            with self.subTest(button=button):
                guarded = self.host.status()["landing_guard"]
                row = self.popup(tab, target, button)
                self.assertEqual((row["url"], row["title"]), (target, f"{MARK} page"))
                self.assertEqual(self.host.status()["landing_guard"], guarded, "a pop-up was replaced")
                self.close_others(tab)
        # A pop-up sent to Jarvis's port: the gate refuses it, so it lands on
        # Chrome's own error page and the stand-in hears nothing.
        stand_in = self.stand_ins["DESKTOP_PORT"].port
        for button in ("b1", "b3"):
            with self.subTest(jarvis_port=button):
                row = self.popup(tab, f"http://127.0.0.1:{stand_in}/hit?w=popup", button)
                self.assertEqual(row["url"], "chrome-error://chromewebdata/")
                self.close_others(tab)
        self.assertEqual(self.stand_in_accepts(), {})
        # A pop-up is still judged where it lands next: moved past the judge
        # (straight to Chrome, as in test_10), the guard replaces it.
        for button in ("b1", "b3"):
            with self.subTest(moved=button):
                row = self.popup(tab, target, button)
                guarded = self.host.status()["landing_guard"]
                popup = self.page(row["tab_id"])
                try:
                    self.run_on(popup.goto(f"data:text/html,<title>{MARK} popup landed</title>"))
                except Exception:  # noqa: BLE001 - the guard may interrupt the navigation
                    pass
                self.assertTrue(self.wait(lambda: self.host.status()["landing_guard"] > guarded, 10))

                def explained():
                    try:
                        return "does not open this kind of address" in self.run_on(popup.content())
                    except H.BrowserRefused:          # caught mid-navigation: ask again
                        return False
                self.assertTrue(self.wait(explained, 10))
                self.assertEqual(popup.url, "about:blank")
                self.close_others(tab)

    def test_11_history(self):
        tab = self.main_tab
        self.owner("POST", f"/browser/tabs/{tab}/navigate", {"url": f"http://127.0.0.1:{self.fx}/{MARK}/page"})
        self.owner("POST", f"/browser/tabs/{tab}/navigate", {"url": f"http://localhost:{self.fx}/echo"})
        self.owner("POST", f"/browser/tabs/{tab}/history", {"action": "back"})
        self.assertTrue(self.wait(lambda: self.page(tab).url.endswith(f"/{MARK}/page"), 10))
        self.assertEqual(self.ask("POST", f"/browser/tabs/{tab}/history", {"action": "jump"})[0], 400)
        self.assertEqual(self.ask("POST", "/browser/tabs/deadbeef/navigate", {"url": "about:blank"})[0], 404)
        self.assertEqual(self.ask("POST", "/browser/tabs/NOTANID/navigate", {"url": "about:blank"})[0], 400)

    def test_11b_navigation_identity_is_current_without_a_hud_report(self):
        url = f"http://127.0.0.1:{self.fx}/echo"
        tab = self.owner("POST", "/browser/tabs", {"url": url})["tab_id"]
        try:
            first = self.host.pane_tab(tab)
            self.assertIsNotNone(first)
            self.assertEqual(first["url"], url)
            self.owner("POST", f"/browser/tabs/{tab}/history", {"action": "reload"})
            self.assertTrue(self.wait(lambda: (self.host.pane_tab(tab) or {}).get(
                "document_generation", 0) > first["document_generation"]))
            reloaded = self.host.pane_tab(tab)
            self.assertEqual(reloaded["url"], url)
            self.assertEqual(reloaded["page_identity"], first["page_identity"])
            self.run_on(self.page(tab).evaluate("history.pushState({}, '', '#same-document')"))
            self.assertTrue(self.wait(lambda: (self.host.pane_tab(tab) or {}).get(
                "document_generation", 0) > reloaded["document_generation"]))
            self.assertEqual(self.host.pane_tab(tab)["url"], url + "#same-document")

            async def from_capture_loop():
                return self.host.pane_tab(tab)
            self.assertEqual(self.run_on(from_capture_loop(), 2), self.host.pane_tab(tab))
        finally:
            self.owner("DELETE", f"/browser/tabs/{tab}")
        self.assertIsNone(self.host.pane_tab(tab))

    def test_11c_stored_restore_never_opens_gate_or_hits_control_ports(self):
        local = Sanity()
        self.addCleanup(local.close)
        tab = self.owner("POST", "/browser/tabs", {})["tab_id"]
        try:
            url = f"http://localhost:{local.port}/hit?w=restore"
            restored = self.owner("POST", f"/browser/tabs/{tab}/restore", {"url": url})
            self.assertFalse(restored["loaded"])
            self.assertEqual(local.accepts, 0)
            self.assertFalse(self.host.loopback_open(local.port))
            before = self.stand_in_accepts()
            for listener in self.stand_ins.values():
                status, _ = self.ask("POST", f"/browser/tabs/{tab}/restore",
                                     {"url": f"http://localhost:{listener.port}/"})
                self.assertEqual(status, 400)
            self.assertEqual(self.stand_in_accepts(), before)
            opened = self.owner("POST", f"/browser/tabs/{tab}/navigate", {"url": url})
            self.assertTrue(opened["loaded"], opened)
            self.assertGreater(local.accepts, 0)
            self.assertTrue(self.host.loopback_open(local.port))
        finally:
            self.owner("DELETE", f"/browser/tabs/{tab}")
        self.assertFalse(self.host.loopback_open(local.port))

    def test_12_the_tab_cap(self):
        self.close_others(self.main_tab)
        rows = [self.owner("POST", "/browser/tabs", {}) for _ in range(2)]
        status, payload = self.ask("POST", "/browser/tabs", {})
        self.assertEqual(status, 409, payload)
        for row in rows:
            self.owner("DELETE", f"/browser/tabs/{row['tab_id']}")

    # -- 4: Private ------------------------------------------------------------------

    def test_13_private_goes_through_the_gate_too(self):
        self.host.close_profile(None)                  # one Chrome at a time
        self.assertEqual(chrome_tree(next((config.BROWSER_DIR / "profiles").iterdir())), [])
        type(self).main_tab = None
        row = self.owner("POST", "/browser/tabs", {"profile": "private",
                                                   "url": f"http://127.0.0.1:{self.fx}/{MARK}/page"})
        self.assertEqual(row["profile"], "private")
        self.assertRegex(row["ctx"], r"^[0-9a-f]{8}$")
        runs = list((config.BROWSER_DIR / "private").iterdir())
        self.assertEqual(len(runs), 1)
        self.assertEqual(oct(runs[0].lstat().st_mode & 0o777), "0o700")
        browser = [pid for pid in chrome_tree(runs[0]) if "--remote-debugging-pipe" in proc_argv(pid)
                   and not any(a.startswith("--type=") for a in proc_argv(pid))]
        self.assertIn("--disable-sync", proc_argv(browser[0]))
        keeper = self.owner("POST", "/browser/tabs", {"profile": "private", "ctx": row["ctx"],
                            "url": f"http://127.0.0.1:{self.sanity.port}/"})["tab_id"]
        self.sanity.ways.clear()
        self.attack(row["tab_id"], f"127.0.0.1:{self.sanity.port}", keep=(keeper,))
        self.assertTrue(self.wait(lambda: {"fetch", "iframe", "302"} <= self.sanity.ways, 10),
                        sorted(self.sanity.ways))
        self.owner("DELETE", f"/browser/tabs/{keeper}")
        ports = [l.port for l in self.stand_ins.values()]
        self.attack(row["tab_id"], f"localhost:{ports[0]}")
        self.attack(row["tab_id"], f"[::1]:{ports[1]}")
        time.sleep(0.5)
        self.assertEqual(self.stand_in_accepts(), {})
        self.measure("private")
        # A second tab in the same context, then the context closes with its last tab.
        second = self.owner("POST", "/browser/tabs", {"profile": "private", "ctx": row["ctx"]})
        self.assertEqual(second["ctx"], row["ctx"])
        self.assertEqual(self.ask("POST", "/browser/tabs", {"profile": "private", "ctx": "00000000"})[0], 404)
        self.assertEqual(self.ask("POST", "/browser/tabs", {"ctx": row["ctx"]})[0], 400)
        for tab_id in (row["tab_id"], second["tab_id"]):
            self.owner("DELETE", f"/browser/tabs/{tab_id}")
        self.assertTrue(self.wait(lambda: not self.host.status()["instances"] and not chrome_tree(config.BROWSER_DIR)
                                 and not list((config.BROWSER_DIR / "private").iterdir()), 10),
                        "the final Private tab closes Chrome and removes its run folder")
        self.assertEqual(list((config.BROWSER_DIR / "private").iterdir()), [], "the run folder went")
        self.assertEqual(chrome_tree(config.BROWSER_DIR), [])

    # -- 5: persistence and forget -----------------------------------------------------

    def test_14_logins_persist_and_forget_wipes_them(self):
        main = self.owner("GET", "/browser")["profiles"][0]["id"]
        row = self.owner("POST", "/browser/tabs", {"url": f"http://127.0.0.1:{self.fx}/setcookie"})
        cookie = self.fixture_state["cookie"]
        self.host.close_profile(None)
        self.assertEqual(chrome_tree(config.BROWSER_DIR), [])
        row = self.owner("POST", "/browser/tabs", {"url": f"http://127.0.0.1:{self.fx}/echo"})
        self.assertEqual(self.owner("GET", "/browser/tabs")[0]["title"], f"cookie=probe={cookie}")
        self.assertEqual(self.ask("POST", "/browser/profiles/p-00000000/forget", {})[0], 404)
        self.assertEqual(self.ask("POST", "/browser/profiles/../forget", {})[0], 400)
        self.owner("POST", f"/browser/profiles/{main}/forget", {})
        self.assertEqual(chrome_tree(config.BROWSER_DIR), [], "forget stops the profile's Chrome")
        folder = config.BROWSER_DIR / "profiles" / main
        self.assertEqual(list(folder.iterdir()), [], "a fresh, empty folder")
        self.assertEqual(oct(folder.lstat().st_mode & 0o777), "0o700")
        self.assertEqual([p["id"] for p in self.owner("GET", "/browser")["profiles"]], [main],
                         "the label stays")
        row = self.owner("POST", "/browser/tabs", {"url": f"http://127.0.0.1:{self.fx}/echo"})
        self.assertEqual(self.owner("GET", "/browser/tabs")[0]["title"], "cookie=")
        type(self).main_tab = row["tab_id"]

    # -- 6: settings -------------------------------------------------------------------

    def test_15_settings_are_the_owners_and_protected(self):
        self.assertEqual(self.owner("GET", "/browser/settings"),
                         {"lan": False, "local_servers": False, "auto_open": True})
        self.assertEqual(self.ask("PATCH", "/browser/settings", {"lan": "yes"})[0], 400)
        self.assertEqual(self.ask("PATCH", "/browser/settings", {})[0], 400)
        # An unknown key is counted, never echoed (review of PR #69, NIT).
        status, payload = self.ask("PATCH", "/browser/settings", {"sound": True, f"{MARK}key": 1})
        self.assertEqual((status, payload), (400, {"error": "unknown fields (2)"}))
        self.assertEqual(self.owner("PATCH", "/browser/settings", {"lan": True}),
                         {"lan": True, "local_servers": False, "auto_open": True})
        path = config.BROWSER_SETTINGS_PATH
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertTrue(self.ask("GET", "/status", listener="api")[1]["browser"]["lan"])
        self.owner("PATCH", "/browser/settings", {"lan": False})
        self.assertIn(path.resolve(), v1_files._protected_state())
        self.assertIn(path.resolve(), permissions.protected_paths())
        original = path.read_bytes()
        with runtime.tools_bound({"write_file", "read_file"}):
            tools.dispatch("read_file", json.dumps({"path": str(path)}))
            out = tools.dispatch("write_file", json.dumps({"path": str(path),
                                                           "content": '{"lan": true}'})).text
        self.assertIn("safety layer", out)
        self.assertEqual(path.read_bytes(), original)

    # -- 7: stop -------------------------------------------------------------------------

    def test_16_stop_leaves_nothing(self):
        self.assertTrue(chrome_tree(config.BROWSER_DIR))
        started = time.monotonic()
        self.host.stop()
        self.measured["stop_s"] = round(time.monotonic() - started, 2)
        time.sleep(0.3)
        self.assertEqual(chrome_tree(config.BROWSER_DIR), [], "no Chrome left after stop()")
        self.assertFalse(self.host.status()["running"])
        self.assertEqual(self.ask("POST", "/browser/tabs", {})[0], 409, "stop is final")
        self.assertFalse(self.host._thread.is_alive())

    def test_17_v1_session_never_started(self):
        v1 = sys.modules.get("jarvis.browser")
        if v1 is not None:
            self.assertIsNone(v1.SESSION._thread)
            self.assertIsNone(v1.SESSION._pw)
        source = (Path(H.__file__).parent).glob("*.py")
        for path in source:
            text = path.read_text()
            self.assertNotIn("jarvis.browser import", text.replace("jarvis.v2.browser", ""))
            self.assertNotIn("from jarvis import browser", text)

    def test_18_no_url_or_title_on_the_bus_or_in_the_log(self):
        bus = json.dumps(self.records)
        logs = "\n".join(self.logs)
        self.assertTrue(self.logs, "the log was captured")
        for needle in (MARK, f":{self.fx}", f"127.0.0.1:{self.sanity.port}", "/hit?w=", "cookie=",
                       *(f":{l.port}/" for l in self.stand_ins.values())):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, bus)
                self.assertNotIn(needle, logs)

    @unittest.skipUnless(SNIFF, "needs the private network namespace (JARVIS_NETNS_SNIFF=1)")
    def test_19_nothing_left_on_any_interface_but_loopback(self):
        """In a namespace with `lo` and a dummy interface holding a default
        route, every packet sent anywhere but `lo` would be the suite's
        Chrome leaving the machine: mDNS (224.0.0.251:5353) and IGMP from
        WebRTC's host candidates, DNS, anything. The kernel's own IPv6
        neighbour and multicast-listener messages (ICMPv6) are not Chrome's."""
        packets = self.sniffer.snapshot()
        self.assertTrue(any(p[0] == "lo" for p in packets), "the sniffer saw nothing at all")
        away = [p for p in packets if p[0] != "lo" and p[1] != 58]
        udp = [p for p in packets if p[1] == 17]
        self.measured["sniffed_packets"] = len(packets)
        self.assertEqual(away, [], "a packet left on an interface other than lo")
        self.assertEqual(udp, [], "a UDP datagram was sent")


class Folder(unittest.TestCase):
    """The engine refuses a folder that is not private. No Chrome starts."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="obf-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name, value in dict(BROWSER_DIR=self.root / "browser",
                                BROWSER_SETTINGS_PATH=self.root / "browser.json").items():
            guard = patch.object(config, name, value)
            guard.start()
            self.addCleanup(guard.stop)

    def refused(self, why):
        host = H.BrowserHost(None, resolver=deny_every_name, connector=loopback_only)
        self.addCleanup(host.stop)
        with self.assertRaises(H.BrowserRefused) as raised:
            host.open_tab()
        self.assertEqual(raised.exception.status, 409)
        self.assertIn(why, str(raised.exception))
        self.assertEqual(chrome_tree(self.root), [])

    def test_a_symlinked_folder(self):
        real = self.root / "elsewhere"
        real.mkdir(mode=0o700)
        (self.root / "browser").symlink_to(real)
        self.refused("symlink")

    def test_an_open_folder(self):
        (self.root / "browser").mkdir(mode=0o700)
        os.chmod(self.root / "browser", 0o755)
        self.refused("open to other users")

    def test_an_open_profile_folder(self):
        for sub in ("browser", "browser/profiles"):
            (self.root / sub).mkdir(mode=0o700)
        os.chmod(self.root / "browser" / "profiles", 0o750)
        self.refused("open to other users")

    def test_an_unreadable_profiles_list(self):
        (self.root / "browser").mkdir(mode=0o700)
        (self.root / "browser" / "profiles.json").write_text("{not json")
        (self.root / "browser" / "profiles.json").chmod(0o600)
        self.refused("profiles.json is unreadable")


class Lifecycle(unittest.TestCase):
    """`stop()` during a start (review of PR #69, LOW): it used to find
    nothing to stop and return, and the start then left its gate, its loop
    thread and its driver running behind a stopped Browser. No Chrome: the
    start is held inside `_ensure_main` and Playwright is a stand-in."""

    def test_stop_waits_for_a_start_in_progress(self):
        tmp = tempfile.TemporaryDirectory(prefix="obl-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name, value in dict(BROWSER_DIR=root / "browser",
                                BROWSER_SETTINGS_PATH=root / "browser.json").items():
            guard = patch.object(config, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        host = H.BrowserHost(None, resolver=deny_every_name, connector=loopback_only)
        entered, release = threading.Event(), threading.Event()
        real_main = host._ensure_main

        def held_main():
            entered.set()
            release.wait(10)
            real_main()

        async def no_playwright():
            await asyncio.sleep(0)

        host._ensure_main = held_main
        host._start_playwright = no_playwright
        outcome = {}

        def start():
            try:
                host._ensure_started()
                outcome["start"] = "started"
            except H.BrowserRefused as refusal:
                outcome["start"] = refusal.status
        starter = threading.Thread(target=start)
        starter.start()
        self.assertTrue(entered.wait(10))
        stopper = threading.Thread(target=host.stop)
        stopper.start()
        time.sleep(0.5)
        self.assertTrue(stopper.is_alive(), "stop() did not wait for the start in progress")
        release.set()
        starter.join(15)
        stopper.join(15)
        self.assertFalse(starter.is_alive() or stopper.is_alive())
        self.assertEqual(outcome["start"], "started")
        self.assertIsNotNone(host._gate)
        self.assertFalse(host._gate._thread.is_alive(), "the gate outlived stop()")
        self.assertFalse(host._thread.is_alive(), "the Browser's loop outlived stop()")
        self.assertFalse(host.status()["running"])
        with self.assertRaises(H.BrowserRefused):
            host._ensure_started()


class Pids(unittest.TestCase):
    """`chrome_pids` names a process only if it runs the launched Chrome
    (review of PR #69, LOW): a process that merely carries the folder in its
    command line is not ours to signal."""

    def test_a_decoy_with_the_folder_in_its_argv_is_left_alone(self):
        tmp = tempfile.TemporaryDirectory(prefix="obp-")
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name) / "profile"
        folder.mkdir(mode=0o700)
        import subprocess
        decoy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)",
                                  f"--user-data-dir={folder}", "--type=renderer"])
        self.addCleanup(decoy.wait)
        self.addCleanup(decoy.kill)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and f"--user-data-dir={folder}" not in proc_argv(decoy.pid):
            time.sleep(0.05)
        self.assertIn(f"--user-data-dir={folder}", proc_argv(decoy.pid))
        self.assertEqual(H.chrome_pids(folder), [])
        H.stop_pids(folder, timeout=1.0)
        self.assertIsNone(decoy.poll(), "stop_pids signalled a process that is not Chrome")


if __name__ == "__main__":
    unittest.main(verbosity=2)
