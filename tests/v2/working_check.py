"""The owner's working set, WS-1 (docs/plans/2026-11-14-inbox-working-set-plan.md,
revision 2, D-1 to D-11, §4 and §5's WS-1 test list).

* **The producer table (D-2)**, row by row, on a real daemon's bus: each
  kind bumps `seq`/`last` on the thread the code logs it on, a `subagent`
  op only when started or finished, a compaction record never, a task's
  thread never (D-7), an archived thread or project never (D-8), and the
  records that are not activity never.
* **Time (D-3, D-11)**: a fake clock with relative expiry, the idle-limit
  setting (1 to 30, backend-validated), the timer (first sweep after
  start, then hourly), a restart that keeps membership, seeding once.
* **Eligibility (D-4, IN-8)**: an approval, a chat question and a
  quiet-hours deferral each hold a thread in; so does a running turn; a
  failed `inbox.rows()` read fails closed (an exception, a broker or
  deferral source that failed, warnings switched off).
* **Done and Undo (D-4)**: a stale or bogus `seq` is a 409 with the current
  entry, a repeated Done is a 409, an old token or one after the 10 s
  monotonic deadline is a 409, `seen_thread` is called once, activity
  during the check refuses the write, and new activity brings it back.
* **Lifecycle (D-8)**: archive, restore, delete, move, rename, and a
  project's archive and delete.
* **The read (D-5, D-6)**: every `inbox.rows()` row exactly once, on its
  entry or in `needs_you`; `count`/`pending` as `waitingCount`/`anyPending`.
* **Boundaries (§4)**: every route owner-only (the API listener, the HUD
  listener without the HUD's Origin, a foreign Origin, the phone listener),
  `working_changed` with no ids or data, the module SELF_PROTECTED and the
  file refused to agent writes (reads pinned as today), and `inbox.rows()`,
  the phone's inbox and stream, and the morning review unchanged.

Free and hermetic: temp config and data, ephemeral ports, fake brains, no model.

    systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 \\
        env PYTHONPATH=. .venv/bin/python tests/v2/working_check.py
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import http.client
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis import config  # noqa: E402
from jarvis.v2 import inbox, quiet_defer, remote_api, review, trash as T, working  # noqa: E402
from jarvis.v2.approvals import ApprovalRequest  # noqa: E402
from jarvis.v2.daemon import APIError, Daemon  # noqa: E402
from jarvis.v2.model import ProviderName as PN, Role, TaskState  # noqa: E402
from jarvis.v2.provider import Decision, Event, EventKind as K, SessionHandle, Usage, UserMessage  # noqa: E402
from jarvis.v2.stores import Stores  # noqa: E402

CHANGED = {"kind": "working_changed"}
T0 = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


def eventually(predicate, timeout=5):
    until = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > until:
            raise AssertionError("condition did not settle")
        time.sleep(0.005)


class Clock:
    """The wall clock (UTC) and the monotonic one, moved by hand."""

    def __init__(self):
        self.now = T0
        self.mono_now = 1000.0

    def __call__(self):
        return self.now

    def mono(self):
        return self.mono_now

    def advance(self, **kw):
        self.now += timedelta(**kw)
        self.mono_now += timedelta(**kw).total_seconds()


class Brain:
    """A brain whose turn asks one question when told to, and waits for it."""

    def __init__(self, name):
        self.name = name
        self.ask = False
        self.awaiting: set[str] = set()
        self.counter = 0

    def health(self):
        return True, "fake"

    def start(self, thread, brief, permit):
        return SessionHandle(thread.id, self.name, "fake:" + thread.id, SimpleNamespace(brief=brief))

    resume = start

    def usage(self, handle):
        return Usage()

    def send(self, handle, message):
        tid = handle.thread_id
        yield Event(K.TURN_STARTED, tid)
        if self.ask:
            self.counter += 1
            req_id = f"q-{self.counter}"
            self.awaiting.add(req_id)
            yield Event(K.QUESTION, tid, {"req_id": req_id, "text": "which?", "options": ["a", "b"]})
            until = time.monotonic() + 5
            while req_id in self.awaiting and time.monotonic() < until:
                time.sleep(0.005)
        yield Event(K.TEXT, tid, {"text": "ok"})
        yield Event(K.TURN_FINISHED, tid, {"stop": "end"})

    def answer(self, handle, req_id, decision):
        if req_id not in self.awaiting:
            raise ValueError("Unknown or resolved request")
        self.awaiting.discard(req_id)

    def interrupt(self, handle):
        self.awaiting.clear()

    def close(self, handle):
        pass


def _never_recycle(path):
    raise AssertionError(f"the Recycle Bin was reached for {path}")


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root / "xdg"),
                                      "JARVIS_CLAUDE_CREDENTIALS": str(self.root / "none.json")})
        env.start()
        self.addCleanup(env.stop)
        for name, value in dict(V2_DATA_DIR=self.root / "data", ALLOWLIST_PATH=self.root / "allow.json",
                                MODELS_PATH=self.root / "models.json",
                                PROVIDER_DEFAULTS_PATH=self.root / "provider_defaults.json").items():
            guard = patch.object(config, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        (self.root / "repo").mkdir()
        (self.root / "repo2").mkdir()
        self.stores = Stores()
        self.brain = Brain(PN.FAST)
        self.daemon = Daemon(self.stores, {name: (self.brain if name == PN.FAST else Brain(name)) for name in PN},
                             lambda *_: lambda *_: Decision.DENY, 0)
        self.daemon.trash = T.Trash(self.stores.root, root=self.root / "Trash", recycle=_never_recycle)
        self.daemon.start()
        self.addCleanup(self.daemon.stop)
        self.ws = self.daemon.working
        self.clock = Clock()
        self.ws.clock, self.ws.mono = self.clock, self.clock.mono
        self.project = self.stores.projects.create("Calc", str(self.root / "repo"))
        self.other = self.stores.projects.create("Other", str(self.root / "repo2"))
        self.changes: list[dict] = []
        self.daemon.bus.observe(lambda r: self.changes.append(dict(r)) if r.get("kind") == "working_changed"
                                else None)
        # Every record, seen after the working set's observer has run on it.
        self.records: list[dict] = []
        self.daemon.bus.observe(self.records.append)

    # -- helpers ------------------------------------------------------------------

    def thread(self, project=None, *, task_id=None, role=Role.CHAT):
        """A saved thread with no session (records are published by hand)."""
        thread = self.stores.threads.create((project or self.project).id, role, PN.FAST, task_id=task_id)
        self.stores.threads.save(thread)
        return thread

    def open_chat(self, project=None):
        return self.daemon.open_thread((project or self.project).id, Role.CHAT, PN.FAST, {})

    def publish(self, kind, thread_id, **extra):
        record = {"kind": kind, "thread_id": thread_id, "project_id": self.project.id, "at": "x", **extra}
        self.daemon.bus.publish(record)
        return record

    def seq(self, thread_id):
        entry = self.ws.entry(thread_id)
        return None if entry is None else entry["seq"]

    def is_in(self, thread_id):
        entry = self.ws.entry(thread_id)
        return entry is not None and entry["in"]

    def call(self, method, path, body=None, status=200, *, owner=True, port=None, origin=None,
             headers=None):
        port = port or (self.daemon.face_port if owner else self.daemon.port)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        sent = {"Content-Type": "application/json"} if body is not None else {}
        if owner:
            sent["Origin"] = origin or f"http://127.0.0.1:{port}"
        elif origin:
            sent["Origin"] = origin
        sent.update(headers or {})
        try:
            conn.request(method, path, json.dumps(body).encode() if body is not None else None, sent)
            response = conn.getresponse()
            content = response.read()
            self.assertEqual(response.status, status, content[:400])
            return json.loads(content) if content else None
        finally:
            conn.close()

    def done(self, thread_id, seq=None, status=200):
        seq = self.seq(thread_id) if seq is None else seq
        return self.call("POST", f"/working/{thread_id}/done", {"seq": seq}, status)

    def ask(self, thread_id, **kw):
        """An approval asked on a background thread, as a permit asks."""
        req = ApprovalRequest(tool=kw.pop("tool", "Bash"), args={"command": "make deploy"},
                              command="make deploy", provider="codex", thread_id=thread_id, **kw)
        out = {}
        worker = threading.Thread(target=lambda: out.setdefault("decision", self.daemon.approvals.ask(req)),
                                  daemon=True)
        worker.start()
        # Announced, not only admitted: the broker tells the bus after it
        # admits the ask, and the working set reads that record.
        eventually(lambda: any(r.get("kind") == "approval_requested" and req.req_id
                               and (r.get("data") or {}).get("req_id") == req.req_id for r in list(self.records)))
        return req, out, worker

    def defer(self, thread_id, req_id="dfr0000001"):
        req = ApprovalRequest(tool="Bash", args={"command": "make deploy"}, command="make deploy",
                              provider="claude", thread_id=thread_id)
        req.req_id = req_id
        self.daemon.inbox.deferred.add(req, None)
        (row,) = [r for r in inbox.rows(self.daemon) if r["id"] == f"defer:{req_id}"]
        self.assertEqual((row["rank"], row["thread_id"]), (4, thread_id))
        return row

    def stale(self, thread_id):
        """Make the thread's `last` older than the idle limit."""
        self.publish("turn_finished", thread_id, data={"stop": "end"})
        self.clock.advance(days=self.ws.idle_days, minutes=1)


