"""The Browser's stream, input, hold, dialogs, tabs and files (BR-1b; plan §11's
BR-1b list, amendment 1 §B.8–§B.9), with real headless Google Chrome.

One port-0 daemon, its Browser reached through the owner-only `/browser`
routes and a tab's ticketed socket on the HUD listener, a window played by
`websocket-client`. What it proves:

- **the ticket and takeover** (the terminal's pattern): single use, one tab,
  30 s; owner-only (no Origin, a foreign Origin, the API listener); a tab
  another window shows is asked for, kept, given, or refused unanswered;
  every attach publishes `browser_attached` with exactly `{tab_id, at}`;
- **latest-frame delivery under a slow reader**: one frame in flight until
  the window acks, the newest one next, Chrome's frames meanwhile replaced
  (not queued); a hidden or detached window gets none; and the frame rate
  with an immediate ack;
- **keys, text, mouse and wheel** in BR-2's `remote.ts` shapes arriving at a
  fixture page that logs them as trusted events; every message of the wrong
  shape or past its bound refused (`{t:"bad"}`), the socket's 64 KiB cap;
- **the hold**: with an approval pending nothing arrives, `held` is sent,
  and nothing is replayed after the card resolves — not even input queued
  *before* the card; the owner's routes that reach a page answer 409; a press
  a card cut off and let go off the page (BR-2's `HeldButtons`) never clicks;
- **dialogs** forwarded and answered (alert, confirm, prompt), dismissed on
  their timeout and at once when no window shows the tab;
- **pop-ups** told to the opener's window and listed with their opener; the
  cap refuses a pop-up rather than closing a tab;
- **downloads** saved 0600 in a 0700 folder of their own under a cleaned
  name, served owner-only as an attachment that runs nothing, an executable
  one held; `files.save` keeps a `../` name inside its folder;
- **`set_files`**: a page's file chooser answered only through the owner's
  route, with H8's refusals (credential, hidden, symlink out, hard link,
  size) and the bytes of exactly the file judged;
- the `{t:"agent"}` channel: owner input pauses a driving agent before it is
  delivered, Resume is refused under a card, a reattach replays the list;
- **nothing on the bus or in the log**: no URL, title, typed text or frame.

**Hard rules** (the brief's): one Chrome at a time, at most 3 tabs
(`BROWSER_TAB_CAP=3`), `BROWSER_DIR` and HOME temp folders, the gate's
resolver answering nothing and its connector refusing everything but
loopback, fixture servers on ephemeral loopback ports, never 8402, 8403 or
8405, and no Chrome left behind (stopped by exact PID). Run it under the
memory cap:

    systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 \\
        env PYTHONPATH=. .venv/bin/python tests/v2/browser_routes_check.py
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import http.client
import json
import logging
import os
from pathlib import Path
import signal
import socket
import stat
import struct
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, quote, urlsplit

import websocket

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis import config  # noqa: E402
from jarvis.v2.approvals import ApprovalRequest  # noqa: E402
from jarvis.v2.browser import files as F, gate as G, host as H, policy as P, stream as S  # noqa: E402
from jarvis.v2.daemon import Daemon  # noqa: E402
from jarvis.v2.model import ProviderName as PN  # noqa: E402
from jarvis.v2.provider import Event, EventKind as K, SessionHandle, Usage  # noqa: E402
from jarvis.v2.stores import Stores  # noqa: E402

LIVE_PORTS = {8402, 8403, 8405}
MARK = "MARKbr1bq"          # in every fixture title, URL and file: never on the bus or in the log
TYPED = "TYPEDz"            # typed text: never on the bus or in the log
PSS_LIMIT = 1.5 * 1024 ** 3


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


def deny_every_name(name, port):
    raise socket.gaierror(socket.EAI_NONAME, "the suite resolves nothing")


async def loopback_only(address, port):
    import ipaddress
    if P.address_class(ipaddress.ip_address(address)) != "loopback":
        raise ConnectionRefusedError("the suite never leaves loopback")
    return await G.default_connect(address, port)


# -- the fixture pages ------------------------------------------------------------------------

LOGGER = """<script>
window.__log = [];
const L = (e, extra) => __log.push(Object.assign({type: e.type, trusted: e.isTrusted,
  target: e.target && e.target.id || (e.target && e.target.nodeName) || ''}, extra));
for (const k of ['keydown', 'keyup'])
  addEventListener(k, e => L(e, {key: e.key, code: e.code, ctrl: e.ctrlKey, shift: e.shiftKey}), true);
for (const k of ['mousedown', 'mouseup', 'click', 'auxclick', 'dblclick'])
  addEventListener(k, e => L(e, {x: e.clientX, y: e.clientY, button: e.button, detail: e.detail}), true);
addEventListener('wheel', e => L(e, {dy: e.deltaY}), {capture: true, passive: true});
addEventListener('input', e => L(e, {value: e.target.value}), true);
</script>"""

PAGES = {
    "/input": f"""<!doctype html><title>{MARK} input</title>
<style>body{{margin:0;height:3000px}} #b{{position:absolute;left:100px;top:100px;width:200px;height:80px}}
#t{{position:absolute;left:100px;top:300px;width:300px;height:40px}}
#s{{position:absolute;left:100px;top:400px}}</style>
<button id=b>press</button><input type=checkbox id=box><input id=t><p id=s>select me {MARK}</p>{LOGGER}""",
    "/anim": f"""<!doctype html><title>{MARK} anim</title><style>body{{margin:0}}</style>
<div id=d style="width:100vw;height:100vh"></div><script>
let i = 0; (function f() {{ d.style.background = 'hsl(' + (i++ % 360) + ',60%,50%)'; requestAnimationFrame(f); }})();
</script>""",
    "/dialogs": f"""<!doctype html><title>{MARK} dialogs</title><style>button{{position:absolute;width:100px;height:40px}}</style>
<button id=a style="left:10px;top:10px" onclick="alert('hello {MARK}'); __r.push('alert-done')">a</button>
<button id=c style="left:10px;top:60px" onclick="__r.push('confirm:' + confirm('sure?'))">c</button>
<button id=p style="left:10px;top:110px" onclick="__r.push('prompt:' + prompt('name?', 'def'))">p</button>
<button id=n style="left:10px;top:160px" onclick="location = '/input'">n</button>
<script>window.__r = [];</script>""",
    "/upload": f"""<!doctype html><title>{MARK} upload</title>
<input type=file id=f multiple style="position:absolute;left:10px;top:10px;width:300px;height:60px">
<script>window.__files = null;
f.addEventListener('change', async () => {{
  __files = await Promise.all([...f.files].map(async x => ({{name: x.name, size: x.size, text: await x.text()}})));
}});</script>""",
}


def fixture_handler(port_box):
    class Fixture(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, body: bytes, ctype="text/html", headers=()):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path in PAGES:
                return self._send(200, PAGES[url.path].encode())
            if url.path == "/popup":
                target = json.dumps(q.get("u", ""))
                return self._send(200, f"""<!doctype html><title>{MARK} popup</title>
