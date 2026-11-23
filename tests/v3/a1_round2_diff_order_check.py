"""(A1 verifier r2, orchestrator-owned.) A stale local diff order file must leave the freeze report unchanged."""

import unittest

import a1_review_check as fixtures


class DiffOrder(fixtures.Report):
    def test_missing_local_order_file_does_not_break_report(self):
        base = self.commit(self.tree(), "baseline")
        blob = self.object("blob", b"fixture\n")
        hud = self.tree("40000", "hud", self.tree("100644", "archive", blob))
        head = self.commit(hud, "archive change", base)
        self.set_range(base, head)
        expected = (1, f"{head[:7]} archive change\n  frozen: hud/archive\n", "")
        clean = self.report()
        self.assertEqual((clean.returncode, clean.stdout, clean.stderr), expected)
        with self.config.open("a") as config:
            config.write(f'[diff]\n\torderFile = "{self.repo / "missing-order"}"\n')
        result = self.report()
        self.assertEqual((result.returncode, result.stdout, result.stderr), expected)


if __name__ == "__main__":
    unittest.main()
