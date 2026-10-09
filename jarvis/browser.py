"""Browser session management.

One long-lived Playwright browser per process, wrapped so the agent tools stay
thin. Three controls are built in rather than bolted on:

  Allowlist  — named hosts always pass; '*' opens the public internet but
               never private/LAN address space or the face's own origin.
               Checked on the URL the agent asks for AND on wherever the
               page actually lands, so a redirect can't smuggle the browser
               somewhere the first check never saw.
  Clean      — a fresh context every run, never a real Chrome profile. No
               cookies, no saved logins, so a confused agent has no
               credentials to misuse.
  Budget     — a hard cap on actions per session, so a stuck loop stops.

Headed by default (WSLg renders the window on the Windows desktop, so you can
watch); set JARVIS_BROWSER_HEADLESS=1 for batch runs.
"""

from __future__ import annotations

import base64
import os
import queue
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from . import config, untrusted


class BrowserError(RuntimeError):
    pass


# One scan, two consumers: snapshot() renders these elements as text, and
# screenshot_b64(marked=True) draws their refs onto the page as badges. Both
# set the same data-jarvis-ref attribute, so a ref from either channel is
# clickable — that is what lets a vision run work without text snapshots.
_REF_SCAN_JS = """(max) => {
    const out = [];
    const sel = 'a,button,input,select,textarea,[role=button],[role=link],[role=checkbox],[role=radio],[onclick]';
    document.querySelectorAll(sel).forEach((el, i) => {
        if (out.length >= max) return;
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) return;
        const style = getComputedStyle(el);
        if (style.visibility === 'hidden' || style.display === 'none') return;
        el.setAttribute('data-jarvis-ref', 'e' + i);
        out.push({
            ref: 'e' + i,
            tag: el.tagName.toLowerCase(),
            type: el.getAttribute('type') || '',
            text: (el.innerText || el.value || el.getAttribute('aria-label')
                   || el.getAttribute('placeholder') || '').trim().slice(0, 80),
            checked: el.checked === true,
            rect: {x: r.x, y: r.y},
        });
    });
    return out;
}"""

# The snapshot, minus what a person looking at the page cannot see.
#
# innerText already leaves out display:none and visibility:hidden — judged on
# the *computed* style, so class rules and external stylesheets count here,
# which fetch_page (no renderer) cannot do. What innerText keeps, and a reader
# does not see, is text that is rendered but invisible: opacity ~0 (or
# `filter: opacity(0)`), a near-zero font size, a clip or clip-path that leaves
# nothing, a transform or `scale` that flattens to nothing, a (nearly) zero-size
# box that clips its overflow, and anything no scrolling can reach: off the top
# or the reachable left of the page (left/margin/text-indent: -9999px — the
# text's own rectangle is measured, so every spelling of "off-screen" counts;
# on a right-to-left page the reachable left runs negative, computed from the
# scroll width), unless it lies inside the scroll range of a scroll container
# that is itself in reach (a wide table in an `overflow-x:auto` wrapper, a
# chat log scrolled to the bottom). Content pushed left of a scroller's own
# range stays out of reach, so `left:-9999px` inside a scroller is still
# hidden. Those text nodes are hidden for the one innerText read and put back,
# the same node objects in the same places, in a `finally`.
#
# Kept on purpose, because a reader does see it: an opacity that is an
# animation's start state (a transition on opacity, or an animation running or
# pending — scroll reveals and load fade-ins), and aria-hidden text, which means
# "not for assistive technology", not "invisible".
#
# **No page script runs before the read.** Moving a text node or inserting a
# plain <span> fires no custom-element reaction, so the page text and every
# label are read first and the `data-jarvis-ref` attributes (which a custom
# element can observe) are written last. The one element that would have to
# move — a hidden customized built-in `<select is=…>`, whose connectedCallback
# runs on a move — fails the snapshot closed instead.
#
# Interactive elements follow the same rule: a hidden link or button is not
# offered at all; a hidden form control is (custom checkboxes and file inputs
# hide the native control under a visible label), named by its visible <label>
# and marked "(hidden control)", never with its own page text. A visible
# element is labelled by its visible text (a password field never by its
# value); only one with none (an icon button) falls back to its unseen name,
# capped. A select is set aside whole only when the select itself is hidden: a
# `font-size:0` option inside a visible select does not take the select away.
#
# Bounded three ways. Scanning stops once twice the 4000-character slice of
# *visible* text has gone by (innerText follows DOM order, so nothing later can
# reach the slice); hidden text never spends that budget, or padding a page
# with hidden nodes would walk a payload past the scan. Past WRAP_CAP hiding
# places — counted per outermost hiding element, so an accessibility MathML
# copy of one formula is one place however many tokens it has — or past
# WRAP_NODE_CAP hidden text nodes in all, the page text is withheld outright
# (fail closed) rather than returned unfiltered. Nodes innerText already leaves
# out (display:none subtrees, unrendered content, visibility:hidden) are
# neither wrapped nor counted.
#
# Known limits: mask-image and opaque overlays (text under another element is
# "visible" to every property read here), a `:has()` rule that restyles an
# element when its children change can shift the page under the wrap, and
# `Session._submit` has no timeout.
WRAP_CAP = 5000
WRAP_NODE_CAP = 50_000

