"""The owner inbox, IN-0, IN-2 and IN-3 (docs/plans/2026-10-24-owner-inbox-design.md §9).

Three parts (the third is `Missed` and `MissedLive`, below):

* **"Denied while you were away"** (IN-3, `inbox.MissedLog`): a timed-out
  or shut-down ask leaves one record of public `to_json` fields (a private
  card's, only its private projection's), N asks at shutdown leave N, a
  write failure never breaks the resolve, the file is bounded and 0600 in a
  0700 folder, rows sit at rank 8 with code-built title/blocks, answering
  one is a 400, `inbox_changed` names it with no text, only the last 7 days
  show, and the file is protected state refused to agent writes.

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
# A thread title can be model-written (titles.py): a row's own title/blocks
# name ids, never it (review of #303: `chat {thread.title or thread.id}`
# survived every check before this sentinel).
TITLE = "SENTINEL-thread-title-91c2"
ROW_ID = re.compile(r"^(appr|q|tq|prop|task|missed):[A-Za-z0-9_:\-]+$")
# A "denied while you were away" record's fields (design §3.2), no more.
MISSED_FIELDS = {"req_id", "tool", "origin", "thread_id", "task_id", "at", "resolution"}


def assert_code_built(case, rows):
    """Each row's `title` and `blocks` carry no model-written text."""
    for row in rows:
        for key in ("title", "blocks"):
            for sentinel in (SECRET, TITLE):
                case.assertNotIn(sentinel, row[key], f"{row['id']} {key}")


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
        chat = Thread(title=TITLE, id="th000001", project_id="p1", role=Role.CHAT, provider=PN.CODEX)
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
                # Agent text never in the code-built fields. `origin` is the
                # approval's `approvals.label` attribution, model-chosen name
                # included: one clean line, rendered as untrusted text.
                self.assertNotIn("\n", row["origin"])
        assert_code_built(self, got.values())
        self.assertEqual(got["appr:REQ1"]["origin"], 'task 0000beef "x"')

    def test_a_question_on_a_turn_that_is_not_running_is_not_a_row(self):
        chat = Thread(title=TITLE, id="th000002", project_id="p1", role=Role.CHAT, provider=PN.CODEX)
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
        d = fake_daemon(approvals=[r], threads=[Thread(title=TITLE, id="th000001", project_id="p1",
                                                       role=Role.CHAT, provider=PN.CLAUDE)])
        (row,) = inbox.rows(d)
        self.assertEqual(row["detail"], r.to_json())
        self.assertEqual(set(row["detail"]), {"req_id", "code", "tool", "layer", "thread_id", "asked_at",
                                              "allowlistable", "timeout_s", "hud_only"})
        self.assertIsNone(row["task_id"])
        self.assertIsNone(row["project_id"])
        self.assertEqual(row["origin"], "")
        self.assertNotIn(SECRET, json.dumps(row))
        self.assertNotIn(TITLE, json.dumps(row))
        self.assertNotIn("cccc0001", json.dumps(row))
        # A public one's detail is exactly its card too.
        pub = request("PUB", asked_at="2026-10-25T10:00:00+00:00", task_id="cccc0002")
        (row,) = inbox.rows(fake_daemon(approvals=[pub]))
        self.assertEqual(row["detail"], pub.to_json())
        self.assertEqual(row["task_id"], "cccc0002")

    def test_an_archived_project_hides_its_tasks_but_never_an_approval(self):
        projects = [SimpleNamespace(id="p1", archived="2026-10-25T00:00:00+00:00")]
        chat = Thread(title=TITLE, id="th000001", project_id="p1", role=Role.CHAT, provider=PN.CODEX)
        d = fake_daemon(approvals=[request("A", asked_at="2026-10-25T10:00:00+00:00")],
                        tasks=[task("dddd0001", TaskState.BLOCKED)], threads=[chat], projects=projects)
        self.assertEqual([r["id"] for r in inbox.rows(d)], ["appr:A"])
        assert_code_built(self, inbox.rows(d))

    def test_a_task_thread_question_names_its_task_not_its_title(self):
        chat = Thread(title=TITLE, id="th000003", project_id="p1", role=Role.ORCHESTRATOR,
                      provider=PN.CODEX, task_id="eeee0001")
        d = fake_daemon(sessions=[session(chat, {"q-1"}, {"q-1": {"text": SECRET}})], threads=[chat],
                        approvals=[request("B", asked_at="2026-10-25T10:00:00+00:00", thread_id="th000003")])
        rows = inbox.rows(d)
        self.assertEqual([r["id"] for r in rows], ["appr:B", "q:th000003:q-1"])
        self.assertEqual(rows[1]["title"], "task eeee0001 is stopped until you answer")
        assert_code_built(self, rows)

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
        # Set: the turn ends with its question still open, as a provider
        # turn that errors or gives up does — no answer, no interrupt.
        self.give_up = threading.Event()

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
            while (req_id in self.awaiting and not self.give_up.is_set()
                   and time.monotonic() < until):
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
        # A model-written title, as titles.py writes one: never in a row's
        # own fields. `set_title` is the one writer; the session's copy too.
        self.stores.threads.set_title(thread.id, TITLE, "auto")
        self.daemon._sessions[thread.id].thread.title = TITLE
        self.brain.ask = True
        self.daemon.send(thread.id, UserMessage("go"))
        eventually(lambda: any(i.startswith(f"q:{thread.id}:") for i in
                               (r["id"] for r in inbox.rows(self.daemon))))
        (row,) = [r for r in inbox.rows(self.daemon) if r["kind"] == "question"]
        assert_code_built(self, inbox.rows(self.daemon))
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
        # The live row is gone; the "denied while you were away" one (IN-3)
        # takes its place, and neither takes an answer.
        self.assertEqual(self.ids(), [f"missed:{req.req_id}"])
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

    def _ended_open(self, end):
        thread, row = self.question()
        end(thread)

        def naming():
            return [(ids, then) for ids, then in self.seen_at_change if row["id"] in ids]
        eventually(lambda: len(naming()) >= 2)                        # asked, then ended
        # At the moment `inbox_changed` names the row, the projection has
        # already dropped it: no later event would correct it (review of #303).
        ids, rows_then = naming()[-1]
        self.assertNotIn(row["id"], rows_then)
        return thread, row

    def test_a_turn_that_ends_with_its_question_open_says_so(self):
        # The provider ends the turn itself with the question unanswered.
        thread, row = self._ended_open(lambda thread: self.brain.give_up.set())
        reasons = [c["data"]["reason"] for c in self.changes if row["id"] in c["data"]["ids"]]
        self.assertEqual(reasons, ["question", "turn_finished"])
        # Answering it now is a 409, never a guess.
        self.call("POST", f"/inbox/{row['id']}/answer", {"text": "npm"}, 409)

    def test_an_interrupted_question_says_so(self):
        self._ended_open(lambda thread: self.daemon.interrupt(thread.id))

    def test_a_percent_encoded_id_is_the_same_row(self):
        """The HUD sends path ids through `encodeURIComponent` (api.ts)."""
        req, out, worker = self.ask(thread_id="th-e")
        from urllib.parse import quote
        encoded = quote(f"appr:{req.req_id}", safe="")
        self.assertIn("%3A", encoded)
        self.call("POST", f"/inbox/{encoded}/answer", {"decision": "deny"})
        worker.join(5)
        self.assertEqual(out["decision"], Decision.DENY)
        self.call("POST", f"/inbox/{encoded}/answer", {"decision": "deny"}, 409)
        # Decoded once only, and an id no row names is still a 409, never a match.
        self.call("POST", f"/inbox/{quote(encoded, safe='')}/answer", {"decision": "deny"}, 400)
        self.call("POST", "/inbox/appr%3Anope/answer", {"decision": "deny"}, 409)
        self.call("POST", "/inbox/appr%3A%FF/answer", {"decision": "deny"}, 400)

    def test_a_project_archive_restore_or_delete_rereads(self):
        for kind in ("project_archived", "project_restored", "project_deleted"):
            self.daemon.bus.publish({"kind": kind, "project_id": self.project.id,
                                     "data": {"project_id": self.project.id}})
        self.assertEqual([(c["data"]["reason"], c["data"]["ids"]) for c in self.changes],
                         [("project_archived", []), ("project_restored", []), ("project_deleted", [])])

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


