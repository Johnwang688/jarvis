"""Opening public web pages in new Remote Browser panes of the owner's HUD, on
a chat's word (owner, 2026-11-13: "open 3 other panels in the HUD and pull up
instagram in the browser in them"; decisions D2–D4 of
`docs/plans/2026-11-13-agent-pane-grid-decisions.md`).

`hud_open_browser` (`tools/hud_open_browser.py`) reaches this service and
nothing else. It judges every URL here first, refusing the whole call as text
if any one is not a public http(s) page (`judge_public_url`), then publishes
one bus record, `browser_open` `{thread_id, data: {urls, request_id?}}`, and
waits up to three seconds for the first window's counts
(`agent_panes.PaneReports`), never under the daemon lock.

Each HUD window places the panes as one batch (`lib/agentpanes.ts`, as
`hud_open_subagents`): existing free panes first, then a balanced grid,
never the chat's own pane, the focused or keyboard pane, a pane an agent is
driving or one holding work, never moving focus. Each new pane is a Browser
view on the default (named, never Private) profile with one Remote tab at
its URL, marked **not the owner's** (`source: {kind: "unknown"}`, so no
agent can look at or drive it until the owner navigates it himself), and
created through the window's restore path, which is never the owner's own
navigation: it admits no loopback port in the gate (BR-1a/BR-1c and
amendment 2's L2). The window judges each URL again and refuses loopback.

What it cannot do: open a loopback, LAN, link-local or Jarvis address, any
scheme but http(s), a Private pane, a Direct frame, take focus, close a pane,
or touch a pane's see/drive switches. Bound as `hud_open_subagents` is: a
turn the owner started at the HUD in this Claude or Codex chat, the
conversation itself, checked by the tool and again here under the daemon
lock (`agent_panes.refuse_desk_caller`).
"""
from __future__ import annotations

import ipaddress
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from urllib.parse import urlsplit

from .agent_panes import REPORT_WAIT_S, PaneReports, refuse_desk_caller
from .browser import policy

NOT_IN_CHAT = "Error: hud_open_browser works only inside a Jarvis HUD chat."
NOT_AT_DESK = ("Error: Browser panes can be opened only during a turn the owner started at the "
               "HUD in this Claude or Codex chat (not Discord, the DM, a schedule, a task, a "
               "sub-agent or a turn another chat's message started).")
NOT_FROM_A_SUB_AGENT = "Error: a sub-agent cannot open panes; only the conversation itself can."
STOPPING = "Error: Jarvis is stopping, so no panes are opened."

# The HUD draws at most 12 panes; one shows this chat.
OPEN_MAX = 11
# What a HUD pane stores of a URL (`workspace.ts` URL_MAX).
URL_MAX = 2048
RESOLVE_TIMEOUT_S = 3.0

# Names that never mean a public site: one label, or a suffix for this
# machine, a home or office network, or reverse DNS (RFC 6761/6762/8375).
LOCAL_SUFFIXES = (".localhost", ".local", ".lan", ".home", ".internal", ".intranet", ".corp",
                  ".localdomain", ".home.arpa", ".arpa", ".test", ".invalid", ".example")

PUBLIC_ONLY = ("only public http or https web pages open this way (no loopback, home network, "
               "Jarvis or other private address)")


class NotPublic(ValueError):
    """A URL `hud_open_browser` will not open. Its text never contains the URL."""


def _resolve(name: str, port: int) -> list[str]:
    from .browser.gate import default_resolve
    return default_resolve(name, port)


# One lookup per URL at once; a lookup the call stopped waiting for still holds
# its thread until the system resolver gives up (as the gate's DNS pool).
_RESOLVER = ThreadPoolExecutor(max_workers=2 * OPEN_MAX, thread_name_prefix="browser-open-dns")


def judge_public_url(url, *, refused: frozenset[int], resolve=_resolve,
                     timeout: float = RESOLVE_TIMEOUT_S) -> str:
    """The URL to open, or `NotPublic`. First the Browser's own URL judge (the
    one agent navigation and the owner's routes use: `policy.judge_url`,
    which refuses other schemes, user names, ambiguous authorities and every
    spelling of Jarvis's ports); then this tool's stricter rule: http(s)
    only, no Jarvis port on any host (`policy.judge_port`), and a host that is
    public: an IP literal of the public class, or a name that is not local
    by its spelling and resolves only to public addresses now. The SOCKS gate
    still judges every connection the page makes afterwards."""
    if not isinstance(url, str) or len(url.strip()) > URL_MAX:
        raise NotPublic(f"each URL is text of at most {URL_MAX} characters")
    try:
        target = policy.judge_url(url, refused=refused)
    except policy.URLRefused as refusal:
        raise NotPublic(str(refusal)) from None
    if target == "about:blank":
        raise NotPublic(PUBLIC_ONLY)
    parts = urlsplit(target)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise NotPublic(PUBLIC_ONLY)
    try:
        host, port = parts.hostname or "", parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        raise NotPublic(policy.MALFORMED_SENTENCE) from None
    if policy.judge_port(port, refused=refused) is not None:
        raise NotPublic(policy.JARVIS_PORT_SENTENCE)
    kind, address = policy.host_kind(host)
    if kind == "literal":
        if policy.address_class(address) != "public":
            raise NotPublic(PUBLIC_ONLY)
        return target
    if kind != "name":
        raise NotPublic(PUBLIC_ONLY)
    bare = host.lower().rstrip(".")
    if "." not in bare or bare.endswith(LOCAL_SUFFIXES) or bare in ("localhost", "local"):
        raise NotPublic(PUBLIC_ONLY)
    try:
        addresses = _RESOLVER.submit(resolve, bare, port).result(timeout=timeout)
    except FutureTimeout:
        raise NotPublic("a page's address could not be looked up in time") from None
    except Exception:
        raise NotPublic("a page's address could not be looked up") from None
    if not addresses:
        raise NotPublic("a page's address could not be looked up")
    for value in addresses:
        try:
            ip = ipaddress.ip_address(str(value).split("%", 1)[0])
        except ValueError:
            raise NotPublic(PUBLIC_ONLY) from None
        if policy.address_class(ip) != "public":
            raise NotPublic(PUBLIC_ONLY)
    return target


