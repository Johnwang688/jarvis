"""Race, failure and rollback checks for the v3 chat approval boundary."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import a6c1_acceptance_check as fixtures
from jarvis.v3 import approvals


class BrokerChecks(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.log = self.root / "approvals.jsonl"
        self.clock = fixtures.Clock()
        self.broker = approvals.ApprovalBroker(log=self.log, clock=self.clock, timeout_s=10)
        self.addCleanup(self.broker.shutdown)

    def ask(self, turn):
        answers = []
        thread = threading.Thread(target=lambda: answers.append(self.broker.ask(
            "vault_add", {}, fixtures.CARD, session_id=fixtures.SID, turn_id=turn,
            verdict=fixtures.VERDICT,
        )), daemon=True)
        thread.start()
        return thread, answers

    def pending(self, count):
        deadline = time.monotonic() + 5
        while True:
            pending = self.broker.pending()
            if len(pending) == count:
                return pending
            self.assertLess(time.monotonic(), deadline, "pending count did not change")
            time.sleep(0.01)

    def finish(self, waiter):
        thread, answers = waiter
        thread.join(2)
        self.assertFalse(thread.is_alive(), "approval waiter did not finish")
        self.assertEqual(len(answers), 1)
        return answers[0]

    def rows(self):
        return [json.loads(row) for row in self.log.read_text().splitlines()]

    def test_answer_racing_expiry_has_exactly_one_final_state(self):
        for attempt in range(12):
            waiter = self.ask(f"turn-{attempt}")
            (item,) = self.pending(1)
            ready = threading.Barrier(2, timeout=2)
            outcome = []

            def answer():
                ready.wait()
                try:
                    outcome.append(self.broker.answer(item["id"], item["digest"], "approve"))
                except approvals.ApprovalConflict:
                    outcome.append("expired")

            thread = threading.Thread(target=answer, daemon=True)
            thread.start()
            ready.wait()
            self.clock.now += 10
            self.assertEqual(self.broker.pending(), [])
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(outcome), 1)
            self.assertIs(self.finish(waiter), outcome[0] == "approved")
            self.assertEqual([row["state"] for row in self.rows() if row["id"] == item["id"]],
                             ["pending", outcome[0]])
            with self.assertRaises(approvals.ApprovalConflict):
                self.broker.answer(item["id"], item["digest"], "deny")

    def test_cancel_releases_only_the_named_turn(self):
        first = self.ask("one")
        (item,) = self.pending(1)
        second = self.ask("two")
        _, other = self.pending(2)
        self.broker.cancel("one")
        self.assertIs(self.finish(first), False)
        self.assertEqual([row["id"] for row in self.pending(1)], [other["id"]])
        self.assertTrue(second[0].is_alive())
        self.assertEqual(self.broker.answer(other["id"], other["digest"], "approve"), "approved")
        self.assertIs(self.finish(second), True)
        self.assertEqual([(row["id"], row["state"]) for row in self.rows() if row["state"] != "pending"],
                         [(item["id"], "cancelled"), (other["id"], "approved")])

    def test_log_symlink_and_directory_deny(self):
        target = self.root / "other.jsonl"
        target.write_text("untouched")
        self.log.symlink_to(target)
        self.assertIs(self.finish(self.ask("symlink")), False)
        self.assertEqual(target.read_text(), "untouched")
        self.log.unlink()
        self.log.mkdir(mode=0o700)
        self.assertIs(self.finish(self.ask("directory")), False)
        self.assertEqual(self.broker.pending(), [])

    def test_a_log_write_failure_denies_before_or_after_listing(self):
        with patch.object(approvals.os, "write", side_effect=OSError("fixture write failed")):
            self.assertIs(self.finish(self.ask("pending failure")), False)
        self.assertEqual(self.broker.pending(), [])
        self.assertEqual(self.rows(), [])

        waiter = self.ask("final failure")
        (item,) = self.pending(1)
        with patch.object(approvals.os, "write", side_effect=OSError("fixture write failed")):
            with self.assertRaises(approvals.ApprovalConflict):
                self.broker.answer(item["id"], item["digest"], "approve")
        self.assertIs(self.finish(waiter), False)
        self.assertEqual(self.broker.pending(), [])
        self.assertEqual([row["state"] for row in self.rows()], ["pending"])


class ChatChecks(fixtures.Base):
    def test_archive_card_shows_the_replacement(self):
        vault = self.vault()
        vault.add(**fixtures.ADD)
        vault.add(**dict(fixtures.ADD, name="preferred-tea"))
        svc = self.talk(fixtures.reply(calls=[("vault_archive", {
            "name": "favourite-tea", "replaced_by": "preferred-tea",
        })]), fixtures.reply("Archived."))
        svc.send("Archive the old tea note and point to the new one.")
        item = self.next_card()
        rows = {row["label"]: row["value"] for row in item["card"]["rows"]}
        self.assertEqual((rows["Entry"], rows["Replacement"]), ("facts/favourite-tea", "facts/preferred-tea"))
        self.broker.answer(item["id"], item["digest"], "approve")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertIn("Archived", self.results()[-1])

    def test_calendar_create_reminders_and_grant_is_single_use(self):
        from jarvis.v3.tools.calendar import CalendarTools

        self.token.write_text("fake-token")
        calls = []
        event = dict(fixtures.CREATE, reminders=[5, 35])
        self.model = fixtures.Model(fixtures.reply(calls=[("calendar_create", event)]), fixtures.reply("Created."))

        def calendar(approve=None):
            client = CalendarTools(request=self.cal, now=lambda: fixtures.NOW, approve=approve)
            if approve is not None:
                calls.append(client)
            return client

        svc = self.service(self.model, approvals=self.broker, calendar=calendar)
        svc.send("Book a session with two reminders.")
        item = self.next_card()
        rows = {row["label"]: row["value"] for row in item["card"]["rows"]}
        self.assertIn("5, 35", rows["Reminders"])
        self.assertNotIn(fixtures.FAKE_TOKEN, rows["Description"])
        self.broker.answer(item["id"], item["digest"], "approve")
        self.assertEqual(self.wait_idle(svc), "idle")
        (client,) = calls
        self.assertEqual(client.calendar_create(**event), "Error: Calendar write requires approval.")
        (post,) = [row for row in self.cal.calls if row[0] == "POST"]
        self.assertEqual(post[3]["reminders"]["overrides"],
                         [{"method": "popup", "minutes": 5}, {"method": "popup", "minutes": 35}])

    def test_unowned_calendar_event_is_refused_before_building_a_card(self):
        from jarvis.v3 import fastpath
        from jarvis.v3.tools.calendar import CalendarTools

        self.token.write_text("fake-token")

        def request(*args, **kwargs):
            status, event = self.cal(*args, **kwargs)
            return status, {**event, "extendedProperties": {}}

        self.model = fixtures.Model(fixtures.reply(calls=[("calendar_delete", {"event_id": "e1"})]),
                                    fixtures.reply("Kept."))
        svc = self.service(self.model, approvals=self.broker,
                           calendar=lambda approve=None: CalendarTools(request=request, approve=approve))
        with patch.object(fastpath, "build_card", wraps=fastpath.build_card) as card:
            svc.send("Delete the event.")
            self.assertEqual(self.wait_idle(svc), "idle")
            card.assert_not_called()
        self.assertEqual(self.broker.pending(), [])
        self.assertEqual(self.states(None), [])
        self.assertEqual(self.cal.writes(), [])
        self.assertTrue(self.results()[-1].startswith("Refused:"))

    def test_calendar_policy_can_match_a_jarvis_owned_event(self):
        from jarvis.v3.policy import set_rule

        self.token.write_text("fake-token")
        set_rule(self.policy_file, "calendar.event_delete", "L3",
                 when={"owner_event": False, "attendees": 0})
        svc = self.talk(fixtures.reply(calls=[("calendar_delete", {"event_id": "e1"})]),
                        fixtures.reply("Kept."))
        svc.send("Delete the event.")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertTrue(self.results()[-1].startswith("Refused:"))
        self.assertEqual(self.states(None), [])
        self.assertEqual(self.cal.writes(), [])

    def test_blocked_vault_content_never_reaches_the_card_builder(self):
        from jarvis.v3 import fastpath

        event = dict(fixtures.ADD, body="token " + fixtures.FAKE_TOKEN)
        svc = self.talk(fixtures.reply(calls=[("vault_add", event)]), fixtures.reply("Refused."))
        with patch.object(fastpath, "build_card", wraps=fastpath.build_card) as card:
            svc.send("Save the note.")
            self.assertEqual(self.wait_idle(svc), "idle")
            card.assert_not_called()
        self.assertEqual(self.states(None), [])

    def test_card_shows_a_4000_character_body(self):
        body = ("The kettle is blue. " * 250)[:4000]
        svc = self.talk(fixtures.reply(calls=[("vault_add", dict(fixtures.ADD, body=body))]),
                        fixtures.reply("Kept."))
        svc.send("Save the note.")
        item = self.next_card()
        rows = {row["label"]: row["value"] for row in item["card"]["rows"]}
        self.assertEqual(len(body), 4000)
        self.assertEqual(rows["Body"], body)
        self.assertEqual(item["card"]["title"], "Add a vault entry")
        self.assertEqual(list(rows)[:2], ["Entry", "Revision"])
        self.assertEqual(rows["Entry"], "facts/" + fixtures.ADD["name"])
        self.assertEqual(rows["Revision"], "absent")
        self.broker.answer(item["id"], item["digest"], "deny")
        self.assertEqual(self.wait_idle(svc), "idle")

    def test_add_refuses_if_a_file_appeared_at_the_approved_empty_destination(self):
        vault = self.vault()
        svc = self.talk(fixtures.reply(calls=[("vault_add", fixtures.ADD)]), fixtures.reply("Kept."))
        svc.send("Save the tea note.")
        item = self.next_card()
        entry, _ = vault.add(**dict(fixtures.ADD, body="The other tea note."))
        # An interrupted archive leaves a revision at the destination even
        # though ordinary reads no longer list it as active.
        with patch("jarvis.v3.vault.os.rename", side_effect=OSError("interrupted")):
            vault.archive(entry.location.ref)
        target = vault.root / entry.location.folder / entry.location.filename
        before = target.read_bytes()
        self.broker.answer(item["id"], item["digest"], "approve")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(target.read_bytes(), before)
        self.assertIn("changed after approval", self.results()[-1])

    def test_patch_card_shows_both_full_texts_and_pins_the_read_revision(self):
        old, new = ("The tea shelf is here. " * 200)[:4000], ("The mugs are here. " * 220)[:4000]
        vault = self.vault()
        entry, _ = vault.add(**dict(fixtures.ADD, body=old))
        name = entry.location.ref
        svc = self.talk(fixtures.reply(calls=[("vault_read", {"name": name}),
                                             ("vault_patch", {"name": name, "old": old, "new": new})]),
                        fixtures.reply("Done."))
        svc.send("Update the tea note.")
        item = self.next_card()
        rows = {row["label"]: row["value"] for row in item["card"]["rows"]}
        self.assertEqual(list(rows)[:2], ["Entry", "Revision"])
        self.assertEqual((rows["Revision"], rows["Old text"], rows["New text"]),
                         (entry.revision.hex(), old, new))
        edited, _ = vault.patch(name, old, "The owner edited the note.", {entry.location: entry.revision})
        self.broker.answer(item["id"], item["digest"], "approve")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(vault.read(name).revision, edited.revision)
        self.assertIn("changed after approval", self.results()[-1])

    def test_oversized_vault_writes_are_refused_before_a_card(self):
        self.vault().add(**fixtures.ADD)
        oversized = "A" * 4001
        svc = self.talk(fixtures.reply(calls=[
            ("vault_add", dict(fixtures.ADD, name="another-note", body=oversized)),
            ("vault_read", {"name": fixtures.ADD["name"]}),
            ("vault_patch", {"name": fixtures.ADD["name"], "old": oversized, "new": ""}),
            ("vault_patch", {"name": fixtures.ADD["name"], "old": "green", "new": oversized}),
            ("vault_patch", {"name": fixtures.ADD["name"], "old": "green", "new": oversized[:-1]}),
        ]), fixtures.reply("Done."))
        svc.send("Try writing oversized text.")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(self.states(None), [])
        errors = [result for result in self.results() if result.startswith("Refused:")]
        self.assertEqual(len(errors), 4)
        self.assertTrue(all("4000 characters" in error for error in errors))

    def test_broker_boolean_without_a_digest_does_not_grant_a_write(self):
        svc = self.talk(fixtures.reply(calls=[("vault_add", fixtures.ADD)]), fixtures.reply("Done."))
        with patch.object(self.broker, "ask", return_value=True):
            svc.send("Save the tea note.")
            self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(self.notes(), [])
        self.assertEqual(self.states(None), [])

    def test_stop_after_an_approval_prevents_the_write(self):
        svc = self.talk(fixtures.reply(calls=[("vault_add", fixtures.ADD)]), fixtures.reply("Never reached."))
        ask = self.broker.ask

        def stop_after_approval(*args, **kwargs):
            allowed = ask(*args, **kwargs)
            if allowed:
                svc.stop()
            return allowed

        with patch.object(self.broker, "ask", side_effect=stop_after_approval):
            svc.send("Save the note.")
            item = self.next_card()
            self.broker.answer(item["id"], item["digest"], "approve")
            self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(self.notes(), [])
        self.assertEqual(self.states(item["id"]), ["pending", "approved"])
        self.assertIn("stopped", self.kinds(svc))

    def test_a_brokerless_turn_registers_no_gated_tools(self):
        code = """
from pathlib import Path
import sys
from a6b1_acceptance_check import Model, reply
from jarvis import tools
from jarvis.v3.chat import ChatService, GATED_TOOLS

chat = ChatService(Path(sys.argv[1]), completion=Model(reply("Done.")))
assert GATED_TOOLS.isdisjoint(tools.REGISTRY)
chat.send("Hello.")
chat._worker.join(5)
assert chat.snapshot()["state"] == "idle"
assert GATED_TOOLS.isdisjoint(tools.REGISTRY)
assert "jarvis.v3.tools.calendar" not in sys.modules
assert chat.close()
"""
        result = subprocess.run([sys.executable, "-c", code, str(self.v3 / "chats")],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
