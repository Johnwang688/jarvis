"""The owner inbox, IN-0 and IN-2 (docs/plans/2026-10-24-owner-inbox-design.md §9).

Two halves:

* **The projection** (`inbox.rows`) over a fake broker, fake sessions and
  fake stores: every source state yields its row and IN-4 rank, ties go to
  the older item, a private approval shows only its private projection, and
  rows' own `title`/`blocks` never carry agent-written text.
* **The routes and dispatch** on a real daemon with loopback listeners and
  fake brains: each kind answered from the inbox reaches the same function,
  with the same arguments, as from its own surface (spies on
  `Daemon.resolve_approval`, `Daemon.answer` and the TaskControl); a second
  answer is a 409; resolving at the source removes the row within the same
  publish that carries `inbox_changed`; the routes refuse the API listener
  and any request that is not the HUD's own page; `inbox_changed` carries
  `{ids, reason}` and nothing else.

Free and hermetic: temp config and data, ephemeral ports, no model.

    systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 \\
        env PYTHONPATH=. .venv/bin/python tests/v2/inbox_check.py
"""
from __future__ import annotations

import http.client
import json
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

from jarvis import config  # noqa: E402
from jarvis.v2 import inbox  # noqa: E402
from jarvis.v2.approvals import ApprovalRequest, PendingApprovals  # noqa: E402
from jarvis.v2.control import ControlError, answer_refusal  # noqa: E402
from jarvis.v2.daemon import Daemon  # noqa: E402
from jarvis.v2.model import (OpenQuestion, ProviderName as PN, Role, Spec, Task,  # noqa: E402
                             TaskState, Thread, to_json, utcnow)
from jarvis.v2.provider import Decision, Event, EventKind as K, SessionHandle, Usage, UserMessage  # noqa: E402
from jarvis.v2.stores import Stores  # noqa: E402

SECRET = "SENTINEL-agent-text-7f3a"          # agent-written text; must never reach title/blocks/bus
ROW_ID = re.compile(r"^(appr|q|tq|prop|task):[A-Za-z0-9_:\-]+$")


def eventually(predicate, timeout=5):
    until = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > until:
            raise AssertionError("condition did not settle")
        time.sleep(0.005)


# -- the projection, over fakes -----------------------------------------------------

class FakeStore:
    def __init__(self, items=(), journals=None):
        self.items = {i.id: i for i in items}
        self.journals = journals or {}

    def list(self, **filters):
        return [i for i in self.items.values()
                if all(getattr(i, k) == v for k, v in filters.items())]

    def get(self, object_id):
        return self.items.get(object_id)

    def read_journal(self, task_id):
        return list(self.journals.get(task_id, []))


def fake_daemon(*, approvals=(), sessions=(), tasks=(), threads=(), projects=(), journals=None, now=1000.0):
    broker = SimpleNamespace(pending=lambda: list(approvals))
    stores = SimpleNamespace(tasks=FakeStore(tasks, journals), threads=FakeStore(threads),
                             projects=FakeStore(projects))
    stores.projects.cls = SimpleNamespace
    return SimpleNamespace(approvals=broker, stores=stores, _lock=threading.RLock(),
                           _sessions={s.thread.id: s for s in sessions},
                           router=SimpleNamespace(clock=lambda: now))


def request(req_id, *, asked_at, private=False, **kw):
    r = ApprovalRequest(tool=kw.pop("tool", "Bash"), args={"command": SECRET},
                        command=SECRET, reason=SECRET, provider="codex",
                        thread_id=kw.pop("thread_id", "th000001"), task_id=kw.pop("task_id", None),
                        origin=kw.pop("origin", 'task 0000beef "x"'), hud_only=private,
                        browser={"url": "https://" + SECRET} if private else None, **kw)
    r.req_id, r.code, r.asked_at, r.timeout_s = req_id, "abcd", asked_at, 120.0
    return r


