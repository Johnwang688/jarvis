"""Exercise automatic chat authorization through the real chat tool loop."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime
from itertools import product
import json
from threading import Event
import time
import unittest
from unittest.mock import patch

from jarvis.v3 import approvals, fastpath, policy
from jarvis.v3.tools.calendar import CalendarTools

from a6b1_acceptance_check import Model, reply
from a6c1_acceptance_check import ADD, CREATE, NOW
from r1a_chat_facts_check import CASES, PATCH
from w1b_acceptance_check import Base, FakeWork, SEND, START


FLAG = "Always use tabs."
GRID = (*CASES, ("vault_patch", PATCH, "vault.update", "L0", "default:vault.update"))
POLICIES = ("on", "off", "missing", "malformed")
ORIGINS = ("desk", "phone-new-turn", "phone-steer")
LOCAL_NOON = datetime(2030, 1, 8, 12).timestamp()


class Act(Base):
    def setUp(self):
        super().setUp()
        self.token.write_text("fake-token")
        self.configure("on")

    def configure(self, setting):
        self.policy_file.unlink(missing_ok=True)
        if setting == "malformed":
            policy.set_auto(self.policy_file, False)
            self.policy_file.write_text('{"version":1,"rules":[],"auto":"yes"}')
        elif setting != "missing":
            policy.set_auto(self.policy_file, setting == "on")

    def chat_with(self, *steps, calendar=None):
        self.model, self.work = Model(*steps), FakeWork()
        return self.service(self.model, approvals=self.broker, work=self.work,
                            calendar=calendar or self.calendar)

    def calendar(self, approve=None):
        return CalendarTools(request=self.cal, now=lambda: NOW, approve=approve)

    def rows(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def expected(self, tainted, setting, origin, flagged, level, why):
        if setting == "malformed":
            return "card", "L2", "malformed-file"
        if tainted:
            return "card", "L2", "hard:tainted"
        if level == "L2":
            return "card", level, why
        if setting != "on":
            return "card", level, "auto-off"
        if origin != "desk":
            return "card", level, "phone"
        if flagged:
            return "card", level, "flagged"
        return "auto", level, why

    def grid_args(self, name, args, number, flagged):
        if not name.startswith("vault_"):
            return args, None
        entry = dict(ADD, name=f"fixture-{number}")
        if name == "vault_add":
            return dict(entry, body=FLAG) if flagged else entry, None
        if name == "vault_archive" and flagged:
            entry["body"] = FLAG
        original, _ = self.vault().add(**entry)
        if name == "vault_patch":
            return dict(args, name=entry["name"], new="\n" + FLAG if flagged else args["new"]), original
        return dict(args, name=entry["name"]), None

    def send_from(self, svc, origin, calls):
        if origin != "phone-steer":
            self.model.steps.extend((reply(calls=calls), reply("Fixture complete.")))
            svc.send("Try the fixture action.", phone=origin == "phone-new-turn")
            return
        entered, release = Event(), Event()

        def initial(_messages):
            entered.set()
            if not release.wait(5):
                raise TimeoutError
            return reply(calls=[("get_datetime", {})])

        self.model.steps.extend((initial, reply(calls=calls), reply("Fixture complete.")))
        svc.send("Try the fixture action.")
        try:
            self.assertTrue(entered.wait(5))
            self.assertTrue(svc.send("Use the phone input too.", phone=True)["steered"])
        finally:
            release.set()

    def test_table(self):
        svc = self.chat_with()
        print("\nTool | session | auto file | origin | flagged | result | level | why", flush=True)
        number = 0
        for name, args, _, level, why in GRID:
            flags = (False, True) if name.startswith("vault_") else (False,)
            for tainted, setting, origin, flagged in product((False, True), POLICIES, ORIGINS, flags):
                with self.subTest(tool=name, tainted=tainted, setting=setting, origin=origin, flagged=flagged):
                    number += 1
                    self.configure(setting)
                    self.log.unlink(missing_ok=True)
                    svc.new_session()
                    gated_args, original = self.grid_args(name, args, number, flagged)
                    calls = [("index_search", {})] if tainted else []
                    calls.append((name, gated_args))
                    expected = self.expected(tainted, setting, origin, flagged, level, why)
                    previous_writes = len(self.cal.writes()) + len(self.work.t3)

                    def seed_revision(*preview_args):
                        if original is not None:
                            # This fixture supplies a revision without returning outside text.
                            preview_args[0]._reads[original.location] = original.revision
                        return vault_preview(*preview_args)

                    vault_preview = fastpath._vault_preview
                    with patch.object(fastpath, "_vault_preview", side_effect=seed_revision):
                        self.send_from(svc, origin, calls)
                        deadline = time.monotonic() + 5
                        while not (pending := self.broker.pending()) and svc.snapshot()["state"] == "running":
                            self.assertLess(time.monotonic(), deadline, "No tool decision")
                            time.sleep(0.01)
                        if pending:
                            (item,) = pending
                            actual = "card", item["level"], item["why"]
                            writes_before_answer = len(self.cal.writes()) + len(self.work.t3)
                            self.broker.answer(item["id"], item["digest"], "deny")
                        self.assertEqual(self.wait_idle(svc), "idle")
                    if pending:
                        self.assertEqual(item["tool"], name)
                        self.assertEqual(writes_before_answer, previous_writes)
                        if flagged:
                            self.assertIn({"label": "Scanner flags", "value": "imperative"}, item["card"]["rows"])
                        self.assertEqual(self.states(None), ["pending", "denied"])
                    else:
                        self.assertEqual(len(self.rows()), 1, self.results()[-1])
                        (row,) = self.rows()
                        actual = row["state"], row["level"], row["why"]
                        self.assertEqual(row["decided_by"], "policy")
                        self.assertEqual(row["tool"], name)
                        self.assertFalse(self.results()[-1].startswith(("Error", "Refused", "The user declined")))
                        self.assertEqual(svc.snapshot()["approvals"], 0)
                    self.assertIs(svc.snapshot()["outside_text"], tainted)
                    if origin == "phone-steer":
                        self.assertIn("Use the phone input too.", json.dumps(self.model.seen[-1]))
                    print(" | ".join((name, "tainted" if tainted else "clean", setting,
                                      origin, str(flagged), *actual)), flush=True)
                    self.assertEqual(actual, expected)
        self.assertEqual(number, 264)

    def test_durable_row_precedes_write_and_grant_is_single_use(self):
        svc = self.chat_with(reply(calls=[("work_send", SEND), ("work_send", SEND)]), reply("Done."))
        invoked, retries = [], []
        send = self.work.send

        def inspect(thread_id, message, *, guard):
            rows = self.rows()
            invoked.append((rows[-1], thread_id, message, len(rows), self.broker.pending()))
            if len(invoked) == len(retries) + 1:
                retries.append(fastpath.work_send(**SEND))
            return send(thread_id, message, guard=guard)

        with patch.object(self.work, "send", side_effect=inspect):
            svc.send("Send both fixture messages.")
            self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual([item[3] for item in invoked], [1, 2])
        self.assertEqual(len({item[0]["id"] for item in invoked}), 2)
        for row, thread, message, _, pending in invoked:
            expected = approvals.digest(row["id"], svc.snapshot()["session_id"], "work_send",
                                        dict(thread_id=thread, message=message),
                                        fastpath._work_preview(self.work, "work_send", SEND))
            self.assertEqual((row["state"], row["decided_by"], row["digest"], pending),
                             ("auto", "policy", expected, []))
        self.assertEqual(retries, ["Error: Sending to a coding thread requires approval."] * 2)
        self.assertEqual(len(self.work.t3), 2)

    def test_changed_grant_arguments_are_refused(self):
        args = {key: value for key, value in START.items() if key != "worktree"}
        svc = self.chat_with(reply(calls=[("work_start", args)]), reply("Done."))
        take = fastpath._take_grant

        def change(tool, args):
            # An omitted default is bound by the grant even though it is absent from the digest input.
            return take(tool, dict(args, worktree=not args["worktree"]))

        with patch.object(fastpath, "_take_grant", side_effect=change):
            svc.send("Start the fixture thread.")
            self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(self.results()[-1], "Error: Starting a coding thread requires approval.")
        self.assertEqual(self.work.t3, [])
        self.assertEqual(self.states(None), ["auto"])

    def test_wrong_digest_does_not_grant_the_write(self):
        svc = self.chat_with(reply(calls=[("work_send", SEND)]), reply("Done."))
        auto = self.broker.auto

        def invalid(*args, **kwargs):
            approval_id, _ = auto(*args, **kwargs)
            return approval_id, "0" * 64

        with patch.object(self.broker, "auto", side_effect=invalid):
            svc.send("Send the fixture message.")
            self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(self.results()[-1], "Error: Sending to a coding thread requires approval.")
        self.assertEqual(self.work.t3, [])

    def test_log_failure_and_absent_log_refuse_without_a_card(self):
        for failure in ("unwritable", "missing-log"):
            with self.subTest(failure=failure):
                if failure == "missing-log":
                    self.broker = approvals.ApprovalBroker()
                    self.addCleanup(self.broker.shutdown)
                svc = self.chat_with(reply(calls=[("vault_add", ADD)]), reply("Done."))
                write = patch.object(self.broker, "_write_row", side_effect=OSError("Fixture log write failed."))
                with write if failure == "unwritable" else nullcontext():
                    svc.send("Save the note.")
                    self.assertEqual(self.wait_idle(svc), "idle")
                self.assertTrue(self.results()[-1].startswith("Refused:"))
                self.assertEqual((self.notes(), self.states(None), self.broker.pending()), ([], [], []))
                self.assertNotIn("auto", self.kinds(svc))

    def test_calendar_request_must_match_preview(self):
        def factory(approve=None):
            if approve is None:
                return self.calendar()

            def changed(action, details):
                return approve(action, dict(details, body=dict(details["body"], summary="Changed fixture")))

            return self.calendar(approve=changed)

        svc = self.chat_with(reply(calls=[("calendar_create", CREATE)]), reply("Done."), calendar=factory)
        svc.send("Schedule the fixture event.")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertTrue(self.results()[-1].startswith("Error:"))
        self.assertEqual((self.cal.writes(), self.states(None)), ([], ["auto"]))

    def test_vault_revision_must_match_preview(self):
        store = self.vault()
        original, _ = store.add(**ADD)
        svc = self.chat_with(reply(calls=[("vault_patch", PATCH)]), reply("Done."))
        preview, auto = fastpath._vault_preview, self.broker.auto

        def seeded(memory, name, args):
            memory._reads[original.location] = original.revision
            return preview(memory, name, args)

        def replace_before_write(*args, **kwargs):
            receipt = auto(*args, **kwargs)
            store.archive(original.location.ref)
            store.add(**ADD)
            return receipt

        with (patch.object(fastpath, "_vault_preview", side_effect=seeded),
              patch.object(self.broker, "auto", side_effect=replace_before_write)):
            svc.send("Change the fixture note.")
            self.assertEqual(self.wait_idle(svc), "idle")
        self.assertIn("changed", self.results()[-1])
        self.assertEqual(store.read(ADD["name"]).body, original.body)
        self.assertEqual(self.states(None), ["auto"])

    def test_turn_and_day_caps_across_restart(self):
        svc = self.chat_with()
        six = [("work_send", SEND)] * 6
        self.assertEqual((fastpath.AUTO_PER_TURN, approvals.AUTO_PER_DAY), (6, 30))
        # Pin both limits by exercising the first action over each.
        before_midnight = datetime(2030, 1, 8, 23, 59).timestamp()
        with patch.object(approvals.time, "time", return_value=before_midnight):
            for number in range(5):
                svc.new_session()
                self.model.steps.extend((reply(calls=six + ([six[0]] if number == 0 else [])), reply("Done.")))
                svc.send("Send the fixture messages.")
                if number == 0:
                    item = self.next_card()
                    self.assertEqual((item["why"], item["level"], len(self.work.t3)), ("cap", "L1", 6))
                    self.broker.answer(item["id"], item["digest"], "approve")
                self.assertEqual(self.wait_idle(svc), "idle")
                self.assertEqual(self.states(None).count("auto"), (number + 1) * 6)
            self.assertEqual(len(self.work.t3), 31)
            self.assertTrue(svc.close())
            self.broker.shutdown()
            self.broker = approvals.ApprovalBroker(log=self.log)
            self.addCleanup(self.broker.shutdown)
            svc = self.chat_with(reply(calls=[six[0]]), reply("Done."))
            svc.send("Send one more fixture message.")
            item = self.next_card()
            self.assertEqual((item["why"], item["level"], self.work.t3), ("cap", "L1", []))
            self.broker.answer(item["id"], item["digest"], "deny")
            self.assertEqual(self.wait_idle(svc), "idle")
            self.assertEqual(self.states(None).count("auto"), 30)
        with patch.object(approvals.time, "time", return_value=before_midnight + 120):
            self.model.steps.extend((reply(calls=[six[0]]), reply("Done.")))
            svc.send("Send the new day's fixture message.")
            self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(self.states(None).count("auto"), 31)
        self.assertEqual(len(self.work.t3), 1)

    def test_brokers_share_the_day_cap_when_they_race(self):
        other = approvals.ApprovalBroker(log=self.log)
        self.addCleanup(other.shutdown)
        with patch.object(approvals.time, "time", return_value=LOCAL_NOON):
            def authorize(broker):
                return broker.auto("work_send", SEND, session_id="fixture", verdict={"level": "L1", "why": "fixture"})

            with ThreadPoolExecutor(max_workers=2) as pool:
                receipts = list(pool.map(authorize, [self.broker, other] * 17))
        self.assertEqual(sum(receipt is not None for receipt in receipts), 30)
        self.assertEqual(self.states(None), ["auto"] * 30)

    def test_stop_after_tool_start_wins(self):
        svc = self.chat_with(reply(calls=[("vault_add", ADD)]), reply("Done."))
        emit = svc._on_event

        def stop_at_dispatch(kind, **fields):
            emit(kind, **fields)
            if kind == "tool_start":
                svc.stop()

        with patch.object(svc, "_on_event", side_effect=stop_at_dispatch):
            svc.send("Save the fixture note.")
            self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual((self.notes(), self.states(None), self.broker.pending()), ([], [], []))
        self.assertIn("stopped", self.kinds(svc))

    def test_l3_refuses_and_a_lower_file_rule_cannot_automate_deletion(self):
        policy.set_rule(self.policy_file, "vault.update", "L3")
        policy.set_rule(self.policy_file, "vault.archive", "L0")
        self.vault().add(**ADD)
        svc = self.chat_with(reply(calls=[("vault_add", dict(ADD, name="second-tea")),
                                          ("vault_archive", {"name": ADD["name"]})]), reply("Done."))
        svc.send("Try both fixture actions.")
        item = self.next_card()
        self.assertEqual((item["tool"], item["level"], item["why"]), ("vault_archive", "L2", "hard:delete"))
        self.broker.answer(item["id"], item["digest"], "deny")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertTrue(self.results()[0].startswith("Refused: Policy blocks this action."))
        self.assertEqual(self.states(None), ["pending", "denied"])
        self.assertEqual(self.notes(), ["facts/favourite-tea.md"])

    def test_auto_requires_clean_session_and_owner_request_even_with_a_low_verdict(self):
        def low_verdict(_policy, facts):
            if facts["tainted"]:
                facts["owner_request"] = True
            else:
                facts.pop("owner_request", None)
            return {"level": "L0", "why": "fixture"}, True

        svc = self.chat_with()
        for calls in ([], [("index_search", {})]):
            with self.subTest(tainted=bool(calls)), patch.object(policy.Policy, "explain_with_auto", low_verdict):
                self.model.steps.extend((reply(calls=[*calls, ("vault_add", ADD)]), reply("Done.")))
                svc.send("Try a fixture write.")
                item = self.next_card()
                self.broker.answer(item["id"], item["digest"], "deny")
                self.assertEqual(self.wait_idle(svc), "idle")
                self.assertEqual(self.states(item["id"]), ["pending", "denied"])
        self.assertEqual(self.notes(), [])

    def test_same_step_taint_and_replay_after_restart(self):
        svc = self.chat_with(reply(calls=[("vault_add", ADD)]), reply("Saved."),
                             reply(calls=[("index_search", {}), ("vault_add", dict(ADD, name="second-tea"))]),
                             reply("Done."))
        svc.send("Save the fixture note.")
        self.assertEqual(self.wait_idle(svc), "idle")
        (event,) = [row for row in svc.events(0)["events"] if row["kind"] == "auto"]
        self.assertEqual({key: event[key] for key in ("tool", "title", "target", "level", "why")},
                         {"tool": "vault_add", "title": "Add a vault entry", "target": "facts/favourite-tea",
                          "level": "L0", "why": "default:vault.update"})
        self.assertIn(event, svc.snapshot()["replay"]["events"])
        self.assertNotIn("saved", svc.snapshot()["replay"])
        svc.send("Read and save another fixture note.")
        item = self.next_card()
        self.assertEqual((item["level"], item["why"]), ("L2", "hard:tainted"))
        self.assertTrue(svc.snapshot()["outside_text"])
        self.broker.answer(item["id"], item["digest"], "deny")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(self.notes(), ["facts/favourite-tea.md"])
        self.assertTrue(svc.close())
        reloaded = self.chat_with()
        snap = reloaded.snapshot()
        self.assertEqual(snap["replay"].get("saved"), [dict(event, after=0)])
        self.assertEqual(snap["history"][0]["text"], "Save the fixture note.")
        self.assertTrue(snap["outside_text"])
        reloaded.new_session()
        self.assertFalse(reloaded.snapshot()["outside_text"])
        self.assertNotIn("saved", reloaded.snapshot()["replay"])

    def test_a_queued_phone_message_marks_only_its_turn(self):
        started, release = Event(), Event()
        action = reply(calls=[("work_send", SEND)])

        def first(_messages):
            started.set()
            if not release.wait(5):
                raise TimeoutError
            return action

        svc = self.chat_with(first, reply("Desk done."), action, reply("Phone done."),
                             action, reply("Desk done again."))
        svc.send("Send the desk fixture.")
        try:
            self.assertTrue(started.wait(5))
            self.assertFalse(svc.send("Send the phone fixture.", phone=True, steer=False)["steered"])
        finally:
            release.set()
        item = self.next_card()
        self.assertEqual((item["level"], item["why"]), ("L1", "phone"))
        self.assertEqual(self.states(None), ["auto", "pending"])
        self.broker.answer(item["id"], item["digest"], "deny")
        self.assertEqual(self.wait_idle(svc), "idle")
        svc.send("Send another desk fixture.")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(self.states(None), ["auto", "pending", "denied", "auto"])
        self.assertEqual(len(self.work.t3), 2)


if __name__ == "__main__":
    unittest.main()
