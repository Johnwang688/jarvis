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

Added by the review (2026-10-09), each verified to fail against 18475c6:

  - an unstarted (INTAKE) task is idle, live and after a reload alike;
  - a broker approval outlives the turn that raised it (a provider question
    does not), for a chat thread and for a task's thread;
  - no sidecar write recreates a deleted thread or task, and a disk that
    refuses one never turns `/seen` into an error;
  - a moved thread's records name the project it is in now.
"""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import shutil
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

    def test_a_queued_message_keeps_the_thread_working_between_turns(self):
        """Steering (#22): a message waiting behind a turn runs the moment it
        ends, so the dot never flashes unread in between — and a stop that
        drops it never leaves the thread working for ever."""
        thread = self.chat()
        self.block(thread)
        self.send(thread, "queued behind it")           # FakeProvider: no steer, so it waits
        statuses = []
        self.d.bus.observe(lambda r: statuses.append(r["data"]["status"])
                           if r.get("kind") == "activity" and r["data"].get("id") == thread.id
                           else None)
        self.release(thread)
        eventually(lambda: [m.text for m in self.fake.messages] == ["block", "queued behind it"])
        self.finish(thread)
        self.assertNotIn("unread", statuses[:-1], statuses)
        self.assertEqual(self.status(thread), "unread")

        other = self.chat()
        self.block(other)
        self.send(other, "dropped by the stop")
        self.request("POST", f"/threads/{other.id}/interrupt", {})
        self.finish(other)
        self.assertEqual(self.status(other), "idle", "an interrupted turn; nothing left running")

    def test_a_queue_that_cannot_run_never_leaves_the_thread_working(self):
        """A turn ends with a message waiting (`next`), and that message is
        then dropped (the thread could not resume): the dot comes back."""
        thread = self.chat()
        publish = self.d.bus.publish
        publish({"kind": "user_message", "thread_id": thread.id, "turn_id": "a" * 32, "data": {}})
        publish({"kind": "turn_started", "thread_id": thread.id, "turn_id": "a" * 32, "data": {}})
        publish({"kind": "queue_cleared", "thread_id": thread.id,
                 "data": {"reason": "stopped", "messages": []}})
        self.assertEqual(self.status(thread), "working", "mid-turn, a cleared queue changes nothing")
        publish({"kind": "turn_finished", "thread_id": thread.id, "turn_id": "a" * 32,
                 "data": {"stop": "end", "next": 1}})
        self.assertEqual(self.status(thread), "working", "the next turn is about to start")
        publish({"kind": "queue_cleared", "thread_id": thread.id,
                 "data": {"reason": "the thread could not resume", "messages": []}})
        self.assertEqual(self.status(thread), "unread", "it never will: the last turn is unread")

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
        self.ask(thread, kind="question", req_id="q2", code=None)
        self.release(thread)
        self.assertEqual(self.status(thread), "unread", "a finished turn closes the questions it left open")

    def test_a_broker_approval_outlives_the_turn_that_raised_it(self):
        # An interrupted turn can end with its permit still blocked, and the
        # escape hatch raises its approval after turn_finished on a thread of
        # its own. Only `approval_resolved` (resolve, timeout, shutdown) closes
        # one; a provider question still ends with its turn.
        thread = self.chat()
        self.block(thread)
        self.ask(thread, req_id="r1")
        self.ask(thread, kind="question", req_id="q1", code=None)
        self.release(thread)
        self.assertEqual(self.status(thread), "needs_input", "the approval is still waiting on the owner")
        self.assertEqual(self.request("GET", "/activity")["threads"], {thread.id: "needs_input"})
        self.d.bus.publish({"kind": "approval_resolved", "thread_id": thread.id, "data": {"req_id": "r1"}})
        self.assertEqual(self.status(thread), "unread", "the question ended with the turn")
        # Raised after the turn finished (the hatch's shape): it still counts.
        self.ask(thread, req_id="r2")
        self.assertEqual(self.status(thread), "needs_input")
        self.d.bus.publish({"kind": "approval_resolved", "thread_id": thread.id, "data": {"req_id": "r2"}})
        self.assertEqual(self.status(thread), "unread")

    def test_a_task_waits_while_its_threads_approval_outlives_the_turn(self):
        task = self.task(TaskState.CLARIFYING, TaskState.PLANNED, TaskState.RUNNING)
        worker = self.stores.threads.create(self.project.id, Role.IMPLEMENTER, ProviderName.FAST,
                                            task_id=task.id)
        self.stores.threads.save(worker)
        self.d.bus.publish({"kind": "turn_started", "thread_id": worker.id})
        self.ask(worker)
        self.d.bus.publish({"kind": "turn_finished", "thread_id": worker.id, "turn_id": "t1",
                            "data": {"stop": "interrupted"}})
        self.assertEqual(self.d.activity.task_status(task), "needs_input")
        self.d.bus.publish({"kind": "approval_resolved", "thread_id": worker.id, "data": {"req_id": "r1"}})
        self.assertEqual(self.d.activity.task_status(task), "working")

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
        self.release(thread)      # still waiting on the approval: no change
        self.d.bus.publish({"kind": "approval_resolved", "thread_id": thread.id, "data": {"req_id": "r1"}})
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

    def test_nothing_recreates_a_deleted_thread(self):
        from jarvis.v2 import projects, trash
        self.d.trash = trash.Trash(self.stores.root, root=self.root / "Trash",
                                   recycle=lambda path: self.fail("nothing here is on Windows"))
        thread = self.chat()
        self.block(thread)
        self.release(thread)                     # a sidecar, and the thread's meta remembered
        folder = self.stores.threads.path(thread.id).parent
        self.assertTrue((folder / "activity.json").is_file())
        projects.archive_thread(self.d, thread.id)
        projects.delete_thread(self.d, thread.id)
        self.assertFalse(folder.exists())
        # A late record for it, and a read that raced the delete.
        self.d.bus.publish({"kind": "turn_finished", "thread_id": thread.id, "turn_id": "late",
                            "data": {"stop": "end"}})
        self.d.activity.seen_thread(thread.id)
        self.assertFalse(folder.exists(), "the activity sidecar brought the deleted thread back")
        self.request("POST", f"/threads/{thread.id}/seen", {}, 404)

    def test_nothing_recreates_a_deleted_task(self):
        task = self.task(TaskState.CLARIFYING, TaskState.PLANNED, TaskState.RUNNING,
                         TaskState.VERIFYING, TaskState.DONE)
        folder = self.stores.tasks.path(task.id).parent
        # The owner's read raced the delete: its sidecar was read just before
        # the project (and the task with it) went to the trash.
        side = self.d.activity._sidecar(self.stores.tasks, task.id)
        self.assertEqual(side, {"terminal": "done", "seen": False})
        shutil.rmtree(folder)
        with patch.object(self.d.activity, "_sidecar", return_value=side):
            self.d.activity.seen_task(task)
        self.assertFalse(folder.exists(), "the activity sidecar brought the deleted task back")

    def test_seen_never_answers_500_when_the_disk_refuses(self):
        thread = self.chat()
        self.block(thread)
        self.release(thread)
        with patch("jarvis.v2.activity._write_bytes", side_effect=OSError(28, "No space left on device")):
            value = self.request("POST", f"/threads/{thread.id}/seen", {})
        self.assertIn(value["status"], ("idle", "unread"))

    def test_records_carry_the_project_the_thread_is_in_now(self):
        (self.root / "other").mkdir()
        other = self.stores.projects.create("other", str(self.root / "other"))
        thread = self.chat()
        self.block(thread)
        self.release(thread)
        self.request("POST", f"/threads/{thread.id}/seen", {})
        self.request("PATCH", f"/threads/{thread.id}", {"project_id": other.id})
        self.send(thread, "again")
        self.finish(thread)
        mine = [r["data"] for r in self.seen_records if r["data"]["id"] == thread.id]
        self.assertEqual(mine[-1]["status"], "unread")
        self.assertEqual(mine[-1]["project_id"], other.id, "a moved thread's records named its old project")

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

    def test_an_unstarted_task_is_idle_live_and_after_a_reload(self):
        # INTAKE waits for the owner to press Start: nothing is running. Live
        # (the records) and a reload (GET /activity) must say the same.
        task = self.request("POST", "/tasks", {"project_id": self.project.id, "brief": "later"}, 201)
        self.assertEqual(task["state"], "intake")
        self.assertEqual(self.d.activity.task_status(self.stores.tasks.get(task["id"])), "idle")
        self.assertEqual(self.request("GET", "/activity")["tasks"], {})
        mine = lambda: [r["data"]["status"] for r in self.seen_records if r["data"]["id"] == task["id"]]  # noqa: E731
        # task_created is followed: idle is remembered, so a brand-new task
        # adds nothing to the stream, and Start is a change worth a record.
        self.assertEqual(self.d.activity._shown.get(("task", task["id"])), "idle")
        self.assertEqual(mine(), [])
        moved = self.stores.tasks.transition(task["id"], TaskState.CLARIFYING)
        self.d.bus.publish({"kind": "task_status_changed", "task_id": moved.id,
                            "project_id": moved.project_id, "data": to_json(moved)})
        self.assertEqual(mine(), ["working"])
        self.assertEqual(self.request("GET", "/activity")["tasks"], {task["id"]: "working"})

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
        self.d.bus.publish({"kind": "approval_resolved", "thread_id": worker.id, "data": {"req_id": "r1"}})
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
