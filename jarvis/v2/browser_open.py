"""Opening public web pages in new Remote Browser panes of the owner's HUD, on
a chat's word (owner, 2026-11-13: "open 3 other panels in the HUD and pull up
instagram in the browser in them"; decisions D2-D6 of
`docs/plans/2026-11-13-agent-pane-grid-decisions.md`).

`hud_open_browser` (`tools/hud_open_browser.py`) reaches this service and
nothing else. Every URL is judged here first and one refusal refuses the
whole call, as text that never repeats the URL:

  1. the Browser's URL judge and this tool's public-only rule
     (`judge_public_url`: http(s), no Jarvis port on any host, a public IP
     literal, or a name not local by its spelling that resolves only to
     public addresses);
  2. drive `navigate`'s own fixed rules, the same function it calls
     (`act_rules.pre_judge` on `navigate_facts`): H2's credential in the
     address and H7's belt refuse; an ASK floor (a login or OAuth, an account
     deletion, a payment, an executable download) needs the owner, so it is
     refused with `NEEDS_OWNER` (go there with `hud_browser_act`, which asks);
  3. drive `navigate`'s own reviewer for what those rules CLASSIFY
     (`act_review.judge` and `decide`): anything but its allow is refused the
     same way. No new approval path: this never asks the owner.

The order (delta review of #359): every fixed check on every URL first, then
the name lookups, then the reviewer, so a URL any rule refuses costs no
lookup and no model call, for it or its siblings.

The budget (review of #359): at most `TURN_MAX` (11) pages in one turn
across all its calls, and `HOUR_MAX` (44) in any rolling hour for the
daemon, on the monotonic clock. The reviewer, a paid model call, has its
own: `REVIEWS_TURN_MAX` (22) and `REVIEWS_HOUR_MAX` (88), charged for every
URL before any is sent, whether the call then opens anything or not. Then it
publishes one bus record,
`browser_open` `{thread_id, data: {urls, request_id?}}`, and waits up to
three seconds for the first window's counts (`agent_panes.PaneReports`),
never under the daemon lock.

Each HUD window places the panes as one batch (`lib/agentpanes.ts`, as
`hud_open_subagents`): existing free panes first, then a balanced grid,
never the chat's own pane, the focused or keyboard pane, a pane an agent is
driving or one holding work, never moving focus. **Every page opens in the
owner's default named profile**: a new pane has no profile of its own (the
default), and a free pane is reused only when it is on the default profile,
never Private or another named one, never one whose tabs are full and never
one already showing a page an agent opened (`agentPageMayReuse`, so a
turn's later calls cannot reuse its earlier ones' panes). Each page is one
Remote tab marked **the agent's** (`source: {kind: "agent"}`), created
through the window's restore path, which is never the owner's own
navigation: it admits no loopback port in the gate (BR-1a/BR-1c and
amendment 2's L2). The window judges each URL again and refuses loopback.

**What the chat may then do** (D5, D6): opening does not depend on the
global `agents_may_drive` switch or any pane's drive switch. The chat may
read an opened page whenever that pane's "Jarvis can see" is on (it is by
default); clicking and typing there still need the pane's "Jarvis can
drive", Remote mode and `agents_may_drive`, as on any owner pane. So with
driving off, a chat can still load any public page that passes the rules
above in the owner's default profile, signed in as he is there, and read it.

What it cannot do: open a loopback, LAN, link-local or Jarvis address, any
scheme but http(s), an address drive navigate would refuse or ask about, a
Private pane, a Direct frame, take focus, close a pane, or touch a pane's
see/drive switches. Bound as `hud_open_subagents` is: a turn the owner
started at the HUD in this Claude or Codex chat, the conversation itself,
checked by the tool and again here under the daemon lock
(`agent_panes.refuse_desk_caller`).
"""
from __future__ import annotations

import collections
import ipaddress
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from urllib.parse import urlsplit

from .agent_panes import REPORT_WAIT_S, PaneReports, refuse_desk_caller
from .browser import act_review, act_rules, policy

NOT_IN_CHAT = "Error: hud_open_browser works only inside a Jarvis HUD chat."
NOT_AT_DESK = ("Error: Browser panes can be opened only during a turn the owner started at the "
               "HUD in this Claude or Codex chat (not Discord, the DM, a schedule, a task, a "
               "sub-agent or a turn another chat's message started).")
NOT_FROM_A_SUB_AGENT = "Error: a sub-agent cannot open panes; only the conversation itself can."
STOPPING = "Error: Jarvis is stopping, so no panes are opened."

# The HUD draws at most 12 panes; one shows this chat.
OPEN_MAX = 11
# The budget (review of #359): at most this many pages in one turn, across
# every call it makes, and this many in any rolling hour for the daemon.
TURN_MAX = 11
HOUR_MAX = 44
HOUR_S = 3600.0
# Turns whose counts are kept (oldest forgotten first; a turn is short).
TURNS_KEPT = 64
# Drive navigate's reviewer is a paid model call, charged per URL sent to it
# whether the call then opens anything or not (delta review of #359: refused
# calls used to pay every review and charge nothing). Twice the page budget.
REVIEWS_TURN_MAX = 2 * TURN_MAX
REVIEWS_HOUR_MAX = 2 * HOUR_MAX
# What a HUD pane stores of a URL (`workspace.ts` URL_MAX).
URL_MAX = 2048
RESOLVE_TIMEOUT_S = 3.0

