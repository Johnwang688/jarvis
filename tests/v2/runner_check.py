"""Free WP11 checks: scripted providers, temporary stores, no network, no CLI.

Every provider here is a fake whose reply is keyed on the message it receives
(the `longhorizon_check` pattern), so the whole §10.1 lifecycle runs in-process
in milliseconds. `config.V2_DATA_DIR` and `config.ROUTING_PATH` are pointed at
a temp directory for the life of each test — a suite that reads or writes the
owner's real state is the failure this project has paid for twice.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from jarvis import config, models
from jarvis.v2 import roles, runner as R, worktrees
from jarvis.v2.control import ControlError
from jarvis.v2.daemon import Daemon
from jarvis.v2.ledger import UsageLedger
from jarvis.v2.model import (PermissionProfile, ProviderName as P, Role, TaskState as S,
                             to_json)
from jarvis.v2.provider import (Brief, Decision, Event, EventKind as K, SessionHandle,
                                Usage, UserMessage)
from jarvis.v2.router import Router
from jarvis.v2.stores import Stores


def eventually(predicate, timeout=10, what="condition"):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"{what} did not become true within {timeout}s")
        time.sleep(0.005)


def block(payload: dict) -> str:
    return "Here you go.\n\n```json\n" + json.dumps(payload) + "\n```\n"


SPEC = {"goal": "Add a health endpoint", "deliverable": "GET /healthz returning 200",
        "acceptance": ["/healthz returns 200", "a test covers it"],
        "constraints": ["no new dependencies"], "questions": []}
PLAN = {"steps": ["write the handler", "write the test", "run the test suite"]}
REPORT = {"done": ["added /healthz"], "changed": ["app/health.py"],
          "verified": "reviewer ran the suite; green", "open": [], "next": "merge the branch"}


class FakeProvider:
    """One scripted CLI. `responder(role, text, thread_id)` returns a string (one
    TEXT event) or a list of Events; the TURN_FINISHED is added here."""

    supported_mcp_servers = None

    def __init__(self, name, responder):
        self.name = P(name)
        self.responder = responder
        self.sent: list[tuple[str, Role, str, str]] = []
        self.interrupted: list[str] = []
        self.closed: list[str] = []
        self.started: list[str] = []
        self.briefs: dict[str, Brief] = {}
        self.healthy = True

    def health(self):
        return (True, "fake ready") if self.healthy else (False, "fake is down")

    def start(self, thread, brief, permit):
        self.started.append(thread.id)
        self.briefs[thread.id] = brief
        return SessionHandle(thread.id, self.name, f"{self.name.value}:{thread.id}",
                             {"role": brief.role, "stop": threading.Event()})

    resume = start

    def send(self, handle, message):
        role = handle.native["role"]
        stop = handle.native["stop"]
        stop.clear()
        self.sent.append((handle.thread_id, role, message.text, message.origin))
        out = self.responder(role, message.text, handle.thread_id)
        events = out if isinstance(out, list) else [Event(K.TEXT, "", {"text": str(out)})]
        for event in events:
            if stop.is_set():
                break
            if callable(event):        # hold the turn open; it is handed `stop`
                event(stop)
                continue
            yield Event(event.kind, handle.thread_id, dict(event.data))
        yield Event(K.TURN_FINISHED, handle.thread_id,
                    {"stop": "interrupted" if stop.is_set() else "end"})

    def interrupt(self, handle):
        self.interrupted.append(handle.thread_id)
        handle.native["stop"].set()

    def answer(self, handle, req_id, decision):
        pass

    def usage(self, handle):
        return Usage()

    def close(self, handle):
        self.closed.append(handle.thread_id)

    def texts_to(self, role):
        return [text for _tid, sent_role, text, _origin in self.sent if sent_role == role]


class Fixture(unittest.TestCase):
    max_active = 3

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for target, value in (("V2_DATA_DIR", root / "v2"),
                              ("ROUTING_PATH", root / "routing.json")):
            patcher = patch.object(config, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in (patch.object(models, "tier", return_value="anthropic/claude-test"),
                        patch.object(models, "effort_for", return_value="high"),
                        patch.object(models, "cached_info", return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)

        logging.getLogger("jarvis.v2.runner").setLevel(logging.CRITICAL)
        self.stores = Stores(root / "v2")
        self.project_root = root / "work"
        self.project_root.mkdir()
        self.project = self.stores.projects.create("Demo", str(self.project_root))
        self.claude = FakeProvider(P.CLAUDE, self.reply)
        self.codex = FakeProvider(P.CODEX, self.reply)
        self.providers = {P.CLAUDE: self.claude, P.CODEX: self.codex}
        self.daemon = Daemon(self.stores, self.providers,
                             lambda thread_id, brief: (lambda *a: Decision.ALLOW), 0)
        self.addCleanup(self.daemon.stop)
        self.ledger = UsageLedger(self.stores)
        self.router = Router(self.stores, {n.value: p for n, p in self.providers.items()},
                             self.ledger)
        self.runner = R.TaskRunner(self.daemon, self.router, max_active=self.max_active,
                                   turn_timeout=20)
        self.addCleanup(self.runner.stop, 3)
        self.published: list[dict] = []
        self.subscription = self.daemon.bus.subscribe()
        self.script = {}
        self.writes = 0
        self._wrap_writes()

    def _wrap_writes(self):
        """Count the writes the runner itself makes. `worktrees.ensure` and
        `router.resolve` also write the task; the runner publishes one snapshot
        after each of those calls rather than one per internal save."""
        real_save, real_transition = self.runner._save, self.runner._transition

        def save(task):
            self.writes += 1
            return real_save(task)

        def transition(task_id, state, **kw):
            self.writes += 1
            return real_transition(task_id, state, **kw)

        self.runner._save = save
        self.runner._transition = transition

    # -- scripting ---------------------------------------------------------

    def reply(self, role, text, thread_id):
        """Default script: the happy path. Tests override entries in self.script."""
        for needle, value in self.script.items():
            if needle in text:
                return value(role, text, thread_id) if callable(value) else value
        if role == Role.ORCHESTRATOR:
            if "Write the SPEC" in text:
                return block(SPEC)
            if "Write the PLAN" in text:
                return block(PLAN)
            if "What next?" in text:
                return self.next_instruction(text)
            if "Write the REPORT" in text:
                return block(REPORT)
            return "acknowledged"
        if role == Role.REVIEWER:
            return block({"pass": True, "findings": ["clean"]})
        return f"did it: {text.strip().splitlines()[-1][:40]}"

    def next_instruction(self, text):
        done = sum(1 for _t, role, sent, _o in self.claude.sent + self.codex.sent
                   if role == Role.IMPLEMENTER)
        if done >= len(PLAN["steps"]):
            return block({"next": None, "done": True})
        return block({"next": PLAN["steps"][done], "done": False})

    # -- helpers -----------------------------------------------------------

    def new_task(self, brief="add a health endpoint", **values):
        task = self.stores.tasks.create(self.project.id, brief, **values)
        self.stores.tasks.save(task)
        return task

    def drain(self):
        while True:
            try:
                self.published.append(self.subscription.get_nowait())
            except Exception:
                return self.published

    def snapshots(self):
        return [r for r in self.drain() if r.get("kind") == "task_status_changed"]

    def task(self, task_id):
        return self.stores.tasks.get(task_id)

    def wait_state(self, task_id, *states, timeout=10):
        return eventually(lambda: self.task(task_id) if self.task(task_id).state in states else None,
                          timeout, f"task {task_id} reaching {[s.value for s in states]}")

    def journal(self, task_id, event=None):
        rows = self.stores.tasks.read_journal(task_id)
        return [r for r in rows if event is None or r["event"] == event]

    def states(self, task_id):
        return [r["new_state"] for r in self.journal(task_id, "transition")]

    def thread_roles(self, task_id):
        return {tid: self.stores.threads.get(tid).role for tid in self.task(task_id).thread_ids}

    def run_to_done(self, **values):
        task = self.new_task(**values)
        self.runner.start(task.id)
        return self.wait_state(task.id, S.DONE, S.BLOCKED, S.FAILED, S.CANCELLED)


class Lifecycle(Fixture):
    def test_happy_path_state_sequence_and_journal(self):
        task = self.run_to_done()
        self.assertEqual(task.state, S.DONE)
        self.assertEqual(self.states(task.id),
                         ["clarifying", "planned", "running", "verifying", "done"])
        self.assertEqual(task.spec.goal, SPEC["goal"])
        self.assertEqual(task.plan, PLAN["steps"])
        self.assertEqual(task.status.steps, 3)
        self.assertTrue(task.worktree and Path(task.worktree).is_dir())
        events = [r["event"] for r in self.journal(task.id)]
        for expected in ("worktree_created", "thread_opened", "spec", "plan",
                         "instruction", "worker_report", "review", "report"):
            self.assertIn(expected, events, expected)
        # Every relayed instruction is journalled with the thread it went to.
        implementer = [tid for tid, role in self.thread_roles(task.id).items()
                       if role == Role.IMPLEMENTER][0]
        for row in self.journal(task.id, "instruction"):
            self.assertEqual(row["thread_id"], implementer)

    def test_report_is_saved_in_the_section_10_4_shape(self):
        task = self.run_to_done()
        self.assertEqual(task.report.done, REPORT["done"])
        self.assertEqual(task.report.changed, REPORT["changed"])
        self.assertEqual(task.report.verified, REPORT["verified"])
        self.assertEqual(task.report.open, [])
        self.assertEqual(task.report.next, REPORT["next"])
        # COST is the runner's, never the model's prose (§10.3).
        self.assertIsInstance(task.report.cost, dict)
        reloaded = json.loads((self.stores.tasks.path(task.id)).read_text())
        self.assertEqual(reloaded["report"]["verified"], REPORT["verified"])

    def test_each_role_gets_its_own_thread_and_the_reviewer_prefers_the_other_provider(self):
        task = self.run_to_done()
        roles_by_thread = self.thread_roles(task.id)
        self.assertEqual(sorted(r.value for r in roles_by_thread.values()),
                         ["implementer", "orchestrator", "reviewer"])
        self.assertEqual(len(set(roles_by_thread)), 3)
        threads = {role: self.stores.threads.get(tid)
                   for tid, role in roles_by_thread.items()}
        self.assertEqual(threads[Role.ORCHESTRATOR].provider, P.CLAUDE)
        self.assertEqual(threads[Role.IMPLEMENTER].provider, P.CODEX)
        # §8.2 soft preference: two families miss different things.
        self.assertNotEqual(threads[Role.REVIEWER].provider,
                            threads[Role.IMPLEMENTER].provider)
        self.assertEqual(task.thread_ids[0], next(tid for tid, role in roles_by_thread.items()
                                                  if role == Role.ORCHESTRATOR))

    def test_status_record_is_built_from_events_not_from_the_model(self):
        self.script["Do step 1 now"] = [
            Event(K.TOOL_STARTED, "", {"call_id": "1", "name": "Edit",
                                       "args": {"file_path": "/work/app/health.py"}}),
            Event(K.TOOL_FINISHED, "", {"call_id": "1", "name": "Edit", "ok": True}),
            Event(K.USAGE, "", {"input": 1000, "output": 200, "cached": 100,
                                "cost_usd": 0.02}),
            Event(K.TEXT, "", {"text": "wrote the handler"}),
        ]
        task = self.run_to_done()
        self.assertEqual(task.status.last_tool, "Edit")
        self.assertEqual(task.status.last_file, "/work/app/health.py")
        self.assertAlmostEqual(task.status.cost_usd, 0.02, places=6)
        self.assertEqual(task.status.tokens, 1100)
        self.assertGreater(task.status.elapsed_s, 0)
        self.assertTrue(task.status.started)
        self.assertEqual(task.status.phase, S.DONE)
        self.assertGreaterEqual(len(task.status.routing), 3)

    def test_every_task_write_is_published_as_task_status_changed(self):
        task = self.run_to_done()
        snapshots = self.snapshots()
        self.assertGreater(self.writes, 0)
        self.assertGreaterEqual(len(snapshots), self.writes,
                                "every runner write publishes a snapshot")
        for record in snapshots:
            self.assertEqual(record["task_id"], task.id)
            self.assertEqual(record["project_id"], self.project.id)
            self.assertIn("state", record["data"])
        # Every transition is visible to the Reporter, and the last snapshot
        # matches what is actually on disk.
        seen = [r["data"]["state"] for r in snapshots]
        for state in self.states(task.id):
            self.assertIn(state, seen)
        self.assertEqual(snapshots[-1]["data"], to_json(self.task(task.id)))


class IntakeGate(Fixture):
    def test_a_blocking_question_parks_the_task_in_clarifying(self):
        spec = dict(SPEC, questions=[{"text": "Which port?", "blocking": True,
                                      "options": ["8080", "9000"]}])
        self.script["Write the SPEC"] = block(spec)
        task = self.new_task()
        self.runner.start(task.id)
        parked = eventually(lambda: self.task(task.id) if self.task(task.id).spec.questions else None,
                            5, "the spec to be saved")
        time.sleep(0.2)
        parked = self.task(task.id)
        self.assertEqual(parked.state, S.CLARIFYING)
        self.assertEqual(parked.status.open_question, "Which port?")
        self.assertEqual(self.claude.texts_to(Role.ORCHESTRATOR).count("Write the PLAN"), 0)
        asked = [r for r in self.drain() if r.get("kind") == "task_question"]
        self.assertEqual(len(asked), 1)
        self.assertEqual(asked[0]["data"]["questions"][0]["options"], ["8080", "9000"])
        # The parked task releases its slot rather than holding a worker.
        self.assertNotIn(task.id, self.runner._active)

        done = self.runner.answer_question(task.id, 0, "8080")
        self.assertEqual(done.spec.questions[0].answer, "8080")
        task = self.wait_state(task.id, S.DONE)
        self.assertEqual(self.states(task.id),
                         ["clarifying", "planned", "running", "verifying", "done"])
        plan_prompt = [t for t in self.claude.texts_to(Role.ORCHESTRATOR) if "Write the PLAN" in t][0]
        self.assertIn("owner: 8080", plan_prompt)

    def test_an_assumable_question_proceeds_with_its_assumption_in_the_spec(self):
        spec = dict(SPEC, questions=[{"text": "Which port?", "blocking": False,
                                      "assumed": "8080, the project default"}])
        self.script["Write the SPEC"] = block(spec)
        task = self.run_to_done()
        self.assertEqual(task.state, S.DONE)
        self.assertEqual(task.spec.questions[0].assumed, "8080, the project default")
        self.assertIsNone(task.spec.questions[0].answer)
        self.assertIsNone(task.status.open_question)
        plan_prompt = [t for t in self.claude.texts_to(Role.ORCHESTRATOR) if "Write the PLAN" in t][0]
        self.assertIn("assumed: 8080, the project default", plan_prompt)

    def test_an_unparseable_spec_is_reasked_once_and_then_blocks(self):
        self.script["Write the SPEC"] = "The goal is roughly to add health checks, I think."
        self.script["reply with ONLY the fenced json block"] = "Sure: add health checks."
        task = self.run_to_done()
        self.assertEqual(task.state, S.BLOCKED)
        asks = [t for t in self.claude.texts_to(Role.ORCHESTRATOR)
                if t.startswith(roles.REASK)]
        self.assertEqual(len(asks), 1, "the runner re-asks exactly once")
        self.assertIn('"acceptance"', asks[0], "the re-ask quotes the contract")
        reason = self.journal(task.id, "blocked")[-1]["reason"]
        self.assertIn("spec", reason)
        self.assertEqual(self.claude.texts_to(Role.ORCHESTRATOR).count("Write the PLAN"), 0)
        self.assertEqual(self.task(task.id).spec.goal, "")

    def test_a_spec_is_never_guessed_from_prose(self):
        self.assertIsNone(roles.parse_block("The goal is to add health checks.", "spec"))
        self.assertIsNone(roles.parse_block("```json\n{\"goal\": \"x\"}\n```", "spec"))
        self.assertIsNone(roles.parse_block("```json\n{\"goal\": \"g\", \"deliverable\": \"d\","
                                            " \"acceptance\": []}\n```", "spec"))
        parsed = roles.parse_block("prose\n```json\n" + json.dumps(SPEC) + "\n```\nmore", "spec")
        self.assertEqual(parsed["goal"], SPEC["goal"])
        self.assertEqual(parsed["constraints"], SPEC["constraints"])
        # The LAST block wins: a model that shows an example first must not win with it.
        two = ("```json\n{\"pass\": false, \"findings\": [\"a\"]}\n```\n"
               "```json\n{\"pass\": true, \"findings\": []}\n```")
        self.assertEqual(roles.parse_block(two, "review"), {"pass": True, "findings": []})
        self.assertIsNone(roles.parse_block("```json\n{\"pass\": false, \"findings\": []}\n```",
                                            "review"), "a failing review must say why")
        self.assertIsNone(roles.parse_block("```json\n{\"next\": null, \"done\": false}\n```",
                                            "next"))


class Verification(Fixture):
    def test_a_failing_review_loops_once_and_a_second_failure_blocks(self):
        findings = ["/healthz returns 500", "no test covers it"]
        self.script["Verify the deliverable"] = block({"pass": False, "findings": findings})
        self.script["What next?"] = block({"next": None, "done": True})
        task = self.run_to_done()
        self.assertEqual(task.state, S.BLOCKED)
        self.assertEqual(self.states(task.id),
                         ["clarifying", "planned", "running", "verifying", "running",
                          "verifying", "blocked"])
        reviews = self.journal(task.id, "review")
        self.assertEqual(len(reviews), 2)
        self.assertEqual([r["passed"] for r in reviews], [False, False])
        # Two reviewer threads: each verification is a fresh session (§10.1).
        reviewer_threads = [tid for tid, role in self.thread_roles(task.id).items()
                            if role == Role.REVIEWER]
        self.assertEqual(len(reviewer_threads), 2)
        # The findings are relayed to the implementer as the next instruction.
        relayed = [t for t in self.codex.texts_to(Role.IMPLEMENTER) if findings[0] in t]
        self.assertTrue(relayed, "the findings reach the implementer")
        self.assertIn(findings[1], relayed[0])
        reason = self.journal(task.id, "blocked")[-1]["reason"]
        self.assertIn("twice", reason)
        self.assertIn(findings[0], reason)

    def test_a_pass_after_a_failure_finishes_the_task(self):
        seen = {"n": 0}

        def review(role, text, thread_id):
            seen["n"] += 1
            if seen["n"] == 1:
                return block({"pass": False, "findings": ["missing test"]})
            return block({"pass": True, "findings": []})

        self.script["Verify the deliverable"] = review
        self.script["What next?"] = block({"next": None, "done": True})
        task = self.run_to_done()
        self.assertEqual(task.state, S.DONE)
        self.assertEqual(self.states(task.id)[-3:], ["running", "verifying", "done"])
        self.assertEqual(seen["n"], 2)


class Steering(Fixture):
    def test_steer_is_delivered_to_the_orchestrator_at_the_next_boundary(self):
        gate = threading.Event()
        self.script["Do step 1 now"] = lambda *a: (gate.wait(5), "wrote the handler")[1]
        task = self.new_task()
        self.runner.start(task.id)
        eventually(lambda: self.codex.texts_to(Role.IMPLEMENTER), 5, "step 1 to be running")
        self.runner.steer(task.id, "use TLS on the endpoint")
        self.runner.steer(task.id, "and keep it under 50 lines", spoken=True)
        gate.set()
        task = self.wait_state(task.id, S.DONE)
        steered = [t for t in self.claude.texts_to(Role.ORCHESTRATOR)
                   if t.startswith("[owner steering]")]
        self.assertEqual(len(steered), 2)
        self.assertEqual(steered[0], "[owner steering] use TLS on the endpoint")
        self.assertEqual(steered[1], "[owner steering] [voice note] and keep it under 50 lines")
        rows = self.journal(task.id, "steering_delivered")
        self.assertEqual([r["spoken"] for r in rows], [False, True])
        # The property that matters: the orchestrator hears the steering BEFORE
        # it is asked for the instruction that follows it, so the steering can
        # change what that instruction says.
        to_orchestrator = self.claude.texts_to(Role.ORCHESTRATOR)
        first_steer = next(i for i, t in enumerate(to_orchestrator)
                           if t.startswith("[owner steering]"))
        relays = [i for i, t in enumerate(to_orchestrator) if "What next?" in t]
        self.assertTrue(relays, "the orchestrator was asked for a next instruction")
        self.assertLess(first_steer, relays[-1],
                        "steering lands before the next instruction is asked for")
        # And before the instruction it influences is relayed to the implementer.
        order = [r["event"] for r in self.journal(task.id)
                 if r["event"] in ("steering_delivered", "instruction")]
        self.assertLess(order.index("steering_delivered"), len(order) - 1)

    def test_steer_carries_an_owner_provider_override_into_the_journal(self):
        task = self.new_task()
        self.runner.steer(task.id, "use codex for reviewer, and hurry")
        rows = self.journal(task.id, "owner_override")
        self.assertEqual(rows[-1]["provider"], "codex")
        self.assertEqual(rows[-1]["role"], "reviewer")
        queued = self.runner._run(task.id).steers[0][0]
        self.assertNotIn("use codex", queued)


class Cancelling(Fixture):
    def test_cancel_lands_at_a_boundary_while_a_turn_is_mid_flight(self):
        started, release = threading.Event(), threading.Event()

        def slow(role, text, thread_id):
            started.set()
            release.wait(5)
            return "half-finished"

        self.script["Do step 1 now"] = slow
        task = self.new_task()
        self.runner.start(task.id)
        self.assertTrue(started.wait(5), "the implementer turn started")
        returned = self.runner.cancel(task.id)
        self.assertIn(returned.state, (S.RUNNING, S.CANCELLED))
        release.set()
        task = self.wait_state(task.id, S.CANCELLED)
        self.assertEqual(self.states(task.id)[-1], "cancelled")
        self.assertTrue(self.codex.interrupted, "the running turn was interrupted")
        # The worktree is left in place (§7): a cancelled task keeps its branch.
        self.assertTrue(Path(task.worktree).is_dir())
        # Nothing was relayed after the cancel.
        self.assertEqual(len(self.journal(task.id, "instruction")), 1)

    def test_cancel_before_any_turn_transitions_immediately(self):
        task = self.new_task()
        cancelled = self.runner.cancel(task.id)
        self.assertEqual(cancelled.state, S.CANCELLED)
        self.assertEqual(self.claude.sent, [])

    def test_cancel_within_the_proposal_grace_window_withdraws_the_task(self):
        task = self.new_task()
        self.stores.tasks.journal(task.id, "proposed_by_fastpath", brief=task.brief,
                                  thread_id="0" * 8, turn_id="t1", reply="Opened task",
                                  not_before=time.time() + 60)
        with self.assertRaises(ControlError):
            self.runner.start(task.id)
        self.assertEqual(self.runner.cancel(task.id).state, S.CANCELLED)
        self.assertTrue(any(r["event"] == "transition" and r["new_state"] == "cancelled"
                            and "grace" in r["reason"] for r in self.journal(task.id)))


class Ceilings(Fixture):
    def test_a_ceiling_hit_blocks_with_the_spend_and_resume_needs_it_raised(self):
        self.script["Do step 1 now"] = [
            Event(K.USAGE, "", {"input": 5000, "output": 500, "cost_usd": 7.5}),
            lambda stop: stop.wait(5),     # the turn is still running when it lands
            Event(K.TEXT, "", {"text": "wrote the handler"}),
        ]
        task = self.run_to_done(ceilings={"usd": 1.0})
        self.assertEqual(task.state, S.BLOCKED)
        reason = self.journal(task.id, "ceiling_hit")[-1]["reason"]
        self.assertIn("usd ceiling hit", reason)
        self.assertIn("7.5", reason)
        self.assertIn("Raise the ceiling", reason)
        self.assertTrue(self.codex.interrupted, "the running turn was interrupted")
        with self.assertRaises(ControlError) as caught:
            self.runner.resume(task.id)
        self.assertIn("ceiling", str(caught.exception))

        raised = self.task(task.id)
        raised.ceilings = {"usd": 100.0}
        self.stores.tasks.save(raised)
        self.script["Do step 1 now"] = "wrote the handler"
        self.runner.resume(task.id)
        task = self.wait_state(task.id, S.DONE, S.BLOCKED)
        self.assertEqual(task.state, S.DONE)


class OrchestratorRestart(Fixture):
    def test_a_fatal_orchestrator_error_blocks_and_resume_restarts_from_disk(self):
        fail = {"armed": True}

        def flaky(role, text, thread_id):
            if fail["armed"]:
                fail["armed"] = False
                return [Event(K.ERROR, "", {"message": "claude health check failed: login expired",
                                            "fatal": True})]
            return self.next_instruction(text)

        self.script["What next?"] = flaky
        task = self.run_to_done()
        self.assertEqual(task.state, S.BLOCKED)
        reason = self.journal(task.id, "blocked")[-1]["reason"]
        self.assertIn(f"Task {task.id} blocked", reason)
        self.assertIn("orchestrator claude unavailable", reason)
        self.assertIn("login expired", reason)
        self.assertIn(f"resume {task.id} on codex", reason)
        self.assertTrue(self.journal(task.id, "orchestrator_unavailable"))

        before = list(self.codex.sent)
        self.runner.resume(task.id, provider=P.CODEX)
        task = self.wait_state(task.id, S.DONE, S.BLOCKED, timeout=15)
        self.assertEqual(task.state, S.DONE)
        self.assertTrue(self.journal(task.id, "orchestrator_restarted"))
        new_orchestrator = [t for t in self.codex.sent[len(before):]
                            if t[1] == Role.ORCHESTRATOR]
        self.assertTrue(new_orchestrator, "the new orchestrator runs on codex")
        first = new_orchestrator[0][2]
        self.assertIn(SPEC["goal"], first)
        self.assertIn(SPEC["acceptance"][0], first)
        self.assertIn(PLAN["steps"][0], first)
        self.assertIn("did it:", first, "the durable worker reports travel, not the transcript")
        self.assertIn("there is none", first)
        self.assertNotIn("Write the SPEC", first)
        # Two orchestrator threads exist and the newest is first (§4 ordering).
        orchestrators = [tid for tid in self.task(task.id).thread_ids
                         if self.stores.threads.get(tid).role == Role.ORCHESTRATOR]
        self.assertEqual(len(orchestrators), 2)
        self.assertEqual(self.task(task.id).thread_ids[0], orchestrators[0])
        self.assertEqual(self.stores.threads.get(orchestrators[0]).provider, P.CODEX)

    def test_routing_with_no_survivor_blocks_the_task(self):
        self.claude.healthy = False
        self.codex.healthy = False
        task = self.run_to_done()
        self.assertEqual(task.state, S.BLOCKED)
        reason = self.journal(task.id, "blocked")[-1]["reason"]
        self.assertIn("unavailable", reason)
        self.assertEqual(self.claude.started, [])


class EscapeHatchInterop(Fixture):
    def test_a_reviewer_declined_mid_run_does_not_break_the_step_loop(self):
        """The hatch (WP5) answers a decline by sending an `owner-ran` message on
        the worker's own thread. The runner must ride that out, not collide."""
        hatch_done = threading.Event()

        def declining(role, text, thread_id):
            # Fire the decline, then let the hatch send its result while the
            # runner is trying to relay the next instruction.
            def owner_ran():
                time.sleep(0.05)
                try:
                    self.daemon.send(thread_id, UserMessage(
                        text="[owner ran: npm test]\nall green", origin="owner-ran"))
                except Exception:
                    pass
                hatch_done.set()

            threading.Thread(target=owner_ran, daemon=True).start()
            return [Event(K.REVIEWER_DECLINED, "", {"tool": "Bash", "args": {},
                                                    "command": "npm test",
                                                    "reason": "network"}),
                    Event(K.TEXT, "", {"text": "wrote the handler; npm test was declined"})]

        self.script["Do step 1 now"] = declining
        task = self.run_to_done()
        self.assertTrue(hatch_done.wait(5))
        self.assertEqual(task.state, S.DONE)
        self.assertEqual(len(self.journal(task.id, "instruction")), len(PLAN["steps"]))
        origins = [origin for _t, role, _text, origin in self.codex.sent
                   if role == Role.IMPLEMENTER]
        self.assertIn("owner-ran", origins)


