#!/usr/bin/env python3
"""The Remote Browser's input-to-picture pipeline, stage by stage (B-PERF;
plan 2026-10-10 §10 M1/M2). Manual, never in a suite.

A port-0 daemon with its real Browser (headless Google Chrome, temp HOME,
fixture pages on an ephemeral loopback port, the gate resolving nothing),
exactly as `tests/v2/browser_routes_check.BrowserStream` builds it, and a
headless Playwright Chromium playing the HUD: a page the daemon's HUD
listener serves from a temporary HUD folder, running the pane's real frame
pipeline (`scripts/browser_measure/page.ts`, bundled here with the HUD's
esbuild). The HUD page sends the owner's input over the tab's ticketed
socket; every stage is timed:

    HUD send → daemon reader (enqueue) → pump → CDP answered → page handler
    → screencast capture (Chrome's frame timestamp) → daemon `_frame`
    (JPEG encoded, base64 through Playwright's pipe) → decoded and offered
    → sent (after the window's ack) → written → HUD receive → decode
    (createImageBitmap) → draw → ack.

Which frame first shows an input is read from the picture itself: the
fixture pages draw a 16-bit counter of handled events as black and white
blocks (a row number on the scroll page), and the HUD reads it from each
frame after acking it. End-to-end latency is HUD clock only (send to the
first frame drawn that shows the input); the stage split lines up the
daemon's `time.time()`, the HUD's and the remote page's
`performance.timeOrigin + now()` (one machine's real-time clock; the offsets
measured by round trip are printed).

    systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 \\
        env PYTHONPATH=. .venv/bin/python scripts/browser_measure.py \\
        [--size pane|4k|both] [--scenario typing,hover,wheel,scroll] [--json out.json]

Knobs for trying fixes without editing the product: `--quality N`,
`--ack draw|recv` (the HUD's ack after the draw, as before B-PERF, or on
receipt; the default is the pane's own, at decode start), `--chrome-flag=--x`
(added to `launch_args`, whose flags are all kept). `BPERF_LOG=1` prints the
Browser's log. Nothing here touches 8402/8403/8405, the owner's state or any
profile but the temporary one. Results: docs/notes/browser.md, "Remote input
to picture, stage by stage".
"""
from __future__ import annotations

import argparse
import collections
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from jarvis import config  # noqa: E402
from jarvis.v2 import ws as W  # noqa: E402
from jarvis.v2.browser import host as H, stream as S  # noqa: E402
from jarvis.v2.daemon import Daemon  # noqa: E402
from jarvis.v2.model import ProviderName as PN  # noqa: E402
from jarvis.v2.stores import Stores  # noqa: E402
from tests.v2 import browser_routes_check as R  # noqa: E402

LIVE_PORTS = {8402, 8403, 8405}
SIZES = {"pane": (1200, 800), "4k": (2540, 1340)}   # CSS px at DPR 1.5: a typical pane; 4K at 150% full screen
DPR = 1.5

# -- the fixture pages -------------------------------------------------------------------------

MARK_JS = """<script>
const now = () => performance.timeOrigin + performance.now();
window.__ev = []; let seq = 0;
const mk = document.getElementById('mk');
for (let b = 0; b < 16; b++) mk.appendChild(document.createElement('i'));
function mark(v) { for (let b = 0; b < 16; b++) mk.children[b].style.background = (v >> b) & 1 ? '#000' : '#fff'; }
mark(0);
function rec(e, extra) { seq++; mark(seq); __ev.push(Object.assign({seq, type: e.type, t: now()}, extra)); }
</script>"""
MARK_CSS = "#mk{position:fixed;left:0;top:0;height:6px;z-index:9;display:flex}#mk i{width:6px;height:6px}"
WORDS = ("the quick brown fox jumps over a lazy dog while pack my box with five dozen liquor jugs "
         "sphinx of black quartz judge my vow how vexingly quick daft zebras jump").split()


