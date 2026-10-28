"""Writing from the phone (phone surface design §5, §11 questions 1 and 2;
work package P-5): `jarvis/v2/remote_write.py`, the phone's half of
`Daemon.deliver` (`NEVER_STEER`), `daemon.task_verb` and
`Daemon.create_task`, the task-cancel half of `remote_auth.answer_options`
and the proposal withdraw in `remote_answer`.

Free and hermetic, as tests/v2/remote_answer_check.py (whose `Phone` base it
reuses): real daemons on ephemeral loopback ports (never 8402, 8403, 8405 or
8408), temporary HOME and data, fake providers, a spy task runner, real
WebAuthn assertions from a software authenticator.

What it pins (the package's acceptance items):

  - a phone message is recorded `via="phone"`, `desk` false, never at the
    desk (the session's desk slot stays absent), and marked "via the phone"
    in the chat reads and "You (phone)" on the Discord mirror;
  - it queues behind a running turn and never steers it, on a provider that
    can steer (a HUD message on the same turn does steer);
  - the session's rate (30 a minute), the text cap, attachments refused,
    any other field refused;
  - a new chat on each provider (fast, Claude, Codex) and its first message;
  - start and resume reach `TaskControl` with exactly the HUD's arguments;
    a task is created in INTAKE (the intake gate) and starts only on Start;
    `TASK_START` off takes those two routes out;
  - cancel takes a fresh passkey assertion bound to the task and the verb;
    an approval's assertion is never a cancel's, nor the reverse;
  - a proposal's withdraw from the phone, the session only;
  - every verb is in the task's journal as the phone's;
  - permanent delete, archive, merge and the rest are unreachable;
  - unverified session 401; the API and HUD listeners 404; 400 / 404 / 409
    for malformed, unknown, wrong state.

Run: PYTHONPATH=. .venv/bin/python tests/v2/remote_write_check.py
"""
from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rnc = _load("remote_answer_check")
rac, ic = rnc.rac, rnc.ic

from jarvis.v2 import chat_reads, daemon as daemon_mod, desk, remote_answer, remote_auth, remote_write  # noqa: E402
from jarvis.v2.control import ControlError  # noqa: E402
from jarvis.v2.discord import mirror  # noqa: E402
from jarvis.v2.model import ProviderName as PN, Role, TaskState, to_json  # noqa: E402
from jarvis.v2.provider import Event, EventKind as K, UserMessage  # noqa: E402

HOST = rac.HOST
eventually = rnc.eventually


class Gate(ic.Brain):
    """A brain whose turn runs until `release` is set, and that can steer:
    every message it is handed (turn or steer) is recorded with its flags."""

    def __init__(self, name):
        super().__init__(name)
        self.release = threading.Event()
        self.turns: list[UserMessage] = []
        self.steered: list[UserMessage] = []
        self.running = threading.Event()

    def send(self, handle, message):
        tid = handle.thread_id
        self.turns.append(message)
        yield Event(K.TURN_STARTED, tid)
        self.running.set()
        self.release.wait(10)
        yield Event(K.TEXT, tid, {"text": "ok"})
        yield Event(K.TURN_FINISHED, tid, {"stop": "end"})

    def steer(self, handle, message):
        self.steered.append(message)
        return True


class Runner(ic.SpyRunner):
    """The TaskControl the routes reach, recording every call with its
    arguments, refusing as the runner does (`runner.TaskRunner`)."""

    def start(self, task_id):
        self.calls.append(("start", task_id))
        task = self.daemon.stores.tasks.get(task_id)
        if task.state != TaskState.INTAKE:
            raise ControlError(f"Task {task_id} is already {task.state.value}; it cannot be started again.")
        return self.daemon.stores.tasks.transition(task_id, TaskState.CLARIFYING, reason="intake accepted")

    def resume(self, task_id, *, provider=None):
        self.calls.append(("resume", task_id, provider))
        task = self.daemon.stores.tasks.get(task_id)
        if task.state != TaskState.BLOCKED:
            raise ControlError(f"Task {task_id} is {task.state.value}; only a blocked task resumes.")
        return self.daemon.stores.tasks.transition(task_id, TaskState.RUNNING, reason="owner resumed")


