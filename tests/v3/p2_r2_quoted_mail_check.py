from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from jarvis.v3.brain_mcp import Brain
from jarvis.v3.commitments import load
from jarvis.v3.index import Index
from jarvis.v3.vault import Vault


class QuotedMail(unittest.TestCase):
    def test_sent_mail_does_not_make_quoted_words_the_owners(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "index.sqlite"
            vault = Vault(root / "vault")
            with Index(database) as index:
                for ref, body in (
                    ("promise", "I will return the keys to Ada."),
                    ("done", "I returned the keys to Ada today."),
                    ("reply", "Thanks for the update.\n\n"
                              "On Monday, Ada wrote:\n"
                              "> I will send the invoice.\n"
                              "> I returned the keys today."),
                    ("forward", "For reference.\n\n"
                                "---------- Forwarded message ---------\n"
                                "From: Ada <ada@example.invalid>\n\n"
                                "I will send the invoice.\n"
                                "I returned the keys today."),
                ):
                    index.add("gmail", ref, title="Project mail", ts=1,
                              messages=[("owner", body, 1)],
                              meta={"from_owner": 1, "labels": ["SENT"]})
            brain = Brain(vault=vault, index=database, profile="consolidate")
            fields = {"who": "Ada", "direction": "owner-owes"}
            added = brain.call("commitment_upsert", {
                **fields, "what": "Return the keys", "source": "gmail:promise",
                "evidence": "I will return the keys to Ada.",
            })
            self.assertFalse(added["isError"], added)
            row, = load(vault)
            path = vault.root / "commitments.md"
            before = path.read_bytes()

            for ref in ("reply", "forward"):
                for tool, arguments in (
                    ("commitment_upsert", {**fields, "what": "Send the invoice"}),
                    ("commitment_close", {"id": row.id, "state": "done"}),
                ):
                    with self.subTest(ref=ref, tool=tool):
                        result = brain.call(tool, {**arguments, "source": f"gmail:{ref}"})
                        after = path.read_bytes()
                        if after != before:
                            path.write_bytes(before)
                        self.assertTrue(result["isError"], result)
                        self.assertEqual(after, before)

            closed = brain.call("commitment_close", {
                "id": row.id, "state": "done", "source": "gmail:done",
                "evidence": "I returned the keys to Ada today.",
            })
            self.assertFalse(closed["isError"], closed)
            self.assertEqual(load(vault)[0].state, "done")


if __name__ == "__main__":
    unittest.main()
