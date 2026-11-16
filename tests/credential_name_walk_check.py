"""Mutation-minded name/walk regressions; temp files only, no live credentials."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jarvis import runtime
from jarvis.tools import search, secrets, shell
from jarvis.v2 import permissions


class CredentialWalk(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="credential-walk-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_non_env_intermediate_hop_counts_even_when_target_is_ordinary(self):
        target = self.root / "ordinary.txt"
        target.write_text("fixture\n")
        for name in sorted(secrets.PROTECTED_NAMES | secrets.EXACT_NAMES):
            with self.subTest(name=name):
                hop = self.root / (name.upper() + " .")
                hop.symlink_to(target.name)
                alias = self.root / "alias.txt"
                alias.symlink_to(hop.name)
                self.assertTrue(secrets.is_credential(alias))
                self.assertEqual(secrets.is_env_credential(alias),
                                 secrets.is_env_credential_name(hop.name))
                alias.unlink()

    def test_realpath_name_is_judged_after_a_parent_symlink(self):
        real = self.root / "real"
        real.mkdir()
        (self.root / "parent").symlink_to(real, target_is_directory=True)
        (real / "GOOGLE_TOKEN.JSON .").write_text("fixture\n")
        alias = self.root / "alias.txt"
        alias.symlink_to("parent/GOOGLE_TOKEN.JSON .")
        self.assertTrue(secrets.is_credential(alias))
        self.assertFalse(secrets.is_env_credential(alias))
        # The walk also follows the last component through a parent alias.
        (real / "alias.txt").symlink_to("GOOGLE_TOKEN.JSON .")
        self.assertTrue(secrets.is_credential(self.root / "parent/alias.txt"))

    def test_both_walkers_fail_closed_on_readlink_and_lstat_errors(self):
        alias = self.root / "alias.txt"
        alias.symlink_to("ordinary.txt")
        for walk in (secrets.is_credential, secrets.is_env_credential):
            with self.subTest(walker=walk.__name__, error="readlink"):
                with patch.object(os, "readlink", side_effect=PermissionError):
                    self.assertTrue(walk(alias))
            # Orchestrator rewrite (verifier round 1, regression 2): an lstat error
            # before any link is followed keeps the name verdict, as main's
            # Path.is_symlink() did (a grep pattern word can coincide with an
            # unreadable path); once a link was followed it is an unfollowable
            # chain and refuses.
            with self.subTest(walker=walk.__name__, error="lstat at the first hop"):
                with patch.object(Path, "lstat", side_effect=PermissionError):
                    self.assertFalse(walk(alias))
                    self.assertTrue(walk(self.root / ".env.production"))
            with self.subTest(walker=walk.__name__, error="lstat after a followed link"):
                real_lstat = Path.lstat
                calls = []
                def lstat_then_fail(path_self, *a, **k):
                    calls.append(path_self)
                    if len(calls) > 1:
                        raise PermissionError
                    return real_lstat(path_self, *a, **k)
                with patch.object(Path, "lstat", lstat_then_fail):
                    self.assertTrue(walk(alias))

    def test_both_walkers_judge_realpath_and_fail_closed_if_it_errors(self):
        alias = self.root / "ordinary.txt"
        for walk, name in ((secrets.is_credential, ".NETRC ."),
                           (secrets.is_env_credential, ".ENV.production")):
            with self.subTest(walker=walk.__name__):
                with patch.object(os.path, "realpath", return_value=str(self.root / name)):
                    self.assertTrue(walk(alias))
                with patch.object(os.path, "realpath", side_effect=OSError):
                    self.assertTrue(walk(alias))

    def test_long_chain_refuses_but_short_ordinary_chain_does_not(self):
        (self.root / "last").write_text("fixture\n")
        for i in reversed(range(41)):
            (self.root / f"hop{i}").symlink_to("last" if i == 40 else f"hop{i + 1}")
        for walk in (secrets.is_credential, secrets.is_env_credential):
            with self.subTest(walker=walk.__name__):
                self.assertTrue(walk(self.root / "hop0"))
                self.assertFalse(walk(self.root / "hop3"))

    def test_missing_and_dangling_paths_are_judged_by_name(self):
        alias = self.root / "alias.txt"
        alias.symlink_to("missing.txt")
        self.assertFalse(secrets.is_credential(alias))
        alias.unlink()
        alias.symlink_to(".NETRC .")
        self.assertTrue(secrets.is_credential(alias))
        self.assertFalse(secrets.is_env_credential(alias))
        self.assertTrue(secrets.is_credential(self.root / "GOOGLE_TOKEN.JSON ."))

    def test_oversized_ordinary_path_keeps_name_only_verdict_but_link_refuses(self):
        oversized = "x" * 300
        alias = self.root / "alias.txt"
        alias.symlink_to(oversized)
        for walk in (secrets.is_credential, secrets.is_env_credential):
            with self.subTest(walker=walk.__name__):
                self.assertFalse(walk(self.root / oversized))
                self.assertTrue(walk(alias))
                self.assertTrue(walk(self.root / (".env." + oversized)))

    def test_raw_name_boundaries_still_exclude_lookalikes(self):
        for name in ("notes.GIT-CREDENTIALS.txt", "my_NETRC", "x.NETRC",
                     ".GIT-CREDENTIALS.bak2x", "GOOGLE_TOKEN.JSON.example"):
            with self.subTest(name=name):
                self.assertFalse(secrets.is_credential_name(name))
                self.assertIsNone(secrets.protected_in_command(f"cat /tmp/{name}"))
        self.assertTrue(secrets.raw_names_in("python -c \"open('.NETRC')\""))

    def test_bound_relative_glob_and_tilde_operands_judge_aliases(self):
        (self.root / "payload").write_text("fixture\n")
        (self.root / "_NETRC .").symlink_to("payload")
        (self.root / "alias.txt").symlink_to("_NETRC .")
        with patch.object(runtime, "working_dir", return_value=str(self.root)):
            for operand in ("alias.txt", "*.txt", "--file=alias.txt", "-f./alias.txt"):
                with self.subTest(operand=operand):
                    self.assertEqual(shell._credential_operand("cat", ["cat", operand]), operand)
        with patch.dict(os.environ, {"HOME": str(self.root)}):
            self.assertEqual(shell._credential_operand("cat", ["cat", "~/alias.txt"]),
                             "~/alias.txt")

    def test_search_filter_and_v2_read_judge_non_env_intermediate_hop(self):
        (self.root / "payload").write_text("fixture\n")
        (self.root / "_NETRC .").symlink_to("payload")
        alias = self.root / "alias.txt"
        alias.symlink_to("_NETRC .")
        # Isolate name judging from the independently tested folder rule.
        with patch.object(search, "credential_folder_judge", return_value=lambda _: False):
            self.assertTrue(search.refuser()(alias))
        self.assertIsNotNone(permissions.denied_read("Read", {"file_path": str(alias)}))

    def test_plain_recursive_search_of_folder_holding_file_remains_a_limit(self):
        (self.root / "_netrc").write_text("fixture\n")
        with patch("jarvis.tools.files.credential_folder_judge", return_value=lambda _: False), \
             patch("jarvis.tools.files._credential_roots", return_value=[]), \
             patch("jarvis.tools.files.credential_folder_holds", return_value=False):
            self.assertIsNone(shell._credential_operand("grep", ["grep", "-r", "fixture", str(self.root)]))
            self.assertIsNone(shell._credential_operand("rg", ["rg", "fixture", str(self.root)]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
