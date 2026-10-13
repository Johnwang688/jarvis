"""Memory-only dev-server registry and lazy agent listener watch (AO-1b-i).

The rules and all process reads belong to devlisten. This module never logs
process facts, URLs, responses or tags, and publishes only §D.1's id record.
Embedded daemons have no reader unless their caller supplies fixture roots;
daemon2's main explicitly opts into the machine's proc and cgroup roots.
"""
from __future__ import annotations

from collections import Counter, deque
from copy import deepcopy
from dataclasses import dataclass, field, replace
import logging
import os
import re
import secrets
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

from . import devlisten as dl
from .model import utcnow

LOG = logging.getLogger(__name__)

ENTRIES_MAX = 32
DOWN_KEEP_S = 60.0
CLAIM_LEASE_S = 90.0
SOURCE_RATE_S = 10.0
GLOBAL_RATE_S = 60.0
GLOBAL_RATE_MAX = 6
FACT_RETRIES = 3
_WINDOW = re.compile(r"[0-9a-f]{32}")


def boottime() -> float:
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def enabled(daemon) -> bool:
    """The single auto-open seam, backed by the daemon's BrowserSettings."""
    return daemon.browser_settings.auto_open()


def valid_window(value) -> bool:
    return isinstance(value, str) and _WINDOW.fullmatch(value) is not None


@dataclass
class Entry:
    id: str
    port: int
    scheme: str
    path: str
    source: dict
    first_seen: float
    state: str = "up"
    listening: bool = True
    window: str | None = None
    lease_until: float | None = None
    down_at: float | None = None
    prior_state: str = "up"
    misses: int = 0
    claims: list = field(default_factory=list)
    claim_until: float | None = None
    delivery: object | None = field(default=None, repr=False)

    @property
    def url(self):
        return f"{self.scheme}://localhost:{self.port}{self.path}"


@dataclass
class Candidate:
    listener: dl.Listener
    facts: list | None = None
    complete: bool = False
    retries: int = 0
    probing: bool = False
    probe: dl.Probe | None = None
    scheme: str = "http"
    attempts: int = 0
    next_probe: float = 0.0
    done: bool = False