# -- the contract ------------------------------------------------------------------------


class Contract(unittest.TestCase):
    def test_the_constants_and_table_are_pinned(self):
        self.assertEqual((working.NAME, working.IDLE_DAYS_DEFAULT, working.IDLE_DAYS_MIN, working.IDLE_DAYS_MAX,
                          working.FIRST_SWEEP_S, working.SWEEP_S, working.UNDO_S, working.BLOCKING_MAX_RANK,
                          working.COUNTED_MAX_RANK, working.PENDING_MAX_RANK),
                         ("working.json", 3, 1, 30, 300.0, 3600.0, 10.0, 4, 5, 2))
        self.assertEqual(working.ACTIVITY_KINDS, frozenset({
            "user_message", "turn_started", "turn_finished", "error", "approval_requested", "question",
            "question_answered", "relay_in", "relay_delivered", "relay_reply", "decision_posted",
            "check_posted", "thread_opened"}))
        self.assertEqual(working.SUBAGENT_OPS, frozenset({"started", "finished"}))

    def test_the_badge_rules_mirror_the_hud(self):
        source = (config.REPO_ROOT / "hud/src/lib/inbox.ts").read_text()
        self.assertIn("export const COUNTED_MAX_RANK = 5;", source)
        self.assertIn("export const PENDING_MAX_RANK = 2;", source)
        self.assertIn("return rows.filter((r) => !isHidden(r) && r.rank >= 1 && r.rank <= COUNTED_MAX_RANK).length;",
                      source)
        self.assertIn("return rows.some((r) => r.rank >= 1 && r.rank <= PENDING_MAX_RANK);", source)

        def row(rank, kind="x", hidden=None, rid=None):
            return {"id": rid or f"x:{rank}{kind}", "kind": kind, "rank": rank,
                    **({"hidden": hidden} if hidden is not None else {})}
        hid = {"snoozed_until": None, "dismissed_at": "2026-11-14T00:00:00+00:00"}
        rows = [row(1), row(2), row(4, hidden=hid),                 # a rank 1-4 "hidden" is never hidden
                row(5, "pr", hidden=hid, rid="pr:o/r#1"),           # hidden: not counted
                row(5, "pr_unavailable", hidden=hid, rid="prx:list"),  # never hidden: counted
                row(4, "deferred", hidden=hid, rid="defer:a"),      # never hidden: counted
                row(5, "pr", rid="pr:o/r#2"),                       # counted
                row(6), row(8)]                                     # never counted
        self.assertEqual(working.waiting_count(rows), 6)
        self.assertTrue(working.any_pending(rows))
        self.assertFalse(working.any_pending([row(3), row(4)]))
        self.assertEqual(working.waiting_count([]), 0)

    def test_the_module_and_file_are_protected(self):
        from jarvis.tools import files
        from jarvis.v2 import permissions as v2_permissions
        self.assertIn("jarvis/v2/working.py", files.SELF_PROTECTED)
        self.assertTrue(files._self_protected(config.REPO_ROOT / "jarvis/v2/working.py"))
        with tempfile.TemporaryDirectory() as tmp, patch.object(config, "V2_DATA_DIR", Path(tmp)):
            target = Path(tmp) / "inbox" / "working.json"
            self.assertIn(target, files._protected_state_paths())
            self.assertTrue(files._self_protected(target), "v1 write tools refuse it")
            self.assertTrue(v2_permissions.denied_file(str(target)), "v2 layer 1 refuses a file tool")

    def test_agent_writes_are_refused_and_reads_are_as_today(self):
        from jarvis import permissions as v1_permissions, rules
        from jarvis.tools import files
        from jarvis.v2 import permissions as v2_permissions
        with tempfile.TemporaryDirectory() as tmp, patch.object(config, "V2_DATA_DIR", Path(tmp)), \
                patch.object(config, "ALLOWLIST_PATH", Path(tmp) / "cfg" / "allowlist.json"):
            d = Path(tmp)
            (d / "inbox").mkdir()
            target = d / "inbox" / "working.json"
            target.write_text('{"threads": {}}')
            for command in (f"echo '{{}}' > {target}", f"cp /tmp/forged.json {target}", f"rm {target}",
                            f": > {target}", f"tee {target} < /tmp/x", f"cp x {d}/*/working.json",
                            f"mv /tmp/x {d}/inbox/working.json", f"sed -i s/a/b/ {target}"):
                with self.subTest(command=command):
                    self.assertIsNotNone(v2_permissions._command_writes_protected_state(command), command)
                    self.assertIsNotNone(v2_permissions.denied_command(command), "layer 1 refuses it")
                    self.assertIn(v1_permissions.static_verdict(command).decision, (rules.ASK, rules.DENY))
            # The write tools refuse it (v1 `write_file`/`edit_file`), and
            # the file is unchanged.
            refused = files.write_file(str(target), '{"threads": {"deadbeef": {}}}')
            self.assertTrue(refused.startswith("Error") or "refus" in refused.lower() or "protected" in refused.lower(),
                            refused)
            self.assertEqual(target.read_text(), '{"threads": {}}')
            # §9 Q7: reads are not refused (membership is derivable anyway);
            # pinned as today, the same as the other `<data>/inbox/` files.
            self.assertIsNone(v2_permissions._command_writes_protected_state(f"cat {target}"))
            self.assertIn('{"threads": {}}', files.read_file(str(target)))

    def test_working_changed_never_reaches_the_phone(self):
        self.assertIsNone(remote_api.stream_record(dict(CHANGED)))
        self.assertFalse(remote_api._wanted(dict(CHANGED)))


