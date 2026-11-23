"""A1 review regressions (verifier r1, orchestrator-owned; the fixer must not edit this file).

Detached Git fixtures without an index."""

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zlib


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/v2_freeze_report.py"


class Report(unittest.TestCase):
    def setUp(self):
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.repo / "config"
        self.config.write_text("[core]\n\trepositoryformatversion = 0\n\tbare = true\n")
        (self.repo / "refs/heads").mkdir(parents=True)

    def object(self, kind, payload):
        data = f"{kind} {len(payload)}\0".encode() + payload
        oid = hashlib.sha1(data).hexdigest()
        path = self.object_path(oid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(zlib.compress(data))
        return oid

    def object_path(self, oid):
        return self.repo / "objects" / oid[:2] / oid[2:]

    def tree(self, mode=None, name=None, oid=None):
        entry = b"" if mode is None else f"{mode} {name}\0".encode() + bytes.fromhex(oid)
        return self.object("tree", entry)

    def commit(self, tree, subject, parent=None):
        identity = "Fixture <fixture@example.invalid> 1700000000 +0000"
        headers = [f"tree {tree}"]
        if parent is not None:
            headers.append(f"parent {parent}")
        headers += [f"author {identity}", f"committer {identity}"]
        return self.object("commit", ("\n".join(headers) + f"\n\n{subject}\n").encode())

    def set_range(self, base, head):
        (self.repo / "refs/heads/baseline").write_text(base + "\n")
        (self.repo / "HEAD").write_text(head + "\n")

    def report(self):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", str(self.repo), "--since", "baseline", "--check"],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
            env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]},
        )

    def test_local_ignore_submodules_keeps_frozen_change(self):
        before = self.tree("40000", "hud", self.tree("160000", "vendor", "1" * 40))
        after = self.tree("40000", "hud", self.tree("160000", "vendor", "2" * 40))
        base = self.commit(before, "baseline")
        head = self.commit(after, "update HUD dependency", base)
        self.set_range(base, head)
        expected = (1, f"{head[:7]} update HUD dependency\n  frozen: hud/vendor\n", "")
        clean_config = self.report()
        self.assertEqual((clean_config.returncode, clean_config.stdout, clean_config.stderr), expected)
        with self.config.open("a") as config:
            config.write("[diff]\n\tignoreSubmodules = all\n")
        result = self.report()
        self.assertEqual((result.returncode, result.stdout, result.stderr), expected)

    def test_shallow_gap_cannot_pass_freeze_check(self):
        empty = self.tree()
        blob = self.object("blob", b"fixture\n")
        frozen = self.tree("40000", "hud", self.tree("100644", "archived", blob))
        base = self.commit(empty, "baseline")
        missed = self.commit(frozen, "add archived file", base)
        head = self.commit(empty, "remove archived file", missed)
        self.set_range(base, head)
        complete = self.report()
        self.assertEqual(complete.returncode, 1, (complete.stdout, complete.stderr))
        self.assertIn("  frozen: hud/archived\n", complete.stdout)
        (self.repo / "shallow").write_text(head + "\n")
        self.object_path(missed).unlink()
        truncated = self.report()
        self.assertEqual(truncated.returncode, 2, (truncated.stdout, truncated.stderr))
        self.assertTrue(truncated.stderr)

    def test_newline_in_path_stays_on_one_file_line(self):
        base = self.commit(self.tree(), "baseline")
        blob = self.object("blob", b"fixture\n")
        frozen = self.tree("40000", "hud", self.tree("100644", "é space\nchild", blob))
        head = self.commit(frozen, "add one file", base)
        self.set_range(base, head)
        result = self.report()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 2, repr(result.stdout))


if __name__ == "__main__":
    unittest.main()
