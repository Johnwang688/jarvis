"""AO-3 fixture: production claim/arbitration/lease state with no process reads.

Only the mock daemon's HTTP and SSE transport are scripted. Each browser page
has its own subscriber; replacing a socket retires only that page's old one.
"""
from __future__ import annotations
import collections
import json
import threading
from types import SimpleNamespace
from jarvis.v2.devservers import DevServers, valid_window


class DevFixture:
    def __init__(self, mock):
        self.mock = mock
        self.enabled = True
        self.closed = False
        self.condition = threading.Condition()
        self.streams = {}
        self.sequence = 0
        self.now = 1000.0
        daemon = SimpleNamespace(
            browser_settings=SimpleNamespace(auto_open=lambda: self.enabled),
            bus=SimpleNamespace(publish=self.publish))
        self.registry = DevServers(daemon, clock=lambda: self.now)
        self.registry._ensure_thread = lambda: None
        self.claim_gate = None
        self.renew_gate = None
        mock.dev_fixture = self

    def add(self, port, source=None, path="/", window=None):
        with self.registry._lock:
            entry = self.registry._register(port, "http", path,
                source or {"kind": "thread", "thread_id": "t1", "task_id": None},
                self.now, window=window)
            if entry is None:
                raise AssertionError("fixture exceeded registry rate/cap")
            return entry

    def off(self, enabled=False):
        self.enabled = enabled
        self.publish({"kind": "dev_server_settings", "data": {"auto_open": enabled}})
        self.registry.settings_changed()

    def down(self, entry):
        with self.registry._lock:
            entry.listening = False
            entry.prior_state, entry.state, entry.down_at = entry.state, "down", self.now
            entry.window = entry.lease_until = None
            self.registry._publish(entry)
            self.registry._changed.notify_all()

    def publish(self, frame):
        with self.condition:
            for _, queue in self.streams.values():
                queue.append(frame)
            self.condition.notify_all()

    def stream(self, handler, window):
        if not valid_window(window):
            handler._err(400, "invalid page identity")
            return
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        with self.condition:
            self.sequence += 1
            token, queue = self.sequence, collections.deque()
            self.streams[window] = token, queue
            with self.mock._sse_cond:
                self.mock._sse_gen += 1
                self.mock._sse_cond.notify_all()
        try:
            while True:
                with self.condition:
                    self.condition.wait_for(lambda: self.closed or queue
                        or self.streams.get(window, (None,))[0] != token, timeout=0.2)
                    if self.closed or self.streams.get(window, (None,))[0] != token:
                        return
                    frame = queue.popleft() if queue else None
                body = ("data: " + json.dumps(frame) + "\n\n").encode() if frame else b": keepalive\n\n"
                handler.wfile.write(body)
                handler.wfile.flush()
        except (OSError, ValueError):
            pass
        finally:
            ended = False
            with self.condition:
                if self.streams.get(window, (None,))[0] == token:
                    del self.streams[window]
                    ended = True
            if ended:
                self.registry.window_ended(window)

    def handle(self, h, method, path, body):
        if path == "/events" and method == "GET":
            self.stream(h, body.get("window"))
            return True
        if path == "/dev-servers" and method == "GET":
            h._json(self.registry.listing(body.get("window")))
            return True
        if path == "/browser/settings" and method == "PATCH":
            if set(body) != {"auto_open"} or type(body["auto_open"]) is not bool:
                h._err(400, "invalid setting")
            else:
                self.off(body["auto_open"])
                h._json({"auto_open": self.enabled})
            return True
        parts = path.strip("/").split("/")
        if len(parts) != 3 or parts[0] != "dev-servers" or method != "POST":
            return False
        from jarvis.v2.daemon import APIError
        try:
            window = body.get("window")
            if not valid_window(window):
                h._err(400, "invalid page identity")
                return True
            if parts[2] == "claim":
                gate = self.claim_gate
                status, data = self.registry.claim(parts[1], window, body["shows"], body["focused_at"])
                if gate:
                    gate.wait(5)
            else:
                gate = self.renew_gate if parts[2] == "renew" else None
                status, data = self.registry.act(parts[1], parts[2], window)
                if gate:
                    gate.wait(5)
            try: h._json(data, status)
            except (OSError, ValueError): pass  # page closed during a held reply
        except APIError as error:
            h._err(error.status, str(error))
        return True

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        self.registry.stop()
        self.mock.dev_fixture = None