# -- the producer table (D-2) ---------------------------------------------------------------


class Producers(Base):
    def shapes(self, tid):
        """One record of each producer, shaped as its code publishes it."""
        return [
            ("user_message", dict(turn_id="t1", data={"text": "hi", "message_id": "m1"})),
            ("turn_started", dict(turn_id="t1", data={})),
            ("turn_finished", dict(turn_id="t1", data={"stop": "end"})),
            ("error", dict(turn_id="t1", data={"message": "x", "fatal": False})),
            ("approval_requested", dict(data={"req_id": "r1", "code": "abcd", "tool": "Bash"})),
            ("question", dict(turn_id="t1", data={"req_id": "q1", "text": "?", "options": []})),
            ("question_answered", dict(data={"req_id": "q1"})),
            # chat_relay._record: on the receiving thread (`relay_in`,
            # `relay_delivered`) and on the sender (`relay_reply`).
            ("relay_in", dict(turn_id="t1", data={"message_id": "m1", "from": "0000beef"})),
            ("relay_delivered", dict(turn_id=None, data={"message_id": "m1", "delivery": "step"})),
            ("relay_reply", dict(turn_id=None, data={"message_id": "m1", "from": "0000beef", "status": "done"})),
            ("decision_posted", dict(data={"decision_id": "d1"})),
            ("check_posted", dict(data={"check_id": "c1"})),
            ("thread_opened", dict(data={"id": tid})),
        ]

    def test_each_producer_joins_and_bumps_its_thread(self):
        threads = {}
        for kind, extra in self.shapes("x"):
            with self.subTest(kind=kind):
                tid = threads[kind] = self.thread().id
                self.changes.clear()
                self.publish(kind, tid, **extra)
                entry = self.ws.entry(tid)
                self.assertIsNotNone(entry, kind)
                self.assertEqual((entry["seq"], entry["in"], entry["last"]), (1, True, self.clock.now.isoformat()))
                self.assertEqual(self.changes, [CHANGED])
                self.clock.advance(seconds=5)
                self.publish(kind, tid, **extra)
                self.assertEqual(self.seq(tid), 2)
                self.assertEqual(self.ws.entry(tid)["last"], self.clock.now.isoformat())
        self.assertEqual(set(threads), working.ACTIVITY_KINDS, "every row of the table is exercised")

    def test_subagent_start_and_finish_count_progress_does_not(self):
        tid = self.thread().id
        for op, expected in (("progress", None), ("started", 1), ("progress", 1), ("finished", 2)):
            with self.subTest(op=op):
                self.publish("subagent", tid, turn_id="t1", data={"sid": "cl:1", "op": op, "status": "running"})
                self.assertEqual(self.seq(tid), expected)

    def test_compaction_is_skipped(self):
        fresh, member = self.thread().id, self.thread().id
        self.publish("user_message", member, data={})
        for kind in ("user_message", "turn_started", "turn_finished", "error"):
            with self.subTest(kind=kind):
                self.publish(kind, fresh, compaction=True, data={})
                self.publish(kind, member, compaction=True, data={})
        self.assertIsNone(self.ws.entry(fresh), "a compaction never joins")
        self.assertEqual(self.seq(member), 1, "nor bumps")

    def test_records_that_are_not_activity(self):
        tid = self.thread().id
        for kind in ("approval_resolved", "queue_cleared", "thread_updated", "thread_moved", "model",
                     "activity", "inbox_changed", "compact_started", "compacted", "relay_out", "relay_state",
                     "relay_progress", "decision_answered", "check_marked", "tool_started", "text", "usage",
                     "thread_closed", "thread_restored", "queued_started", "steer_queued", "notice",
                     "subagent_event", "working_changed"):
            with self.subTest(kind=kind):
                self.publish(kind, tid, data={"op": "started"})
        self.assertIsNone(self.ws.entry(tid))

    def test_task_threads_never_join(self):
        task = self.stores.tasks.create(self.project.id, "add multiply")
        self.stores.tasks.save(task)
        task_thread = self.thread(task_id=task.id, role=Role.CHAT).id
        builder = self.thread(task_id=task.id, role=Role.IMPLEMENTER).id
        for tid in (task_thread, builder):
            for kind, extra in self.shapes(tid):
                self.publish(kind, tid, **extra)
            self.assertIsNone(self.ws.entry(tid), tid)

    def test_archived_threads_and_threads_in_archived_projects_never_join(self):
        archived = self.thread()
        archived.archived = "2026-11-14T00:00:00+00:00"
        self.stores.threads.save(archived)
        in_archived = self.thread(self.other)
        project = self.stores.projects.get(self.other.id)
        project.archived = "2026-11-14T00:00:00+00:00"
        self.stores.projects.save(project)
        for tid in (archived.id, in_archived.id):
            self.publish("user_message", tid, data={})
            self.assertIsNone(self.ws.entry(tid))

    def test_a_real_turn_joins_through_the_daemons_own_records(self):
        thread = self.open_chat()
        self.assertEqual(self.seq(thread.id), 1, "thread_opened")
        self.daemon.send(thread.id, UserMessage("go"))
        # user_message, turn_started, turn_finished.
        eventually(lambda: self.seq(thread.id) == 4)
        eventually(lambda: self.daemon._sessions[thread.id].worker is None)
        self.assertTrue(self.is_in(thread.id))

    def test_the_store_is_d1_shaped_and_written_atomically(self):
        tid = self.thread().id
        self.publish("user_message", tid, data={})
        path = self.stores.root / "inbox" / "working.json"
        data = json.loads(path.read_text())
        self.assertEqual(set(data), {"seeded_at", "idle_days", "threads"})
        self.assertEqual(data["threads"][tid], {"since": T0.isoformat(), "last": T0.isoformat(), "seq": 1,
                                                "done_at": None, "done_seq": None, "done_token": None,
                                                "reason": None})
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(path.parent.stat().st_mode & 0o777), "0o700")
        self.assertEqual(sorted(p.name for p in path.parent.iterdir() if p.name.startswith(".working")), [],
                         "no temp file left")