class Base(rnc.Phone):
    def setUp(self):
        super().setUp()
        self.gate = Gate(PN.CLAUDE)
        self.d.providers[PN.CLAUDE] = self.gate
        self.addCleanup(self.gate.release.set)
        self.runner = Runner(self.d)
        self.d.runner = self.runner
        clock = rac.Clock(1000.0)
        guard = patch.object(remote_write, "clock", clock)
        guard.start()
        self.addCleanup(guard.stop)
        self.rate_clock = clock

    def post(self, path, value=None, cookie=None, **kw):
        status, _, body = self.phone("POST", "/m/api/" + path, value if value is not None else {},
                                     cookie=self.cookie if cookie is None else cookie, **kw)
        return status, body

    def api(self, method, path, value=None):
        """The HUD's own task routes, on the API listener (as the HUD's page calls them)."""
        host = f"127.0.0.1:{self.d.port}"
        body = json.dumps(value).encode() if value is not None else None
        headers = [("Host", host), ("Origin", f"http://{host}"), ("Content-Type", "application/json")]
        status, _, data = rac.raw(self.d.port, method, path, headers, body)
        return status, (json.loads(data) if data else None)

    def chat(self, provider=PN.FAST):
        return self.d.open_thread(self.project.id, Role.CHAT, provider, {})

    def users(self, thread_id):
        return [r for r in self.d.stores.threads.read_log(thread_id) if r.get("kind") == "user"]

    def task(self, state=None):
        task = self.d.stores.tasks.create(self.project.id, "add multiply")
        self.d.stores.tasks.save(task)
        if state is not None:
            for step in {TaskState.BLOCKED: (TaskState.CLARIFYING, TaskState.BLOCKED),
                         TaskState.CLARIFYING: (TaskState.CLARIFYING,),
                         TaskState.DONE: (TaskState.CLARIFYING, TaskState.PLANNED, TaskState.RUNNING,
                                          TaskState.VERIFYING, TaskState.DONE)}[state]:
                task = self.d.stores.tasks.transition(task.id, step)
        return task

    def journal(self, task_id):
        return [(r.get("verb"), r.get("via")) for r in self.d.stores.tasks.read_journal(task_id)
                if r.get("event") == "owner_verb"]

    def idle(self, thread_id):
        with self.d._lock:
            session = self.d._sessions.get(thread_id)
            return session is None or session.worker is None


# -- sending ----------------------------------------------------------------------------