class Proposals(Fixture):
    def test_a_fast_path_proposal_becomes_a_task_that_starts_after_its_grace(self):
        now = {"t": 1_800_000_000.0}
        self.router.clock = lambda: now["t"]
        thread = self.stores.threads.create(self.project.id, Role.CHAT, P.FAST)
        self.stores.threads.save(thread)
        record = {"kind": "turn_finished", "thread_id": thread.id, "turn_id": "turn-1",
                  "project_id": self.project.id,
                  "data": {"stop": "end", "proposal": {"brief": "add a health endpoint"}}}
        reply = self.runner.on_proposal(record)
        self.assertIn("Opened task", reply)
        self.assertEqual(self.runner.proposal_replies["turn-1"], reply)
        published = [r for r in self.drain() if r.get("kind") == "proposal_reply"]
        self.assertEqual(published[-1]["data"], {"turn_id": "turn-1", "reply": reply})
        task_id = [t.id for t in self.stores.tasks.list()][0]
        self.assertEqual(self.task(task_id).state, S.INTAKE)

        self.runner._admit_ready()
        time.sleep(0.15)
        self.assertEqual(self.task(task_id).state, S.INTAKE, "the grace window holds it")
        now["t"] += 61
        self.runner._admit_ready()
        task = self.wait_state(task_id, S.DONE, S.BLOCKED)
        self.assertEqual(task.state, S.DONE)

    def test_an_unplaceable_proposal_returns_the_question_and_opens_nothing(self):
        thread = self.stores.threads.create(self.project.id, Role.CHAT, P.FAST)
        self.stores.threads.save(thread)
        inbox = self.stores.projects.inbox()
        thread.project_id = inbox.id
        self.stores.threads.save(thread)
        reply = self.runner.on_proposal(
            {"kind": "turn_finished", "thread_id": thread.id, "turn_id": "t2",
             "data": {"stop": "end", "proposal": {"brief": "do a thing"}}})
        self.assertIn("project", reply.lower())
        self.assertEqual(self.stores.tasks.list(), [])