# -- time: expiry, the setting, the timer, restart, seeding (D-3, D-11) ------------------------


class FakeTimer:
    made: list = []

    def __init__(self, delay, fn):
        self.delay, self.fn, self.daemon, self.started, self.cancelled = delay, fn, False, False, False
        FakeTimer.made.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


class Time(Base):
    def test_relative_expiry_on_a_fake_clock(self):
        tid = self.thread().id
        self.publish("turn_finished", tid, data={"stop": "end"})
        self.clock.advance(days=3, minutes=-1)
        self.assertEqual(self.ws.sweep(), [])
        self.assertTrue(self.is_in(tid))
        self.clock.advance(minutes=2)
        self.changes.clear()
        self.assertEqual(self.ws.sweep(), [tid])
        entry = self.ws.entry(tid)
        self.assertEqual((entry["in"], entry["reason"]), (False, "idle"))
        self.assertEqual(self.changes, [CHANGED])
        # Leaving changed nothing else: the thread is where it was.
        stored = self.stores.threads.get(tid)
        self.assertEqual((stored.project_id, stored.archived), (self.project.id, None))
        # Activity brings it back, relative to the new time.
        self.publish("user_message", tid, data={})
        self.assertTrue(self.is_in(tid))
        self.clock.advance(days=2)
        self.assertEqual(self.ws.sweep(), [])

    def test_unread_and_failed_do_not_hold_a_thread_in(self):
        thread = self.open_chat()
        self.daemon.send(thread.id, UserMessage("go"))
        eventually(lambda: self.daemon.activity.thread_status(thread.id) == "unread")
        self.clock.advance(days=3, minutes=1)
        self.assertEqual(self.ws.sweep(), [thread.id], "§9 Q2: unread does not hold it in")

    def test_the_idle_limit_setting(self):
        self.assertEqual(self.call("GET", "/working/settings"),
                         {"idle_days": 3, "min": 1, "max": 30, "default": 3})
        for bad in (0, 31, -1, True, "3", 2.5, None):
            with self.subTest(bad=bad):
                self.call("POST", "/working/settings", {"idle_days": bad}, 400)
        self.call("POST", "/working/settings", {"idle_days": 3, "extra": 1}, 400)
        self.call("POST", "/working/settings", {}, 400)
        self.assertEqual(self.call("POST", "/working/settings", {"idle_days": 1})["idle_days"], 1)
        tid = self.thread().id
        self.publish("turn_finished", tid, data={"stop": "end"})
        self.clock.advance(hours=23)
        self.assertEqual(self.ws.sweep(), [])
        self.clock.advance(hours=2)
        self.assertEqual(self.ws.sweep(), [tid])
        # Kept across a restart; 30 is the most.
        self.assertEqual(working.WorkingSet(self.daemon).idle_days, 1)
        self.assertEqual(self.call("POST", "/working/settings", {"idle_days": 30})["idle_days"], 30)

    def test_the_timer_runs_after_start_then_hourly(self):
        FakeTimer.made = []
        ws = working.WorkingSet(self.daemon, clock=self.clock, mono=self.clock.mono, timer_factory=FakeTimer)
        ws.start()
        (first,) = FakeTimer.made
        self.assertEqual((first.delay, first.started, first.daemon), (300.0, True, True))
        swept = []
        with patch.object(ws, "sweep", lambda: swept.append(1) or []):
            first.fn()
        self.assertEqual(swept, [1])
        second = FakeTimer.made[-1]
        self.assertEqual((second.delay, second.started), (3600.0, True))
        ws.stop()
        self.assertTrue(second.cancelled)
        second.fn()                                     # a timer that fires after stop does nothing
        self.assertEqual((swept, len(FakeTimer.made)), ([1], 2))

    def test_a_restart_keeps_membership_and_last(self):
        tid = self.thread().id
        self.publish("user_message", tid, data={})
        self.publish("turn_finished", tid, data={"stop": "end"})
        before = self.ws.entry(tid)
        seeded = self.ws.seeded_at
        self.assertIsNotNone(seeded, "the daemon's start seeded (nothing) once")
        again = working.WorkingSet(self.daemon, clock=self.clock, mono=self.clock.mono)
        self.assertEqual(again.entry(tid), before)
        self.assertEqual(again.seeded_at, seeded)
        self.assertEqual(again.seed(), 0, "seeded once: never again after a restart")
        self.clock.advance(days=2)
        self.assertEqual(again.sweep(), [], "a recent `last` keeps it in across the restart")
        self.clock.advance(days=1, minutes=1)
        self.assertEqual(again.sweep(), [tid])

    def test_seeding_once(self):
        recent, old, linked, archived = (self.thread() for _ in range(4))
        task = self.stores.tasks.create(self.project.id, "t")
        self.stores.tasks.save(task)
        task_thread = self.thread(task_id=task.id)
        now = datetime.now(timezone.utc)
        for thread, age in ((recent, timedelta(hours=1)), (old, timedelta(days=5)),
                            (linked, timedelta(days=9)), (archived, timedelta(hours=1)),
                            (task_thread, timedelta(hours=1))):
            stored = self.stores.threads.get(thread.id)
            stored.updated = (now - age).isoformat()
            if thread is archived:
                stored.archived = now.isoformat()
            self.stores.threads.save(stored)
        self.defer(linked.id)
        (self.stores.root / "inbox" / "working.json").unlink(missing_ok=True)
        clock = Clock()
        clock.now = now
        ws = working.WorkingSet(self.daemon, clock=clock, mono=clock.mono)
        self.assertIsNone(ws.seeded_at)
        self.changes.clear()
        self.assertEqual(ws.seed(), 2)
        self.assertEqual(ws.members(), sorted([recent.id, linked.id]))
        self.assertEqual(self.changes, [CHANGED])
        self.assertEqual(ws.entry(recent.id)["seq"], 0)
        self.assertEqual(ws.entry(linked.id)["last"], (now - timedelta(days=9)).isoformat(timespec="seconds"))
        self.assertIsNotNone(ws.seeded_at)
        self.assertEqual(ws.seed(), 0)
        reloaded = working.WorkingSet(self.daemon, clock=clock, mono=clock.mono)
        self.assertEqual((reloaded.seed(), reloaded.members()), (0, sorted([recent.id, linked.id])))
        # The linked thread's old `last` does not let it leave while it waits.
        self.assertEqual(reloaded.sweep(), [])
        self.assertTrue(reloaded.entry(linked.id)["in"])

    def test_an_empty_start_writes_nothing(self):
        # The daemon in setUp started with no threads: its seed added nothing,
        # so no file and no `<data>/inbox/` folder were made (the inbox's own
        # stores check that folder's absence and modes themselves).
        self.assertFalse((self.stores.root / "inbox").exists())
        self.assertIsNotNone(self.ws.seeded_at, "seeded, in memory")
        self.assertEqual(self.ws.seed(), 0)
        self.assertFalse((self.stores.root / "inbox").exists())
        # The first activity writes it, `seeded_at` included.
        tid = self.thread().id
        self.publish("user_message", tid, data={})
        data = json.loads((self.stores.root / "inbox" / "working.json").read_text())
        self.assertEqual(data["seeded_at"], self.ws.seeded_at)

    def test_a_file_that_is_not_ours_reads_empty(self):
        tid = self.thread().id
        self.publish("user_message", tid, data={})
        path = self.stores.root / "inbox" / "working.json"
        for content in ("not json", "[]", json.dumps({"threads": {"zz": {}, tid: {"seq": "1"}}})):
            with self.subTest(content=content[:12]):
                path.write_text(content)
                self.assertEqual(working.WorkingSet(self.daemon).members(), [])
        path.unlink()
        elsewhere = self.root / "elsewhere.json"
        elsewhere.write_text(json.dumps({"seeded_at": None, "threads": {}}))
        path.symlink_to(elsewhere)
        self.assertEqual(working.WorkingSet(self.daemon).members(), [], "a symlink is not ours")


