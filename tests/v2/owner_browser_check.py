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
- the refused schemes and a user name in the address, typed; the landing
  guard replacing a page that lands where the judge refuses;
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
injected and answers no name, and its connector refuses every address but
loopback: nothing reaches DNS or the network, Chrome's own background
traffic included. Run it under the memory cap:

    systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 \\
        env PYTHONPATH=. .venv/bin/python tests/v2/owner_browser_check.py
"""
from __future__ import annotations

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
PSS_LIMIT = 1.5 * 1024 ** 3
WAYS = ("iframe", "img", "fetch", "xhr", "ws", "form", "sw", "open")
NAV_WAYS = ("302", "meta", "loc")
ENV_ALLOWED = {"HOME", "PATH", "USER", "LOGNAME", "LANG", "LANGUAGE", "TZ"}


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


class Sanity(Listener):
    """An ordinary local server: answers every request with a tiny 200 and
    records which way (`?w=`) each one came by."""

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
                             b"Content-Length: 2\r\nConnection: close\r\n\r\nok")
            except OSError:
                pass


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


async def loopback_only(address, port):
    import ipaddress
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
        cls.sanity = Sanity()
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
        cls.daemon.browser = H.BrowserHost(cls.daemon, resolver=deny_every_name, connector=loopback_only)
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
            for listener in (*cls.stand_ins.values(), cls.sanity):
                listener.close()
            for p in reversed(cls.patches):
                p.stop()
            cls.tmp.cleanup()
            print("\nmeasured:", json.dumps(cls.measured), file=sys.stderr)
            if left:
                raise AssertionError(f"{len(left)} Chrome processes were left running")

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

    def attack(self, tab_id, target):
        """Every page-initiated way, aimed at `target` (host:port)."""
        type(self).sw += 1
        self.owner("POST", f"/browser/tabs/{tab_id}/navigate",
                   {"url": f"http://127.0.0.1:{self.fx}/multi?n={self.sw}&t={target}"})
        page = self.page(tab_id)
        self.assertTrue(self.wait(lambda: self.run_on(page.evaluate("window.__done === true")), 10))
        self.run_on(page.click("#open"))           # a user gesture, so no pop-up blocker
        time.sleep(2.0)
        self.close_others(tab_id)
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
        for flag in ("--headless", f"--user-data-dir={profile}", "--proxy-bypass-list=<-loopback>",
                     f"--proxy-server=socks5://127.0.0.1:{self.host._gate.port}", "--disable-quic",
                     "--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "--disable-extensions",
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
        self.assertFalse(H.load_settings()["lan"], "a refused PATCH changed nothing")
        self.assertIn(self.main_tab, [t["tab_id"] for t in self.owner("GET", "/browser/tabs")])

    # -- 3: the twelve ways (typed above) ----------------------------------------------

    def test_08_the_ways_reach_an_ordinary_server(self):
        # The sanity half: every way really fires, through the gate.
        allowed_before = self.host._gate.stats()["allowed"]
        for spelling in ("127.0.0.1", "localhost"):
            self.sanity.ways.clear()
            self.attack(self.main_tab, f"{spelling}:{self.sanity.port}")
            self.assertTrue(self.wait(lambda: set(WAYS) | set(NAV_WAYS) <= self.sanity.ways, 10),
                            f"{spelling}: only {sorted(self.sanity.ways)}")
        self.assertGreater(self.host._gate.stats()["allowed"], allowed_before)

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

    def test_11_history(self):
        tab = self.main_tab
        self.owner("POST", f"/browser/tabs/{tab}/navigate", {"url": f"http://127.0.0.1:{self.fx}/{MARK}/page"})
        self.owner("POST", f"/browser/tabs/{tab}/navigate", {"url": f"http://localhost:{self.fx}/echo"})
        self.owner("POST", f"/browser/tabs/{tab}/history", {"action": "back"})
        self.assertTrue(self.wait(lambda: self.page(tab).url.endswith(f"/{MARK}/page"), 10))
        self.assertEqual(self.ask("POST", f"/browser/tabs/{tab}/history", {"action": "jump"})[0], 400)
        self.assertEqual(self.ask("POST", "/browser/tabs/deadbeef/navigate", {"url": "about:blank"})[0], 404)
        self.assertEqual(self.ask("POST", "/browser/tabs/NOTANID/navigate", {"url": "about:blank"})[0], 400)

    def test_12_the_tab_cap(self):
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
        self.sanity.ways.clear()
        self.attack(row["tab_id"], f"127.0.0.1:{self.sanity.port}")
        self.assertTrue(self.wait(lambda: {"fetch", "iframe", "302"} <= self.sanity.ways, 10),
                        sorted(self.sanity.ways))
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
        self.assertTrue(self.wait(lambda: self.host.status()["instances"][0]["contexts"] == 0, 5))
        self.host.close_profile("private")
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
        self.assertEqual(self.owner("GET", "/browser/settings"), {"lan": False})
        self.assertEqual(self.ask("PATCH", "/browser/settings", {"lan": "yes"})[0], 400)
        self.assertEqual(self.ask("PATCH", "/browser/settings", {"sound": True})[0], 400)
        self.assertEqual(self.owner("PATCH", "/browser/settings", {"lan": True}), {"lan": True})
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
        self.refused("profiles.json is unreadable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
