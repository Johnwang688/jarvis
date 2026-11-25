import unittest

from jarvis.v3 import commitments


class CellNewline(unittest.TestCase):
    def test_bare_cr_inside_owner_edited_cell_is_malformed(self):
        fields = {"who": "Ada", "what": "Send the draft", "direction": "owner-owes",
                  "source": "gmail:sent-1", "due": ""}
        _, row, _ = commitments.upsert("", **fields)
        block = commitments.render([row], newline="\r\n")
        edited = block.replace("Send the draft |", "Send the draft\r |")
        with self.subTest(operation="parse"), self.assertRaises(commitments.CommitmentError):
            commitments.parse(edited)
        with self.subTest(operation="upsert"), self.assertRaises(commitments.CommitmentError):
            commitments.upsert(edited, **{**fields, "what": "Send the final copy"})


if __name__ == "__main__":
    unittest.main()
