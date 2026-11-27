"""One Windows browser tab behind Jarvis's SOCKS gate.

What enforces what:

- The SOCKS gate is the network boundary (home network, loopback, Jarvis's own
  ports). The address checks here refuse early and react to where the main
  frame lands; they are not a second wall.
- `location()` and `is_current()` are snapshots taken from outside the
  browser. A page can navigate between the moment the browser answers and the
  moment the caller acts, and no check made from here can close that gap.
  They are for choosing what to do and for refusing early. **Nothing secret
  may be released on their word alone:** a step that types a credential must
  also check the origin from inside the document, in the same turn in which
  it fills the field (an execution context belongs to one document, so a
  replaced document cannot receive the call).
"""

from contextlib import suppress
from queue import SimpleQueue
import secrets
import threading
import time
from typing import NamedTuple
from urllib.parse import urlsplit

from jarvis.v2.browser import policy
from jarvis.v2.browser.gate import Gate
from jarvis.v3.browser import signin
from jarvis.v3.browser.link import LinkError, windows_link
from jarvis.v3.logins import canonical_origin


_BLANK = "about:blank"
_CHECK_TIMEOUT = 5.0
_CHECK_RETRY = 2.0
_CHECK_MAX_AGE = 300.0
_GUARD_TIMEOUT = 5.0
_EVENT_POLL = 0.05
_CLOSED = "The browser session is closed."
_UNSETTLED = "The browser's current address could not be verified."
_NOT_A_SITE = "The browser is not showing a public website."
_MOVED = "The browser moved away from the sign-in document."
_READ_MOVED = "The browser moved away from the document being read."
_DEFAULT_READ = object()
_GATE_BYPASSED = "The browser did not use Jarvis's network gate."


class BrowserError(Exception):
    def __init__(self, message, *, reason):
        super().__init__(message)
        self.reason = reason


class Landing(NamedTuple):
    url: str
    outcome: str


class Location(NamedTuple):
    """A snapshot of the main frame's committed document. See the module note."""

    url: str
    frame_id: str
    loader_id: str


def _frame_location(frame):
    return Location(frame["url"] + frame.get("urlFragment", ""), frame["id"], frame["loaderId"])


def _read_origin(location: Location) -> str:
    # CDP reports Chromium's canonical URL. Drop everything outside the origin.
    parts = urlsplit(location.url)
    host, port = parts.hostname, parts.port
    if parts.scheme not in policy.SCHEMES or not host:
        raise BrowserError(_NOT_A_SITE, reason="not_a_site")
    if ":" in host:
        host = f"[{host}]"
    default_port = 443 if parts.scheme == "https" else 80
    suffix = "" if port in (None, default_port) else f":{port}"
    return f"{parts.scheme}://{host}{suffix}"


def _context_lost(message):
    message = (message or "").lower()
    return "context" in message and any(word in message for word in
                                        ("destroyed", "cannot find", "not found", "no longer"))


def _judge(url, refused, *, landed=False):
    # The v2 judge trims surrounding whitespace. Reject controls even there.
    if isinstance(url, str) and any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise policy.URLRefused("malformed", policy.CONTROL_SENTENCE)
    address = policy.judge_url(url, refused=refused, landed=landed)
    network_address = address[5:] if landed and address.lower().startswith("blob:") else address
    try:
        parts = urlsplit(network_address)
    except ValueError:
        raise policy.URLRefused("malformed", policy.MALFORMED_SENTENCE) from None
    if parts.scheme not in policy.SCHEMES:
        if landed and network_address == address:
            return address
        raise policy.URLRefused("scheme", "Only http and https addresses open in this browser.")
    port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
    reason = policy.judge_port(port, refused=refused)
    if reason == policy.JARVIS_PORT:
        raise policy.URLRefused(reason, policy.JARVIS_PORT_SENTENCE)
    if reason:
        raise policy.URLRefused(reason, policy.MALFORMED_SENTENCE)
    kind, literal = policy.host_kind(parts.hostname)
    if kind == "loopback_name" or (kind == "literal" and
                                  policy.judge_address(literal, via=kind, lan=False)):
        raise policy.URLRefused("network", "That address is outside the public network.")
    return address