# -- eligibility (D-4, IN-8) ---------------------------------------------------------------------


class Eligibility(Base):
    def test_an_approval_holds_a_thread_in(self):
        thread = self.open_chat()
        req, out, worker = self.ask(thread.id)
        got = self.done(thread.id, status=409)
        self.assertEqual(got["error"], "an approval waits on you in this chat")
        self.assertTrue(got["entry"]["in"])
        self.clock.advance(days=4)
        self.assertEqual(self.ws.sweep(), [])
        self.daemon.resolve_approval(req.req_id, Decision.DENY, False, owner_verified=True)
        worker.join(5)
        self.assertEqual(out["decision"], Decision.DENY)
        # `approval_resolved` is announced by the waiter's own thread.
        eventually(lambda: self.daemon.activity.thread_status(thread.id) != "needs_input")
        self.assertEqual(self.ws.sweep(), [thread.id], "eligible once it is answered")

    def test_a_question_holds_a_thread_in(self):
        thread = self.open_chat()
        self.brain.ask = True
        self.daemon.send(thread.id, UserMessage("go"))
        eventually(lambda: any(r["kind"] == "question" and r["thread_id"] == thread.id
                               for r in inbox.rows(self.daemon)))
        got = self.done(thread.id, status=409)
        self.assertEqual(got["error"], "a question waits on you in this chat")
        self.clock.advance(days=4)
        self.assertEqual(self.ws.sweep(), [])
        (row,) = [r for r in inbox.rows(self.daemon) if r["kind"] == "question"]
        self.daemon.answer(thread.id, row["detail"]["req_id"], "a")
        eventually(lambda: self.daemon._sessions[thread.id].worker is None)
        eventually(lambda: self.daemon.activity.thread_status(thread.id) != "working")
        self.done(thread.id)

    def test_a_quiet_hours_deferral_holds_a_thread_in(self):
        tid = self.thread().id
        self.publish("user_message", tid, data={})
        self.publish("turn_finished", tid, data={"stop": "end"})
        self.assertEqual(self.daemon.activity.thread_status(tid), "idle")
        row = self.defer(tid)
        got = self.done(tid, status=409)
        self.assertEqual(got["error"], "an approval deferred overnight waits on you in this chat")
        self.clock.advance(days=4)
        self.assertEqual(self.ws.sweep(), [])
        self.assertTrue(self.is_in(tid))
        self.daemon.inbox.deferred.settle(row["detail"]["deferral_id"], "leave")
        self.assertEqual(self.ws.sweep(), [tid])

    def test_a_running_turn_holds_a_thread_in(self):
        tid = self.thread().id
        self.publish("turn_started", tid, data={})
        self.assertEqual(self.done(tid, status=409)["error"], "a turn is running in this chat")
        self.clock.advance(days=4)
        self.assertEqual(self.ws.sweep(), [])
        self.publish("turn_finished", tid, data={"stop": "end"})
        self.done(tid)

    def test_a_failed_read_fails_closed(self):
        tid = self.thread().id
        self.publish("turn_finished", tid, data={"stop": "end"})
        self.clock.advance(days=4)

        def broken(*_a, **_k):
            raise OSError("broker gone")
        cases = [
            ("rows raises", patch.object(inbox, "rows", broken)),
            ("the broker cannot be read", patch.object(self.daemon.approvals, "pending", broken)),
            ("the deferrals cannot be read", patch.object(self.daemon.inbox.deferred, "_open", broken)),
            ("the questions source fails", patch.object(inbox, "_question_rows", broken)),
            ("warnings switched off", patch.object(inbox.LOG, "disabled", True)),
        ]
        for name, guard in cases:
            with self.subTest(name=name), guard:
                got = self.done(tid, status=409)
                self.assertEqual(got["error"], working.UNREADABLE)
                self.assertEqual(self.ws.sweep(), [])
                self.assertTrue(self.is_in(tid))
        # A task store that cannot be read fails no chat thread's check:
        # task rows never carry a `thread_id` (D-7).
        with patch.object(self.stores.tasks, "list", broken):
            self.assertEqual(self.ws.why_not([tid]), {tid: None})
        # A warning on another thread meanwhile is not this read's failure.
        noise = threading.Thread(target=lambda: inbox.LOG.warning("Cannot read pending approvals for the inbox"))
        real = inbox.rows

        def rows_with_noise(*a, **k):
            noise.start()
            noise.join()
            return real(*a, **k)
        with patch.object(inbox, "rows", rows_with_noise):
            self.assertEqual(self.ws.why_not([tid]), {tid: None})
        self.assertEqual(self.ws.sweep(), [tid])


