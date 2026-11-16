"""Round-one hardening regressions; temporary files and fake credentials only."""
from __future__ import annotations

import errno
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

# Reuse the verifier's isolated import and per-test HOME/config setup.
import name_protection_verifier_check as verifier
from jarvis import runtime
from jarvis.tools import search, secrets, shell


class OrdinaryFlags(unittest.TestCase):
    setUp = verifier.Counterexamples.setUp

    def test_ordinary_short_option_clusters_still_run(self):
        (self.project / "readme.txt").write_text("x\n")
        for command in ("ls -la", "grep -rn x .", "date -u"):
            with self.subTest(command=command):
                out = verifier.call("run_readonly", command=command)
                self.assertIn("[exit 0]", out)
                self.assertFalse(out.startswith("Error:"), out)


class WalkErrors(unittest.TestCase):
    def test_initial_eacces_keeps_name_verdict_but_followed_link_refuses(self):
        with tempfile.TemporaryDirectory(prefix="nameprot-errors-") as tmp:
            root = Path(tmp)
            target = root / "ordinary.txt"
            alias = root / "alias.txt"
            alias.symlink_to(target.name)
            lstat = Path.lstat

            def inaccessible(path, *args, **kwargs):
                if path == target:
                    raise PermissionError(errno.EACCES, "fixture permission denied", str(path))
                return lstat(path, *args, **kwargs)

            with patch.object(Path, "lstat", inaccessible):
                for walk in (secrets.is_credential, secrets.is_env_credential):
                    with self.subTest(walker=walk.__name__):
                        self.assertFalse(walk(target))
                        self.assertTrue(walk(alias))

    def test_link_target_parent_dotdot_preserves_protected_hop(self):
        with tempfile.TemporaryDirectory(prefix="nameprot-target-") as tmp:
            root = Path(tmp)
            (root / "real" / "inner").mkdir(parents=True)
            (root / "bridge").symlink_to("real/inner", target_is_directory=True)
            (root / "payload").write_text("fixture\n")
            (root / "real" / ".netrc").symlink_to("../payload")
            (root / "real" / "alias").symlink_to(".netrc")
            (root / "entry").symlink_to("bridge/../alias")
            self.assertTrue(secrets.is_credential(root / "entry"))
            self.assertFalse(secrets.is_env_credential(root / "entry"))

    def test_bare_option_tail_keeps_folder_judging_rule(self):
        with tempfile.TemporaryDirectory(prefix="nameprot-options-") as tmp:
            with patch.object(runtime, "working_dir", return_value=tmp), \
                 patch("jarvis.tools.files.credential_folder_judge", return_value=lambda _: True):
                self.assertIsNone(shell._credential_operand("date", ["date", "-falias"]))
                self.assertEqual(shell._credential_operand("date", ["date", "-f./alias"]),
                                 "-f./alias")

    def test_search_refuses_folder_root_before_either_backend(self):
        with tempfile.TemporaryDirectory(prefix="nameprot-search-") as tmp:
            with patch.object(search, "credential_folder_judge", return_value=lambda _: True), \
                 patch.object(search.shutil, "which") as backend:
                for mode in search.MODES:
                    self.assertTrue(search.grep_files("fixture", tmp, mode=mode).startswith("No matches"))
                backend.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