class Sending(Base):
    def test_a_phone_message_is_via_phone_and_never_at_the_desk(self):
        thread = self.chat(PN.CLAUDE)
        status, body = self.post(f"threads/{thread.id}/messages", {"text": "  add a test  "})
        self.assertEqual((status, body["status"]), (202, "started"), body)
        self.assertEqual(set(body) - set(remote_write.SEND_FIELDS), set(), body)
        eventually(self.gate.running.is_set, what="the turn")
        (message,) = self.gate.turns
        self.assertEqual((message.origin, message.via, message.desk, message.text, message.images),
                         ("owner", "phone", False, "add a test", []))
        with self.d._lock:
            session = self.d._sessions[thread.id]
            self.assertIs(session.desk.slot.get("present"), False, "a phone turn is never at the desk")
            self.assertFalse(desk.desk_message(session.brief, message))
            # Even a phone message claiming the desk is not one (`via` must be "hud").
            import dataclasses
            self.assertFalse(desk.desk_message(session.brief, dataclasses.replace(message, desk=True)))
        (record,) = self.users(thread.id)
        self.assertEqual((record["data"]["via"], record["data"]["typed"], record["data"]["origin"]),
                         ("phone", "add a test", "owner"))
        self.gate.release.set()
        eventually(lambda: self.idle(thread.id), what="the turn's end")
        # The chat reads mark it, and the Discord mirror names the phone.
        rows = [r for r in chat_reads.projected(self.d.stores.threads.read_log(thread.id), thread.provider)
                if r[1].role == "owner"]
        self.assertEqual(rows[0][1].marks, ("via the phone",))
        self.assertEqual(mirror.user_line(record["data"]), "You (phone): add a test")
        print("ok  a phone message: via phone, origin owner, desk false (slot absent), marked in reads and mirror")

    def test_it_queues_behind_a_running_turn_and_never_steers(self):
        thread = self.chat(PN.CLAUDE)
        self.assertEqual(self.post(f"threads/{thread.id}/messages", {"text": "first"})[0], 202)
        eventually(self.gate.running.is_set, what="the turn")
        status, body = self.post(f"threads/{thread.id}/messages", {"text": "second"})
        self.assertEqual((status, body["status"], body["position"]), (202, "queued", 1), body)
        self.assertEqual(self.gate.steered, [], "a phone message never steers")
        # The control: the same turn takes a HUD message as a steer, so the
        # provider can steer and the phone's queueing is the phone's rule.
        # (A message already waits, so a HUD one would queue too: drain first.)
        self.assertEqual(self.users(thread.id)[-1]["data"].get("queued"), True)
        self.gate.release.set()
        eventually(lambda: len(self.gate.turns) == 2, what="the queued turn")
        self.assertEqual(self.gate.turns[1].text, "second")
        self.assertEqual(self.gate.turns[1].via, "phone")
        self.gate.release.clear()
        self.gate.running.clear()
        eventually(lambda: self.idle(thread.id), what="the queued turn's end")
        self.d.deliver(thread.id, UserMessage("third"))
        eventually(self.gate.running.is_set, what="a third turn")
        reply = self.d.deliver(thread.id, UserMessage("from the HUD", via="hud", desk=True))
        self.assertEqual(reply["status"], "steered", reply)
        self.assertEqual([m.text for m in self.gate.steered], ["from the HUD"])
        status, body = self.post(f"threads/{thread.id}/messages", {"text": "from the phone"})
        self.assertEqual((status, body["status"]), (202, "queued"), body)
        self.assertEqual([m.text for m in self.gate.steered], ["from the HUD"], "still never the phone's")
        self.gate.release.set()
        print("ok  a phone message queues behind a running turn; a HUD one on the same turn steers")

    def test_never_steer_is_read_from_the_message(self):
        # Any caller of `deliver`, not only the phone's route.
        self.assertEqual(daemon_mod.NEVER_STEER, ("phone",))
        thread = self.chat(PN.CLAUDE)
        self.d.deliver(thread.id, UserMessage("go"))
        eventually(self.gate.running.is_set, what="the turn")
        reply = self.d.deliver(thread.id, UserMessage("hi", origin="owner", via="phone"))
        self.assertEqual(reply["status"], "queued")
        self.assertEqual(self.gate.steered, [])
        self.gate.release.set()
        print("ok  NEVER_STEER: a via='phone' message queues whoever delivers it")

    def test_a_full_queue_is_409_with_the_huds_words(self):
        thread = self.chat(PN.CLAUDE)
        self.post(f"threads/{thread.id}/messages", {"text": "go"})
        eventually(self.gate.running.is_set, what="the turn")
        for i in range(daemon_mod.QUEUE_MAX):
            self.assertEqual(self.post(f"threads/{thread.id}/messages", {"text": f"m{i}"})[0], 202)
        status, body = self.post(f"threads/{thread.id}/messages", {"text": "one too many"})
        self.assertEqual((status, body["error"]), (409, daemon_mod.QUEUE_FULL))
        self.gate.release.set()
        print("ok  a full queue (three waiting) is 409, the HUD's words")

    def test_caps_attachments_and_fields(self):
        thread = self.chat()
        path = f"threads/{thread.id}/messages"
        for body, word in (({"text": "x", "images": [{"b64": "AA==", "mime": "image/png"}]}, remote_write.NO_ATTACHMENTS),
                           ({"text": "x", "attachments": []}, remote_write.NO_ATTACHMENTS),
                           ({"text": "x", "spoken": True}, "bad request"),
                           ({"text": "x", "desk": True}, "bad request"),
                           ({"text": "x", "via": "hud"}, "bad request"),
                           ({}, "bad request"),
                           ({"text": "   "}, "text must be a nonempty string"),
                           ({"text": 7}, "text must be a nonempty string"),
                           ({"text": "x" * (remote_write.MESSAGE_CHARS + 1)},
                            f"text is at most {remote_write.MESSAGE_CHARS} characters from the phone")):
            with self.subTest(body=str(body)[:60]):
                status, got = self.post(path, body)
                self.assertEqual((status, got), (400, {"error": word}))
        status, _, got = self.phone("POST", "/m/api/" + path, cookie=self.cookie, body=b"[1]",
                                    extra=[("Content-Type", "application/json")])
        self.assertEqual(status, 400, got)
        self.assertEqual(self.users(thread.id), [], "nothing was sent")
        self.assertEqual(remote_write.MESSAGE_CHARS, 20000)
        status, got = self.post(path, {"text": "y" * remote_write.MESSAGE_CHARS})
        self.assertEqual(status, 202, got)
        print("ok  caps: 20,000 characters, attachments and images refused, any other field 400")

    def test_the_rate_is_per_session(self):
        self.assertEqual((remote_write.RATE, remote_write.RATE_S), (30, 60.0))
        thread = self.chat()
        path = f"threads/{thread.id}/messages"
        for _ in range(remote_write.RATE):
            self.assertEqual(self.post(path, {"text": ""})[0], 400)
        status, body = self.post(path, {"text": "go"})
        self.assertEqual((status, body), (429, {"error": remote_write.RATE_WORD}))
        self.assertEqual(self.users(thread.id), [])
        # Another session is its own.
        other = self.signed_in(self.pair())
        self.assertEqual(self.post(path, {"text": ""}, cookie=other)[0], 400)
        # A new chat and a task verb count too.
        self.assertEqual(self.post("threads", {"project_id": self.project.id, "provider": "fast", "text": "x"})[0], 429)
        self.assertEqual(self.post("tasks", {"project_id": self.project.id, "brief": "b"})[0], 429)
        self.rate_clock.now += remote_write.RATE_S
        self.assertEqual(self.post(path, {"text": "go"})[0], 202)
        print("ok  the rate: 30 writes a minute per session, 429 past it, another session unaffected")

    def test_unknown_archived_or_task_threads(self):
        self.assertEqual(self.post("threads/0123abcd/messages", {"text": "x"}), (404, {"error": "not found"}))
        self.assertEqual(self.post("threads/NOTANID/messages", {"text": "x"})[0], 404)
        thread = self.chat()
        stored = self.d.stores.threads.get(thread.id)
        task = self.task()
        stored.task_id = task.id
        self.d.stores.threads.save(stored)
        self.assertEqual(self.post(f"threads/{thread.id}/messages", {"text": "x"}), (409, {"error": remote_write.NOT_A_CHAT}))
        other = self.chat()
        stored = self.d.stores.threads.get(other.id)
        stored.archived = "2026-10-28T00:00:00+00:00"
        self.d.stores.threads.save(stored)
        self.assertEqual(self.post(f"threads/{other.id}/messages", {"text": "x"})[0], 404)
        status, _, body = self.phone("GET", f"/m/api/threads/{other.id}/messages", cookie=self.cookie)
        self.assertEqual(status, 405, body)
        print("ok  an unknown or archived thread is 404, a task's thread 409, GET 405")


