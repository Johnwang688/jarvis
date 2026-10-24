"""The Browser's SOCKS5 gate (BR-1a, plan §4 layer 2): the boundary between
the owner's daemon-owned Chrome and the network.

Chrome is launched with `--proxy-server=socks5://127.0.0.1:<this port>` and
`--proxy-bypass-list=<-loopback>`, so every TCP connection it makes — a page,
a redirect, a frame, fetch, XHR, a WebSocket, a worker, a service worker,
prefetch and Chrome's own background traffic — arrives here as a SOCKS5
CONNECT, loopback included. Nothing leaves around it: QUIC is off
(`--disable-quic`), WebRTC may send no UDP that does not go through a proxy
(`--webrtc-ip-handling-policy=disable_non_proxied_udp` — a SOCKS proxy
carries no UDP, so WebRTC gathers no candidate and sends no STUN or ICE
check; review of PR #69: the first launch named a flag Chrome does not have,
and 158 datagrams went straight out), WebRTC's mDNS responder is off (it
joined the mDNS group on every interface for any `RTCPeerConnection`), the
media router's LAN discovery is off, and `MAP * ~NOTFOUND` keeps Chrome from
sending a DNS query of its own (defense in depth: with the proxy every name
is sent to the gate anyway). Measured in a private network namespace with an
AF_PACKET sniffer: nothing leaves on any interface but `lo`
(`owner_browser_check`, `JARVIS_NETNS_SNIFF=1`).
For each connection the gate:

- refuses Jarvis's own ports (`policy.refused_ports`: BR-0's control ports,
  the HUD and API ports the daemon reports, the workshop's 8403 unless it is
  the daemon's own workshop) and its own port, **on every address**, before
  any name is resolved;
- reads the host the way Chrome sends it — always as a name, IP literals
  included (measured: Chrome 155 sends `127.0.0.1`, `::1`, `0.0.0.0` and
  `::ffff:7f00:1` as domain names) — parsing literals in every spelling an
  IPv4 parser accepts, and treating `localhost` and `*.localhost` as this
  machine without DNS, as Chrome does;
- **resolves a real name exactly once and connects to an address it judged**
  (its own socket, `sock_connect` to the numeric address: nothing resolves
  the name a second time), so DNS rebinding between check and connect has
  nowhere to land;
- refuses a name that resolves to loopback (rebinding onto the owner's own
  services), private-network addresses unless the owner's LAN switch is on
  (B-5), multicast and reserved space, and unresolvable names;
- **closes this machine like the LAN** (the lead's decision on PR #69, MED
  2): behind a proxy Chrome's Local Network Access check no longer protects
  loopback, so a loopback port is reachable only after the owner opened it
  in a pane (`loopback(port)`, the host's per-port allowance), and every
  other is `loopback_closed`;
- **pins each name's address class** for its lifetime: a name first
  resolved to public addresses never later connects to a private one, and
  one first resolved private (the LAN switch on) stays private — rebinding
  on a later answer is `rebinding`. A pin younger than `PIN_YOUNG_S` is never
  forgotten to make room (a page cannot flush its own pin by resolving 8,192
  other names); while the table is full of young pins, an unpinned name may
  not reach private space (`pins_full`);
- resolves through a bounded pool: one lookup per name in flight (others
  share it), at most `DOMAIN_LOOKUPS` threads per domain (`domain_key`: the
  registrable domain where a small suffix table can tell, else the name's
  parent), a lookup past that **waiting** for a slot within its
  `RESOLVE_TIMEOUT_S` (only then `dns_busy`), and a positive answer reused
  for `CACHE_S` — still judged, and still held to the name's pin, like a
  fresh one. A page aiming lookups at a black-holed name server cannot starve
  every other tab, and a page with many subdomains of one site waits rather
  than fails (delta review of PR #69: refusing at once failed 6 of 8 images
  on one CDN, and every `*.co.uk` shared two slots);
- allows public addresses and the loopback ports the owner opened, then
  pipes bytes both ways.

It logs and counts the class of each refusal, never a host, a port or a URL.
It binds loopback only and asks no credentials: any program running as the
owner could use it, and could equally connect directly, so it is no new
lever. `resolver` and `connector` are seams for the suites, which never let
a name reach DNS or an address reach the network.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import threading
import time
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from . import policy

LOG = logging.getLogger("jarvis.v2.browser.gate")

HANDSHAKE_TIMEOUT_S = 10.0
RESOLVE_TIMEOUT_S = 5.0
CONNECT_TIMEOUT_S = 15.0
# DNS on worker threads: a getaddrinfo the gate has stopped waiting for still
# holds its thread until glibc gives up, so the pool is sized for the worst
# case and each domain may hold only a few of its threads at once; a lookup
# past that waits for one of them, within its own RESOLVE_TIMEOUT_S.
DNS_WORKERS = 32
DOMAIN_LOOKUPS = 4
SLOT_GRACE_S = 0.1
# A positive answer is reused this long (and judged again each time).
CACHE_S = 5.0
CACHE_CAP = 1024
# Names whose address class is pinned, oldest forgotten first — but never
# one pinned less than PIN_YOUNG_S ago.
PIN_CAP = 8192
PIN_YOUNG_S = 30 * 60
# Once one direction of a piped connection has ended, the other must keep
# moving: a server that never answers a half-close is dropped after this.
HALF_CLOSED_IDLE_S = 60.0
MAX_CONNECTIONS = 256
CHUNK = 64 * 1024

# Two-label public suffixes under which the registrable domain is three
# labels — the common ccTLD second levels and the hosting platforms whose
# every customer is a subdomain. Not the Public Suffix List (no dependency):
# what it misses under a TLD in STRUCTURED_TLDS is keyed on the name's
# parent, and anywhere else on the last two labels, which only ever makes a
# lookup wait for a slot, never refuses it early.
MULTI_LABEL_SUFFIXES = frozenset({
    "co.uk", "org.uk", "me.uk", "ac.uk", "gov.uk", "net.uk", "ltd.uk", "plc.uk", "sch.uk", "nhs.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "asn.au", "id.au",
    "co.jp", "ne.jp", "or.jp", "ac.jp", "go.jp", "ad.jp", "ed.jp", "gr.jp", "lg.jp",
    "com.br", "net.br", "org.br", "gov.br", "edu.br",
    "co.nz", "net.nz", "org.nz", "govt.nz", "ac.nz", "school.nz",
    "co.za", "org.za", "gov.za", "ac.za", "net.za",
    "co.in", "net.in", "org.in", "gov.in", "ac.in", "edu.in", "firm.in", "gen.in", "ind.in",
    "co.kr", "or.kr", "go.kr", "ac.kr", "ne.kr",
    "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn",
    "com.tw", "net.tw", "org.tw", "edu.tw", "gov.tw",
    "com.hk", "net.hk", "org.hk", "edu.hk", "gov.hk",
    "com.sg", "net.sg", "org.sg", "edu.sg", "gov.sg",
    "com.my", "net.my", "org.my", "edu.my", "gov.my",
    "co.id", "or.id", "ac.id", "go.id", "web.id",
    "co.th", "or.th", "ac.th", "go.th", "in.th",
    "co.il", "org.il", "net.il", "ac.il", "gov.il",
    "com.ar", "net.ar", "org.ar", "gob.ar", "edu.ar",
    "com.mx", "net.mx", "org.mx", "gob.mx", "edu.mx",
    "com.tr", "net.tr", "org.tr", "gov.tr", "edu.tr",
    "com.ua", "net.ua", "org.ua", "gov.ua", "edu.ua",
    "co.ke", "or.ke", "ac.ke", "go.ke",
    "com.ng", "org.ng", "gov.ng", "edu.ng",
    "com.pk", "org.pk", "gov.pk", "edu.pk",
    "com.ph", "org.ph", "gov.ph", "edu.ph",
    "com.vn", "net.vn", "org.vn", "gov.vn", "edu.vn",
    "com.eg", "org.eg", "gov.eg", "edu.eg",
    "com.sa", "org.sa", "gov.sa", "edu.sa",
    # Hosting platforms: every customer is a subdomain of one of these.
    "github.io", "gitlab.io", "herokuapp.com", "vercel.app", "netlify.app", "pages.dev",
    "workers.dev", "web.app", "firebaseapp.com", "appspot.com", "blogspot.com",
    "cloudfront.net", "azurewebsites.net", "fly.dev", "onrender.com", "glitch.me",
    "repl.co", "ngrok.io", "ngrok-free.app", "trycloudflare.com", "readthedocs.io",
})
# TLDs whose second level is often a public suffix: a name under one whose
# last two labels are not in the table above is keyed on its parent.
STRUCTURED_TLDS = frozenset({
    "uk", "au", "jp", "br", "nz", "za", "in", "kr", "cn", "tw", "hk", "sg", "my", "id",
    "th", "il", "ar", "mx", "tr", "ua", "ke", "ng", "pk", "ph", "vn", "eg", "sa",
})


def domain_key(name: str) -> str:
    """Which domain a lookup for `name` counts against (`DOMAIN_LOOKUPS`):
    the registrable domain where the suffix table can tell (`www.bbc.co.uk`
    → `bbc.co.uk`, `a.b.example.com` → `example.com`), else — under a TLD
    whose second level the table does not know — the name's parent."""
    labels = name.lower().rstrip(".").split(".")
    if len(labels) <= 2:
        return ".".join(labels)
    if ".".join(labels[-2:]) in MULTI_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    if labels[-1] in STRUCTURED_TLDS:
        return ".".join(labels[1:])
    return ".".join(labels[-2:])


