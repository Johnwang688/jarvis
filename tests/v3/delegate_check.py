"""Exercise the desk, phone and voice hand-off boundary with fake T3 and models."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime
from itertools import count
import json
import stat
import unittest
from unittest.mock import Mock, patch

from a6c1_acceptance_check import Base, Model, reply
from r1a_policy_grid_check import baseline_policy, fact_grid
from voice_brain_check import Factory, PREFIX, Recorder, script

from jarvis import config, tools
from jarvis.v3 import claude_jobs, fastpath, policy, t3_client
from jarvis.v3.approvals import digest
from jarvis.v3.voice.approving import VoiceApproving
from jarvis.v3.voice.claude_brain import ClaudeVoiceBrain, STEP_LIMIT_REPLY
from jarvis.v3.voice.turns import _approval_sentence
from jarvis.v3.work import DELEGATE_PROMPT, Ledger, Registry, WorkError, WorkService


TASK = "Write the fixture tests."
NAMED = {"task": TASK, "project": "fixture"}
BRIEFING = (
    "The owner asked Jarvis for this task. Jarvis handed it to you to do in the background. "
    "Do the task. If something you need is missing or the task is unsafe or unclear, stop and say "
    "what you need instead of guessing. Finish with a short, plain summary of what you did and "
    "what, if anything, is left."
)
REMINDER = "If you cannot finish this yourself, hand it off with delegate and tell the owner you did."
BAD = ("get_datetime", {"extra": "invalid"})
GOOD = ("get_datetime", {})
SOL = {"provider": "codex", "model": "gpt-6.2-sol"}


class Delegation(Base):
    def setUp(self):
        super().setUp()
        self.clock.now = datetime(2030, 1, 15, 23, 30).timestamp()
        self.registry = Registry(self.v3 / "work" / "projects.json")
        self.ledger = Ledger(self.v3 / "work" / "threads.jsonl")
        self.workspace = self.v3 / "work" / "workspace"
        self.project = self.tmp / "fixture"
        self.project.mkdir()
        self.t3 = Mock(spec=t3_client.T3)
        self.targets = {}
        self.t3.project_at.side_effect = self.targets.get
        self.t3.project_add.side_effect = self.register_t3
        runs = count(1)

        def start(*args, **kwargs):
            number = next(runs)
            return t3_client.Run(f"fixture-thread-{number}", f"fixture-run-{number}", "queued")

        self.t3.start.side_effect = start
        self.work = self.work_service()
        self.enterContext(patch.object(t3_client, "resolve", return_value="/example/bin/fake-t3"))
        actual = self.enterContext(patch.object(t3_client.T3, "_invoke", side_effect=AssertionError("real T3")))
        self.addCleanup(actual.assert_not_called)

    def register_t3(self, path, *, title):
        self.targets[path] = "project-workspace"

    def register(self):
        return self.registry.add("fixture", self.project, provider="claude", model="fixture-model", base="main")

    def work_service(self, ledger=None):
        return WorkService(registry=self.registry, ledger=ledger or self.ledger, t3=self.t3,
                           clock=self.clock, token=lambda _: "abcd")

    def desk(self, *steps, broker=True):
        self.model = Model(*steps)
        return self.service(self.model, work=self.work, approvals=self.broker if broker else None)

    def finish(self, svc):
        self.assertEqual(self.wait_idle(svc), "idle")
        return self.results()

    def voice(self, *scripts, broker=True):
        self.enterContext(patch.object(claude_jobs, "resolve_native_cli", return_value="/example/bin/fake-claude"))
        self.enterContext(patch.object(claude_jobs, "cli_version_status", return_value="ok"))
        spawn = self.enterContext(patch("subprocess.Popen", side_effect=AssertionError("real child process")))
        self.addCleanup(spawn.assert_not_called)
        factory = Factory()
        factory.scripts.extend(scripts)
        self.voice_events = Recorder()
        brain = ClaudeVoiceBrain(self.v3, approvals=self.broker if broker else None, work=self.work,
                                 client_factory=factory, wall_clock=self.clock)
        self.addCleanup(brain.close)
        brain.subscribe(self.voice_events)
        return brain, factory

    def test_named_project_runs_on_sol_in_worktree_with_raw_task(self):
        registered = self.register()
        first_line = "Write fixture documentation with several detailed examples"
        task = f" \t{first_line.replace(' ', '  ')} covering limits\nKeep this line.\n "
        planned = self.work.plan_delegate(task, "fixture")
        thread = self.work.start_delegate(planned, origin="desk")
        self.assertEqual(DELEGATE_PROMPT, BRIEFING)
        self.assertEqual(thread.title, first_line)
        self.t3.start.assert_called_once_with(
            str(self.project), BRIEFING + "\n\n" + task, title=first_line,
            worktree="main", branch=self.work.plan_start("fixture", first_line, task)["branch"], **SOL,
        )
        self.assertLessEqual(len(first_line), 60)
        self.assertEqual(self.registry.list(), [registered])
        self.assertEqual((thread.delegated, thread.origin), (True, "desk"))
        self.assertEqual(self.ledger.get(thread.thread_id), thread)
        self.assertEqual(self.work.plan_delegate("a" * 60 + " more")["title"], "a" * 60)
        self.assertEqual(self.work.plan_delegate("a" * 61 + " more")["title"], "a" * 60)

    def test_general_workspace_created_only_on_approved_start_and_registered_once(self):
        for project in ({}, {"project": None}):
            with self.subTest(project=project):
                svc = self.desk(reply(calls=[("delegate", {"task": TASK, **project})]), reply("Done."))
                svc.send("Hand off the fixture.")
                card = self.next_card()
                if not self.t3.start.call_count:
                    self.assertFalse(self.workspace.exists())
                self.assertEqual(card["card"]["rows"][0], {"label": "Project", "value": "General workspace"})
                self.broker.answer(card["id"], card["digest"], "approve")
                self.assertEqual(self.finish(svc)[-1], f'Handed off to Sol: "{TASK}".')
        self.assertEqual(stat.S_IMODE(self.workspace.stat().st_mode), 0o700)
        self.t3.project_add.assert_called_once_with(str(self.workspace), title="Jarvis workspace")
        self.assertEqual(self.t3.start.call_count, 2)
        self.t3.start.assert_called_with("project-workspace", BRIEFING + "\n\n" + TASK, title=TASK, **SOL)
        self.assertEqual(self.registry.list(), [])

    def test_wrong_workspace_target_and_t3_error_are_refused_without_retry(self):
        self.t3.project_add.side_effect = lambda path, **kw: self.register_t3(path + "-other", **kw)
        with self.assertRaisesRegex(WorkError, "exact Jarvis workspace"):
            self.work.start_delegate(self.work.plan_delegate(TASK), origin="desk")
        self.t3.start.assert_not_called()
        self.t3.project_add.assert_called_once()
        self.register()
        self.t3.start.side_effect = t3_client.T3Error("failed")
        policy.set_auto(self.policy_file, True)
        svc = self.desk(reply(calls=[("delegate", NAMED)]), reply("Could not start."))
        svc.send("Hand off the fixture.")
        self.assertTrue(self.finish(svc)[-1].startswith("Error:"))
        self.t3.start.assert_called_once()
        self.assertEqual(self.work.threads(), [])

    def test_a_work_brief_with_invisible_characters_is_refused(self):
        self.register()
        for brief in ("Write a fixture\u200b quietly.", "Write a fixture.\x07", "Write a fixture.\U000e0041"):
            with self.subTest(brief=ascii(brief)), self.assertRaisesRegex(WorkError, "cannot be shown"):
                self.work.plan_start("fixture", "Fixture title", brief)
        self.assertEqual(self.work.plan_start("fixture", "Fixture title", "Write a fixture ✅ in café.")["brief"],
                         "Write a fixture ✅ in café.")
        self.t3.start.assert_not_called()

    def test_invalid_tasks_and_unknown_project_never_ask_or_start(self):
        self.register()
        for args, reason in (
            (dict(NAMED, project="unknown"), "Registered projects: fixture"),
            ({"task": " \n "}, "1 to 4,000"),
            ({"task": "a" * 4_001}, "1 to 4,000"),
            ({"task": "Visible title\x00"}, "Invalid T3 CLI arguments"),
            ({"task": "Write a fixture.\nUse the label alpha\x07beta."}, "cannot be shown"),
            ({"task": "Write a fixture\u200b quietly."}, "cannot be shown"),
            ({"task": "Write a fixture.\U000e0041"}, "cannot be shown"),
        ):
            with self.subTest(reason=reason):
                svc = self.desk(reply(calls=[("delegate", args)]), reply("Refused."))
                svc.send("Try the fixture.")
                self.assertIn(reason, self.finish(svc)[-1])
        self.assertEqual(self.states(None), [])
        self.t3.start.assert_not_called()
        self.assertFalse(self.workspace.exists())

    def test_turn_limit_counts_starts_and_resets(self):
        policy.set_auto(self.policy_file, True)
        calls = [("delegate", {"task": f"Fixture {number}"}) for number in range(4)]
        svc = self.desk(reply(calls=calls), reply("Done."), reply(calls=[calls[-1]]), reply("Done."))
        svc.send("Hand off four.")
        results = self.finish(svc)
        self.assertEqual(results[-1], "Refused: Only 3 tasks may be handed off per turn. (the user was not asked).")
        self.assertEqual(self.t3.start.call_count, 3)
        svc.send("Hand off the last task.")
        self.finish(svc)
        self.assertEqual(self.t3.start.call_count, 4)

    def test_daily_limit_is_shared_across_services_and_resets_at_local_midnight(self):
        start = self.work.plan_delegate(TASK)
        for _ in range(11):
            self.work.start_delegate(start, origin="voice")
        fresh = self.work_service(Ledger(self.ledger.path))
        other = fresh.plan_delegate(TASK)

        def try_start(service, plan):
            try:
                return service.start_delegate(plan, origin="phone").thread_id
            except WorkError as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=2) as workers:
            a = workers.submit(try_start, self.work, start)
            b = workers.submit(try_start, fresh, other)
            outcomes = [a.result(), b.result()]
        self.assertCountEqual(outcomes, ["fixture-thread-12", "Only 12 tasks may be handed off per day."])
        restarted = self.work_service(Ledger(self.ledger.path))
        with self.assertRaisesRegex(WorkError, "Only 12"):
            restarted.plan_delegate(TASK)
        self.assertEqual(self.t3.start.call_count, 12)
        self.clock.now = datetime(2030, 1, 16, 0, 0).timestamp()
        restarted.start_delegate(restarted.plan_delegate(TASK), origin="desk")
        self.assertEqual(self.t3.start.call_count, 13)

    def test_policy_grid_matches_origin_main(self):
        self.assertEqual(fastpath._POLICY_KINDS["delegate"], "thread.start")
        main = baseline_policy(self)
        self.assertEqual((policy.DEFAULTS, policy.OWNER_REQUEST, policy.NEVER),
                         (main.DEFAULTS, main.OWNER_REQUEST, main.NEVER))
        current, before = policy.Policy(self.policy_file), main.Policy(self.policy_file)
        for auto in (False, True):
            policy.set_auto(self.policy_file, auto)
            for facts in fact_grid(policy.DEFAULTS):
                self.assertEqual(current.explain_with_auto(facts), before.explain_with_auto(facts), facts)

    def test_desk_auto_and_cards_for_taint_auto_off_and_phone(self):
        self.register()
        task = "Write the fixture tests\n" + "Full task detail. " * 200
        args = dict(NAMED, task=task)
        self.vault().add(folder="facts", name="fixture", description="Fixture", body="Example outside text.",
                         source="fixture")
        for reason, auto, before_call, phone, approve in (
            ("auto", True, [], False, True),
            ("hard:tainted", True, [("vault_read", {"name": "fixture"})], False, False),
            ("auto-off", False, [], False, False),
            ("phone", True, [], True, True),
        ):
            with self.subTest(reason=reason):
                policy.set_auto(self.policy_file, auto)
                starts = self.t3.start.call_count
                logged = len(self.states(None))
                svc = self.desk(reply(calls=[*before_call, ("delegate", args)]), reply("Done."))
                svc.new_session()
                svc.send("Hand off the fixture.", phone=phone)
                if reason != "auto":
                    item = self.next_card()
                    self.assertEqual((item["tool"], item["why"]), ("delegate", reason))
                    self.assertEqual(item["card"], {"title": "Hand off to Sol", "rows": [
                        {"label": label, "value": value} for label, value in (
                            ("Project", "fixture"), ("Folder", str(self.project)),
                            ("Model", "GPT-6.2 Sol"), ("Task", task),
                        )]})
                    self.assertEqual(self.t3.start.call_count, starts)
                    expected = self.work.plan_delegate(**args)
                    self.assertEqual(item["digest"], digest(item["id"], svc.snapshot()["session_id"],
                                                            "delegate", args, expected))
                    self.broker.answer(item["id"], item["digest"], "approve" if approve else "deny")
                self.finish(svc)
                if reason == "auto":
                    self.assertEqual([event["target"] for event in svc.events(0)["events"]
                                      if event["kind"] == "auto"], ["fixture"])
                states = ["auto"] if reason == "auto" else ["pending", "approved" if approve else "denied"]
                self.assertEqual(self.states(None)[logged:], states)
                self.assertEqual(self.t3.start.call_count, starts + approve)
        rows = [json.loads(line) for line in self.ledger.path.read_text().splitlines()]
        self.assertEqual([(row["delegated"], row["origin"]) for row in rows], [(True, "desk"), (True, "phone")])

    def test_grants_cannot_be_used_with_changed_task_project_or_model(self):
        self.register()
        fastpath.register_tools()
        policy.set_auto(self.policy_file, True)
        binding = fastpath._TurnTools(
            "fixture-session", "fixture-turn", self.v3, self.v3 / "feed", self.clock,
            lambda: nullcontext(True), lambda: False, lambda: None, lambda: False, lambda: None,
            work=self.work,
        )
        approve = fastpath.ChatApprover(self.broker, binding)
        token = fastpath._TURN.set(binding)
        try:
            for field, value in (("task", "Different task"), ("project", None), ("model", "fixture-model")):
                self.assertIs(approve(tools.REGISTRY["delegate"], NAMED), True)
                args = NAMED.copy()
                if field == "model":
                    binding.grant.preview[field] = value
                else:
                    args[field] = value
                self.assertIn("requires approval", fastpath.delegate(**args))
        finally:
            fastpath._TURN.reset(token)
        self.t3.start.assert_not_called()

    def test_voice_questions_and_spoken_approval_start_the_same_plan(self):
        self.register()
        scenarios = [
            (NAMED, "Hand this to Sol in fixture. Approve?"),
            ({"task": TASK}, "Hand this to Sol. Approve?"),
        ]
        brain, factory = self.voice(*(script(("", [("delegate", args)])) for args, _ in scenarios))
        microphone = VoiceApproving(self.broker, clock=self.clock)
        for index, (_, expected) in enumerate(scenarios, 1):
            turn = brain.send("Hand off the fixture.", steer=True, input_id=f"input-{index}", fresh_after=600)
            self.voice_events.wait("approval_pending", turn=turn)
            card, = brain.pending_approvals(turn)
            self.assertEqual(_approval_sentence(card), expected)
            self.assertNotIn(TASK, _approval_sentence(card))
            self.assertEqual(self.t3.start.call_count, index - 1)
            self.assertTrue(microphone.ask(card))
            microphone.prompt_finished(card["id"], self.clock())
            self.assertTrue(microphone.heard("yes"))
            self.voice_events.wait("reply", turn=turn)
            name, result, error = factory.clients[-1].results[-1]
            self.assertEqual((name, error), ("delegate", False))
            self.assertTrue(result.startswith("Handed off to Sol: "))
        self.assertTrue(all(thread.delegated and thread.origin == "voice" for thread in self.work.threads()))
        self.assertEqual(microphone.status()["voice_approvals"], 2)

    def test_clean_voice_can_delegate_automatically(self):
        policy.set_auto(self.policy_file, True)
        brain, _ = self.voice(script(("", [("delegate", {"task": TASK})])))
        turn = brain.send("Hand off the fixture.", steer=True, input_id="fixture", fresh_after=600)
        self.voice_events.wait("reply", turn=turn)
        self.assertEqual(self.states(None), ["auto"])
        self.assertEqual(self.voice_events.kinds(turn).count("auto"), 1)
        self.assertNotIn("approval_pending", self.voice_events.kinds(turn))
        self.assertEqual(self.work.threads()[0].origin, "voice")

    def test_reminder_matches_between_surfaces_and_keeps_result_provenance(self):
        clock = self.enterContext(patch("jarvis.tools.clock.datetime", wraps=datetime))
        clock.now.return_value = datetime.fromtimestamp(self.clock()).astimezone()
        for calls, trigger in (([BAD, BAD, BAD], 1), ([GOOD] * (config.FAST_STEPS - 1), config.FAST_STEPS - 3)):
            with self.subTest(trigger=trigger):
                steps = [[call] for call in calls]
                desk = self.desk(*(reply(calls=step) for step in steps), reply("Finished."))
                desk.new_session()
                desk.send("Try the fixture.")
                typed = self.finish(desk)
                self.assertEqual([i for i, value in enumerate(typed) if REMINDER in value], [trigger])
                self.assertTrue(typed[trigger].endswith("\n" + REMINDER))
                self.assertFalse(desk.snapshot()["outside_text"])
                self.assertEqual(self.kinds(desk).count("delegate_reminder"), 1)
                brain, factory = self.voice(script(*(("", step) for step in steps)))
                turn = brain.send("Try the fixture.", steer=True, input_id="fixture", fresh_after=600)
                self.voice_events.wait("reply", turn=turn)
                voiced = factory.clients[-1].results
                self.assertEqual([i for i, (_, value, _) in enumerate(voiced) if REMINDER in value], [trigger])
                self.assertEqual(voiced[trigger][1], typed[trigger])
                self.assertEqual(voiced[trigger][2], calls[trigger] == BAD)
                self.assertEqual(self.voice_events.kinds(turn).count("delegate_reminder"), 1)

    def test_reminder_is_skipped_without_tool_or_after_a_delegate_call(self):
        for holds in (False, True):
            with self.subTest(holds=holds):
                steps = [[BAD], [BAD, ("delegate", {})] if holds else [BAD], [BAD]]
                desk = self.desk(*(reply(calls=step) for step in steps), reply("Finished."), broker=holds)
                desk.send("Try the fixture.")
                self.assertFalse(any(REMINDER in value for value in self.finish(desk)))
                self.assertNotIn("delegate_reminder", self.kinds(desk))
                brain, factory = self.voice(script(*(("", step) for step in steps)), broker=holds)
                turn = brain.send("Try the fixture.", steer=True, input_id="fixture", fresh_after=600)
                self.voice_events.wait("reply", turn=turn)
                self.assertFalse(any(REMINDER in value for _, value, _ in factory.clients[-1].results))
                self.assertNotIn("delegate_reminder", self.voice_events.kinds(turn))

    def test_prompts_only_name_delegate_when_it_is_offered(self):
        for holds in (False, True):
            with self.subTest(holds=holds):
                desk = self.desk(reply("Fixture."), broker=holds)
                desk.send("Try the fixture.")
                self.finish(desk)
                request = self.model.seen[0]
                self.assertEqual("delegate" in request["tools"], holds)
                self.assertEqual(config.V3_DELEGATE_PARAGRAPH in request["messages"][0]["content"], holds)
                brain, factory = self.voice(script(), broker=holds)
                turn = brain.send("Try the fixture.", steer=True, input_id="fixture", fresh_after=600)
                self.voice_events.wait("reply", turn=turn)
                options = factory.clients[-1].options
                self.assertEqual(PREFIX + "delegate" in options.allowed_tools, holds)
                self.assertEqual(config.V3_DELEGATE_PARAGRAPH in options.system_prompt, holds)

    def test_step_limit_keeps_existing_closing_without_claiming_handoff(self):
        steps = [[("delegate", {})]] + [[GOOD]] * (config.FAST_STEPS - 1)
        desk = self.desk(*(reply(calls=step) for step in steps), reply("The fixture is still open."))
        desk.send("Try the fixture.")
        self.assertIn("missing required argument", self.finish(desk)[0])
        closing = [event["text"] for event in desk.events(0)["events"] if event["kind"] == "reply"][-1]
        self.assertIn("stopped after", closing)
        self.assertNotIn("handed off", closing.lower())
        brain, _ = self.voice(script(*(("", step) for step in steps)))
        turn = brain.send("Try the fixture.", steer=True, input_id="fixture", fresh_after=600)
        self.assertEqual(self.voice_events.wait("reply", turn=turn)["text"], STEP_LIMIT_REPLY)
        self.t3.start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