# -- new chats --------------------------------------------------------------------------


class NewChat(Base):
    def test_a_new_chat_on_each_provider(self):
        for provider in ("fast", "claude", "codex"):
            with self.subTest(provider=provider):
                self.gate.release.set()
                status, body = self.post("threads", {"project_id": self.project.id, "provider": provider,
                                                     "text": f"hello {provider}"})
                self.assertEqual(status, 201, body)
                tid = body["thread"]["id"]
                stored = self.d.stores.threads.get(tid)
                self.assertEqual((stored.provider.value, stored.role, stored.task_id, stored.project_id),
                                 (provider, Role.CHAT, None, self.project.id))
                self.assertEqual(body["send"]["status"], "started")
                self.assertNotIn("cwd", body["thread"], "no path leaves")
                (record,) = self.users(tid)
                self.assertEqual((record["data"]["via"], record["data"]["typed"]), ("phone", f"hello {provider}"))
                eventually(lambda: self.idle(tid), what="its first turn")
        print("ok  a new chat on fast, Claude and Codex, its first message via the phone")

    def test_the_new_chat_sheet_and_refusals(self):
        status, _, body = self.phone("GET", "/m/api/thread-models", cookie=self.cookie)
        self.assertEqual(status, 200, body)
        self.assertEqual(set(body["providers"]), {"fast", "claude", "codex"})
        for entry in body["providers"].values():
            self.assertEqual(set(entry), {"label", "available", "default", "default_effort", "models"})
            self.assertTrue(entry["available"])
        good = {"project_id": self.project.id, "provider": "fast", "text": "x"}
        for change, want in (({"provider": "gpt"}, 400), ({"provider": None}, 400), ({"model": 7}, 400),
                             ({"text": ""}, 400), ({"brief": {"profile": "auto"}}, 400),
                             ({"images": []}, 400), ({"project_id": "0123abcd"}, 404),
                             ({"model": "not-a-model"}, 400)):
            with self.subTest(change=change):
                self.assertEqual(self.post("threads", {**good, **change})[0], want)
        archived = self.d.stores.projects.create("Old", str(self.root / "old"))
        archived.archived = "2026-10-28T00:00:00+00:00"
        self.d.stores.projects.save(archived)
        self.assertEqual(self.post("threads", {**good, "project_id": archived.id})[0], 404)
        self.assertEqual([t for t in self.d.stores.threads.list()], [], "no thread was left behind")
        del self.d.providers[PN.CODEX]
        status, body = self.post("threads", {**good, "provider": "codex"})
        self.assertEqual(status, 409, body)
        status, _, body = self.phone("GET", "/m/api/thread-models", cookie=self.cookie)
        self.assertFalse(body["providers"]["codex"]["available"])
        print("ok  the new-chat sheet (labels, defaults, models) and its refusals (400, 404, 409)")


