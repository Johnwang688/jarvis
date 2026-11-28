"""Desktop control: the WSL half of the Windows bridge.

`windows/bridge.py` runs on Windows and dials in here; this module owns the
listener, hands requests across, and blocks the calling thread until the reply
comes back. It is the same shape as `browser.Session` — one long-lived
resource, a lock, and errors that come back as text rather than exceptions —
for the same reason: the agent loop wants a synchronous call it can retry.

Why the bridge dials in rather than being dialed: WSL2 forwards `localhost`
from Windows into the distro, so an outbound connection from Windows needs no
firewall exception and no address discovery. The reverse direction needs both,
and the gateway address changes on every boot.

The safety boundary lives here, not on Windows: `config.DESKTOP_APPS` is the
allowlist, and no tool takes a window title, handle, or path — only a
registered app name. That is the same structural confinement as the Onshape
sandbox pin (a write URL is always built from the pinned document, never from
a model-supplied id): the model cannot widen its own reach by argument, only
the owner can, by editing the registry.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any

from . import config
from windows import desktopwire, voicewire


# How long to let a running bridge redial before declaring it absent. The
# bridge's own retry interval is 2s, so this covers one full cycle plus slack.
CONNECT_GRACE = 6.0


class DesktopError(RuntimeError):
    """A desktop operation failed in a way the model should see as text."""


class DesktopSession:
    """Listens for the bridge and marshals one request at a time.

    A managed session starts only with its daemon and accepts paired peers.
    Explicit lifecycle control lets the daemon close in-flight sockets on stop.
    """

    def __init__(self, port: int | None = None, *, key_path=None, managed=False):
        # `is None`, not `or`: port 0 means "any free port", which the tests
        # rely on to avoid fighting a real bridge for the fixed one.
        self.port = config.DESKTOP_PORT if port is None else port
        self._server: socket.socket | None = None
        self._conn: socket.socket | None = None
        self._stream: Any = None
        self._lock = threading.Lock()
        self._requests = threading.Lock()
        self._handshake: socket.socket | None = None
        self._managed = managed
        self._accept_thread: threading.Thread | None = None
        self._next_id = 0
        self._bridge_info: dict | None = None
        self._key_path = key_path
        self._paired = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Open the listener. Idempotent, so every surface can just call it."""
        with self._lock:
            if self._server is not None:
                return
            server = socket.socket()
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Loopback only: the bridge reaches us through WSL's localhost
            # forwarding, so there is never a reason to be on the network.
            try:
                server.bind(("127.0.0.1", self.port))
                server.listen(1)
                self._server = server
                self._accept_thread = threading.Thread(
                    target=self._accept_loop, args=(server,), name="jarvis-desktop", daemon=True)
                self._accept_thread.start()
            except (OSError, RuntimeError):
                self._server = self._accept_thread = None
                server.close()
                raise

    @property
    def bound_port(self) -> int | None:
        with self._lock:
            return self._server.getsockname()[1] if self._server is not None else None

    @staticmethod
    def _shutdown(sock):
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def _accept_loop(self, server) -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with self._lock:
                if self._server is not server:
                    conn.close()
                    return
                self._handshake = conn
            stream = None
            try:
                conn.settimeout(3.0)
                stream = conn.makefile("rwb")
                hello = voicewire.read_json(stream, 4096)
                paired = hello.get("hello") == "jarvis-desktop-bridge" and hello.get("version") == "2"
                if self._managed and not paired:
                    raise DesktopError("The daemon requires a paired desktop bridge.")
                if paired:
                    key = desktopwire.private_key(self._key_path or config.voice_link_key())
                    desktopwire.prove_daemon(stream, hello, key)
                conn.settimeout(None)
            except Exception:
                if stream is not None:
                    stream.close()
                conn.close()
                with self._lock:
                    if self._handshake is conn:
                        self._handshake = None
                continue
            # Wait for a request in flight: replacing its connection would fail a
            # call whose action may already have happened. stop() still ends both.
            with self._requests, self._lock:
                if self._handshake is conn:
                    self._handshake = None
                if self._server is not server or (self._conn is not None and self._paired and not paired):
                    stream.close()
                    conn.close()
                    continue
                if self._conn is not None:
                    try:
                        self._shutdown(self._conn)
                        self._stream.close()
                    except OSError:
                        pass
                self._conn, self._stream = conn, stream
                self._bridge_info = hello
                self._paired = paired

    def stop(self) -> None:
        with self._lock:
            for sock in (self._server, self._handshake, self._conn):
                self._shutdown(sock)
            if self._stream is not None:
                self._stream.close()
            self._conn = self._server = self._stream = self._handshake = None
            self._bridge_info = None
            self._paired = False
            worker, self._accept_thread = self._accept_thread, None
        if worker is not None:
            worker.join()

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def wait_for_bridge(self, timeout: float = 0.0) -> bool:
        deadline = time.time() + timeout
        while True:
            if self.connected:
                return True
            if time.time() >= deadline:
                return False
            time.sleep(0.2)

    # -- request/response --------------------------------------------------

    def request(self, op: str, timeout: float = 90.0, *, require_pair=False, current=None, **fields) -> dict:
        """Send one op to the bridge and wait for its reply."""
        if self._managed:
            if self.bound_port is None:
                raise DesktopError("The desktop bridge listener is stopped.")
        else:
            self.start()
        with self._lock:
            server = self._server
        # The bridge redials every couple of seconds, so a Jarvis that just
        # started has to give it a moment to find the new listener. Without
        # this wait the first desktop call of every session reports "no
        # bridge" even though one is running and about to connect.
        if not self.connected:
            self.wait_for_bridge(CONNECT_GRACE)
        if not self.connected:
            raise DesktopError(
                "the Windows desktop bridge is not running. Start it on the "
                f"Windows side with:  {config.DESKTOP_BRIDGE_CMD}"
            )
        with self._requests:
            with self._lock:
                if server is None or self._server is not server:
                    # A call that waited across a stop must not run on a later listener.
                    raise DesktopError("The desktop bridge listener stopped.")
                conn, stream = self._conn, self._stream
                if conn is None or stream is None:
                    raise DesktopError("the desktop bridge disconnected.")
                if require_pair and not self._paired:
                    raise DesktopError("Start the paired Windows desktop bridge to use it from HUD chats.")
                if current is not None and not current():
                    raise DesktopError("The owner chat turn is no longer current.")
                self._next_id += 1
                payload = {"id": self._next_id, "op": op, **fields}
            try:
                conn.settimeout(timeout)
                stream.write((json.dumps(payload) + "\n").encode())
                stream.flush()
                line = stream.readline()
            except (OSError, ValueError) as exc:
                self._drop_peer(conn, stream)
                raise DesktopError(
                    f"lost the desktop bridge mid-request ({type(exc).__name__}). "
                    "Restart it on the Windows side."
                ) from exc
            if not line:
                self._drop_peer(conn, stream)
                raise DesktopError("the desktop bridge closed the connection.")
        reply = json.loads(line)
        if reply.get("id") != payload["id"]:
            raise DesktopError("Mismatched reply from the Windows desktop bridge.")
        if not reply.get("ok"):
            raise DesktopError(reply.get("error", "unknown bridge error"))
        return reply.get("result", {})

    def _drop_peer(self, conn, stream):
        with self._lock:
            if self._conn is conn:
                self._shutdown(conn)
                stream.close()
                self._conn = self._stream = self._bridge_info = None
                self._paired = False

    # -- operations --------------------------------------------------------

    def app_spec(self, app: str) -> dict:
        """Resolve an app name against the allowlist. The safety boundary."""
        key = (app or "").strip().lower()
        spec = config.DESKTOP_APPS.get(key)
        if spec is None:
            known = ", ".join(sorted(config.DESKTOP_APPS))
            raise DesktopError(
                f"{app!r} is not a registered desktop app. Available: {known}. "
                "Jarvis can only drive apps the owner has registered."
            )
        return {"name": key, **spec}

    def open(self, app: str) -> str:
        spec = self.app_spec(app)
        result = self.request("open", spec=spec, timeout=120.0)
        return (f"{spec['name']} is open (window {result['title']!r}, "
                f"{result['backend']} backend).\n\n{result['snapshot']}")

    def snapshot(self, app: str) -> str:
        self.app_spec(app)
        return self.request("snapshot", app=app.strip().lower())["snapshot"]

    def click(self, app: str, ref: str) -> str:
        self.app_spec(app)
        result = self.request("click", app=app.strip().lower(), ref=ref)
        return (f"Clicked {result['clicked']!r} (via {result['via']}). "
                "Take a fresh desktop_snapshot to see the result.")

    def type_text(self, app: str, ref: str, text: str, submit: bool = False) -> str:
        self.app_spec(app)
        result = self.request("type", app=app.strip().lower(), ref=ref,
                              text=text, submit=submit)
        suffix = " and pressed Enter" if submit else ""
        return (f"Typed into {result['typed_into']!r}{suffix}. "
                "Take a fresh desktop_snapshot to see the result.")

    def send_keys(self, app: str, keys: str) -> str:
        self.app_spec(app)
        result = self.request("key", app=app.strip().lower(), keys=keys)
        return f"Sent {result['sent']}. Take a fresh desktop_snapshot to see the result."

    def screenshot_b64(self, app: str, marked: bool = True) -> tuple[str, int, int]:
        self.app_spec(app)
        result = self.request("screenshot", app=app.strip().lower(), marked=marked)
        return result["image_b64"], result["bytes"], result["marks"]

    def status(self) -> str:
        registry = "\n".join(
            f"  {name:12} {spec.get('description', '')}"
            for name, spec in sorted(config.DESKTOP_APPS.items())
        )
        if not self.connected:
            if not self._managed:
                try:
                    self.start()
                except OSError:
                    return ("Another Jarvis process holds the desktop bridge port, so the bridge "
                            f"reports to it and not to this command.\n\nRegistered apps:\n{registry}")
            # A managed session is opened only by its daemon; asking never opens it.
            if self.bound_port is not None:
                self.wait_for_bridge(CONNECT_GRACE)
        if not self.connected:
            return (
                "The Windows desktop bridge is NOT connected.\n"
                f"Start it on Windows with:\n  {config.DESKTOP_BRIDGE_CMD}\n\n"
                f"Registered apps:\n{registry}"
            )
        info = self._bridge_info or {}
        return (f"Desktop bridge connected (v{info.get('version', '?')}).\n\n"
                f"Registered apps:\n{registry}")


SESSION = DesktopSession()