# -- IN-3: "denied while you were away" ------------------------------------------------

class Clock:
    def __init__(self, at="2026-10-25T12:00:00+00:00"):
        from datetime import datetime
        self.now = datetime.fromisoformat(at)

    def __call__(self):
        return self.now

    def advance(self, **delta):
        from datetime import timedelta
        self.now += timedelta(**delta)


def lines_of(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def mode(path):
    return os.stat(path).st_mode & 0o777


class Missed(unittest.TestCase):
    """`MissedLog` and the `missed` rows, over a temp data root and a fake clock."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.clock = Clock()
        self.log = inbox.MissedLog(self.root, clock=self.clock)

    def daemon(self, **kw):
        d = fake_daemon(**kw)
        d.inbox = SimpleNamespace(missed=self.log)
        return d

    def test_a_record_holds_public_fields_only(self):
        r = request("PUBa1", asked_at="2026-10-25T11:58:00+00:00", task_id="tk000001",
                    origin=f'task tk000001 "{SECRET}"', headline=SECRET)
        self.assertTrue(self.log.record(r, "timeout"))
        (rec,) = lines_of(self.log.path)
        self.assertEqual(set(rec), MISSED_FIELDS)
        self.assertEqual(rec, {"req_id": "PUBa1", "tool": "Bash", "origin": f'task tk000001 "{SECRET}"',
                               "thread_id": "th000001", "task_id": "tk000001",
                               "at": "2026-10-25T12:00:00+00:00", "resolution": "timeout"})
        # Not the command, its arguments, its reason or headline: no card text.
        text = self.log.path.read_text()
        self.assertEqual(text.count(SECRET), 1, "only the origin's attribution, nothing of the card")

    def test_a_private_card_records_only_its_private_fields(self):
        r = request("PRIVa1", asked_at="2026-10-25T11:58:00+00:00", private=True, tool="hud_browser_act",
                    task_id="tk000002", origin=f'task tk000002 "{SECRET}"', headline=SECRET)
        r._hud_private = True
        self.assertTrue(self.log.record(r, "shutdown"))
        (rec,) = lines_of(self.log.path)
        self.assertEqual(rec, {"req_id": "PRIVa1", "tool": "hud_browser_act", "origin": "",
                               "thread_id": "th000001", "task_id": None,
                               "at": "2026-10-25T12:00:00+00:00", "resolution": "shutdown"})
        text = self.log.path.read_text()
        for leak in (SECRET, "tk000002", "https://"):
            self.assertNotIn(leak, text)
        (row,) = inbox.rows(self.daemon())
        self.assertEqual((row["origin"], row["task_id"], row["project_id"]), ("", None, None))
        self.assertNotIn(SECRET, json.dumps(row))
        self.assertNotIn("tk000002", json.dumps(row))

    def test_only_timeout_and_shutdown_are_recorded(self):
        for resolution in ("deny", "allow", "approved-always", "cancelled", "nowhere-to-ask",
                           "invalid-timeout", "", "TIMEOUT"):
            with self.subTest(resolution=resolution):
                self.assertFalse(self.log.record(request("R1", asked_at=utcnow()), resolution))
        self.assertFalse(self.log.path.exists())
        # Never admitted (a closed broker's ask has no id): no waiter ended.
        self.assertFalse(self.log.record(request("", asked_at=""), "shutdown"))
        self.assertFalse(self.log.path.exists())

    def test_the_file_keeps_the_newest_500(self):
        self.assertEqual(inbox.MISSED_KEEP, 500)
        for i in range(inbox.MISSED_KEEP + 7):
            self.assertTrue(self.log.record(request(f"B{i:04d}", asked_at=utcnow()), "timeout"))
        recs = lines_of(self.log.path)
        self.assertEqual(len(recs), inbox.MISSED_KEEP)
        self.assertEqual((recs[0]["req_id"], recs[-1]["req_id"]), ("B0007", "B0506"))
        self.assertEqual(mode(self.log.path), 0o600, "the rewrite keeps the file private")
        self.assertEqual(len(inbox.rows(self.daemon())), inbox.MISSED_KEEP)
        # No temp file left beside it.
        self.assertEqual(sorted(p.name for p in self.log.dir.iterdir()), ["missed.jsonl"])

    def test_a_torn_last_line_is_dropped_never_glued_onto(self):
        self.log.record(request("T1", asked_at=utcnow()), "timeout")
        with open(self.log.path, "ab") as f:
            f.write(b'{"req_id":"T2","tool":"Ba')          # a crash mid-append
        self.assertEqual([r["id"] for r in inbox.rows(self.daemon())], ["missed:T1"])
        self.log.record(request("T3", asked_at=utcnow()), "timeout")
        self.assertEqual([r["req_id"] for r in lines_of(self.log.path)], ["T1", "T3"])

    def test_modes_are_0600_and_0700_whatever_the_umask_or_what_was_there(self):
        old = os.umask(0)
        try:
            self.log.record(request("M1", asked_at=utcnow()), "timeout")
        finally:
            os.umask(old)
        self.assertEqual((mode(self.log.dir), mode(self.log.path)), (0o700, 0o600))
        os.chmod(self.log.dir, 0o755)
        os.chmod(self.log.path, 0o644)
        self.log.record(request("M2", asked_at=utcnow()), "timeout")
        self.assertEqual((mode(self.log.dir), mode(self.log.path)), (0o700, 0o600), "tightened when found wider")

    def test_a_symlinked_folder_or_file_is_refused(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.log.dir.symlink_to(elsewhere, target_is_directory=True)
        self.assertFalse(self.log.record(request("S1", asked_at=utcnow()), "timeout"))
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.log.dir.unlink()
        self.log.dir.mkdir(mode=0o700)
        target = self.root / "target.jsonl"
        target.write_text("")
        self.log.path.symlink_to(target)
        self.assertFalse(self.log.record(request("S2", asked_at=utcnow()), "timeout"))
        self.assertEqual(target.read_text(), "")
        self.assertEqual(self.log.recent(), [])

    def test_rows_sit_at_rank_8_with_code_built_title_and_blocks(self):
        chat = Thread(title=TITLE, id="th000001", project_id="p1", role=Role.CHAT, provider=PN.CODEX)
        self.log.record(request("RW1", asked_at=utcnow(), origin=f'task tk000003 "{SECRET}"',
                                task_id="tk000003"), "timeout")
        self.clock.advance(seconds=5)
        self.log.record(request("RW2", asked_at=utcnow(), origin=TITLE), "shutdown")
        d = self.daemon(threads=[chat], tasks=[task("ffff0001", TaskState.FAILED,
                                                    updated="2026-10-25T12:00:02+00:00")])
        got = inbox.rows(d)
        self.assertEqual([(r["id"], r["kind"], r["rank"]) for r in got],
                         [("missed:RW1", "missed", 8), ("task:ffff0001", "task", 8),
                          ("missed:RW2", "missed", 8)], "rank 8, ties to the older item")
        one, two = got[0], got[2]
        self.assertEqual(one["title"], "Denied while you were away: Bash")
        self.assertEqual(one["blocks"], "task tk000003 was denied it: nobody answered in time")
        self.assertEqual(two["blocks"], "chat th000001 was denied it: Jarvis stopped before you answered")
        self.assertEqual((one["thread_id"], one["task_id"], one["project_id"], one["expires_at"]),
                         ("th000001", "tk000003", None, None))
        self.assertEqual(one["asked_at"], "2026-10-25T12:00:00+00:00")
        self.assertEqual(one["origin"], f'task tk000003 "{SECRET}"')
        self.assertEqual(set(one["detail"]), MISSED_FIELDS)
        for row in got:
            self.assertRegex(row["id"], ROW_ID)
        assert_code_built(self, got)
        # A tool name cannot fake a second line in a title either.
        self.log.record(request("RW3", asked_at=utcnow(), tool="Bash\nAPPROVED‮"), "timeout")
        row = next(r for r in inbox.rows(d) if r["id"] == "missed:RW3")
        self.assertNotIn("\n", row["title"])
        self.assertNotIn("‮", row["title"])

    def test_only_the_last_7_days_show(self):
        self.assertEqual(inbox.MISSED_WINDOW.days, 7)
        self.log.record(request("W1", asked_at=utcnow()), "timeout")
        self.clock.advance(days=3)
        self.log.record(request("W2", asked_at=utcnow()), "timeout")
        ids = lambda: [r["id"] for r in inbox.rows(self.daemon())]  # noqa: E731
        self.clock.advance(days=3, hours=23, minutes=59)
        self.assertEqual(ids(), ["missed:W1", "missed:W2"])
        self.clock.advance(minutes=2)
        self.assertEqual(ids(), ["missed:W2"], "older than 7 days leaves the list")
        self.assertEqual(len(lines_of(self.log.path)), 2, "the window hides; the bound trims")
        self.clock.advance(days=3, minutes=1)
        self.assertEqual(ids(), [])

    def test_malformed_lines_are_never_rows(self):
        self.log.record(request("G1", asked_at=utcnow()), "timeout")
        good = {"req_id": "G2", "tool": "Bash", "origin": "", "thread_id": None, "task_id": None,
                "at": "2026-10-25T12:00:00+00:00", "resolution": "timeout"}
        bad = ["not json", "[]", json.dumps({**good, "req_id": "a b"}), json.dumps({**good, "req_id": 7}),
               json.dumps({**good, "resolution": "allow"}), json.dumps({**good, "at": "yesterday"}),
               json.dumps({**good, "req_id": "x" * 65})]
        with open(self.log.path, "a") as f:
            f.write("\n".join(bad + [json.dumps(good)]) + "\n")
        self.assertEqual([r["id"] for r in inbox.rows(self.daemon())], ["missed:G1", "missed:G2"])

    def test_a_daemon_without_the_log_has_no_missed_rows(self):
        self.assertEqual(inbox.rows(fake_daemon()), [])

    def test_the_file_is_protected_state_refused_to_agent_writes(self):
        from jarvis import permissions as v1_permissions, rules
        from jarvis.tools import files
        from jarvis.v2 import permissions as v2_permissions
        data = self.root / "v2data"
        with patch.object(config, "V2_DATA_DIR", data), \
                patch.object(config, "ALLOWLIST_PATH", self.root / "cfg" / "allowlist.json"):
            target = data / "inbox" / "missed.jsonl"
            self.assertIn(target.resolve(), files._protected_state())
            self.assertIn(target.resolve(), v2_permissions.protected_paths())
            self.assertTrue(files._self_protected(target))
            self.assertTrue(v2_permissions.denied_file(str(target)))
            for command in (f"echo x > {target}", f"cp /tmp/x {target}", f"rm {target}", f": > {target}"):
                with self.subTest(command=command):
                    self.assertNotEqual(v1_permissions.static_verdict(command).decision, rules.ALLOW)
                    self.assertTrue(v2_permissions._command_writes_protected_state(command))
            # Deleting the folder that holds it is no free pass either (the
            # shell gate's ancestor rule; as for the speaker files).
            for command in (f"rm -rf {target.parent}", f"cd {data} && rm -rf inbox"):
                with self.subTest(command=command):
                    self.assertNotEqual(v1_permissions.static_verdict(command).decision, rules.ALLOW)
            self.assertIsNone(v2_permissions._command_writes_protected_state(f"cat {target}"),
                              "reading it is not writing it")
            # Its siblings stay ordinary.
            self.assertFalse(files._self_protected(data / "inbox" / "notes.txt"))


class MissedLive(unittest.TestCase):
    """IN-3 on a real daemon: the broker's own timeout and shutdown paths.
    `Live`'s daemon and wire helpers, without re-running its tests."""

    setUp, call, ids, ask, spy = Live.setUp, Live.call, Live.ids, Live.ask, Live.spy

    def missed_file(self):
        return self.stores.root / "inbox" / "missed.jsonl"

    def test_a_timed_out_ask_leaves_one_record_and_names_it_in_inbox_changed(self):
        req, out, worker = self.ask(thread_id="th-t1", task_id="tk-live1", max_wait_s=0.3,
                                    origin='task tk-live1 "x"', reason=SECRET, headline=SECRET)
        worker.join(5)
        self.assertEqual(out["decision"], Decision.DENY)
        (rec,) = lines_of(self.missed_file())
        self.assertEqual(set(rec), MISSED_FIELDS)
        self.assertEqual({k: rec[k] for k in ("req_id", "tool", "origin", "thread_id", "task_id", "resolution")},
                         {"req_id": req.req_id, "tool": "Bash", "origin": 'task tk-live1 "x"',
                          "thread_id": "th-t1", "task_id": "tk-live1", "resolution": "timeout"})
        self.assertNotIn(SECRET, self.missed_file().read_text())
        self.assertNotIn("make deploy", self.missed_file().read_text())
        self.assertEqual((mode(self.missed_file().parent), mode(self.missed_file())), (0o700, 0o600))
        rid, mid = f"appr:{req.req_id}", f"missed:{req.req_id}"
        # One inbox_changed names the swap, and the projection at that moment
        # already holds the record and no longer the card.
        naming = [(ids, then) for ids, then in self.seen_at_change if mid in ids]
        self.assertEqual(len(naming), 1, self.seen_at_change)
        ids, rows_then = naming[0]
        self.assertEqual(ids, sorted([rid, mid]))
        self.assertIn(mid, rows_then)
        self.assertNotIn(rid, rows_then)
        change = next(c for c in self.changes if mid in c["data"]["ids"])
        self.assertEqual(set(change), {"kind", "data"})
        self.assertEqual(set(change["data"]), {"ids", "reason"})
        self.assertEqual(change["data"]["reason"], "approval_resolved")
        self.assertIn(change["data"]["reason"], inbox.SOURCE_KINDS)
        self.assertNotIn(SECRET, json.dumps(change))
        (row,) = self.call("GET", "/inbox")
        self.assertEqual((row["id"], row["kind"], row["rank"]), (mid, "missed", 8))
        self.assertEqual(row["title"], "Denied while you were away: Bash")
        self.assertEqual(row["blocks"], "task tk-live1 was denied it: nobody answered in time")

    def test_a_private_card_records_only_its_private_fields(self):
        req, out, worker = self.ask(thread_id="th-p1", task_id="tk-priv1", hud_only=True, tool="hud_browser_act",
                                    origin=f'task tk-priv1 "{SECRET}"', browser={"url": "https://" + SECRET},
                                    max_wait_s=0.3)
        worker.join(5)
        (rec,) = lines_of(self.missed_file())
        self.assertEqual(set(rec), MISSED_FIELDS)
        self.assertEqual((rec["origin"], rec["task_id"], rec["thread_id"], rec["tool"]),
                         ("", None, "th-p1", "hud_browser_act"))
        for leak in (SECRET, "tk-priv1", "https://", "make deploy"):
            self.assertNotIn(leak, self.missed_file().read_text())
        (row,) = self.call("GET", "/inbox")
        self.assertEqual((row["origin"], row["task_id"], row["project_id"]), ("", None, None))
        self.assertNotIn(SECRET, json.dumps(row))

    def test_a_shutdown_with_five_asks_leaves_five(self):
        asks = [self.ask(thread_id=f"th-s{i}") for i in range(5)]
        self.daemon.approvals.shutdown()
        for _, out, worker in asks:
            worker.join(5)
            self.assertEqual(out["decision"], Decision.DENY)
        recs = lines_of(self.missed_file())
        self.assertEqual(len(recs), 5)
        self.assertEqual({r["req_id"] for r in recs}, {req.req_id for req, _, _ in asks})
        self.assertEqual({r["resolution"] for r in recs}, {"shutdown"})
        rows = self.call("GET", "/inbox")
        self.assertEqual(sorted(r["id"] for r in rows), sorted(f"missed:{req.req_id}" for req, _, _ in asks))
        self.assertTrue(all(r["blocks"].endswith("Jarvis stopped before you answered") for r in rows))

    def test_an_answered_or_denied_ask_leaves_nothing(self):
        req, out, worker = self.ask(thread_id="th-d1")
        self.call("POST", f"/inbox/appr:{req.req_id}/answer", {"decision": "deny"})
        worker.join(5)
        req2, out2, worker2 = self.ask(thread_id="th-d2")
        self.daemon.approvals.resolve(req2.req_id, Decision.ALLOW)
        worker2.join(5)
        self.assertFalse(self.missed_file().exists())
        self.assertEqual(self.ids(), [])

    def test_answering_one_is_a_400_with_nothing_to_answer(self):
        req, out, worker = self.ask(thread_id="th-a1", max_wait_s=0.3)
        worker.join(5)
        mid = f"missed:{req.req_id}"
        calls = self.spy("resolve_approval")
        for body in ({}, {"decision": "allow"}, {"text": "again"}, {"withdraw": True}):
            with self.subTest(body=body):
                got = self.call("POST", f"/inbox/{mid}/answer", body, 400)
                self.assertIn("takes no answer", json.dumps(got))
        from urllib.parse import quote
        self.call("POST", f"/inbox/{quote(mid, safe='')}/answer", {}, 400)
        self.assertEqual(calls, [])
        self.assertEqual(self.ids(), [mid], "still there: nothing was answered or dismissed")
        self.call("POST", f"/inbox/missed:{req.req_id}", {}, 404)

    def test_a_write_failure_never_breaks_the_resolve(self):
        published = []
        self.daemon.bus.observe(lambda r: published.append(r) if r.get("kind") == "approval_resolved" else None)
        # The folder is a file: every write fails.
        self.stores.root.mkdir(parents=True, exist_ok=True)
        (self.stores.root / "inbox").write_text("not a folder")
        req, out, worker = self.ask(thread_id="th-f1", max_wait_s=0.3)
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(out["decision"], Decision.DENY)
        self.assertEqual([p["data"]["req_id"] for p in published], [req.req_id], "the resolve was still published")
        self.assertEqual(self.daemon.approvals.decisions[-1]["resolution"], "timeout")
        self.assertEqual(self.ids(), [])
        self.assertFalse(any(f"missed:{req.req_id}" in c["data"]["ids"] for c in self.changes),
                         "no row named that was never written")
        (self.stores.root / "inbox").unlink()
        # A write that raises inside the log, and a log that raises outright.
        for target, name in ((inbox.MissedLog, "_append"), (inbox.MissedLog, "record")):
            with self.subTest(fails=name), patch.object(target, name, side_effect=PermissionError("no")):
                req, out, worker = self.ask(thread_id="th-f2", max_wait_s=0.3)
                worker.join(5)
                self.assertEqual(out["decision"], Decision.DENY)
                self.assertIn(req.req_id, [p["data"]["req_id"] for p in published])
        self.assertFalse(self.missed_file().exists())
        # And once the folder is writable again, the next one is recorded.
        req, out, worker = self.ask(thread_id="th-f3", max_wait_s=0.3)
        worker.join(5)
        self.assertEqual([r["req_id"] for r in lines_of(self.missed_file())], [req.req_id])



if __name__ == "__main__":
    unittest.main(verbosity=2)
