"""Private owner HTTP authentication and supervised navigation.

Only exact CDP paused-request provenance admits a prompt. Secrets live solely
in the transient owner socket reply and one continueWithAuth call.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
import secrets
import threading
import time
from . import auth_state as bounds

@dataclass(eq=False)
class Navigation:
    id: str
    tab: object
    page: object
    instance: object
    clock: object
    ready: object = field(default_factory=asyncio.Event)
    task: object = None
    result: dict | None = None
    refused: bool = False
    restart: object = None

@dataclass(eq=False)
class Request:
    origin: object
    network: str | None = None

@dataclass(eq=False)
class Prompt:
    request: str
    challenge: object
    session: object
    document: int
    token: object
    key: str
    record: object
    nonce: str | None = None
    sock: object = None
    generation: int = -1
    hold: int = -1
    timer: object = None
    answering: bool = False
    epoch: int = -1

@dataclass(eq=False)
class Session:
    tab: object
    page: object
    instance: object
    cdp: object
    requests: dict = field(default_factory=dict)
    networks: dict = field(default_factory=dict)
    budget: object = field(default_factory=bounds.AuthBudget)
    prompt: Prompt | None = None
    navigation: Navigation | None = None
    tasks: set = field(default_factory=set)
    closed: bool = False
    restart: object = None
    epoch: int = 0
    retiring: object = None

class OwnerAuth:
    def __init__(self, host, *, clock=time.monotonic):
        self.host, self.clock = host, clock
        self.sessions = {}
        self._pending = set()
        self._installing = {}
        self._install_tasks = set()
        self._cleanup = set()
        self._answer_lock = threading.Lock()
        self._answers = {}
        self._answer_tasks = set()
        self._closing = False
        self.navigation_timeout = 20.0

    def current(self, session):
        host, tab = self.host, session.tab
        return (not session.closed and not host._stopped
                and host._tabs.get(tab.id) is tab and tab.page is session.page
                and host._instances.get(tab.profile) is session.instance
                and not session.instance.closing and tab.cdp is session.cdp)

    def input_epoch(self, tab):
        session = self.sessions.get(tab.id)
        return (session, session.epoch) if session is not None and session.tab is tab else (None, -1)

    def input_current(self, tab, pinned):
        return self.input_epoch(tab) == pinned and not self.blocked(tab)

    def blocked(self, tab):
        session = self.sessions.get(tab.id)
        return bool(session and session.tab is tab and session.prompt)

    async def install(self, tab):
        pending = self._installing.get(tab.id)
        if pending is not None and pending[0] is tab:
            session = await asyncio.shield(pending[1])
            if session.tab is not tab or not self.current(session):
                raise RuntimeError("authentication session changed")
            return session
        if len(self._install_tasks) >= bounds.REQUESTS_MAX:
            raise RuntimeError("authentication setup is busy")
        task = asyncio.create_task(self._install(tab))
        entry = (tab, task)
        self._installing[tab.id] = entry
        self._install_tasks.add(task)
        def done(finished):
            self._install_tasks.discard(finished)
            if self._installing.get(tab.id) is entry:
                self._installing.pop(tab.id, None)
            if not finished.cancelled():
                finished.exception()
        task.add_done_callback(done)
        return await asyncio.shield(task)

    async def _install(self, tab):
        old = self.sessions.get(tab.id)
        if old is not None:
            if self.current(old):
                if old.tab is tab:
                    return old
                raise RuntimeError("authentication session changed")
            self.release(old.tab)
        page = tab.page
        instance = self.host._instances.get(tab.profile)
        if self.host._tabs.get(tab.id) is not tab or instance is None:
            raise RuntimeError("authentication session changed")
        cdp = await self.host._ensure_cdp(tab)
        if (cdp is None or self.host._tabs.get(tab.id) is not tab or tab.page is not page
                or self.host._instances.get(tab.profile) is not instance or instance.closing):
            raise RuntimeError("authentication session changed")
        session = Session(tab, page, instance, cdp)
        self.sessions[tab.id] = session
        cdp.on("Fetch.requestPaused", lambda raw: self._schedule(session, self._request(session, self._event(raw))))
        cdp.on("Fetch.authRequired", lambda raw: self._schedule(session, self._challenge(session, self._event(raw))))
        cdp.on("Network.loadingFinished", lambda raw: self._completed(session, raw))
        cdp.on("Network.loadingFailed", lambda raw: self._completed(session, raw))
        try:
            await cdp.send("Network.enable")
            if not self.current(session):
                raise RuntimeError("authentication session changed")
            await cdp.send("Fetch.enable", {"handleAuthRequests": True})
            if not self.current(session):
                raise RuntimeError("authentication session changed")
        except BaseException:
            session.closed = True
            if self.sessions.get(tab.id) is session:
                self.sessions.pop(tab.id, None)
            await self._send(session, "Fetch.disable", {})
            raise
        return session

    def _schedule(self, session, coroutine):
        # CDP events never create an unbounded queue of tasks or retained URLs.
        if len(session.tasks) >= bounds.REQUESTS_MAX:
            coroutine.close()
            self._retire(session)
            return
        async def run():
            try:
                if not self.current(session):
                    return await coroutine
                if session.restart is not None:
                    # Controlled Sound restoration already owns the exclusive
                    # restart token. It may continue ordinary traffic, but an
                    # auth challenge is cancelled rather than asking/reopening
                    # admission before the replacement is fully restored.
                    if not self.host.activity.restart_current(session.restart,
                            session.tab.profile, session.instance.generation):
                        coroutine.close()
                        return
                    return await coroutine
                return await self.host._activity_run(session.tab, lambda: coroutine)
            except Exception:
                coroutine.close()
                self._retire(session)
        task = asyncio.create_task(run())
        session.tasks.add(task)
        def done(finished):
            session.tasks.discard(finished)
            if not finished.cancelled():
                finished.exception()
        task.add_done_callback(done)

    def _retire(self, session):
        if session.retiring is not None or not self.current(session):
            return
        async def close_exact():
            await self.cancel_navigation(session.tab)
            async with self.host._launching.setdefault(session.tab.profile, asyncio.Lock()):
                if not self.current(session):
                    return
                try:
                    await asyncio.wait_for(session.page.close(), 5.0)
                finally:
                    self.host._dropped(session.tab)
        session.retiring = asyncio.create_task(close_exact())
        self._cleanup.add(session.retiring)
        def done(task):
            self._cleanup.discard(task)
            if not task.cancelled():
                task.exception()
        session.retiring.add_done_callback(done)

    async def _send(self, session, method, params, *, current=None):
        async def send_once():
            if current is not None and not current():
                return False
            await session.cdp.send(method, params)
            return True
        try:
            return await asyncio.wait_for(send_once(), 2.0)
        except Exception:
            return False

    @staticmethod
    def _event(raw):
        """Retain only bounded provenance, never request headers or auth data."""
        if not isinstance(raw, dict):
            return {}
        def text(value, cap):
            return value if isinstance(value, str) and len(value) <= cap else None
        request = raw.get("request")
        challenge = raw.get("authChallenge")
        result = {"requestId": text(raw.get("requestId"), 256),
                  "networkId": text(raw.get("networkId"), 256),
                  "request": {"url": text(request.get("url"), 2048)} if isinstance(request, dict) else {}}
        if isinstance(challenge, dict):
            result["authChallenge"] = {"source": text(challenge.get("source"), 16),
                "scheme": text(challenge.get("scheme"), 16),
                "realm": text(challenge.get("realm"), 4096),
                "origin": text(challenge.get("origin"), 2048)}
        return result

    @staticmethod
    def _request_id(raw):
        value = raw.get("requestId") if isinstance(raw, dict) else None
        return value if isinstance(value, str) and 0 < len(value) <= 256 and value.isascii() and not any(ord(c)<33 or ord(c)==127 for c in value) else None

    def _completed(self, session, raw):
        request = self._request_id(raw)
        fetch = session.networks.pop(request, None)
        if fetch is not None:
            session.requests.pop(fetch, None)

    async def _request(self, session, raw):
        request = self._request_id(raw)
        if request is None:
            self._retire(session)  # an unaddressable pause must not hang forever
            return
        if not self.current(session):
            await self._send(session, "Fetch.failRequest", {"requestId": request, "errorReason": "Aborted"})
            return
        try:
            origin = bounds.origin((raw.get("request") or {}).get("url"), refused=self.host.refused_ports())
        except (bounds.AuthRefused, AttributeError, TypeError):
            await self._send(session, "Fetch.failRequest", {"requestId": request, "errorReason": "Aborted"})
            return
        if request not in session.requests and len(session.requests) >= bounds.REQUESTS_MAX:
            await self._send(session, "Fetch.failRequest", {"requestId": request, "errorReason": "Aborted"})
            return
        network = raw.get("networkId")
        if isinstance(network, str) and 0 < len(network) <= 256 and network.isascii():
            session.networks[network] = request
        prior = session.requests.get(request)
        if prior is not None and prior.network and prior.network != network:
            session.networks.pop(prior.network, None)
        prompt = session.prompt
        if prompt is not None and prompt.request == request:
            await self._finish(prompt, "changed", cancel=True)
        session.requests[request] = Request(origin, network if isinstance(network, str) else None)
        await self._send(session, "Fetch.continueRequest", {"requestId": request})

    async def _deny_request(self, session, request):
        await self._send(session, "Fetch.continueWithAuth", {
            "requestId": request, "authChallengeResponse": {"response": "CancelAuth"}})

    async def _challenge(self, session, raw):
        request = self._request_id(raw)
        if request is None:
            return
        now = self.clock()
        try:
            if (not self.current(session) or session.restart is not None
                    or request not in session.requests):
                raise bounds.AuthRefused("authentication refused")
            record = session.requests[request]
            challenge = bounds.challenge(record.origin, raw.get("authChallenge"),
                                         refused=self.host.refused_ports())
            # AuthRequired carries its own request as well: no redirect borrowing.
            if bounds.origin((raw.get("request") or {}).get("url"), refused=self.host.refused_ports()) != challenge.origin:
                raise bounds.AuthRefused("authentication refused")
            if session.prompt is not None:
                # Duplicate notification must not leave a live prompt for an
                # already cancelled CDP request. Other subresources simply deny.
                if session.prompt.request == request:
                    await self._finish(session.prompt, "changed", cancel=True)
                    return
                raise bounds.AuthRefused("authentication refused")
            if len(self._pending) >= bounds.PROMPTS_MAX:
                raise bounds.AuthRefused("authentication refused")
            if not session.budget.announce(now):
                raise bounds.AuthRefused("authentication refused")
            key = "auth:" + secrets.token_hex(16)
            token = self.host.page_hold(session.tab, key)
            if token is None:
                raise bounds.AuthRefused("authentication refused")
        except (bounds.AuthRefused, AttributeError, TypeError):
            if session.navigation is not None:
                session.navigation.refused = True
            await self._deny_request(session, request)
            if self.current(session) and session.tab.stream is not None and session.restart is None:
                session.tab.stream.send({"t": "error", "reason": "Authentication refused. Reload to retry."})
            return
        prompt = Prompt(request, challenge, session, session.tab.document_generation, token, key, record)
        session.prompt = prompt
        session.epoch += 1
        if session.tab.stream is not None:
            session.tab.stream.hold_through(notify=False)
        self._pending.add(prompt)
        if session.navigation is not None:
            session.navigation.clock.set_paused(True, now)
            session.navigation.ready.set()
        self.host._publish_pane_tabs()
        prompt.timer = asyncio.create_task(self._expire(prompt))
        self._publish(prompt)

    def _publish(self, prompt):
        session, host = prompt.session, self.host
        tab, stream = session.tab, session.tab.stream
        if not self.current(session) or session.prompt is not prompt or prompt.answering or stream is None:
            return
        state = stream.owner_state()
        remaining = session.budget.remaining(self.clock())
        if (remaining <= 0 or host.input_held() or not state["shown"] or not state["visible"]
                or prompt.document != tab.document_generation):
            prompt.nonce = None
            prompt.sock = None
            return
        sock = stream.attached()
        if sock is None:
            prompt.nonce = None
            prompt.sock = None
            return
        hold = host.capture_hold_epoch()
        if (prompt.nonce is not None and prompt.sock is sock
                and prompt.generation == state["attachment_generation"]
                and prompt.hold == hold and prompt.epoch == session.epoch):
            return  # redundant visible/attach notifications never discard typing
        prompt.nonce = secrets.token_hex(16)
        prompt.sock, prompt.generation = sock, state["attachment_generation"]
        prompt.hold = host.capture_hold_epoch()
        prompt.epoch = session.epoch
        stream.send_to(sock, {"t": "auth", "id": prompt.nonce,
            "origin": prompt.challenge.origin.display, "realm": prompt.challenge.realm,
            "scheme": prompt.challenge.scheme, "expires_in": remaining})

    def refresh(self, tab):
        session = self.sessions.get(tab.id)
        if session is not None and session.tab is tab and session.prompt is not None:
            self._publish(session.prompt)

    def document_changed(self, tab):
        session = self.sessions.get(tab.id)
        if session and session.tab is tab and session.prompt:
            prompt = session.prompt
            old_nonce, old_sock = prompt.nonce, prompt.sock
            self.invalidate(tab)
            if old_nonce and old_sock and tab.stream:
                tab.stream.send_to(old_sock, {"t": "auth_closed", "id": old_nonce, "reason": "changed"})
            self._schedule(session, self._finish(prompt, "changed", cancel=True))

    def invalidate(self, tab):
        session = self.sessions.get(tab.id)
        if session and session.tab is tab:
            session.epoch += 1
            if session.prompt:
                session.prompt.nonce = None
                session.prompt.sock = None

    async def _expire(self, prompt):
        try:
            await asyncio.sleep(prompt.session.budget.remaining(self.clock()))
            await self._finish(prompt, "expired", cancel=True)
        except asyncio.CancelledError:
            pass

    async def _finish(self, prompt, reason, *, cancel=False):
        session = prompt.session
        if session.prompt is not prompt:
            return
        session.prompt = None
        if cancel and session.navigation is not None:
            session.navigation.refused = True
        self._pending.discard(prompt)
        self.host._publish_pane_tabs()
        nonce, sock = prompt.nonce, prompt.sock
        prompt.nonce = None
        prompt.sock = None
        if prompt.timer is not None and prompt.timer is not asyncio.current_task():
            prompt.timer.cancel()
        try:
            if cancel:
                await self._deny_request(session, prompt.request)
        finally:
            if session.navigation is not None:
                session.navigation.clock.set_paused(False, self.clock())
            if nonce and sock and session.tab.stream is not None:
                session.tab.stream.send_to(sock, {"t": "auth_closed", "id": nonce, "reason": reason})
            self.host.release_page_hold(session.tab, prompt.key, prompt.token)

    def receive(self, tab, sock, generation, hold, fields):
        """Reader-thread admission: at most one queued owner reply per tab.

        Reserve before scheduling on the host loop, so a flood cannot retain
        credential dictionaries in unbounded Futures/coroutines. Rejected and
        never-started replies clear fields synchronously or in exact completion.
        """
        token = object()
        with self._answer_lock:
            if self._closing or tab.id in self._answers:
                fields.clear()
                return False
            self._answers[tab.id] = token
        def release():
            fields.clear()
            with self._answer_lock:
                if self._answers.get(tab.id) is token:
                    self._answers.pop(tab.id, None)
        def begin():
            if self._closing:
                release()
                return
            task = asyncio.create_task(self.answer(tab, sock, generation, hold, fields))
            self._answer_tasks.add(task)
            def done(finished):
                self._answer_tasks.discard(finished)
                release()
                if not finished.cancelled():
                    finished.exception()
            task.add_done_callback(done)
        loop = getattr(self.host, "_loop", None)
        try:
            if loop is None or loop.is_closed():
                release()
                return False
            loop.call_soon_threadsafe(begin)
            return True
        except RuntimeError:
            release()
            return False

    async def answer(self, tab, sock, generation, hold, fields):
        try:
            await self._answer(tab, sock, generation, hold, fields)
        finally:
            fields.clear()

    def _reply_current(self, session, prompt, sock, generation, hold):
        tab = session.tab
        state = tab.stream.owner_state() if tab.stream is not None else {}
        return bool(self.current(session) and not getattr(tab, "retired", False)
                    and prompt.sock is sock and tab.stream.attached() is sock
                    and prompt.generation == generation
                    and state.get("attachment_generation") == generation
                    and prompt.hold == hold and self.host.capture_hold_epoch() == hold
                    and prompt.epoch == session.epoch
                    and not self.host.input_held() and state.get("shown") and state.get("visible")
                    and prompt.document == tab.document_generation
                    and session.requests.get(prompt.request) is prompt.record
                    and session.budget.remaining(self.clock()) > 0)

    async def _answer(self, tab, sock, generation, hold, fields):
        session = self.sessions.get(tab.id)
        prompt = session.prompt if session and session.tab is tab else None
        if prompt is None:
            return
        if (prompt.nonce != fields.get("id") or prompt.answering
                or not self._reply_current(session, prompt, sock, generation, hold)):
            return
        prompt.answering = True
        tab.stream.owner_input()
        if fields.get("cancel"):
            await self._finish(prompt, "cancelled", cancel=True)
            return
        try:
            username, secret = bounds.credential_fields(fields.get("username"), fields.get("password"))
            if not session.budget.submit(self.clock()):
                await self._finish(prompt, "reload", cancel=True)
                return
        except bounds.AuthRefused:
            await self._finish(prompt, "refused", cancel=True)
            return
        # No await between the final exact-identity/hold check and dispatch.
        response = {"requestId": prompt.request, "authChallengeResponse": {
            "response": "ProvideCredentials", "username": username, "password": secret}}
        # Retire nonce before Chrome can emit a same-request wrong-password retry.
        session.prompt = None
        self._pending.discard(prompt)
        self.host._publish_pane_tabs()
        nonce = prompt.nonce
        prompt.nonce = None
        if prompt.timer is not None:
            prompt.timer.cancel()
        try:
            sent = await self._send(session, "Fetch.continueWithAuth", response,
                current=lambda: self._reply_current(session, prompt, sock, generation, hold))
            if not sent:
                if session.navigation is not None:
                    session.navigation.refused = True
                await self._deny_request(session, prompt.request)
        finally:
            response.clear()
            username = secret = None
            fields.clear()
            if session.navigation is not None and session.prompt is None:
                session.navigation.clock.set_paused(False, self.clock())
            if nonce:
                tab.stream.send_to(sock, {"t": "auth_closed", "id": nonce, "reason": "answered"})
            self.host.release_page_hold(tab, prompt.key, prompt.token)

    async def _clear_navigation(self, session, nav):
        if session.navigation is not nav:
            return
        if session.prompt is not None:
            await self._finish(session.prompt, "changed", cancel=True)
        # _finish awaits CDP. An old finally cannot erase a newer operation.
        if session.navigation is nav:
            session.navigation = None
            session.restart = None
            session.tab.pending_port = None
            self.host._sync_ports(session.tab)
            self.host._publish_pane_tabs()
            nav.ready.set()

    async def cancel_navigation(self, tab):
        session = self.sessions.get(tab.id)
        if session is None or session.tab is not tab:
            return
        session.epoch += 1
        nav = session.navigation
        if nav is not None:
            nav.refused = True
        prompt = session.prompt
        if prompt is not None:
            # Stop the navigation while it is still paused on its challenge.
            # CancelAuth first let Chrome go on to commit the 401 it held, and
            # a renderer slow to commit it (any busy machine) interrupted the
            # newer navigation this cancel makes way for ("navigation failed").
            # The prompt is superseded from here on: marked answering before
            # that await, so no refresh republishes it with a fresh nonce
            # (`_publish`), no reply can answer it (`_answer`) and no receipt
            # adopts it; `_finish` still sends its `auth_closed`.
            prompt.answering = True
            self.host._publish_pane_tabs()
            if (nav is not None and nav.task is not None
                    and nav.task is not asyncio.current_task() and not nav.task.done()):
                await self._send(session, "Page.stopLoading", {})
            if session.prompt is prompt:            # (the stop may have settled it)
                await self._finish(prompt, "changed", cancel=True)
        if nav is not None and nav.task is not asyncio.current_task():
            try:
                if not nav.task.done():
                    if not nav.task.cancelling():
                        nav.task.cancel()
                    await self._send(session, "Page.stopLoading", {})
                await asyncio.gather(nav.task, return_exceptions=True)
            finally:
                await self._clear_navigation(session, nav)

    def release(self, tab):
        session = self.sessions.get(tab.id)
        if session is None or session.tab is not tab or session.closed:
            return
        session.closed = True
        self.invalidate(tab)
        nav = session.navigation
        if nav and nav.task and not nav.task.done() and not nav.task.cancelling():
            nav.task.cancel()
        # Reserved one-per-session cleanup is independent of the saturated CDP
        # event queue. It retains the prompt hold until actual denial settles.
        async def cleanup():
            if session.prompt is not None:
                await self._finish(session.prompt, "changed", cancel=True)
            tasks = tuple(session.tasks)
            if nav is not None and nav.task is not None:
                tasks += (nav.task,)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if nav is not None:
                await self._clear_navigation(session, nav)
            if self.sessions.get(tab.id) is session:
                self.sessions.pop(tab.id, None)
        task = asyncio.create_task(cleanup())
        self._cleanup.add(task)
        def done(finished):
            self._cleanup.discard(finished)
            if not finished.cancelled():
                finished.exception()
        task.add_done_callback(done)

    async def settle(self):
        self._closing = True
        for session in list(self.sessions.values()):
            await self.cancel_navigation(session.tab)
            session.closed = True
            if session.prompt is not None:
                await self._finish(session.prompt, "changed", cancel=True)
        tasks = [task for session in self.sessions.values() for task in session.tasks]
        tasks += list(self._cleanup) + list(self._install_tasks) + list(self._answer_tasks)
        current = asyncio.current_task()
        if tasks:
            await asyncio.gather(*(task for task in tasks if task is not current), return_exceptions=True)
        self.sessions.clear()
        self._installing.clear()
        self._install_tasks.clear()
        self._pending.clear()

    async def navigate(self, tab, operation, *, owner=False, port=None, restart_token=None):
        try:
            session = await self.install(tab)
        except Exception:
            return {"loaded": False, "error": "authentication interception is unavailable"}
        await self.cancel_navigation(tab)
        session.restart = restart_token
        if owner:
            session.budget = bounds.AuthBudget()
            session.requests.clear()
            session.networks.clear()
        now = self.clock()
        nav = Navigation(secrets.token_hex(16), tab, tab.page, session.instance,
                         bounds.NavigationClock(self.navigation_timeout, now, session.budget),
                         restart=restart_token)
        session.navigation = nav
        if owner:
            tab.pending_port = port
            self.host._sync_ports(tab)
        async def child():
            try:
                waiter = asyncio.create_task(operation())
                try:
                    while not waiter.done():
                        remaining = nav.clock.remaining(self.clock())
                        if remaining <= 0:
                            await self._send(session, "Page.stopLoading", {})
                            waiter.cancel()
                            await asyncio.gather(waiter, return_exceptions=True)
                            nav.result = {"loaded": False, "error": "navigation timed out"}
                            break
                        await asyncio.wait({waiter}, timeout=min(remaining, 0.25))
                    if nav.result is None:
                        await waiter
                        nav.result = {"loaded": not nav.refused}
                        if nav.refused:
                            nav.result["error"] = "authentication refused; Reload to retry"
                finally:
                    if not waiter.done():
                        waiter.cancel()
                    await asyncio.gather(waiter, return_exceptions=True)
            except asyncio.CancelledError:
                await self._send(session, "Page.stopLoading", {})
                nav.result = {"loaded": False, "error": "navigation cancelled"}
                raise
            except Exception as exc:
                from .host import _net_error
                nav.result = {"loaded": False, "error": _net_error(exc)}
            finally:
                await self._clear_navigation(session, nav)
        async def owned():
            try:
                if restart_token is not None:
                    if not self.host.activity.restart_current(restart_token, tab.profile, session.instance.generation):
                        raise RuntimeError("Browser restart changed")
                    return await child()
                return await self.host._activity_run(tab, child)
            finally:
                # Admission can fail before child() ever starts.
                await self._clear_navigation(session, nav)
        nav.task = asyncio.create_task(owned())
        def done(task):
            if not task.cancelled():
                task.exception()
            nav.ready.set()
        nav.task.add_done_callback(done)
        ready = asyncio.create_task(nav.ready.wait())
        try:
            await asyncio.wait({nav.task, ready}, return_when=asyncio.FIRST_COMPLETED)
            if nav.task.done():
                await nav.task
                return nav.result or {"loaded": False, "error": "navigation failed"}
            if session.prompt is not None and self.current(session) and session.navigation is nav:
                return {"loaded": False, "auth_pending": True, "nav_id": nav.id}
            await nav.task
            return nav.result or {"loaded": False, "error": "navigation failed"}
        except BaseException:
            await self.cancel_navigation(tab)
            raise
        finally:
            ready.cancel()

    def receipt_current(self, tab, nav_id):
        session = self.sessions.get(tab.id)
        return bool(session and self.current(session) and session.navigation
                    and session.navigation.id == nav_id and session.prompt is not None
                    and not session.prompt.answering)
