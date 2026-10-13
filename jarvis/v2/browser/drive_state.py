"""Bounded in-memory ownership and ref capabilities for the dormant BR9b driver.

No browser IO, provider, approval or production enable switch lives here.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import re
import secrets
import threading
import time
from typing import Callable

from jarvis import runtime
from .act_rules import MAX_ACTIONS_PER_TAB_HOUR, MAX_JUDGED_PER_TURN
from .panes import PaneSnapshot

MAX_TURNS = 64
MAX_TABS = 64
LEASE_S = 30.0
REFS_S = 1800.0
_SERIAL = re.compile(r"[0-9a-f]{32}")
_REF = re.compile(r"e[1-9][0-9]{0,2}")


@dataclass
class DriveTurn:
    thread_id: str
    turn_id: str
    strict: bool
    project_root: str = field(repr=False)
    owner_words: str = field(repr=False)
    attempted: bool = False
    pane: str = ""
    closed: bool = False

    @property
    def key(self):
        return self.thread_id, self.turn_id

    def exhaustion(self):
        if not self.attempted:return None
        pane="Browser pane "+self.pane if self.pane else "the Browser pane"
        return "Stopped after eight steps in "+pane+". Say 'go on' to continue."


_TURN: ContextVar[DriveTurn | None] = ContextVar("jarvis_browser_drive_turn", default=None)


def current_turn() -> DriveTurn | None:
    turn = _TURN.get()
    caller = runtime.caller() or {}
    if (turn is None or turn.closed or type(turn.strict) is not bool
            or not turn.thread_id or not turn.turn_id
            or caller.get("thread_id") != turn.thread_id or caller.get("turn_id") != turn.turn_id
            or caller.get("provider") != "fast"):
        return None
    return turn


@contextmanager
def turn_scope(turn: DriveTurn, retire: Callable[[tuple[str, str]], None]):
    token = _TURN.set(turn)
    try:
        yield turn
    finally:
        turn.closed = True
        try:
            retire(turn.key)
        finally:
            _TURN.reset(token)


class DriveStateRefused(Exception):
    """Fixed safe wording, never a page, path or typed text."""


@dataclass(frozen=True)
class RefCapability:
    owner: tuple[str, str]
    snapshot: PaneSnapshot
    serial: str
    refs: frozenset[str]
    at: float


@dataclass(frozen=True)
class ActionLease:
    tab_id: str
    owner: tuple[str, str]
    token: str


@dataclass
class _Driver:
    owner: tuple[str, str]
    token: str
    at: float
    active: bool = True
    retired: bool = False


class DriveState:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.closed = False
        self.refs: OrderedDict[tuple[str, str], RefCapability] = OrderedDict()
        self.drivers: dict[str, _Driver] = {}
        self.judged: dict[tuple[str, str], int] = {}
        self.hourly: dict[str, deque[float]] = {}

    def publish(self, turn: DriveTurn, snapshot: PaneSnapshot, serial: str, refs) -> None:
        """Only the successful guarded Remote tool-read path may call this."""
        if (not isinstance(snapshot, PaneSnapshot) or snapshot.mode != "remote"
                or not snapshot.tab_id or not snapshot.see or snapshot.covered or not snapshot.drawn
                or not isinstance(serial, str) or _SERIAL.fullmatch(serial) is None):
            return
        if not isinstance(refs, (list, tuple, set, frozenset)) or len(refs) > 200:
            return
        if any(not isinstance(ref, str) or _REF.fullmatch(ref) is None for ref in refs):
            return
        with self.lock:
            self._prune()
            if self.closed or turn.closed:
                return
            # A new isolated scan replaces this tab's Map. Never leave another
            # caller's e1 pointing silently at the new element.
            for owner, old in list(self.refs.items()):
                if old.snapshot.tab_id == snapshot.tab_id:
                    self.refs.pop(owner, None)
            self.refs.pop(turn.key, None)
            self.refs[turn.key] = RefCapability(turn.key, snapshot, serial, frozenset(refs), self.clock())
            while len(self.refs) > MAX_TURNS:
                self.refs.popitem(last=False)

    def capability(self, turn: DriveTurn, snapshot: PaneSnapshot, target: str) -> RefCapability:
        with self.lock:
            self._prune()
            cap = self.refs.get(turn.key)
            if (self.closed or turn.closed or cap is None or cap.snapshot != snapshot
                    or target not in cap.refs):
                raise DriveStateRefused("look at this Browser pane again before choosing that target")
            return cap

    def refs_current(self, cap: RefCapability) -> bool:
        """An issued prepared action cannot outlive a newer look or turn."""
        with self.lock:
            self._prune()
            return not self.closed and self.refs.get(cap.owner) == cap

    def _prune(self):
        now = self.clock()
        for owner, cap in list(self.refs.items()):
            if now - cap.at >= REFS_S:
                self.refs.pop(owner, None)
        for tab, driver in list(self.drivers.items()):
            if not driver.active and now - driver.at >= LEASE_S:
                self.drivers.pop(tab, None)
        for tab, history in list(self.hourly.items()):
            while history and now - history[0] >= 3600:
                history.popleft()
            if not history:
                self.hourly.pop(tab, None)

    def reserve(self, turn: DriveTurn, tab_id: str) -> ActionLease:
        with self.lock:
            self._prune()
            if self.closed or turn.closed:
                raise DriveStateRefused("this Browser-driving turn has ended")
            owner = self.drivers.get(tab_id)
            if owner is not None and (owner.active or owner.owner != turn.key):
                raise DriveStateRefused("another Browser action or driver is using this pane")
            if owner is None and len(self.drivers) >= MAX_TABS:
                raise DriveStateRefused("too many Browser panes are being driven")
            if turn.key not in self.judged and len(self.judged) >= MAX_TURNS:
                raise DriveStateRefused("too many Browser-driving turns are active")
            token = secrets.token_hex(16)
            self.drivers[tab_id] = _Driver(turn.key, token, self.clock())
            self.judged.setdefault(turn.key, 0)
            return ActionLease(tab_id, turn.key, token)

    def _driver(self, lease: ActionLease):
        found = self.drivers.get(lease.tab_id)
        if (self.closed or found is None or found.owner != lease.owner
                or found.token != lease.token or not found.active or found.retired):
            raise DriveStateRefused("this Browser action was stopped")
        return found

    def current(self, lease: ActionLease) -> bool:
        with self.lock:
            try:
                self._driver(lease)
                return True
            except DriveStateRefused:
                return False

    def counts(self, lease: ActionLease) -> tuple[int, int]:
        with self.lock:
            self._prune()
            self._driver(lease)
            return self.judged.get(lease.owner, 0), len(self.hourly.get(lease.tab_id, ()))

    def charge_judgement(self, lease: ActionLease):
        with self.lock:
            self._driver(lease)
            used = self.judged.get(lease.owner, 0)
            if used >= MAX_JUDGED_PER_TURN:
                raise DriveStateRefused("this turn has reached its Browser review limit; stop and ask the owner to continue")
            self.judged[lease.owner] = used + 1

    def charge_action(self, lease: ActionLease):
        with self.lock:
            self._prune()
            self._driver(lease)
            history = self.hourly.get(lease.tab_id)
            if history is None:
                if len(self.hourly) >= MAX_TABS:
                    raise DriveStateRefused("too many Browser panes have recent actions")
                history = self.hourly[lease.tab_id] = deque()
            if len(history) >= MAX_ACTIONS_PER_TAB_HOUR:
                raise DriveStateRefused("this Browser pane has reached its hourly action limit")
            history.append(self.clock())

    def finish(self, lease: ActionLease):
        with self.lock:
            found = self.drivers.get(lease.tab_id)
            # A stale cleanup must never erase a newer action or driver's lease.
            if found is None or found.owner != lease.owner or found.token != lease.token:
                return
            if found.retired or self.closed:
                self.drivers.pop(lease.tab_id, None)
            else:
                found.active = False
                found.at = self.clock()

    def retire(self, owner: tuple[str, str]):
        with self.lock:
            self.refs.pop(owner, None)
            self.judged.pop(owner, None)
            for tab, found in list(self.drivers.items()):
                if found.owner != owner:
                    continue
                if found.active:
                    # Stop per-event checks immediately, but keep the reservation
                    # until input cleanup finishes; no overlapping new driver.
                    found.retired = True
                else:
                    self.drivers.pop(tab, None)

    def stop(self):
        with self.lock:
            self.closed = True
            self.refs.clear()
            self.judged.clear()
            self.hourly.clear()
            for found in self.drivers.values():
                found.retired = True

