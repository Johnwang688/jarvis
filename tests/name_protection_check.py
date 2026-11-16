"""Name-only credential protection holds for every protected name, in every
spelling and through symlinks (orchestrator-owned acceptance tests; the
implementer may not edit this file).

Found by the GPT-6.1 Sol verifier on PR #386 (2026-11-15) and confirmed on
main for `.git-credentials`: `secrets.EXACT_NAMES` and the bundles in
`secrets.PROTECTED_NAMES` were matched case-sensitively and without Windows
trimming, `secrets.is_credential` followed symlinks only for `.env*` names, and
`run_readonly` judged each operand only against the credential *folders*, so a
copy or an alias outside a credential folder was readable:

  - `.GIT-CREDENTIALS` (Windows names are case-insensitive) by `read_file`,
    `run_readonly cat`, `grep_files` and v2's Read;
  - `ordinary.txt -> .git-credentials` by `run_readonly cat` and v2's Read
    (and `ordinary.txt -> .env` by `run_readonly cat`);
  - `cat <folder>/*` over a folder holding one.

Not changed, and not pinned here: a recursive `grep -r` of a folder that
holds a credential *file* stays the stated limit (docs/notes/
safety-permissions.md, "the class of `grep -r foo .`").

Hermetic: temporary HOME and folders, no network, no daemon.

Run: PYTHONPATH=. .venv/bin/python tests/name_protection_check.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jarvis import config, runtime  # noqa: E402
from jarvis.tools import dispatch, files, search, secrets, shell  # noqa: E402
from jarvis.v2 import permissions  # noqa: E402

SENTINEL = "sentinel-credential-value-0123456789abcdef"
# Every name refused by name alone, env floor included.
NAMES = sorted(secrets.EXACT_NAMES | secrets.PROTECTED_NAMES)


def tool_result(name, arguments):
    with runtime.tools_bound({"read_file", "grep_files", "run_readonly"}):
        return dispatch(name, json.dumps(arguments)).text


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="nameprot-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        home = self.root / "home"
        home.mkdir(mode=0o700)
        env = patch.dict(os.environ, {"HOME": str(home), "JARVIS_WINDOWS_USERS": str(self.root / "Users")})
        env.start()
        self.addCleanup(env.stop)
        for name, value in {"V2_DATA_DIR": self.root / "data", "BROWSER_DIR": self.root / "browser"}.items():
            p = patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.n = 0

    def folder(self) -> Path:
        self.n += 1
        d = self.root / "plain" / f"f{self.n}"
        d.mkdir(parents=True)
        return d

    def assert_unreadable(self, path: Path, label: str):
        with self.subTest(label=label, reader="read_file"):
            self.assertNotIn(SENTINEL, tool_result("read_file", {"path": str(path)}))
        with self.subTest(label=label, reader="run_readonly cat"):
            self.assertNotIn(SENTINEL, tool_result("run_readonly", {"command": "cat " + shlex.quote(str(path))}))
        with self.subTest(label=label, reader="grep_files"):
            self.assertNotIn(SENTINEL, tool_result("grep_files", {"pattern": "sentinel", "path": str(path)}))
        with self.subTest(label=label, reader="v2 Read"):
            self.assertIsNotNone(permissions.denied_read("Read", {"file_path": str(path)}, str(self.root)))

    def assert_readable(self, path: Path, label: str):
        with self.subTest(label=label, reader="read_file"):
            self.assertIn(SENTINEL, tool_result("read_file", {"path": str(path)}))
        with self.subTest(label=label, reader="run_readonly cat"):
            self.assertIn(SENTINEL, tool_result("run_readonly", {"command": "cat " + shlex.quote(str(path))}))
        with self.subTest(label=label, reader="v2 Read"):
            self.assertIsNone(permissions.denied_read("Read", {"file_path": str(path)}, str(self.root)))


class NameRule(unittest.TestCase):
    def test_one_name_rule_case_and_windows_trim(self):
        for name in NAMES:
            for variant in (name, name.upper(), name.swapcase(), name + ".", name + " ", name.upper() + " ."):
                with self.subTest(variant=variant):
                    self.assertTrue(secrets.is_credential_name(variant))
                    self.assertTrue(secrets.is_protected("/some/dir/" + variant))

    def test_lookalikes_stay_ordinary(self):
        for name in ("netrc", "my_netrc", "x.netrc", "notes.git-credentials.txt", ".git-credentials.bak2x",
                     "google_token.json.example", ".pypirc-help.md", "readme.md", ".envrc", "app.env"):
            with self.subTest(name=name):
                # A suffix after an exact name is another file (only `.env.<x>` is a family).
                self.assertFalse(secrets.is_credential_name(name))
                self.assertFalse(secrets.is_protected("/x/" + name))

    def test_raw_command_names_ignore_case(self):
        for name in NAMES:
            with self.subTest(name=name):
                self.assertTrue(secrets.protected_in_command("cat /tmp/x/" + name.upper()))
        self.assertIsNone(secrets.protected_in_command("cat /tmp/x/notes.GIT-CREDENTIALS.txt"))


class Readers(Case):
    def test_case_and_trim_variants_are_refused_by_every_reader(self):
        for name in NAMES:
            for variant in (name.upper(), name + " ", name + "."):
                path = self.folder() / variant
                path.write_text(SENTINEL + "\n")
                self.assert_unreadable(path, f"{name} as {variant!r}")

    def test_a_symlink_alias_is_refused_by_every_reader(self):
        for name in NAMES:
            d = self.folder()
            real = d / name
            real.write_text(SENTINEL + "\n")
            alias = d / "ordinary.txt"
            alias.symlink_to(real)
            self.assert_unreadable(alias, f"alias to {name}")
            hop2 = d / "notes.md"
            hop2.symlink_to(alias)
            self.assert_unreadable(hop2, f"two hops to {name}")
            upper = d / "cased.txt"
            other = self.folder() / name.upper()
            other.write_text(SENTINEL + "\n")
            upper.symlink_to(other)
            self.assert_unreadable(upper, f"alias to {name.upper()}")

    def test_a_glob_over_a_plain_folder_holding_one_is_refused(self):
        for name in NAMES:
            d = self.folder()
            (d / name.upper()).write_text(SENTINEL + "\n")
            (d / "readme.txt").write_text("ordinary\n")
            with self.subTest(name=name):
                out = tool_result("run_readonly", {"command": "cat " + shlex.quote(str(d)) + "/*"})
                self.assertNotIn(SENTINEL, out)

    def test_an_unfollowable_symlink_is_refused(self):
        d = self.folder()
        loop = d / "loop.txt"
        loop.symlink_to(loop)
        self.assertTrue(secrets.is_credential(loop))


class StillReadable(Case):
    def test_ordinary_files_aliases_and_globs_still_read(self):
        d = self.folder()
        plain = d / "notes.git-credentials.txt"
        plain.write_text(SENTINEL + "\n")
        self.assert_readable(plain, "lookalike name")
        target = d / "data.txt"
        target.write_text(SENTINEL + "\n")
        alias = d / "link.txt"
        alias.symlink_to(target)
        self.assert_readable(alias, "ordinary alias")
        out = tool_result("run_readonly", {"command": "cat " + shlex.quote(str(d)) + "/*.txt"})
        self.assertIn(SENTINEL, out)
        out = tool_result("grep_files", {"pattern": "sentinel", "path": str(d)})
        self.assertIn(SENTINEL, out)

    def test_plain_recursive_grep_of_an_ordinary_folder_still_runs(self):
        d = self.folder()
        (d / "sub").mkdir()
        (d / "sub" / "a.py").write_text(SENTINEL + "\n")
        out = tool_result("run_readonly", {"command": "grep -r sentinel " + shlex.quote(str(d))})
        self.assertIn(SENTINEL, out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