# What may be printed into a ref line. A tag or a `type` attribute is the
# page's to choose; only a standard HTML element name and a standard input
# type are printed as they are.
_HTML_TAGS = frozenset("""
    a abbr address area article aside audio b bdi bdo blockquote body br button canvas
    caption cite code col colgroup data datalist dd del details dfn dialog div dl dt em
    embed fieldset figcaption figure footer form h1 h2 h3 h4 h5 h6 header hgroup hr i
    iframe img input ins kbd label legend li main map mark menu meter nav object ol
    optgroup option output p picture pre progress q rp rt ruby s samp search section
    select slot small source span strong sub summary sup table tbody td textarea tfoot th
    thead time tr u ul var video wbr svg math
""".split())
_INPUT_TYPES = frozenset("""
    button checkbox color date datetime-local email file hidden image month number
    password radio range reset search submit tel text time url week
""".split())

_JUDGE_JS = r"""
    const se = document.scrollingElement || document.documentElement;
    const minX = getComputedStyle(document.documentElement).direction === 'rtl'
        ? Math.min(0, se.clientWidth - se.scrollWidth) : 0;
    const sx = window.scrollX, sy = window.scrollY;
    const styles = new Map();
    const css = (e) => {
        let s = styles.get(e);
        if (!s) { s = getComputedStyle(e); styles.set(e, s); }
        return s;
    };
    const moving = (e, cs, prop) => {
        const props = cs.transitionProperty.split(',').map((s) => s.trim());
        const durs = cs.transitionDuration.split(',').map(parseFloat);
        for (let i = 0; i < props.length; i++)
            if ((props[i] === prop || props[i] === 'all') && durs[i % durs.length] > 0) return true;
        try {
            return e.getAnimations().some((a) => a.playState === 'running' || a.playState === 'pending');
        } catch (_) { return false; }
    };
    const faint = (e, cs) => {
        if (parseFloat(cs.opacity) <= 0.05 && !moving(e, cs, 'opacity')) return true;
        const m = /opacity\(([^)]*)\)/.exec(cs.filter || '');
        if (!m || !m[1].trim()) return false;
        const v = /%\s*$/.test(m[1]) ? parseFloat(m[1]) / 100 : parseFloat(m[1]);
        return v <= 0.05 && !moving(e, cs, 'filter');
    };
    const clipRect = (cs) => {
        if (cs.position !== 'absolute' && cs.position !== 'fixed') return false;
        const m = /^rect\((.*)\)$/.exec(cs.clip || '');
        if (!m) return false;
        const n = m[1].split(/[\s,]+/).map(parseFloat);
        return n.length === 4 && n.every(Number.isFinite)
            && (n[1] - n[3] <= 1 || n[2] - n[0] <= 1);
    };
    const clipPath = (v) => {
        if (!v || v === 'none') return false;
        let m = /^(circle|ellipse)\(([^)]*)\)/.exec(v);
        if (m) {
            // circle(0), or an ellipse with either radius zero: no area.
            const radii = m[2].split(' at ')[0].trim().split(/\s+/).slice(0, m[1] === 'circle' ? 1 : 2);
            return radii.some((x) => x !== '' && parseFloat(x) === 0);
        }
        m = /^polygon\((?:\s*(?:nonzero|evenodd)\s*,)?([^)]*)\)/.exec(v);
        if (m) {
            const pts = m[1].split(',').map((p) => p.trim().split(/\s+/));
            if (pts.some((p) => p.length !== 2)) return false;
            const units = new Set(pts.flat().filter((s) => parseFloat(s) !== 0)
                .map((s) => (s.endsWith('%') ? '%' : 'px')));
            if (units.size > 1) return false;
            let a = 0;
            for (let i = 0; i < pts.length; i++) {
                const [x1, y1] = pts[i].map(parseFloat);
                const [x2, y2] = pts[(i + 1) % pts.length].map(parseFloat);
                a += x1 * y2 - x2 * y1;
            }
            return pts.length < 3 || a === 0;
        }
        m = /^inset\(([^)]*)\)/.exec(v);
        if (!m) return false;
        let s = m[1].split(' round ')[0].trim().split(/\s+/);
        s = s.length === 1 ? [s[0], s[0], s[0], s[0]] : s.length === 2 ? [s[0], s[1], s[0], s[1]]
            : s.length === 3 ? [s[0], s[1], s[2], s[1]] : s.slice(0, 4);
        if (s.some((x) => /px$/.test(x) && parseFloat(x) >= 999)) return true;
        const pct = (x) => (parseFloat(x) === 0 ? 0 : /%$/.test(x) ? parseFloat(x) : NaN);
        return pct(s[0]) + pct(s[2]) >= 100 || pct(s[1]) + pct(s[3]) >= 100;
    };
    const flat = (cs) => {
        const m = /^matrix\(([^)]*)\)$/.exec(cs.transform || '');
        if (m) {
            const [a, b, c, d] = m[1].split(',').map(parseFloat);
            if (a * d - b * c === 0) return true;
        }
        if (!cs.scale || cs.scale === 'none') return false;
        const s = cs.scale.split(/\s+/).map(parseFloat);
        return s[0] === 0 || s[s.length > 1 ? 1 : 0] === 0;
    };
    // Does this element hide everything inside it? Cached, and resolved
    // top-down without recursion, so a deeply nested page cannot overflow.
    const ruled = new Map();
    const hides = (el) => {
        const chain = [];
        for (let e = el; e && e !== document.documentElement && !ruled.has(e); e = e.parentElement)
            chain.push(e);
        for (let i = chain.length - 1; i >= 0; i--) {
            const e = chain[i];
            let h = !!(e.parentElement && ruled.get(e.parentElement));
            if (!h) {
                const cs = css(e);
                h = faint(e, cs) || clipRect(cs) || clipPath(cs.clipPath) || flat(cs);
                if (!h) {
                    const cx = /hidden|clip/.test(cs.overflowX), cy = /hidden|clip/.test(cs.overflowY);
                    if (cx || cy) {
                        const r = e.getBoundingClientRect();
                        h = (cx && r.width <= 1) || (cy && r.height <= 1);
                    }
                }
            }
            ruled.set(e, h);
        }
        return !!ruled.get(el);
    };
    // The outermost element whose hiding hides `el` (one hiding place).
    const hidingRoot = (el) => {
        let g = el;
        while (g.parentElement && ruled.get(g.parentElement)) g = g.parentElement;
        return g;
    };
    // Is rect r, inside element e, out of every reader's reach? Off the top or
    // the reachable left of the page it is, unless it lies inside the scroll
    // range of a scroll container that is itself in reach. The range starts at
    // the scroller's content edge (for RTL, scrollWidth - clientWidth further
    // left), so content pushed left of it is still out of reach.
    const offPage = (r, e) => {
        if (r.right + sx > minX && r.bottom + sy > 0) return false;
        for (let a = e; a && a !== document.body && a !== document.documentElement; a = a.parentElement) {
            const cs = css(a);
            if (!/auto|scroll/.test(cs.overflowX) && !/auto|scroll/.test(cs.overflowY)) continue;
            const b = a.getBoundingClientRect();
            const x0 = b.left + a.clientLeft - a.scrollLeft
                - (cs.direction === 'rtl' ? a.scrollWidth - a.clientWidth : 0);
            const y0 = b.top + a.clientTop - a.scrollTop;
            if (r.right <= x0 + 1 || r.bottom <= y0 + 1) return true;
            return offPage(b, a.parentElement);
        }
        return true;
    };
    const range = document.createRange();
    const textHidden = (n, p, cs) => {
        if (parseFloat(cs.fontSize) < 2 || hides(p)) return true;
        range.selectNodeContents(n);
        const r = range.getBoundingClientRect();
        return r.width < 1 || r.height < 1 || offPage(r, p);
    };
"""