# RFC 1928 reply codes.
REP_OK = 0x00
REP_GENERAL = 0x01
REP_NOT_ALLOWED = 0x02
REP_HOST_UNREACHABLE = 0x04
REP_REFUSED = 0x05
REP_BAD_COMMAND = 0x07
REP_BAD_ATYP = 0x08

def default_resolve(name: str, port: int) -> list[str]:
    """The system resolver, once: every address `getaddrinfo` gives, in order."""
    out: list[str] = []
    for info in socket.getaddrinfo(name, port, type=socket.SOCK_STREAM):
        address = info[4][0]
        if address not in out:
            out.append(address)
    return out


async def default_connect(address: str, port: int):
    """A connection to exactly `address`, which is numeric: the socket is made
    here and connected to the address the gate judged, so nothing can resolve
    a name again on the way."""
    loop = asyncio.get_running_loop()
    ip = ipaddress.ip_address(address)
    family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setblocking(False)
    try:
        await asyncio.wait_for(loop.sock_connect(sock, (str(ip), port)), CONNECT_TIMEOUT_S)
    except BaseException:
        sock.close()
        raise
    return await asyncio.open_connection(sock=sock, limit=2 * CHUNK)


def _wake(waiter: asyncio.Future) -> None:
    if not waiter.done():
        waiter.set_result(None)


