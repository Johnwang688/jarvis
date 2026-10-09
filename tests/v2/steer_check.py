"""Free checks: a message sent while a turn runs is never a dead end (2026-10-08).

The owner's report: "I can't steer claude or codex sessions while they are
working ... it just throws an error and I can't continue the session." A send
during a turn used to answer 409 "a turn is already running on this thread";
the HUD then dropped its hold on the running turn (no Stop, no orb interrupt)
until it ended. Now `Daemon.deliver` steers the message into the running turn,
or queues it behind it (up to three, one queue for the HUD and Discord), and
only the owner's Stop drops what waits.

Fake providers and a real daemon on ephemeral loopback ports; no model, no
Discord, nothing of the owner's. The ones written to bite, each verified to
fail against the code before this change:

  - a send during a running chat turn answers 202 `steered`, not 409, and the
    provider receives it in the same turn;
  - what cannot be steered waits, in order, three at most, and runs next;
  - a turn waiting on an approval is never steered — the approval is never
    answered by a message — and the message runs after;
  - the owner's Stop drops what waits and says so (`queue_cleared`);
  - the running session is never dropped by a concurrent send.

Run:  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python tests/v2/steer_check.py
"""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis import config  # noqa: E402
from jarvis.v2 import daemon as mod  # noqa: E402
from jarvis.v2.approvals import ApprovalRequest  # noqa: E402
from jarvis.v2.model import ProviderName, Role  # noqa: E402
from jarvis.v2.provider import (Brief, Decision, Event, EventKind as K, SessionHandle,  # noqa: E402
                                SteerRefused, Usage, UserMessage)
from jarvis.v2.stores import Stores  # noqa: E402


def eventually(predicate, timeout=4.0, what="condition"):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"{what} did not become true")
        time.sleep(0.005)


class Steerable:
    """A provider whose turns block on cue and which steers natively.

    `steer_mode`: "native" (taken into the running turn), "refuse"
    (SteerRefused, queue), "interrupt" (SteerRefused with the interrupt
    fallback), "raise" (an unexpected error). A steered message is answered
    in the turn ("steered: …") unless `consume` is False, in which case the
    turn ends without it and `undelivered` hands it back (the fast path's
    final-answer race)."""

    name = ProviderName.FAST

    def __init__(self):
        self.handles = {}
        self.messages = []          # what `send` was given, in order
        self.steers = []            # what `steer` was given
        self.interrupted = []
        self.closed = []
        self.answers = []
        self.steer_mode = "native"
        self.consume = True

    def health(self):
        return True, "fake"

    def start(self, thread, brief, permit):
        native = {"permit": permit, "brief": brief, "stop": threading.Event(),
                  "entered": threading.Event(), "release": threading.Event(),
                  "inbox": [], "leftover": [], "turn": False, "lock": threading.Lock()}
        handle = SessionHandle(thread.id, self.name, "fake:" + thread.id, native)
        self.handles[thread.id] = handle
        return handle

    def resume(self, thread, brief, permit):
        return self.start(thread, brief, permit)

    def usage(self, handle):
        return Usage()

    def send(self, h, message):
        self.messages.append(message)
        n = h.native
        n["stop"].clear()
        with n["lock"]:
            n["turn"] = True
        n["entered"].set()
        yield Event(K.TURN_STARTED, h.thread_id)
        try:
            if message.text == "approval":
                decision = n["permit"]("Bash", {"command": "rm -rf build"}, n["brief"])
                yield Event(K.TEXT, h.thread_id, {"text": f"the gate said {Decision(decision).value}"})
            if message.text in ("block", "approval"):
                while not n["release"].wait(0.005):
                    if self.consume:
                        with n["lock"]:
                            taken, n["inbox"] = n["inbox"], []
                        for steered in taken:
                            yield Event(K.TEXT, h.thread_id, {"text": f"steered: {steered.text}"})
                    if n["stop"].is_set():
                        yield Event(K.TURN_FINISHED, h.thread_id, {"stop": "interrupted"})
                        return
            yield Event(K.TEXT, h.thread_id, {"text": f"answered: {message.text}"})
            yield Event(K.TURN_FINISHED, h.thread_id, {"stop": "end"})
        finally:
            with n["lock"]:
                n["turn"] = False
                n["leftover"].extend(n["inbox"])
                n["inbox"] = []

    def steer(self, h, message):
        self.steers.append(message)
        if self.steer_mode == "refuse":
            raise SteerRefused("the provider is busy")
        if self.steer_mode == "interrupt":
            raise SteerRefused("no way to steer", fallback="interrupt")
        if self.steer_mode == "raise":
            raise RuntimeError("steer exploded")
        n = h.native
        with n["lock"]:
            if not n["turn"]:
                raise SteerRefused("no turn is running")
            n["inbox"].append(message)

    def undelivered(self, h):
        n = h.native
        with n["lock"]:
            out, n["leftover"] = n["leftover"], []
        return out

    def interrupt(self, h):
        self.interrupted.append(h.thread_id)
        h.native["stop"].set()

    def answer(self, h, req_id, decision):
        self.answers.append((req_id, decision))

    def close(self, h):
        self.closed.append(h.thread_id)
        h.native["stop"].set()


