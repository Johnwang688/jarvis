"""Only a user-owned regular executable should be eligible to run."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jarvis.v3.t3_client import T3, T3Error, resolve


class BinaryTrust(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home)}, clear=True))
        self.binary = self.home / "fake-cli"
        self.binary.write_text("#!/bin/sh\nprintf '[]'\n", encoding="utf-8")
        self.binary.chmod(0o700)

    def test_runtime_symlink_is_skipped(self):
        link = self.home / ".t3" / "wsl-runtime" / "fake" / "t3"
        link.parent.mkdir(parents=True)
        link.symlink_to(self.binary)
        with self.assertRaises(T3Error) as caught:
            resolve(home=self.home, env={})
        self.assertEqual(caught.exception.kind, "missing")

    def test_explicit_symlink_is_refused_before_launch(self):
        link = self.home / "linked-cli"
        link.symlink_to(self.binary)
        with self.assertRaises(T3Error) as caught:
            T3(link).list(".")
        self.assertEqual(caught.exception.kind, "missing")

    def test_foreign_owner_is_refused_before_launch(self):
        original_stat = Path.stat
        metadata = list(original_stat(self.binary))
        metadata[4] = os.geteuid() + 1  # st_uid in the stat_result sequence
        foreign = os.stat_result(metadata)

        def stat_with_foreign_owner(path, *args, **kwargs):
            if path == self.binary:
                return foreign
            return original_stat(path, *args, **kwargs)

        with patch.object(Path, "stat", stat_with_foreign_owner):
            with self.assertRaises(T3Error) as caught:
                T3(self.binary).list(".")
        self.assertEqual(caught.exception.kind, "missing")


if __name__ == "__main__":
    unittest.main()