def task(tid, state, *, updated="2026-10-25T10:00:00+00:00", created=None, questions=()):
    return Task(id=tid, project_id="p1", brief=SECRET, state=state, updated=updated,
                created=created or updated, spec=Spec(questions=list(questions)))


def session(thread, questions, detail, *, running=True):
    return SimpleNamespace(thread=thread, questions=set(questions), question_detail=detail,
                           worker=object() if running else None, retired=False, closing=False)


class Projection(unittest.TestCase):
    def test_every_source_state_yields_its_row_and_rank(self):
        chat = Thread(id="th000001", project_id="p1", role=Role.CHAT, provider=PN.CODEX)
        tasks = [
            task("aaaa0001", TaskState.CLARIFYING, questions=[
                OpenQuestion("which db?", blocking=True, options=["pg", "sqlite"]),
                OpenQuestion("answered", blocking=True, answer="yes"),
                OpenQuestion("assumable", blocking=False)]),
            task("aaaa0002", TaskState.INTAKE),                         # proposal, in grace
            task("aaaa0003", TaskState.INTAKE),                         # proposal, grace over
            task("aaaa0004", TaskState.INTAKE),                         # plain intake: nothing waits
            task("aaaa0005", TaskState.BLOCKED),
            task("aaaa0006", TaskState.FAILED),
            task("aaaa0007", TaskState.RUNNING),
            task("aaaa0008", TaskState.DONE),
            task("aaaa0009", TaskState.CANCELLED),
        ]
        journals = {"aaaa0002": [{"event": "proposed_by_fastpath", "not_before": 1060.0}],
                    "aaaa0003": [{"event": "proposed_by_fastpath", "not_before": 999.0}]}
        d = fake_daemon(
            approvals=[request("REQ1", asked_at="2026-10-25T10:00:00+00:00")],
            sessions=[session(chat, {"q-1"}, {"q-1": {"text": SECRET, "options": ["a", "b"],
                                                      "asked_at": "2026-10-25T10:00:00+00:00"}})],
            tasks=tasks, threads=[chat], journals=journals)
        got = {r["id"]: r for r in inbox.rows(d)}
        expect = {"appr:REQ1": ("approval", 1), "q:th000001:q-1": ("question", 2),
                  "tq:aaaa0001:0": ("task_question", 2), "prop:aaaa0002": ("proposal", 3),
                  "task:aaaa0005": ("task", 4), "task:aaaa0006": ("task", 8)}
        self.assertEqual({k: (v["kind"], v["rank"]) for k, v in got.items()}, expect)
        # Ranked; the rank order is the list order.
        self.assertEqual([r["rank"] for r in inbox.rows(d)], sorted(r["rank"] for r in got.values()))
        prop = got["prop:aaaa0002"]["detail"]
        self.assertEqual(prop["withdraw_until"], "1970-01-01T00:17:40+00:00")
        self.assertEqual(got["tq:aaaa0001:0"]["detail"],
                         {"task_id": "aaaa0001", "index": 0, "text": "which db?", "options": ["pg", "sqlite"]})
        self.assertEqual(got["q:th000001:q-1"]["detail"],
                         {"thread_id": "th000001", "req_id": "q-1", "text": SECRET, "options": ["a", "b"]})
        self.assertEqual(got["appr:REQ1"]["expires_at"], "2026-10-25T10:02:00+00:00")
        for row in got.values():
            with self.subTest(row=row["id"]):
                self.assertRegex(row["id"], ROW_ID)
                self.assertEqual(set(row), {"id", "kind", "rank", "title", "blocks", "project_id",
                                            "thread_id", "task_id", "asked_at", "expires_at",
                                            "origin", "detail"})
                # Agent text only ever inside `detail`.
                for key in ("title", "blocks", "origin"):
                    self.assertNotIn(SECRET, row[key])

    def test_a_question_on_a_turn_that_is_not_running_is_not_a_row(self):
        chat = Thread(id="th000002", project_id="p1", role=Role.CHAT, provider=PN.CODEX)
        d = fake_daemon(sessions=[session(chat, {"q-1"}, {}, running=False)], threads=[chat])
        self.assertEqual(inbox.rows(d), [])
        # The set is the truth: a detail whose id has left it is never shown.
        d = fake_daemon(sessions=[session(chat, set(), {"q-9": {"text": "old"}})], threads=[chat])
        self.assertEqual(inbox.rows(d), [])

    def test_ties_go_to_the_older_item(self):
        d = fake_daemon(
            approvals=[request("NEW", asked_at="2026-10-25T10:00:05+00:00"),
                       request("OLD", asked_at="2026-10-25T10:00:01+00:00")],
            tasks=[task("bbbb0002", TaskState.BLOCKED, updated="2026-10-25T09:00:00.500000+00:00"),
                   task("bbbb0001", TaskState.BLOCKED, updated="2026-10-25T09:00:00+00:00"),
                   task("bbbb0003", TaskState.FAILED, updated="2026-10-25T08:00:00+00:00")])
        self.assertEqual([r["id"] for r in inbox.rows(d)],
                         ["appr:OLD", "appr:NEW", "task:bbbb0001", "task:bbbb0002", "task:bbbb0003"])

    def test_a_private_approval_shows_only_its_private_fields(self):
        r = request("PRIV", asked_at="2026-10-25T10:00:00+00:00", private=True,
                    tool="hud_browser_act", task_id="cccc0001")
        r._hud_private = True
        d = fake_daemon(approvals=[r], threads=[Thread(id="th000001", project_id="p1",
                                                       role=Role.CHAT, provider=PN.CLAUDE)])
        (row,) = inbox.rows(d)
        self.assertEqual(row["detail"], r.to_json())
        self.assertEqual(set(row["detail"]), {"req_id", "code", "tool", "layer", "thread_id", "asked_at",
                                              "allowlistable", "timeout_s", "hud_only"})
        self.assertIsNone(row["task_id"])
        self.assertIsNone(row["project_id"])
        self.assertEqual(row["origin"], "")
        self.assertNotIn(SECRET, json.dumps(row))
        self.assertNotIn("cccc0001", json.dumps(row))
        # A public one's detail is exactly its card too.
        pub = request("PUB", asked_at="2026-10-25T10:00:00+00:00", task_id="cccc0002")
        (row,) = inbox.rows(fake_daemon(approvals=[pub]))
        self.assertEqual(row["detail"], pub.to_json())
        self.assertEqual(row["task_id"], "cccc0002")

    def test_an_archived_project_hides_its_tasks_but_never_an_approval(self):
        projects = [SimpleNamespace(id="p1", archived="2026-10-25T00:00:00+00:00")]
        chat = Thread(id="th000001", project_id="p1", role=Role.CHAT, provider=PN.CODEX)
        d = fake_daemon(approvals=[request("A", asked_at="2026-10-25T10:00:00+00:00")],
                        tasks=[task("dddd0001", TaskState.BLOCKED)], threads=[chat], projects=projects)
        self.assertEqual([r["id"] for r in inbox.rows(d)], ["appr:A"])

    def test_model_text_cannot_fake_a_second_line_in_a_title(self):
        r = request("T", asked_at="2026-10-25T10:00:00+00:00", tool="Bash\nAPPROVED‮")
        (row,) = inbox.rows(fake_daemon(approvals=[r]))
        self.assertNotIn("\n", row["title"])
        self.assertNotIn("‮", row["title"])


