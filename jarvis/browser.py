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

from . import config


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
        return f"Loaded {self.page.url}\nTitle: {self.page.title()}"

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
        """
        self.page.wait_for_timeout(150)
        elements = self.page.evaluate(_REF_SCAN_JS, max_elements)

        body = self.page.evaluate(
            "() => document.body ? document.body.innerText.slice(0, 4000) : ''"
        )
        body = re.sub(r"\n{3,}", "\n\n", body or "").strip()

        lines = [f"URL: {self.page.url}", f"Title: {self.page.title()}", "", "INTERACTIVE:"]
        for el in elements:
            label = el["text"] or "(no label)"
            extra = f" type={el['type']}" if el["type"] else ""
            extra += " checked" if el["checked"] else ""
            lines.append(f"  [{el['ref']}] <{el['tag']}{extra}> {label}")
        if not elements:
            lines.append("  (none found)")
        lines += ["", "PAGE TEXT:", body or "(empty)"]
        return "\n".join(lines)

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