class Queueing(Fixture):
    max_active = 2

    def test_max_active_queues_the_rest_in_order(self):
        gate = threading.Event()
        order: list[str] = []
        lock = threading.Lock()

        def hold(role, text, thread_id):
            with lock:
                order.append(thread_id)
            gate.wait(8)
            return block(SPEC)

        self.script["Write the SPEC"] = hold
        tasks = [self.new_task(f"task {i}") for i in range(4)]
        for task in tasks:
            self.runner.start(task.id)
        eventually(lambda: len(order) >= 2, 5, "two tasks to run at once")
        time.sleep(0.2)
        self.assertEqual(len(order), 2, "MAX_ACTIVE caps concurrency")
        self.assertEqual(len(self.runner._active), 2)
        self.assertEqual(list(self.runner._queue), [tasks[2].id, tasks[3].id])
        gate.set()
        for task in tasks:
            self.wait_state(task.id, S.DONE, S.BLOCKED, timeout=20)
        self.assertEqual([self.task(t.id).state for t in tasks], [S.DONE] * 4)


class ControlSurface(Fixture):
    def test_every_control_error_path_raises_control_error(self):
        missing = "0" * 8
        for call in (lambda: self.runner.start(missing),
                     lambda: self.runner.steer(missing, "x"),
                     lambda: self.runner.cancel(missing),
                     lambda: self.runner.resume(missing),
                     lambda: self.runner.status(missing),
                     lambda: self.runner.answer_question(missing, 0, "x")):
            with self.assertRaises(ControlError):
                call()
        task = self.new_task()
        with self.assertRaises(ControlError):
            self.runner.steer(task.id, "   ")
        with self.assertRaises(ControlError):
            self.runner.answer_question(task.id, 0, "x")     # not clarifying
        with self.assertRaises(ControlError):
            self.runner.resume(task.id)                      # not blocked
        self.runner.cancel(task.id)
        for call in (lambda: self.runner.start(task.id),
                     lambda: self.runner.steer(task.id, "x"),
                     lambda: self.runner.cancel(task.id),
                     lambda: self.runner.resume(task.id)):
            with self.assertRaises(ControlError):
                call()
        # A failed task is terminal in model.TRANSITIONS; say so rather than
        # raising StoreError from underneath.
        failed = self.new_task()
        self.stores.tasks.transition(failed.id, S.CLARIFYING)
        self.stores.tasks.transition(failed.id, S.PLANNED)
        self.stores.tasks.transition(failed.id, S.RUNNING)
        self.stores.tasks.transition(failed.id, S.FAILED)
        with self.assertRaises(ControlError) as caught:
            self.runner.resume(failed.id)
        self.assertIn("cannot be resumed", str(caught.exception))

    def test_status_and_list_tasks(self):
        done = self.run_to_done()
        other = self.new_task("second")
        active = self.runner.list_tasks()
        self.assertEqual([t.id for t in active], [other.id])
        everything = {t.id for t in self.runner.list_tasks(active_only=False)}
        self.assertEqual(everything, {done.id, other.id})
        self.assertEqual(self.runner.list_tasks(project_id="1" * 8), [])
        self.assertEqual(self.runner.status(done.id).state, S.DONE)

    def test_an_answer_out_of_range_or_empty_is_refused(self):
        spec = dict(SPEC, questions=[{"text": "Which port?", "blocking": True}])
        self.script["Write the SPEC"] = block(spec)
        task = self.new_task()
        self.runner.start(task.id)
        eventually(lambda: self.task(task.id).status.open_question, 5, "the question")
        for index in (-1, 5, True):
            with self.assertRaises(ControlError):
                self.runner.answer_question(task.id, index, "8080")
        with self.assertRaises(ControlError):
            self.runner.answer_question(task.id, 0, "")
        self.runner.cancel(task.id)

    def test_resume_refuses_the_fast_path_as_an_orchestrator(self):
        task = self.new_task()
        self.stores.tasks.transition(task.id, S.CLARIFYING)
        self.stores.tasks.transition(task.id, S.BLOCKED)
        with self.assertRaises(ControlError):
            self.runner.resume(task.id, provider=P.FAST)