class Unsteerable(Steerable):
    """A provider with no `steer` at all (as the daemon's older fakes are)."""
    steer = None
    undelivered = None


class Harness(unittest.TestCase):
    provider_cls = Steerable

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "project").mkdir()
        for name, value in (("V2_DATA_DIR", self.root / "v2"),
                            ("MODELS_PATH", self.root / "models.json"),
                            ("PROVIDER_DEFAULTS_PATH", self.root / "provider_defaults.json"),
                            ("ALLOWLIST_PATH", self.root / "allowlist.json")):
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.stores = Stores()
        self.fake = self.provider_cls()

        def factory(thread_id, brief):
            # A permit that asks the owner through the daemon's real broker.
            def permit(tool, args, supplied):
                return self.d.approvals.ask(ApprovalRequest(
                    tool=tool, args=dict(args), command=args.get("command"), thread_id=thread_id))
            return permit

        self.d = mod.Daemon(self.stores, {ProviderName.FAST: self.fake}, factory, 0)
        self.d.start()
        self.addCleanup(self.d.stop)
        self.project = self.stores.projects.create("test", str(self.root / "project"))
        self.events = self.d.bus.subscribe()
        self.addCleanup(lambda: self.d.bus.unsubscribe(self.events))
        self.seen = []

    # -- helpers --------------------------------------------------------------

    def chat(self, role=Role.CHAT, task_id=None):
        return self.d.open_thread(self.project.id, role, ProviderName.FAST,
                                  Brief(role, str(self.root / "project"), task_id=task_id))

    def post(self, path, body, status=202):
        conn = http.client.HTTPConnection("127.0.0.1", self.d.port, timeout=4)
        try:
            conn.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
            response = conn.getresponse()
            value = json.loads(response.read() or b"null")
            self.assertEqual(response.status, status, value)
            return value
        finally:
            conn.close()

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.d.port, timeout=4)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            return json.loads(response.read())
        finally:
            conn.close()

    def send(self, thread, text, status=202):
        return self.post(f"/threads/{thread.id}/send", {"text": text}, status)

    def running(self, thread):
        eventually(lambda: self.fake.handles[thread.id].native["entered"].is_set(), what="the turn")

    def release(self, thread):
        self.fake.handles[thread.id].native["release"].set()

    def idle(self, thread):
        def done():
            with self.d._lock:
                session = self.d._sessions.get(thread.id)
                return ((session is None or session.worker is None)
                        and not self.d._queues.get(thread.id))
        eventually(done, what="the thread to go idle")

    def bus(self, kind=None):
        while True:
            try:
                self.seen.append(self.events.get_nowait())
            except Exception:
                break
        return [e for e in self.seen if kind is None or e.get("kind") == kind]

    def log(self, thread, kind=None):
        return [r for r in self.stores.threads.read_log(thread.id) if kind is None or r["kind"] == kind]


