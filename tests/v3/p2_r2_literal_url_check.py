from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from jarvis.v3 import commitments
from jarvis.v3.brain_mcp import Brain
from jarvis.v3.index import Index
from jarvis.v3.vault import Vault


FIELDS = {"who": "Ada & Lin", "direction": "owed-to-owner", "source": "gmail:mail"}
WHAT = "Review https://[2001:db8::1]/draft_v2"
URL_EVIDENCE = f"I will {WHAT.lower()}."
DRAFT_EVIDENCE = "I will review the draft."
FINAL_EVIDENCE = "I will review the final copy."


class LiteralURL(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        database = root / "index.sqlite"
        self.vault = Vault(root / "vault")
        with Index(database) as index:
            index.add("gmail", "mail", title="Draft review", ts=1,
                      messages=[("sender", f"{URL_EVIDENCE} {DRAFT_EVIDENCE} {FINAL_EVIDENCE}", 1)])
        self.brain = Brain(vault=self.vault, index=database, profile="consolidate")

    def test_literal_ipv6_url_can_be_recorded(self):
        result = self.brain.call("commitment_upsert", {**FIELDS, "what": WHAT, "evidence": URL_EVIDENCE})
        self.assertFalse(result["isError"], result)
        row, = commitments.load(self.vault)
        self.assertEqual(row.what, WHAT)

    def test_crlf_table_with_a_hand_edited_url_remains_writable(self):
        result = self.brain.call("commitment_upsert", {
            **FIELDS, "what": "Review the draft", "evidence": DRAFT_EVIDENCE,
        })
        self.assertFalse(result["isError"], result)
        row, = commitments.load(self.vault)
        path = self.vault.root / "commitments.md"
        front, _, _ = path.read_bytes().partition(commitments.BEGIN.encode())
        first, last = "Notes before the table.\r\n\r\n", "\r\nNotes after the table.\r\n"
        body = first + commitments.render([replace(row, what=WHAT)], newline="\r\n") + last
        path.write_bytes(front.replace(b"\n", b"\r\n") + body.encode())

        result = self.brain.call("commitment_upsert", {
            **FIELDS, "what": "Review the final copy", "evidence": FINAL_EVIDENCE,
        })
        self.assertFalse(result["isError"], result)
        self.assertEqual([item.what for item in commitments.load(self.vault)],
                         [WHAT, "Review the final copy"])
        body = self.vault.read("commitments").body
        self.assertTrue(body.startswith(first))
        self.assertTrue(body.endswith(last))
        self.assertNotIn("\n", body.replace("\r\n", ""))


if __name__ == "__main__":
    unittest.main()
