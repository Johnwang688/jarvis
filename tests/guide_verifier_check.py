"""slim_claude.py --check/--self-test after the guide moved to AGENTS.md (PR #413 review).

Intra-file #anchors are not checked; that predates the move.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path("scripts/slim_claude.py")


class GuideVerifier(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.copy = Path(temp.name)
        (self.copy / SCRIPT.parent).mkdir()
        shutil.copy2(ROOT / SCRIPT, self.copy / SCRIPT)
        shutil.copytree(ROOT / "docs", self.copy / "docs")
        (self.copy / "swecompare").mkdir()
        shutil.copy2(ROOT / "swecompare/RUNBOOK.md", self.copy / "swecompare/RUNBOOK.md")
        for name in ("AGENTS.md", "CLAUDE.md"):
            shutil.copy2(ROOT / name, self.copy / name)
        self.env = dict(os.environ, GIT_DIR=self.git("rev-parse", "--absolute-git-dir").decode().strip())
        baseline = self.check()
        self.assertEqual(baseline.returncode, 0, baseline.stderr)

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=ROOT)

    def check(self, mode="--check"):
        return subprocess.run([sys.executable, str(SCRIPT), mode], cwd=self.copy,
                              env=self.env, capture_output=True, text=True)

    def replace(self, name, old, new):
        path = self.copy / name
        original = path.read_bytes()
        self.assertIn(old, original)
        path.write_bytes(original.replace(old, new, 1))

    def assert_rejected(self, mode="--check"):
        result = self.check(mode)
        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_broken_claude_import(self):
        self.replace("CLAUDE.md", b"@AGENTS.md", b"@AGENTS-typo.md")
        self.assert_rejected()

    def test_missing_claude_import_file(self):
        (self.copy / "CLAUDE.md").unlink()
        self.assert_rejected()

    def test_check_rejects_legacy_guide_in_migrated_checkout(self):
        (self.copy / "AGENTS.md").unlink()
        (self.copy / "CLAUDE.md").write_bytes(self.git("show", "HEAD~2:CLAUDE.md"))
        self.assert_rejected()

    def test_self_test_exercises_migrated_layout(self):
        # Mutant: the verifier reads the guide from the pointer file again.
        self.replace(SCRIPT, b'guide = (root/GUIDE).read_bytes()', b'guide = (root/POINTER).read_bytes()')
        self.assert_rejected()
        self.assert_rejected("--self-test")

    def test_retained_edit(self):
        self.replace("AGENTS.md", b"## Invariants ", b"## Invariantz ")
        self.assert_rejected()

    def test_missing_agents(self):
        (self.copy / "AGENTS.md").unlink()
        self.assert_rejected()

    def test_broken_agents_link(self):
        self.replace("AGENTS.md", b"(docs/jarvis-v2-design.md)", b"(missing.md)")
        self.assert_rejected()

    def test_broken_claude_link(self):
        self.replace("CLAUDE.md", b"(AGENTS.md)", b"(missing.md)")
        self.assert_rejected()



if __name__ == "__main__":
    unittest.main()
