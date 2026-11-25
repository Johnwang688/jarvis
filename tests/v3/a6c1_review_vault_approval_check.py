"""The vault write must match the revision and content the owner reviewed."""

import unittest
from unittest.mock import patch

from a6c1_acceptance_check import ADD, Base, reply


class VaultApprovalReview(Base):
    def test_vault_write_methods_require_a_broker_decision(self):
        from jarvis.v3 import fastpath

        self.vault().add(**ADD)
        tools = [
            ("vault_read", {"name": ADD["name"]}),
            ("vault_add", dict(ADD, name="another-note")),
            ("vault_patch", {"name": ADD["name"], "old": "green", "new": "oolong"}),
            ("vault_archive", {"name": ADD["name"]}),
        ]
        for label, approver in (("missing", None), ("unrecorded", lambda *_: True)):
            with (
                self.subTest(approver=label),
                patch.object(fastpath, "ChatApprover", return_value=approver),
                patch.object(fastpath.MemoryTools, "memory_add", return_value="fixture") as add,
                patch.object(fastpath.MemoryTools, "memory_patch", return_value="fixture") as update,
                patch.object(fastpath.MemoryTools, "memory_archive", return_value="fixture") as archive,
            ):
                service = self.talk(reply(calls=tools), reply("Done."))
                service.send("Check the write guards.")
                self.wait_idle(service)
                self.assertEqual(self.states(None), [])
                reached = [writer for writer, mock in (("add", add), ("patch", update), ("archive", archive))
                           if mock.called]
                self.assertEqual(reached, [], "No writer should be reached without a broker decision.")

    def test_archive_preserves_a_note_edited_while_approval_is_pending(self):
        vault = self.vault()
        original, _ = vault.add(**ADD)
        target = original.location.ref
        service = self.talk(reply(calls=[("vault_archive", {"name": target})]), reply("Done."))
        service.send("Archive the tea note.")
        card = self.next_card()

        edited, _ = vault.patch(target, "green", "oolong", {original.location: original.revision})
        self.assertNotEqual(edited.revision, original.revision)
        self.broker.answer(card["id"], card["digest"], "approve")
        self.assertEqual(self.wait_idle(service), "idle")

        self.assertIn(target + ".md", self.notes(), "The edited revision must stay active.")
        self.assertEqual(vault.read(target).revision, edited.revision)

    def test_card_shows_the_full_body_of_a_vault_add(self):
        body = ("The tea shelf is next to the mugs.\n" * 50) + "The oolong tin is empty."
        service = self.talk(reply(calls=[("vault_add", dict(ADD, body=body))]), reply("Done."))
        service.send("Save the tea shelf inventory.")
        card = self.next_card()
        shown = "".join(row["value"] for row in card["card"]["rows"] if row["label"].startswith("Body"))

        self.broker.answer(card["id"], card["digest"], "approve")
        self.assertEqual(self.wait_idle(service), "idle")
        stored = self.vault().read(ADD["name"]).body
        self.assertEqual(stored, body)
        self.assertTrue(shown == stored, "The approval card omitted part of the body it authorized.")


if __name__ == "__main__":
    unittest.main()
