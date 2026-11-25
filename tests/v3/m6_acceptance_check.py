"""Orchestrator-owned acceptance tests for M6 (implementers: do not edit; if one is wrong, stop and say why).

Contract
  Usage: every successful SkillTools.skill_view of a LEARNED skill's SKILL.md adds one to root/.usage.json:
    {"<name>": {"count": int, "last_used": <epoch seconds>}}   0600, written atomically under the store lock, never through a link.
    A missing, torn or wrong-shaped file is treated as empty and replaced on the next view. Owner skills are not counted.
  jarvis/v3/skill_curator.py
    DAY = 86400; STALE_DAYS = 14; ARCHIVE_DAYS = 30; WEEK_S = 7 * DAY; IDLE_S = 2 * 3600
    due(now, *, last_run, idle_s) -> bool        True only when idle_s >= IDLE_S and (last_run is None or now - last_run >= WEEK_S)
    Curator(store, *, referenced=None)            referenced: callable -> iterable of skill names a schedule or trigger uses (exempt)
      run(now=None) -> list[dict]                 one line per change, also appended to <ledger dir>/curator.jsonl (0600):
                                                  {"ts": iso, "skill": name, "change": "stale"|"fresh"|"archived", "idle_days": int}
    A skill's age is now minus its last use; never used, minus its newest ledger record (create, patch, restore, rollback ...).
    Only live, learned, unpinned, unlocked skills are judged. >= 14 d -> stale (kept live and linked; recorded once, state kept
    next to the usage counts); used again -> fresh; >= 30 d -> archived through the store (ledger op "archive", actor "curator"),
    which `SkillStore.rollback(id)` restores. No model is called; nothing is deleted.
"""
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path

from jarvis.v3 import skill_curator
from jarvis.v3.skill_curator import Curator, due
from jarvis.v3.skill_store import SkillStore
from jarvis.v3.tools.skills import SkillTools

DAY = 86400
BODY = "# Release\n\n1. Run the tests.\n2. Tag the commit.\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        self.root, self.led = self.tmp / "v3" / "skills", self.tmp / "v3" / "skill-ledger"
        self.links = [self.tmp / "claude" / "skills", self.tmp / "codex" / "skills"]
        self.owner = self.tmp / "repo" / "skills"
        for d in (*self.links, self.owner):
            d.mkdir(parents=True)
        self.store = SkillStore(self.root, ledger=self.led, links=self.links, owner_roots=[self.owner, *self.links])
        self.t = SkillTools(self.store, session="s1")
        self.now = time.time()

    def create(self, name):
        text = self.t.skill_manage("create", name, description="How to do it.", body=BODY)
        self.assertFalse(text.startswith("Error:"), text)

    def view(self, name, t=None):
        text = (t or self.t).skill_view(name)
        self.assertFalse(text.startswith("Error:"), text)
        return text

    def usage(self):
        return json.loads((self.root / ".usage.json").read_text())

    def live(self, name):
        return all((d / name).is_symlink() for d in self.links) and (self.root / name / "SKILL.md").is_file()

    def run_at(self, days, **kw):
        return Curator(self.store, **kw).run(now=self.now + days * DAY)

    def changes(self, lines):
        return sorted((line["skill"], line["change"]) for line in lines)


class Usage(Base):
    def test_views_are_counted(self):
        self.create("deploy-web")
        self.view("deploy-web")
        self.view("deploy-web", SkillTools(self.store, session="s2"))
        row = self.usage()["deploy-web"]
        self.assertEqual(row["count"], 2)
        self.assertAlmostEqual(row["last_used"], time.time(), delta=60)
        self.assertEqual(stat.S_IMODE((self.root / ".usage.json").stat().st_mode), 0o600)

    def test_failed_and_owner_views_are_not_counted(self):
        self.create("deploy-web")
        (self.owner / "wharton").mkdir()
        (self.owner / "wharton" / "SKILL.md").write_text("---\nname: wharton\ndescription: d\n---\nowner text\n")
        self.view("wharton")
        self.assertTrue(self.t.skill_view("missing-skill").startswith("Error:"))
        self.assertTrue(self.t.skill_view("deploy-web", "references/none.md").startswith("Error:"))
        path = self.root / ".usage.json"
        names = set(json.loads(path.read_text())) if path.exists() else set()
        self.assertLessEqual(names, {"deploy-web"})
        self.assertNotIn("wharton", names)

    def test_a_bad_usage_file_never_breaks_a_view(self):
        self.create("deploy-web")
        for junk in ("{", "[]", '{"deploy-web": 3}', '{"deploy-web": {"count": "x", "last_used": null}}', "\x00" * 10):
            (self.root / ".usage.json").write_text(junk)
            self.view("deploy-web")
            self.assertEqual(self.usage()["deploy-web"]["count"], 1, junk)

    def test_a_linked_usage_file_is_not_followed(self):
        self.create("deploy-web")
        outside = self.tmp / "outside.json"
        outside.write_text("{}")
        (self.root / ".usage.json").unlink(missing_ok=True)
        (self.root / ".usage.json").symlink_to(outside)
        self.view("deploy-web")
        self.assertEqual(outside.read_text(), "{}")
        self.assertEqual(self.run_at(40), [])   # usage unreadable: nothing is archived on a guess
        self.assertTrue(self.live("deploy-web"))


