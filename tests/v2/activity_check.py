"""Free checks for the sidebar's per-thread and per-task status (activity.py).

Fake provider, temporary stores, loopback HTTP only. The ones written to bite:

  - an approval or a question outranks a running turn (both arrive mid-turn),
    and an approval record with no broker code is ignored;
  - a finished chat turn is unread until `POST /threads/<id>/seen`, a failed
    one is failed until then, and an interrupted one is never either;
  - unread survives a daemon restart, and a thread or task from before this
    shipped (no sidecar) is idle rather than unread;
  - a task's own threads are never unread, the task row carries the outcome,
    and an approval on one of its threads makes the task need input;
  - an `activity` record is published only when a status changes, after the
    record that caused it, and never on a thread- or project-filtered stream.
"""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jarvis import config
from jarvis.v2 import daemon as mod
from jarvis.v2.activity import Activity
from jarvis.v2.bus import EventBus
from jarvis.v2.model import ProviderName, Role, TaskState, to_json
from jarvis.v2.provider import Brief, Decision
from jarvis.v2.stores import Stores
from tests.v2.daemon_check import FakeProvider, eventually


class ActivityChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "project").mkdir()
        for name, value in (("V2_DATA_DIR", self.root / "v2"), ("MODELS_PATH", self.root / "models"),
                            ("PROVIDER_DEFAULTS_PATH", self.root / "pd"),
                            ("ALLOWLIST_PATH", self.root / "allow")):
            p = patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.stores = Stores()
        self.fake = FakeProvider()
        self.d = mod.Daemon(self.stores, {ProviderName.FAST: self.fake},
                            lambda tid, brief: (lambda *a: Decision.DENY), 0)
        self.d.start()
        self.addCleanup(self.d.stop)
        self.project = self.stores.projects.create("test", str(self.root / "project"))
        self.seen_records = []
        self.d.bus.observe(lambda r: self.seen_records.append(r) if r.get("kind") == "activity" else None)

    # -- helpers ------------------------------------------------------------

    def request(self, method, path, body=None, expected=200):
        conn = http.client.HTTPConnection("127.0.0.1", self.d.port, timeout=3)
        try:
            conn.request(method, path, body=json.dumps(body) if body is not None else None,
                         headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            value = json.loads(response.read())
            self.assertEqual(response.status, expected, value)
            return value
        finally:
            conn.close()

    def chat(self):
        return self.d.open_thread(self.project.id, Role.CHAT, ProviderName.FAST,
                                  Brief(Role.CHAT, str(self.root / "project")))

    def status(self, thread):
        return self.d.activity.thread_status(thread.id)

    def send(self, thread, text):
        self.request("POST", f"/threads/{thread.id}/send", {"text": text}, 202)

    def finish(self, thread):
        eventually(lambda: self.d._sessions.get(thread.id) is None
                   or self.d._sessions[thread.id].worker is None)

    def block(self, thread):
        self.send(thread, "block")
        eventually(lambda: self.fake.handles[thread.id].native["entered"].is_set())

    def release(self, thread):
        self.fake.handles[thread.id].native["release"].set()
        self.finish(thread)

    def ask(self, thread, kind="approval_requested", req_id="r1", code="ABCD"):
        data = {"req_id": req_id}
        if code:
            data["code"] = code
        self.d.bus.publish({"kind": kind, "thread_id": thread.id, "data": data})

    def task(self, *states):
        task = self.stores.tasks.create(project_id=self.project.id, brief="do it")
        self.stores.tasks.save(task)
        for state in states:
            task = self.stores.tasks.transition(task.id, state)
            self.d.bus.publish({"kind": "task_status_changed", "task_id": task.id,
                                "project_id": task.project_id, "data": to_json(task)})
        return self.stores.tasks.get(task.id)

    # -- chat threads -------------------------------------------------------

    def test_a_turn_is_working_then_unread_until_seen(self):
        thread = self.chat()
        self.assertEqual(self.status(thread), "idle")
        self.block(thread)
        self.assertEqual(self.status(thread), "working")
        self.assertEqual(self.request("GET", "/activity")["threads"], {thread.id: "working"})
        self.release(thread)
        self.assertEqual(self.status(thread), "unread")
        self.assertEqual(self.request("POST", f"/threads/{thread.id}/seen", {}), {"status": "idle"})
        self.assertEqual(self.request("GET", "/activity"), {"threads": {}, "tasks": {}})
        # Seeing it again, with nothing new, changes nothing.
        self.assertEqual(self.request("POST", f"/threads/{thread.id}/seen", {}), {"status": "idle"})

    def test_a_failed_turn_is_failed_until_seen(self):
        thread = self.chat()
        self.send(thread, "raise")
        self.finish(thread)
        self.assertEqual(self.status(thread), "failed")
        self.request("POST", f"/threads/{thread.id}/seen", {})
        self.assertEqual(self.status(thread), "idle")

    def test_an_interrupted_turn_is_idle_not_failed(self):
        thread = self.chat()
        self.block(thread)
        self.request("POST", f"/threads/{thread.id}/interrupt", {})
        self.finish(thread)
        self.assertEqual(self.status(thread), "idle")

    def test_an_approval_or_question_outranks_working(self):
        thread = self.chat()
        self.block(thread)
        self.ask(thread, code=None)
        self.assertEqual(self.status(thread), "working", "a code-less record is the gate, not a question")
        self.ask(thread)
        self.assertEqual(self.status(thread), "needs_input")
        self.ask(thread, kind="question", req_id="q1", code=None)
        self.d.bus.publish({"kind": "approval_resolved", "thread_id": thread.id, "data": {"req_id": "r1"}})
        self.assertEqual(self.status(thread), "needs_input", "the question is still open")
        self.d.bus.publish({"kind": "question_answered", "thread_id": thread.id, "data": {"req_id": "q1"}})
        self.assertEqual(self.status(thread), "working")
        self.ask(thread, req_id="r2")
        self.release(thread)
        self.assertEqual(self.status(thread), "unread", "a finished turn closes what it left open")

    def test_unread_survives_a_restart_and_old_threads_start_read(self):
        thread, old = self.chat(), self.chat()
        self.block(thread)
        self.release(thread)
        fresh = Activity(self.stores, lambda r: None)
        self.assertEqual(fresh.thread_status(thread.id), "unread")
        self.assertEqual(fresh.thread_status(old.id), "idle")
        self.assertTrue(self.stores.threads.path(thread.id).with_name("activity.json").is_file())
        self.assertFalse(self.stores.threads.path(old.id).with_name("activity.json").exists())

    def test_published_only_on_change(self):
        thread = self.chat()
        self.block(thread)
        self.ask(thread)
        self.ask(thread)          # the same request again: no change
        self.release(thread)
        mine = [r["data"]["status"] for r in self.seen_records if r["data"]["id"] == thread.id]
        self.assertEqual(mine, ["working", "needs_input", "unread"])
        self.request("POST", f"/threads/{thread.id}/seen", {})
        self.assertEqual(self.seen_records[-1], {"kind": "activity", "data": {
            "of": "thread", "id": thread.id, "project_id": self.project.id, "status": "idle"}})

    def test_filtered_streams_never_carry_it(self):
        thread = self.chat()
        q = self.d.bus.subscribe({"thread_id": thread.id})
        p = self.d.bus.subscribe({"project_id": self.project.id})
        self.block(thread)
        self.release(thread)
        kinds = []
        while not q.empty():
            kinds.append(q.get_nowait()["kind"])
        while not p.empty():
            kinds.append(p.get_nowait()["kind"])
        self.assertIn("turn_finished", kinds)
        self.assertNotIn("activity", kinds)

    def test_seen_after_a_restart_still_tells_the_hud(self):
        thread = self.chat()
        self.block(thread)
        self.release(thread)
        records = []
        fresh = Activity(self.stores, records.append)
        fresh.seen_thread(thread.id)
        self.assertEqual([r["data"]["status"] for r in records], ["idle"])

    def test_observe_never_raises(self):
        a = Activity(self.stores, lambda r: (_ for _ in ()).throw(RuntimeError("boom")))
        a.observe({"kind": "turn_started", "thread_id": "nothex!!"})
        a.observe({"kind": "task_status_changed", "task_id": "deadbeef"})
        a.observe({"kind": "approval_requested", "thread_id": self.chat().id, "data": None})

    def test_unknown_thread_or_task_seen_is_404(self):
        self.request("POST", "/threads/deadbeef/seen", {}, 404)
        self.request("POST", "/tasks/deadbeef/seen", {}, 404)

    # -- tasks ----------------------------------------------------------------

    def test_task_phases(self):
        a = self.d.activity
        running = self.task(TaskState.CLARIFYING, TaskState.PLANNED, TaskState.RUNNING)
        self.assertEqual(a.task_status(running), "working")
        blocked = self.task(TaskState.CLARIFYING, TaskState.BLOCKED)
        self.assertEqual(a.task_status(blocked), "needs_input")
        done = self.task(TaskState.CLARIFYING, TaskState.PLANNED, TaskState.RUNNING,
                         TaskState.VERIFYING, TaskState.DONE)
        self.assertEqual(a.task_status(done), "unread")
        failed = self.task(TaskState.CLARIFYING, TaskState.PLANNED, TaskState.RUNNING, TaskState.FAILED)
        self.assertEqual(a.task_status(failed), "failed")
        cancelled = self.task(TaskState.CANCELLED)
        self.assertEqual(a.task_status(cancelled), "idle")
        self.assertEqual(self.request("GET", "/activity")["tasks"],
                         {running.id: "working", blocked.id: "needs_input",
                          done.id: "unread", failed.id: "failed"})
        self.assertEqual(self.request("POST", f"/tasks/{done.id}/seen", {}), {"status": "idle"})
        self.assertEqual(self.request("POST", f"/tasks/{failed.id}/seen", {}), {"status": "idle"})
        self.assertEqual(self.request("POST", f"/tasks/{running.id}/seen", {}), {"status": "working"})

    def test_a_task_done_before_this_shipped_is_idle(self):
        task = self.stores.tasks.create(project_id=self.project.id, brief="old")
        self.stores.tasks.save(task)
        for state in (TaskState.CLARIFYING, TaskState.PLANNED, TaskState.RUNNING,
                      TaskState.VERIFYING, TaskState.DONE):
            task = self.stores.tasks.transition(task.id, state)   # nobody published
        self.assertEqual(self.d.activity.task_status(task), "idle")

    def test_task_threads_are_never_unread_and_their_asks_reach_the_task(self):
        task = self.task(TaskState.CLARIFYING, TaskState.PLANNED, TaskState.RUNNING)
        worker = self.stores.threads.create(self.project.id, Role.IMPLEMENTER, ProviderName.FAST,
                                            task_id=task.id)
        self.stores.threads.save(worker)
        self.d.bus.publish({"kind": "turn_started", "thread_id": worker.id})
        self.assertEqual(self.d.activity.thread_status(worker.id), "working")
        self.ask(worker)
        self.assertEqual(self.d.activity.task_status(task), "needs_input")
        self.assertEqual(self.seen_records[-1]["data"]["id"], task.id)
        self.d.bus.publish({"kind": "turn_finished", "thread_id": worker.id, "turn_id": "t1",
                            "data": {"stop": "error"}})
        self.assertEqual(self.d.activity.thread_status(worker.id), "idle")
        self.assertEqual(self.d.activity.task_status(task), "working")


class BusObserverChecks(unittest.TestCase):
    def test_observer_runs_after_fan_out_and_may_publish(self):
        bus = EventBus(capacity=4)
        q = bus.subscribe()
        bus.observe(lambda r: bus.publish({"kind": "echo"}) if r["kind"] == "x" else None)
        bus.observe(lambda r: 1 / 0)
        bus.publish({"kind": "x"})
        self.assertEqual([q.get_nowait()["kind"], q.get_nowait()["kind"]], ["x", "echo"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
