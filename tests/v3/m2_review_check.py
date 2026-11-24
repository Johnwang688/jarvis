"""M2 regressions for editor saves, storage failures and legacy imports."""

import errno
import os
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jarvis.v3 import vault
from jarvis.v3.tools.memory import MemoryTools


class M2Review(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.root = self.tmp / "vault"
        self.store = vault.Vault(self.root)
        self.tools = MemoryTools(self.store)
        self.tea = self.root / "facts" / "tea.md"

    def add(self, name="tea", description="A drink", body="Mint tea."):
        self.assertEqual(self.tools.memory_add("facts", name, description, body, "owner"),
                         f"Added facts/{name}.")

    def test_editor_save_during_patch_validation_survives(self):
        self.add()
        self.assertIn("Mint tea.", self.tools.memory_read("tea"))
        edited = self.tea.read_bytes() + b"\nOwner note about the kettle.\n"
        scan = vault.content_scan.scan

        def save_in_editor(text, *, purpose):
            if "Black tea." in text:
                replacement = self.tea.with_suffix(".swp")
                replacement.write_bytes(edited)
                replacement.replace(self.tea)
            return scan(text, purpose=purpose)

        with patch.object(vault.content_scan, "scan", save_in_editor):
            result = self.tools.memory_patch("tea", "Mint", "Black")
        self.assertEqual(self.tea.read_bytes(), edited, result)
        self.assertTrue(result.startswith("Error:"), result)

    def test_full_flag_log_cannot_report_a_failed_patch_that_committed(self):
        self.add()
        self.assertIn("Mint tea.", self.tools.memory_read("tea"))
        original = self.tea.read_bytes()
        fdopen = os.fdopen

        class FullDisk:
            def write(self, _data):
                raise OSError(errno.ENOSPC, "fixture disk is full")

        @contextmanager
        def open_stream(fd, mode, **kwargs):
            if mode == "ab":
                yield FullDisk()
            else:
                with fdopen(fd, mode, **kwargs) as stream:
                    yield stream

        with patch.object(vault.os, "fdopen", open_stream):
            result = self.tools.memory_patch("tea", "Mint tea.", "Always serve mint tea.")
        if result.startswith("Error:"):
            self.assertEqual(self.tea.read_bytes(), original, result)
        else:
            self.assertIn(b"Always serve mint tea.", self.tea.read_bytes())
            self.assertIn('"imperative"', (self.root / ".flags.jsonl").read_text())
            self.assertTrue(any(flag["rule"] == "imperative" for flag in self.tools.flags()))

    def test_obsidian_properties_and_links_survive_a_patch(self):
        self.add()
        edited = self.tea.read_bytes().replace(
            b"status: active\n", b"status: active\n\ntags:\n  - drinks\naliases: [Herbal tea]\n"
        ).replace(b"Mint tea.", b"Mint tea. See [[Kettle]].")
        self.tea.write_bytes(edited.replace(b"\n", b"\r\n"))

        self.assertIn("A drink", self.tools.memory_list("facts"))
        self.assertIn("[[Kettle]]", self.tools.memory_search("Mint tea"))
        self.assertIn("Mint tea.", self.tools.memory_read("facts/tea"))
        self.assertEqual(self.tools.memory_patch("tea", "Mint", "Herbal"), "Patched facts/tea.")
        saved = self.tea.read_text()
        self.assertIn("tags:\n  - drinks", saved)
        self.assertIn("aliases: [Herbal tea]", saved)
        self.assertIn("Herbal tea. See [[Kettle]].", saved)

    def test_renamed_note_is_still_discoverable(self):
        self.add()
        self.tea.rename(self.tea.with_name("mint-tea.md"))
        self.assertIn("A drink", self.tools.memory_list("facts"))
        self.assertIn("Mint tea.", self.tools.memory_search("Mint tea"))

    def test_obsidian_archive_properties_do_not_make_a_visible_row(self):
        self.add()
        self.tea.write_bytes(self.tea.read_bytes().replace(
            b"status: active\n", b"status: archived\ntags: [drinks]\n"
        ))
        self.assertNotIn("facts/tea", self.tools.memory_list("facts"))
        self.assertNotIn("Mint tea", self.tools.memory_search("Mint tea"))

    def test_metadata_is_not_written_as_a_yaml_comment_or_mapping(self):
        for name, description in (("mint", "# Preferred drink"), ("black", "Tea: served cold")):
            with self.subTest(name=name):
                result = self.tools.memory_add("facts", name, description, "A tea note.", "owner")
                path = self.tea.with_name(f"{name}.md")
                if result.startswith("Error:"):
                    self.assertFalse(path.exists(), result)
                else:
                    self.assertNotIn(f"\ndescription: {description}\n", path.read_text())

    def test_unreadable_fact_does_not_free_a_cap_slot(self):
        self.add()
        edited = self.tea.read_bytes() + b"\xff"
        self.tea.write_bytes(edited)
        with patch.object(vault, "FACTS_CAP", 1):
            result = self.tools.memory_add("facts", "coffee", "A drink", "Black coffee.", "owner")
        self.assertTrue(result.startswith("Error:"), result)
        self.assertEqual(list(self.tea.parent.glob("*.md")), [self.tea])
        self.assertEqual(self.tea.read_bytes(), edited)


if __name__ == "__main__":
    unittest.main()