class Due(unittest.TestCase):
    def test_weekly_and_only_when_idle(self):
        now = 1_800_000_000.0
        self.assertTrue(due(now, last_run=None, idle_s=2 * 3600))
        self.assertFalse(due(now, last_run=None, idle_s=2 * 3600 - 1))
        self.assertFalse(due(now, last_run=now - 7 * DAY + 60, idle_s=10 * 3600))
        self.assertTrue(due(now, last_run=now - 7 * DAY, idle_s=10 * 3600))
        self.assertFalse(due(now, last_run=now + 3600, idle_s=10 * 3600))   # a clock that went backwards waits
        self.assertEqual((skill_curator.STALE_DAYS, skill_curator.ARCHIVE_DAYS), (14, 30))


class Run(Base):
    def setUp(self):
        super().setUp()
        self.create("deploy-web")

    def test_fourteen_and_thirty_days(self):
        self.assertEqual(self.run_at(13.9), [])
        lines = self.run_at(14.1)
        self.assertEqual(self.changes(lines), [("deploy-web", "stale")])
        self.assertEqual(lines[0]["idle_days"], 14)
        self.assertTrue(self.live("deploy-web"))
        self.assertEqual(self.run_at(20), [])                       # recorded once
        self.assertEqual(self.changes(self.run_at(30.1)), [("deploy-web", "archived")])
        self.assertFalse(any((d / "deploy-web").exists() or (d / "deploy-web").is_symlink() for d in self.links))
        self.assertNotIn("deploy-web", self.store.names())
        self.assertEqual(self.run_at(45), [])
        digest = [json.loads(line) for line in (self.led / "curator.jsonl").read_text().splitlines()]
        self.assertEqual([row["change"] for row in digest], ["stale", "archived"])
        self.assertEqual(stat.S_IMODE((self.led / "curator.jsonl").stat().st_mode), 0o600)

    def test_archive_is_ledgered_and_rollback_restores(self):
        before = (self.root / "deploy-web" / "SKILL.md").read_bytes()
        self.run_at(31)
        last = self.store.records()[-1]
        self.assertEqual((last["op"], last["skill"], last["actor"]), ("archive", "deploy-web", "curator"))
        self.store.rollback(last["id"])
        self.assertTrue(self.live("deploy-web"))
        self.assertEqual((self.root / "deploy-web" / "SKILL.md").read_bytes(), before)
        self.assertEqual(self.run_at(32), [])                       # the restore counts as activity
        self.assertEqual(self.changes(self.run_at(31 + 14.5)), [("deploy-web", "stale")])

    def test_a_use_resets_the_clock(self):
        self.run_at(15)
        self.view("deploy-web")
        usage = self.usage()
        usage["deploy-web"]["last_used"] = self.now + 16 * DAY
        (self.root / ".usage.json").write_text(json.dumps(usage))
        self.assertEqual(self.changes(self.run_at(17)), [("deploy-web", "fresh")])
        self.assertEqual(self.run_at(29), [])
        self.assertEqual(self.changes(self.run_at(16 + 14.5)), [("deploy-web", "stale")])
        self.assertTrue(self.live("deploy-web"))

    def test_pinned_locked_owner_and_referenced_are_untouched(self):
        for name in ("pinned-one", "locked-one", "used-by-schedule"):
            self.create(name)
        self.store.pin("pinned-one", True)
        self.store.lock("locked-one", True)
        (self.owner / "wharton").mkdir()
        (self.owner / "wharton" / "SKILL.md").write_text("---\nname: wharton\ndescription: d\n---\nowner text\n")
        (self.links[0] / "hand-made").mkdir()
        (self.links[0] / "hand-made" / "SKILL.md").write_text("owner text\n")
        lines = self.run_at(90, referenced=lambda: ["used-by-schedule", "wharton"])
        self.assertEqual(self.changes(lines), [("deploy-web", "archived")])
        for name in ("pinned-one", "locked-one", "used-by-schedule"):
            self.assertTrue(self.live(name), name)
        self.assertTrue((self.owner / "wharton" / "SKILL.md").is_file())
        self.assertTrue((self.links[0] / "hand-made" / "SKILL.md").is_file())

    def test_a_failing_reference_source_changes_nothing(self):
        def broken():
            raise RuntimeError("schedule store unreadable")
        self.assertEqual(self.run_at(90, referenced=broken), [])
        self.assertTrue(self.live("deploy-web"))

    def test_no_model_and_no_v2(self):
        source = Path(skill_curator.__file__).read_text()
        for word in ("claude_jobs", "openrouter", "jarvis.llm", "import llm", "jarvis.v2", "subprocess"):
            self.assertNotIn(word, source)


if __name__ == "__main__":
    unittest.main()
