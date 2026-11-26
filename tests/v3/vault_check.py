"""Process and on-disk regression checks for the v3 vault."""

import fcntl
import multiprocessing
from pathlib import Path
import re
import signal
import stat
import tempfile
import unittest
from unittest.mock import patch

from jarvis.v3.tools.memory import MemoryTools
from jarvis.v3.vault import Vault


WAIT = 15


def first_writer(root, channel):
    save = Vault._save

    def pause_before_save(*args):
        channel.send("ready to save")
        if not channel.poll(WAIT) or channel.recv() != "save":
            raise TimeoutError
        return save(*args)

    with patch.object(Vault, "_save", staticmethod(pause_before_save)):
        channel.send(MemoryTools(Vault(root)).memory_add(
            "facts", "tea", "The first entry", "Mint tea.", "owner"))


def competing_writer(root, channel):
    flock = fcntl.flock

    def observe_contention(fd, operation):
        try:
            flock(fd, operation | fcntl.LOCK_NB)
        except BlockingIOError:
            channel.send("waiting for writer")
            flock(fd, operation)

    with patch.object(fcntl, "flock", observe_contention):
        channel.send(MemoryTools(Vault(root)).memory_add(
            "facts", "tea", "The competing entry", "Black tea.", "owner"))


def hold_lock(root, channel):
    with Vault(root)._locked(write=True):
        channel.send("holding lock")
        channel.poll(WAIT * 2)


