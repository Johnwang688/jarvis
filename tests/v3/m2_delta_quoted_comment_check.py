"""A YAML comment after a quoted description keeps a note readable."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from jarvis.v3.tools.memory import MemoryTools
from jarvis.v3.vault import Vault


class QuotedDescription(unittest.TestCase):
    def test_quoted_description_with_inline_comment(self):
        root = Path(self.enterContext(TemporaryDirectory())) / "vault"
        tools = MemoryTools(Vault(root))
        self.assertEqual(
            tools.memory_add("facts", "tea", "Tea notes", "Mint tea.", "owner"),
            "Added facts/tea.",
        )
        note = root / "facts" / "tea.md"
        note.write_bytes(note.read_bytes().replace(
            b"description: Tea notes\n", b'description: "Tea notes" # checked in the kitchen\n'
        ))

        result = tools.memory_read("facts/tea")
        self.assertIn("Mint tea.", result)
        self.assertIn("Mint tea.", tools.memory_search("Mint tea"))


if __name__ == "__main__":
    unittest.main()