# -- the routes, on a real daemon ------------------------------------------------------

class Brain:
    """A brain whose turn asks one question (when told to) and waits for it."""

    def __init__(self, name):
        self.name = name
        self.ask = False
        self.answered = threading.Event()
        self.awaiting: set[str] = set()
        self.answers: list = []
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
            yield Event(K.QUESTION, tid, {"req_id": req_id, "text": SECRET, "options": ["npm", "pnpm"]})
            until = time.monotonic() + 5
            while req_id in self.awaiting and time.monotonic() < until:
                time.sleep(0.005)
        yield Event(K.TEXT, tid, {"text": "ok"})
        yield Event(K.TURN_FINISHED, tid, {"stop": "end"})

    def answer(self, handle, req_id, decision):
        # As the Codex provider does: an unknown or already-answered id raises.
        if req_id not in self.awaiting:
            raise ValueError("Unknown or resolved request")
        if type(decision) is not str:
            raise ValueError("A question requires text")
        self.answers.append((handle.thread_id, req_id, decision))
        self.awaiting.discard(req_id)

    def interrupt(self, handle):
        self.awaiting.clear()

    def close(self, handle):
        pass


class SpyRunner:
    """The TaskControl the routes call, recording each call. Behaves as the
    runner does for the two verbs the inbox reaches (answer_refusal, the
    router's grace-window withdraw, a published snapshot)."""

    def __init__(self, daemon):
        self.daemon, self.calls = daemon, []

    def _publish(self, task):
        self.daemon.bus.publish({"kind": "task_status_changed", "task_id": task.id,
                                 "project_id": task.project_id, "data": to_json(task)})

    def answer_question(self, task_id, index, text):
        self.calls.append(("answer_question", task_id, index, text))
        task = self.daemon.stores.tasks.get(task_id)
        refusal = answer_refusal(task_id, task.state, task.spec.questions, index, text)
        if refusal:
            raise ControlError(refusal)
        task.spec.questions[index].answer = text.strip()
        self.daemon.stores.tasks.save(task)
        self._publish(task)
        return task

    def cancel(self, task_id):
        self.calls.append(("cancel", task_id))
        if not self.daemon.router.cancel_proposal(task_id):
            self.daemon.stores.tasks.transition(task_id, TaskState.CANCELLED, reason="owner cancelled")
        task = self.daemon.stores.tasks.get(task_id)
        self._publish(task)
        return task


