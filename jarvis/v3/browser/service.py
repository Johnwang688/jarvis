"""Lease the daemon's lazy Windows browser sessions."""

from contextlib import contextmanager
from dataclasses import dataclass, field
import threading
import time

from .session import BrowserSession


class BrowserBusy(Exception):
    """The lease wait expired."""


class BrowserUnavailable(Exception):
    """The service is closed or the session failed to start."""


_CLOSED = "The browser service is closed."
_START_FAILED = "The browser could not be started."


@dataclass
class _Slot:
    lease: threading.Lock = field(default_factory=threading.Lock)
    session: BrowserSession | None = None
    starting: bool = False
    ready: bool = False
    closing: int = 0
    last_used: float = 0.0
    failed: bool = False


class BrowserService:
    def __init__(self, *, session_factory=BrowserSession, clock=time.monotonic,
                 idle_seconds=600.0, lease_wait=30.0):
        if not 0 <= lease_wait <= threading.TIMEOUT_MAX:
            raise ValueError("The browser lease wait is out of range.")
        self._session_factory = session_factory
        self._clock = clock
        self._idle_seconds = idle_seconds
        self._lease_wait = lease_wait
        self._slots = {browser: _Slot() for browser in ("chrome", "edge")}
        # Take a lease before entering this lock when both are needed.
        # Session startup and cleanup run outside this lock.
        self._lock = threading.Lock()
        self._closed = False
        self._reaper: threading.Thread | None = None

    def _close_session(self, slot, session):
        with self._lock:
            if slot.session is not session:
                return
            slot.ready = False
            slot.closing += 1
        closed = False
        try:
            session.close()
            closed = True
        except Exception:
            pass
        finally:
            with self._lock:
                slot.closing -= 1
                # A shutdown retry can overlap a teardown after its lease timed out.
                if not slot.closing and (closed or not session.alive):
                    slot.session = None

    @contextmanager
    def use(self, browser):
        if browser not in ("chrome", "edge"):
            raise ValueError("Choose chrome or edge.")
        slot = self._slots[browser]
        with self._lock:
            if self._closed:
                raise BrowserUnavailable(_CLOSED)
        if not slot.lease.acquire(timeout=self._lease_wait):
            with self._lock:
                if self._closed:
                    raise BrowserUnavailable(_CLOSED)
            raise BrowserBusy("The browser is in use.")
        try:
            session = self._session_for(browser, slot)
            try:
                yield session
            finally:
                with self._lock:
                    slot.last_used = self._clock()
                    died = not self._closed and not session.alive
                if died:
                    self._close_session(slot, session)
        finally:
            slot.lease.release()

    def _session_for(self, browser, slot):
        with self._lock:
            if self._closed:
                raise BrowserUnavailable(_CLOSED)
            old = slot.session
            if slot.ready and old.alive:
                return old
        if old is not None:
            self._close_session(slot, old)
        with self._lock:
            if self._closed:
                raise BrowserUnavailable(_CLOSED)
            if slot.session is not None:
                raise BrowserUnavailable(_START_FAILED)

        try:
            session = self._session_factory(browser)
        except Exception:
            session = None
        if session is None:
            with self._lock:
                slot.failed = True
                reason = _CLOSED if self._closed else _START_FAILED
            raise BrowserUnavailable(reason)

        with self._lock:
            if self._closed:
                raise BrowserUnavailable(_CLOSED)
            slot.session = session
            slot.starting = True
        try:
            try:
                session.start()
                alive = session.alive
            except Exception:
                alive = False
            except BaseException:
                self._close_session(slot, session)
                raise
            with self._lock:
                if alive and not self._closed:
                    slot.ready = True
                    slot.failed = False
                    slot.starting = False
                    return session
                slot.failed = not alive
            self._close_session(slot, session)
        finally:
            with self._lock:
                slot.starting = False
        with self._lock:
            reason = _CLOSED if self._closed else _START_FAILED
        raise BrowserUnavailable(reason)

    def reap(self) -> int:
        count = 0
        for slot in self._slots.values():
            if not slot.lease.acquire(blocking=False):
                continue
            try:
                with self._lock:
                    if (self._closed or slot.session is None
                            or self._clock() - slot.last_used <= self._idle_seconds):
                        continue
                    session = slot.session
                self._close_session(slot, session)
                count += 1
            except Exception:
                pass
            finally:
                slot.lease.release()
        return count

    def reap_async(self) -> None:
        with self._lock:
            if self._closed or (self._reaper is not None and self._reaper.is_alive()):
                return
            worker = threading.Thread(target=self.reap, name="jarvis-browser-reap", daemon=True)
            worker.start()
            self._reaper = worker

    def status(self) -> dict[str, str]:
        with self._lock:
            return {browser: self._state(slot) for browser, slot in self._slots.items()}

    def _state(self, slot):
        leased = slot.lease.locked() and (slot.session is not None or not self._closed)
        if leased or slot.closing or slot.starting:
            return "in_use"
        if slot.session is not None:
            return "open" if slot.session.alive else "failed"
        if slot.failed and not self._closed:
            return "failed"
        return "closed"

    def close(self) -> None:
        with self._lock:
            self._closed = True
            reaper = self._reaper
        deadline = time.monotonic() + self._lease_wait
        for slot in self._slots.values():
            with self._lock:
                if slot.session is None:
                    continue
                starting = slot.starting
            # Startup owns cleanup if shutdown begins before it finishes.
            # Wait for that handoff before trying to close the session ourselves.
            acquired = slot.lease.acquire() if starting else slot.lease.acquire(timeout=self._lease_wait)
            try:
                with self._lock:
                    session = slot.session
                if session is not None:
                    self._close_session(slot, session)
            finally:
                if acquired:
                    slot.lease.release()
        if reaper is not None and reaper is not threading.current_thread():
            # The time spent waiting on the leases counts toward the worker wait.
            reaper.join(max(0, deadline - time.monotonic()))
