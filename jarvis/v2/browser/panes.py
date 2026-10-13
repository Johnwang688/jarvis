"""Owner Browser pane snapshots shared by the HUD registry and BR5.

This module's public values are immutable and memory-only. They are not event
payloads, log fields or persisted browsing history. In particular, URLs and
source identities never go onto the bus; browser_viewed uses only view_id.
The registry implementation resolves a displayed slot, never a raw tab ID,
and revalidates generation plus host document identity around a capture.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace, replace
import re
import secrets
import threading
import time
from typing import Literal
from urllib.parse import urlsplit

from . import policy


@dataclass(frozen=True)
class ResolvedSource:
    """An immutable identity resolved under the dev-server registry lock.

    An unknown/legacy source is a refusal, not an owner-entered URL. A terminal
    source additionally requires current terminal readability at capture time;
    a display label or a client-supplied terminal ID never grants that access.
    """

    kind: Literal["owner", "terminal", "thread", "unknown"]
    entry_id: str | None = None
    terminal_id: str | None = None
    thread_id: str | None = None
    task_id: str | None = None


@dataclass(frozen=True)
class PaneSnapshot:
    """The complete identity BR5 must revalidate before and after a look.

    page_identity and document_generation are None only for Direct. Remote
    navigation invalidates document_generation even on a same-URL reload and
    before the HUD sends another report. view_id is the daemon tab ID for
    Remote, or a host-issued opaque binding ID for Direct; it is never the
    temporary hidden-load ID or a local six-character tab ID.
    """

    window: str
    pane_id: int
    slot: int
    local_tab_id: str
    mode: Literal["direct", "remote"]
    tab_id: str | None
    view_id: str
    profile: str
    ctx: str | None
    url: str = field(repr=False)
    see: bool
    drive: bool
    drawn: bool
    covered: bool
    generation: int
    page_identity: int | None
    document_generation: int | None
    source: ResolvedSource


class PaneRefused(Exception):
    """Fixed safe refusal text, with no URL, title or source label."""

    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status


# Lock order: daemon SSE lifecycle -> this registry -> host identity cache.
# Source copies are collected BEFORE this lock. No registry code takes the
# daemon/DevServers locks, and no lock is held across Chrome/network calls.
_WINDOW = re.compile(r"[0-9a-f]{32}")
_LOCAL = re.compile(r"[a-z0-9]{6}")
_REMOTE = re.compile(r"[0-9a-f]{8}")
_PROFILE = re.compile(r"p-[0-9a-f]{8}")
_ENTRY = re.compile(r"ds_[0-9a-f]{8}")
LEASE_S = 60
MAX_WINDOWS = 16
SAFE_INTEGER = 2**53 - 1


def _bad(text="invalid Browser pane report"):
    raise PaneRefused(400, text)


def _shape(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        _bad()
    return value


def _id(value, pattern):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        _bad()
    return value


def _number(value, low, high):
    if type(value) is not int or not low <= value <= high:
        _bad()
    return value


def _boolean(value):
    if type(value) is not bool:
        _bad()
    return value


@dataclass(frozen=True)
class _Binding:
    local_id: str
    mode: str
    tab_id: str | None
    url: str = field(repr=False)
    source: ResolvedSource
    view_id: str
    generation: int
    pending: int = 0


@dataclass(frozen=True)
class _Pane:
    pane_id: int
    slot: int | None
    drawn: bool
    profile: str
    ctx: str | None
    see: bool
    active: str | None
    tabs: dict


@dataclass
class _Window:
    seq: int = 0
    focus_seq: int = 0
    focused_pane: int | None = None
    covered: bool = False
    expires: float = 0
    panes: dict = field(default_factory=dict)


class BrowserPanes:
    """Bounded owner-only mappings. Snapshots never leave the process except
    through the separately guarded BR5 capture result. The owner receipt has
    opaque event IDs only; no URL or source identity is published on the bus.
    """

    def __init__(self, daemon, *, clock=time.monotonic):
        self.daemon = daemon
        self.host = daemon.browser
        self.clock = clock
        self._lock = threading.RLock()
        self._live = {}
        self._sequences = {}
        self._windows = {}
        self._dormant = {}  # bounded by live windows; frozen source/focus survives lease expiry
        self._front = None
        self._generation = 0
        self._retiring = set()

    def window_started(self, window):
        if not isinstance(window, str) or not _WINDOW.fullmatch(window):
            return
        with self._lock:
            if window not in self._live and len(self._live) >= MAX_WINDOWS:
                return
            self._live[window] = self._live.get(window, 0) + 1

    def stop(self):
        with self._lock:
            self._live.clear()
            self._sequences.clear()
            self._windows.clear()
            self._dormant.clear()
            self._front = None

    def window_ended(self, window):
        with self._lock:
            count = self._live.get(window, 0)
            if count > 1:
                self._live[window] = count - 1
            else:
                self._live.pop(window, None)
                self._sequences.pop(window, None)
                self._drop(window)

    def _drop(self, window):
        self._windows.pop(window, None)
        self._dormant.pop(window, None)
        if self._front and self._front[0] == window:
            self._front = None

    def _expire(self):
        now = self.clock()
        for window, state in list(self._windows.items()):
            if window not in self._live:
                self._drop(window)
            elif state.expires <= now:
                # Expiry revokes discoverability, not provenance. The live
                # window's next report must not erase source or invent focus.
                self._dormant[window] = self._windows.pop(window)
                if self._front and self._front[0] == window:
                    self._front = None

    def _next(self):
        self._generation += 1
        return self._generation

    def _admit(self, window, seq, *, existing=False):
        _id(window, _WINDOW)
        _number(seq, 1, SAFE_INTEGER)
        self._expire()
        if window not in self._live:
            raise PaneRefused(409, "a live owner window is required")
        if seq <= self._sequences.get(window, 0):
            raise PaneRefused(409, "a newer Browser report is already applied")
        state = self._windows.get(window)
        if state is None:
            if existing:
                raise PaneRefused(409, "report this Browser pane first")
            state = self._dormant.get(window)
            if state is None and len(self._windows) + len(self._dormant) >= MAX_WINDOWS:
                raise PaneRefused(409, "too many Browser windows are active")
        elif seq <= state.seq:
            raise PaneRefused(409, "a newer Browser report is already applied")
        return state

    def _direct(self, value):
        if value == "":
            return ""
        target = policy.judge_url(value, refused=self.host.refused_ports())
        parts = urlsplit(target)
        host = (parts.hostname or "").lower()
        if (parts.scheme not in ("http", "https") or not
                (host in ("localhost", "127.0.0.1", "0.0.0.0") or host.endswith(".localhost"))):
            raise PaneRefused(400, "Direct loads only frameable local servers")
        return target

    def _source(self, value):
        if value is None:
            return ResolvedSource("owner")
        if not isinstance(value, dict):
            _bad()
        if value == {"kind": "unknown"}:
            return ResolvedSource("unknown")
        _shape(value, ("kind", "entryId"))
        if value["kind"] != "dev_server":
            _bad()
        entry = _id(value["entryId"], _ENTRY)
        source = self.daemon.devservers.source_snapshot(entry)
        if isinstance(source, dict):
            if source.get("kind") == "terminal" and isinstance(source.get("terminal_id"), str):
                return ResolvedSource("terminal", entry_id=entry, terminal_id=source["terminal_id"])
            if source.get("kind") == "thread" and isinstance(source.get("thread_id"), str):
                return ResolvedSource("thread", entry_id=entry, thread_id=source["thread_id"],
                                      task_id=source.get("task_id"))
        return ResolvedSource("unknown", entry_id=entry)

    def _parse(self, body):
        _shape(body, ("window", "seq", "focus_seq", "focused_slot", "covered", "panes"))
        _id(body["window"], _WINDOW)
        _number(body["seq"], 1, SAFE_INTEGER)
        _number(body["focus_seq"], 0, SAFE_INTEGER)
        if body["focused_slot"] is not None:
            _number(body["focused_slot"], 1, 8)
        _boolean(body["covered"])
        if not isinstance(body["panes"], list) or len(body["panes"]) > 8:
            _bad()
        panes, slots, remote_ids = {}, set(), set()
        for raw in body["panes"]:
            _shape(raw, ("pane_id", "slot", "drawn", "profile", "ctx", "see", "drive", "active", "tabs"))
            pid = _number(raw["pane_id"], 1, 8)
            if pid in panes:
                _bad()
            slot = raw["slot"]
            if slot is not None:
                _number(slot, 1, 8)
                if slot in slots:
                    _bad()
                slots.add(slot)
            drawn = _boolean(raw["drawn"])
            if drawn and slot is None:
                _bad()
            _boolean(raw["see"])
            if _boolean(raw["drive"]):
                raise PaneRefused(409, "Agents may drive is off")
            profile = raw["profile"]
            if profile != "private":
                _id(profile, _PROFILE)
                if raw["ctx"] is not None:
                    _bad()
            elif raw["ctx"] is not None:
                _id(raw["ctx"], _REMOTE)
            if not isinstance(raw["tabs"], list) or len(raw["tabs"]) > 12:
                _bad()
            tabs = {}
            for tab in raw["tabs"]:
                if not isinstance(tab, dict) or tab.get("mode") not in ("direct", "remote"):
                    _bad()
                mode = tab["mode"]
                _shape(tab, ("id", "mode", "source", "url" if mode == "direct" else "tab_id"))
                lid = _id(tab["id"], _LOCAL)
                if lid in tabs:
                    _bad()
                source = self._source(tab["source"])
                tid, url = None, ""
                if mode == "direct":
                    if profile == "private":
                        raise PaneRefused(400, "Private panes use Remote only")
                    url = self._direct(tab["url"])
                elif tab["tab_id"] is not None:
                    tid = _id(tab["tab_id"], _REMOTE)
                    if tid in remote_ids:
                        _bad()
                    remote_ids.add(tid)
                tabs[lid] = _Binding(lid, mode, tid, url, source, "", 0)
            active = raw["active"]
            if active is not None and (not isinstance(active, str) or active not in tabs):
                _bad()
            panes[pid] = _Pane(pid, slot, drawn, profile, raw["ctx"], raw["see"], active, tabs)
        if body["focused_slot"] is not None and body["focused_slot"] not in slots:
            _bad()
        return panes

    def _remote(self, pane, tab):
        row = self.host.pane_tab(tab.tab_id) if tab.tab_id and tab.tab_id not in self._retiring else None
        if row is None or row["profile"] != pane.profile or row["ctx"] != pane.ctx:
            raise PaneRefused(409, "this Remote tab is no longer available in this profile")
        return row

    @staticmethod
    def _context(pane, covered):
        return (pane.slot, pane.drawn, pane.profile, pane.ctx, pane.see, pane.active, covered)

    def report(self, body):
        # Parsing copies source identities without this lock. Host reads below
        # are bounded cached snapshots, safe even on the host event-loop thread.
        panes = self._parse(body)
        with self._lock:
            old = self._admit(body["window"], body["seq"])
            if old and body["focus_seq"] < old.focus_seq:
                raise PaneRefused(409, "an older Browser focus report was refused")
            for pane in panes.values():
                for tab in pane.tabs.values():
                    if tab.mode == "remote" and tab.tab_id:
                        self._remote(pane, tab)
            updated = {}
            for pid, pane in panes.items():
                prior = old.panes.get(pid) if old else None
                tabs = {}
                for lid, tab in pane.tabs.items():
                    previous = prior.tabs.get(lid) if prior else None
                    if previous and previous.source.kind != "owner":
                        tab = replace(tab, source=previous.source)
                    context_same = prior and self._context(prior, old.covered) == self._context(pane, body["covered"])
                    binding_same = previous and (previous.mode, previous.tab_id, previous.url, previous.source) == (tab.mode, tab.tab_id, tab.url, tab.source)
                    same = context_same and binding_same and not previous.pending and old.expires > self.clock()
                    identity_same = binding_same and prior.profile == pane.profile and prior.ctx == pane.ctx
                    view = previous.view_id if identity_same else (tab.tab_id if tab.mode == "remote" else "v_" + secrets.token_hex(12))
                    tabs[lid] = replace(tab, view_id=view or "", generation=previous.generation if same else self._next())
                updated[pid] = replace(pane, tabs=tabs)
            focused = next((p.pane_id for p in updated.values() if p.slot == body["focused_slot"]), None)
            fresh_focus = body["focus_seq"] > (old.focus_seq if old else 0)
            if body["focused_slot"] is not None and focused is None:
                _bad()
            state = _Window(body["seq"], body["focus_seq"], focused, body["covered"], self.clock() + LEASE_S, updated)
            self._windows[body["window"]] = state
            self._dormant.pop(body["window"], None)
            self._sequences[body["window"]] = body["seq"]
            if fresh_focus and focused is not None:
                # Arrival order is authoritative across windows. Local counters
                # are compared only within their own window.
                self._front = (body["window"], focused)
            if self._front and self._front[0] == body["window"]:
                current = updated.get(self._front[1])
                if focused is None or state.covered or current is None or not current.drawn or current.slot is None:
                    self._front = None
            return self._receipt(body["window"], state)

    @staticmethod
    def _receipt(window, state):
        return {"window": window, "seq": state.seq, "panes": [
            {"pane_id": pane.pane_id, "see": pane.see, "drive": False,
             "tabs": [{"id": tab.local_id, "view_id": tab.view_id, "generation": tab.generation}
                      for tab in pane.tabs.values()]} for pane in state.panes.values()]}

    def permissions(self, pane_id, body):
        if not isinstance(body, dict) or not {"window", "seq"} < set(body) or not set(body) <= {"window", "seq", "see", "drive"}:
            _bad()
        _number(pane_id, 1, 8)
        for key in ("see", "drive"):
            if key in body:
                _boolean(body[key])
        if body.get("drive"):
            raise PaneRefused(409, "Agents may drive is off")
        with self._lock:
            state = self._admit(body["window"], body["seq"], existing=True)
            pane = state.panes.get(pane_id)
            if pane is None:
                raise PaneRefused(404, "no such Browser pane")
            see = body.get("see", pane.see)
            if see != pane.see:
                pane = replace(pane, see=see, tabs={lid: replace(tab, generation=self._next()) for lid, tab in pane.tabs.items()})
                state.panes[pane_id] = pane
            state.seq = body["seq"]
            self._sequences[body["window"]] = body["seq"]
            state.expires = self.clock() + LEASE_S
            return self._receipt(body["window"], state)

    def _snapshot(self, window, state, pane):
        if state.covered or not pane.drawn or pane.slot is None or pane.active is None:
            raise PaneRefused(409, "that Browser pane is not visible")
        tab = pane.tabs.get(pane.active)
        if tab is None or tab.pending:
            raise PaneRefused(409, "that Browser pane is changing")
        url, page, document = tab.url, None, None
        if tab.mode == "remote":
            row = self._remote(pane, tab)
            url, page, document = row["url"], row["page_identity"], row["document_generation"]
        if not url or not tab.view_id:
            raise PaneRefused(409, "that Browser pane has no page open")
        return PaneSnapshot(window, pane.pane_id, pane.slot, tab.local_id, tab.mode,
                            tab.tab_id, tab.view_id, pane.profile, pane.ctx, url,
                            pane.see, False, pane.drawn, state.covered, tab.generation,
                            page, document, tab.source)

    def resolve(self, pane=""):
        if not isinstance(pane, str) or (pane and not re.fullmatch(r"[1-8]", pane)):
            raise PaneRefused(400, "choose a displayed Browser pane number")
        with self._lock:
            self._expire()
            if not pane:
                if not self._front:
                    raise PaneRefused(409, "focus a visible Browser pane first")
                window, pid = self._front
                state = self._windows.get(window)
                item = state.panes.get(pid) if state else None
                if item is None:
                    raise PaneRefused(409, "focus a visible Browser pane first")
                return self._snapshot(window, state, item)
            matches = [(window, state, item) for window, state in self._windows.items()
                       for item in state.panes.values() if item.slot == int(pane) and item.drawn and not state.covered]
            if len(matches) != 1:
                raise PaneRefused(409, "choose one visible Browser pane in the owner window")
            return self._snapshot(*matches[0])

    def resolve_owner(self, window, pane_id, local_tab_id, expected_view_id):
        """Pin one owner-selected visible binding; never grant see or drive."""
        _id(window, _WINDOW)
        _number(pane_id, 1, 8)
        _id(local_tab_id, _LOCAL)
        if not isinstance(expected_view_id, str) or not re.fullmatch(r"(?:v_[0-9a-f]{24}|[0-9a-f]{8})", expected_view_id):
            _bad()
        with self._lock:
            self._expire()
            state = self._windows.get(window) if window in self._live else None
            pane = state.panes.get(pane_id) if state else None
            if pane is None:
                raise PaneRefused(409, "that Browser view is no longer visible")
            snapshot = self._snapshot(window, state, pane)
            if snapshot.local_tab_id != local_tab_id or snapshot.view_id != expected_view_id:
                raise PaneRefused(409, "that Browser view has changed")
            return snapshot

    def owner_current(self, snapshot):
        """Recheck the entire immutable binding, including its permission state."""
        if not isinstance(snapshot, PaneSnapshot):
            return False
        with self._lock:
            self._expire()
            state = self._windows.get(snapshot.window) if snapshot.window in self._live else None
            pane = state.panes.get(snapshot.pane_id) if state else None
            if pane is None:
                return False
            try:
                return self._snapshot(snapshot.window, state, pane) == snapshot
            except PaneRefused:
                return False

    def still_current(self, snapshot):
        if not isinstance(snapshot, PaneSnapshot) or not snapshot.see:
            return False
        with self._lock:
            self._expire()
            state = self._windows.get(snapshot.window)
            pane = state.panes.get(snapshot.pane_id) if state else None
            if pane is None:
                return False
            try:
                return self._snapshot(snapshot.window, state, pane) == snapshot
            except PaneRefused:
                return False

    def after_navigation(self, snapshot):
        """Rebind only a completed drive's document, never the visible binding.

        The caller must have finished all judged input and recheck its driver,
        owner-input and stop state separately. Ordinary looks keep using strict
        still_current. Production snapshots keep drive=False until a separately
        approved driving rollout; this helper cannot enable them.
        """
        if (not isinstance(snapshot, PaneSnapshot) or snapshot.mode != "remote"
                or not snapshot.see or not snapshot.drive
                or type(snapshot.page_identity) is not int
                or type(snapshot.document_generation) is not int):
            return None
        with self._lock:
            self._expire()
            state = self._windows.get(snapshot.window) if snapshot.window in self._live else None
            pane = state.panes.get(snapshot.pane_id) if state else None
            if pane is None:
                return None
            try:
                fresh = self._snapshot(snapshot.window, state, pane)
            except PaneRefused:
                return None
            if (type(fresh.document_generation) is not int
                    or fresh.document_generation < snapshot.document_generation
                    or (fresh.url != snapshot.url
                        and fresh.document_generation == snapshot.document_generation)):
                return None
            pinned = replace(fresh, url=snapshot.url,
                             document_generation=snapshot.document_generation)
            return fresh if pinned == snapshot else None

    def address(self, pane_id, local_id, body):
        _number(pane_id, 1, 8)
        _id(local_id, _LOCAL)
        _shape(body, ("window", "seq", "url", "mode"))
        if body["mode"] not in ("direct", "remote"):
            _bad()
        target = self._direct(body["url"]) if body["mode"] == "direct" else policy.judge_url(body["url"], refused=self.host.refused_ports())
        if not target:
            _bad("enter an address")
        with self._lock:
            state = self._admit(body["window"], body["seq"], existing=True)
            pane = state.panes.get(pane_id)
            tab = pane.tabs.get(local_id) if pane else None
            if tab is None:
                raise PaneRefused(404, "no such Browser tab")
            if pane.profile == "private" and body["mode"] != "remote":
                raise PaneRefused(400, "Private panes use Remote only")
            token = self._next()
            state.panes[pane_id] = replace(pane, tabs={**pane.tabs, local_id: replace(tab, pending=token, generation=token)})
            state.seq = body["seq"]
            self._sequences[body["window"]] = body["seq"]
        created = None
        try:
            if self.host.input_held():
                raise PaneRefused(409, "answer the approval card before navigating")
            if body["mode"] == "remote":
                if tab.mode == "remote" and tab.tab_id:
                    row = self.host.navigate(tab.tab_id, target, owner=True)
                else:
                    row = self.host.open_tab(pane.profile, target, pane.ctx, owner=True)
                    created = row["tab_id"]
                if not row.get("loaded", False):
                    raise PaneRefused(409, "that address could not be loaded")
                tid = row["tab_id"]
                identity = self.host.pane_tab(tid)
                if identity is None or identity["profile"] != pane.profile or (pane.ctx is not None and identity["ctx"] != pane.ctx):
                    raise PaneRefused(409, "the Remote page changed while opening")
            else:
                tid, identity = None, None
            with self._lock:
                self._expire()
                current = self._windows.get(body["window"])
                now_pane = current.panes.get(pane_id) if current else None
                now_tab = now_pane.tabs.get(local_id) if now_pane else None
                if now_tab is None or now_tab.generation != token or not now_tab.pending or body["window"] not in self._live:
                    raise PaneRefused(409, "that Browser tab changed while opening")
                new = _Binding(local_id, body["mode"], tid, target if tid is None else "", ResolvedSource("owner"), tid or "v_" + secrets.token_hex(12), self._next())
                current.panes[pane_id] = replace(now_pane, ctx=identity["ctx"] if identity else now_pane.ctx, tabs={**now_pane.tabs, local_id: new})
                current.expires = self.clock() + LEASE_S
                created = None
                return {"tab_id": tid, "ctx": current.panes[pane_id].ctx, "url": identity["url"] if identity else target, **self._receipt(body["window"], current)}
        finally:
            with self._lock:
                current = self._windows.get(body["window"])
                now_pane = current.panes.get(pane_id) if current else None
                now_tab = now_pane.tabs.get(local_id) if now_pane else None
                if now_tab and now_tab.pending == token:
                    # Failure never removes the source restriction, and the old
                    # snapshot is invalid even if the old page survived.
                    current.panes[pane_id] = replace(now_pane, tabs={**now_pane.tabs, local_id: replace(now_tab, pending=0, generation=self._next())})
            if created:
                # Another owner report may have adopted the just-created tab
                # while navigation completed. Close only an actual orphan;
                # exclude new bindings during cleanup without holding our lock
                # across Chrome. Existing bound tabs are never cleaned up here.
                with self._lock:
                    orphan = not any(tab.tab_id == created for state in (*self._windows.values(), *self._dormant.values())
                                     for pane in state.panes.values() for tab in pane.tabs.values())
                    if orphan:
                        self._retiring.add(created)
                if orphan:
                    try:
                        self.host.close_tab(created)
                    except Exception:
                        pass  # host shutdown/cleanup already owns the orphan
                    finally:
                        with self._lock:
                            self._retiring.discard(created)