class RoleBriefs(unittest.TestCase):
    def test_every_role_has_a_brief_and_the_prohibitions_are_stated(self):
        for role in (Role.ORCHESTRATOR, Role.IMPLEMENTER, Role.REVIEWER, Role.RESEARCHER):
            brief = roles.brief_for(role)
            self.assertEqual(brief.role, role)
            self.assertTrue(brief.system_append.strip())
            self.assertIsInstance(brief.max_turns, int)
            self.assertGreater(brief.max_turns, 0)
        self.assertIn("NEVER implement", roles.brief_for(Role.ORCHESTRATOR).system_append)
        self.assertIn("NEVER edit", roles.brief_for(Role.REVIEWER).system_append)
        self.assertIn("FINDINGS ONLY", roles.brief_for(Role.RESEARCHER).system_append)
        with self.assertRaises(KeyError):
            roles.brief_for("nonsense")

    def test_allowed_tools_honour_each_providers_meaning(self):
        reviewer = roles.brief_for(Role.REVIEWER)
        self.assertEqual(reviewer.tools_for(P.CLAUDE), list(roles.REVIEW_TOOLS))
        # Codex refuses a non-None allowed_tools outright (codex_config); sending
        # one would BriefRefused a task for a reason unrelated to the task.
        self.assertIsNone(reviewer.tools_for(P.CODEX))
        self.assertIsNone(roles.brief_for(Role.IMPLEMENTER).tools_for(P.CLAUDE))
        for role in (Role.ORCHESTRATOR, Role.REVIEWER, Role.RESEARCHER):
            tools = roles.brief_for(role).tools_for(P.CLAUDE)
            self.assertNotIn("Edit", tools)
            self.assertNotIn("Write", tools)
        self.assertIn("Bash", roles.brief_for(Role.REVIEWER).tools_for(P.CLAUDE))
        self.assertNotIn("Bash", roles.brief_for(Role.ORCHESTRATOR).tools_for(P.CLAUDE))

    def test_every_shape_has_a_contract_the_runner_can_quote(self):
        self.assertEqual(sorted(roles.SHAPES), sorted(roles.CONTRACTS))
        for shape, contract in roles.CONTRACTS.items():
            self.assertIn("```json", contract, shape)
        self.assertEqual(roles.parse_block(block(PLAN), "plan"), PLAN)
        self.assertIsNone(roles.parse_block(block({"steps": []}), "plan"))
        report = roles.parse_block(block(REPORT), "report")
        self.assertEqual(set(report), {"done", "changed", "verified", "open", "next"})
        with self.assertRaises(KeyError):
            roles.parse_block("{}", "invented")