# -- Done and Undo (D-4) -------------------------------------------------------------------------


class DoneUndo(Base):
    def member(self):
        tid = self.thread().id
        self.publish("user_message", tid, data={})
        self.publish("turn_finished", tid, turn_id="t1", data={"stop": "end"})
        return tid

    def test_done_then_undo(self):
        tid = self.member()
        seen = []
        real_seen = self.daemon.activity.seen_thread
        with patch.object(self.daemon.activity, "seen_thread", lambda t: seen.append(t) or real_seen(t)):
            self.changes.clear()
            got = self.done(tid)
        self.assertEqual(seen, [tid], "seen_thread once")
        self.assertEqual(self.changes, [CHANGED])
        self.assertEqual((got["done"], got["undo_s"], got["entry"]["in"], got["entry"]["reason"]),
                         (True, 10.0, False, "done"))
        self.assertRegex(got["token"], r"^[A-Za-z0-9_-]{16,64}$")
        self.assertNotIn("done_token", got["entry"])
        self.assertEqual(self.ws.entry(tid)["done_seq"], 2)
        view = self.call("GET", "/working")
        self.assertEqual(view["entries"], [])
        self.assertNotIn(got["token"], json.dumps(view))
        self.assertNotIn(got["token"], json.dumps(self.changes))
        back = self.call("POST", f"/working/{tid}/undo", {"token": got["token"]})
        self.assertEqual((back["done"], back["entry"]["in"]), (False, True))
        self.assertEqual([e["thread_id"] for e in self.call("GET", "/working")["entries"]], [tid])
        self.call("POST", f"/working/{tid}/undo", {"token": got["token"]}, 409)

    def test_stale_and_bogus_seq(self):
        tid = self.member()
        drawn = self.seq(tid)
        self.publish("relay_reply", tid, data={})              # activity after the HUD drew it
        got = self.done(tid, drawn, status=409)
        self.assertEqual(got["entry"]["seq"], drawn + 1, "the 409 carries the current entry")
        self.done(tid, drawn + 5, status=409)
        for bad in (True, -1, "2", 1.0, None):
            with self.subTest(bad=bad):
                self.call("POST", f"/working/{tid}/done", {"seq": bad}, 400)
        self.call("POST", f"/working/{tid}/done", {}, 400)
        self.call("POST", f"/working/{tid}/done", {"seq": drawn, "x": 1}, 400)
        self.call("POST", "/working/NOTANID/done", {"seq": 0}, 400)
        self.assertTrue(self.is_in(tid))
        self.done(tid, drawn + 1)

    def test_repeated_done_and_done_on_a_stranger(self):
        tid = self.member()
        self.done(tid)
        again = self.done(tid, status=409)
        self.assertEqual(again["error"], "this chat is not in your Inbox")
        self.assertEqual(self.done("0000beef", 0, status=409)["entry"], None)

    def test_an_old_undo_token_after_a_later_dismissal(self):
        tid = self.member()
        first = self.done(tid)["token"]
        self.publish("user_message", tid, data={})             # back in on new activity
        entry = self.ws.entry(tid)
        self.assertTrue(entry["in"])
        self.assertGreater(entry["seq"], 2)
        self.publish("turn_finished", tid, data={"stop": "end"})
        second = self.done(tid)["token"]
        self.assertNotEqual(first, second)
        self.call("POST", f"/working/{tid}/undo", {"token": first}, 409)
        self.call("POST", f"/working/{tid}/undo", {"token": "x" * 24}, 409)
        self.call("POST", f"/working/{tid}/undo", {"token": "short"}, 400)
        self.call("POST", f"/working/{tid}/undo", {"token": 5}, 400)
        self.call("POST", f"/working/{tid}/undo", {"token": second})

    def test_undo_after_the_deadline_and_after_a_restart(self):
        tid = self.member()
        token = self.done(tid)["token"]
        self.clock.mono_now += 9.99
        self.assertTrue(self.ws.mono() < self.ws._undo[tid][1])
        self.clock.mono_now += 0.01
        self.assertEqual(self.call("POST", f"/working/{tid}/undo", {"token": token}, 409)["error"],
                         "too late to undo")
        tid2 = self.member()
        token2 = self.done(tid2)["token"]
        self.daemon.working = again = working.WorkingSet(self.daemon, clock=self.clock, mono=self.clock.mono)
        self.addCleanup(setattr, self.daemon, "working", self.ws)
        self.assertEqual(again.entry(tid2)["reason"], "done")
        self.call("POST", f"/working/{tid2}/undo", {"token": token2}, 409)   # its deadline died with the process

    def test_activity_during_the_check_refuses_the_write(self):
        tid = self.member()
        real = self.ws.why_not

        def racing(ids, rows=None):
            out = real(ids, rows)
            self.publish("relay_in", tid, data={})              # lands between the read and the write
            return out
        with patch.object(self.ws, "why_not", racing):
            got = self.done(tid, status=409)
        self.assertEqual(got["error"], "this chat changed since the Inbox was drawn")
        self.assertTrue(self.is_in(tid))

    def test_a_late_record_cannot_undo_a_done_but_new_activity_brings_it_back(self):
        tid = self.member()
        self.done(tid)
        self.assertFalse(self.is_in(tid))
        self.publish("seen", tid)
        self.publish("approval_resolved", tid, data={"req_id": "r"})
        self.assertFalse(self.is_in(tid), "records that are not activity leave it out")
        self.clock.advance(minutes=1)
        self.publish("subagent", tid, data={"op": "finished"})
        entry = self.ws.entry(tid)
        self.assertEqual((entry["in"], entry["since"], entry["reason"]), (True, self.clock.now.isoformat(), None))


