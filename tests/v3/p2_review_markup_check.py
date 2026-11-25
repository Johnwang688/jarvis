from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from jarvis.v3.brain_mcp import Brain
from jarvis.v3.index import Index
from jarvis.v3.vault import Vault


class CellMarkup(unittest.TestCase):
    def test_tool_does_not_store_raw_html_in_a_cell(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "index.sqlite"
            vault = Vault(root / "vault")
            with Index(database) as index:
                index.add("gmail", "sent-1", title="Draft", ts=1,
                          messages=[("owner", "I will send Ada the draft. I will send Ada the final copy.", 1)],
                          meta={"from_owner": 1})
            brain = Brain(vault=vault, index=database, profile="consolidate")
            fields = {"who": "Ada", "direction": "owner-owes", "source": "gmail:sent-1"}
            added = brain.call("commitment_upsert", {
                **fields, "what": "Send the draft", "evidence": "I will send Ada the draft.",
            })
            self.assertFalse(added["isError"], added)

            path = vault.root / "commitments.md"
            before = path.read_bytes()
            result = brain.call("commitment_upsert", {
                **fields, "what": "Send the final copy <!-- editorial note -->",
                "evidence": "I will send Ada the final copy.",
            })
            after = path.read_bytes()
            self.assertNotIn(b"<!-- editorial note -->", after)
            if result["isError"]:
                self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
