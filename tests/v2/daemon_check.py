"""Free WP7 checks: fake providers, temporary stores, loopback HTTP/SSE only."""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from jarvis import config
from jarvis.v2 import daemon as mod
from jarvis.v2.bus import EventBus
from jarvis.v2.model import ProviderName, Role
from jarvis.v2.provider import Brief, Decision, Event, EventKind as K, SessionHandle, Usage, UserMessage
from jarvis.v2.stores import Stores, StoreError


def eventually(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true")
        time.sleep(0.005)


def dead_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeProvider:
    name = ProviderName.FAST

    def __init__(self):
        self.handles = {}
        self.interrupted = []
        self.closed = []
        self.resumed = []
        self.messages = []
        self.answers = []

    def health(self):
        return True, "fake ready"

    def start(self, thread, brief, permit):
        native = {"permit": permit, "brief": brief, "stop": threading.Event(),
                  "entered": threading.Event(), "release": threading.Event(), "usage": Usage()}
        handle = SessionHandle(thread.id, self.name, "fake:" + thread.id, native)
        self.handles[thread.id] = handle
        return handle

    def resume(self, thread, brief, permit):
        self.resumed.append((thread.id, brief))
        return self.start(thread, brief, permit)

    def usage(self, handle):
        return handle.native["usage"]

    def send(self, h, message):
        self.messages.append(message)
        n = h.native
        n["stop"].clear()
        n["entered"].set()
        yield Event(K.TURN_STARTED, h.thread_id)
        if message.text == "raise":
            raise RuntimeError("scripted failure")
        if message.text in ("block", "stubborn"):
            while not n["release"].wait(0.005):
                if message.text != "stubborn" and n["stop"].is_set():
                    yield Event(K.TURN_FINISHED, h.thread_id, {"stop": "interrupted"})
                    return
        if message.text == "permit":
            n["permit"]("example", {"thread": h.thread_id}, n["brief"])
        if message.text == "wrong-thread":
            yield Event(K.TEXT, "deadbeef", {"text": "wrong"})
            return
        yield Event(K.TEXT_DELTA, h.thread_id, {"text": "he"})
        yield Event(K.TEXT, h.thread_id, {"text": "hello"})
        yield Event(K.TOOL_STARTED, h.thread_id, {"name": "read", "call_id": "1", "args": {}})
        yield Event(K.TOOL_FINISHED, h.thread_id, {"name": "read", "call_id": "1", "ok": True, "summary": "ok"})
        yield Event(K.USAGE, h.thread_id, {"input": 10, "output": 4, "cached": 3, "cost_usd": 0.25})
        yield Event(K.TURN_FINISHED, h.thread_id, {"stop": "end"})

    def interrupt(self, h):
        self.interrupted.append(h.thread_id)
        h.native["stop"].set()

    def answer(self, h, req_id, decision):
        if req_id != "pending":
            raise ValueError("unknown request")
        self.answers.append((h.thread_id, req_id, decision))

    def close(self, h):
        self.closed.append(h.thread_id)
        h.native["stop"].set()


class SSE:
    def __init__(self, daemon, query=""):
        self.conn = http.client.HTTPConnection("127.0.0.1", daemon.port, timeout=4)
        self.conn.request("GET", "/events" + query)
        self.response = self.conn.getresponse()
        assert self.response.status == 200
        assert self.response.readline() == b": connected\n"
        self.response.readline()

    def next(self):
        while True:
            line = self.response.readline()
            if not line:
                return None
            if line.startswith(b"data: "):
                return json.loads(line[6:])

    def turn(self):
        events = []
        while True:
            event = self.next()
            assert event is not None
            events.append(event)
            if event["kind"] == "turn_finished":
                return events

    def close(self):
        self.response.close()
        self.conn.close()


class DaemonChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project_root = self.root / "project"
        self.project_root.mkdir()
        self.config_patch = patch.object(config, "V2_DATA_DIR", self.root / "v2")
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        # WP12 mounts real picker routes; never let legacy negative probes
        # touch the owner's roster or catalog.
        for name in ("MODELS_PATH", "PROVIDER_DEFAULTS_PATH", "ALLOWLIST_PATH"):
            local = patch.object(config, name, self.root / name.lower())
            local.start()
            self.addCleanup(local.stop)
        self.stores = Stores()
        self.fake = FakeProvider()
        self.permits = []

        def factory(thread_id, brief):
            def permit(tool, args, supplied):
                self.permits.append((thread_id, args["thread"], brief, supplied))
                return Decision.DENY
            return permit

        self.factory = factory
        self.d = mod.Daemon(self.stores, {ProviderName.FAST: self.fake}, factory, 0)
        self.d.start()
        self.addCleanup(self.d.stop)
        self.project = self.stores.projects.create("test", str(self.project_root))

    def request(self, method, path, body=None, expected=200, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.d.port, timeout=3)
        try:
            conn.request(method, path, body=raw if raw is not None else json.dumps(body) if body is not None else None,
                         headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            value = json.loads(response.read())
            self.assertEqual(response.status, expected, value)
            self.assertEqual(response.getheader("Content-Type"), "application/json")
            if expected >= 400:
                self.assertIsInstance(value["error"], str)
            return value
        finally:
            conn.close()

    def thread(self):
        return self.d.open_thread(self.project.id, Role.CHAT, ProviderName.FAST,
                                  Brief(Role.CHAT, str(self.project_root), system_append="saved brief"))

    def wait_turn(self, thread):
        eventually(lambda: self.d._sessions[thread.id].worker is None)

    def sse(self, query=""):
        stream = SSE(self.d, query)
        self.addCleanup(stream.close)
        return stream

    def test_sse_order_log_usage_disconnect_and_filters(self):
        thread = self.thread()
        one = self.sse("?thread=" + thread.id)
        two = self.sse("?project=" + self.project.id)
        turn = self.request("POST", f"/threads/{thread.id}/send", {"text": "block", "images": [{"b64": "eA==", "mime": "image/png"}]}, 202)
        eventually(lambda: self.fake.handles[thread.id].native["entered"].is_set())
        self.request("POST", f"/threads/{thread.id}/send", {"text": "second"}, 409)
        disconnected = self.sse()
        disconnected.close()
        self.fake.handles[thread.id].native["release"].set()
        first, second = one.turn(), two.turn()
        self.assertEqual(first, second)
        # The owner's message leads its turn on the bus (PR C, `user_message`).
        self.assertEqual([e["kind"] for e in first], ["user_message"] + [k.value for k in (K.TURN_STARTED, K.TEXT_DELTA, K.TEXT, K.TOOL_STARTED, K.TOOL_FINISHED, K.USAGE, K.TURN_FINISHED)])
        self.assertEqual(first[0]["data"]["images"], 1)
        self.assertEqual(first[0]["data"]["via"], "hud")
        self.assertTrue(all(e["turn_id"] == turn["turn_id"] for e in first))
        self.wait_turn(thread)
        record = self.stores.threads.get(thread.id)
        self.assertEqual((record.turns, record.tokens, record.cost_usd), (1, 11, 0.25))
        log = self.request("GET", f"/threads/{thread.id}/log")
        self.assertEqual([e["kind"] for e in log], ["user"] + [e["kind"] for e in first if e["kind"] not in ("text_delta", "user_message")])
        self.assertEqual(self.request("GET", f"/threads/{thread.id}/log?after=2"), log[2:])
        self.assertEqual(self.fake.messages[-1].images[0]["mime"], "image/png")
        self.d.send(thread.id, UserMessage("next"))
        self.wait_turn(thread)
        record = self.stores.threads.get(thread.id)
        self.assertEqual((record.turns, record.tokens, record.cost_usd), (2, 22, 0.5))

    def test_interrupt_raise_and_thread_reuse(self):
        thread = self.thread()
        stream = self.sse("?thread=" + thread.id)
        self.d.send(thread.id, UserMessage("block"))
        eventually(lambda: self.fake.handles[thread.id].native["entered"].is_set())
        self.request("POST", f"/threads/{thread.id}/interrupt", {})
        self.assertEqual(stream.turn()[-1]["data"]["stop"], "interrupted")
        self.wait_turn(thread)
        self.assertIn(thread.id, self.fake.interrupted)
        self.request("POST", f"/threads/{thread.id}/interrupt", {}, 409)
        self.d.send(thread.id, UserMessage("raise"))
        events = stream.turn()
        self.assertEqual(events[-2]["kind"], "error")
        self.assertIn("scripted failure", events[-2]["data"]["message"])
        self.assertEqual(events[-1]["data"]["stop"], "error")
        self.wait_turn(thread)
        self.d.send(thread.id, UserMessage("recovered"))
        self.assertEqual(stream.turn()[-1]["data"]["stop"], "end")
        self.wait_turn(thread)
        self.d.close_thread(thread.id)
        self.assertIn(thread.id, self.fake.closed)
        self.assertEqual(stream.next()["kind"], "thread_closed")
        self.request("POST", f"/threads/{thread.id}/answer", {"req_id": "pending", "text": "x"}, 409)

    def test_permit_isolation_and_concurrent_threads(self):
        a, b = self.thread(), self.thread()
        self.d.send(a.id, UserMessage("permit"))
        self.d.send(b.id, UserMessage("permit"))
        self.wait_turn(a)
        self.wait_turn(b)
        self.assertEqual({row[0] for row in self.permits}, {a.id, b.id})
        self.assertTrue(all(bound == actual and brief is supplied for bound, actual, brief, supplied in self.permits))
        self.d.send(a.id, UserMessage("wrong-thread"))
        self.wait_turn(a)
        log = self.stores.threads.read_log(a.id)
        self.assertEqual(log[-2]["kind"], "error")
        self.assertTrue(all(e["thread_id"] == a.id for e in log))

    def test_interrupt_during_provider_start_is_not_lost(self):
        thread = self.thread()
        entered, proceed = threading.Event(), threading.Event()

        def delayed_send(h, message):
            entered.set()
            proceed.wait(1)
            # Both merged providers reset their cancellation state at startup.
            h.native["stop"].clear()
            yield Event(K.TURN_STARTED, h.thread_id)
            if not h.native["stop"].wait(1):
                raise RuntimeError("early interrupt was lost")
            yield Event(K.TURN_FINISHED, h.thread_id, {"stop": "interrupted"})

        self.fake.send = delayed_send
        self.d.send(thread.id, UserMessage("delayed"))
        self.assertTrue(entered.wait(1))
        self.d.interrupt(thread.id)
        proceed.set()
        self.wait_turn(thread)
        log = self.stores.threads.read_log(thread.id)
        self.assertEqual([e["kind"] for e in log], ["user", "turn_started", "turn_finished"])
        self.assertEqual(log[-1]["data"]["stop"], "interrupted")

    def test_project_thread_task_and_worktree_routes(self):
        created = self.request("POST", "/projects", {"name": "new", "root": str(self.project_root), "profile": "ask"}, 201)
        pid = created["id"]
        self.assertEqual(self.request("GET", "/projects/" + pid), created)
        self.assertEqual(len(self.request("GET", "/projects")), 2)
        updated = self.request("PATCH", "/projects/" + pid, {"name": "renamed", "routing": {"chains": {"chat": ["fast"]}}, "always_ask": ["deploy"]})
        self.assertEqual(updated["name"], "renamed")
        created_thread = self.request("POST", "/threads", {"project_id": pid, "role": "chat", "provider": "fast", "brief": {}}, 201)
        self.assertEqual(self.request("GET", "/threads?project=" + pid), [created_thread])
        self.assertEqual(len(self.request("GET", "/threads")), 1)
        tid = created_thread["id"]
        self.request("POST", f"/threads/{tid}/answer", {"req_id": "pending", "decision": "deny"})
        self.request("POST", f"/threads/{tid}/answer", {"req_id": "pending", "text": "clarification"})
        self.assertIs(self.fake.answers[0][2], Decision.DENY)
        task = self.request("POST", "/tasks", {"project_id": pid, "brief": "example task"}, 201)
        task_id = task["id"]
        self.assertEqual(task["state"], "intake")
        self.assertEqual(self.request("GET", "/tasks/" + task_id), task)
        self.assertEqual(self.request("GET", "/tasks?project=" + pid), [task])
        self.assertEqual(self.request("GET", "/tasks"), [task])
        path = f"/tasks/{task_id}/worktree"
        self.assertFalse(self.request("GET", path)["exists"])
        ensured = self.request("POST", path, {})
        self.assertTrue(self.request("GET", path)["exists"])
        self.assertEqual(self.request("POST", path, {})["worktree"], ensured["worktree"])
        file = Path(ensured["worktree"]) / "work.txt"
        file.write_text("keep")
        self.request("DELETE", path, expected=409)
        self.assertTrue(file.exists())
        self.assertTrue(self.request("GET", path)["dirty"])
        self.request("DELETE", path + "?force=true")
        self.assertFalse(file.exists())
        self.assertFalse(self.request("GET", path)["exists"])
        self.request("POST", path, {})
        self.request("DELETE", path + "?force=false")

    def test_lifecycle_and_project_filters(self):
        all_events = self.sse()
        filtered = self.d.bus.subscribe({"project_id": "ffffffff"})
        thread = self.thread()
        self.assertEqual(all_events.next()["kind"], "thread_opened")
        task = self.request("POST", "/tasks", {"project_id": self.project.id, "brief": "task"}, 201)
        self.assertEqual(all_events.next()["kind"], "task_created")
        self.request("POST", f"/tasks/{task['id']}/worktree", {})
        self.assertEqual(all_events.next()["kind"], "task_worktree_ensured")
        self.request("DELETE", f"/tasks/{task['id']}/worktree")
        self.assertEqual(all_events.next()["kind"], "task_worktree_removed")
        self.d.close_thread(thread.id)
        self.assertEqual(all_events.next()["kind"], "thread_closed")
        self.assertTrue(filtered.empty())
        self.d.bus.unsubscribe(filtered)

    def test_bad_requests_and_server_survives_exceptions(self):
        thread = self.thread()
        tid = thread.id
        task = self.stores.tasks.create(self.project.id, "task")
        self.stores.tasks.save(task)
        invalid = [
            ("POST", "/projects", {}),
            ("POST", "/projects", {"name": "x", "root": "relative"}),
            ("PATCH", "/projects/" + self.project.id, {"id": "bad"}),
            ("PATCH", "/projects/" + self.project.id, {"routing": {"chains": []}}),
            ("POST", "/threads", {"project_id": self.project.id, "role": "bad", "provider": "fast", "brief": {}}),
            ("POST", "/threads", {"project_id": self.project.id, "role": "chat", "provider": "fast", "brief": {"role": "reviewer"}}),
            ("POST", f"/threads/{tid}/send", {"text": 4}),
            ("POST", f"/threads/{tid}/send", {"text": "hi", "images": [{}]}),
            ("POST", f"/threads/{tid}/send", {"text": "hi", "origin": "system"}),
            ("POST", f"/threads/{tid}/interrupt", {"foo": True}),
            ("POST", f"/threads/{tid}/answer", {"req_id": "pending"}),
            ("POST", f"/threads/{tid}/answer", {"req_id": "pending", "text": "x", "decision": "allow"}),
            ("POST", f"/threads/{tid}/answer", {"req_id": "pending", "decision": "always"}),
            ("POST", f"/threads/{tid}/answer", {"req_id": "unknown", "text": "x"}),
            ("GET", f"/threads/{tid}/log?after=-1", None),
            ("GET", f"/threads/{tid}/log?after=no", None),
            ("POST", "/tasks", {"project_id": self.project.id, "brief": "x", "state": "running"}),
            ("POST", "/tasks", {"project_id": self.project.id, "brief": 2}),
            ("DELETE", f"/tasks/{task.id}/worktree?force=maybe", None),
            ("POST", f"/tasks/{task.id}/worktree", {"force": True}),
            ("GET", "/events?thread=bad", None),
            ("GET", "/events?thread=" + tid + "&thread=" + tid, None),
            ("GET", "/projects/bad", None),
            ("GET", "/threads?project=bad", None),
            ("GET", "/tasks?project=bad", None),
        ]
        for method, path, body in invalid:
            with self.subTest(path=path, body=body):
                self.request(method, path, body, 400)
        for method, path in [("GET", "/projects/deadbeef"), ("PATCH", "/projects/deadbeef"),
                             ("GET", "/tasks/deadbeef"), ("POST", "/threads/deadbeef/send"),
                             ("POST", "/threads/deadbeef/interrupt"), ("POST", "/threads/deadbeef/answer"),
                             ("GET", "/threads/deadbeef/log"), ("GET", "/tasks/deadbeef/worktree"),
                             ("POST", "/tasks/deadbeef/worktree"), ("DELETE", "/tasks/deadbeef/worktree"),
                             ("GET", "/events?project=deadbeef"), ("GET", "/unknown")]:
            self.request(method, path, expected=404)
        for raw in ("{", "[]", "null"):
            self.request("POST", "/projects", raw=raw, expected=400)
        self.request("POST", "/threads", {"project_id": self.project.id, "role": "chat", "provider": "claude", "brief": {}}, 409)
        with patch.object(self.stores.projects, "get", side_effect=RuntimeError("exploded")):
            with self.assertLogs(mod.LOG, level="ERROR"):
                self.request("GET", "/projects/" + self.project.id, expected=409)
        self.assertEqual(self.request("GET", "/status")["version"], 2)

    def test_corrupt_files_skip_and_strict_store_unchanged(self):
        thread = self.thread()
        task = self.stores.tasks.create(self.project.id, "task")
        self.stores.tasks.save(task)
        for name, good in (("projects", self.project), ("threads", thread), ("tasks", task)):
            store = getattr(self.stores, name)
            bad = store.path("badbad00")
            bad.parent.mkdir(parents=True, exist_ok=True)
            bad.write_text("{ broken")
            with self.assertRaises(StoreError):
                store.list()
            with self.assertLogs(mod.LOG, level="WARNING") as captured:
                records = self.request("GET", "/" + name)
            self.assertEqual([r["id"] for r in records], [good.id])
            self.assertIn(str(bad), "\n".join(captured.output))
        self.assertEqual(self.request("GET", "/status")["threads_open"], 1)

    def test_single_instance_dead_probe_and_stop_releases_waiters(self):
        self.assertFalse(mod.is_running(dead_port()))
        self.assertTrue(mod.is_running(self.d.port))
        other = mod.Daemon(self.stores, {}, self.factory, self.d.port)
        with self.assertRaisesRegex(mod.DaemonError, "already running"):
            other.start()
        thread = self.thread()
        self.d.send(thread.id, UserMessage("block"))
        eventually(lambda: self.fake.handles[thread.id].native["entered"].is_set())
        worker = self.d._sessions[thread.id].worker
        stream = self.sse()
        result = []

        def read():
            while True:
                event = stream.next()
                if event is None or event["kind"] == "shutdown":
                    result.append(event)
                    return
        waiter = threading.Thread(target=read, daemon=True)
        waiter.start()
        start = time.monotonic()
        self.d.stop()
        self.assertLess(time.monotonic() - start, mod.STOP_TIMEOUT + 0.3)
        waiter.join(1)
        self.assertFalse(waiter.is_alive())
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [{"kind": "shutdown"}])
        self.assertIn(thread.id, self.fake.closed)
        self.assertFalse(mod.is_running(self.d.port))
        self.d.stop()

    def test_stubborn_provider_does_not_exceed_shutdown_bound(self):
        thread = self.thread()
        self.d.send(thread.id, UserMessage("stubborn"))
        native = self.fake.handles[thread.id].native
        eventually(native["entered"].is_set)
        worker = self.d._sessions[thread.id].worker
        stream = self.sse()
        start = time.monotonic()
        with patch.object(mod, "STOP_TIMEOUT", 0.15), self.assertLogs(mod.LOG, level="WARNING"):
            self.d.stop()
        self.assertLess(time.monotonic() - start, 0.5)
        # The subscription opened after send(), so turn_started may or may not
        # precede it on this stream; only the shutdown record is guaranteed.
        event = stream.next()
        while event.get("kind") != "shutdown":
            event = stream.next()
        self.assertEqual(event, {"kind": "shutdown"})
        before = self.stores.threads.read_log(thread.id)
        native["release"].set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.stores.threads.read_log(thread.id), before)

    def test_occupied_port_and_v1_health_marker(self):
        from jarvis import daemon as v1
        self.assertTrue(v1.is_running(self.d.port))
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            other = mod.Daemon(self.stores, {}, self.factory, listener.getsockname()[1])
            with self.assertRaisesRegex(mod.DaemonError, "port is already in use"):
                other.start()

    def test_resume_preserves_brief_and_usage(self):
        thread = self.thread()
        self.d.send(thread.id, UserMessage("first"))
        self.wait_turn(thread)
        self.d.stop()
        self.d = mod.Daemon(Stores(self.root / "v2"), {ProviderName.FAST: self.fake}, self.factory, 0)
        self.d.start()
        self.addCleanup(self.d.stop)
        self.request("POST", f"/threads/{thread.id}/send", {"text": "second"}, 202)
        self.wait_turn(thread)
        self.assertEqual(self.fake.resumed[0][1].system_append, "saved brief")
        self.assertEqual(self.stores.threads.get(thread.id).turns, 2)
        self.assertEqual(self.stores.threads.get(thread.id).cost_usd, 0.5)

    def test_lazy_import_failure_health(self):
        with patch.object(mod.importlib, "import_module", side_effect=ImportError("SDK absent")) as imp:
            providers = mod.load_providers([ProviderName.CLAUDE])
        imp.assert_called_once_with("jarvis.v2.providers.claude")
        self.d.providers.update(providers)
        health = self.request("GET", "/status")["providers"]["claude"]
        self.assertFalse(health["ok"])
        self.assertIn("SDK absent", health["reason"])
        self.request("POST", "/threads", {"project_id": self.project.id, "role": "chat", "provider": "claude", "brief": {}}, 409)
        with patch.object(self.fake, "health", side_effect=RuntimeError("health broke")):
            self.assertFalse(self.request("GET", "/status")["providers"]["fast"]["ok"])

    def test_cumulative_codex_usage_is_not_double_counted(self):
        fake = FakeProvider()
        fake.name = ProviderName.CODEX
        self.d.providers[ProviderName.CODEX] = fake
        thread = self.d.open_thread(self.project.id, Role.IMPLEMENTER, ProviderName.CODEX,
                                    Brief(Role.IMPLEMENTER, str(self.project_root)))

        def send(h, message):
            for total in (Usage(10, 4, 3), Usage(20, 8, 6), Usage(20, 8, 6)):
                h.native["usage"] = total
                yield Event(K.USAGE, h.thread_id, {"input": total.input_tokens, "output": total.output_tokens,
                                                  "cached": total.cached_tokens, "cost_usd": None})
            yield Event(K.TURN_FINISHED, h.thread_id, {"stop": "end"})
        fake.send = send
        self.d.send(thread.id, UserMessage("cumulative"))
        self.wait_turn(thread)
        record = self.stores.threads.get(thread.id)
        self.assertEqual((record.turns, record.tokens, record.cost_usd), (1, 22, 0.0))
        self.d.send(thread.id, UserMessage("duplicate totals"))
        self.wait_turn(thread)
        self.assertEqual(self.stores.threads.get(thread.id).tokens, 22)