# -- lifecycle (D-8) ------------------------------------------------------------------------------


class Lifecycle(Base):
    def test_archive_restore_delete(self):
        thread = self.open_chat()
        req, out, worker = self.ask(thread.id)
        self.changes.clear()
        self.call("POST", f"/threads/{thread.id}/archive", {})
        eventually(lambda: thread.id not in self.daemon._sessions)     # its session is closed
        entry = self.ws.entry(thread.id)
        self.assertEqual((entry["in"], entry["reason"]), (False, "archived"))
        self.assertEqual(self.changes, [CHANGED])
        view = self.call("GET", "/working")
        self.assertEqual(view["entries"], [])
        self.assertEqual([r["id"] for r in view["needs_you"]], [f"appr:{req.req_id}"],
                         "an archived thread's approval shows in needs_you")
        self.daemon.resolve_approval(req.req_id, Decision.DENY, False, owner_verified=True)
        worker.join(5)
        self.call("POST", f"/threads/{thread.id}/restore", {})
        self.assertFalse(self.is_in(thread.id), "restore does not rejoin")
        self.publish("user_message", thread.id, data={})
        self.assertTrue(self.is_in(thread.id), "new activity does")
        self.call("POST", f"/threads/{thread.id}/archive", {})
        self.changes.clear()
        self.call("DELETE", f"/threads/{thread.id}")
        self.assertIsNone(self.ws.entry(thread.id))
        self.assertEqual(self.changes, [CHANGED])

    def test_move_and_rename_redraw_without_a_bump(self):
        tid = self.thread().id
        self.publish("user_message", tid, data={})
        seq = self.seq(tid)
        self.changes.clear()
        self.call("PATCH", f"/threads/{tid}", {"project_id": self.other.id})
        self.assertEqual(self.changes, [CHANGED])
        self.call("PATCH", f"/threads/{tid}", {"title": "Renamed"})
        self.assertEqual(self.changes, [CHANGED, CHANGED])
        self.assertEqual(self.seq(tid), seq)
        (entry,) = self.call("GET", "/working")["entries"]
        self.assertEqual(entry["project_id"], self.other.id)
        # A thread outside the set redraws nothing.
        outsider = self.thread().id
        self.changes.clear()
        self.call("PATCH", f"/threads/{outsider}", {"title": "Other"})
        self.assertEqual(self.changes, [])

    def test_a_projects_archive_and_delete(self):
        mine, theirs = self.thread(self.other).id, self.thread().id
        for tid in (mine, theirs):
            self.publish("user_message", tid, data={})
        token = self.call("GET", f"/projects/{self.other.id}/impact", owner=False)["token"]
        self.call("POST", f"/projects/{self.other.id}/archive?expect={token}", {})
        self.assertEqual((self.ws.entry(mine)["reason"], self.is_in(theirs)), ("archived", True))
        self.call("POST", f"/projects/{self.other.id}/restore", {})
        self.assertFalse(self.is_in(mine), "a project's restore does not rejoin")
        token = self.call("GET", f"/projects/{self.other.id}/impact", owner=False)["token"]
        self.call("POST", f"/projects/{self.other.id}/archive?expect={token}", {})
        token = self.call("GET", f"/projects/{self.other.id}/impact", owner=False)["token"]
        self.call("DELETE", f"/projects/{self.other.id}?expect={token}")
        self.assertIsNone(self.ws.entry(mine))
        self.assertTrue(self.is_in(theirs))


# -- the read (D-5, D-6) ---------------------------------------------------------------------------


class View(Base):
    def test_every_row_exactly_once_and_the_badge(self):
        a, b = self.open_chat(), self.open_chat()
        appr_a, out_a, worker_a = self.ask(a.id)
        appr_b, out_b, worker_b = self.ask(b.id)
        self.call("POST", f"/threads/{b.id}/archive", {})         # out: its row goes to needs_you
        defer_a = self.defer(a.id)
        task = self.stores.tasks.create(self.project.id, "t")
        self.stores.tasks.save(task)
        self.stores.tasks.transition(task.id, TaskState.CLARIFYING)
        self.stores.tasks.transition(task.id, TaskState.BLOCKED, reason="waiting")
        self.addCleanup(lambda: [self.daemon.resolve_approval(r.req_id, Decision.DENY, False, owner_verified=True)
                                 for r in (appr_a, appr_b) if self.daemon.approvals.pending()])
        view = self.call("GET", "/working")
        served = inbox.rows(self.daemon, marks="flag")
        (entry,) = view["entries"]
        self.assertEqual((entry["thread_id"], entry["project_id"], entry["status"]),
                         (a.id, self.project.id, "needs_input"))
        self.assertEqual(entry["why_not"], "an approval waits on you in this chat")
        self.assertEqual(sorted(r["id"] for r in entry["rows"]), sorted([f"appr:{appr_a.req_id}", defer_a["id"]]))
        self.assertEqual(sorted(r["id"] for r in view["needs_you"]),
                         sorted([f"appr:{appr_b.req_id}", f"task:{task.id}"]))
        drawn = [r["id"] for r in entry["rows"]] + [r["id"] for r in view["needs_you"]]
        self.assertEqual(sorted(drawn), sorted(r["id"] for r in served), "every row, once")
        self.assertEqual(len(drawn), len(set(drawn)))
        self.assertEqual((view["count"], view["pending"]), (4, True))
        self.assertEqual((view["count"], view["pending"]),
                         (working.waiting_count(served), working.any_pending(served)))
        self.assertEqual(set(view), {"entries", "needs_you", "count", "pending"})
        self.assertEqual(set(entry), {"thread_id", "project_id", "since", "last", "seq", "status", "rows",
                                      "why_not"})

    def test_a_task_threads_rows_stay_in_needs_you(self):
        task = self.stores.tasks.create(self.project.id, "t")
        self.stores.tasks.save(task)
        task_thread = self.thread(task_id=task.id)
        req, out, worker = self.ask(task_thread.id, task_id=task.id)
        view = self.call("GET", "/working")
        self.assertEqual(view["entries"], [])
        self.assertEqual([r["id"] for r in view["needs_you"]], [f"appr:{req.req_id}"])
        self.daemon.resolve_approval(req.req_id, Decision.DENY, False, owner_verified=True)
        worker.join(5)

    def test_order_newest_last_first(self):
        old, new = self.thread().id, self.thread().id
        self.publish("user_message", old, data={})
        self.clock.advance(minutes=1)
        self.publish("user_message", new, data={})
        self.assertEqual([e["thread_id"] for e in self.call("GET", "/working")["entries"]], [new, old])


