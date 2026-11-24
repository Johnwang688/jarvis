"""Fixture regressions for M5 ownership, receipts, and recovery."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jarvis.v3 import skill_store
from jarvis.v3.skill_store import SkillStore, StoreError
from jarvis.v3.tools.skills import SkillTools


NAME = "release-workflow"
BODY = "Run the fixture checks.\n"
REFERENCE = "references/checklist.md"


class BoundaryReview(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = self.home / "learned"
        self.ledger = self.home / "ledger"
        self.owner = self.home / "owner-skills"
        self.owner.mkdir(mode=0o755)
        self.claude = self.home / ".claude/skills"
        self.codex = self.home / ".codex/skills"
        self.links = (self.claude, self.codex)
        self.store = self.reopen()
        self.tools = SkillTools(self.store, session="first-session")
        self.main = self.root / NAME / "SKILL.md"

    def reopen(self):
        return SkillStore(self.root, ledger=self.ledger, links=self.links,
                          owner_roots=(self.owner,))

    def create(self, tools=None):
        return (tools or self.tools).skill_manage(
            "create", NAME, description="Prepare a release.", body=BODY)

    def ok(self, result):
        self.assertFalse(result.startswith("Error:"), result)

    def write_reference(self, content):
        self.ok(self.tools.skill_view(NAME))
        self.ok(self.tools.skill_manage("write_file", NAME, path=REFERENCE, content=content))

    def crash_before_links(self, action):
        def stop(*args, **kwargs):
            os._exit(79)

        pid = os.fork()
        if pid == 0:
            try:
                with patch.object(skill_store._Operation, "_sync_links", stop):
                    action()
            finally:
                os._exit(97)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 79)

    def test_archive_recovery_removes_dangling_consumer_links(self):
        self.ok(self.create())
        original = self.main.read_bytes()
        self.crash_before_links(lambda: self.tools.skill_manage("archive", NAME))
        reopened = self.reopen()
        archive = reopened.records(with_status=True)[-1]
        self.assertEqual((archive["op"], archive["status"]), ("archive", "applied"))
        self.assertEqual((self.root / ".archive" / archive["id"] / "SKILL.md").read_bytes(), original)
        self.assertNotIn(NAME, reopened.names())
        self.assertFalse(any(os.path.lexists(folder / NAME) for folder in self.links))

    def test_create_recovery_exposes_skill_to_both_consumers(self):
        self.crash_before_links(self.create)
        reopened = self.reopen()
        self.assertEqual(reopened.records(with_status=True)[-1]["status"], "applied")
        self.assertIn(NAME, reopened.names())
        for folder in self.links:
            with self.subTest(consumer=folder.parent.name):
                link = folder / NAME
                self.assertTrue(link.is_symlink())
                self.assertEqual((link / "SKILL.md").read_bytes(), self.main.read_bytes())

    def test_codex_owner_name_is_reserved_even_with_a_different_folder_name(self):
        owner_skill = self.codex / "owner-release-checklist" / "SKILL.md"
        owner_skill.parent.mkdir(parents=True)
        owner_skill.write_text(f"---\nname: {NAME}\ndescription: Owner workflow.\n---\nOwner steps.\n")
        original = owner_skill.read_bytes()
        self.assertTrue(self.create().startswith("Error:"), "Create duplicated an owner skill name.")
        self.assertEqual(owner_skill.read_bytes(), original)
        self.assertFalse(self.main.exists())

    def test_rollback_cannot_revive_a_reference_in_a_recreated_skill(self):
        self.ok(self.create())
        self.write_reference("Instructions from the first skill.\n")
        self.ok(self.tools.skill_manage("remove_file", NAME, path=REFERENCE))
        removed = self.store.records()[-1]["id"]
        self.ok(self.tools.skill_manage("archive", NAME))
        self.ok(self.create(SkillTools(self.reopen(), session="second-session")))

        with self.assertRaises(StoreError):
            self.reopen().rollback(removed)
        self.assertFalse((self.main.parent / REFERENCE).exists())

    def test_blocked_fixture_text_is_absent_from_the_ledger_and_result(self):
        self.ok(self.create())
        self.ok(self.tools.skill_view(NAME))
        blocked = "ghp" + "_" + "S5k6" * 9
        name = f"references/{blocked}.md"
        result = self.tools.skill_manage("write_file", NAME, path=name, content=blocked)
        self.assertTrue(result.startswith("Error:"))
        self.assertFalse(any((folder / NAME / name).exists() for folder in self.links))
        self.assertFalse(blocked in result, "The tool result included blocked fixture text.")
        ledger = (self.ledger / "ledger.jsonl").read_text()
        self.assertFalse(blocked in ledger, "The ledger included blocked fixture text.")



if __name__ == "__main__":
    unittest.main()