# -- tasks --------------------------------------------------------------------------------


class Tasks(Base):
    def test_start_and_resume_reach_task_control_as_the_huds_routes_do(self):
        for verb, state in (("start", None), ("resume", TaskState.BLOCKED)):
            with self.subTest(verb=verb):
                hud_task, phone_task = self.task(state), self.task(state)
                self.runner.calls.clear()
                status, _ = self.api("POST", f"/tasks/{hud_task.id}/{verb}", {})
                self.assertEqual(status, 200)
                status, body = self.post(f"tasks/{phone_task.id}/{verb}", {})
                self.assertEqual(status, 200, body)
                self.assertEqual(body["task"]["id"], phone_task.id)
                self.assertNotIn("worktree", body["task"])
                hud_call, phone_call = self.runner.calls
                self.assertEqual(hud_call[0], phone_call[0])
                self.assertEqual(hud_call[2:], phone_call[2:], "the same arguments")
                self.assertEqual(self.journal(phone_task.id), [(verb, "phone")])
                self.assertEqual(self.journal(hud_task.id), [], "the desk's own verb is not the phone's")
                # Wrong state: 409, the runner's words.
                status, body = self.post(f"tasks/{phone_task.id}/{verb}", {})
                self.assertEqual(status, 409, body)
                # The HUD's resume body may switch provider; the phone's never.
                self.assertEqual(self.post(f"tasks/{phone_task.id}/{verb}", {"provider": "claude"})[0], 400)
        print("ok  start and resume: daemon.task_verb, the HUD's call and arguments; journaled as the phone's")

    def test_a_task_is_made_in_intake_and_starts_only_on_start(self):
        status, body = self.post("tasks", {"project_id": self.project.id, "brief": "add divide"})
        self.assertEqual(status, 201, body)
        tid = body["task"]["id"]
        self.assertEqual(body["task"]["state"], "intake")
        self.assertEqual(self.d.stores.tasks.get(tid).state, TaskState.INTAKE)
        self.assertEqual(self.runner.calls, [], "made, never started")
        self.assertEqual(self.journal(tid), [("create", "phone")])
        self.assertEqual(self.post(f"tasks/{tid}/start", {})[0], 200)
        self.assertEqual(self.runner.calls, [("start", tid)])
        self.assertEqual(self.d.stores.tasks.get(tid).state, TaskState.CLARIFYING)
        # The HUD's own POST /tasks is the same function.
        status, made = self.api("POST", "/tasks", {"project_id": self.project.id, "brief": "x"})
        self.assertEqual((status, made["state"]), (201, "intake"))
        for bad, want in (({"project_id": self.project.id}, 400), ({"project_id": self.project.id, "brief": " "}, 400),
                          ({"project_id": self.project.id, "brief": "b" * (remote_write.BRIEF_CHARS + 1)}, 400),
                          ({"project_id": self.project.id, "brief": "b", "state": "running"}, 400),
                          ({"project_id": "0123abcd", "brief": "b"}, 404)):
            self.assertEqual(self.post("tasks", bad)[0], want, bad)
        print("ok  a task from the phone waits in INTAKE (the intake gate) until Start")

    def test_task_start_is_separable(self):
        task = self.task()
        with patch.object(remote_write, "TASK_START", False):
            self.assertEqual(self.post(f"tasks/{task.id}/start", {})[0], 404)
            self.assertEqual(self.post("tasks", {"project_id": self.project.id, "brief": "b"})[0], 405)
            self.assertEqual(self.phone("GET", "/m/api/tasks", cookie=self.cookie)[0], 200, "the read stays")
        self.assertEqual(self.runner.calls, [])
        print("ok  TASK_START off: no task made or started from the phone; the reads stay")

    def cancel_assertion(self, task_id, cookie=None, authenticator=None):
        status, body = self.post(f"tasks/{task_id}/cancel/options", {}, cookie=cookie)
        self.assertEqual(status, 200, body)
        return (authenticator or self.a).get(body["publicKey"])

    def test_cancel_takes_a_fresh_passkey_assertion(self):
        task, other = self.task(TaskState.CLARIFYING), self.task(TaskState.CLARIFYING)
        self.assertEqual(self.post(f"tasks/{task.id}/cancel", {}), (401, {"error": remote_write.CANCEL_NEEDS_PASSKEY}))
        self.assertEqual(self.post(f"tasks/{task.id}/cancel", {"assertion": "x"})[0], 401)
        # Another task's assertion, an unverified one, a used one.
        credential = self.cancel_assertion(other.id)
        self.assertEqual(self.post(f"tasks/{task.id}/cancel", {"assertion": credential})[0], 401)
        uv = self.post(f"tasks/{task.id}/cancel/options", {})[1]["publicKey"]
        self.assertEqual(self.post(f"tasks/{task.id}/cancel", {"assertion": self.a.get(uv, uv=False)})[0], 401)
        credential = self.cancel_assertion(task.id)
        self.assertEqual(self.runner.calls, [], "nothing cancelled yet")
        status, body = self.post(f"tasks/{task.id}/cancel", {"assertion": credential})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.runner.calls, [("cancel", task.id)])
        self.assertEqual(self.d.stores.tasks.get(task.id).state, TaskState.CANCELLED)
        self.assertEqual(self.journal(task.id), [("cancel", "phone")])
        self.assertEqual(self.post(f"tasks/{task.id}/cancel", {"assertion": credential})[0], 409, "spent")
        self.assertEqual(self.post(f"tasks/{task.id}/cancel/options", {}),
                         (409, {"error": "this task is already cancelled"}))
        # The HUD's own cancel is the same call.
        hud = self.task(TaskState.CLARIFYING)
        self.api("POST", f"/tasks/{hud.id}/cancel", {})
        self.assertEqual(self.runner.calls[-1], ("cancel", hud.id))
        print("ok  cancel: its own assertion (task and verb bound, single use); TaskControl.cancel as the HUD's")

    def test_an_approvals_assertion_is_never_a_cancels(self):
        self.secure_ok()
        task = self.task(TaskState.CLARIFYING)
        request, _, _ = self.ask()
        status, body = self.post(f"inbox/{self.row(request)}/answer/options", {"decision": "allow"})
        allow = self.a.get(body["publicKey"])
        self.assertEqual(self.post(f"tasks/{task.id}/cancel", {"assertion": allow})[0], 401)
        cancel = self.cancel_assertion(task.id)
        status, body = self.post(f"inbox/{self.row(request)}/answer", {"decision": "allow", "assertion": cancel})
        self.assertEqual(status, 401, body)
        self.assertTrue(self.pending(request))
        self.assertEqual(self.runner.calls, [])
        # The challenge's subjects and answers never cross.
        session = next(iter(self.auth._sessions.values()))
        for subject, decision in ((f"task:{task.id}", "allow"), (f"task:{task.id}", "always"),
                                  (request.req_id, "cancel"), (f"x:{task.id}", "cancel"), ("task:XYZ", "cancel"),
                                  (f"task:{task.id}", "delete")):
            with self.subTest(subject=subject, decision=decision):
                with self.assertRaises(remote_auth.AuthError) as caught:
                    self.auth.answer_options(session, subject, decision)
                self.assertEqual(caught.exception.status, 400)
        self.assertEqual(remote_auth.TASK_DECISIONS, ("cancel",))
        self.assertEqual(self.post(f"inbox/{self.row(request)}/answer/options", {"decision": "cancel"})[0], 400)
        print("ok  an Allow's assertion is never a cancel's, nor the reverse; subjects and answers never cross")

    def test_a_proposal_is_withdrawn_from_the_phone_with_the_session(self):
        task = self.task()
        self.d.stores.tasks.journal(task.id, "proposed_by_fastpath", brief="b", thread_id=None, turn_id=None,
                                    reply="", not_before=self.d.router.clock() + 60)
        for bad in ({"withdraw": 1}, {"withdraw": False}, {"withdraw": True, "x": 1}, {}):
            self.assertEqual(self.post_answer(f"prop:{task.id}", bad)[0], 400, bad)
        status, _, body = self.post_answer(f"prop:{task.id}", {"withdraw": True})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.runner.calls, [("cancel", task.id)])
        self.assertEqual(self.d.stores.tasks.get(task.id).state, TaskState.CANCELLED)
        self.assertEqual(self.journal(task.id), [("withdraw", "phone")])
        self.assertEqual(self.post_answer(f"prop:{task.id}", {"withdraw": True})[0], 409, "no longer waiting")
        print("ok  a proposal's withdraw from the phone: the session only, the inbox's own call, journaled")

    def test_unknown_or_hidden_tasks(self):
        for path in ("tasks/0123abcd/start", "tasks/0123abcd/resume", "tasks/0123abcd/cancel/options",
                     "tasks/zz/resume"):
            self.assertEqual(self.post(path, {}), (404, {"error": "not found"}), path)
        self.assertEqual(self.post("tasks/0123abcd/cancel", {"assertion": {}})[0], 404)
        self.assertEqual(self.runner.calls, [])
        print("ok  an unknown or malformed task id is 404")