_SNAPSHOT_JS = "(args) => {" + _JUDGE_JS + r"""
    const {limit, max, cap, nodeCap} = args;
    // Reads only: no attribute is written here (see stamp()).
    const scan = (labels) => {
        const out = [];
        const sel = 'a,button,input,select,textarea,[role=button],[role=link],[role=checkbox],[role=radio],[onclick]';
        document.querySelectorAll(sel).forEach((el, i) => {
            if (out.length >= max) return;
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return;
            const st = css(el);
            if (st.visibility === 'hidden' || st.display === 'none') return;
            const control = /^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName);
            const unseen = hides(el) || offPage(r, el.parentElement);
            if (unseen && !control) return;
            const password = el.tagName === 'INPUT' && (el.type || '').toLowerCase() === 'password';
            let text;
            if (!labels) text = '(label withheld)';
            else if (unseen) {
                const named = el.labels && el.labels[0] ? el.labels[0].innerText.trim().slice(0, 40) : '';
                text = named ? named + ' (hidden control)' : '(hidden control)';
            } else {
                text = (el.innerText || (password ? '' : el.value) || el.getAttribute('placeholder') || '')
                    .trim().slice(0, 80);
                if (!text) text = (el.getAttribute('aria-label') || el.getAttribute('title')
                                   || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 40);
            }
            out.push({el, info: {ref: 'e' + i, tag: el.tagName.toLowerCase(),
                                 type: el.getAttribute('type') || '', text, checked: el.checked === true}});
        });
        return out;
    };
    // The only write a custom element can observe, so it comes last, after
    // the page text and every label have been read.
    const stamp = (found) => {
        for (const {el, info} of found) el.setAttribute('data-jarvis-ref', info.ref);
        return found.map((f) => f.info);
    };
    const closed = (reason) => ({elements: stamp(scan(false)), text: '', withheld: reason});
    const body = document.body;
    if (!body) return {elements: stamp(scan(true)), text: '', withheld: ''};
    const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'TEXTAREA']);
    const walker = document.createTreeWalker(body, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
        acceptNode(node) {
            if (node.nodeType === Node.TEXT_NODE)
                return /\S/.test(node.data) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
            const tag = node.nodeName.toUpperCase();
            if (SKIP.has(tag)) return NodeFilter.FILTER_REJECT;
            const cs = css(node);
            if (cs.display === 'none') return NodeFilter.FILTER_REJECT;
            // Not rendered at all (closed <details>, hidden=until-found …):
            // innerText leaves it out already. display:contents and <option>
            // have no box of their own but their text is read, so walk in.
            if (cs.display !== 'contents' && tag !== 'OPTION' && tag !== 'OPTGROUP'
                && node.checkVisibility && !node.checkVisibility())
                return NodeFilter.FILTER_REJECT;
            return NodeFilter.FILTER_SKIP;
        },
    });
    let budget = 2 * limit;
    const hidden = [], queued = new Set(), places = new Set();
    for (let n = walker.nextNode(); n && budget > 0; n = walker.nextNode()) {
        const p = n.parentElement;
        if (!p) continue;
        const cs = css(p);
        if (cs.visibility !== 'visible') continue;  // innerText leaves it out; it spends nothing
        const select = p.closest('select');
        // An option's text is drawn by its <select>: it is hidden when the
        // select is, and then the whole select is set aside.
        const h = select ? hides(select) || parseFloat(css(select).fontSize) < 2
                           || offPage(select.getBoundingClientRect(), select.parentElement)
                         : textHidden(n, p, cs);
        if (!h) { budget -= n.data.replace(/\s+/g, ' ').trim().length; continue; }
        if (select && select.hasAttribute('is')) return closed('custom-select');
        const target = select || n;
        if (queued.has(target)) continue;
        queued.add(target);
        hidden.push(target);
        const owner = target === n ? p : select;
        places.add(hides(owner) ? hidingRoot(owner) : target);
        if (places.size > cap || hidden.length > nodeCap) return closed('flood');
    }
    const wraps = [];
    let found, text;
    try {
        for (const n of hidden) {
            const s = document.createElement('span');
            s.style.setProperty('display', 'none', 'important');
            n.parentNode.insertBefore(s, n);
            s.appendChild(n);
            wraps.push([s, n]);
        }
        found = scan(true);
        text = body.innerText.slice(0, limit);
    } finally {
        for (const [s, n] of wraps) s.replaceWith(n);
    }
    return {elements: stamp(found), text, withheld: ''};
}"""