# Names that never mean a public site: one label, or a suffix for this
# machine, a home or office network, or reverse DNS (RFC 6761/6762/8375).
LOCAL_SUFFIXES = (".localhost", ".local", ".lan", ".home", ".internal", ".intranet", ".corp",
                  ".localdomain", ".home.arpa", ".arpa", ".test", ".invalid", ".example")

PUBLIC_ONLY = ("only public http or https web pages open this way (no loopback, home network, "
               "Jarvis or other private address)")
# An address drive `navigate` would ask the owner about (an ASK floor: a
# login or OAuth, an account deletion, a payment, an executable download), or
# one its reviewer does not clear: never opened unasked.
NEEDS_OWNER = ("that page needs the owner's OK; open a Browser pane and go there with the Browser's "
               "own navigate (hud_browser_act), which asks the owner first")


class NotPublic(ValueError):
    """A URL `hud_open_browser` will not open. Its text never contains the URL."""


def _resolve(name: str, port: int) -> list[str]:
    from .browser.gate import default_resolve
    return default_resolve(name, port)


# One lookup per URL at once; a lookup the call stopped waiting for still holds
# its thread until the system resolver gives up (as the gate's DNS pool).
_RESOLVER = ThreadPoolExecutor(max_workers=2 * OPEN_MAX, thread_name_prefix="browser-open-dns")


def judge_public_url(url, *, refused: frozenset[int], resolve=_resolve,
                     timeout: float = RESOLVE_TIMEOUT_S, lookup: bool = True) -> str:
    """The URL to open, or `NotPublic`. First the Browser's own URL judge (the
    one agent navigation and the owner's routes use: `policy.judge_url`,
    which refuses other schemes, user names, ambiguous authorities and every
    spelling of Jarvis's ports); then this tool's stricter rule: http(s)
    only, no Jarvis port on any host (`policy.judge_port`), and a host that is
    public: an IP literal of the public class, or a name that is not local
    by its spelling and resolves only to public addresses now. The SOCKS gate
    still judges every connection the page makes afterwards. With `lookup`
    false, every check but the name lookup (no network, no model)."""
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
    # Drive `navigate`'s own fixed rules, the same function it calls (H2: a
    # credential in the address; H7's belt; the ASK floors). REFUSE is its
    # fixed sentence; an ASK floor needs the owner, so it is not opened here.
    pre = act_rules.pre_judge(navigate_facts(target))
    if pre.kind == act_rules.REFUSE:
        raise NotPublic(pre.reason or "the Browser's rules refuse that address")
    if pre.kind == act_rules.ASK:
        raise NotPublic(NEEDS_OWNER)
    if pre.kind not in (act_rules.ALLOW, act_rules.CLASSIFY):
        raise NotPublic(NEEDS_OWNER)
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
    if not lookup:
        return target
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


def navigate_facts(url: str) -> act_rules.Facts:
    """A `navigate` to `url` from a new tab, as drive's `_facts` builds one for
    `act_rules.pre_judge`: every state fact (H3-H12) at its passing value,
    because opening is not driving (D6: `agents_may_drive` and the pane's
    drive switch govern clicks and typing, not this) and the budget is this
    service's own (`TURN_MAX`, `HOUR_MAX`)."""
    return act_rules.Facts(
        action="navigate", nav_url=url, url="about:blank", card_pending=False, paused=False,
        owner_has_input=False, owner_input_age_s=None, agents_may_drive=True, pane_can_drive=True,
        pane_can_see=True, private=False, tab_drawn=True, direct_mode=False, url_refused=False,
        other_driver=False, judged_this_turn=0, actions_this_hour=0, strict_profile=False, at_desk=True,
        depth=0, sub_agent=False)


def review_url(url: str, review=None) -> None:
    """Drive `navigate`'s reviewer for an address its fixed rules CLASSIFY
    (every ordinary one): `act_review.judge` and `act_review.decide`, exactly
    as drive calls them. Anything but `allow` (an ask, a deny, no model, a
    timeout) is `NotPublic(NEEDS_OWNER)`: this path never asks the owner."""
    facts = navigate_facts(url)
    pre = act_rules.pre_judge(facts)
    if pre.kind == act_rules.ALLOW:
        return
    if pre.kind != act_rules.CLASSIFY:
        raise NotPublic(NEEDS_OWNER)
    verdict = (review or act_review.judge)(facts, "")
    if act_review.decide(pre, verdict) != act_review.ALLOW:
        raise NotPublic(NEEDS_OWNER)


def _refused(n: int, refusal: NotPublic) -> str:
    # The fixed sentence only: the URL itself is never echoed.
    return f"Error: URL {n} was refused, so nothing was opened: {refusal}."


