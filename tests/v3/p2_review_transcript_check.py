from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from jarvis.v3.brain_mcp import Brain
from jarvis.v3.commitments import load
from jarvis.v3.index import Index
from jarvis.v3.vault import Vault


class TranscriptEvidence(unittest.TestCase):
    def test_owner_promise_and_close_require_a_real_owner_turn(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "index.sqlite"
            vault = Vault(root / "vault")
            with Index(database) as index:
                index.add("claude", "chat-1", title="Lab notes", ts=2, messages=[
                    ("user", "I will send the draft to Ada.", 1),
                    ("assistant", "The draft has already been sent to Ada.", 2),
                ])
                index.add("claude", "reply-only", title="Lab notes", ts=3, messages=[
                    ("assistant", "The final copy has already been sent to Ada.", 3),
                ])
            brain = Brain(vault=vault, index=database, profile="consolidate")
            fields = {"who": "Ada", "what": "Send the draft", "direction": "owner-owes"}
            citation = "transcript:claude/chat-1"
            added = brain.call("commitment_upsert", {
                **fields, "source": citation + "#1", "evidence": "I will send the draft to Ada.",
            })
            self.assertFalse(added["isError"], added)

            path = vault.root / "commitments.md"
            before = path.read_bytes()
            commitment_id = load(vault)[0].id
            for source, evidence in (
                (citation + "#2", "The draft has already been sent to Ada."),
                (citation + "#99", "I will send the draft to Ada."),
                ("transcript:claude/reply-only", "The final copy has already been sent to Ada."),
            ):
                for tool, arguments in (
                    ("commitment_upsert", {
                        **fields, "what": "Send the final copy", "source": source, "evidence": evidence,
                    }),
                    ("commitment_close", {
                        "id": commitment_id, "state": "done", "source": source, "evidence": evidence,
                    }),
                ):
                    with self.subTest(source=source, tool=tool):
                        result = brain.call(tool, arguments)
                        after = path.read_bytes()
                        if after != before:
                            path.write_bytes(before)
                        self.assertTrue(result["isError"], result)
                        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