_MARK_JS = """(els) => {
    document.getElementById('__jarvis-marks')?.remove();
    const box = document.createElement('div');
    box.id = '__jarvis-marks';
    for (const el of els) {
        const m = document.createElement('div');
        m.textContent = el.ref;
        m.style.cssText =
            'position:fixed;left:' + Math.max(0, el.rect.x - 2) + 'px;' +
            'top:' + Math.max(0, el.rect.y - 13) + 'px;' +
            'background:#111;color:#ffd400;font:700 10px/1.2 monospace;' +
            'padding:1px 3px;border-radius:3px;pointer-events:none;' +
            'z-index:2147483647;';
        box.appendChild(m);
    }
    document.body.appendChild(box);
}"""


@dataclass
class BrowserPolicy:
    allowed_hosts: list[str] = field(
        default_factory=lambda: ["localhost", "127.0.0.1", "*"]
    )
    """Hosts the agent may navigate to. Named hosts always pass; '*' means
    the public internet — private/LAN address space stays blocked unless a
    host is listed by name. Drop '*' to pin a session to specific hosts
    (benches do this to stay hermetic)."""

    max_actions: int = 120
    headless: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_BROWSER_HEADLESS") == "1"
    )
    slow_mo_ms: int = field(
        default_factory=lambda: int(os.environ.get("JARVIS_BROWSER_SLOWMO", "250"))
    )
    viewport: tuple[int, int] = (1280, 800)
    trace_dir: Path = field(default_factory=lambda: config.REPO_ROOT / "traces")

    def allows(self, url: str) -> bool:
        # Refused ahead of everything, including '*': the face is where the
        # owner approves dangerous tools, and an agent that can open its own
        # HUD can click its own approval button. A gate you can reach is not
        # a gate.
        if config.is_face_origin(url):
            return False
        host = (urlparse(url).hostname or "").lower()
        if any(
            host == a.lower() or host.endswith("." + a.lower())
            for a in self.allowed_hosts
            if a != "*"
        ):
            return True
        # '*' opens the public internet only. The LAN — router admin panel,
        # other devices — stays blocked so a hostile page can't steer the
        # agent onto the home network.
        return "*" in self.allowed_hosts and not config.is_lan_host(host)


