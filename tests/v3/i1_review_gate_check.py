"""I1 review: an agent must not be able to change the decision-rights policy.

Plan section 13: `policy.write` is L3 (owner only) and "a change takes effect only
through John's tap, never by the agent writing the file". The file itself is
protected state, but `jarvis policy set` writes it from inside Python, so the
shell gate sees only "runs the jarvis CLI": ASK, answered yes by an existing
`jarvis` allowlist entry or by mode "all". The other owner CLI writers
(`jarvis hud install`, `jarvis agent start`) are DENY through OWNER_OPS.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import config, permissions, protected_state, rules

SET = "jarvis policy set pr.merge L0"


class PolicySetIsAnOwnerOp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))

    def test_policy_set_is_refused_to_agents_like_other_owner_writers(self):
        self.assertEqual(rules.decide("jarvis hud install").decision, rules.DENY)  # the precedent
        for command in (SET, "python -m jarvis policy set mail.draft L0",
                        "/usr/bin/env jarvis policy set thread.start L1"):
            with self.subTest(command=command):
                self.assertTrue(protected_state.owner_op(command), command)
                self.assertEqual(permissions.static_verdict(command).decision, rules.DENY)

    def test_a_jarvis_allowlist_entry_does_not_cover_policy_set(self):
        allow = self.tmp / "allowlist.json"
        allow.write_text(json.dumps([{"tool": "run_command", "prefix": "jarvis"}]))
        policy_file = self.tmp / "v3" / "policy.json"
        with patch.object(config, "ALLOWLIST_PATH", allow), patch.object(config, "V3_POLICY_FILE", policy_file):
            # A direct write to the policy file is never covered by an entry ...
            self.assertFalse(permissions.allows("run_command", {"command": f"cp /tmp/x {policy_file}"}))
            # ... and neither may the CLI that writes the same file.
            self.assertFalse(permissions.allows("run_command", {"command": SET}))


if __name__ == "__main__":
    unittest.main()