class BriefWiring(Fixture):
    def test_each_thread_is_opened_with_its_role_brief_and_the_task_worktree(self):
        task = self.run_to_done()
        briefs = {**self.claude.briefs, **self.codex.briefs}
        by_role = {self.stores.threads.get(tid).role: briefs[tid]
                   for tid in task.thread_ids if tid in briefs}
        for role, brief in by_role.items():
            self.assertEqual(brief.role, role)
            self.assertEqual(brief.cwd, task.worktree)
            self.assertEqual(brief.task_id, task.id)
            self.assertEqual(brief.system_append, roles.brief_for(role).system_append)
            self.assertEqual(brief.max_turns, roles.brief_for(role).max_turns)
            self.assertEqual(brief.profile, self.project.profile)
        self.assertIsNone(by_role[Role.IMPLEMENTER].allowed_tools)

    def test_a_project_profile_and_always_ask_reach_every_brief(self):
        self.project.profile = PermissionProfile.ASK
        self.project.always_ask = ["custom-rule"]
        self.stores.projects.save(self.project)
        task = self.run_to_done()
        briefs = {**self.claude.briefs, **self.codex.briefs}
        for tid in task.thread_ids:
            self.assertEqual(briefs[tid].profile, PermissionProfile.ASK)
            self.assertEqual(briefs[tid].always_ask, ["custom-rule"])