def _site(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


class BrowserOpens:
    def __init__(self, daemon, reports: PaneReports | None = None, *, resolve=_resolve):
        self.daemon = daemon
        self.closed = True
        self.resolve = resolve
        self.reports = reports or getattr(daemon, "agent_pane_reports", None) or PaneReports(daemon)

    def _refused_ports(self) -> frozenset[int]:
        host = getattr(self.daemon, "browser", None)
        ports = getattr(host, "refused_ports", None)
        return ports() if callable(ports) else policy.refused_ports(
            hud_port=getattr(self.daemon, "face_port", None), api_port=getattr(self.daemon, "port", None))

    def open(self, caller: dict | None, urls) -> str:
        """Ask every HUD window to open `urls` (1 to 11 public pages), each in
        its own new Remote Browser pane. Returns what was shown, or a
        sentence starting "Error: " (and then nothing was published)."""
        if (not isinstance(caller, dict) or not isinstance(caller.get("thread_id"), str)
                or not isinstance(caller.get("turn_id"), str)):
            return NOT_IN_CHAT
        if (not isinstance(urls, list) or not urls or len(urls) > OPEN_MAX
                or not all(isinstance(u, str) and u.strip() for u in urls)):
            return f"Error: urls is a list of 1 to {OPEN_MAX} web addresses."
        with self.daemon._lock:
            why_not, session = refuse_desk_caller(self.daemon, self.closed, caller, NOT_AT_DESK, STOPPING)
            if why_not is not None:
                return why_not
            thread_id = session.thread.id
        # Judged outside the daemon lock (a name is looked up), every one
        # before anything is published: one refusal refuses the whole call.
        refused = self._refused_ports()
        with ThreadPoolExecutor(max_workers=len(urls), thread_name_prefix="browser-open-judge") as pool:
            futures = [pool.submit(judge_public_url, url, refused=refused, resolve=self.resolve) for url in urls]
            judged = []
            for n, future in enumerate(futures, 1):
                try:
                    judged.append(future.result())
                except NotPublic as refusal:
                    return f"Error: URL {n} was refused, so nothing was opened: {refusal}."
        # The turn may have ended while names were looked up.
        with self.daemon._lock:
            why_not, _ = refuse_desk_caller(self.daemon, self.closed, caller, NOT_AT_DESK, STOPPING)
            if why_not is not None:
                return why_not
        return self._publish_and_wait(thread_id, judged)

    def _publish_and_wait(self, thread_id: str, urls: list[str]) -> str:
        from .model import utcnow
        reports, n = self.reports, len(urls)
        sites = ", ".join(dict.fromkeys(_site(u) for u in urls))
        windows = reports.windows()
        request_id = reports.expect(n) if windows != 0 else None
        data = {"urls": list(urls)}
        if request_id is not None:
            data["request_id"] = request_id
        try:
            self.daemon.bus.publish({"kind": "browser_open", "thread_id": thread_id, "at": utcnow(),
                                     "data": data})
        except Exception:
            reports.forget(request_id)
            raise
        asked = f"Asked the HUD to open {n} Browser pane(s) at {sites}."
        if windows == 0:
            return f"{asked} No HUD window is open, so none is shown."
        answer = reports.wait(request_id)
        if answer is None:
            return (f"{asked} No HUD window replied within {REPORT_WAIT_S:g} seconds (a card or "
                    f"Settings may be open there), so whether all {n} opened is not known.")
        placed, not_placed = answer["placed"], answer["not_placed"]
        if not_placed == 0:
            return (f"Opened {placed} of {n} Browser pane(s) at {sites}, in Remote mode. They are "
                    "not yours to look at or drive until the owner navigates them himself.")
        return (f"Opened {placed} of {n} Browser pane(s) at {sites}; no room for {not_placed} (the "
                "window is too small for more panes, or the rest hold the owner's work).")

    # -- install / stop (as subagent_panes.SubagentPanes) ------------------------------

    def install(self) -> None:
        global _installed
        with _installed_lock:
            self.closed = False
            _installed = self

    def stop(self) -> None:
        global _installed
        self.closed = True
        with _installed_lock:
            if _installed is self:
                _installed = None


_installed_lock = threading.Lock()
_installed: BrowserOpens | None = None


def installed() -> BrowserOpens | None:
    """The running daemon's service, or None (no daemon, or it stopped)."""
    with _installed_lock:
        service = _installed
    return service if service is not None and not service.closed else None
