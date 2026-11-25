"""I1 review: rule-order and fact-handling defects in jarvis/v3/policy.py."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from jarvis.v3.policy import Policy, set_rule


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        self.path = self.tmp / "policy.json"
        self.p = Policy(self.path)

    def write(self, data):
        self.path.write_text(json.dumps(data))
        self.path.chmod(0o600)


class SetKeepsStricterConditions(Base):
    def test_unconditional_set_does_not_shadow_a_stricter_conditional_rule(self):
        # The owner hand-wrote "ask before filing school mail", then lowered the kind in general.
        self.write({"version": 1, "rules": [{"kind": "mail.file", "level": "L2", "when": {"label": "school"}}]})
        school = {"kind": "mail.file", "tainted": False, "label": "school"}
        self.assertEqual(self.p.level_for(school), "L2")
        set_rule(self.path, "mail.file", "L1")   # "replace the same selector": a different selector
        # The new unconditional rule is put first, so the owner's condition is dead.
        self.assertEqual(self.p.level_for(school), "L2")


class ConditionalRuleFactsMissing(Base):
    def test_a_permissive_condition_does_not_make_other_proposals_ask(self):
        # Contract: first rule whose `when` facts all equal the proposal's, else DEFAULTS[kind].
        self.write({"version": 1, "rules": [{"kind": "pr.draft_open", "level": "L0", "when": {"repo": "jarvis"}}]})
        self.assertEqual(self.p.level_for({"kind": "pr.draft_open", "tainted": False, "repo": "jarvis"}), "L0")
        # A proposal without `repo` does not match the rule, so the table's L1 applies; the code asks.
        self.assertEqual(self.p.level_for({"kind": "pr.draft_open", "tainted": False}), "L1")


class OddKeys(Base):
    def test_a_money_fact_under_a_str_subclass_key_is_not_silently_dropped(self):
        class Key(str):
            pass
        proposal = {"kind": "vault.update", "tainted": False, Key("money"): True}
        self.assertEqual(self.p.level_for(proposal), "L2")


if __name__ == "__main__":
    unittest.main()
