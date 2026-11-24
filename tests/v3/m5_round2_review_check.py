"""Round 2 regressions for M5 recovery, ownership, and rollback."""

import os
from pathlib import Path
import shutil
import tempfile
import unittest

from jarvis.v3.skill_store import SkillStore
from jarvis.v3.tools.skills import SkillTools


NAME = "release-workflow"


class RoundTwoReview(unittest.TestCase):
    def setUp(self):
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.claude = home / ".claude/skills"
        self.codex = home / ".codex/skills"
        self.root = home / "learned"
        self.ledger = home / "ledger"
        self.skill = self.root / NAME
        self.store = SkillStore(self.root, ledger=self.ledger,
                                links=(self.claude, self.codex), owner_roots=())
        self.tools = SkillTools(self.store, session="fixture-session")

    def create(self, body="Run the fixture checks.\n"):
        return self.tools.skill_manage("create", NAME,
                                       description="Prepare a release.", body=body)

    def ok(self, result):
        self.assertFalse(result.startswith("Error:"), result)

    def test_read_keeps_a_removed_consumer_home_absent(self):
        self.ok(self.create())
        shutil.rmtree(self.codex.parent)

        self.assertEqual(len(self.store.records()), 1)
        self.assertFalse(self.codex.parent.exists(), "Ledger read recreated the consumer home.")

    def test_ledger_read_survives_an_unavailable_read_only_consumer(self):
        self.ok(self.create())
        shutil.rmtree(self.claude)
        self.codex.chmod(0o100)
        self.addCleanup(self.codex.chmod, 0o700)
        if os.access(self.codex, os.R_OK):
            self.skipTest("Fixture requires directory permissions to be enforced.")

        self.assertEqual(len(self.store.records()), 1)

    def test_yaml_inline_comment_does_not_change_owner_name(self):
        owner = self.codex / "owner-checklist" / "SKILL.md"
        owner.parent.mkdir(parents=True)
        original = (f"---\nname: {NAME} # owner catalog name\n"
                    "description: Owner workflow.\n---\nOwner steps.\n")
        owner.write_text(original)

        result = self.create()
        self.assertTrue(result.startswith("Error:"), "Create duplicated the owner's name.")
        self.assertEqual(owner.read_text(), original)
        self.assertFalse(self.skill.exists())



if __name__ == "__main__":
    unittest.main()