<button id=o style="position:absolute;left:10px;top:10px;width:120px;height:40px"
 onclick='window.open({target})'>open</button>""".encode())
            if url.path == "/dl":
                name = q.get("n", "file.bin")
                body = {"elf": b"\x7fELF" + b"\0" * 60, "mz": b"MZ" + b"\0" * 62}.get(
                    q.get("k", ""), f"{MARK} download body\n".encode())
                return self._send(200, body, "application/octet-stream", headers=[
                    ("Content-Disposition", f"attachment; filename*=UTF-8''{quote(name, safe='')}")])
            return self._send(404, b"<title>no</title>")
    return Fixture


# -- the window ------------------------------------------------------------------------------

def eventually(step, timeout, what):
    deadline = time.monotonic() + timeout
    while True:
        value = step()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.02)


def jpeg_size(data: bytes) -> tuple[int, int]:
    """A JPEG's pixel width and height, from its start-of-frame marker."""
    i = 2
    while i + 9 <= len(data):
        if data[i] != 0xFF:
            raise AssertionError("a frame's JPEG markers are malformed")
        marker, length = data[i + 1], struct.unpack("!H", data[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2):
            height, width = struct.unpack("!HH", data[i + 5:i + 9])
            return width, height
        i += 2 + length
    raise AssertionError("a frame has no JPEG size")


class Window:
    """A HUD window's socket to one tab: keeps every frame (id, CSS width and
    height, bytes, JPEG pixel width and height) and message."""

    def __init__(self, conn):
        self.conn = conn
        self.frames: list[tuple[int, int, int, int, int, int]] = []
        self.messages: list[dict] = []
        self.closed = None
        conn.settimeout(0.05)

    def pump(self):
        try:
            opcode, data = self.conn.recv_data()
        except websocket.WebSocketTimeoutException:
            return
        except (websocket.WebSocketConnectionClosedException, OSError):
            self.closed = self.closed or "dropped"
            return
        if opcode == websocket.ABNF.OPCODE_BINARY:
            fid, width, height = struct.unpack("!IHH", data[:8])
            if data[8:10] != b"\xff\xd8":
                raise AssertionError("a frame is not a JPEG")
            self.frames.append((fid, width, height, len(data) - 8, *jpeg_size(data[8:])))
        elif opcode == websocket.ABNF.OPCODE_TEXT:
            self.messages.append(json.loads(data))
        elif opcode == websocket.ABNF.OPCODE_CLOSE:
            self.closed = struct.unpack("!H", data[:2])[0] if len(data) >= 2 else 1005

    def drain(self, seconds: float):
        end = time.monotonic() + seconds
        while time.monotonic() < end and self.closed is None:
            self.pump()

    def until(self, predicate, timeout=8.0, what="window condition"):
        def step():
            if self.closed is None:
                self.pump()
            return predicate(self)
        return eventually(step, timeout, what)

    def send(self, payload: dict):
        self.conn.send(json.dumps(payload))

    def settled(self):
        """Fixture input at human pace: wait for the prior CDP answer.
        Safety/race tests use raw send() to exercise banked input explicitly."""
        before = len([m for m in self.messages if m.get("t") == "pong"])
        self.send({"t": "ping"})
        self.until(lambda w: len([m for m in w.messages if m.get("t") == "pong"]) > before,
                   what="reader consumed prior input")
        eventually(self.input_idle, 6, "prior CDP input answered")

    def kinds(self):
        return [m.get("t") for m in self.messages]

    def last(self, t, **match):
        for m in reversed(self.messages):
            if m.get("t") == t and all(m.get(k) == v for k, v in match.items()):
                return m
        return None

    def wait_for(self, t, timeout=8.0, **match):
        return self.until(lambda w: w.last(t, **match), timeout, f"{t} {match}")

    def ack_last(self):
        if self.frames:
            self.send({"t": "ack", "id": self.frames[-1][0]})

    def close(self):
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass


# BR-2's `remote.ts` shapes (`cdpKey`, `cdpMouse`, `cdpWheel`, `HeldButtons`,
# `pasteMessages`), spelled out.

def key(k, code, vk, text=None, *, up=False, mods=0):
    e = {"type": "keyUp", "modifiers": mods, "key": k, "code": code, "windowsVirtualKeyCode": vk,
         "nativeVirtualKeyCode": vk, "location": 0}
    if not up:
        e["type"] = "keyDown" if text else "rawKeyDown"
        if text:
            e["text"] = e["unmodifiedText"] = text
    return {"t": "key", "e": e}


def mouse(kind, x, y, *, button="left", buttons=None, count=1):
    pressed = kind != "mouseMoved"
    if buttons is None:
        buttons = 1 if kind == "mousePressed" else 0
    return {"t": "mouse", "e": {"type": kind, "x": x, "y": y, "modifiers": 0, "button": button,
                                 "buttons": buttons, "clickCount": count if pressed else 0,
                                 "pointerType": "mouse"}}


def click(window, x, y):
    window.send(mouse("mousePressed", x, y))
    window.send(mouse("mouseReleased", x, y))


def wheel(x, y, dy):
    return {"t": "mouse", "e": {"type": "mouseWheel", "x": x, "y": y, "modifiers": 0, "button": "none",
                                 "buttons": 0, "clickCount": 0, "deltaX": 0, "deltaY": dy,
                                 "pointerType": "mouse"}}


def type_keys(window, text, *, pace=False):
    for ch in text:
        code = f"Key{ch.upper()}" if ch.isalpha() else ("Digit" + ch if ch.isdigit() else "")
        vk = ord(ch.upper())
        if pace:
            window.settled()
        window.send(key(ch, code, vk, ch))
        if pace:
            window.settled()
        window.send(key(ch, code, vk, up=True))
    if pace:
        window.settled()


def chrome_tree(folder: Path) -> list[int]:
    return H.chrome_pids(folder)


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


class Pure(unittest.TestCase):
    """The message rules, cleaned names and tickets: no Chrome."""

    def test_br2_shapes_parse(self):
        good = [key("a", "KeyA", 65, "a"), key("a", "KeyA", 65, up=True), key("Enter", "Enter", 13, "\r"),
                key("Backspace", "Backspace", 8), key("@", "KeyQ", 81, "@"),
                key("a", "KeyA", 65, mods=2),
                mouse("mousePressed", 10.5, 20), mouse("mouseReleased", 10.5, 20),
                mouse("mouseMoved", 0, 0, button="none"), wheel(5, 5, -120),
                # HeldButtons.releaseAll: off the page, never a click.
                mouse("mouseMoved", -1, -1, button="left", buttons=1),
                mouse("mouseReleased", -1, -1, button="left", buttons=0, count=0),
                {"t": "text", "text": "line one\r\nline two\ttab " + "é" * 8000},
                {"t": "ack", "id": 7}, {"t": "resize", "width": 800, "height": 600, "deviceScaleFactor": 1.5},
                {"t": "visible", "visible": False}, {"t": "resume"}, {"t": "stop"}, {"t": "selection"},
                {"t": "focus"}, {"t": "blur"}, {"t": "ping"},
                {"t": "dialog", "id": "0123abcd", "accept": True, "text": "x"},
                {"t": "takeover", "id": "x", "allow": True}]
        for message in good:
            with self.subTest(message=str(message)[:80]):
                S.parse(json.dumps(message).encode())
        self.assertTrue(S.parse(json.dumps(key("a", "KeyA", 65, "a")).encode()).takeover)
        self.assertFalse(S.parse(json.dumps(key("Control", "ControlLeft", 17)).encode()).takeover)
        self.assertFalse(S.parse(json.dumps(mouse("mouseMoved", 1, 1, button="none")).encode()).takeover)
        self.assertTrue(S.parse(json.dumps(mouse("mouseMoved", 1, 1, buttons=1)).encode()).takeover)
        self.assertFalse(S.parse(b'{"t":"ack","id":1}').takeover)

    def test_every_wrong_shape_and_bound_is_refused(self):
        k = key("a", "KeyA", 65, "a")["e"]
        m = mouse("mousePressed", 1, 1)["e"]
        bad = [b"not json", b"[]", b'{"t":1}', b'{"t":"nope"}', b'{"t":"key"}',
               {"t": "key", "e": {**k, "extra": 1}}, {"t": "key", "e": {**k, "type": "char"}},
               {"t": "key", "e": {**k, "modifiers": 16}}, {"t": "key", "e": {**k, "modifiers": True}},
               {"t": "key", "e": {**k, "key": ""}}, {"t": "key", "e": {**k, "key": "x" * 33}},
               {"t": "key", "e": {**k, "key": "\x1b"}}, {"t": "key", "e": {**k, "code": "Key A"}},
               {"t": "key", "e": {**k, "windowsVirtualKeyCode": 256}},
               {"t": "key", "e": {**k, "text": "\x1b[201~"}}, {"t": "key", "e": {**k, "text": "x" * 9}},
               {"t": "key", "e": {**k, "location": 4}}, {"t": "key", "e": {**k, "autoRepeat": 1}},
               {"t": "key", "e": k, "x": 1},
               {"t": "mouse", "e": {**m, "x": 4097}}, {"t": "mouse", "e": {**m, "y": -2}},
               {"t": "mouse", "e": {**m, "x": float("nan")}}, {"t": "mouse", "e": {**m, "button": "x"}},
               {"t": "mouse", "e": {**m, "buttons": 32}}, {"t": "mouse", "e": {**m, "clickCount": 4}},
               {"t": "mouse", "e": {**m, "pointerType": "touch"}}, {"t": "mouse", "e": {**m, "deltaY": 3}},
               {"t": "mouse", "e": {**m, "type": "mouseWheel"}},
               wheel(1, 1, 5001), {"t": "mouse", "e": {**m, "extra": 1}},
               {"t": "text", "text": ""}, {"t": "text", "text": "x" * (16 * 1024 + 1)},
               {"t": "text", "text": "é" * 8193}, {"t": "text", "text": "a\x1bb"},
               {"t": "text", "text": "a\x85b"}, {"t": "text", "text": 5},
               {"t": "ack", "id": -1}, {"t": "ack", "id": "1"},
               {"t": "resize", "width": 0, "height": 10}, {"t": "resize", "width": 4097, "height": 10},
               {"t": "resize", "width": 4096, "height": 4096, "deviceScaleFactor": 2},
               {"t": "visible", "visible": 1}, {"t": "resume", "x": 1},
               {"t": "dialog", "id": "zz", "accept": True}, {"t": "dialog", "id": "0123abcd", "accept": 1},
               {"t": "dialog", "id": "0123abcd", "accept": True, "text": "x" * 5000}]
        for message in bad:
            raw = message if isinstance(message, bytes) else json.dumps(message).encode()
            with self.subTest(message=raw[:80]):
                with self.assertRaises(S.Bad):
                    S.parse(raw)
        # A lone surrogate (JSON can carry one) is not text.
        with self.assertRaises(S.Bad):
            S.parse(b'{"t":"text","text":"a\\ud800b"}')

    def test_clean_names(self):
        cases = {"../../escape.txt": "escape.txt", "..\\..\\x.exe": "x.exe", ".env": "env",
                 "...hidden": "hidden", "a\x00b.txt": "ab.txt", "re‮port.txt": "report.txt",
                 "re​port.pdf": "report.pdf", "a<b>c:d|e?f*.txt": "a_b_c_d_e_f_.txt",
                 "trail. . ": "trail", "": "download", "   ": "download", "/": "download",
                 "é.txt": "é.txt"}
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(F.clean_name(raw), want)
        long = "x" * 300 + ".tar.gz"
        self.assertEqual(len(F.clean_name(long)), F.NAME_CAP)
        self.assertTrue(F.clean_name(long).endswith(".gz"))
        self.assertTrue(F.executable("setup.exe"))
        self.assertTrue(F.executable("SETUP.EXE."))
        self.assertTrue(F.executable("tool", b"\x7fELF\x02"))
        self.assertTrue(F.executable("run", b"#!/bin/sh"))
        self.assertFalse(F.executable("notes.txt", b"hello"))

    def test_tickets(self):
        now = [100.0]
        tickets = S.Tickets(clock=lambda: now[0])
        one = tickets.mint("aaaaaaaa")
        self.assertFalse(tickets.redeem(one, "bbbbbbbb"), "bound to its tab")
        self.assertFalse(tickets.redeem(one, "aaaaaaaa"), "spent by being presented")
        two = tickets.mint("aaaaaaaa")
        now[0] += S.TICKET_TTL_S + 0.1
        self.assertFalse(tickets.redeem(two, "aaaaaaaa"), "expired")
        self.assertFalse(tickets.redeem("", "aaaaaaaa"))
        self.assertFalse(tickets.redeem(None, "aaaaaaaa"))
        three = tickets.mint("aaaaaaaa")
        self.assertTrue(tickets.redeem(three, "aaaaaaaa"))

    def test_a_dot_dot_name_never_leaves_its_folder(self):
        tmp = tempfile.TemporaryDirectory(prefix="brc-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        with patch.object(config, "BROWSER_DIR", root / "browser"):
            H.secure_dir(root / "browser")
            source = root / "src.bin"
            source.write_bytes(b"x" * 10)
            row = F.save(str(source), "p-00000000", "../../escape.txt", "0123abcd")
            folder = root / "browser" / "downloads" / "p-00000000" / "0123abcd"
            self.assertEqual(sorted(os.listdir(folder)), ["escape.txt"])
            self.assertFalse((root / "browser" / "downloads" / "escape.txt").exists())
            self.assertFalse((root / "browser" / "escape.txt").exists())
            self.assertEqual(row["name"], "escape.txt")
            self.assertEqual(stat.S_IMODE(os.stat(folder / "escape.txt").st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o700)
            with self.assertRaises(FileExistsError):
                F.save(str(source), "p-00000000", "again.txt", "0123abcd")


    def test_long_utf8_name_saves(self):
        with tempfile.TemporaryDirectory(prefix="brc-") as tmp:
            root = Path(tmp)
            with patch.object(config, "BROWSER_DIR", root / "browser"):
                H.secure_dir(config.BROWSER_DIR)
                source = root / "source"
                source.write_bytes(b"data")
                row = F.save(str(source), "p-00000000", "界" * 119 + ".txt", "0123abcd")
                self.assertLessEqual(len(row["name"].encode("utf-8")), 200)
                self.assertTrue(row["name"].endswith(".txt"))
                self.assertEqual((config.BROWSER_DIR / "downloads" / "p-00000000" / "0123abcd" / row["name"]).read_bytes(), b"data")

    def test_windows_reserved_names(self):
        for name in ("CON", "nul.txt", "com1.log", "LPT9", "PrN.pdf", "aux"):
            with self.subTest(name=name):
                self.assertEqual(F.clean_name(name), "_" + name)

    def test_executable_extensions(self):
        extensions = "js jse wsf wsh vbe vbs chm url scf msc appref-ms com py pyw desktop command hta lnk ps1 psm1 reg jar msi msp scr cpl bat cmd exe dll sh run appimage deb rpm pkg dmg".split()
        for ext in extensions:
            for prefix in ("x.", "x.pdf."):
                with self.subTest(ext=ext, prefix=prefix):
                    self.assertTrue(F.executable(prefix + ext.upper(), b"plain bytes"))
        self.assertTrue(F.executable("x.com", b"MZ"))

    def test_pump_checks_hold_without_observer(self):
        async def check():
            host = H.BrowserHost()
            host.input_held = lambda: True
            stream = S.TabStream("aaaaaaaa")
            stream._queue.append((1, stream.gen, S.Msg("text", "Input.insertText", {"text": "banked"})))
            tab = H._Tab(id="aaaaaaaa", profile="p-00000000", ctx=None, page=None,
                         stream=stream, wakeup=asyncio.Event())
            sent = []
            async def deliver(*args):
                sent.append(args)
            host._deliver = deliver
            tab.wakeup.set()
            task = asyncio.create_task(host._pump(tab))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self.assertEqual(sent, [], "the pump sent input while an unobserved card was pending")
        asyncio.run(check())

    def test_popup_closes_before_waiting_for_opener(self):
        async def check():
            calls = []
            class Popup:
                async def close(self):
                    calls.append("close")
                async def opener(self):
                    calls.append("opener")
                    return None
            await H.BrowserHost()._refuse_popup(Popup())
            self.assertEqual(calls, ["close", "opener"])
        asyncio.run(check())

    def test_unanswered_input_drops_without_a_dialog(self):
        async def check():
            gate = asyncio.Event()
            sent = []
            class CDP:
                async def send(self, method, params):
                    sent.append(params)
                    await gate.wait()
            host = H.BrowserHost()
            tab = H._Tab(id="aaaaaaaa", profile="p-00000000", ctx=None, page=None, cdp=CDP())
            tab.stream = S.TabStream(tab.id, blocked=lambda: tab.input_task is not None and not tab.input_task.done())
            instance = H._Instance(tab.profile, Path("/synthetic/browser-route-fixture"),
                                   SimpleNamespace(), SimpleNamespace(), generation=1)
            host._instances[tab.profile] = instance
            host._tabs[tab.id] = tab
            self.assertTrue(host.activity.opened(tab.profile, instance.generation))
            with patch.object(H, "DELIVER_WAIT_S", 0.01):
                await host._deliver(tab, "Input.insertText", {"text": "first"})
                await host._deliver(tab, "Input.insertText", {"text": "banked"})
            gate.set()
            await tab.input_task
            self.assertEqual(sent, [{"text": "first"}])
        asyncio.run(check())

    def test_blocked_input_is_dropped_on_arrival(self):
        messages = []
        sock = SimpleNamespace(send_text=lambda raw: messages.append(json.loads(raw)))
        blocked = [True]
        stream = S.TabStream("aaaaaaaa", blocked=lambda: blocked[0])
        stream._sock = sock
        msg = S.Msg("text", "Input.insertText", {"text": "banked"})
        stream.enqueue(sock, msg)
        stream.enqueue(sock, msg)
        self.assertIsNone(stream.take(), "blocked input was queued for later")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0], {"t": "input_dropped", "reason": "the page has a dialog open or an input still unanswered; this input was dropped"})
        blocked[0] = False
        stream.enqueue(sock, msg)
        self.assertIsNotNone(stream.take())
        blocked[0] = True
        stream.enqueue(sock, msg)
        self.assertEqual(len(messages), 2)

    def test_cut_off_char_is_dropped(self):
        async def check():
            sent = []
            class CDP:
                async def send(self, method, params):
                    sent.append(params)
            host = H.BrowserHost()
            tab = H._Tab(id="aaaaaaaa", profile="p-00000000", ctx=None, page=None, cdp=CDP(), stream=S.TabStream("aaaaaaaa"))
            instance = H._Instance(tab.profile, Path("/synthetic/browser-route-fixture"),
                                   SimpleNamespace(), SimpleNamespace(), generation=1)
            host._instances[tab.profile] = instance
            host._tabs[tab.id] = tab
            self.assertTrue(host.activity.opened(tab.profile, instance.generation))
            await host._deliver(tab, "Input.dispatchKeyEvent", {"type": "rawKeyDown", "code": "Space"})
            tab.stream.hold_through(notify=False)
            await host._deliver(tab, "Input.dispatchKeyEvent", {"type": "char", "code": "Space", "text": " "})
            await host._deliver(tab, "Input.dispatchKeyEvent", {"type": "keyUp", "code": "Space"})
            await host._deliver(tab, "Input.dispatchKeyEvent", {"type": "keyUp", "code": "Space"})
            await host._deliver(tab, "Input.dispatchKeyEvent", {"type": "char", "code": "Space", "text": " "})
            await host._deliver(tab, "Input.dispatchKeyEvent", {"type": "rawKeyDown", "code": "Space", "autoRepeat": True})
            self.assertEqual(len(sent), 1)
        asyncio.run(check())


class BrowserStream(unittest.TestCase):
    """Ordered sections sharing one daemon and one Chrome (at most 3 tabs)."""

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory(prefix="brc-")
        cls.tmp = tmp
        cls.root = Path(tmp.name)
        cls.home = cls.root / "home"
        cls.home.mkdir()
        cls.work = cls.root / "work"
        cls.work.mkdir()
        cls.patches = []

        def keep(p):
            p.start()
            cls.patches.append(p)

        keep(patch.dict(os.environ, {"HOME": str(cls.home), "XDG_DATA_HOME": str(cls.root / "xdg"),
                                     "JARVIS_CLAUDE_CREDENTIALS": str(cls.root / "none.json"),
                                     "OPENROUTER_API_KEY": "sk-dummy-not-a-key", "LC_ALL": "C.UTF-8"}))
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
                      BROWSER_TAB_CAP=3)
        for name, value in values.items():
            keep(patch.object(config, name, value))
        cls.fixture = ThreadingHTTPServer(("127.0.0.1", 0), fixture_handler(None))
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

        cls.daemon = Daemon(Stores(cls.root / "stores"), {name: Fake(name) for name in PN},
                            lambda *_: (lambda *_: None), 0)
        cls.daemon.browser = H.BrowserHost(cls.daemon, resolver=deny_every_name, connector=loopback_only)
        cls.daemon.bus.observe(cls.records.append)
        cls.daemon.start()
        cls.ports = {"api": cls.daemon.port, "hud": cls.daemon.face_port}
        assert not set(cls.ports.values()) & LIVE_PORTS
        cls.host = cls.daemon.browser
        cls.project = cls.daemon.stores.projects.create("Work", str(cls.work))
        cls.windows: list[Window] = []
        cls.measured: dict = {}
        cls.tab = None

    @classmethod
    def tearDownClass(cls):
        try:
            for window in cls.windows:
                window.close()
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
            for p in reversed(cls.patches):
                p.stop()
            cls.tmp.cleanup()
            print("\nmeasured:", json.dumps(cls.measured), file=sys.stderr)
            if os.environ.get("BROWSER_ROUTES_LOG"):
                print("\n".join(line for line in cls.logs if "browser" in line.lower()
                                or "Browser" in line), file=sys.stderr)
            if left:
                raise AssertionError(f"{len(left)} Chrome processes were left running")

    # -- helpers ------------------------------------------------------------------------------

    def ask(self, method, path, body=None, *, listener="hud", origin=True, headers=None, raw=False):
        port = self.ports[listener]
        h = {"Content-Type": "application/json"} if body is not None else {}
        if origin:
            h["Origin"] = f"http://127.0.0.1:{port}"
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        try:
            conn.request(method, path, json.dumps(body).encode() if body is not None else None, h)
            response = conn.getresponse()
            data = response.read()
            got = dict(response.getheaders())
        finally:
            conn.close()
        if raw:
            return response.status, got, data
        try:
            payload = json.loads(data) if data else None
        except ValueError:
            payload = data
        return response.status, payload

    def owner(self, method, path, body=None):
        status, payload = self.ask(method, path, body)
        self.assertLess(status, 300, f"{method} {path}: {status} {payload}")
        return payload

    def url(self, path):
        return f"http://127.0.0.1:{self.fx}{path}"

    def run_on(self, coro, timeout=30):
        return self.host._call(coro, timeout)

    def page(self, tab_id):
        return self.host._tabs[tab_id].page

    def js(self, tab_id, expression):
        try:
            return self.run_on(self.page(tab_id).evaluate(expression))
        except H.BrowserRefused:
            return None

    def wait(self, predicate, timeout=10.0, what="condition"):
        return eventually(predicate, timeout, what)

    def ticket(self, tab_id):
        return self.owner("POST", f"/browser/tabs/{tab_id}/ticket", {})["ticket"]

    def connect(self, tab_id, ticket="fresh", *, origin="hud", listener="hud"):
        if ticket == "fresh":
            ticket = self.ticket(tab_id)
        port = self.ports[listener]
        options = {"origin": f"http://127.0.0.1:{self.ports['hud']}"} if origin == "hud" else (
            {"suppress_origin": True} if origin is None else {"origin": origin})
        conn = websocket.create_connection(
            f"ws://127.0.0.1:{port}/browser/tabs/{tab_id}/attach?ticket={ticket}", timeout=10, **options)
        window = Window(conn)
        tab = self.host._tabs[tab_id]
        window.input_idle = lambda: not tab.stream._queue and (tab.input_task is None or tab.input_task.done())
        self.windows.append(window)
        self.addCleanup(window.close)           # a failed section never holds the tab for the next
        return window

    def refused(self, tab_id, ticket="fresh", **kw):
        with self.assertRaises(websocket.WebSocketBadStatusException) as caught:
            self.connect(tab_id, ticket, **kw)
        return caught.exception.status_code

    def attached(self, tab_id):
        window = self.connect(tab_id)
        window.wait_for("attached")
        window.wait_for("agent", kind="list")       # the agent channel's replay follows `attached`
        return window

    def stream_until(self, window, predicate, what, timeout=8.0):
        """Read frames as a drawing window does: ack each one as it lands."""
        def step(w):
            w.ack_last()
            return predicate(w)
        return window.until(step, timeout, what)

    def navigate(self, tab_id, path):
        self.owner("POST", f"/browser/tabs/{tab_id}/navigate", {"url": self.url(path)})
        self.wait(lambda: self.js(tab_id, "document.readyState") == "complete", 10, f"{path} loaded")

    def log(self, tab_id, kind=None):
        rows = self.js(tab_id, "window.__log || []") or []
        return [r for r in rows if kind is None or r["type"] == kind]

    def close_others(self, keep):
        for row in self.owner("GET", "/browser/tabs"):
            if row["tab_id"] != keep:
                self.owner("DELETE", f"/browser/tabs/{row['tab_id']}")

    @contextmanager
    def card(self):
        """An approval waiting on the owner (a real broker request)."""
        request = ApprovalRequest(tool="Bash", args={"command": "true"}, command="true")
        thread = threading.Thread(target=self.daemon.approvals.ask, args=(request,), daemon=True)
        thread.start()
        self.wait(lambda: self.daemon.approvals.pending(), 5, "the card")
        try:
            yield request
        finally:
            try:
                self.daemon.approvals.resolve(request.req_id, "deny")
            except ValueError:
                pass
            thread.join(5)

    def measure(self, name):
        pss = pss_bytes(chrome_tree(config.BROWSER_DIR))
        self.measured[f"pss_mb_{name}"] = round(pss / 2 ** 20, 1)
        self.assertLess(pss, PSS_LIMIT, f"the Chrome tree uses {pss / 2 ** 20:.0f} MiB")

    # -- 1: the routes before Chrome ---------------------------------------------------------------

    def test_01_nothing_starts_and_everything_is_owner_only(self):
        self.assertEqual(self.ask("POST", "/browser/tabs/0123abcd/ticket", {})[0], 404)
        self.assertEqual(self.owner("GET", "/browser/downloads"), {"downloads": [], "in_progress": 0})
        self.assertEqual(self.ask("GET", "/browser/downloads/0123abcd")[0], 404)
        self.assertEqual(self.ask("GET", "/browser/downloads/../x")[0], 404)
        self.assertEqual(self.ask("POST", "/browser/tabs/0123abcd/files",
                                  {"chooser": "x", "files": []})[0], 404)
        for listener, origin in (("api", True), ("hud", False)):
            for method, path, body in (("POST", "/browser/tabs/0123abcd/ticket", {}),
                                       ("GET", "/browser/downloads", None),
                                       ("POST", "/browser/tabs/0123abcd/activate", {}),
                                       ("POST", "/browser/tabs/0123abcd/files", {"chooser": "x", "files": []})):
                with self.subTest(listener=listener, path=path):
                    self.assertEqual(self.ask(method, path, body, listener=listener, origin=origin)[0], 403)
        self.assertFalse(config.BROWSER_DIR.exists(), "nothing started Chrome or made its folder")

    # -- 2: the ticket and the socket ---------------------------------------------------------------

    def test_02_ticket_attach_and_frames(self):
        row = self.owner("POST", "/browser/tabs", {"url": self.url("/input")})
        tab = type(self).tab = row["tab_id"]
        self.assertEqual(self.refused(tab, self.ticket(tab), origin=None), 403, "a socket needs the HUD's Origin")
        # A ticket is single use: once an attach has spent it, it opens nothing.
        ticket = self.ticket(tab)
        status, got, _ = self.ask("GET", f"/browser/tabs/{tab}/attach?ticket={ticket}", raw=True,
                                  headers={"Upgrade": "websocket", "Connection": "Upgrade",
                                           "Sec-WebSocket-Version": "13",
                                           "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="})
        self.assertEqual(status, 101)
        # The hand-written 101 carries the listener's frame headers (WP-E).
        self.assertIn("frame-ancestors 'none'", got["Content-Security-Policy"])
        self.assertEqual(got["X-Frame-Options"], "DENY")
        self.wait(lambda: not self.host._tabs[tab].stream.attached(), 5, "the raw attach let go")
        self.assertEqual(self.refused(tab, ticket), 403, "a ticket is spent by being presented")
        self.assertEqual(self.refused(tab, self.ticket(tab), origin="http://evil.example"), 403)
        self.assertEqual(self.refused(tab, self.ticket(tab), listener="api"), 403)
        self.assertEqual(self.refused(tab, None), 403)
        self.assertEqual(self.refused("0123abcd", self.ticket(tab)), 404)
        # A ticket is bound to its tab.
        other = self.owner("POST", "/browser/tabs", {})["tab_id"]
        self.assertEqual(self.refused(other, self.ticket(tab)), 403)
        self.owner("DELETE", f"/browser/tabs/{other}")
        before = len([r for r in self.records if r.get("kind") == "browser_attached"])
        window = self.attached(tab)
        attached = window.last("attached")
        self.assertEqual(attached["tab"]["tab_id"], tab)
        self.assertEqual(set(attached["tab"]), {"tab_id", "profile", "ctx", "opener"})
        window.wait_for("location")
        self.assertTrue(window.last("location")["url"].endswith("/input"))
        # The agent channel's state on attach: not paused, an empty list.
        self.assertEqual(window.last("agent", kind="paused"),
                         {"t": "agent", "kind": "paused", "paused": False, "by": None})
        self.assertEqual(window.last("agent", kind="list")["actions"], [])
        window.until(lambda w: w.frames, what="a frame")
        fid, width, height, size, *_ = window.frames[0]
        self.assertEqual((width, height), (1280, 800))
        self.assertGreater(size, 100)
        events = [r for r in self.records if r.get("kind") == "browser_attached"][before:]
        self.assertEqual(len(events), 1)
        self.assertEqual(set(events[0]["data"]), {"tab_id", "at"})
        self.assertEqual(events[0]["data"]["tab_id"], tab)
        listed = {r["tab_id"]: r for r in self.owner("GET", "/browser/tabs")}
        self.assertTrue(listed[tab]["shown"])
        self.assertIsNone(listed[tab]["opener"])
        status = self.ask("GET", "/status", listener="api")[1]["browser"]
        self.assertEqual(status["streams"], 1)
        window.close()
        self.wait(lambda: not self.host._tabs[tab].stream.attached(), 5, "the window let go")

    def test_03_takeover(self):
        tab = self.tab
        first = self.attached(tab)

        def asks(w):
            return [m for m in w.messages if m.get("t") == "takeover_request"]
        second = self.connect(tab)
        first.until(lambda w: len(asks(w)) == 1, what="the first question")
        second.wait_for("waiting")
        first.send({"t": "takeover", "id": asks(first)[0]["id"], "allow": False})
        second.wait_for("refused")
        self.assertIn("kept it", second.last("refused")["reason"])
        # Unanswered: refused when the wait runs out.
        with patch.object(S, "TAKEOVER_TIMEOUT_S", 1.0):
            third = self.connect(tab)
            first.until(lambda w: len(asks(w)) == 2, what="the second question")
            third.wait_for("refused", timeout=6)
            self.assertIn("did not answer", third.last("refused")["reason"])
        # Allowed: the first is told it was taken and let go; the second streams.
        fourth = self.connect(tab)
        first.until(lambda w: len(asks(w)) == 3, what="the third question")
        first.send({"t": "takeover", "id": asks(first)[2]["id"], "allow": True})
        fourth.wait_for("attached")
        first.wait_for("taken")
        first.until(lambda w: w.closed is not None, what="the first window closed")
        fourth.until(lambda w: w.frames, what="frames for the new window")
        fourth.close()

    # -- 3: frames under a slow reader --------------------------------------------------------------

    def test_04_latest_frame_under_a_slow_reader(self):
        tab = self.tab
        self.navigate(tab, "/anim")
        window = self.attached(tab)
        window.until(lambda w: w.frames, what="a first frame")
        stream = self.host._tabs[tab].stream
        # The window draws nothing for two seconds: exactly one frame in
        # flight, Chrome's newer ones replacing the one that waits.
        window.drain(2.0)
        self.assertEqual(len(window.frames), 1, "a frame went out before the last was acked")
        replaced = stream.frames_replaced
        self.assertGreater(replaced, 10, "the animation produced frames meanwhile")
        self.assertLessEqual(self.host._tabs[tab].stream.attached()._pending, S.FRAME_MAX_BYTES + 4096)
        first = window.frames[0][0]
        window.ack_last()
        window.until(lambda w: len(w.frames) >= 2, what="the newest frame")
        self.assertGreater(window.frames[1][0], first + 10, "the next frame is the newest, not the next one")
        # An ack for an older frame changes nothing; an immediate ack streams.
        window.send({"t": "ack", "id": 0})
        start, count = time.monotonic(), len(window.frames)
        window.ack_last()
        end = start + 3.0
        while time.monotonic() < end:
            seen = len(window.frames)
            window.pump()
            if len(window.frames) > seen:
                window.ack_last()
        fps = (len(window.frames) - count) / (time.monotonic() - start)
        self.measured["fps_immediate_ack"] = round(fps, 1)
        self.measured["frame_kb"] = round(sum(f[3] for f in window.frames) / len(window.frames) / 1024, 1)
        self.assertGreater(fps, 5)
        # Hidden: nothing; visible again: frames.
        window.send({"t": "visible", "visible": False})
        time.sleep(0.5)
        window.drain(0.3)
        window.ack_last()
        seen = len(window.frames)
        window.drain(1.0)
        self.assertLessEqual(len(window.frames) - seen, 1, "a hidden window gets no frames")
        window.send({"t": "visible", "visible": True})
        self.stream_until(window, lambda w: len(w.frames) > seen + 3, "frames once visible")
        # A resize: the viewport and the frames follow.
        window.send({"t": "resize", "width": 640, "height": 400, "deviceScaleFactor": 1.5})
        self.stream_until(window, lambda w: any(f[1:3] == (640, 400) for f in w.frames),
                          "a frame at the new viewport")
        self.assertEqual(self.js(tab, "[innerWidth, innerHeight, devicePixelRatio]"), [640, 400, 1.5])
        # In the window's device pixels, not stretched from 1× (RASTER_SCALE).
        self.stream_until(window, lambda w: w.frames[-1][1:3] == (640, 400), "a newest frame at 640 × 400")
        self.assertEqual(window.frames[-1][4:6], (960, 600), "a 1.5× pane gets 1.5× pixels")
        window.send({"t": "resize", "width": 1280, "height": 800, "deviceScaleFactor": 1})
        self.measure("frames")
        window.close()
        self.wait(lambda: not stream.attached(), 5, "the window let go")
        time.sleep(0.5)
        self.assertIsNone(self.host._tabs[tab].casting, "a tab nobody shows is not cast")

    # -- 4: input ----------------------------------------------------------------------------------

    def test_05_keys_text_mouse_and_wheel(self):
        tab = self.tab
        self.navigate(tab, "/input")
        window = self.attached(tab)
        click(window, 200, 140)
        self.wait(lambda: self.log(tab, "click"), what="the click")
        clicks = self.log(tab, "click")
        self.assertEqual(len(clicks), 1)
        self.assertTrue(clicks[0]["trusted"])
        self.assertEqual((clicks[0]["x"], clicks[0]["y"], clicks[0]["detail"]), (200, 140, 1))
        click(window, 250, 320)                         # focus the text field
        type_keys(window, "hi")
        window.send({"t": "text", "text": f" {TYPED} pasted"})
        window.settled()
        window.send(key("Backspace", "Backspace", 8))
        window.settled()
        window.send(key("Backspace", "Backspace", 8, up=True))
        self.wait(lambda: self.js(tab, "t.value") == f"hi {TYPED} paste", what="typed text")
        keys = [r["key"] for r in self.log(tab, "keydown")]
        self.assertEqual(keys, ["h", "i", "Backspace"])
        self.assertTrue(all(r["trusted"] for r in self.log(tab)))
        window.send(wheel(400, 500, 600))
        self.wait(lambda: (self.js(tab, "scrollY") or 0) > 0, what="the wheel scrolled")
        # Chrome reports deltaY × its own scale ÷ the page's (measured; the
        # scroll itself is the delta sent), so a 1× window reads 1.5×.
        self.assertEqual(self.log(tab, "wheel")[0]["dy"], 600 * H.RASTER_SCALE / self.js(tab, "devicePixelRatio"))
        # A bad message is refused in words and reaches nothing.
        before = len(self.log(tab))
        window.send({"t": "key", "e": {"type": "char", "key": "x"}})
        window.send({"t": "text", "text": "a\x1b[201~b"})
        window.send({"t": "mouse", "e": {**mouse("mousePressed", 1, 1)["e"], "x": 99999}})
        window.until(lambda w: len([m for m in w.messages if m.get("t") == "bad"]) >= 3, what="three refusals")
        for m in [m for m in window.messages if m.get("t") == "bad"]:
            self.assertNotIn("201", m["reason"])
        time.sleep(0.3)
        self.assertEqual(len(self.log(tab)), before)
        # Past the socket's 64 KiB a message closes the socket (1009).
        window.conn.send(json.dumps({"t": "text", "text": "x" * 70000}))
        window.until(lambda w: w.closed is not None, what="the socket closed")
        # 1009 when the close frame is read before the unread rest of the
        # message resets the connection; either way the socket is gone.
        self.assertIn(window.closed, (1009, "dropped"))
        self.assertNotIn("x" * 100, self.js(tab, "t.value"))

    def test_05b_a_burst_is_never_dropped(self):
        """Unpaced input, as a window sends a burst: every key lands and no
        `input_dropped` is said while no dialog is open (PR #77 delta review:
        an arrival check on the in-flight CDP call dropped 190 of 200)."""
        tab = self.tab
        self.navigate(tab, "/input")
        window = self.attached(tab)
        click(window, 250, 320)
        text = "abcdefghij" * 10        # 200 events: under INPUT_QUEUE_MAX, whose refusal is its own rule
        type_keys(window, text)
        self.wait(lambda: self.js(tab, "t.value") == text, timeout=30, what="the whole burst")
        self.assertFalse([m for m in window.messages if m.get("t") == "input_dropped"])

    # -- 5: the hold --------------------------------------------------------------------------------

    def test_06_the_hold(self):
        tab = self.tab
        self.navigate(tab, "/input")
        window = self.attached(tab)
        click(window, 250, 320)
        self.wait(lambda: self.log(tab, "click"), what="focus")
        base = len(self.log(tab))
        with self.card():
            window.wait_for("held")             # told the moment the card was raised
            type_keys(window, "ab")
            window.send({"t": "text", "text": "under the card"})
            click(window, 200, 140)
            window.send(wheel(400, 400, 300))
            window.drain(1.0)
            self.assertEqual(len(self.log(tab)), base, "input reached the page under a card")
            self.assertEqual(self.js(tab, "t.value"), "")
            # The owner's routes that reach a page wait too.
            for path, body in ((f"/browser/tabs/{tab}/navigate", {"url": self.url("/anim")}),
                               (f"/browser/tabs/{tab}/history", {"action": "reload"}),
                               ("/browser/tabs", {"url": self.url("/anim")})):
                status, payload = self.ask("POST", path, body)
                self.assertEqual(status, 409, path)
                self.assertIn("approval card", payload["error"])
            # Resume Jarvis is refused under a card.
            held = len([m for m in window.messages if m.get("t") == "held"])
            window.send({"t": "resume"})
            window.until(lambda w: len([m for m in w.messages if m.get("t") == "held"]) > held
                         or w.last("agent", kind="paused") is not None, what="resume answered")
        # Nothing held is replayed after the card.
        window.drain(1.5)
        self.assertEqual(len(self.log(tab)), base, "held input was delivered after the card")
        self.assertEqual(self.js(tab, "t.value"), "")
        type_keys(window, "c")
        self.wait(lambda: self.js(tab, "t.value") == "c", what="input after the card")

    def test_07_input_queued_before_a_card_is_never_delivered_after_it(self):
        tab = self.tab
        self.navigate(tab, "/input")
        window = self.attached(tab)
        click(window, 250, 320)
        self.wait(lambda: self.log(tab, "click"), what="focus")
        gate = self.run_on(self._event())
        real = self.host._deliver
        calls = {"n": 0}

        async def slow(t, method, params):
            calls["n"] += 1
            await real(t, method, params)
            if calls["n"] == 1:
                await gate.wait()           # the pump is busy: what follows waits in the queue

        self.host._deliver = slow
        try:
            type_keys(window, "x", pace=False)
            self.wait(lambda: calls["n"] == 1, 5, "the first key delivered")
            type_keys(window, "yz", pace=False)
            window.send({"t": "text", "text": "queued"})
            time.sleep(0.3)
            with self.card():
                window.wait_for("held")
            # The card has come and gone; the pump moves on only now.
            self.host._loop.call_soon_threadsafe(gate.set)
            window.drain(1.5)
        finally:
            self.host._deliver = real
        self.assertEqual(self.js(tab, "t.value"), "x", "input queued before the card was replayed after it")
        type_keys(window, "w")
        self.wait(lambda: self.js(tab, "t.value") == "xw", what="input after the card")

    async def _event(self):
        return asyncio.Event()

    def test_08_a_press_a_card_cut_off_never_clicks(self):
        tab = self.tab
        self.navigate(tab, "/input")
        window = self.attached(tab)
        # Sanity: a press and release on the button is a click (the fixture sees clicks).
        click(window, 200, 140)
        self.wait(lambda: self.log(tab, "click"), what="a sanity click")
        base = len(self.log(tab, "click"))
        window.send(mouse("mousePressed", 200, 140))
        self.wait(lambda: len(self.log(tab, "mousedown")) == 2, what="the press")
        with self.card():
            window.wait_for("held")
            window.send(mouse("mouseReleased", 200, 140))     # dropped under the card
            window.drain(0.5)
        # BR-2's HeldButtons.releaseAll(false): off the page. Chrome would
        # fire `click` on <html> for that release (measured); the daemon lets
        # go instead, so no click fires anywhere, and no mouseup either.
        window.send(mouse("mouseMoved", -1, -1, button="left", buttons=1))
        window.send(mouse("mouseReleased", -1, -1, button="left", buttons=0, count=0))
        window.drain(0.8)
        self.assertEqual(len(self.log(tab, "click")), base,
                         f"a press a card cut off became a click: {self.log(tab)}")
        # The press is gone: the next click is an ordinary one.
        click(window, 200, 140)
        self.wait(lambda: len(self.log(tab, "click")) == base + 1, what="an ordinary click after")
        self.assertEqual(self.log(tab, "click")[-1]["target"], "b")
        # Its own release on the button, after a card came and went, is no click either.
        window.send(mouse("mousePressed", 200, 140))
        self.wait(lambda: len(self.log(tab, "mousedown")) == 4, what="a press")
        with self.card():
            window.wait_for("held")
        window.send(mouse("mouseReleased", 200, 140))
        window.drain(0.8)
        self.assertEqual(len(self.log(tab, "click")), base + 1, "a press from before a card clicked")
        # And a release off the page with no card at all (a window blurred mid-press).
        window.send(mouse("mousePressed", 200, 140))
        self.wait(lambda: len(self.log(tab, "mousedown")) == 5, what="a press")
        window.send(mouse("mouseReleased", -1, -1, button="left", buttons=0, count=0))
        window.drain(0.8)
        self.assertEqual(len(self.log(tab, "click")), base + 1, "a release off the page clicked")
        click(window, 200, 140)
        self.wait(lambda: len(self.log(tab, "click")) == base + 2, what="clicks work after")

    def test_08b_space_cut_off_by_card_never_activates(self):
        tab = self.tab
        window = self.attached(tab)
        for target in ("b", "box"):
            self.navigate(tab, "/input")
            self.js(tab, target + ".focus()")
            window.send(key(" ", "Space", 32, " "))
            self.wait(lambda: self.log(tab, "keydown"), what="Space down")
            with self.card():
                window.wait_for("held")
            window.send(key(" ", "Space", 32, up=True))
            window.send(key(" ", "Space", 32, up=True))  # duplicate release also stays cut off
            window.drain(0.4)
            self.assertEqual(self.log(tab, "click"), [], target + " activated after the card")
            self.assertFalse(self.js(tab, "box.checked"))
            window.send(key(" ", "Space", 32, " "))
            window.settled()
            window.send(key(" ", "Space", 32, up=True))
            self.wait(lambda: self.log(tab, "click"), what="ordinary Space activates " + target)
            self.assertEqual(self.log(tab, "click")[-1]["target"], target)
            if target == "box":
                self.assertTrue(self.js(tab, "box.checked"))

    def test_08c_hold_during_mousedown_dispatch(self):
        tab = self.tab
        self.navigate(tab, "/input")
        window = self.attached(tab)
        self.js(tab, "b.onmousedown = () => { const end = performance.now() + 1500; while(performance.now() < end) {} }")
        window.send(mouse("mousePressed", 200, 140))
        self.wait(lambda: self.host._tabs[tab].input_task is not None and not self.host._tabs[tab].input_task.done(), what="press in flight")
        time.sleep(0.4)
        with self.card():
            window.wait_for("held")
        self.wait(lambda: self.host._tabs[tab].input_task.done(), what="busy handler completed")
        window.send(mouse("mouseReleased", 200, 140))
        window.drain(0.5)
        self.assertEqual(self.log(tab, "click"), [], "a hold during dispatch was missed")

    def test_08d_dialog_drops_input_and_timeout_waits_for_card(self):
        tab = self.tab
        self.navigate(tab, "/input")
        window = self.attached(tab)
        self.js(tab, "t.value = 'old'; t.focus(); b.onmousedown = e => { e.preventDefault(); alert('blocked'); }")
        with patch.object(H, "DIALOG_TIMEOUT_S", 1.0), patch.object(H, "DELIVER_WAIT_S", 0.1):
            window.send(mouse("mousePressed", 200, 140))
            dialog = window.wait_for("dialog")
            window.send(mouse("mouseReleased", 200, 140))
            window.send({"t": "text", "text": "banked"})
            window.drain(0.15)  # the release/text are consumed before the card, never banked
            with self.card():
                window.drain(1.3)
                self.assertIsNone(window.last("dialog_closed", id=dialog["id"]), "timeout dismissed under a card")
                self.assertIn(dialog["id"], self.host._tabs[tab].dialogs)
            window.wait_for("dialog_closed", timeout=5, id=dialog["id"], by="timeout")
        self.wait(lambda: self.host._tabs[tab].input_task.done(), what="press answered")
        window.drain(0.3)
        self.assertEqual(self.js(tab, "t.value"), "old")
        self.assertEqual(self.log(tab, "mouseup"), [], "banked release landed")
        self.assertEqual(self.log(tab, "click"), [])
        self.assertEqual(len([m for m in window.messages if m.get("t") == "input_dropped"]), 1)

    def test_08e_hold_during_keydown_dispatch(self):
        tab = self.tab
        self.navigate(tab, "/input")
        window = self.attached(tab)
        self.js(tab, "b.focus(); b.onkeydown = () => { const end = performance.now() + 1500; while(performance.now() < end) {} }")
        window.send(key(" ", "Space", 32, " "))
        self.wait(lambda: self.host._tabs[tab].input_task is not None and not self.host._tabs[tab].input_task.done(), what="keydown in flight")
        time.sleep(0.4)
        with self.card():
            window.wait_for("held")
        self.wait(lambda: self.host._tabs[tab].input_task.done(), what="busy key handler completed")
        window.send(key(" ", "Space", 32, up=True))
        window.drain(0.4)
        self.assertEqual(self.log(tab, "click"), [], "a hold during keydown dispatch was missed")

    # -- 6: the agent channel (amendment §B.9) -------------------------------------------------------

    def test_09_owner_input_pauses_an_agent_before_it_is_delivered(self):
        tab = self.tab
        self.navigate(tab, "/input")
        window = self.attached(tab)
        stream = self.host._tabs[tab].stream
        # No agent: input is stamped, nothing is paused.
        click(window, 250, 320)
        self.wait(lambda: self.log(tab, "click"), what="a click")
        state = self.host.owner_state(tab)
        self.assertFalse(state["paused"])
        self.assertLess(state["owner_quiet_s"], 5)
        # A plain pointer move is not taking over.
        stream.owner_input_at = None
        window.send(mouse("mouseMoved", 5, 5, button="none"))
        time.sleep(0.3)
        self.assertIsNone(self.host.owner_state(tab)["owner_quiet_s"])
        # An agent at work (BR-9b's calls, made here by hand).
        stream.agent_driver("thread-1", "fast")
        stream.agent_action({"id": "a1", "at": 1, "action": "click", "target": "Sign in " + "x" * 200,
                             "text": "", "outcome": "running", "rect": {"x": 1, "y": 2, "width": 3, "height": 4}})
        window.wait_for("agent", kind="action")
        self.assertEqual(len(window.last("agent", kind="action")["target"]), 60)
        self.assertEqual(window.last("agent", kind="driver")["thread_id"], "thread-1")
        delivered_before = len(self.log(tab))
        type_keys(window, "k")
        window.wait_for("agent", kind="paused", paused=True)
        paused_at = [i for i, m in enumerate(window.messages) if m.get("t") == "agent"
                     and m.get("kind") == "paused" and m.get("paused")][-1]
        self.assertEqual(window.messages[paused_at]["by"], "owner")
        self.wait(lambda: len(self.log(tab)) > delivered_before, what="the key, after the pause")
        self.assertTrue(self.host.owner_state(tab)["paused"])
        # Resume under a card: refused; after it: resumed.
        with self.card():
            window.wait_for("held")
            window.send({"t": "resume"})
            window.drain(0.5)
            self.assertTrue(stream.paused)
        window.send({"t": "resume"})
        window.wait_for("agent", kind="paused", paused=False)
        window.send({"t": "stop"})
        window.wait_for("agent", kind="paused", by="stop")
        # A reattach is told the driver, the pause and the list again.
        window.close()
        self.wait(lambda: not stream.attached(), 5, "the window let go")
        again = self.attached(tab)
        self.assertEqual(again.last("agent", kind="driver")["provider"], "fast")
        self.assertEqual(again.last("agent", kind="paused")["by"], "stop")
        self.assertEqual([a["id"] for a in again.last("agent", kind="list")["actions"]], ["a1"])
        stream.agent_driver(None)
        stream.resume()
        again.close()

    def test_10_selection(self):
        tab = self.tab
        self.navigate(tab, "/input")
        window = self.attached(tab)
        self.js(tab, "t.value = 'hello world'; t.focus(); t.setSelectionRange(6, 11)")
        window.send({"t": "selection"})
        self.assertEqual(window.wait_for("selection")["text"], "world")
        self.js(tab, "t.blur(); getSelection().selectAllChildren(s)")
        window.messages.clear()
        window.send({"t": "selection"})
        self.assertEqual(window.wait_for("selection")["text"], f"select me {MARK}")
        window.close()

    # -- 7: dialogs ---------------------------------------------------------------------------------

    def test_11_dialogs(self):
        tab = self.tab
        self.navigate(tab, "/dialogs")
        window = self.attached(tab)
        click(window, 60, 30)
        dialog = window.wait_for("dialog")
        self.assertEqual(dialog["kind"], "alert")
        self.assertEqual(dialog["message"], f"hello {MARK}")
        window.send({"t": "dialog", "id": dialog["id"], "accept": True})
        window.wait_for("dialog_closed", id=dialog["id"])
        self.wait(lambda: self.js(tab, "__r") == ["alert-done"], what="the alert answered")
        click(window, 60, 80)
        dialog = window.until(lambda w: [m for m in w.messages if m.get("t") == "dialog"
                                         and m["kind"] == "confirm"], what="confirm")[0]
        window.send({"t": "dialog", "id": dialog["id"], "accept": False})
        self.wait(lambda: (self.js(tab, "__r") or [])[-1:] == ["confirm:false"], what="confirm dismissed")
        click(window, 60, 130)
        dialog = window.until(lambda w: [m for m in w.messages if m.get("t") == "dialog"
                                         and m["kind"] == "prompt"], what="prompt")[0]
        self.assertEqual(dialog["default"], "def")
        # Under a card the answer is held and the dialog stays open.
        with self.card():
            window.wait_for("held")
            window.send({"t": "dialog", "id": dialog["id"], "accept": True, "text": "nope"})
            window.drain(0.5)
            self.assertIsNone(window.last("dialog_closed", id=dialog["id"]))
        window.send({"t": "dialog", "id": dialog["id"], "accept": True, "text": "Ada"})
        self.wait(lambda: (self.js(tab, "__r") or [])[-1:] == ["prompt:Ada"], what="prompt answered")
        # Unanswered: dismissed when the wait runs out.
        with patch.object(H, "DIALOG_TIMEOUT_S", 1.0):
            click(window, 60, 80)
            dialog = window.until(lambda w: [m for m in w.messages if m.get("t") == "dialog"
                                             and m["kind"] == "confirm"][1:], what="a second confirm")[-1]
            window.wait_for("dialog_closed", timeout=6, id=dialog["id"], by="timeout")
        self.wait(lambda: (self.js(tab, "__r") or [])[-1:] == ["confirm:false"], what="timed out")
        # beforeunload: stay (dismiss), then leave (accept).
        self.js(tab, "onbeforeunload = e => { e.preventDefault(); e.returnValue = 'x'; }")
        click(window, 60, 180)
        dialog = window.until(lambda w: [m for m in w.messages if m.get("t") == "dialog"
                                         and m["kind"] == "beforeunload"], what="beforeunload")[-1]
        window.send({"t": "dialog", "id": dialog["id"], "accept": False})
        window.wait_for("dialog_closed", id=dialog["id"])
        time.sleep(0.5)
        self.assertTrue(self.page(tab).url.endswith("/dialogs"), "a dismissed beforeunload stays")
        click(window, 60, 180)
        dialog = window.until(lambda w: [m for m in w.messages if m.get("t") == "dialog"
                                         and m["kind"] == "beforeunload"][1:], what="beforeunload again")[-1]
        window.send({"t": "dialog", "id": dialog["id"], "accept": True})
        self.wait(lambda: self.page(tab).url.endswith("/input"), what="an accepted beforeunload leaves")
        self.navigate(tab, "/dialogs")
        window.close()
        self.wait(lambda: not self.host._tabs[tab].stream.attached(), 5, "the window let go")
        # With no window showing the tab, nobody could answer: dismissed at once.
        self.js(tab, "setTimeout(() => { __r.push('c2:' + confirm('?')); }, 0)")
        self.wait(lambda: (self.js(tab, "__r") or [])[-1:] == ["c2:false"], 5, "dismissed unseen")

    # -- 8: pop-ups and the cap ----------------------------------------------------------------------

    def test_12_popups_open_in_the_openers_pane_and_the_cap_refuses(self):
        tab = self.tab
        self.close_others(tab)
        self.navigate(tab, f"/popup?u={quote(self.url('/input'), safe='')}")
        window = self.attached(tab)
        click(window, 70, 30)
        popup = window.wait_for("popup")
        listed = {r["tab_id"]: r for r in self.owner("GET", "/browser/tabs")}
        self.assertEqual(listed[popup["tab_id"]]["opener"], tab)
        self.assertFalse(listed[popup["tab_id"]]["shown"])
        # The landing guard applies to a pop-up as to any page (BR-1a).
        self.wait(lambda: self.page(popup["tab_id"]).url.endswith("/input"), what="the pop-up landed")
        # Three tabs is the cap here: the next pop-up is refused, no tab closed.
        self.owner("POST", "/browser/tabs", {})
        self.assertEqual(len(self.owner("GET", "/browser/tabs")), 3)
        click(window, 70, 30)
        refused = window.wait_for("popup_refused")
        self.assertIn("3 tabs", refused["reason"])
        time.sleep(0.5)
        self.assertEqual(len(self.owner("GET", "/browser/tabs")), 3)
        self.assertGreaterEqual(self.ask("GET", "/status", listener="api")[1]["browser"]["popups_refused"], 1)
        # Activate (switch) and close: the closed tab's window is told.
        self.owner("POST", f"/browser/tabs/{popup['tab_id']}/activate", {})
        popup_window = self.attached(popup["tab_id"])
        self.owner("DELETE", f"/browser/tabs/{popup['tab_id']}")
        popup_window.wait_for("closed")
        popup_window.until(lambda w: w.closed is not None, what="its socket closed")
        self.close_others(tab)
        window.close()

    # -- 9: downloads --------------------------------------------------------------------------------

    def test_13_downloads(self):
        tab = self.tab
        window = self.attached(tab)
        self.navigate(tab, "/input")
        name = f"{MARK} report.txt"
        self.owner("POST", f"/browser/tabs/{tab}/navigate", {"url": self.url(f"/dl?n={quote(name)}")})
        done = window.wait_for("download", timeout=15, state="done")
        self.assertTrue(window.last("download", state="started"))
        self.assertEqual(done["name"], name)
        listing = self.owner("GET", "/browser/downloads")["downloads"]
        row = next(r for r in listing if r["id"] == done["id"])
        self.assertFalse(row["held"])
        folder = config.BROWSER_DIR / "downloads" / row["profile"] / row["id"]
        self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(folder / name).st_mode), 0o600)
        incoming = config.BROWSER_DIR / "downloads" / row["profile"] / F.INCOMING
        self.wait(lambda: not os.listdir(incoming), 5, "Playwright's copy deleted")
        # Served to the owner only, as an attachment that runs nothing.
        status, got, body = self.ask("GET", f"/browser/downloads/{row['id']}", raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(body, f"{MARK} download body\n".encode())
        self.assertEqual(got["Content-Type"], "application/octet-stream")
        self.assertEqual(got["X-Content-Type-Options"], "nosniff")
        self.assertTrue(got["Content-Disposition"].startswith("attachment; "))
        self.assertIn("default-src 'none'", got["Content-Security-Policy"])
        self.assertNotIn("script-src 'self'", got["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", got["Content-Security-Policy"])
        # The owner's same-origin read (no Origin) is the owner's; anything else is not.
        status = self.ask("GET", f"/browser/downloads/{row['id']}", origin=False, raw=True,
                          headers={"Sec-Fetch-Site": "same-origin"})[0]
        self.assertEqual(status, 200)
        self.assertEqual(self.ask("GET", f"/browser/downloads/{row['id']}", origin=False, raw=True)[0], 403)
        self.assertEqual(self.ask("GET", f"/browser/downloads/{row['id']}", listener="api", raw=True)[0], 403)
        self.assertEqual(self.ask("GET", f"/browser/downloads/{row['id']}", raw=True,
                                  headers={"Sec-Fetch-Dest": "script"})[0], 403)
        # An executable is held: by its name and by its first bytes.
        for query, label in (("n=setup.exe&k=mz", "setup.exe"), ("n=tool&k=elf", "tool")):
            self.owner("POST", f"/browser/tabs/{tab}/navigate", {"url": self.url(f"/dl?{query}")})
            held = window.wait_for("download", timeout=15, state="held", name=label)
            self.assertTrue(held["held"])
            status, payload = self.ask("GET", f"/browser/downloads/{held['id']}")
            self.assertEqual(status, 409)
            self.assertIn("held", payload["error"])
            self.assertEqual(self.owner("DELETE", f"/browser/downloads/{held['id']}")["deleted"], True)
            self.assertEqual(self.ask("GET", f"/browser/downloads/{held['id']}")[0], 404)
        self.assertEqual(self.ask("DELETE", f"/browser/downloads/{row['id']}", listener="api")[0], 403)
        self.owner("DELETE", f"/browser/downloads/{row['id']}")
        window.close()

    # -- 10: set_files -------------------------------------------------------------------------------

    def test_14_set_files(self):
        tab = self.tab
        (self.work / "notes.txt").write_text(f"UPLOADz {MARK}\n")
        (self.work / ".env").write_text("SECRET=not-a-real-value\n")   # a fake, in the test's temp folder
        (self.work / ".hidden").mkdir()
        (self.work / ".hidden" / "x.txt").write_text("hidden\n")
        (self.work / "twin.txt").write_text("twin\n")
        os.link(self.work / "twin.txt", self.work / "twin-link.txt")
        os.symlink("/etc/hostname", self.work / "out.txt")
        with open(self.work / "big.bin", "wb") as handle:
            handle.truncate(51 * 1024 * 1024)
        self.navigate(tab, "/upload")
        window = self.attached(tab)
        click(window, 150, 40)
        chooser = window.wait_for("filechooser")
        self.assertTrue(chooser["multiple"])
        path = f"/browser/tabs/{tab}/files"

        def files(*names, project=None):
            return [{"project": project or self.project.id, "path": n} for n in names]
        for name, status in ((".env", 403), (".hidden/x.txt", 403), ("twin.txt", 403),
                             ("out.txt", 403), ("big.bin", 403), ("missing.txt", 403),
                             ("../home/x", 403)):
            with self.subTest(name=name):
                self.assertEqual(self.ask("POST", path, {"chooser": chooser["id"], "files": files(name)})[0],
                                 status)
        self.assertEqual(self.ask("POST", path, {"chooser": chooser["id"],
                                                 "files": files("notes.txt", project="nope")})[0], 404)
        self.assertEqual(self.ask("POST", path, {"chooser": chooser["id"], "files": files("/etc/hostname")})[0], 400)
        self.assertEqual(self.ask("POST", path, {"chooser": chooser["id"],
                                                 "files": [{"project": self.project.id}]})[0], 400)
        self.assertEqual(self.ask("POST", path, {"chooser": "deadbeef", "files": files("notes.txt")})[0], 409)
        self.assertEqual(self.ask("POST", path, {"chooser": chooser["id"], "files": files("notes.txt")},
                                  listener="api")[0], 403)
        with self.card():
            self.assertEqual(self.ask("POST", path, {"chooser": chooser["id"], "files": files("notes.txt")})[0], 409)
        self.assertIsNone(self.js(tab, "__files"), "nothing was uploaded by a refusal")
        result = self.owner("POST", path, {"chooser": chooser["id"], "files": files("notes.txt")})
        self.assertEqual(result["files"], 1)
        self.wait(lambda: self.js(tab, "__files"), what="the upload")
        self.assertEqual(self.js(tab, "__files"), [{"name": "notes.txt", "size": len(f"UPLOADz {MARK}\n"),
                                                    "text": f"UPLOADz {MARK}\n"}])
        window.wait_for("filechooser_closed", id=chooser["id"])
        self.assertEqual(self.ask("POST", path, {"chooser": chooser["id"], "files": files("notes.txt")})[0], 409,
                         "a chooser is answered once")
        window.close()

    def test_14a_upload_folder_swap_and_same_path_replacement(self):
        from jarvis.v2.browser import act_rules as rules
        tab = self.tab
        self.navigate(tab, "/upload")
        window = self.attached(tab)
        click(window, 150, 40)
        chooser = window.wait_for("filechooser")
        real = rules.upload_refusal
        for kind in ("folder", "inode"):
            folder = self.work / ("swap-" + kind)
            folder.mkdir()
            source = folder / "notes.txt"
            source.write_text("judged")
            def swap(path, project, **kw):
                result = real(path, project, **kw)
                if kind == "folder":
                    # The fd will still have the judged inode: only realpath catches this.
                    moved = self.work / "moved"
                    folder.rename(moved)
                    folder.symlink_to(moved, target_is_directory=True)
                else:
                    replacement = self.work / "replacement"
                    replacement.write_text("other bytes")
                    replacement.replace(source)
                return result
            with patch.object(rules, "upload_refusal", swap):
                status, _ = self.ask("POST", f"/browser/tabs/{tab}/files", {
                    "chooser": chooser["id"], "files": [{"project": self.project.id, "path": str(source.relative_to(self.work))}]})
            self.assertEqual(status, 409, kind + " swap uploaded different file")
            self.assertIsNone(self.js(tab, "__files"))

    def test_14b_a_private_tab_streams_too(self):
        # One Chrome at a time: Main stops first, and Private is its own Chrome.
        main = self.owner("GET", "/browser")["profiles"][0]["id"]
        self.host.close_profile(main)
        self.wait(lambda: not chrome_tree(config.BROWSER_DIR / "profiles"), 10, "Main stopped")
        row = self.owner("POST", "/browser/tabs", {"profile": "private", "url": self.url("/input")})
        window = self.attached(row["tab_id"])
        window.until(lambda w: w.frames, what="a Private frame")
        click(window, 250, 320)
        type_keys(window, "pv")
        self.wait(lambda: self.js(row["tab_id"], "t.value") == "pv", what="Private input")
        # Each Private context owns its downloads, including listing/serving.
        ctx_folder = config.BROWSER_DIR / "downloads" / "private" / row["ctx"]
        for state in ("context", "chrome"):
            self.owner("POST", f"/browser/tabs/{row['tab_id']}/navigate", {"url": self.url("/dl?n=private.txt")})
            done = window.wait_for("download", timeout=15, state="done")
            self.assertTrue((ctx_folder / done["id"] / done["name"]).is_file())
            listed = {d["id"]: d for d in self.owner("GET", "/browser/downloads")["downloads"]}
            self.assertEqual(listed[done["id"]]["profile"], "private")
            self.assertEqual(self.ask("GET", f"/browser/downloads/{done['id']}", raw=True)[0], 200)
            if state == "context":
                self.owner("DELETE", f"/browser/tabs/{row['tab_id']}")
                window.wait_for("closed")
                self.assertFalse(ctx_folder.exists())
                row = self.owner("POST", "/browser/tabs", {"profile": "private", "url": self.url("/input")})
                window = self.attached(row["tab_id"])
                ctx_folder = config.BROWSER_DIR / "downloads" / "private" / row["ctx"]
            else:
                self.host.close_profile("private")
                self.assertFalse(ctx_folder.exists())
        type(self).tab = self.owner("POST", "/browser/tabs", {"url": self.url("/input")})["tab_id"]

    # -- 11: nothing leaves memory, and the end ------------------------------------------------------------

    def test_15_nothing_on_the_bus_or_in_the_log(self):
        text = json.dumps([r for r in self.records if not str(r.get("kind", "")).startswith("approval")])
        for needle in (MARK, TYPED, "UPLOADz", f"127.0.0.1:{self.fx}", "/input", "report.txt"):
            self.assertNotIn(needle, text, f"{needle} reached the bus")
            self.assertFalse([line for line in self.logs if needle in line], f"{needle} reached the log")
        kinds = {r.get("kind") for r in self.records if "browser" in str(r.get("kind"))}
        self.assertEqual(kinds, {"browser_attached", "browser_sound_changed"})
        for record in self.records:
            if record.get("kind") == "browser_attached":
                self.assertEqual(set(record["data"]), {"tab_id", "at"})
            elif record.get("kind") == "browser_sound_changed":
                self.assertEqual(set(record["data"]), {"generation"})
                self.assertIs(type(record["data"]["generation"]), int)
                self.assertGreaterEqual(record["data"]["generation"], 0)

    def test_16_stop_tells_every_window_and_leaves_nothing(self):
        window = self.attached(self.tab)
        self.measure("end")
        self.host.stop()
        window.wait_for("closed")
        self.assertEqual(window.last("closed")["reason"], "ended")
        window.until(lambda w: w.closed is not None, what="the socket closed")
        self.assertEqual(chrome_tree(config.BROWSER_DIR), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