class SteerChecks(Harness):
    def test_a_send_during_a_turn_is_steered_into_it_not_refused(self):
        thread = self.chat()
        first = self.send(thread, "block")
        self.assertEqual(first["status"], "started")
        self.running(thread)
        session = self.d._sessions[thread.id]
        worker = session.worker
        steered = self.send(thread, "do it differently")
        self.assertEqual((steered["status"], steered["mode"], steered["turn_id"]),
                         ("steered", "native", first["turn_id"]))
        self.assertEqual([m.text for m in self.fake.steers], ["do it differently"])
        eventually(lambda: any(e.get("kind") == "text" and e["data"]["text"] == "steered: do it differently"
                               for e in self.bus()), what="the steer answered in the running turn")
        # The very same session and turn: nothing dropped, nothing restarted.
        self.assertIs(self.d._sessions[thread.id], session)
        self.assertIs(session.worker, worker)
        self.assertFalse(session.lost)
        self.release(thread)
        self.idle(thread)
        self.assertEqual([m.text for m in self.fake.messages], ["block"], "one turn, not two")
        self.assertEqual(self.fake.closed, [])
        finished = self.log(thread, "turn_finished")
        self.assertEqual([(r["turn_id"], r["data"]["stop"]) for r in finished],
                         [(first["turn_id"], "end")])
        users = self.log(thread, "user")
        self.assertEqual(users[1]["turn_id"], first["turn_id"])
        self.assertTrue(users[1]["data"]["steer"])
        self.assertEqual(users[1]["data"]["message_id"], steered["message_id"])
        published = [e for e in self.bus("user_message") if e["data"].get("steer")]
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]["data"]["typed"], "do it differently")
        marks = [m.get("mark") for m in self.get(f"/threads/{thread.id}/transcript")["messages"]
                 if m["role"] == "user"]
        self.assertEqual(marks, [None, "steering"])

    def test_a_turn_waiting_on_an_approval_is_never_steered_and_the_card_stays(self):
        thread = self.chat()
        self.send(thread, "approval")
        pending = eventually(lambda: self.d.approvals.pending(), what="the approval card")
        reply = self.send(thread, "no, use make clean")
        self.assertEqual(reply["status"], "queued")
        self.assertEqual(self.fake.steers, [], "never handed to a provider blocked on the owner")
        time.sleep(0.1)
        still = self.d.approvals.pending()
        self.assertEqual([r.req_id for r in still], [pending[0].req_id],
                         "the steer neither answered nor withdrew the card")
        self.assertEqual(self.fake.answers, [])
        self.d.resolve_approval(pending[0].req_id, Decision.DENY)
        self.release(thread)
        self.idle(thread)
        self.assertEqual([m.text for m in self.fake.messages], ["approval", "no, use make clean"])
        texts = [r["data"]["text"] for r in self.log(thread, "text")]
        self.assertIn("the gate said deny", texts, "the owner's own answer decided it")

    def test_a_provider_question_queues_rather_than_steers(self):
        thread = self.chat()
        self.send(thread, "block")
        self.running(thread)
        session = self.d._sessions[thread.id]
        self.d._record(session, Event(K.QUESTION, thread.id, {"req_id": "q-1", "text": "npm or pnpm?"}))
        self.assertEqual(self.send(thread, "files only, no answer")["status"], "queued")
        self.assertEqual(self.fake.steers, [])
        self.d.answer(thread.id, "q-1", "pnpm")
        self.assertEqual(self.send(thread, "now steer")["status"], "queued",
                         "behind the waiting one: nothing overtakes it")
        self.release(thread)
        self.idle(thread)
        self.assertEqual([m.text for m in self.fake.messages],
                         ["block", "files only, no answer", "now steer"])

    def test_a_refused_or_failed_steer_queues_and_runs_next(self):
        for mode in ("refuse", "raise"):
            with self.subTest(mode=mode):
                self.fake.steer_mode = mode
                thread = self.chat()
                self.send(thread, "block")
                self.running(thread)
                self.assertEqual(self.send(thread, f"after {mode}")["status"], "queued")
                self.release(thread)
                self.idle(thread)
                self.assertEqual(self.fake.messages[-1].text, f"after {mode}")

    def test_a_provider_that_cannot_steer_is_interrupted_for_the_message(self):
        """SteerRefused(fallback="interrupt") — a Codex without turn/steer: the
        turn stops and the message runs next, the model told why. Not the
        owner's Stop: what else waits is kept."""
        self.fake.steer_mode = "interrupt"
        thread = self.chat()
        first = self.send(thread, "block")
        self.running(thread)
        reply = self.send(thread, "redirect")
        self.assertEqual((reply["status"], reply["mode"]), ("steered", "interrupt"))
        self.assertIn(thread.id, self.fake.interrupted)
        self.idle(thread)
        stops = [(r["turn_id"], r["data"]["stop"]) for r in self.log(thread, "turn_finished")]
        self.assertEqual(stops[0], (first["turn_id"], "interrupted"))
        ran = self.fake.messages[-1]
        self.assertTrue(ran.text.startswith(mod.INTERRUPTED_NOTE), ran.text)
        self.assertTrue(ran.text.endswith("redirect"))
        user = [r for r in self.log(thread, "user") if r["data"].get("message_id") == reply["message_id"]]
        self.assertEqual(user[0]["data"]["text"], "redirect", "the owner's words, never the note")
        self.assertEqual(self.bus("queue_cleared"), [])

    def test_the_owner_stop_drops_what_waits_and_hands_it_back(self):
        self.fake.steer_mode = "refuse"
        thread = self.chat()
        self.send(thread, "block")
        self.running(thread)
        one = self.send(thread, "one")
        two = self.send(thread, "two")
        self.assertEqual((one["position"], two["position"]), (1, 2))
        self.assertEqual(self.get("/threads")[0]["queued"], 2)
        self.post(f"/threads/{thread.id}/interrupt", {}, 200)
        self.idle(thread)
        cleared = self.bus("queue_cleared")
        self.assertEqual(len(cleared), 1)
        self.assertEqual([(m["message_id"], m["typed"]) for m in cleared[0]["data"]["messages"]],
                         [(one["message_id"], "one"), (two["message_id"], "two")])
        self.assertEqual(cleared[0]["data"]["reason"], "stopped")
        self.assertEqual([m.text for m in self.fake.messages], ["block"], "stop means stop")
        self.assertEqual([r["data"]["message_id"] for r in self.log(thread, "queued_dropped")],
                         [one["message_id"], two["message_id"]])
        marks = [m.get("mark") for m in self.get(f"/threads/{thread.id}/transcript")["messages"]
                 if m["role"] == "user"]
        self.assertEqual(marks, [None, "not sent", "not sent"])
        record = self.get("/threads")[0]
        self.assertEqual((record["running"], record["queued"]), (False, 0))
        # And the thread goes on: the next message is an ordinary turn.
        self.assertEqual(self.send(thread, "fresh start")["status"], "started")
        self.idle(thread)

    def test_an_undelivered_steer_runs_next_unless_the_owner_stopped(self):
        """The fast path's race: a steer taken while the final answer was
        being written never reaches the model; it runs as the next turn."""
        self.fake.consume = False
        thread = self.chat()
        self.send(thread, "block")
        self.running(thread)
        late = self.send(thread, "and in French")
        self.assertEqual(late["status"], "steered")
        self.release(thread)
        self.idle(thread)
        self.assertEqual([m.text for m in self.fake.messages], ["block", "and in French"])
        started = self.log(thread, "queued_started")
        self.assertEqual([r["data"]["message_id"] for r in started], [late["message_id"]])

        self.fake.handles[thread.id].native["release"].clear()
        self.send(thread, "block")
        eventually(lambda: len(self.fake.messages) == 3, what="the third turn")
        lost = self.send(thread, "never mind")
        self.assertEqual(lost["status"], "steered")
        self.post(f"/threads/{thread.id}/interrupt", {}, 200)
        self.idle(thread)
        self.assertEqual(len(self.fake.messages), 3, "a stop drops it; it never runs")
        dropped = self.bus("queue_cleared")
        self.assertEqual([m["message_id"] for d in dropped for m in d["data"]["messages"]],
                         [lost["message_id"]])

    def test_the_running_session_is_never_dropped_by_a_concurrent_send(self):
        thread = self.chat()
        self.send(thread, "block")
        self.running(thread)
        session = self.d._sessions[thread.id]
        for text in ("a", "b", "c"):
            self.send(thread, text)
        self.assertIs(self.d._sessions[thread.id], session)
        self.assertFalse(session.lost)
        self.assertEqual(self.fake.closed, [])
        record = self.get("/threads")[0]
        self.assertTrue(record["running"])
        self.release(thread)
        self.idle(thread)
        self.assertIs(self.d._sessions.get(thread.id), session)

    def test_a_non_fatal_refusal_from_a_provider_keeps_the_session(self):
        """The providers' own "a turn is already running" refusal is not fatal
        any more; a fatal one made the daemon drop and close the session."""
        thread = self.chat()
        session = self.d._sessions[thread.id]

        def refused(h, message):
            yield Event(K.ERROR, h.thread_id,
                        {"message": "a turn is already running on this thread", "fatal": False})

        self.fake.send = refused
        self.d.send(thread.id, UserMessage("x"))
        self.idle(thread)
        self.assertIs(self.d._sessions.get(thread.id), session)
        self.assertFalse(session.lost)
        self.assertEqual(self.fake.closed, [])

    def test_task_threads_and_the_runner_path_still_refuse(self):
        """Task turns are the runner's: a running task thread still answers
        409, and `send` (the runner's and the escape hatch's) still refuses
        while a turn runs — they retry on exactly that."""
        task = self.stores.tasks.create(self.project.id, "a task")
        self.stores.tasks.save(task)
        worker = self.chat(Role.IMPLEMENTER, task_id=task.id)
        self.d.send(worker.id, UserMessage("block"))
        self.running(worker)
        with self.assertRaisesRegex(mod.DaemonError, "already running"):
            self.d.deliver(worker.id, UserMessage("steer a task?"))
        self.send(worker, "over http", status=409)
        self.release(worker)
        self.idle(worker)
        chat = self.chat()
        self.d.send(chat.id, UserMessage("block"))
        self.running(chat)
        with self.assertRaisesRegex(mod.DaemonError, "already running"):
            self.d.send(chat.id, UserMessage("runner-style"))
        self.release(chat)
        self.idle(chat)


