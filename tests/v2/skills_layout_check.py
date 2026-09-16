"""Free checks for shared skills, migration and conservative CLI linking."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import warnings

from jarvis import config
from jarvis.tools import skills
from jarvis.v2 import skills_link
from scripts.convert_skills import convert, converted, JARVIS_ONLY

REPO = Path(__file__).resolve().parents[2]


class SkillsLayoutCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.library = self.root / "skills"
        self.library.mkdir()
        self.addCleanup(patch.stopall)
        patch.object(config, "SKILLS_DIR", self.library).start()
        patch.object(skills, "_index_cache", None).start()
        patch.object(skills, "_warned_flat", set()).start()

    def flat(self, name="alpha", description="Use when testing"):
        path = self.library / f"{name}.md"
        path.write_text(f"---\ndescription: {description}\n---\n\nInstructions.\n\n", encoding="utf-8")
        return path

    def test_conversion_idempotence_body_and_index(self):
        # All checked-in skills must preserve their original bodies and index.
        for path in (REPO / "skills").glob("*/SKILL.md"):
            old = path.read_bytes().replace(f"name: {path.parent.name}\n".encode(), b"", 1)
            old = old.replace(b"jarvis-only: true\n", b"", 1)
            (self.library / f"{path.parent.name}.md").write_bytes(old)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            before = skills.index().encode()
        originals = {p.stem: p.read_bytes().split(b"\n---\n", 1)[1]
                     for p in self.library.glob("*.md")}
        self.assertEqual(len(convert(self.library)), len(originals))
        self.assertEqual(before, skills.index().encode())
        self.assertEqual(convert(self.library), [])
        for name, body in originals.items():
            path = self.library / name / "SKILL.md"
            self.assertEqual(path.read_bytes().split(b"\n---\n", 1)[1], body)
            meta = skills.metadata(path.read_text())
            self.assertEqual(meta["name"], name)
            self.assertEqual(meta.get("jarvis-only") == "true", name in JARVIS_ONLY)

    def test_conversion_refuses_drift_before_any_move(self):
        alpha, zulu = self.flat(), self.flat("zulu")
        dest = self.library / "zulu" / "SKILL.md"
        dest.parent.mkdir()
        dest.write_text("different")
        with self.assertRaisesRegex(ValueError, "Refusing"):
            convert(self.library)
        self.assertTrue(alpha.exists())
        self.assertTrue(zulu.exists())
        self.assertEqual(dest.read_text(), "different")

    def test_git_move_and_matching_destination(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        source = self.flat()
        subprocess.run(["git", "add", "skills"], cwd=self.root, check=True)
        convert(self.library)
        tracked = subprocess.check_output(["git", "ls-files"], cwd=self.root, text=True)
        self.assertEqual(tracked.strip(), "skills/alpha/SKILL.md")
        source = self.flat("beta")
        target = self.library / "beta" / "SKILL.md"
        target.parent.mkdir()
        target.write_bytes(converted(source))
        convert(self.library)
        self.assertFalse(source.exists())

    def test_both_layouts_warn_once_and_new_wins(self):
        self.flat()
        self.flat("beta")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.assertIn("alpha", skills.index())
            skills.skill_list()
            skills.skill_read("alpha")
            self.assertEqual(len(caught), 2)
            skills.skill_write("alpha", "updated", "new body")
            self.assertIn("new body", skills.skill_read("alpha"))
            self.assertEqual(skills.skill_list().count("- alpha:"), 1)
            self.assertEqual(len(caught), 2)
        self.assertTrue((self.library / "alpha" / "SKILL.md").exists())
        self.assertIn("updated", skills.index())
        path = self.library / "alpha" / "SKILL.md"
        path.write_text(path.read_text().replace("updated", "externally updated longer"))
        self.assertIn("externally updated longer", skills.index())

    def test_write_quoted_description_and_preserve_metadata(self):
        description = 'Use when: "quoted" # text\nsecond line'
        skills.skill_write("whiteboard", description, "body")
        path = self.library / "whiteboard" / "SKILL.md"
        path.write_text(path.read_text().replace("name: whiteboard", "name: whiteboard\njarvis-only: true\nallowed-tools: [Read]"))
        skills.skill_write("whiteboard", description, "changed")
        self.assertEqual(skills._parse(path.read_text()), (description, "changed"))
        self.assertIn("jarvis-only: true", path.read_text())
        self.assertIn("allowed-tools: [Read]", path.read_text())
        self.assertFalse((self.library / "whiteboard.md").exists())

    def make_link_world(self):
        home = self.root / "home"
        for consumer in (".claude", ".codex"):
            (home / consumer / "skills").mkdir(parents=True)
        for name in ("alpha", "beta", "gamma", "whiteboard"):
            self.flat(name)
        convert(self.library)
        return home

    def test_link_idempotence_conflicts_and_unlink(self):
        home = self.make_link_world()
        real = home / ".claude/skills/beta"
        real.mkdir()
        (real / "keep").write_text("keep")
        foreign = home / ".codex/skills/gamma"
        foreign.symlink_to(self.root.parent / "wp8-elsewhere")  # broken foreign link
        rows = skills_link.link(repo=self.root, home=home)
        self.assertEqual(sum(r[2] == "linked" for r in rows), 4)
        self.assertEqual(sum(r[2].startswith("refused") for r in rows), 2)
        self.assertEqual(sum(r[2] == "skipped: jarvis-only" for r in rows), 2)
        self.assertEqual((home / ".claude/skills/alpha").resolve(), self.library / "alpha")
        self.assertFalse((home / ".claude/skills/whiteboard").exists())
        self.assertEqual(sum(r[2] == "skipped: correct link" for r in skills_link.link(repo=self.root, home=home)), 4)
        # Also remove stale owned links even if their source skill was deleted.
        stale = home / ".claude/skills/stale"
        stale.symlink_to(self.library / "missing")
        rows = skills_link.link(repo=self.root, home=home, unlink=True, dry_run=True)
        self.assertEqual(sum(r[2] == "would unlink" for r in rows), 5)
        self.assertTrue(stale.is_symlink())
        skills_link.link(repo=self.root, home=home, unlink=True)
        self.assertFalse(stale.is_symlink())
        self.assertTrue(foreign.is_symlink())
        self.assertEqual((real / "keep").read_text(), "keep")
        self.assertFalse((home / ".claude/skills/alpha").is_symlink())

    def test_dry_run_and_absent_parents(self):
        self.flat()
        convert(self.library)
        home = self.root / "home"
        home.mkdir()
        rows = skills_link.link(repo=self.root, home=home)
        self.assertTrue(all("absent parent" in row[2] for row in rows))
        self.assertEqual(list(home.iterdir()), [])
        (home / ".claude").mkdir()
        rows = skills_link.link(repo=self.root, home=home, dry_run=True)
        self.assertIn((".claude", "alpha", "would link"), rows)
        self.assertFalse((home / ".claude/skills").exists())
        skills_link.link(repo=self.root, home=home)
        self.assertTrue((home / ".claude/skills/alpha").is_symlink())
        self.assertFalse((home / ".codex").exists())

    def test_cli_table_temp_home(self):
        home = self.root / "home"
        for consumer in (".claude", ".codex"):
            (home / consumer / "skills").mkdir(parents=True)
        env = dict(os.environ, HOME=str(home), PYTHONPATH=str(REPO))
        command = [sys.executable, "-m", "jarvis", "skills", "link"]
        plan = subprocess.run(command + ["--dry-run"], env=env, capture_output=True, text=True, check=True)
        self.assertIn("Consumer", plan.stdout)
        self.assertIn("would link", plan.stdout)
        self.assertEqual(list((home / ".claude/skills").iterdir()), [])
        subprocess.run(command, env=env, capture_output=True, check=True)
        self.assertTrue((home / ".codex/skills/email-triage").is_symlink())
        subprocess.run(command + ["--unlink"], env=env, capture_output=True, check=True)
        self.assertEqual(list((home / ".codex/skills").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