def prose(n: int, seed: int = 1) -> str:
    out, k = [], seed
    for p in range(n):
        words = []
        for _ in range(90):
            k = (k * 1103515245 + 12345) & 0x7FFFFFFF
            words.append(WORDS[k % len(WORDS)])
        out.append(f"<p>{' '.join(words).capitalize()}.</p>")
    return "".join(out)


PAGES = {
    "/typing": f"""<!doctype html><title>typing</title><style>{MARK_CSS}
body{{margin:0;font:15px/1.45 sans-serif;color:#222;background:#fafafa}} main{{padding:24px 16px;columns:3 360px}}
textarea{{position:fixed;left:16px;top:16px;width:520px;height:90px;font:16px monospace;z-index:5}}</style>
<div id=mk></div><textarea id=t></textarea><main>{prose(60)}</main>{MARK_JS}<script>
t.focus(); addEventListener('keydown', e => rec(e, {{key: e.key}}), true);</script>""",
    "/hover": f"""<!doctype html><title>hover</title><style>{MARK_CSS}
body{{margin:0;font:11px sans-serif}} #g{{display:grid;grid-template-columns:repeat(auto-fill,32px)}}
#g b{{height:32px;border:1px solid #ccd;color:#445;font-weight:400;display:flex;align-items:center;justify-content:center}}
#g b:hover{{background:#2a7;color:#fff;transform:scale(1.15)}}</style>
<div id=mk></div><div id=g></div>{MARK_JS}<script>
const g = document.getElementById('g'); const f = document.createDocumentFragment();
for (let i = 0; i < 9000; i++) {{ const b = document.createElement('b'); b.textContent = i % 1000; f.appendChild(b); }}
g.appendChild(f);
addEventListener('mousemove', e => rec(e, {{x: e.clientX, y: e.clientY}}), true);</script>""",
    # The scroll page needs no counter: each 24 px row carries its own number
    # as blocks at its left, so the frame says how far it scrolled.
    "/scroll": f"""<!doctype html><title>scroll</title><style>
body{{margin:0;font:13px/24px sans-serif;color:#222}} .r{{display:flex;height:24px;overflow:hidden;white-space:nowrap}}
.r i{{flex:none;width:6px;height:24px}} .r span{{padding-left:8px}}</style><div id=rows></div><script>
const now = () => performance.timeOrigin + performance.now(); window.__ev = [];
const W = {json.dumps(WORDS)}; const rows = document.getElementById('rows'); const f = document.createDocumentFragment();
for (let r = 0; r < 4000; r++) {{
  const d = document.createElement('div'); d.className = 'r';
  for (let b = 0; b < 16; b++) {{ const i = document.createElement('i'); i.style.background = (r >> b) & 1 ? '#000' : '#fff'; d.appendChild(i); }}
  const s = document.createElement('span'); let t = ''; for (let k = 0; k < 40; k++) t += W[(r * 7 + k * 13) % W.length] + ' ';
  s.textContent = t; d.appendChild(s); f.appendChild(d);
}}
rows.appendChild(f);
addEventListener('wheel', e => __ev.push({{type: 'wheel', t: now(), dy: e.deltaY}}), {{passive: true, capture: true}});
</script>""",
}


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = PAGES.get(self.path.split("?")[0])
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


# -- the daemon's clocks (patched in, never in the product) ------------------------------------

class Trace:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with getattr(self, "lock", threading.Lock()):
            self.enq: list = []          # (t, t-kind, params)
            self.deliver: list = []      # (t0, t1, method, params)
            self.cdp_frames: list = []   # (t_event, t_offered, capture_ts, bytes)
            self.sent: dict = {}         # fid -> {capture, event, offered, send, written...}
            self.acks: list = []         # (t, id)
            self.pictures: dict = {}     # id(jpeg) -> (jpeg, record)
            self.writes: list = []       # (t0, t1, bytes) of large writes, in order


TRACE = Trace()
_frame_ctx = threading.local()


