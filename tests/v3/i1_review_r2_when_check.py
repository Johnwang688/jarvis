"""I1 review round 2: a fact of the wrong type now silently skips a STRICTER owner
rule. Before the fix it was "unjudgeable" (L2); the contract says anything it
cannot judge is L2."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from jarvis.v3.policy import Policy


class StricterRuleTypeMismatch(unittest.TestCase):
    def test_a_mistyped_fact_does_not_skip_a_stricter_rule(self):
        tmp = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        path = tmp / "policy.json"
        path.write_text(json.dumps({"version": 1, "rules": [
            {"kind": "vault.update", "level": "L2", "when": {"sensitive": True}}]}))
        path.chmod(0o600)
        p = Policy(path)
        self.assertEqual(p.level_for({"kind": "vault.update", "tainted": False, "sensitive": True}), "L2")
        for odd in ("true", 1, [True]):
            with self.subTest(sensitive=odd):
                # Cannot tell whether the owner's "ask" condition holds: ask, do not fall to L0.
                self.assertEqual(p.level_for({"kind": "vault.update", "tainted": False, "sensitive": odd}), "L2")


if __name__ == "__main__":
    unittest.main()