def _site(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


class BrowserOpens:
    def __init__(self, daemon, reports: PaneReports | None = None, *, resolve=_resolve, review=None,
                 clock=time.monotonic):
        self.daemon = daemon
        self.closed = True
        self.resolve = resolve
        # Drive navigate's reviewer (`act_review.judge` unless a test scripts one).
        self.review = review
        self.clock = clock
        self._budget_lock = threading.Lock()
        self._turns: collections.OrderedDict[tuple, int] = collections.OrderedDict()
        self._hour: collections.deque[float] = collections.deque()
        self._review_turns: collections.OrderedDict[tuple, int] = collections.OrderedDict()
        self._review_hour: collections.deque[float] = collections.deque()
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
        turn = (thread_id, caller["turn_id"])
        over = self._over_budget(turn, len(urls), charge=False)
        if over:
            return over
        refused = self._refused_ports()
        # 1. Every fixed check on every URL, in order, before any lookup or
        #    model call: a URL any rule refuses costs nothing and its siblings
        #    are never looked up or reviewed (delta review of #359).
        for n, url in enumerate(urls, 1):
            try:
                judge_public_url(url, refused=refused, resolve=self.resolve, lookup=False)
            except NotPublic as refusal:
                return _refused(n, refusal)
        # 2. The names, looked up in parallel (no model).
        judged = []
        with ThreadPoolExecutor(max_workers=len(urls), thread_name_prefix="browser-open-dns-judge") as pool:
            futures = [pool.submit(judge_public_url, url, refused=refused, resolve=self.resolve) for url in urls]
            for n, future in enumerate(futures, 1):
                try:
                    judged.append(future.result())
                except NotPublic as refusal:
                    for other in futures:
                        other.cancel()
                    return _refused(n, refusal)
        # 3. Drive navigate's reviewer, charged for every URL before any is
        #    sent, whether the call then opens anything or not.
        over = self._charge_reviews(turn, len(judged))
        if over:
            return over
        with ThreadPoolExecutor(max_workers=len(judged), thread_name_prefix="browser-open-review") as pool:
            futures = [pool.submit(review_url, target, self.review) for target in judged]
            for n, future in enumerate(futures, 1):
                try:
                    future.result()
                except NotPublic as refusal:
                    return _refused(n, refusal)
        # The turn may have ended while names were looked up.
        with self.daemon._lock:
            why_not, _ = refuse_desk_caller(self.daemon, self.closed, caller, NOT_AT_DESK, STOPPING)
            if why_not is not None:
                return why_not
        over = self._over_budget(turn, len(judged), charge=True)
        if over:
            return over
        return self._publish_and_wait(thread_id, judged)

    def _charge_reviews(self, turn: tuple, n: int) -> str | None:
        """Count `n` reviewer calls against this turn's `REVIEWS_TURN_MAX` and
        the daemon's rolling `REVIEWS_HOUR_MAX`, or a refusal (nothing counted)
        when they would pass either. Charged before the calls are made."""
        with self._budget_lock:
            now = self.clock()
            while self._review_hour and now - self._review_hour[0] >= HOUR_S:
                self._review_hour.popleft()
            used = self._review_turns.get(turn, 0)
            if used + n > REVIEWS_TURN_MAX or len(self._review_hour) + n > REVIEWS_HOUR_MAX:
                return ("Error: too many Browser pages have been checked this way recently, so "
                        "nothing was opened; open fewer, or try again later.")
            self._review_turns[turn] = used + n
            self._review_turns.move_to_end(turn)
            while len(self._review_turns) > TURNS_KEPT:
                self._review_turns.popitem(last=False)
            self._review_hour.extend([now] * n)
        return None

    def _over_budget(self, turn: tuple, n: int, *, charge: bool) -> str | None:
        """A refusal when `n` more pages would pass this turn's `TURN_MAX` or
        the daemon's rolling `HOUR_MAX`, else None (and, with `charge`, they
        are counted). Checked before judging and charged after, under one
        lock, so two calls cannot both pass on the same room."""
        with self._budget_lock:
            now = self.clock()
            while self._hour and now - self._hour[0] >= HOUR_S:
                self._hour.popleft()
            used = self._turns.get(turn, 0)
            if used + n > TURN_MAX:
                return (f"Error: one turn opens at most {TURN_MAX} Browser pages; this turn has "
                        f"already asked for {used}, so nothing more was opened.")
            if len(self._hour) + n > HOUR_MAX:
                return (f"Error: at most {HOUR_MAX} Browser pages open this way in an hour, and "
                        "that many have been, so nothing was opened; try again later.")
            if charge:
                self._turns[turn] = used + n
                self._turns.move_to_end(turn)
                while len(self._turns) > TURNS_KEPT:
                    self._turns.popitem(last=False)
                self._hour.extend([now] * n)
        return None

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
            return (f"Opened {placed} of {n} Browser pane(s) at {sites}, in Remote mode. You can "
                    "look at them, and drive them where the owner allows it, as with any Browser "
                    "pane.")
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