def instrument():
    enqueue, deliver, frame, offer, send_frame, ack, send_all = (
        S.TabStream.enqueue, H.BrowserHost._deliver, H.BrowserHost._frame, S.TabStream.offer_frame,
        S.TabStream._send_frame, S.TabStream.ack, W.WebSocket._send_all)

    def t_enqueue(self, sock, msg):
        TRACE.enq.append((time.time(), msg.t, dict(msg.params or {})))
        return enqueue(self, sock, msg)

    async def t_deliver(self, tab, method, params):
        t0 = time.time()
        try:
            return await deliver(self, tab, method, params)
        except Exception as exc:
            if os.environ.get("BPERF_LOG"):
                print("deliver failed:", type(exc).__name__, getattr(exc, "args", None), file=sys.stderr)
            raise
        finally:
            TRACE.deliver.append((t0, time.time(), method, dict(params)))

    def t_frame(self, tab, cdp, params):
        t = time.time()
        meta = (params or {}).get("metadata") or {}
        rec = {"event": t, "capture": meta.get("timestamp"), "b64": len((params or {}).get("data") or "")}
        _frame_ctx.rec = rec
        try:
            return frame(self, tab, cdp, params)
        finally:
            _frame_ctx.rec = None
            rec["done"] = time.time()
            TRACE.cdp_frames.append(rec)

    def t_offer(self, jpeg, width, height):
        rec = getattr(_frame_ctx, "rec", None)
        if rec is not None:
            rec["offered"] = time.time()
            rec["bytes"] = len(jpeg)
            rec["q"] = jpeg_quality(jpeg)
            with TRACE.lock:
                TRACE.pictures[id(jpeg)] = (jpeg, rec)
                if len(TRACE.pictures) > 400:
                    for key in list(TRACE.pictures)[:200]:
                        del TRACE.pictures[key]
        return offer(self, jpeg, width, height)

    def t_send_frame(self, sock, frame_tuple):
        fid, jpeg = frame_tuple[0], frame_tuple[1]
        with TRACE.lock:
            rec = (TRACE.pictures.get(id(jpeg)) or (None, None))[1]
        TRACE.sent[fid] = {"send": time.time(), **(rec or {})}
        return send_frame(self, sock, frame_tuple)

    def t_ack(self, sock, frame_id):
        TRACE.acks.append((time.time(), frame_id))
        return ack(self, sock, frame_id)

    def t_send_all(self, data):
        t0 = time.time()
        try:
            return send_all(self, data)
        finally:
            if len(data) > 4096:
                TRACE.writes.append((t0, time.time(), len(data)))

    S.TabStream.enqueue = t_enqueue
    H.BrowserHost._deliver = t_deliver
    H.BrowserHost._frame = t_frame
    S.TabStream.offer_frame = t_offer
    S.TabStream._send_frame = t_send_frame
    S.TabStream.ack = t_ack
    W.WebSocket._send_all = t_send_all


def jpeg_quality(data: bytes) -> int | None:
    """A JPEG's luminance quantizer's first entry: libjpeg writes 16 scaled
    by its quality, so 10 is quality 70 and 16 is quality 50."""
    i = 2
    while i + 5 < len(data) and data[i] == 0xFF:
        marker, length = data[i + 1], int.from_bytes(data[i + 2:i + 4], "big")
        if marker == 0xDB:
            return data[i + 5]
        if marker == 0xDA:
            break
        i += 2 + length
    return None


# -- the HUD page -------------------------------------------------------------------------------

INDEX = """<!doctype html><meta charset=utf-8><title>measure</title>
<style>body{margin:0;background:#111}#s{position:relative;overflow:hidden}
#s canvas{position:absolute;inset:0;width:100%;height:100%}</style>
<div id=s><canvas id=c></canvas></div><script type=module src="/assets/m.js"></script>"""