# -- what the phone never reaches --------------------------------------------------------


class Never(Base):
    def test_delete_archive_merge_and_the_rest_are_not_routes(self):
        thread, task = self.chat(), self.task(TaskState.BLOCKED)
        attempts = [("DELETE", f"/m/api/threads/{thread.id}"), ("POST", f"/m/api/threads/{thread.id}/archive"),
                    ("POST", f"/m/api/threads/{thread.id}/delete"), ("PATCH", f"/m/api/threads/{thread.id}"),
                    ("POST", f"/m/api/threads/{thread.id}/bypass"), ("POST", f"/m/api/threads/{thread.id}/interrupt"),
                    ("DELETE", f"/m/api/tasks/{task.id}"), ("POST", f"/m/api/tasks/{task.id}/archive"),
                    ("POST", f"/m/api/tasks/{task.id}/delete"), ("POST", f"/m/api/tasks/{task.id}/merge"),
                    ("POST", f"/m/api/tasks/{task.id}/steer"), ("POST", f"/m/api/tasks/{task.id}/answer"),
                    ("POST", f"/m/api/tasks/{task.id}/worktree"), ("DELETE", f"/m/api/tasks/{task.id}/worktree"),
                    ("POST", f"/m/api/projects/{self.project.id}/archive"),
                    ("DELETE", f"/m/api/projects/{self.project.id}"), ("POST", "/m/api/trash/empty"),
                    ("POST", "/m/api/projects"), ("PATCH", f"/m/api/projects/{self.project.id}"),
                    ("POST", "/m/api/thread-models")]
        for method, path in attempts:
            with self.subTest(method=method, path=path):
                status, _, body = self.phone(method, path, {}, cookie=self.cookie)
                self.assertIn(status, (404, 405), body)
                self.assertIsNone(remote_write._match(path[len("/m/api/"):].split("/"))
                                  if method != "POST" or not path.endswith("thread-models") else None)
        self.assertIsNotNone(self.d.stores.threads.get(thread.id))
        self.assertFalse(self.d.stores.threads.get(thread.id).archived)
        self.assertEqual(self.d.stores.tasks.get(task.id).state, TaskState.BLOCKED)
        self.assertFalse(self.d.stores.projects.get(self.project.id).archived)
        self.assertEqual(self.runner.calls, [])
        print("ok  delete, archive, merge, steer, Bypass, worktree, projects: not phone routes (404/405)")

    def test_without_a_session_401_and_never_on_the_api_or_hud_listener(self):
        thread, task = self.chat(), self.task(TaskState.BLOCKED)
        writes = [f"/m/api/threads/{thread.id}/messages", "/m/api/threads", "/m/api/tasks",
                  f"/m/api/tasks/{task.id}/start", f"/m/api/tasks/{task.id}/resume",
                  f"/m/api/tasks/{task.id}/cancel", f"/m/api/tasks/{task.id}/cancel/options"]
        body = {"text": "x", "project_id": self.project.id, "provider": "fast", "brief": "b"}
        for path in writes + ["/m/api/thread-models"]:
            method = "GET" if path.endswith("thread-models") else "POST"
            with self.subTest(path=path):
                self.assertEqual(self.phone(method, path, body if method == "POST" else None)[0], 401)
                self.assertEqual(self.phone(method, path, body if method == "POST" else None,
                                            cookie=self.cookie + "x")[0], 401)
                if method == "POST":
                    self.assertEqual(self.phone(method, path, body, cookie=self.cookie,
                                                origin="https://evil.ts.net")[0], 403)
                    self.assertEqual(self.phone(method, path, body, cookie=self.cookie, host="evil.ts.net")[0], 400)
                for port in (self.d.port, self.d.face_port):
                    status, got = self.hud(method, path, body, port=port)
                    self.assertEqual(status, 404, (port, got))
        self.assertEqual(self.users(thread.id), [])
        self.assertEqual(self.runner.calls, [])
        print("ok  every write: 401 without a session, 403 cross-origin, 400 bad Host; 404 on the API and HUD")


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main(verbosity=1)
