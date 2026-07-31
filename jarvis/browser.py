"""Browser session management.

One long-lived Playwright browser per process, wrapped so the agent tools stay
thin. Three controls are built in rather than bolted on:

  Allowlist  — navigation outside the allowed hosts is refused. This is the
               control that matters: it makes wandering off structurally
               impossible instead of something a human has to catch in time.
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
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from . import config


class BrowserError(RuntimeError):
    pass


@dataclass
class BrowserPolicy:
    allowed_hosts: list[str] = field(
        default_factory=lambda: ["localhost", "127.0.0.1"]
    )
    """Hosts the agent may navigate to. '*' disables the check entirely."""

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
        if "*" in self.allowed_hosts:
            return True
        host = (urlparse(url).hostname or "").lower()
        return any(
            host == a.lower() or host.endswith("." + a.lower()) for a in self.allowed_hosts
        )


class Session:
    """A running browser. Lazily started, explicitly stopped."""

    def __init__(self, policy: BrowserPolicy | None = None):
        self.policy = policy or BrowserPolicy()
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self.actions = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
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
        """Close everything and write the trace. Returns the trace path."""
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

    def goto(self, url: str) -> str:
        if not url.startswith(("http://", "https://")):
            raise BrowserError("url must start with http:// or https://")
        if not self.policy.allows(url):
            raise BrowserError(
                f"navigation to {urlparse(url).hostname!r} is blocked. "
                f"Allowed hosts: {', '.join(self.policy.allowed_hosts)}"
            )
        self._spend()
        self.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        return f"Loaded {self.page.url}\nTitle: {self.page.title()}"

    def snapshot(self, max_elements: int = 120) -> str:
        """Text view of the page: interactive elements with stable refs.

        This is the channel text-only models use. Refs are exact, so there is
        no coordinate guessing — usually more reliable than vision for the web.
        """
        self.page.wait_for_timeout(150)
        elements = self.page.evaluate(
            """(max) => {
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
                    });
                });
                return out;
            }""",
            max_elements,
        )

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
                f"no element {ref!r} on this page. Take a snapshot first — refs "
                "are reassigned whenever the page changes."
            )
        return locator.first

    def click(self, ref: str) -> str:
        self._spend()
        self._locator(ref).click(timeout=10_000)
        self.page.wait_for_timeout(300)
        return f"Clicked {ref}. Now at {self.page.url}"

    def type_text(self, ref: str, text: str, submit: bool = False) -> str:
        self._spend()
        locator = self._locator(ref)
        locator.fill(text, timeout=10_000)
        if submit:
            locator.press("Enter")
            self.page.wait_for_timeout(500)
        return f"Typed into {ref}{' and pressed Enter' if submit else ''}."

    def screenshot_b64(self, full_page: bool = False) -> tuple[str, int]:
        self._spend()
        raw = self.page.screenshot(full_page=full_page, type="png")
        return base64.b64encode(raw).decode(), len(raw)


SESSION = Session()