class DaemonRoutes(Fixture):
    def setUp(self):
        super().setUp()
        try:
            self.daemon.start()
        except (OSError, PermissionError) as exc:      # sandboxes refuse loopback
            self.skipTest(f"cannot bind a loopback socket: {exc}")
        self.daemon.runner = self.runner

    def request(self, method, path, body=None):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.daemon.port, timeout=10)
        try:
            conn.request(method, path, json.dumps(body) if body is not None else None,
                         {"Content-Type": "application/json"})
            response = conn.getresponse()
            return response.status, json.loads(response.read() or b"null")
        finally:
            conn.close()

    def test_the_task_verbs_are_reachable_over_the_local_api(self):
        spec = dict(SPEC, questions=[{"text": "Which port?", "blocking": True}])
        self.script["Write the SPEC"] = block(spec)
        task = self.new_task()
        status, body = self.request("POST", f"/tasks/{task.id}/start", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "clarifying")
        eventually(lambda: self.task(task.id).status.open_question, 5, "the question")
        self.assertEqual(self.request("POST", f"/tasks/{task.id}/steer",
                                      {"text": "keep it small"})[:1], (200,))
        self.assertEqual(self.request("POST", f"/tasks/{task.id}/steer",
                                      {"text": "spoken", "spoken": True})[0], 200)
        status, body = self.request("POST", f"/tasks/{task.id}/answer",
                                    {"index": 0, "text": "8080"})
        self.assertEqual(status, 200)
        self.assertEqual(body["spec"]["questions"][0]["answer"], "8080")
        self.wait_state(task.id, S.DONE, timeout=15)
        self.assertEqual(self.request("POST", f"/tasks/{task.id}/cancel", {})[0], 409)

    def test_the_routes_refuse_bad_input_and_a_missing_runner(self):
        task = self.new_task()
        self.assertEqual(self.request("POST", f"/tasks/{task.id}/answer", {"index": 0})[0], 400)
        self.assertEqual(self.request("POST", f"/tasks/{task.id}/steer",
                                      {"text": "x", "spoken": "yes"})[0], 400)
        self.assertEqual(self.request("POST", f"/tasks/{task.id}/resume",
                                      {"provider": "nonsense"})[0], 400)
        self.assertEqual(self.request("POST", "/tasks/00000000/start", {})[0], 404)
        self.assertEqual(self.request("POST", f"/tasks/{task.id}/invented", {})[0], 404)
        self.daemon.runner = None
        self.assertEqual(self.request("POST", f"/tasks/{task.id}/start", {})[0], 409)