class BusChecks(unittest.TestCase):
    def test_drop_oldest_filter_copy_shutdown(self):
        bus = EventBus(capacity=2)
        slow = bus.subscribe({"thread_id": "a"})
        fast = bus.subscribe(lambda e: e.get("thread_id") == "a")
        rejected = bus.subscribe({"thread_id": "b"})
        for i in range(5):
            bus.publish(Event(K.TEXT, "a", {"text": str(i)}))
            fast.get_nowait()["data"]["text"] = "client mutation"
        self.assertEqual(slow.dropped, 3)
        self.assertEqual([slow.get_nowait()["data"]["text"] for _ in range(2)], ["3", "4"])
        self.assertTrue(rejected.empty())
        self.assertEqual(fast.dropped, 0)
        for i in range(3):
            bus.publish({"kind": "text", "thread_id": "a"})
        bus.close()
        self.assertEqual(slow.get_nowait()["kind"], "text")
        self.assertEqual(slow.get_nowait(), {"kind": "shutdown"})
        self.assertEqual(rejected.get_nowait(), {"kind": "shutdown"})
        self.assertEqual(bus.subscribe().get_nowait(), {"kind": "shutdown"})
        bus.publish(Event(K.TEXT, "a"))
        self.assertTrue(slow.empty())
        bus.unsubscribe(slow)


if __name__ == "__main__":
    unittest.main(verbosity=2)