# -- the boundaries (§4) -----------------------------------------------------------------------------


class OwnerOnly(Base):
    ROUTES = (("GET", "/working", None), ("GET", "/working/settings", None),
              ("POST", "/working/settings", {"idle_days": 5}),
              ("POST", "/working/0000beef/done", {"seq": 0}),
              ("POST", "/working/0000beef/undo", {"token": "a" * 24}),
              ("POST", "/working/nothing", {}), ("GET", "/working/x/y/z", None))

    def test_every_route_refuses_every_client_but_the_hud(self):
        tid = self.thread().id
        self.publish("user_message", tid, data={})
        api, face = self.daemon.port, self.daemon.face_port
        for method, path, body in self.ROUTES + (("POST", f"/working/{tid}/done", {"seq": 1}),):
            with self.subTest(path=path, method=method):
                self.call(method, path, body, 403, owner=False)                          # the API listener
                self.call(method, path, body, 403, owner=False, port=face)               # the HUD's, no Origin
                self.call(method, path, body, 403, owner=False, origin=f"http://127.0.0.1:{api}")
                self.call(method, path, body, 403, owner=False, port=face,
                          origin=f"http://127.0.0.1:{self.daemon.workshop_port}")        # another page
                self.call(method, path, body, 403, owner=False, port=face, origin="null")
                self.call(method, path, body, 403, owner=False, port=face,
                          headers={"Sec-Fetch-Site": "same-site"})
        self.assertTrue(self.is_in(tid), "refused means unchanged")
        self.assertEqual(self.ws.idle_days, 3)
        # The HUD's own page reads it with a same-origin GET and no Origin.
        self.call("GET", "/working", owner=False, port=face, headers={"Sec-Fetch-Site": "same-origin"})

    def test_the_phone_listeners_mark_is_refused_first(self):
        handler = SimpleNamespace(server=SimpleNamespace(remote_only=True, server_address=("127.0.0.1",
                                                                                            self.daemon.face_port)),
                                  headers={"Host": f"127.0.0.1:{self.daemon.face_port}",
                                           "Origin": f"http://127.0.0.1:{self.daemon.face_port}"},
                                  command="GET")
        with self.assertRaises(APIError) as caught:
            working.route(handler, self.daemon, ["working"], {})
        self.assertEqual(caught.exception.status, 403)

    def test_working_changed_carries_no_ids_and_no_data(self):
        stream = self.daemon.bus.subscribe()
        self.addCleanup(self.daemon.bus.unsubscribe, stream)
        tid = self.thread().id
        self.publish("user_message", tid, data={})
        self.publish("turn_finished", tid, data={"stop": "end"})
        self.done(tid)
        records = []
        while not stream.empty():
            records.append(stream.get_nowait())
        mine = [r for r in records if r.get("kind") == "working_changed"]
        self.assertGreaterEqual(len(mine), 3)
        for record in mine:
            self.assertEqual(record, CHANGED)


class Phone(unittest.TestCase):
    """The phone's listener serves none of `/working` (its own route table)."""

    def test_the_remote_listener_serves_no_working_route(self):
        import remote_listener_check as rl
        case = type("Case", (rl.Base,), {"runTest": lambda self: None})()
        case.setUp()
        try:
            daemon = case.daemon()
            case.port = daemon.remote_port
            self.assertIsInstance(case.port, int)
            for method, path in (("GET", "/working"), ("POST", "/working/0000beef/done"),
                                 ("GET", "/working/settings"), ("GET", "/m/api/working")):
                status = case.get(path, method=method, origin=f"https://{rl.HOST}")[0]
                self.assertIn(status, (401, 403, 404), path)   # never served: no session, or no route
        finally:
            case.doCleanups()


class Unchanged(Base):
    def test_rows_phone_and_review_are_unchanged(self):
        chat = self.open_chat()
        quiet = self.thread().id
        req, out, worker = self.ask(chat.id)
        self.defer(chat.id)
        self.publish("user_message", quiet, data={})
        self.publish("turn_finished", quiet, data={"stop": "end"})

        # The review reads the quiet-hours and deferral clocks: held still,
        # so the two snapshots differ only if the working set changed them.
        fixed = datetime.now(timezone.utc)
        for guard in (patch.object(self.daemon.quiet, "clock", lambda: fixed.astimezone()),
                      patch.object(self.daemon.inbox.deferred, "clock", lambda: fixed)):
            guard.start()
            self.addCleanup(guard.stop)

        def snapshot():
            return (inbox.rows(self.daemon), inbox.rows(self.daemon, marks="flag"),
                    remote_api.inbox(self.daemon), review.report(self.daemon, phone=False),
                    review.report(self.daemon, phone=True))
        before = snapshot()
        self.call("GET", "/working")
        self.done(quiet)
        self.done(chat.id, status=409)
        self.clock.advance(days=5)
        self.ws.sweep()
        self.call("POST", "/working/settings", {"idle_days": 7})
        self.assertEqual(snapshot(), before)
        self.daemon.resolve_approval(req.req_id, Decision.DENY, False, owner_verified=True)
        worker.join(5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