class Session:
    """A running browser. Lazily started, explicitly stopped.

    Every public method executes on the session's own thread (see _submit);
    to callers it is just a slow function call. Outside code must not use
    `.page` for anything that talks to the browser — that only works from
    the browser thread. Tests and benches that need scripting use eval_js().
    """

    def __init__(self, policy: BrowserPolicy | None = None):
        self.policy = policy or BrowserPolicy()
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self.actions = 0
        self._work: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None

    # -- the browser thread ------------------------------------------------
    # Playwright's sync API is thread-affine, and starting it parks a
    # *running* asyncio event loop on the caller's thread. Both properties
    # hurt: prompt_toolkit refuses to prompt on a thread with a live loop
    # (the 2026-07-31 chat-REPL crash: "asyncio.run() cannot be called from
    # a running event loop"), and the face answers every request on a fresh
    # thread, which Playwright would reject outright. So one dedicated
    # thread owns the browser and everything else marshals onto it.

    def _submit(self, fn):
        if self._thread is not None and threading.current_thread() is self._thread:
            return fn()  # nested call, already on the browser thread
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._serve, name="jarvis-browser", daemon=True
            )
            self._thread.start()
        box: list = [None, None]  # [result, exception]
        done = threading.Event()
        self._work.put((fn, box, done))
        done.wait()
        if box[1] is not None:
            raise box[1]
        return box[0]

    def _serve(self) -> None:
        while True:
            fn, box, done = self._work.get()
            try:
                box[0] = fn()
            except BaseException as exc:  # hand every failure to the caller
                box[1] = exc
            finally:
                done.set()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._submit(self._start)

    def _start(self) -> None:
        if self._page is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise BrowserError(f"playwright is not installed: {exc}") from exc

        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(
                headless=self.policy.headless,
                slow_mo=self.policy.slow_mo_ms if not self.policy.headless else 0,
            )
        except Exception as exc:
            self._pw.stop()
            self._pw = None
            raise BrowserError(
                f"could not launch chromium: {exc}\n"
                "If this mentions missing libraries, run:\n"
                "  sudo .venv/bin/playwright install-deps chromium"
            ) from exc

        # A fresh context every run: no stored cookies, no logged-in sessions.
        self._context = self._browser.new_context(
            viewport={"width": self.policy.viewport[0], "height": self.policy.viewport[1]},
        )
        self.policy.trace_dir.mkdir(parents=True, exist_ok=True)
        self._context.tracing.start(screenshots=True, snapshots=True)
        self._page = self._context.new_page()

    def stop(self, trace_name: str = "session") -> Path | None:
        """Close everything and write the trace. Returns the trace path.

        Safe to call when nothing ever started — surfaces call this on the
        way out (a browser left running dies with an EPIPE tantrum from the
        Node driver when the process exits underneath it).
        """
        if self._pw is None:
            return None
        return self._submit(lambda: self._stop(trace_name))

    def _stop(self, trace_name: str) -> Path | None:
        trace_path = None
        try:
            if self._context is not None:
                trace_path = self.policy.trace_dir / f"{trace_name}.zip"
                self._context.tracing.stop(path=str(trace_path))
                self._context.close()
        except Exception:
            trace_path = None
        finally:
            if self._browser is not None:
                self._browser.close()
            if self._pw is not None:
                self._pw.stop()
            self._pw = self._browser = self._context = self._page = None
            self.actions = 0
        return trace_path

    # -- guards ------------------------------------------------------------

    @property
    def page(self):
        if self._page is None:
            self.start()
        return self._page

    def _spend(self) -> None:
        self.actions += 1
        if self.actions > self.policy.max_actions:
            raise BrowserError(
                f"action budget exhausted ({self.policy.max_actions}). "
                "Stop and report what you accomplished."
            )

    # -- actions -----------------------------------------------------------
    # Public wrappers marshal onto the browser thread; _impl versions below
    # hold the logic and run there.

    def goto(self, url: str) -> str:
        return self._submit(lambda: self._goto(url))

    def snapshot(self, max_elements: int = 120) -> str:
        return self._submit(lambda: self._snapshot(max_elements))

    def click(self, ref: str) -> str:
        return self._submit(lambda: self._click(ref))

    def type_text(self, ref: str, text: str, submit: bool = False) -> str:
        return self._submit(lambda: self._type_text(ref, text, submit))

    def screenshot_b64(
        self, full_page: bool = False, marked: bool = False
    ) -> tuple[str, int, int]:
        return self._submit(lambda: self._screenshot_b64(full_page, marked))

    def eval_js(self, expression: str):
        """Evaluate JS on the current page — for tests and benches only.

        Deliberately not registered as a tool: agents must never get a
        JS-eval channel (it would expose test backdoors like
        window.__answerKey).
        """
        return self._submit(lambda: self.page.evaluate(expression))

    def _goto(self, url: str) -> str:
        if not url.startswith(("http://", "https://")):
            raise BrowserError("url must start with http:// or https://")
        if not self.policy.allows(url):
            if config.is_face_origin(url):
                raise BrowserError(
                    "that is Jarvis's own control plane, which is off limits. "
                    "Ask the owner directly instead of driving the window."
                )
            if "*" in self.policy.allowed_hosts:
                raise BrowserError(
                    f"{urlparse(url).hostname!r} is on the private network (or "
                    "does not resolve), which is blocked. Public sites and "
                    "localhost are fine; a LAN host needs the owner to "
                    "allowlist it by name."
                )
            raise BrowserError(
                f"navigation to {urlparse(url).hostname!r} is blocked. "
                f"Allowed hosts: {', '.join(self.policy.allowed_hosts)}"
            )
        self._spend()
        self.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        self._guard_landing()
        # The title is the page's own words, so it is fenced like the snapshot.
        title = untrusted.fence(f"Title: {untrusted.one_line(self.page.title())}", self.page.url)
        return f"Loaded {self.page.url}\n{title}"

    def _guard_landing(self) -> None:
        """Re-check policy on wherever the page actually ended up.

        goto() vets the URL the agent asked for, but redirects and clicked
        links decide where the browser lands — without this, a public page
        could bounce the session onto the LAN or the face.
        """
        url = self.page.url
        if not url.startswith(("http://", "https://")):
            return  # about:blank, chrome-error://, etc.
        if not self.policy.allows(url):
            host = urlparse(url).hostname
            self.page.goto("about:blank")
            raise BrowserError(
                f"the page navigated to blocked host {host!r}; "
                "backed out to about:blank."
            )

    def _snapshot(self, max_elements: int = 120) -> str:
        """Text view of the page: interactive elements with stable refs.

        This is the channel text-only models use. Refs are exact, so there is
        no coordinate guessing — usually more reliable than vision for the web.

        Every line of it is the page talking (the title, the labels, the body
        text), so the whole snapshot is fenced as untrusted web content, and
        it leaves out what a person viewing the page cannot see
        (`_SNAPSHOT_JS`). The format inside the fence is unchanged.
        """
        self.page.wait_for_timeout(150)
        result = self.page.evaluate(
            _SNAPSHOT_JS,
            {"limit": 4000, "max": max_elements, "cap": WRAP_CAP, "nodeCap": WRAP_NODE_CAP},
        )
        elements = result.get("elements") or []
        notes = []
        withheld = result.get("withheld")
        if withheld:
            # Fail closed: a page hiding this much, or hiding text in an element
            # that would run page script to be set aside, is not filtered
            # best-effort.
            body = "(withheld — see the note after the fence)"
            why = (
                "it hides text in a custom <select> element, which would run the "
                "page's own script to be set aside"
                if withheld == "custom-select"
                else f"it hides text in more than {WRAP_CAP:,} places, too many to filter"
            )
            notes.append(
                f"[page text withheld: {why}, so none of its text is shown. "
                "browser_screenshot shows what a reader sees.]"
            )
        else:
            body = untrusted.strip_invisible(result.get("text") or "")
            body = re.sub(r"\n{3,}", "\n\n", body).strip()

        # Title, labels, tag names and type attributes are the page's words:
        # one line each, so none can draw a ref line of its own.
        title = untrusted.one_line(self.page.title())
        lines = [f"URL: {self.page.url}", f"Title: {title}", "", "INTERACTIVE:"]
        for el in elements:
            label = untrusted.one_line(el["text"], cap=80) or "(no label)"
            kind = (el["type"] or "").strip().lower()
            kind = kind if kind in _INPUT_TYPES else ""
            tag = el["tag"] if el["tag"] in _HTML_TAGS else "element"
            extra = f" type={kind}" if kind else ""
            extra += " checked" if el["checked"] else ""
            lines.append(f"  [{el['ref']}] <{tag}{extra}> {label}")
        if not elements:
            lines.append("  (none found)")
        lines += ["", "PAGE TEXT:", body or "(empty)"]
        return untrusted.fence("\n".join(lines), self.page.url, notes=notes)

    def _locator(self, ref: str):
        locator = self.page.locator(f'[data-jarvis-ref="{ref}"]')
        if locator.count() == 0:
            raise BrowserError(
                f"no element {ref!r} on this page. Take a fresh snapshot or "
                "marked screenshot first — refs are reassigned whenever the "
                "page changes."
            )
        return locator.first

    def _click(self, ref: str) -> str:
        self._spend()
        self._locator(ref).click(timeout=10_000)
        self.page.wait_for_timeout(300)
        self._guard_landing()
        return f"Clicked {ref}. Now at {self.page.url}"

    def _type_text(self, ref: str, text: str, submit: bool = False) -> str:
        self._spend()
        locator = self._locator(ref)
        locator.fill(text, timeout=10_000)
        if submit:
            locator.press("Enter")
            self.page.wait_for_timeout(500)
            self._guard_landing()
        return f"Typed into {ref}{' and pressed Enter' if submit else ''}."

    def _screenshot_b64(
        self, full_page: bool = False, marked: bool = False
    ) -> tuple[str, int, int]:
        """PNG of the page as (base64, byte_size, marks_drawn).

        With marked=True, each interactive element gets a badge showing its
        ref (same refs snapshot() would assign), drawn just for the capture
        and removed afterwards. Badges are positioned for the viewport, so
        marked full-page captures may misplace them below the fold.
        """
        self._spend()
        marks = 0
        if marked:
            self.page.wait_for_timeout(150)
            elements = self.page.evaluate(_REF_SCAN_JS, 120)
            marks = len(elements)
            self.page.evaluate(_MARK_JS, elements)
        try:
            raw = self.page.screenshot(full_page=full_page, type="png")
        finally:
            if marked:
                self.page.evaluate(
                    "() => document.getElementById('__jarvis-marks')?.remove()"
                )
        return base64.b64encode(raw).decode(), len(raw), marks


SESSION = Session()
