"""A1 acceptance (orchestrator-owned; the implementer must not edit this file).

PR map A1: v2 is frozen at the tag `v2-final`. The freeze is a written rule in
AGENTS.md plus a report script; v2 fixes carry a `v2-fix:` subject prefix.

Run: PYTHONPATH=<worktree> python tests/v3/a1_acceptance_check.py
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "v2_freeze_report.py"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
           "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tempfile.gettempdir()}


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], env=GIT_ENV, check=True,
                          capture_output=True, text=True).stdout.strip()


def commit(repo, subject, files):
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        git(repo, "add", rel)
    git(repo, "commit", "-q", "-m", subject)
    return git(repo, "rev-parse", "--short", "HEAD")


def run(repo, *extra):
    return subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo), *extra],
                          capture_output=True, text=True, timeout=60)


class Report(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="a1-accept-")
        self.repo = Path(self._tmp.name)
        git(self.repo, "init", "-q", "-b", "main")
        commit(self.repo, "base", {"jarvis/v2/x.py": "a\n", "hud/src/a.ts": "a\n",
                                   "jarvis/v2/owner_ops.py": "a\n", "jarvis/v2/permissions.py": "a\n"})
        git(self.repo, "tag", "-a", "v2-final", "-m", "v2 archived")

    def tearDown(self):
        self._tmp.cleanup()

    def test_clean_history_passes(self):
        commit(self.repo, "v3 work", {"jarvis/v3/y.py": "b\n", "tests/v3/t.py": "b\n"})
        r = run(self.repo, "--check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_unprefixed_v2_change_fails_check_and_is_listed(self):
        bad = commit(self.repo, "tweak v2", {"jarvis/v2/x.py": "b\n"})
        r = run(self.repo, "--check")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(bad, r.stdout)
        self.assertIn("jarvis/v2/x.py", r.stdout)

    def test_unprefixed_hud_change_fails_check(self):
        commit(self.repo, "restyle", {"hud/src/a.ts": "b\n"})
        self.assertEqual(run(self.repo, "--check").returncode, 1)

    def test_v2_fix_prefix_passes_but_is_reported(self):
        good = commit(self.repo, "v2-fix: guard a crash", {"jarvis/v2/x.py": "b\n"})
        r = run(self.repo, "--check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(good, r.stdout)
        self.assertIn("jarvis/v2/x.py", r.stdout)

    def test_shared_gate_files_are_not_frozen(self):
        commit(self.repo, "gate: refuse jarvis agent for agents",
               {"jarvis/v2/owner_ops.py": "b\n", "jarvis/v2/permissions.py": "b\n"})
        r = run(self.repo, "--check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("shared gate", r.stdout)

    def test_without_check_reports_and_exits_zero(self):
        commit(self.repo, "tweak v2", {"jarvis/v2/x.py": "b\n"})
        self.assertEqual(run(self.repo).returncode, 0)

    def test_merge_commits_are_skipped(self):
        git(self.repo, "checkout", "-q", "-b", "side")
        commit(self.repo, "v2-fix: on a branch", {"jarvis/v2/x.py": "b\n"})
        git(self.repo, "checkout", "-q", "main")
        commit(self.repo, "v3", {"jarvis/v3/y.py": "b\n"})
        git(self.repo, "merge", "-q", "--no-ff", "side", "-m", "Merge side into main")
        r = run(self.repo, "--check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("Merge side", r.stdout)

    def test_missing_tag_exits_2(self):
        r = run(self.repo, "--since", "no-such-ref", "--check")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("no-such-ref", r.stdout + r.stderr)  # a missing script also exits 2


class Guide(unittest.TestCase):
    def test_archive_section(self):
        text = (ROOT / "AGENTS.md").read_text()
        heading = "## v2 is archived"
        self.assertEqual(text.count(heading), 1)
        start = text.index(heading)
        section = text[start:text.index("\n## ", start + 1)]
        for needle in ("v2-final", "jarvis hud", "jarvis agent", "jarvis/v3/", "hud3/",
                       "v2-fix:", "scripts/v2_freeze_report.py"):
            self.assertIn(needle, section)
        self.assertLessEqual(len(section.encode()), 700, "keep the section short")
        # Outside every retained block, and before "## Working on v2".
        self.assertLess(start, text.index("## Working on v2"))
        self.assertGreater(start, text.index("<!-- retained:purpose-and-environment:end -->"))

    def test_slim_check_passes(self):
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "slim_claude.py"), "--check"],
                           capture_output=True, text=True, timeout=120, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("verified:", r.stdout)


if __name__ == "__main__":
    unittest.main()
