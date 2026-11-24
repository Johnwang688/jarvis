"""A body patch and an archive preserve untouched frontmatter bytes."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from jarvis.v3.tools.memory import MemoryTools
from jarvis.v3.vault import Vault


class FrontmatterRoundTrip(unittest.TestCase):
    def test_patch_then_archive_preserves_untouched_lines(self):
        root = Path(self.enterContext(TemporaryDirectory())) / "vault"
        note = root / "facts" / "tea.md"
        note.parent.mkdir(parents=True, mode=0o700)
        root.chmod(0o700)
        original = (
            b'---\r\nname: tea\r\nkind: fact\r\ndescription: "Tea notes"\r\n'
            b"source: 'owner'\nupdated: 2026-01-01\r\nstatus: active\r\n"
            b'\r\ntags:\r\n  - drinks\r\n---\nMint tea. See [[Kettle]].\r\n'
        )
        note.write_bytes(original)
        tools = MemoryTools(Vault(root))

        self.assertIn("Mint tea.", tools.memory_read("facts/tea"))
        self.assertEqual(tools.memory_patch("facts/tea", "Mint", "Black"), "Patched facts/tea.")
        patched = note.read_bytes()
        self.assertEqual(tools.memory_archive("facts/tea"), "Archived facts/tea.")
        self.assertFalse(note.exists())
        archived, = (note.parent / ".archive").glob("*.md")

        expected = original.replace(b"Mint tea.", b"Black tea.").replace(
            b"updated: 2026-01-01", b"updated: " + date.today().isoformat().encode("ascii")
        )
        self.assertEqual(
            (patched, archived.read_bytes()),
            (expected, expected.replace(b"status: active", b"status: archived")),
        )


if __name__ == "__main__":
    unittest.main()