class BranchDiff(Fixture):
    """The one place the runner shells out: the diff the reviewer is shown."""

    def vcs(self, cwd, *args):
        import subprocess
        return subprocess.run(["git", "-c", "user.email=t@example.com",
                               "-c", "user.name=T", "-c", "commit.gpgsign=false", *args],
                              cwd=str(cwd), capture_output=True, text=True,
                              check=True).stdout

    def test_the_reviewer_sees_committed_and_uncommitted_work_on_the_branch(self):
        repo = Path(self.tmp.name) / "repo"
        repo.mkdir()
        self.vcs(repo, "init", "-q", "-b", "main")
        (repo / "README.md").write_text("start\n")
        self.vcs(repo, "add", "-A")
        self.vcs(repo, "commit", "-qm", "first")
        project = self.stores.projects.create("Repo", str(repo))
        task = self.stores.tasks.create(project.id, "add a health endpoint")
        self.stores.tasks.save(task)
        task = worktrees.ensure(task, project, self.stores)
        self.assertTrue(task.branch.startswith("jarvis/"))

        self.assertIn("no changes", self.runner._diff(task))
        (Path(task.worktree) / "health.py").write_text("def healthz():\n    return 200\n")
        self.vcs(task.worktree, "add", "-A")
        self.vcs(task.worktree, "commit", "-qm", "handler")
        (Path(task.worktree) / "notes.txt").write_text("still working\n")
        diff = self.runner._diff(task)
        self.assertIn("committed", diff)
        self.assertIn("health.py", diff)
        self.assertIn("def healthz", diff)
        prompt = self.runner._review_prompt(task, "1" * 8)
        self.assertIn("health.py", prompt)
        self.assertIn(task.branch, prompt)

    def test_a_non_git_task_says_so_instead_of_shelling_out(self):
        task = self.new_task()
        self.assertEqual(self.runner._diff(task), "(no git branch for this task)")


class Recovery(Fixture):
    def test_serve_readmits_a_task_a_previous_process_left_mid_flight(self):
        task = self.new_task()
        for state in (S.CLARIFYING, S.PLANNED, S.RUNNING):
            self.stores.tasks.transition(task.id, state)
        current = self.task(task.id)
        current.spec = self.task(task.id).spec
        worktrees.ensure(current, self.project, self.stores)
        self.runner.serve()
        task = self.wait_state(task.id, S.DONE, S.BLOCKED, timeout=15)
        self.assertEqual(task.state, S.DONE)
        self.assertTrue(self.journal(task.id, "recovered"))

    def test_recovery_never_starts_an_unadmitted_intake_task(self):
        task = self.new_task()
        self.runner.serve()
        time.sleep(0.3)
        self.assertEqual(self.task(task.id).state, S.INTAKE)
        self.assertEqual(self.claude.sent, [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