class DevServers:
    def __init__(self, daemon, *, proc_root=None, cgroup_root=None, clock=boottime):
        self.daemon = daemon
        self.clock = clock
        self.reader = (dl.ProcReader(proc_root, cgroup_root or "/sys/fs/cgroup")
                       if proc_root is not None else None)
        self.sessions = dl.Sessions()
        self._lock = threading.RLock()
        self._poll_lock = threading.Lock()
        self._faulted = False
        self._errors = Counter()
        self._logged_errors = set()
        self._changed = threading.Condition(self._lock)
        self._wake = threading.Event()
        self._stopped = False
        self._thread = None
        self._baseline = None
        self._cgroup = None
        self._ephemeral = dl.EPHEMERAL_FALLBACK
        self._candidates = {}
        self._entries = {}                 # one entry per port, including down tombstones
        self._rates = {}
        self._recent = deque()
        self._running = set()
        self._ended = {}
        self._probes = set()
        self._sockets = set()
        self._polls = self._probe_count = 0
        self._refused = Counter()

    def enabled(self):
        return enabled(self.daemon)

    def session_opened(self, thread_id, task_id=None):
        tag = secrets.token_hex(16)
        with self._lock:
            self.sessions.open(tag, thread_id, task_id, at=self.clock())
        return tag

    def session_closed(self, tag):
        if tag is not None:
            with self._lock:
                self.sessions.close(tag, at=self.clock())

    def start(self):
        """Called by Daemon.start; no thread or proc read until a turn wakes it."""
        self._wake.clear()

    def turn_started(self, thread_id, provider):
        with self._lock:
            if not self.sessions.turn_started(thread_id, provider, at=self.clock()):
                return
            self._faulted = False
            self._running.add(thread_id)
            self._ended.pop(thread_id, None)
            self._ensure_thread()

    def turn_ended(self, thread_id):
        with self._lock:
            self.sessions.turn_ended(thread_id, at=self.clock())
            if thread_id in self._running:
                self._running.remove(thread_id)
                self._ended[thread_id] = self.clock()
            self._wake.set()

    def settings_changed(self):
        with self._lock:
            self._faulted = False
            self._ensure_thread()
            self._wake.set()
            if not self.enabled():
                for sock in list(self._sockets):
                    self._close_socket(sock)
                # OFF revokes a placement still waiting on a window/card,
                # including an arbitration in progress. ON cannot revive it.
                for entry in self._entries.values():
                    if entry.listening and entry.state == "claimed":
                        self._lapse(entry)
                    else:
                        entry.claims.clear()
                        entry.claim_until = None
                self._changed.notify_all()

    def _ensure_thread(self):
        if self._stopped or self._faulted or self.reader is None:
            return
        if not self.enabled():
            self._wake.set()
            return
        if self.cadence() is None:
            return
        if self._thread is not None and self._thread.is_alive():
            self._wake.set()
            return
        self._thread = threading.Thread(target=self._run, name="jarvis-devwatch", daemon=True)
        self._thread.start()

    def cadence(self, now=None):
        now = self.clock() if now is None else now
        if self._running or any(now - end <= 120 for end in self._ended.values()):
            return 1.0
        if self.sessions.live(now) or any(e.listening for e in self._entries.values()):
            return 5.0
        if any(not c.done for c in self._candidates.values()):
            return 5.0                              # finish candidates first seen inside the window
        return None

    def _run(self):
        failed = False
        try:
            while True:
                with self._lock:
                    if self._stopped or not self.enabled():
                        return
                    delay = self.cadence()
                    self._wake.clear()
                if delay is None:
                    self._wake.wait()             # parked, no polling while idle
                else:
                    self.poll()
                    self._wake.wait(delay)
        except Exception as exc:
            failed = True
            kind = type(exc).__name__
            with self._lock:
                self._faulted = True
                self._errors[kind] += 1
                if type(exc) not in self._logged_errors:
                    self._logged_errors.add(type(exc))
                    LOG.warning("Dev watcher failed (%s)", kind)
        finally:
            with self._lock:
                self._thread = None
                if not self.enabled():
                    for sock in list(self._sockets):
                        self._close_socket(sock)
                elif not self._stopped and not failed:
                    self._ensure_thread()           # a rapid off/on while this thread ended

    def _known(self):
        pids, leaders, gate_port = self.daemon._dev_known()
        return dl.Known(dl.port_rules(hud_port=self.daemon.face_port,
                                      api_port=self.daemon.port,
                                      workshop_port=self.daemon.workshop_port,
                                      gate_port=gate_port, ephemeral=self._ephemeral),
                        os.getuid(), frozenset(pids), frozenset(leaders))

    def poll(self):
        """Snapshot, read proc without the registry lock, then re-check and apply."""
        # Only polls serialize here; lifecycle hooks and routes use _lock alone.
        with self._poll_lock:
            self._poll()

    def _poll(self):
        with self._lock:
            if self._stopped or self.reader is None or not self.enabled():
                return
            now = self.clock()
            self._expire(now)
            if self.cadence(now) is None:
                return
            baseline_needed = self._baseline is None
        rows = self.reader.net_rows()
        keys = {(r.inode, r.port) for r in rows}
        if baseline_needed:
            cgroup = self.reader.own_cgroup()
            ephemeral = self.reader.ephemeral()
        known = self._known()
        browser_roots = self.daemon._dev_browser_roots()
        with self._lock:
            if self._stopped or not self.enabled():
                return
            self._polls += 1
            if baseline_needed:
                self._cgroup, self._ephemeral, self._baseline = cgroup, ephemeral, keys
                self._liveness({r.port for r in rows}, now)
                return
            self._baseline.intersection_update(keys)
            self._liveness({r.port for r in rows}, now)
            for key in self._candidates.keys() - keys:
                c = self._candidates.pop(key)
                if not c.done and c.listener.last_seen - c.listener.first_seen < dl.MIN_LIFE_S:
                    self._refused[dl.BRIEF] += 1
            detect = self.sessions.live(now)
            for row in rows:
                key = (row.inode, row.port)
                if key in self._baseline:
                    continue
                if key not in self._candidates:
                    if not detect:
                        continue
                    c = Candidate(dl.Listener(row, now, now))
                    self._candidates[key] = c
                    refusal = dl.cheap_refusal(row, known.ports, known.uid)
                    if refusal:
                        self._drop(c, refusal)
                else:
                    c = self._candidates[key]
                    c.listener = dl.Listener(row, c.listener.first_seen, now)
            snapshots = [(c, replace(c)) for c in self._candidates.values() if not c.done]
            sessions = deepcopy(self.sessions)
            cgroup = self._cgroup
        pending = [c for _, c in snapshots]
        need = [c for c in pending if c.facts is None]
        if need:
            pids = self.reader.cgroup_pids(cgroup) if cgroup else []
            owners = self.reader.socket_owners(pids, [c.listener.row.inode for c in need])
            facts = {}
            for c in need:
                c.complete = owners.complete
                if not owners.complete:
                    continue
                holders = owners.get(c.listener.row.inode, [])
                for pid in holders:
                    if pid not in facts:
                        facts[pid] = self.reader.facts(pid, stop=known.jarvis_pids - browser_roots)
                c.facts = [facts[p] for p in holders]
        chrome = {f.pid for c in pending for f in (c.facts or []) if f is not None
                  and (f.ppid in browser_roots or any(p in browser_roots for p, _ in f.ancestors))}
        if chrome:
            known = dl.Known(known.ports, known.uid, known.jarvis_pids | chrome, known.terminal_leaders)
        for original, c in snapshots:
            tags = {}
            def tag_of(f):
                key = (f.pid, f.start)
                if key not in tags:
                    tags[key] = self.reader.tag(f)
                return tags[key]
            verdict, holds = None, False
            if not c.probing and c.facts is not None:
                verdict = dl.attribute(c.listener, c.facts, sessions, now, known,
                                       tag_of=tag_of, probe=c.probe, scan_complete=c.complete)
                if verdict.attributed or verdict.pending == "probe":
                    holds = self._still_holds(verdict.pid, verdict.start, c.listener.row.inode)
            # Re-read identity/fds immediately before applying this candidate;
            # no other candidate's proc work intervenes before publication.
            current_known = self._known()
            current_known = dl.Known(current_known.ports, current_known.uid,
                                    current_known.jarvis_pids | chrome, current_known.terminal_leaders)
            with self._lock:
                if self._stopped or not self.enabled():
                    return
                snapshot = c
                key = (snapshot.listener.row.inode, snapshot.listener.row.port)
                c = self._candidates.get(key)
                if (c is not original or c.done or c.listener != snapshot.listener
                        or c.probing != snapshot.probing or c.probe is not snapshot.probe):
                    continue
                c.facts, c.complete = snapshot.facts, snapshot.complete
                if c.probe is None and now - c.listener.first_seen >= dl.PROBE_BUDGET_S:
                    self._drop(c, dl.NOT_HTTP)
                    continue
                if verdict is None:
                    continue
                fresh = dl.attribute(c.listener, c.facts, self.sessions, now, current_known,
                                     tag_of=lambda f: tags.get((f.pid, f.start), dl.UNREADABLE),
                                     probe=c.probe, scan_complete=c.complete)
                # A changed owner cannot reuse another identity's fd re-check.
                holds = holds and (fresh.pid, fresh.start) == (verdict.pid, verdict.start)
                verdict = fresh
                if verdict.refusal in (dl.OUTSIDE, dl.UNREADABLE):
                    c.retries += 1
                    c.facts = None
                    if c.retries >= FACT_RETRIES:
                        self._drop(c, verdict.refusal)
                elif verdict.outcome == "refused":
                    self._drop(c, verdict.refusal)
                elif verdict.attributed:
                    if not holds:
                        self._drop(c, dl.UNREADABLE)
                        continue
                    self._register(c.listener.row.port, c.scheme, "/", {
                        "kind": "thread", "thread_id": verdict.thread_id, "task_id": verdict.task_id},
                        c.listener.first_seen)
                    c.done = True
                elif verdict.pending == "probe":
                    if now >= c.next_probe and len(self._probes) < dl.PROBES_IN_FLIGHT:
                        if not holds:
                            self._drop(c, dl.UNREADABLE)
                            continue
                        c.probing = True
                        worker = threading.Thread(target=self._probe, args=(c,),
                                                  name="jarvis-devprobe", daemon=True)
                        self._probes.add(worker)
                        worker.start()
        with self._lock:
            self.sessions.prune(now - dl.PROBE_BUDGET_S)

    def _still_holds(self, pid, start, inode):
        before = self.reader.stat(pid)
        if before is None or before.start != start:
            return False
        owners = self.reader.socket_owners([pid], [inode])
        after = self.reader.stat(pid)
        return (owners.complete and pid in owners.get(inode, [])
                and after is not None and after.start == start)

    def _drop(self, candidate, refusal):
        candidate.done = True
        self._refused[refusal] += 1

    @staticmethod
    def _close_socket(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

    def _exchange(self, c, tls=False):
        # Check the current control/gate ports again immediately before connect.
        with self._lock:
            if self._stopped or not self.enabled():
                raise OSError()
            address = dl.probe_address(c.listener.row, self._known().ports, os.getuid())
            if address is None:
                return dl.Probe(dl.JARVIS_PORT)
            self._probe_count += 1
            sock = socket.socket(socket.AF_INET6 if address[0] == "::1" else socket.AF_INET)
            self._sockets.add(sock)
        deadline = time.monotonic() + min(dl.PROBE_RESPONSE_S,
            max(0.01, dl.PROBE_BUDGET_S - (self.clock() - c.listener.first_seen)))
        try:
            sock.settimeout(min(dl.PROBE_CONNECT_S, max(0.01, deadline - time.monotonic())))
            sock.connect(address)
            if tls:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                with self._lock:
                    self._sockets.discard(sock)
                    sock = context.wrap_socket(sock, server_hostname="localhost", do_handshake_on_connect=False)
                    self._sockets.add(sock)
                sock.settimeout(max(0.01, deadline - time.monotonic()))
                sock.do_handshake()
            sock.settimeout(max(0.01, deadline - time.monotonic()))
            sock.sendall(dl.probe_request(address[1]))
            data = bytearray()
            while len(data) < dl.PROBE_READ_MAX:
                sock.settimeout(max(0.01, deadline - time.monotonic()))
                try:
                    chunk = sock.recv(dl.PROBE_READ_MAX - len(data))
                except socket.timeout:
                    if data:
                        break                       # judge received facts; never retain bytes on a timeout
                    raise
                if not chunk:
                    break
                data.extend(chunk)
                # A typed 2xx page or redirect needs no body; a negative status
                # needs markup, so keep reading up to the cap/deadline.
                parsed = dl.parse_response(data)
                if parsed.retry_tls or parsed.refusal is None or parsed.refusal == dl.NO_FRAME:
                    break
                if time.monotonic() >= deadline:
                    break
            return dl.parse_response(data)
        finally:
            with self._lock:
                self._sockets.discard(sock)
            self._close_socket(sock)

    def _probe(self, c):
        result, scheme = None, "http"
        try:
            result = self._exchange(c)
            if result.retry_tls:
                result = self._exchange(c, tls=True)      # exactly one loopback TLS fallback
                scheme = "https"
        except OSError:
            pass                                        # only a class survives, never exception text
        finally:
            with self._lock:
                c.probing = False
                self._probes.discard(threading.current_thread())
                if not self._stopped and self.enabled():
                    if result is not None:
                        c.probe, c.scheme = result, scheme
                    elif c.attempts < len(dl.PROBE_RETRY_S):
                        c.next_probe = self.clock() + dl.PROBE_RETRY_S[c.attempts]
                        c.attempts += 1
                    else:
                        self._drop(c, dl.NOT_HTTP)
                self._wake.set()

    def _publish(self, entry):
        self.daemon.bus.publish({"kind": "dev_server", "data": {
            "id": entry.id, "state": entry.state, "source": dict(entry.source), "at": utcnow()}})
        self._changed.notify_all()

    def _register(self, port, scheme, path, source, first_seen, window=None, delivery=None):
        now = self.clock()
        self._expire(now)
        previous = self._entries.get(port)
        if previous is not None:
            if previous.listening:
                return previous
            if previous.down_at is not None and now - previous.down_at <= DOWN_KEEP_S:
                previous.listening, previous.misses = True, 0
                previous.state = "up" if previous.prior_state == "claimed" else previous.prior_state
                previous.down_at = None
                if previous.source == source and source["kind"] == "terminal":
                    previous.first_seen = first_seen       # a fresh banner gets its startup grace
                return previous                          # same id, first source, no second up
        key = (("task", source["task_id"]) if source.get("task_id")
               else (source["kind"], source.get("thread_id", source.get("terminal_id"))))
        while self._recent and now - self._recent[0] >= GLOBAL_RATE_S:
            self._recent.popleft()
        if (now - self._rates.get(key, float("-inf")) < SOURCE_RATE_S
                or len(self._recent) >= GLOBAL_RATE_MAX or len(self._entries) >= ENTRIES_MAX):
            self._refused["rate"] += 1
            return None
        entry_id = "ds_" + secrets.token_hex(4)
        while any(e.id == entry_id for e in self._entries.values()):
            entry_id = "ds_" + secrets.token_hex(4)
        entry = Entry(entry_id, port, scheme, path, dict(source), first_seen)
        if window is not None:
            entry.window, entry.lease_until, entry.state = window, now + CLAIM_LEASE_S, "claimed"
            entry.delivery = delivery
        self._entries[port] = entry
        self._rates[key] = now
        self._recent.append(now)
        self._publish(entry)
        self._ensure_thread()
        return entry

    def announce_terminal(self, terminal_id, url, *, window=None, delivery=None, refresh=False):
        """Retain a cleaned banner, preclaiming only its attached window.

        Never probes. The first source on a port wins. A delivery receipt is
        private to the terminal reader, so an old failed send cannot release
        a later claim, even when both attachments use the same page nonce.
        A deduplicated banner refreshes a same-source entry, or registers an
        unclaimed entry after expiry under the normal caps. Never a preclaim,
        frame, replacement of another source, or second placement of a live id.
        """
        if not self.enabled() or self._stopped:
            return None
        from .devurl import clean
        url = clean(url, refused=self._known().ports.jarvis)
        if url is None or (window is not None and not valid_window(window)):
            return None
        parsed = urlsplit(url)
        source = {"kind": "terminal", "terminal_id": terminal_id}
        with self._lock:
            if not self.enabled() or self._stopped:
                return None
            self._expire(self.clock())
            previous = self._entries.get(parsed.port)
            if refresh:
                if previous is not None and previous.source != source:
                    return None
                # Retained entries keep id, path, source and prior state.
                # After expiry, normal caps govern a new unclaimed entry.
                # Neither path preclaims a window or sends another frame.
                e = self._register(parsed.port, parsed.scheme, parsed.path or "/",
                                   source, self.clock())
                return e.id if e is not None else None
            if (previous is not None and (window is not None or delivery is not None)
                    and (previous.source != source or previous.state in ("dismissed", "placed")
                         or (previous.state == "claimed"
                             and (previous.window != window or previous.delivery is not delivery)))):
                return None
            e = self._register(parsed.port, parsed.scheme, parsed.path or "/",
                               source, self.clock(), window, delivery)
            if e is None:
                return None
            if delivery is not None and (e.source != source or e.state in ("dismissed", "placed")):
                return None
            if window is not None:
                if e.source != source or not e.listening:
                    return None
                if e.state == "up":
                    e.window, e.lease_until, e.state = window, self.clock() + CLAIM_LEASE_S, "claimed"
                    e.delivery = delivery
                    self._publish(e)
                elif e.state != "claimed" or e.window != window or e.delivery is not delivery:
                    return None
            return e.id

    def deliver_terminal(self, terminal_id, entry_id, window, delivery, enqueue):
        """Queue only while this preclaim is current, before any competing claim.

        The enqueue closure only takes the terminal lock and queues a frame;
        terminal code never acquires the registry lock while holding that lock.
        """
        with self._lock:
            self._expire(self.clock())
            e = next((e for e in self._entries.values() if e.id == entry_id), None)
            if (not self.enabled() or self._stopped or e is None or not e.listening
                    or e.state != "claimed" or e.window != window or e.delivery is not delivery
                    or e.source != {"kind": "terminal", "terminal_id": terminal_id}):
                return False
            return enqueue()

    def terminal_delivery_failed(self, terminal_id, entry_id, window, delivery):
        """Release just the still-current preclaim whose socket refused it."""
        with self._lock:
            e = next((e for e in self._entries.values() if e.id == entry_id), None)
            if (e is not None and e.listening and e.state == "claimed" and e.window == window
                    and e.source == {"kind": "terminal", "terminal_id": terminal_id}
                    and e.delivery is delivery):
                self._lapse(e)

    def _liveness(self, ports, now):
        for e in self._entries.values():
            if not e.listening or (e.source["kind"] == "terminal" and now - e.first_seen < 5):
                continue
            e.misses = 0 if e.port in ports else e.misses + 1
            if e.misses >= 2:
                e.listening, e.down_at = False, now
                e.prior_state, e.state = e.state, "down"
                e.window = e.lease_until = None
                e.delivery = None
                e.claims.clear()
                e.claim_until = None
                self._publish(e)

    def _expire(self, now):
        for port, e in list(self._entries.items()):
            if e.listening and e.state == "claimed" and e.lease_until is not None and now >= e.lease_until:
                self._lapse(e)
            if not e.listening and e.down_at is not None and now - e.down_at > DOWN_KEEP_S:
                del self._entries[port]
        self._ended = {t: end for t, end in self._ended.items() if now - end <= dl.GRACE_S}
        self._rates = {s: at for s, at in self._rates.items() if now - at < GLOBAL_RATE_S}

    def _lapse(self, e):
        e.state, e.window, e.lease_until = "up", None, None
        e.delivery = None
        e.claims.clear()
        e.claim_until = None
        self._publish(e)

    def window_ended(self, window):
        with self._lock:
            for e in self._entries.values():
                if e.state == "claimed" and e.window == window and e.listening:
                    self._lapse(e)
                e.claims = [c for c in e.claims if c.window != window]
            self._changed.notify_all()

    def listing(self, window=None):
        with self._lock:
            self._expire(self.clock())
            return {"auto_open": self.enabled(), "servers": [{"id": e.id, "url": e.url, "state": e.state,
                "source": dict(e.source), "claimed": e.window is not None, "mine": e.window == window
                if window is not None else False} for e in self._entries.values()
                if e.listening and e.state != "dismissed"]}

    def _entry(self, entry_id):
        from .daemon import APIError
        self._expire(self.clock())
        entry = next((e for e in self._entries.values() if e.id == entry_id), None)
        if entry is None:
            raise APIError(404, "dev server not found")
        return entry

    def claim(self, entry_id, window, shows, focused_at):
        # The route validates every field before making this Claim.
        with self._changed:
            if not self.enabled():
                return 409, {"error": "automatic opening is off", "code": "off"}
            e = self._entry(entry_id)
            if not e.listening or e.state in ("down", "dismissed"):
                return 409, {"error": "dev server is gone", "code": "gone"}
            if e.state != "up" or self._stopped:
                return 200, {"won": False}
            arrived = time.monotonic()
            if e.claim_until is None:
                e.claim_until = arrived + dl.ARBITRATE_MS / 1000
            cutoff = e.claim_until
            e.claims.append(dl.Claim(window, shows, focused_at, arrived))
            while e.state == "up" and e.claim_until == cutoff and not self._stopped:
                left = cutoff - time.monotonic()
                if left <= 0:
                    winner = dl.arbitrate(e.claims)
                    if winner is not None:
                        e.window, e.lease_until, e.state = winner.window, self.clock() + CLAIM_LEASE_S, "claimed"
                        e.delivery = None
                        self._publish(e)
                    else:
                        e.claim_until = None
                    break
                self._changed.wait(left)
            if not self.enabled():
                return 409, {"error": "automatic opening is off", "code": "off"}
            if not e.listening or e.state in ("down", "dismissed"):
                return 409, {"error": "dev server is gone", "code": "gone"}
            won = e.state == "claimed" and e.window == window and not self._stopped
            return 200, {"won": True, "url": e.url} if won else {"won": False}

    def act(self, entry_id, action, window):
        from .daemon import APIError
        with self._lock:
            if action != "dismiss" and not self.enabled():
                return 409, {"error": "automatic opening is off", "code": "off"}
            e = self._entry(entry_id)
            if not e.listening or e.state in ("down", "dismissed"):
                return 409, {"error": "dev server is gone", "code": "gone"}
            if action != "dismiss" and (e.state != "claimed" or e.window != window):
                raise APIError(409, "this window does not hold the claim")
            e.delivery = None
            if action == "renew":
                e.lease_until = self.clock() + CLAIM_LEASE_S
            else:
                e.state = "placed" if action == "placed" else "dismissed"
                e.lease_until = None
                self._publish(e)
            return 200, {}

    def note_hook_error(self, kind):
        """A daemon hook into the watcher raised: counted for `/status`, as a
        poll's own fault is (delta review of PR #79, NIT)."""
        with self._lock:
            self._errors["hook:" + kind] += 1

    def status(self):
        with self._lock:
            work = self.reader.work if self.reader else dl.Work()
            return {"active": bool(self.enabled() and not self._stopped and not self._faulted
                                   and self.cadence() is not None and self.reader is not None),
                    "errors": dict(self._errors),
                    "entries": sum(e.listening for e in self._entries.values()), "polls": self._polls,
                    "refused": dict(self._refused), "work": {"rows": work.rows,
                    "fd_readlinks": work.fd_readlinks, "environ_reads": work.environ_reads,
                    "probes": self._probe_count}}

    def stop(self):
        with self._lock:
            self._stopped = True
            self._wake.set()
            self._changed.notify_all()
            for sock in list(self._sockets):
                self._close_socket(sock)
            jobs = [self._thread, *self._probes]
        deadline = time.monotonic() + 2
        for job in jobs:
            if job is not None:
                job.join(max(0, deadline - time.monotonic()))


def route(handler, daemon, parts, query):
    if not parts or parts[0] != "dev-servers":
        return None
    from .daemon import APIError, _object
    from .projects import owner_only
    owner_only(handler, daemon)                       # before body, id or registry lookup
    port = daemon.face_port
    if handler.headers.get("Host", "") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
        raise APIError(403, "untrusted Host")
    registry = daemon.devservers
    if parts == ["dev-servers"] and handler.command == "GET":
        _object(query, ("window",))
        window = query.get("window")
        if window is not None and not valid_window(window):
            raise APIError(400, "invalid window")
        return 200, registry.listing(window)
    _object(query, ())
    if (len(parts) != 3 or handler.command != "POST"
            or parts[2] not in ("claim", "renew", "placed", "dismiss")):
        raise APIError(404, "route not found")
    action = parts[2]
    keys = ("window", "shows", "focused_at") if action == "claim" else ("window",)
    body = _object(handler._body(), keys, keys)
    if not valid_window(body["window"]):
        raise APIError(400, "invalid window")
    if action == "claim":
        focused = body["focused_at"]
        if (not isinstance(body["shows"], bool) or type(focused) is not int
                or not 0 <= focused <= dl.FOCUSED_AT_MAX):
            raise APIError(400, "invalid claim")
        return registry.claim(parts[1], body["window"], body["shows"], focused)
    return registry.act(parts[1], action, body["window"])