class Live(unittest.TestCase):
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
        self.stores = Stores()
        self.brain = Brain(PN.FAST)
        self.daemon = Daemon(self.stores, {name: (self.brain if name == PN.FAST else Brain(name)) for name in PN},
                             lambda *_: lambda *_: Decision.DENY, 0)
        self.runner = SpyRunner(self.daemon)
        self.daemon.runner = self.runner
        self.daemon.start()
        self.addCleanup(self.daemon.stop)
        self.project = self.stores.projects.create("Calc", str(self.root / "repo"))
        self.changes: list[dict] = []
        # A second observer, after the inbox's: when `inbox_changed` arrives,
        # what does the projection say at that same moment?
        self.seen_at_change: list[tuple[list, list]] = []

        def watch(record):
            if record.get("kind") == "inbox_changed":
                self.changes.append(record)
                self.seen_at_change.append((record["data"]["ids"],
                                            [r["id"] for r in inbox.rows(self.daemon)]))
        self.daemon.bus.observe(watch)

    # -- wire helpers ------------------------------------------------------------

    def call(self, method, path, body=None, status=200, *, owner=True, port=None, origin=None):
        port = port or (self.daemon.face_port if owner else self.daemon.port)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if owner:
            headers["Origin"] = origin or f"http://127.0.0.1:{port}"
        elif origin:
            headers["Origin"] = origin
        try:
            conn.request(method, path, json.dumps(body).encode() if body is not None else None, headers)
            response = conn.getresponse()
            content = response.read()
            self.assertEqual(response.status, status, content[:400])
            return json.loads(content) if content else None
        finally:
            conn.close()

    def ids(self):
        return [r["id"] for r in self.call("GET", "/inbox")]

    def ask(self, **kw):
        """An approval asked on a background thread, as a permit asks."""
        req = ApprovalRequest(tool=kw.pop("tool", "Bash"), args={"command": "make deploy"},
                              command="make deploy", provider="codex", **kw)
        out = {}
        worker = threading.Thread(target=lambda: out.setdefault("decision", self.daemon.approvals.ask(req)),
                                  daemon=True)
        worker.start()
        # Announced, not only admitted: the broker admits the ask before it
        # tells the bus, so wait for the inbox to have said so.
        eventually(lambda: req.req_id and any(f"appr:{req.req_id}" in c["data"]["ids"]
                                              for c in self.changes))
        return req, out, worker

    def spy(self, name):
        calls, real = [], getattr(self.daemon, name)

        def wrapper(*args, **kwargs):
            calls.append((args, kwargs))
            return real(*args, **kwargs)
        guard = patch.object(self.daemon, name, wrapper)
        guard.start()
        self.addCleanup(guard.stop)
        return calls

    def clarifying(self, text="which db?"):
        t = self.stores.tasks.create(self.project.id, "add multiply")
        self.stores.tasks.save(t)
        t = self.stores.tasks.transition(t.id, TaskState.CLARIFYING)
        t.spec = Spec(goal="g", questions=[OpenQuestion(text, blocking=True, options=["pg"])])
        self.stores.tasks.save(t)
        return t

    def proposal(self, grace=60.0):
        t = self.stores.tasks.create(self.project.id, "add multiply")
        self.stores.tasks.save(t)
        self.stores.tasks.journal(t.id, "proposed_by_fastpath", brief="add multiply", thread_id=None,
                                  turn_id=None, reply="", not_before=self.daemon.router.clock() + grace)
        return t

    def question(self):
        thread = self.daemon.open_thread(self.project.id, Role.CHAT, PN.FAST, {})
        self.brain.ask = True
        self.daemon.send(thread.id, UserMessage("go"))
        eventually(lambda: any(i.startswith(f"q:{thread.id}:") for i in
                               (r["id"] for r in inbox.rows(self.daemon))))
        (row,) = [r for r in inbox.rows(self.daemon) if r["kind"] == "question"]
        return thread, row

    # -- IN-2: the same function, the same arguments ------------------------------------

    def test_an_approval_answered_here_is_the_card_answered_there(self):
        calls = self.spy("resolve_approval")
        a, a_out, a_worker = self.ask(thread_id="th-a")
        b, b_out, b_worker = self.ask(thread_id="th-b")
        self.assertEqual(set(self.ids()), {f"appr:{a.req_id}", f"appr:{b.req_id}"})
        self.call("POST", f"/approvals/{a.req_id}", {"decision": "allow", "always": True})
        got = self.call("POST", f"/inbox/appr:{b.req_id}/answer", {"decision": "allow", "always": True})
        a_worker.join(5)
        b_worker.join(5)
        self.assertEqual((a_out["decision"], b_out["decision"]), (Decision.ALLOW, Decision.ALLOW))
        (own_args, own_kw), (inbox_args, inbox_kw) = calls
        self.assertEqual(own_args[0], a.req_id)
        self.assertEqual(inbox_args[0], b.req_id)
        self.assertEqual((own_args[1:], own_kw), (inbox_args[1:], inbox_kw))
        self.assertEqual(inbox_kw, {"hud": True})
        self.assertEqual(inbox_args[1:], (Decision.ALLOW, True))
        self.assertEqual(got["req_id"], b.req_id)
        # `always` went through the broker's own rule: one stem entry, minted.
        self.assertTrue(got["allowlisted"])
        # A second answer is a 409, never a guess; so is one for a card the
        # owner answered on its own surface.
        self.call("POST", f"/inbox/appr:{b.req_id}/answer", {"decision": "deny"}, 409)
        self.call("POST", f"/inbox/appr:{a.req_id}/answer", {"decision": "deny"}, 409)
        self.assertEqual(len(calls), 2, "a 409 reaches no function")
        self.assertEqual(self.ids(), [])

    def test_a_private_card_keeps_its_hud_rule(self):
        calls = self.spy("resolve_approval")
        req, out, worker = self.ask(thread_id="th-p", hud_only=True, tool="hud_browser_act",
                                    browser={"url": "https://" + SECRET})
        (row,) = self.call("GET", "/inbox")
        self.assertEqual(row["detail"], req.to_json())
        self.assertNotIn(SECRET, json.dumps(row))
        self.call("POST", f"/inbox/appr:{req.req_id}/answer", {"decision": "allow"})
        worker.join(5)
        self.assertEqual(out["decision"], Decision.ALLOW)
        self.assertEqual(calls, [((req.req_id, Decision.ALLOW, False), {"hud": True})])

    def test_a_timed_out_approval_leaves_and_cannot_be_answered(self):
        req, out, worker = self.ask(thread_id="th-t", max_wait_s=0.3)
        self.assertEqual(self.ids(), [f"appr:{req.req_id}"])
        worker.join(5)
        self.assertEqual(out["decision"], Decision.DENY)
        self.assertEqual(self.ids(), [])
        self.call("POST", f"/inbox/appr:{req.req_id}/answer", {"decision": "allow"}, 409)

    def test_a_chat_question_answered_here_is_the_answer_typed_there(self):
        calls = self.spy("answer")
        thread, row = self.question()
        self.assertEqual(row["detail"]["text"], SECRET)
        self.assertNotIn(SECRET, row["title"] + row["blocks"])
        req_id = row["detail"]["req_id"]
        # Its own surface first, on this question...
        self.call("POST", f"/threads/{thread.id}/answer", {"req_id": req_id, "text": "pnpm"}, owner=False)
        eventually(lambda: not any(r["kind"] == "question" for r in inbox.rows(self.daemon)))
        # ...then the next one, from the inbox.
        eventually(lambda: self.daemon._sessions[thread.id].worker is None)
        self.daemon.send(thread.id, UserMessage("again"))
        eventually(lambda: any(r["kind"] == "question" for r in inbox.rows(self.daemon)))
        (row2,) = [r for r in inbox.rows(self.daemon) if r["kind"] == "question"]
        self.call("POST", f"/inbox/{row2['id']}/answer", {"text": "pnpm"})
        self.assertEqual(calls, [((thread.id, req_id, "pnpm"), {}),
                                 ((thread.id, row2["detail"]["req_id"], "pnpm"), {})])
        self.assertEqual([a[1] for a in self.brain.answers], [req_id, row2["detail"]["req_id"]])
        self.call("POST", f"/inbox/{row2['id']}/answer", {"text": "npm"}, 409)
        self.call("POST", f"/inbox/{row['id']}/answer", {"text": "npm"}, 409)
        self.assertEqual(len(calls), 2)

    def test_a_task_question_answered_here_is_the_task_answer(self):
        one, two = self.clarifying(), self.clarifying()
        self.assertEqual(set(self.ids()), {f"tq:{one.id}:0", f"tq:{two.id}:0"})
        self.call("POST", f"/tasks/{one.id}/answer", {"index": 0, "text": "pg"}, owner=False)
        self.call("POST", f"/inbox/tq:{two.id}:0/answer", {"text": "pg"})
        self.assertEqual(self.runner.calls, [("answer_question", one.id, 0, "pg"),
                                             ("answer_question", two.id, 0, "pg")])
        self.assertEqual(self.ids(), [])
        self.call("POST", f"/inbox/tq:{two.id}:0/answer", {"text": "sqlite"}, 409)
        self.call("POST", f"/inbox/tq:{one.id}:0/answer", {"text": "sqlite"}, 409)
        self.call("POST", f"/inbox/tq:{one.id}:7/answer", {"text": "sqlite"}, 409)
        self.assertEqual(len(self.runner.calls), 2)
        self.assertEqual(self.stores.tasks.get(two.id).spec.questions[0].answer, "pg")

    def test_a_proposal_withdrawn_here_is_the_task_cancel(self):
        one, two = self.proposal(), self.proposal()
        self.assertEqual(set(self.ids()), {f"prop:{one.id}", f"prop:{two.id}"})
        self.call("POST", f"/tasks/{one.id}/cancel", {}, owner=False)
        self.call("POST", f"/inbox/prop:{two.id}/answer", {"withdraw": True})
        self.assertEqual(self.runner.calls, [("cancel", one.id), ("cancel", two.id)])
        self.assertEqual(self.stores.tasks.get(two.id).state, TaskState.CANCELLED)
        self.call("POST", f"/inbox/prop:{two.id}/answer", {"withdraw": True}, 409)
        # A proposal whose window has closed is gone, not cancelled.
        late = self.proposal(grace=-1)
        self.call("POST", f"/inbox/prop:{late.id}/answer", {"withdraw": True}, 409)
        self.assertEqual(self.stores.tasks.get(late.id).state, TaskState.INTAKE)
        self.assertEqual(len(self.runner.calls), 2)

    def test_blocked_and_failed_tasks_are_rows_with_nothing_to_answer(self):
        t = self.stores.tasks.create(self.project.id, "x")
        self.stores.tasks.save(t)
        self.stores.tasks.transition(t.id, TaskState.CLARIFYING)
        self.stores.tasks.transition(t.id, TaskState.BLOCKED)
        f = self.stores.tasks.create(self.project.id, "y")
        self.stores.tasks.save(f)
        for step in (TaskState.CLARIFYING, TaskState.PLANNED, TaskState.RUNNING, TaskState.FAILED):
            self.stores.tasks.transition(f.id, step)
        rows = self.call("GET", "/inbox")
        self.assertEqual([(r["id"], r["rank"]) for r in rows], [(f"task:{t.id}", 4), (f"task:{f.id}", 8)])
        self.call("POST", f"/inbox/task:{t.id}/answer", {}, 400)
        self.assertEqual(self.runner.calls, [])

    # -- fail closed ---------------------------------------------------------------

    def test_unknown_kinds_and_malformed_bodies_are_refused(self):
        req, out, worker = self.ask(thread_id="th-m")
        rid = f"appr:{req.req_id}"
        for path, body in ((f"/inbox/bogus:{req.req_id}/answer", {"decision": "allow"}),
                           (f"/inbox/{req.req_id}/answer", {"decision": "allow"}),
                           ("/inbox/appr:/answer", {"decision": "allow"}),
                           (f"/inbox/{rid}/answer", {"decision": "maybe"}),
                           (f"/inbox/{rid}/answer", {"decision": "allow", "always": "yes"}),
                           (f"/inbox/{rid}/answer", {"decision": "allow", "extra": 1}),
                           (f"/inbox/{rid}/answer", {}),
                           (f"/inbox/{rid}/answer", []),
                           ("/inbox/q:x:y/answer", {"decision": "deny"}),
                           ("/inbox/tq:x:0/answer", {"index": 0, "text": "a"}),
                           ("/inbox/prop:x/answer", {}),
                           ("/inbox/prop:x/answer", {"withdraw": 1})):
            with self.subTest(path=path, body=body):
                self.call("POST", path, body, 400)
        self.call("GET", "/inbox?all=1", None, 400)
        self.call("POST", "/inbox", {}, 404)
        self.call("GET", f"/inbox/{rid}", None, 404)
        self.assertEqual(self.ids(), [rid], "nothing was answered")
        self.daemon.approvals.resolve(req.req_id, Decision.DENY)
        worker.join(5)

    def test_the_routes_refuse_the_api_listener_and_non_owner_pages(self):
        req, out, worker = self.ask(thread_id="th-o")
        rid = f"appr:{req.req_id}"
        api, hud = self.daemon.port, self.daemon.face_port
        for method, path, body in (("GET", "/inbox", None),
                                   ("POST", f"/inbox/{rid}/answer", {"decision": "allow"})):
            with self.subTest(path=path):
                self.call(method, path, body, 403, owner=False)                          # a tool's client
                self.call(method, path, body, 403, owner=False, origin=f"http://127.0.0.1:{api}")
                self.call(method, path, body, 403, owner=False, port=hud)                # HUD port, no Origin
                self.call(method, path, body, 403, owner=False, port=hud,
                          origin=f"http://127.0.0.1:{self.daemon.workshop_port}")
                self.call(method, path, body, 403, owner=False, port=hud, origin="null")
        self.assertEqual([r.req_id for r in self.daemon.approvals.pending()], [req.req_id],
                         "refused means unanswered")
        self.daemon.approvals.resolve(req.req_id, Decision.DENY)
        worker.join(5)

    # -- inbox_changed and the same event cycle --------------------------------------

    def test_resolving_at_the_source_removes_the_row_in_the_same_cycle(self):
        req, out, worker = self.ask(thread_id="th-c")
        rid = f"appr:{req.req_id}"
        ids, rows_then = self.seen_at_change[-1]
        self.assertEqual((ids, rows_then), ([rid], [rid]))
        self.daemon.approvals.resolve(req.req_id, Decision.DENY)      # Discord's in-process path
        worker.join(5)
        ids, rows_then = self.seen_at_change[-1]
        self.assertEqual(ids, [rid])
        self.assertNotIn(rid, rows_then)
        # A chat question: asked, then answered at its own surface.
        thread, row = self.question()
        self.assertIn(([row["id"]], [row["id"]]), self.seen_at_change)
        self.daemon.answer(thread.id, row["detail"]["req_id"], "pnpm")
        ids, rows_then = self.seen_at_change[-1]
        self.assertEqual(ids, [row["id"]])
        self.assertNotIn(row["id"], rows_then)
        # A task question answered by the runner.
        t = self.clarifying()
        self.runner.answer_question(t.id, 0, "pg")
        ids, rows_then = self.seen_at_change[-1]
        self.assertIn(f"tq:{t.id}:0", ids)
        self.assertNotIn(f"tq:{t.id}:0", rows_then)

    def test_a_turn_that_ends_with_its_question_open_says_so(self):
        thread, row = self.question()
        self.daemon.interrupt(thread.id)
        eventually(lambda: any(row["id"] in c["data"]["ids"] and c["data"]["reason"] == "turn_finished"
                               for c in self.changes))
        self.assertEqual([r for r in inbox.rows(self.daemon) if r["kind"] == "question"], [])

    def test_inbox_changed_carries_no_text(self):
        req, out, worker = self.ask(thread_id="th-x", reason=SECRET)
        self.call("POST", f"/inbox/appr:{req.req_id}/answer", {"decision": "deny"})
        worker.join(5)
        thread, row = self.question()
        self.call("POST", f"/inbox/{row['id']}/answer", {"text": SECRET})
        t = self.clarifying(text=SECRET)
        self.daemon.bus.publish({"kind": "task_question", "task_id": t.id, "project_id": t.project_id,
                                 "data": {"questions": [to_json(q) for q in t.spec.questions],
                                          "task": to_json(t)}})
        self.call("POST", f"/inbox/tq:{t.id}:0/answer", {"text": SECRET})
        p = self.proposal()
        self.daemon.bus.publish({"kind": "proposal_reply", "thread_id": thread.id, "project_id": p.project_id,
                                 "data": {"turn_id": "t", "reply": SECRET, "task_id": p.id}})
        self.call("POST", f"/inbox/prop:{p.id}/answer", {"withdraw": True})
        reasons = {c["data"]["reason"] for c in self.changes}
        self.assertTrue({"approval_requested", "approval_resolved", "question", "question_answered",
                         "task_question", "task_status_changed", "proposal_reply"} <= reasons, reasons)
        for change in self.changes:
            with self.subTest(change=change):
                self.assertEqual(set(change), {"kind", "data"})
                self.assertEqual(set(change["data"]), {"ids", "reason"})
                self.assertIn(change["data"]["reason"], inbox.SOURCE_KINDS)
                for i in change["data"]["ids"]:
                    self.assertRegex(i, ROW_ID)
                self.assertNotIn(SECRET, json.dumps(change))
        self.assertNotIn("inbox_changed", inbox.SOURCE_KINDS, "the inbox never answers itself")


if __name__ == "__main__":
    unittest.main(verbosity=2)