class QueueChecks(Harness):
    provider_cls = Unsteerable

    def test_without_steer_messages_wait_three_at_most_and_run_in_order(self):
        thread = self.chat()
        first = self.send(thread, "block")
        self.running(thread)
        replies = [self.send(thread, text) for text in ("one", "two", "three")]
        self.assertEqual([(r["status"], r["position"], r["turn_id"]) for r in replies],
                         [("queued", n, first["turn_id"]) for n in (1, 2, 3)])
        full = self.send(thread, "four", status=409)
        self.assertEqual(full["error"], mod.QUEUE_FULL)
        self.release(thread)
        self.idle(thread)
        self.assertEqual([m.text for m in self.fake.messages], ["block", "one", "two", "three"])
        # Each waited message's record and its turn share the minted id.
        users = {r["data"]["message_id"]: r["turn_id"] for r in self.log(thread, "user")
                 if r["data"].get("queued")}
        for row in self.log(thread, "queued_started"):
            self.assertEqual(users[row["data"]["message_id"]], row["turn_id"])
        turns = [r["turn_id"] for r in self.log(thread, "turn_finished")]
        self.assertEqual(turns[1:], [r["queued_turn_id"] for r in replies])
        marks = [m.get("mark") for m in self.get(f"/threads/{thread.id}/transcript")["messages"]
                 if m["role"] == "user"]
        self.assertEqual(marks, [None, None, None, None], "they ran: none still reads queued")


if __name__ == "__main__":
    unittest.main(verbosity=2)