class Refusal(Exception):
    def __init__(self, reason: str, reply: int = REP_NOT_ALLOWED):
        super().__init__(reason)
        self.reason = reason
        self.reply = reply


class Gate:
    """One loopback SOCKS5 listener on its own thread and event loop
    (`jarvis-browser-gate`), so page loads never wait on the Browser's
    Playwright loop and a busy page never delays the gate's judgement.

    `refused()` returns the port set to refuse (read per connection);
    `lan()` the LAN switch, and `loopback(port)` whether the owner opened
    that loopback port (both read per connection). `resolver(name, port) ->
    [address]` is called at most once per connection, on a worker thread;
    `connector(address, port) -> (reader, writer)` is awaited for each
    allowed address in turn."""

    def __init__(self, *, refused: Callable[[], frozenset[int]] = frozenset,
                 lan: Callable[[], bool] = lambda: False,
                 loopback: Callable[[int], bool] = lambda port: False,
                 resolver=None, connector=None, max_connections: int = MAX_CONNECTIONS):
        self._refused = refused
        self._lan = lan
        self._loopback = loopback
        self._resolver = resolver or default_resolve
        self._connector = connector or default_connect
        self._max = max_connections
        # name -> ("public" | "private", when): the class it was first allowed on.
        self._pins: OrderedDict[str, tuple[str, float]] = OrderedDict()
        # name -> the lookup in flight (gate loop only); name -> (expires, addresses).
        self._lookups: dict[str, asyncio.Future] = {}
        self._cache: OrderedDict[str, tuple[float, list]] = OrderedDict()
        # domain -> threads busy (the workers' finally releases them), and the
        # lookups waiting for one of them (gate loop futures).
        self._domain_busy: Counter = Counter()
        self._slot_waiters: dict[str, list[asyncio.Future]] = {}
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None
        self._executor: ThreadPoolExecutor | None = None
        self._error: BaseException | None = None
        self._active = 0
        self._local_tasks: dict[asyncio.Task, int] = {}
        self._counts = Counter()
        self._refusals = Counter()
        # Launch checks (`expect_check`): name -> Event, set when that exact
        # name reaches the gate. Only pending check names are ever recorded.
        self._checks: dict[str, threading.Event] = {}
        self.port: int | None = None

    # -- lifecycle ------------------------------------------------------------------

    def start(self, timeout: float = 10.0) -> int:
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="jarvis-browser-gate", daemon=True)
                self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("the browser gate did not start")
        if self._error is not None:
            raise RuntimeError("the browser gate could not listen") from self._error
        assert self.port is not None
        return self.port

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        self._executor = ThreadPoolExecutor(max_workers=DNS_WORKERS,
                                            thread_name_prefix="jarvis-browser-dns")
        try:
            self._server = loop.run_until_complete(
                asyncio.start_server(self._client, "127.0.0.1", 0, limit=2 * CHUNK))
            self.port = self._server.sockets[0].getsockname()[1]
        except BaseException as exc:  # noqa: BLE001 - reported by start()
            self._error = exc
            self._ready.set()
            loop.close()
            return
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            # Every connection ends with the gate, including one accepted
            # while the others were being cancelled.
            for _ in range(10):
                tasks = [t for t in asyncio.all_tasks(loop) if not t.done()]
                if not tasks:
                    break
                for task in tasks:
                    task.cancel()
                loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            self._executor.shutdown(wait=False, cancel_futures=True)
            loop.close()

    def stop(self, timeout: float = 5.0) -> None:
        loop, thread = self._loop, self._thread
        if loop is not None and not loop.is_closed():
            def halt():
                if self._server is not None:
                    self._server.close()   # no new connection from here on
                loop.stop()
            try:
                loop.call_soon_threadsafe(halt)
            except RuntimeError:
                pass
        if thread is not None:
            thread.join(timeout)

    def revoke_closed_loopback(self) -> None:
        """Drop established/in-flight local tunnels whose last owner left.

        The host first removes its lease under its lock. The gate rechecks
        after every connect await too, so a new connect cannot race this call.
        Public/LAN tunnels are deliberately absent from this collection.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        def revoke():
            for task, port in list(self._local_tasks.items()):
                if not self._loopback(port):
                    task.cancel()
        try:
            loop.call_soon_threadsafe(revoke)
        except RuntimeError:
            pass

    def expect_check(self, name: str) -> threading.Event:
        """Arm a launch check: the returned Event is set when a CONNECT for
        exactly `name` arrives, which is then refused (it never leaves).
        A Chrome whose connections reach the gate sets it; one that a policy
        or anything else took around the gate never does."""
        event = threading.Event()
        with self._lock:
            self._checks[name.lower()] = event
        return event

    def forget_check(self, name: str) -> None:
        with self._lock:
            self._checks.pop(name.lower(), None)

    def stats(self) -> dict:
        with self._lock:
            return {"port": self.port, "connections": self._counts["connections"],
                    "allowed": self._counts["allowed"], "failed": self._counts["failed"],
                    "open": self._active, "refused": dict(sorted(self._refusals.items()))}

    # -- one connection ------------------------------------------------------------

    def _refused_count(self, reason: str) -> None:
        with self._lock:
            self._refusals[reason] += 1
        LOG.debug("browser gate refused a connection: %s", reason)

    async def _client(self, reader, writer) -> None:
        with self._lock:
            self._counts["connections"] += 1
            busy = self._active >= self._max
            if not busy:
                self._active += 1
        if busy:
            self._refused_count(policy.BUSY)
            writer.close()
            return
        upstream = None
        try:
            try:
                request = await asyncio.wait_for(self._handshake(reader, writer), HANDSHAKE_TIMEOUT_S)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError):
                return
            if request is None:
                return
            name, port = request
            with self._lock:
                check = self._checks.get(name.lower().rstrip("."))
            if check is not None:
                check.set()
                await self._reply(writer, REP_NOT_ALLOWED)
                return
            kind, literal = policy.host_kind(name)
            local = kind == "loopback_name" or (kind == "literal" and policy.address_class(literal) == "loopback")
            if local:
                self._local_tasks[asyncio.current_task()] = port
            try:
                upstream = await self._open(name, port)
            except Refusal as refusal:
                self._refused_count(refusal.reason)
                await self._reply(writer, refusal.reply)
                return
            if upstream is None:
                with self._lock:
                    self._counts["failed"] += 1
                await self._reply(writer, REP_REFUSED)
                return
            with self._lock:
                self._counts["allowed"] += 1
            await self._reply(writer, REP_OK)
            await self._splice(reader, writer, *upstream)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one connection never takes the gate down
            LOG.debug("browser gate connection ended: %s", type(exc).__name__)
        finally:
            self._local_tasks.pop(asyncio.current_task(), None)
            with self._lock:
                self._active -= 1
            for w in (writer, upstream[1] if upstream else None):
                if w is not None:
                    try:
                        w.close()
                    except Exception:  # noqa: BLE001
                        pass

    async def _reply(self, writer, code: int) -> None:
        try:
            writer.write(bytes([5, code, 0, 1, 0, 0, 0, 0, 0, 0]))
            await writer.drain()
        except (ConnectionError, OSError):
            pass

    async def _handshake(self, reader, writer):
        """(host name, port) from a SOCKS5 greeting and CONNECT, or None after
        refusing it. Every address type arrives as text: an IPv4 or IPv6
        address is judged exactly as the same literal sent as a name."""
        version, count = await reader.readexactly(2)
        if version != 5:
            self._refused_count(policy.BAD_REQUEST)
            return None
        methods = await reader.readexactly(count)
        if 0 not in methods:           # "no authentication" is the only method
            self._refused_count(policy.UNSUPPORTED)
            writer.write(b"\x05\xff")
            await writer.drain()
            return None
        writer.write(b"\x05\x00")
        await writer.drain()
        version, command, _reserved, atyp = await reader.readexactly(4)
        if version != 5:
            self._refused_count(policy.BAD_REQUEST)
            return None
        if atyp == 1:
            name = str(ipaddress.IPv4Address(await reader.readexactly(4)))
        elif atyp == 4:
            name = str(ipaddress.IPv6Address(await reader.readexactly(16)))
        elif atyp == 3:
            length = (await reader.readexactly(1))[0]
            raw = await reader.readexactly(length)
            try:
                name = raw.decode("ascii")
            except UnicodeDecodeError:
                name = ""
        else:
            self._refused_count(policy.UNSUPPORTED)
            await self._reply(writer, REP_BAD_ATYP)
            return None
        port = int.from_bytes(await reader.readexactly(2), "big")
        if command != 1:               # CONNECT only: no BIND, no UDP ASSOCIATE
            self._refused_count(policy.UNSUPPORTED)
            await self._reply(writer, REP_BAD_COMMAND)
            return None
        return name, port

    async def _open(self, name: str, port: int):
        """Judge (name, port) and connect to the first allowed address, or
        raise `Refusal`. None means every allowed address failed to connect."""
        refused = frozenset(self._refused())
        reason = policy.judge_port(port, refused=refused, own=self.port)
        if reason:
            raise Refusal(reason)
        kind, literal = policy.host_kind(name)
        if kind == "bad":
            raise Refusal(policy.BAD_REQUEST)
        key = name.lower().rstrip(".")
        if kind == "literal":
            candidates = [literal]
        elif kind == "loopback_name":
            candidates = [ipaddress.IPv4Address("127.0.0.1"), ipaddress.IPv6Address("::1")]
        else:
            candidates = await self._resolve(key, port)
        lan = bool(self._lan())
        loopback_open = bool(self._loopback(port))
        held = self._pins.get(key) if kind == "name" else None
        pinned = held[0] if held is not None else None
        allowed, first = [], None
        for ip in candidates:
            reason = policy.judge_address(ip, via=kind, lan=lan, loopback_open=loopback_open)
            if reason is None and pinned is not None and policy.address_class(ip) != pinned:
                reason = policy.REBINDING
            if reason is None:
                allowed.append(ip)
            elif first is None:
                first = reason
        if not allowed:
            raise Refusal(first or policy.UNRESOLVABLE)
        if kind == "name" and pinned is None:
            # The first answer this name was allowed on decides its class
            # for the gate's lifetime; a mixed answer keeps that class only.
            pin = policy.address_class(allowed[0])
            allowed = [ip for ip in allowed if policy.address_class(ip) == pin]
            if not self._pin(key, pin) and pin != "public":
                # No room to remember it: an unpinned name could rebind
                # later, so it may not reach private space now.
                raise Refusal(policy.PINS_FULL)
        for ip in allowed:
            try:
                connected = await self._connector(str(ip), port)
                if policy.address_class(ip) == "loopback" and not self._loopback(port):
                    connected[1].close()
                    raise Refusal(policy.LOOPBACK_CLOSED)
                return connected
            except (OSError, asyncio.TimeoutError):
                continue
        return None

    def _pin(self, name: str, kind: str) -> bool:
        """Remember `name`'s class; False if the table is full of pins younger
        than `PIN_YOUNG_S` (none is forgotten to make room for it)."""
        now = time.monotonic()
        while len(self._pins) >= PIN_CAP:
            _, (_, when) = next(iter(self._pins.items()))
            if now - when < PIN_YOUNG_S:
                return False
            self._pins.popitem(last=False)
        self._pins[name] = (kind, now)
        return True

    async def _resolve(self, name: str, port: int) -> list:
        """The name's addresses, from one call to the resolver (or a fresh
        cached answer). Never called again for this connection: what is
        judged is what is connected to, and a cached answer is judged — pin
        included — exactly like a new one.

        Bounded (review of PR #69 and its delta review): connections asking
        for the same name at once share one lookup; a domain (`domain_key`)
        may hold at most `DOMAIN_LOOKUPS` worker threads — counted until the
        resolver really returns, not until the gate stops waiting — and a
        lookup past that **waits** for one; the whole wait, slot and answer,
        is `RESOLVE_TIMEOUT_S` (`dns_busy` if no slot came, `unresolvable` if
        no answer did)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + RESOLVE_TIMEOUT_S
        cached = self._cache.get(name)
        if cached is not None:
            if cached[0] > loop.time():
                return list(cached[1])
            del self._cache[name]
        future = self._lookups.get(name)
        if future is None:
            future = asyncio.ensure_future(self._lookup(name, port, deadline))
            self._lookups[name] = future

            def finished(done, n=name):
                if self._lookups.get(n) is done:
                    del self._lookups[n]
                if not done.cancelled():
                    done.exception()        # retrieved: nobody may be waiting any more

            future.add_done_callback(finished)
        try:
            # A beat past the budget, so a lookup that never got a slot says
            # `dns_busy` (its own deadline) rather than racing it to a timeout.
            out = await asyncio.wait_for(asyncio.shield(future),
                                         max(0.0, deadline - loop.time()) + SLOT_GRACE_S)
        except Refusal:
            raise
        except (OSError, asyncio.TimeoutError, UnicodeError, ValueError):
            raise Refusal(policy.UNRESOLVABLE, REP_HOST_UNREACHABLE) from None
        if not out:
            raise Refusal(policy.UNRESOLVABLE, REP_HOST_UNREACHABLE)
        return list(out)

    async def _lookup(self, name: str, port: int, deadline: float) -> list:
        """One resolver call for `name`, once its domain has a free slot."""
        loop = asyncio.get_running_loop()
        domain = domain_key(name)
        await self._take_slot(domain, deadline)

        def lookup():
            try:
                return self._resolver(name, port)
            finally:
                self._give_slot(domain, loop)

        try:
            work = loop.run_in_executor(self._executor, lookup)
        except BaseException:
            self._give_slot(domain, loop)
            raise
        found = await work
        out = []
        for address in found or ():
            try:
                out.append(ipaddress.ip_address(str(address)))
            except ValueError:
                continue
        if out:
            self._cache[name] = (loop.time() + CACHE_S, out)
            self._cache.move_to_end(name)
            while len(self._cache) > CACHE_CAP:
                self._cache.popitem(last=False)
        return out

    async def _take_slot(self, domain: str, deadline: float) -> None:
        """One of `domain`'s `DOMAIN_LOOKUPS` slots, waiting for a lookup to
        give one back; `dns_busy` if none comes before `deadline`."""
        loop = asyncio.get_running_loop()
        while True:
            with self._lock:
                if self._domain_busy[domain] < DOMAIN_LOOKUPS:
                    self._domain_busy[domain] += 1
                    return
                waiter = loop.create_future()
                self._slot_waiters.setdefault(domain, []).append(waiter)
            try:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                await asyncio.wait_for(waiter, remaining)
            except asyncio.TimeoutError:
                raise Refusal(policy.DNS_BUSY, REP_HOST_UNREACHABLE) from None
            finally:
                with self._lock:
                    waiters = self._slot_waiters.get(domain)
                    if waiters is not None and waiter in waiters:
                        waiters.remove(waiter)
                        if not waiters:
                            del self._slot_waiters[domain]

    def _give_slot(self, domain: str, loop) -> None:
        """A slot back (from a worker thread): every waiter on the domain is
        woken to try again, so none is lost to one that timed out meanwhile."""
        with self._lock:
            self._domain_busy[domain] -= 1
            if self._domain_busy[domain] <= 0:
                del self._domain_busy[domain]
            waiters = self._slot_waiters.pop(domain, [])
        for waiter in waiters:
            try:
                loop.call_soon_threadsafe(_wake, waiter)
            except RuntimeError:            # the gate's loop has closed
                pass

    async def _splice(self, client_r, client_w, up_r, up_w) -> None:
        """Bytes both ways until both directions end. A direction that ends
        cleanly half-closes the other side; one that fails ends both; and once
        one has ended, the other is dropped after `HALF_CLOSED_IDLE_S` of
        silence."""
        ended = asyncio.Event()

        async def pipe(src, dst):
            try:
                while True:
                    if ended.is_set():
                        data = await asyncio.wait_for(src.read(CHUNK), HALF_CLOSED_IDLE_S)
                    else:
                        data = await src.read(CHUNK)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
                try:
                    if dst.can_write_eof():
                        dst.write_eof()
                except (OSError, RuntimeError):
                    pass
            finally:
                ended.set()

        tasks = [asyncio.ensure_future(pipe(client_r, up_w)), asyncio.ensure_future(pipe(up_r, client_w))]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            if any(t.exception() is not None for t in done if not t.cancelled()):
                for t in pending:
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