class VaultChecks(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(tmp).resolve() / "vault"
        self.vault = Vault(self.root)
        self.tools = MemoryTools(self.vault)
        self.entry = self.root / "facts" / "tea.md"

    def add_tea(self):
        self.assertEqual(self.tools.memory_add(
            "facts", "tea", "A drink", "Mint tea.", "owner"), "Added facts/tea.")

    def start_process(self, target):
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        self.addCleanup(parent.close)
        process = context.Process(target=target, args=(self.root, child))
        try:
            process.start()
        finally:
            child.close()
        self.addCleanup(self.stop_process, process)
        return process, parent

    @staticmethod
    def stop_process(process):
        if process.is_alive():
            process.kill()
        process.join(WAIT)
        process.close()

    def receive(self, channel):
        self.assertTrue(channel.poll(WAIT), "Timed out waiting for vault process")
        return channel.recv()

    def test_concurrent_writers_cannot_both_add_the_same_name(self):
        first, first_channel = self.start_process(first_writer)
        self.assertEqual(self.receive(first_channel), "ready to save")
        second, second_channel = self.start_process(competing_writer)
        self.assertEqual(self.receive(second_channel), "waiting for writer")
        first_channel.send("save")
        self.assertEqual(self.receive(first_channel), "Added facts/tea.")
        self.assertIn("already exists", self.receive(second_channel))
        for process in (first, second):
            process.join(WAIT)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(self.vault.read("tea").body, "Mint tea.")

    def test_a_killed_process_releases_the_vault_lock(self):
        holder, holder_channel = self.start_process(hold_lock)
        self.assertEqual(self.receive(holder_channel), "holding lock")
        writer, writer_channel = self.start_process(competing_writer)
        self.assertEqual(self.receive(writer_channel), "waiting for writer")
        holder.kill()
        holder.join(WAIT)
        self.assertEqual(holder.exitcode, -signal.SIGKILL)
        self.assertEqual(self.receive(writer_channel), "Added facts/tea.")
        writer.join(WAIT)
        self.assertEqual(writer.exitcode, 0)
        self.assertEqual(self.vault.read("tea").body, "Black tea.")

    def test_crlf_and_hand_edits_preserve_body_and_invalidate_a_read(self):
        self.add_tea()
        original = self.entry.read_bytes()
        self.tools.memory_read("tea")
        edited = original.replace(b"description: A drink", b"description: Tea: the drink")
        edited = edited.replace(b"source: owner", b"source: calendar:fixture")
        edited += b"\n---\nstatus: archived\n---\nSecond line.\n"
        edited = edited.replace(b"\n", b"\r\n")
        self.entry.write_bytes(edited)
        self.assertIn("changed since the read", self.tools.memory_patch("tea", "Mint", "Black"))
        self.assertEqual(self.entry.read_bytes(), edited)
        read = self.tools.memory_read("tea")
        self.assertIn('description: Tea: the drink\r\n', read)      # as the owner typed it
        self.assertIn("source: calendar:fixture\r\n", read)
        old = "Mint tea.\r\n---\r\nstatus: archived\r\n---\r\nSecond"
        new = old.replace("Mint", "Black")
        self.assertIn(old, read)
        self.assertEqual(self.tools.memory_patch("tea", old, new), "Patched facts/tea.")
        self.assertEqual(self.vault.read("tea").body, new + " line.\r\n")

    def test_hand_edited_frontmatter_is_strict(self):
        self.add_tea()
        original = self.entry.read_bytes()
        for before, after in (
            (b"name: tea\n", b"name: tea\nname: tea\n"),
            (b"kind: fact\n", b"kind: person\n"),
            (b"source: owner\n", b"source:owner\n"),
            (b"source: owner\n", b""),
            (b"description: A drink\n", b"description: A\rdrink\n"),
            (b"description: A drink\n", b"description: A\xe2\x80\xa8drink\n"),
            (b"status: active\n", b"status: active\nreplaced_by: coffee\n"),
            (b"status: active\n", b"status: archived\nreplaced_by: ../coffee\n"),
            (b"\n---\nMint", b"\n----\nMint"),
            (b"\n---\nMint tea.", b"\n---"),
        ):
            with self.subTest(after=after):
                damaged = original.replace(before, after)
                self.assertNotEqual(damaged, original)
                self.entry.write_bytes(damaged)
                self.assertTrue(self.tools.memory_read("facts/tea").startswith("Error:"))
                if b"status: archived\n" in after:
                    self.assertNotIn("facts/tea", self.tools.memory_list("facts"))
                else:
                    self.assertIn("unreadable entry", self.tools.memory_list("facts"))
                self.assertEqual(self.entry.read_bytes(), damaged)
        self.entry.write_bytes(re.sub(rb"(?m)^updated: .+$", b"updated: 2026-02-30", original))
        self.assertIn("Invalid memory date", self.tools.memory_read("facts/tea"))

    def test_archive_can_resume_after_status_is_committed(self):
        self.add_tea()
        self.assertEqual(self.tools.memory_add(
            "facts", "coffee", "Replacement drink", "Black coffee.", "owner"),
            "Added facts/coffee.")
        with patch("jarvis.v3.vault.os.rename", side_effect=OSError("interrupted")):
            self.assertEqual(self.tools.memory_archive("facts/tea", "coffee"), "Archived facts/tea.")
        pending = self.entry.read_bytes()
        self.assertIn(b"status: archived\nreplaced_by: coffee\n", pending)
        archive = self.entry.parent / ".archive"
        self.assertEqual(list(archive.iterdir()), [])
        self.assertIn("not found", self.tools.memory_read("facts/tea"))
        self.assertNotIn("facts/tea", self.tools.memory_list())
        self.assertNotIn("Mint tea", self.tools.memory_search("Mint tea"))

        self.assertEqual(self.tools.memory_archive("facts/tea"), "Archived facts/tea.")
        self.assertFalse(self.entry.exists())
        (saved,) = archive.iterdir()
        self.assertEqual(saved.read_bytes(), pending)

    def test_an_unknown_folder_is_refused_with_the_folders_that_exist(self):
        reply = self.tools.memory_add("events", "tea", "A drink", "Mint tea.", "owner")
        self.assertIn("people, projects, preferences, style, facts", reply)
        self.assertFalse(self.root.exists())

    def test_missing_folders_are_lazy_and_recreated_on_write(self):
        self.assertEqual(self.tools.memory_list(), "Memory is empty.")
        self.assertFalse(self.root.exists())
        self.root.mkdir(mode=0o700)
        self.assertEqual(self.tools.memory_list("facts"), "Memory is empty.")
        self.assertIn("not found", self.tools.memory_read("facts/tea"))
        self.assertFalse(self.entry.parent.exists())
        self.add_tea()
        self.assertEqual(stat.S_IMODE(self.entry.parent.stat().st_mode), 0o700)

    def test_hand_made_folder_reports_bad_mode_and_add_tightens_it(self):
        self.add_tea()
        people = self.root / "people"
        people.mkdir()
        people.chmod(0o755)
        self.assertIn("Vault folder people must have mode 0700.", self.tools.memory_list())
        self.assertFalse(self.tools.memory_list().startswith("Error:"))
        self.assertIn("0700", self.tools.memory_list("people"))
        self.assertIn("0700", self.tools.memory_read("people/fixture"))
        self.assertIn("Mint tea.", self.tools.memory_read("tea"))
        self.assertEqual(stat.S_IMODE(people.stat().st_mode), 0o755)

        self.assertEqual(self.tools.memory_add(
            "people", "fixture", "A contact", "A test contact.", "owner"),
            "Added people/fixture.")
        self.assertEqual(stat.S_IMODE(people.stat().st_mode), 0o700)
        self.assertIn("A test contact.", self.tools.memory_read("people/fixture"))

    def test_hand_made_root_reports_bad_mode_and_add_tightens_it(self):
        self.root.mkdir()
        self.root.chmod(0o755)
        self.assertIn("Vault root must have mode 0700", self.tools.memory_read("USER"))
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o755)
        self.add_tea()
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)

    def test_patch_and_archive_tighten_the_existing_folder(self):
        self.add_tea()
        self.tools.memory_read("tea")
        folder = self.entry.parent
        folder.chmod(0o755)
        self.assertEqual(self.tools.memory_patch("facts/tea", "Mint", "Black"), "Patched facts/tea.")
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        folder.chmod(0o755)
        self.assertEqual(self.tools.memory_archive("tea"), "Archived facts/tea.")
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)


class HandEdits(unittest.TestCase):
    def test_a_file_saved_by_an_editor_is_read_and_tightened_on_write(self):
        import stat
        import tempfile
        from pathlib import Path
        from jarvis.v3.tools.memory import MemoryTools
        from jarvis.v3.vault import Vault
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "vault"
            tools = MemoryTools(Vault(root))
            tools.memory_add("facts", "tea", "what they drink", "John prefers tea.", "owner")
            path = root / "facts" / "tea.md"
            text = path.read_text()
            path.unlink()
            path.write_text(text.replace("prefers tea", "prefers mint tea"))
            path.chmod(0o644)
            self.assertIn("mint", tools.memory_read("tea"))
            self.assertIn("facts/tea", tools.memory_search("mint"))
            self.assertEqual(tools.memory_patch("tea", "mint", "green"), "Patched facts/tea.")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