class _Tab:
    """State for one link. Its reader records events and queues CDP work."""

    def __init__(self, link, refused_ports):
        self.link = link
        self.refused_ports = refused_ports
        self.changed = threading.Condition()
        self.targets = threading.Lock()
        self.close_lock = threading.Lock()
        self.actions = SimpleQueue()
        self.owned_targets = set()
        self.popups_closed = 0
        self.stopping = False
        self.link_closed = False
        self.main_target = None
        self.session = None
        self.frame = None
        self.url = ""
        self.loader = None
        self.generation = 0
        self.commit_generation = 0
        self.unreachable = False
        # Empty means a start was reported without a loader to match.
        self.started_loader = None
        self.loading = False
        self.stopped_loader = None
        self.command_navigation = None
        self.current_refused = False
        self.refused = False
        self.blanking = False
        self.loads = None
        self.same_document = False
        self.worker = threading.Thread(target=self._work, name="jarvis-browser-session",
                                       daemon=True)

    def setup(self):
        self.link.subscribe(self._event)
        with self.targets:
            self.link.call("Target.setDiscoverTargets", {"discover": True})
            self.main_target = self._create_target(_BLANK)
        self.worker.start()
        attached = self.link.call("Target.attachToTarget",
                                  {"targetId": self.main_target, "flatten": True})
        self.session = attached["sessionId"]
        self.call("Page.enable")
        self.call("Network.enable")
        self.link.call("Browser.setDownloadBehavior", {"behavior": "deny"})
        self.call("Page.setLifecycleEventsEnabled", {"enabled": True})
        with self.changed:
            generation = self.generation
        frame = self.call("Page.getFrameTree")["frameTree"]["frame"]
        with self.changed:
            if generation == self.generation:
                self._frame_landed(frame)

    def call(self, method, params=None, **options):
        return self.link.call(method, params, session=self.session, **options)

    def create_probe(self, url):
        with self.targets:
            return self._create_target(url, background=True, timeout=_CHECK_TIMEOUT)

    def _create_target(self, url, *, timeout=30.0, **options):
        target = self.link.call("Target.createTarget", {"url": url, **options},
                                timeout=timeout)["targetId"]
        self.owned_targets.add(target)
        return target

    def close_probe(self, target):
        with self.targets:
            self._close_target(target)

    def _close_target(self, target):
        result = self.link.call("Target.closeTarget", {"targetId": target},
                                timeout=_GUARD_TIMEOUT)
        if result.get("success") is not True:
            raise LinkError("The browser did not close the tab.")

    def _frame_landed(self, frame):
        self.frame = frame["id"]
        self.loader = frame.get("loaderId")
        self.unreachable = bool(frame.get("unreachableUrl"))
        self._landed(frame["url"] + frame.get("urlFragment", ""), same_document=False)

    def _landed(self, url, *, same_document):
        if self.started_loader in (None, "", self.loader):
            self.started_loader = None
            if not same_document:
                self.loading = False
        self.generation += 1
        if not same_document:
            self.commit_generation = self.generation
        self.url = url
        self.same_document = same_document
        if self.command_navigation is not None:
            frame, loader, address = self.command_navigation
            if frame is None or self._matches_navigation(
                    frame, loader, address, document_committed=not same_document):
                self.command_navigation = None
        self.current_refused = False
        if url:
            try:
                _judge(url, self.refused_ports(), landed=True)
            except policy.URLRefused:
                self.current_refused = True
                self.refused = True
                if not self.blanking:
                    self.blanking = True
                    self.actions.put(("blank", None))
        self.changed.notify_all()

    def _matches_navigation(self, frame, loader, url, *, document_committed):
        if frame != self.frame:
            return False
        if loader:
            return document_committed and loader == self.loader
        return self.same_document and url == self.url

    def _event(self, message):
        method = message.get("method")
        params = message.get("params", {})
        with self.changed:
            if self.stopping:
                return
            if method == "Target.targetCreated" and "sessionId" not in message:
                info = params["targetInfo"]
                if info["type"] == "page":
                    self.actions.put(("popup", info["targetId"]))
            elif method in ("Target.targetDestroyed", "Target.targetCrashed"):
                target = params["targetId"]
                if target == self.main_target:
                    self._stop_events()
                elif method == "Target.targetDestroyed":
                    self.actions.put(("destroyed", target))
            elif (method == "Target.detachedFromTarget" and self.session is not None
                  and params.get("sessionId") == self.session):
                self._stop_events()
            elif self.session is not None and message.get("sessionId") == self.session:
                if method == "Page.javascriptDialogOpening":
                    self.actions.put(("dialog", None))
                elif method == "Page.frameNavigated" and "parentId" not in params["frame"]:
                    self._frame_landed(params["frame"])
                elif method == "Page.navigatedWithinDocument" and params["frameId"] == self.frame:
                    self._landed(params["url"], same_document=True)
                elif params.get("frameId") == self.frame and self.frame is not None:
                    self._frame_event(method, params)

    def _frame_event(self, method, params):
        if method == "Page.frameStartedNavigating":
            self.started_loader = params.get("loaderId") or ""
        elif method == "Page.frameRequestedNavigation":
            self.started_loader = ""
        elif method == "Page.frameStartedLoading":
            self.loading = True
        elif method == "Page.frameStoppedLoading":
            if self.loading or self.started_loader == "":
                self.stopped_loader = self.started_loader
                if (self.started_loader and self.command_navigation is not None
                        and self.command_navigation[:2] == (self.frame, self.started_loader)):
                    self.command_navigation = None
                self.started_loader = None
            self.loading = False
        elif method == "Page.lifecycleEvent" and self.loads is not None and params["name"] == "load":
            self.loads.add(params["loaderId"])
            self.changed.notify_all()
            return
        else:
            return
        self.generation += 1
        self.changed.notify_all()

    def _blank(self):
        # Keep location unsettled until Chromium reports a replacement landing.
        # If another refused landing arrived during the command, check the latest.
        deadline = time.monotonic() + _GUARD_TIMEOUT
        while True:
            with self.changed:
                generation = self.generation
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LinkError("The browser did not clear the tab.")
            result = self.call("Page.navigate", {"url": _BLANK}, timeout=remaining)
            if result.get("errorText") or result.get("isDownload"):
                raise LinkError("The browser did not clear the tab.")
            with self.changed:
                while self.generation == generation and self.alive:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise LinkError("The browser did not clear the tab.")
                    self.changed.wait(min(remaining, _EVENT_POLL))
                if self.stopping:
                    return
                if self.current_refused:
                    continue
                self.blanking = False
                self.changed.notify_all()
                return

    def _work(self):
        while (action := self.actions.get()) is not None:
            with self.changed:
                if self.stopping:
                    break
            kind, target = action
            try:
                if kind == "destroyed":
                    with self.targets:
                        self.owned_targets.discard(target)
                elif kind == "popup":
                    # targetCreated can precede the createTarget reply.
                    with self.targets:
                        if target not in self.owned_targets:
                            self._close_popup(target)
                elif kind == "dialog":
                    self.call("Page.handleJavaScriptDialog", {"accept": False},
                              timeout=_GUARD_TIMEOUT)
                else:
                    self._blank()
            except Exception:
                # Guard failure makes this tab unusable. Close the browser without
                # reporting raw CDP errors (which may contain page data).
                with self.changed:
                    self._stop_events()
                break
        with suppress(Exception):
            self._close_link()

    def _close_popup(self, target):
        try:
            self._close_target(target)
        except LinkError:
            # A pop-up can close itself while its discovery event is queued.
            targets = self.link.call("Target.getTargets", timeout=_GUARD_TIMEOUT)["targetInfos"]
            if any(info["targetId"] == target for info in targets):
                raise
        else:
            self.popups_closed += 1

    def _stop_events(self):
        self.stopping = True
        self.actions.put(None)
        self.changed.notify_all()

    def _close_link(self):
        with self.close_lock:
            if not self.link_closed:
                self.link.close()
                self.link_closed = True

    def close(self):
        with self.changed:
            self._stop_events()
        try:
            self._close_link()
        finally:
            if self.worker.ident is not None:
                self.worker.join()

    @property
    def alive(self):
        return not self.stopping and self.link.alive

    def require_alive(self):
        if not self.alive:
            raise BrowserError(_CLOSED, reason="closed")

    def take_refusal(self):
        """With changed held, let the worker finish before consuming a refusal."""
        if not self.refused:
            return None
        while self.blanking:
            self.require_alive()
            self.changed.wait(_EVENT_POLL)
        self.require_alive()
        self.refused = False
        return Landing(self.url, "refused")

    def open(self, url, timeout):
        deadline = time.monotonic() + timeout
        with self.changed:
            if (refusal := self.take_refusal()) is not None:
                return refusal
            self.loads = set()
            self.same_document = False
            self.stopped_loader = None
            generation = self.commit_generation
            self.command_navigation = (None, None, None)
        try:
            uncertain = False
            try:
                reply = self.call("Page.navigate", {"url": url},
                                  timeout=max(0, deadline - time.monotonic()))
            except LinkError as exc:
                # Losing the reply does not establish that Chromium rejected the call.
                uncertain = exc.browser_message is None
                reply = {"errorText": True} if time.monotonic() < deadline else {}
            with self.changed:
                frame, loader = reply.get("frameId"), reply.get("loaderId")
                failed = reply.get("errorText") or reply.get("isDownload")
                resolved = self._matches_navigation(
                    frame, loader, url, document_committed=self.commit_generation != generation)
                if (failed and not uncertain) or resolved or (loader and loader == self.stopped_loader):
                    self.command_navigation = None
                elif frame:
                    self.command_navigation = (frame, loader, url)
                if failed and loader and loader == self.started_loader:
                    self.started_loader = None
                    self.loading = False
                while True:
                    self.require_alive()
                    if (refusal := self.take_refusal()) is not None:
                        return refusal
                    if failed or (loader and loader == self.loader and self.unreachable):
                        return Landing("", "failed")
                    loaded = loader and loader == self.loader and loader in self.loads
                    within_document = (not loader and reply.get("frameId") == self.frame
                                       and self.same_document and self.url == url)
                    if loaded or within_document:
                        return Landing(self.url, "loaded")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return Landing(self.url, "timeout")
                    self.changed.wait(min(remaining, _EVENT_POLL))
        finally:
            with self.changed:
                self.loads = None

    def _navigation_pending(self):
        # A main-frame request, navigation start or loading start is provisional
        # until commit or cancellation. Commit consumes the pre-commit loading
        # signal, so subresources that keep loading afterward do not block location.
        # An outstanding Page.navigate also needs its own commit or failure.
        return (self.blanking or self.started_loader is not None or self.loading
                or self.command_navigation is not None)

    def location(self, timeout, *, wait=True):
        deadline = time.monotonic() + timeout
        while True:
            with self.changed:
                self.require_alive()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BrowserError(_UNSETTLED, reason="unsettled")
                pending = self._navigation_pending()
                if pending and wait:
                    self.changed.wait(min(remaining, _EVENT_POLL))
                    continue
                generation = self.generation
                observed = Location(self.url, self.frame, self.loader)
                unreachable = self.unreachable
            try:
                frame = self.call("Page.getFrameTree", timeout=remaining)["frameTree"]["frame"]
                current = _frame_location(frame)
                agrees = current == observed and all(isinstance(part, str) and part for part in current)
                site = False
                if agrees and not (unreachable or frame.get("unreachableUrl")):
                    address = _judge(current.url, self.refused_ports(), landed=True)
                    site = urlsplit(address).scheme in policy.SCHEMES
            except policy.URLRefused:
                site = False
            except (LinkError, KeyError, TypeError, ValueError):
                agrees = False
            with self.changed:
                self.require_alive()
                if generation != self.generation and wait:
                    continue
                if (not agrees or pending or self._navigation_pending() or
                        generation != self.generation or time.monotonic() >= deadline):
                    raise BrowserError(_UNSETTLED, reason="unsettled")
                if not site:
                    raise BrowserError(_NOT_A_SITE, reason="not_a_site")
                return current


