"""M6 review regressions using only fixture storage and an in-process clock."""

from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jarvis.v3 import skill_store
from jarvis.v3.skill_curator import Curator, DAY
from jarvis.v3.skill_store import SkillStore
from jarvis.v3.tools.skills import SkillTools


EPOCH = 1_800_000_000
NAME = "release-guide"
BODY = "Run the checks.\n"


class CuratorReview(unittest.TestCase):
    def setUp(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.root = tmp / "skills"
        self.ledger = tmp / "ledger"
        self.links = [tmp / "claude-skills", tmp / "codex-skills"]
        self.store = SkillStore(self.root, ledger=self.ledger, links=self.links, owner_roots=[])
        self.tools = SkillTools(self.store, session="fixture")
        self.manage_at(0, "create", NAME, description="Release steps.", body=BODY)

    @contextmanager
    def clock_at(self, days):
        stamp = EPOCH + days * DAY
        with patch.object(skill_store, "datetime", wraps=datetime) as clock, \
                patch.object(skill_store.time, "time", return_value=stamp):
            clock.now.return_value = datetime.fromtimestamp(stamp, timezone.utc)
            yield

    def manage_at(self, days, action, name, **fields):
        with self.clock_at(days):
            result = self.tools.skill_manage(action, name, **fields)
        self.assertTrue(result.startswith("Skill change saved."), result)

    def curate_at(self, days):
        return Curator(self.store).run(EPOCH + days * DAY)

    def test_recent_patch_prevents_archive_despite_an_old_view(self):
        with self.clock_at(1):
            self.assertIn(BODY, self.tools.skill_view(NAME))
        self.manage_at(32, "patch", NAME, old="Run the checks.", new="Run the release checks.")

        self.assertEqual(self.curate_at(32 + 5 / 1440), [])
        self.assertTrue((self.root / NAME / "SKILL.md").is_file())

    def test_create_during_clock_regression_gets_a_grace_period(self):
        self.assertEqual([row["change"] for row in self.curate_at(40)], ["archived"])
        new = "publish-guide"
        self.manage_at(1, "create", new, description="Publishing steps.", body=BODY)

        # The wall clock is corrected five minutes after the create.
        self.assertEqual(self.curate_at(40 + 5 / 1440), [])
        self.assertTrue((self.root / new / "SKILL.md").is_file())

    def test_torn_archive_intent_can_recover_after_a_normal_ledger_append(self):
        write = skill_store._write

        def interrupt_archive_append(fd, data):
            if b'"actor":"curator"' in data and b'"op":"archive"' in data:
                os.write(fd, data[:25])
                os.fsync(fd)
                raise OSError(errno.ENOSPC, "fixture ledger write interrupted")
            write(fd, data)

        with patch.object(skill_store, "_write", interrupt_archive_append):
            self.assertEqual(self.curate_at(31), [])
        self.assertTrue((self.root / NAME / "SKILL.md").is_file())
        journal = self.ledger / "ledger.jsonl"
        torn = journal.read_bytes()
        self.assertFalse(torn.endswith(b"\n"))
        self.manage_at(31, "create", "publish-guide", description="Publishing steps.", body=BODY)
        self.assertTrue(journal.read_bytes().startswith(torn))

        rows = self.curate_at(32)
        self.assertEqual([(row["skill"], row["change"]) for row in rows], [(NAME, "archived")])
        self.assertEqual(self.curate_at(33), [])
        digest = (self.ledger / "curator.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(line) for line in digest], rows)

    def test_transient_unlink_failure_is_retried_by_the_next_curator_run(self):
        original = skill_store._Operation._unlink_owned

        def fail_one_consumer(op, fd, name, existing):
            if os.fstat(fd).st_ino == self.links[0].stat().st_ino:
                raise OSError(errno.EIO, "fixture consumer unlink failed")
            return original(op, fd, name, existing)

        with patch.object(skill_store._Operation, "_unlink_owned", fail_one_consumer):
            self.curate_at(31)
        self.assertFalse((self.root / NAME).exists())
        self.assertTrue((self.links[0] / NAME).is_symlink())

        self.curate_at(32)
        self.assertFalse(any((link / NAME).is_symlink() for link in self.links))
        digest = (self.ledger / "curator.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(line)["change"] for line in digest], ["archived"])


if __name__ == "__main__":
    unittest.main()