def bundle(out: Path) -> None:
    esbuild = REPO / "hud" / "node_modules" / ".bin" / "esbuild"
    subprocess.run([str(esbuild), str(REPO / "scripts" / "browser_measure" / "page.ts"), "--bundle",
                    "--format=esm", "--target=es2022", f"--outfile={out / 'assets' / 'm.js'}", "--log-level=warning"],
                   check=True)
    (out / "index.html").write_text(INDEX)


# -- statistics ---------------------------------------------------------------------------------

def pct(values, p):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    k = (len(values) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def summary(values):
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0}
    return {"n": len(values), "p50": round(pct(values, 50), 1), "p95": round(pct(values, 95), 1)}


# -- the run -------------------------------------------------------------------------------------

class Bench:
    def __init__(self, args):
        self.args = args
        self.tmp = tempfile.TemporaryDirectory(prefix="bperf-")
        root = Path(self.tmp.name)
        self.root = root
        (root / "home").mkdir()
        (root / "work").mkdir()
        hud = root / "hud"
        hud.mkdir()
        bundle(hud)
        # Playwright's own Chromium (the HUD's stand-in) is found under the
        # real HOME's cache; only its binaries are read.
        browsers = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(Path.home() / ".cache" / "ms-playwright")
        self.patches = [patch.dict(os.environ, {
            "PLAYWRIGHT_BROWSERS_PATH": browsers, "HOME": str(root / "home"), "XDG_DATA_HOME": str(root / "xdg"),
            "JARVIS_CLAUDE_CREDENTIALS": str(root / "none.json"),
            "OPENROUTER_API_KEY": "sk-dummy-not-a-key", "LC_ALL": "C.UTF-8"})]
        values = dict(V2_DATA_DIR=root / "data", ALLOWLIST_PATH=root / "cfg" / "allow.json",
                      MODELS_PATH=root / "cfg" / "models.json",
                      PROVIDER_DEFAULTS_PATH=root / "cfg" / "provider_defaults.json",
                      ROUTING_PATH=root / "cfg" / "routing.json",
                      CODEX_CATALOG_PATH=root / "cfg" / "codex-models.json",
                      V2_ALWAYS_ASK=root / "cfg" / "always-ask.json",
                      DISCORD_GUILD_PATH=root / "cfg" / "guild.json",
                      DISCORD_TOKEN_PATH=root / "cfg" / "discord_token.json",
                      HUD_DIR=hud, HUD_DIST=hud, HUD_DEV=True,
                      BROWSER_DIR=root / "browser",
                      BROWSER_SETTINGS_PATH=root / "cfg" / "browser.json",
                      BROWSER_TAB_CAP=3)
        for name, value in values.items():
            self.patches.append(patch.object(config, name, value))
        if args.quality is not None:
            self.patches.append(patch.object(H, "JPEG_QUALITY", args.quality))
        if args.chrome_flag:
            launch = H.launch_args

            def more(*a, **kw):
                out = launch(*a, **kw)
                return out[:-1] + list(args.chrome_flag) + out[-1:]
            self.patches.append(patch.object(H, "launch_args", more))
        for p in self.patches:
            p.start()
        instrument()
        self.fixture = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        threading.Thread(target=self.fixture.serve_forever, daemon=True).start()
        self.fx = self.fixture.server_address[1]
        self.daemon = Daemon(Stores(root / "stores"), {n: R.Fake(n) for n in PN}, lambda *_: (lambda *_: None), 0)
        self.daemon.browser = H.BrowserHost(self.daemon, resolver=R.deny_every_name, connector=R.loopback_only)
        self.daemon.start()
        self.hud_port = self.daemon.face_port
        assert self.hud_port not in LIVE_PORTS and self.daemon.port not in LIVE_PORTS
        self.host = self.daemon.browser
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        self.hud = self.pw.chromium.launch(channel="chromium", headless=True)

    def close(self):
        try:
            self.hud.close()
            self.pw.stop()
            self.daemon.stop()
        finally:
            for pid in H.chrome_pids(self.root / "browser"):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            self.fixture.shutdown()
            for p in reversed(self.patches):
                p.stop()
            self.tmp.cleanup()

    def owner(self, method, path, body=None):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.hud_port, timeout=60)
        h = {"Origin": f"http://127.0.0.1:{self.hud_port}"}
        if body is not None:
            h["Content-Type"] = "application/json"
        conn.request(method, path, json.dumps(body).encode() if body is not None else None, h)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        if r.status >= 300:
            raise RuntimeError(f"{method} {path}: {r.status} {data[:200]!r}")
        return json.loads(data) if data else None

    def remote_eval(self, tab_id, expression):
        return self.host._call(self.host._tabs[tab_id].page.evaluate(expression), 30)

    def offset(self, evaluate) -> tuple[float, float]:
        """(page clock − time.time() in ms, half the best round trip)."""
        best = None
        for _ in range(15):
            a = time.time()
            js = evaluate("performance.timeOrigin + performance.now()")
            b = time.time()
            rtt = (b - a) * 1000
            if best is None or rtt < best[1] * 2:
                best = (js - (a + b) / 2 * 1000, rtt / 2)
        return best

    # -- scenarios --------------------------------------------------------------------------------

    def plan(self, scenario, w, h):
        """(messages, send times in ms, how the page reports it)."""
        msgs, times = [], []
        if scenario in ("typing", "slowtype"):
            gap = 125.0 if scenario == "typing" else 400.0
            letters = "abcdefghijklmnopqrstuvwxyz"
            for i in range(self.args.keys):
                ch = letters[i % 26]
                down = {"t": "key", "e": {"type": "keyDown", "modifiers": 0, "key": ch, "code": f"Key{ch.upper()}",
                                          "windowsVirtualKeyCode": ord(ch.upper()),
                                          "nativeVirtualKeyCode": ord(ch.upper()), "location": 0,
                                          "text": ch, "unmodifiedText": ch}}
                up = {"t": "key", "e": {**down["e"], "type": "keyUp"}}
                up["e"].pop("text")
                up["e"].pop("unmodifiedText")
                msgs += [down, up]
                times += [i * gap, i * gap + 45.0]
        elif scenario == "hover":
            span = w - 120
            n = int(self.args.seconds * 160)
            for i in range(n):
                x = 60 + (i * 5) % span
                y = 60 + ((i * 5) // span) * 9
                msgs.append({"t": "mouse", "e": {"type": "mouseMoved", "x": x, "y": min(y, h - 20), "modifiers": 0,
                                                 "button": "none", "buttons": 0, "clickCount": 0,
                                                 "pointerType": "mouse"}})
                times.append(i * 6.25)
        elif scenario in ("wheel", "scroll"):
            # wheel: notches 250 ms apart (a mouse wheel); scroll: a 120 Hz
            # trackpad-like stream of small deltas.
            step, every, n = (96, 250.0, self.args.keys) if scenario == "wheel" else (
                24, 1000 / 120, int(self.args.seconds * 120))
            for i in range(n):
                msgs.append({"t": "mouse", "e": {"type": "mouseWheel", "x": w // 2, "y": h // 2, "modifiers": 0,
                                                 "button": "none", "buttons": 0, "clickCount": 0, "deltaX": 0,
                                                 "deltaY": step, "pointerType": "mouse"}})
                times.append(i * every)
        return msgs, times

    def run(self, scenario, size_name):
        w, h = SIZES[size_name]
        path = {"typing": "/typing", "slowtype": "/typing", "hover": "/hover", "wheel": "/scroll",
                "scroll": "/scroll"}[scenario]
        row = self.owner("POST", "/browser/tabs", {"url": f"http://127.0.0.1:{self.fx}{path}"})
        tab = row["tab_id"]
        R.eventually(lambda: self.remote_eval(tab, "document.readyState") == "complete", 15, "page loaded")
        ctx = self.hud.new_context(viewport={"width": w + 20, "height": h + 20}, device_scale_factor=DPR,
                                    bypass_csp=True)   # the script's evaluate() only
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{self.hud_port}/")
        page.wait_for_function("window.__m !== undefined")
        page.evaluate("([y]) => __m.setMark(y)", [12 if path == "/scroll" else 3])
        page.evaluate("([t, w, h, a]) => __m.start(t, w, h, a)", [tab, w, h, self.args.ack])
        R.eventually(lambda: page.evaluate("__m.take().frames.length") or None, 15, "first frame")
        if path == "/typing":     # focus: a click into the textarea first
            for kind in ("mousePressed", "mouseReleased"):
                page.evaluate("m => __m.send(m)", {"t": "mouse", "e": {
                    "type": kind, "x": 200, "y": 60, "modifiers": 0, "button": "left",
                    "buttons": 1 if kind == "mousePressed" else 0, "clickCount": 1, "pointerType": "mouse"}})
        time.sleep(1.0)
        hud_off = self.offset(page.evaluate)
        page_off = self.offset(lambda e: self.remote_eval(tab, e))
        self.remote_eval(tab, "window.__ev = []")
        page.evaluate("__m.take()")
        TRACE.reset()
        msgs, times = self.plan(scenario, w, h)
        page.evaluate("([m, t]) => __m.play(m, t)", [msgs, times])
        time.sleep(1.0)
        hud = page.evaluate("__m.take()")
        events = self.remote_eval(tab, "window.__ev")
        stream = self.host._tabs[tab].stream
        counters = {"frames_sent": stream.frames_sent, "frames_replaced": stream.frames_replaced,
                    "frames_dropped": stream.frames_dropped, "merged": stream.merged,
                    "chrome_frames": len(TRACE.cdp_frames),
                    "dqt0": dict(sorted(collections.Counter(r.get("q") for r in TRACE.cdp_frames).items(),
                                             key=lambda kv: -1 if kv[0] is None else kv[0]))}
        ctx.close()
        self.owner("DELETE", f"/browser/tabs/{tab}")
        return analyse(scenario, size_name, hud, events, hud_off, page_off, counters)


# -- analysis ---------------------------------------------------------------------------------------

def analyse(scenario, size_name, hud, events, hud_off, page_off, counters):
    # Everything in daemon-clock ms.
    ho, po = hud_off[0], page_off[0]
    frames = [dict(f) for f in hud["frames"]]
    for f in frames:
        for k in ("recv", "dec0", "dec1", "drawn", "ack"):
            if f.get(k) is not None:
                f[k] -= ho
        d = TRACE.sent.get(f["id"]) or {}
        f["d"] = d
    drawn = [f for f in frames if f.get("drawn") is not None]
    writes = list(TRACE.writes)
    # writes line up with sent frames in order (one socket, FIFO; nothing else is that big)
    sent_order = sorted(TRACE.sent.items())
    for (fid, d), wr in zip(sent_order, writes):
        d["written0"], d["written"] = wr[0] * 1000, wr[1] * 1000
    sent = [dict(s, t=s["t"] - ho) for s in hud["sent"]]
    ev = [dict(e, t=e["t"] - po) for e in events]
    deliveries = TRACE.deliver
    enq = TRACE.enq

    def first_frame(pred):
        for f in drawn:
            if f.get("mark") is not None and pred(f["mark"]):
                return f
        return None

    rows = []          # one per sent input: dict of stage -> ms
    if scenario in ("typing", "slowtype"):
        downs = [s for s in sent if s["msg"]["e"]["type"] == "keyDown"]
        kevs = [e for e in ev if e["type"] == "keydown"]
        dl = [d for d in deliveries if d[3].get("type") == "keyDown"]
        en = [e for e in enq if e[2].get("type") == "keyDown"]
        for i, s in enumerate(downs):
            if i >= len(kevs):
                break
            e = kevs[i]
            f = first_frame(lambda m, q=e["seq"]: m >= q)
            rows.append(chain(s, en[i] if i < len(en) else None, dl[i] if i < len(dl) else None, e, f))
    elif scenario == "hover":
        at = {(e["x"], e["y"]): e for e in ev if e["type"] == "mousemove"}
        dl = {(d[3].get("x"), d[3].get("y")): d for d in deliveries}
        en = {(e[2].get("x"), e[2].get("y")): e for e in enq}
        for i, s in enumerate(sent):
            # a move merged into a later one shows when that one does
            k = i
            while k < len(sent) and (sent[k]["msg"]["e"]["x"], sent[k]["msg"]["e"]["y"]) not in at:
                k += 1
            if k >= len(sent):
                break
            key = (sent[k]["msg"]["e"]["x"], sent[k]["msg"]["e"]["y"])
            e = at[key]
            f = first_frame(lambda m, q=e["seq"]: (m - q) % 65536 < 32768)
            rows.append(chain(s, en.get(key), dl.get(key), e, f))
    elif scenario == "wheel":
        # A notch shows in the first frame whose row moved past the rest
        # position before it.
        wevs = [e for e in ev if e["type"] == "wheel"]
        prev_mark = None
        for i, s in enumerate(sent):
            rest = [f for f in drawn if f["drawn"] <= s["t"] and f.get("mark") is not None]
            rest_mark = rest[-1]["mark"] if rest else prev_mark
            f = next((f for f in drawn if f["drawn"] > s["t"] and f.get("mark") is not None
                      and f["mark"] != rest_mark), None)
            e = wevs[i] if i < len(wevs) else None
            rows.append(chain(s, enq[i] if i < len(enq) else None,
                              deliveries[i] if i < len(deliveries) else None, e, f))
            prev_mark = rest_mark
    # Per-frame costs, whatever input caused them.
    per_frame = {
        "bytes_kb": summary([f["bytes"] / 1024 for f in frames]),
        "capture_to_daemon": summary([(d["event"] - d["capture"]) * 1000 for d in
                                      [f["d"] for f in frames] if d.get("capture") and d.get("event")]),
        "b64_offer": summary([(f["d"]["offered"] - f["d"]["event"]) * 1000 for f in frames
                              if f["d"].get("offered")]),
        "wait_ack": summary([(f["d"]["send"] - f["d"]["offered"]) * 1000 for f in frames
                             if f["d"].get("offered")]),
        "write": summary([f["d"]["written"] - f["d"]["written0"] for f in frames if f["d"].get("written")]),
        "wire_out": summary([f["recv"] - f["d"]["written"] for f in frames if f["d"].get("written")]),
        "hud_queue": summary([f["dec0"] - f["recv"] for f in frames if f.get("dec0")]),
        "decode": summary([f["dec1"] - f["dec0"] for f in frames if f.get("dec1")]),
        "draw": summary([f["drawn"] - f["dec1"] for f in frames if f.get("drawn") and f.get("dec1")]),
        "ack_to_daemon": None,
    }
    acks = {fid: t * 1000 for t, fid in TRACE.acks}
    per_frame["ack_to_daemon"] = summary([acks[f["id"]] - f["ack"] for f in frames
                                         if f.get("ack") and f["id"] in acks])
    span = (frames[-1]["recv"] - frames[0]["recv"]) / 1000 if len(frames) > 1 else 0
    per_frame["fps"] = round((len(frames) - 1) / span, 1) if span else None
    intervals = [b["drawn"] - a["drawn"] for a, b in zip(drawn, drawn[1:])]
    per_frame["draw_interval"] = summary(intervals)
    stages = {}
    for name in ("total", "wire_in", "queue", "cdp_answer", "to_handler", "handler_to_capture",
                 "capture_to_daemon", "b64_offer", "wait_ack", "write", "wire_out", "hud_queue",
                 "decode", "draw"):
        stages[name] = summary([r.get(name) for r in rows])
    return {"scenario": scenario, "size": size_name, "inputs": len(sent), "page_events": len(ev),
            "shown": sum(1 for r in rows if r.get("total") is not None), "stages": stages,
            "per_frame": per_frame, "counters": counters,
            "clock": {"hud_offset_ms": round(ho, 2), "hud_err_ms": round(hud_off[1], 2),
                      "page_offset_ms": round(po, 2), "page_err_ms": round(page_off[1], 2)},
            "frame_px": [drawn[0]["px"] >> 16, drawn[0]["px"] & 0xFFFF] if drawn else None}


def chain(s, en, dl, e, f):
    """Stages for one input `s` (HUD send) through its delivery and the first frame `f` showing it."""
    r = {}
    t0 = s["t"]
    if en:
        r["wire_in"] = en[0] * 1000 - t0
    if dl:
        r["queue"] = dl[0] * 1000 - (en[0] * 1000 if en else t0)
        r["cdp_answer"] = (dl[1] - dl[0]) * 1000
        if e:
            r["to_handler"] = e["t"] - dl[0] * 1000
    if f is None:
        return r
    r["total"] = f["drawn"] - t0
    d = f["d"]
    if e and d.get("capture"):
        r["handler_to_capture"] = d["capture"] * 1000 - e["t"]
    if d.get("capture") and d.get("event"):
        r["capture_to_daemon"] = (d["event"] - d["capture"]) * 1000
    if d.get("offered"):
        r["b64_offer"] = (d["offered"] - d["event"]) * 1000
        r["wait_ack"] = (d["send"] - d["offered"]) * 1000
    if d.get("written"):
        r["write"] = d["written"] - d["send"] * 1000
        r["wire_out"] = f["recv"] - d["written"]
    if f.get("dec0"):
        r["hud_queue"] = f["dec0"] - f["recv"]
        r["decode"] = f["dec1"] - f["dec0"]
        r["draw"] = f["drawn"] - f["dec1"]
    return r


def show(result):
    print(f"\n== {result['scenario']} @ {result['size']} ({result['frame_px']} frame px): "
          f"{result['inputs']} inputs, {result['page_events']} page events, {result['shown']} shown; "
          f"counters {result['counters']}; clocks {result['clock']}")
    print("  per input (ms)        n    p50    p95")
    for name, s in result["stages"].items():
        if s.get("n"):
            print(f"  {name:20s} {s['n']:4d} {s['p50']:6.1f} {s['p95']:6.1f}")
    print("  per frame:")
    for name, s in result["per_frame"].items():
        if isinstance(s, dict) and s.get("n"):
            print(f"  {name:20s} {s['n']:4d} {s['p50']:6.1f} {s['p95']:6.1f}")
        elif s is not None and not isinstance(s, dict):
            print(f"  {name:20s} {s}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", default="both", choices=["pane", "4k", "both"])
    ap.add_argument("--scenario", default="typing,hover,wheel,scroll")
    ap.add_argument("--keys", type=int, default=40, help="key presses / wheel notches per run")
    ap.add_argument("--seconds", type=float, default=4.0, help="pointer / scroll stream length")
    ap.add_argument("--quality", type=int, help="the screencast's JPEG_QUALITY")
    ap.add_argument("--ack", default="decode", choices=["decode", "draw", "recv"],
                    help="decode: the HUD's FramePipe; draw: the pre-B-PERF ack, emulated; recv: on receipt")
    ap.add_argument("--chrome-flag", action="append", default=[])
    ap.add_argument("--json")
    ap.add_argument("--label", default="")
    args = ap.parse_args()
    if os.environ.get("BPERF_LOG"):
        import logging
        logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")
    bench = Bench(args)
    out = []
    try:
        for size in (["pane", "4k"] if args.size == "both" else [args.size]):
            for scenario in args.scenario.split(","):
                result = bench.run(scenario, size)
                result["label"] = args.label
                result["load"] = [round(x, 2) for x in os.getloadavg()]
                show(result)
                out.append(result)
    finally:
        bench.close()
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