class BrowserSession:
    def __init__(self, browser, *, window=False, link_factory=windows_link, gate_factory=Gate,
                 extra_refused_ports=frozenset(), clock=time.monotonic, load_timeout=30.0,
                 settle_timeout=5.0):
        self._browser = browser
        self._window = window
        self._link_factory = link_factory
        self._gate_factory = gate_factory
        self._extra_ports = frozenset(extra_refused_ports)
        self._clock = clock
        self._load_timeout = load_timeout
        self._settle_timeout = settle_timeout
        self._lock = threading.RLock()
        self._gate = None
        self._gate_port = None
        self._tab = None
        self._checked_at = None

    def _refused_ports(self):
        # policy.refused_ports already includes the configured and default v3
        # API, remote phone and voice link ports through config.control_ports.
        ports = policy.refused_ports() | self._extra_ports
        return ports if self._gate_port is None else ports | {self._gate_port}

    @property
    def alive(self):
        tab = self._tab
        return self._checked_at is not None and tab is not None and tab.alive

    def start(self):
        with self._lock:
            if self.alive:
                return
            try:
                self._close()
                self._gate = self._gate_factory(refused=self._refused_ports)
                port = self._gate.start()
                self._gate_port = port
                link = self._link_factory(self._browser, proxy_port=port, window=self._window)
                self._tab = _Tab(link, self._refused_ports)
                link.start()
                self._tab.setup()
                self._check_gate()
            except BaseException as exc:
                self._close_quietly()
                if not isinstance(exc, Exception) or isinstance(exc, BrowserError):
                    raise
                raise BrowserError("The browser session could not start.", reason="start_failed") from None

    def _check_gate(self):
        for attempt in range(3):
            if attempt:
                time.sleep(_CHECK_RETRY)
            if not self._tab.alive:
                break
            name = f"{secrets.token_hex(16)}.gate-check.invalid"
            target = None
            try:
                seen = self._gate.expect_check(name)
                deadline = time.monotonic() + _CHECK_TIMEOUT
                target = self._tab.create_probe(f"http://{name}/")
                passed = seen.wait(max(0, deadline - time.monotonic()))
            except LinkError:
                # A lost createTarget reply leaves ownership unknown.
                break
            finally:
                self._gate.forget_check(name)
                if target is not None:
                    self._tab.close_probe(target)
            if passed and self._tab.alive:
                self._checked_at = self._clock()
                return
        raise BrowserError(_GATE_BYPASSED, reason="gate_bypassed")

    def _live_tab(self):
        if not self.alive:
            raise BrowserError(_CLOSED, reason="closed")
        return self._tab

    def open(self, url):
        with self._lock:
            tab = self._live_tab()
            try:
                address = _judge(url, self._refused_ports())
            except policy.URLRefused as exc:
                raise BrowserError(str(exc), reason="refused") from None
            if self._clock() - self._checked_at > _CHECK_MAX_AGE:
                try:
                    self._check_gate()
                except BaseException as exc:
                    self._close_quietly()
                    if not isinstance(exc, Exception):
                        raise
                    raise BrowserError(_GATE_BYPASSED, reason="gate_bypassed") from None
            return tab.open(address, self._load_timeout)

    def location(self, *, timeout=None) -> Location:
        """The committed http(s) main-frame document, as a snapshot (module note)."""
        with self._lock:
            budget = self._settle_timeout if timeout is None else min(timeout, self._settle_timeout)
            return self._live_tab().location(budget)

    def document_call(self, origin, source, arguments, *, timeout=10.0):
        """Run an approved sign-in script in a fresh world of the main document."""
        try:
            function = signin.document_function(source)
            if arguments is not None and (
                type(arguments) is not list or any(type(value) is not str for value in arguments)
            ):
                raise TypeError("Sign-in script arguments must be a list of strings or None.")
            with self._lock:
                deadline = time.monotonic() + timeout
                location = self.location(timeout=timeout)
                if not origin or canonical_origin(location.url) != origin:
                    raise BrowserError(_MOVED, reason="moved")
                tab = self._live_tab()
                reason = "unsettled"
                try:
                    return self._call_in_world(tab, location, origin, function, arguments, deadline)
                except BrowserError as exc:
                    reason = exc.reason
                except LinkError as exc:
                    if _context_lost(exc.browser_message):
                        reason = "moved"
                except (KeyError, TypeError, ValueError):
                    pass
        finally:
            del arguments
        # Leave the protocol error and its request-data traceback behind.
        raise BrowserError(_MOVED if reason == "moved" else _UNSETTLED, reason=reason)

    def read_call(self, location: Location, script=_DEFAULT_READ, *, timeout=10.0):
        """Read a verified main-frame snapshot with the built-in read script."""
        from jarvis.v3.browser import read

        function = read.document_function(read.READ if script is _DEFAULT_READ else script)
        with self._lock:
            deadline = time.monotonic() + timeout
            if self.location(timeout=timeout) != location:
                raise BrowserError(_READ_MOVED, reason="moved")
            tab = self._live_tab()
            origin = _read_origin(location)
            reason = "unsettled"
            try:
                value = self._call_in_world(tab, location, origin, function, None, deadline,
                                            purpose="read")
                if value != "moved":
                    return value
                reason = "moved"
            except BrowserError as exc:
                reason = exc.reason
            except LinkError as exc:
                if _context_lost(exc.browser_message):
                    reason = "moved"
            except (KeyError, TypeError, ValueError):
                pass
        message = {"closed": _CLOSED, "moved": _READ_MOVED}.get(reason, _UNSETTLED)
        raise BrowserError(message, reason=reason)

    @staticmethod
    def _call_in_world(tab, location, origin, function, arguments, deadline, *, purpose="signin"):
        def remaining():
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise BrowserError(_UNSETTLED, reason="unsettled")
            return budget

        world = tab.call("Page.createIsolatedWorld", {
            "frameId": location.frame_id, "worldName": f"jarvis-{purpose}-{secrets.token_hex(16)}",
        }, timeout=remaining())
        context = world["executionContextId"]
        if type(context) is not int or context <= 0:
            raise BrowserError(_UNSETTLED, reason="unsettled")
        with tab.changed:
            if (tab.frame, tab.loader) != (location.frame_id, location.loader_id):
                raise BrowserError(_MOVED, reason="moved")
            if purpose == "read":
                tab.require_alive()
                if tab.url != location.url or tab._navigation_pending():
                    raise BrowserError(_READ_MOVED, reason="moved")
        reply = tab.call("Runtime.callFunctionOn", {
            "executionContextId": context,
            "functionDeclaration": function,
            "arguments": [{"value": value} for value in (origin, *(arguments or ()))],
            "returnByValue": True,
        }, timeout=remaining())
        if "exceptionDetails" in reply:
            raise BrowserError(_UNSETTLED, reason="unsettled")
        return reply["result"]["value"]

    def is_current(self, location: Location) -> bool:
        """Whether a fresh snapshot still shows that document. A hint, not a guarantee."""
        with self._lock:
            try:
                return self._live_tab().location(self._settle_timeout, wait=False) == location
            except BrowserError as exc:
                if exc.reason == "closed":
                    raise
                return False

    def _close_quietly(self):
        with suppress(Exception):
            self._close()

    def _close(self):
        tab, gate = self._tab, self._gate
        self._checked_at = self._tab = self._gate = self._gate_port = None
        try:
            if tab is not None:
                tab.close()
        finally:
            if gate is not None:
                gate.stop()

    def close(self):
        with self._lock:
            try:
                self._close()
            except Exception:
                raise BrowserError("The browser session could not close.", reason="close_failed") from None
