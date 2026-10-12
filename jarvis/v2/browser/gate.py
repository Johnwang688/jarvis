"""The Browser's SOCKS5 gate (BR-1a, plan §4 layer 2): the boundary between
the owner's daemon-owned Chrome and the network.

Chrome is launched with `--proxy-server=socks5://127.0.0.1:<this port>` and
`--proxy-bypass-list=<-loopback>`, so every TCP connection it makes — a page,
a redirect, a frame, fetch, XHR, a WebSocket, a worker, a service worker,
prefetch and Chrome's own background traffic — arrives here as a SOCKS5
CONNECT, loopback included. (QUIC is off and WebRTC may not send UDP that
does not go through a proxy, so nothing leaves around it.) For each one the
gate:

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
- allows public addresses and every other loopback port (dev servers),
  then pipes bytes both ways.

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
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from . import policy

LOG = logging.getLogger("jarvis.v2.browser.gate")

HANDSHAKE_TIMEOUT_S = 10.0
RESOLVE_TIMEOUT_S = 10.0
CONNECT_TIMEOUT_S = 15.0
# Once one direction of a piped connection has ended, the other must keep
# moving: a server that never answers a half-close is dropped after this.
HALF_CLOSED_IDLE_S = 60.0
MAX_CONNECTIONS = 256
CHUNK = 64 * 1024

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
    `lan()` the LAN switch (read per connection). `resolver(name, port) ->
    [address]` is called at most once per connection, on a worker thread;
    `connector(address, port) -> (reader, writer)` is awaited for each
    allowed address in turn."""

    def __init__(self, *, refused: Callable[[], frozenset[int]] = frozenset,
                 lan: Callable[[], bool] = lambda: False, resolver=None, connector=None,
                 max_connections: int = MAX_CONNECTIONS):
        self._refused = refused
        self._lan = lan
        self._resolver = resolver or default_resolve
        self._connector = connector or default_connect
        self._max = max_connections
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None
        self._executor: ThreadPoolExecutor | None = None
        self._error: BaseException | None = None
        self._active = 0
        self._counts = Counter()
        self._refusals = Counter()
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
        self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="jarvis-browser-dns")
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
        if kind == "literal":
            candidates = [literal]
        elif kind == "loopback_name":
            candidates = [ipaddress.IPv4Address("127.0.0.1"), ipaddress.IPv6Address("::1")]
        else:
            candidates = await self._resolve(name, port)
        lan = bool(self._lan())
        allowed, first = [], None
        for ip in candidates:
            reason = policy.judge_address(ip, via=kind, lan=lan)
            if reason is None:
                allowed.append(ip)
            elif first is None:
                first = reason
        if not allowed:
            raise Refusal(first or policy.UNRESOLVABLE)
        for ip in allowed:
            try:
                return await self._connector(str(ip), port)
            except (OSError, asyncio.TimeoutError):
                continue
        return None

    async def _resolve(self, name: str, port: int) -> list:
        """The name's addresses, from one call to the resolver. Never called
        again for this connection: what is judged is what is connected to."""
        loop = asyncio.get_running_loop()
        try:
            found = await asyncio.wait_for(
                loop.run_in_executor(self._executor, self._resolver, name, port), RESOLVE_TIMEOUT_S)
        except (OSError, asyncio.TimeoutError, UnicodeError, ValueError):
            raise Refusal(policy.UNRESOLVABLE, REP_HOST_UNREACHABLE) from None
        out = []
        for address in found or ():
            try:
                out.append(ipaddress.ip_address(str(address)))
            except ValueError:
                continue
        if not out:
            raise Refusal(policy.UNRESOLVABLE, REP_HOST_UNREACHABLE)
        return out

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
