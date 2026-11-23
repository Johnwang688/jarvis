"""Exercise the v2 freeze report against isolated Git histories."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "v2_freeze_report.py"


class Report(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="v2-freeze-")))
        self.repo = root / "fixture repo"
        self.repo.mkdir()
        home = root / "home"
        home.mkdir()
        self.env = {
            "PATH": os.environ["PATH"], "HOME": str(home),
            "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"), "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }
        self.git("init", "-q", "-b", "main")
        self.commit("base", "jarvis/v2/original.py")
        self.git("tag", "-a", "v2-final", "-m", "archive")

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], capture_output=True, check=True,
            text=True, env=self.env, timeout=30,
        ).stdout.strip()

    def commit(self, subject, *files):
        for file in files:
            path = self.repo / file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture\n", encoding="utf-8")
            self.git("add", "--", file)
        self.git("commit", "-q", "-m", subject)
        return self.git("rev-parse", "--short=7", "HEAD")

    def report(self, *args, script=SCRIPT):
        return subprocess.run(
            [sys.executable, str(script), *args], capture_output=True, text=True,
            encoding="utf-8", env=self.env, cwd=self.repo.parent, timeout=30,
        )

    def test_mixed_paths_keep_spaces_unicode_and_shared_labels(self):
        subject = "mention v2-fix: inside the subject"
        sha = self.commit(subject, "hud/é space.ts", "jarvis/v2/owner_ops.py",
                          "jarvis/v2/permissions.py", "jarvis/v3/new.py", "hud3/new.ts")
        expected = [f"{sha} {subject}", "  frozen: hud/é space.ts",
                    "  shared gate: jarvis/v2/owner_ops.py",
                    "  shared gate: jarvis/v2/permissions.py"]
        for args, status in (((), 0), (("--check",), 1)):
            with self.subTest(args=args):
                result = self.report("--repo", str(self.repo), *args)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual(result.stdout.splitlines(), expected)

    def test_rename_out_of_frozen_directory(self):
        self.git("mv", "jarvis/v2/original.py", "moved.py")
        sha = self.commit("move out of v2")
        result = self.report("--repo", str(self.repo), "--check")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.splitlines(),
                         [f"{sha} move out of v2", "  frozen: jarvis/v2/original.py"])

    def test_rename_to_shared_gate_still_reports_frozen_source(self):
        self.git("mv", "jarvis/v2/original.py", "jarvis/v2/owner_ops.py")
        sha = self.commit("move a file to the shared gate")
        result = self.report("--repo", str(self.repo), "--check")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f"{sha} move a file to the shared gate",
                         "  frozen: jarvis/v2/original.py", "  shared gate: jarvis/v2/owner_ops.py"])

    def test_since_excludes_earlier_violation_and_accepts_fix(self):
        self.commit("unprefixed change", "jarvis/v2/earlier.py")
        self.git("tag", "later-base")
        sha = self.commit("v2-fix: handle a crash", "jarvis/v2/later.py")
        result = self.report("--repo", str(self.repo), "--since", "later-base", "--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(),
                         [f"{sha} v2-fix: handle a crash", "  frozen: jarvis/v2/later.py"])

    def test_shallow_history_is_an_error_in_report_mode(self):
        self.commit("v2-fix: archiving", "hud/archive.ts")
        (self.repo / ".git" / "shallow").write_text(self.git("rev-parse", "HEAD") + "\n")
        result = self.report("--repo", str(self.repo))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(result.stdout, "")
        self.assertIn("shallow", result.stderr)

    def test_path_line_breaks_and_literal_backslash_have_distinct_output(self):
        subject = "v2-fix: add fixtures"
        sha = self.commit(subject, "hud/é\nchild", r"hud/é\nchild", "hud/a\r\nb\u2028c")
        result = self.report("--repo", str(self.repo), "--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            f"{sha} {subject}",
            r"  frozen: hud/a\r\nb\u2028c",
            r"  frozen: hud/é\nchild",
            r"  frozen: hud/é\\nchild",
        ])

    def test_shared_only_under_git_config(self):
        sha = self.commit("update shared gate", "jarvis/v2/permissions.py")
        self.git("config", "core.abbrev", "12")
        self.git("config", "log.showSignature", "true")
        self.git("config", "--global", "log.follow", "true")
        result = self.report("--repo", str(self.repo), "--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(),
                         [f"{sha} update shared gate", "  shared gate: jarvis/v2/permissions.py"])

    def test_merge_is_skipped_but_side_commit_is_reported(self):
        self.git("branch", "side")
        self.commit("main v3 work", "hud3/main.ts")
        self.git("checkout", "-q", "side")
        sha = self.commit("v2-fix: side fix", "hud/fix.ts")
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "--no-commit", "side")
        self.commit("merge-only frozen change", "jarvis/v2/merge.py")
        result = self.report("--repo", str(self.repo), "--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(),
                         [f"{sha} v2-fix: side fix", "  frozen: hud/fix.ts"])

    def test_merge_that_keeps_main_tree_still_reports_side_history(self):
        self.git("switch", "-q", "--orphan", "side")
        sha = self.commit("discarded side change", "hud/discarded.ts")
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--allow-unrelated-histories", "-s", "ours", "side",
                 "-m", "keep main tree")
        self.git("config", "log.showRoot", "false")
        result = self.report("--repo", str(self.repo), "--check")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.splitlines(),
                         [f"{sha} discarded side change", "  frozen: hud/discarded.ts"])

    def test_default_repo_is_the_script_checkout(self):
        self.commit("tweak v2", "jarvis/v2/default.py")
        script = self.repo / "scripts" / SCRIPT.name
        script.parent.mkdir()
        shutil.copyfile(SCRIPT, script)
        result = self.report("--check", script=script)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("  frozen: jarvis/v2/default.py", result.stdout)

    def test_git_failures_are_reported_on_stderr(self):
        cases = (
            (("--repo", str(self.repo), "--since=--missing-ref"), "--missing-ref"),
            (("--repo", str(self.repo / "missing-repo")), "missing-repo"),
        )
        for args, diagnostic in cases:
            with self.subTest(args=args):
                result = self.report(*args)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn(diagnostic, result.stderr)


if __name__ == "__main__":
    unittest.main()
