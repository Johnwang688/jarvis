"""Compare owner-request decisions with the policy on origin/main."""

from importlib import import_module, util
from itertools import product
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from jarvis.v3 import policy

from bt3_policy_check import BT4_KINDS, NEW_EXEMPT


MISSING = object()
# The four BT3 kinds have their own before-and-after table.
BT3_KINDS = {"browser.open", "browser.read", "browser.follow", "browser.sign_in"}
FACT_AXES = {
    "tainted": (True, False, MISSING, 0),
    "owner_request": (True, False, MISSING, 1, "true"),
    "attendees": (0, 1, MISSING),
    "owner_event": (False, True, MISSING),
    "money": (False, True, MISSING),
}


def baseline_policy(test):
    source = os.environ.get("P1_MAIN_FILES")
    if not source:
        test.skipTest("set P1_MAIN_FILES to the archived origin/main jarvis/v3 directory")
    spec = util.spec_from_file_location("p1_main", Path(source) / "__init__.py")
    package = util.module_from_spec(spec)
    test.enterContext(patch.dict(sys.modules, {"p1_main": package}))
    spec.loader.exec_module(package)
    return import_module("p1_main.policy")


def fact_grid(kinds):
    for kind, values in product(kinds, product(*FACT_AXES.values())):
        yield {"kind": kind, **{key: value for key, value in zip(FACT_AXES, values) if value is not MISSING}}


def bt3c_baseline_facts(proposal):
    return dict(proposal, tainted=False) if proposal["kind"] in NEW_EXEMPT else proposal


class PolicyGrid(unittest.TestCase):
    def test_legacy_grid_has_only_the_pinned_owner_request_and_bt3c_deltas(self):
        main = baseline_policy(self)

        path = Path(self.enterContext(TemporaryDirectory())) / "policy.json"
        before, after = main.Policy(path), policy.Policy(path)
        pc_kinds = {"app.open", "folder.open", "link.open"}
        kinds = sorted(((policy.DEFAULTS.keys() - pc_kinds - BT3_KINDS) | main.DEFAULTS.keys() | policy.NEVER | {
            "garden.water", "mail.forward", "share.doc",
        }) - BT4_KINDS)
        owner_levels = {"thread.start": "L1", "thread.send": "L1", "calendar.event_change": "L1"}
        self.assertEqual(policy.OWNER_REQUEST, owner_levels)
        if hasattr(main, "OWNER_REQUEST"):
            # The baseline already carries the owner-request change.
            changes = 0
            for configured in (False, True):
                if configured:
                    policy.set_rule(path, "thread.start", "L3")
                    policy.set_rule(path, "calendar.event_change", "L2", when={"attendees": 1})
                for proposal in fact_grid(kinds):
                    actual = after.explain(proposal)
                    self.assertEqual(actual, before.explain(bt3c_baseline_facts(proposal)), (configured, proposal))
                    changes += actual != before.explain(proposal)
            print(f"Policy grid differing cells: {changes} (BT3c exemptions).", flush=True)
            return
        differences = []
        archive_differences = 0
        for configured in (False, True):
            file_kinds = set()
            if configured:
                policy.set_rule(path, "thread.start", "L3")
                policy.set_rule(path, "calendar.event_change", "L2", when={"attendees": 1})
                file_kinds.update(("thread.start", "calendar.event_change"))
            changed = 0
            for proposal in fact_grid(kinds):
                kind = proposal["kind"]
                old, new = before.explain(bt3c_baseline_facts(proposal)), after.explain(proposal)
                if kind == "vault.archive":
                    self.assertEqual(old, {"level": "L2", "why": "default-level"}, proposal)
                    self.assertEqual(new, {"level": "L2", "why": "default:vault.archive"}, proposal)
                    archive_differences += new != old
                else:
                    hard_floor = main._apply_hard_rules({"level": "L1", "why": "probe"}, kind, proposal)
                    owner_override = (proposal.get("owner_request") is True and proposal.get("tainted") is False
                                      and kind not in file_kinds and kind in owner_levels
                                      and hard_floor["level"] == "L1")
                    expected = {"level": owner_levels[kind], "why": f"owner-request:{kind}"} if owner_override else old
                    self.assertEqual(new, expected, (configured, proposal))
                    self.assertEqual(new != old, owner_override, (configured, proposal))
                    changed += new != old
            differences.append(changed)

        self.assertEqual(differences, [38, 18])
        print(f"Policy grid differing cells: {sum(differences) + archive_differences} "
              f"({archive_differences} for vault.archive and {sum(differences)} for legacy kinds: "
              f"{differences[0]} without a file, {differences[1]} with a file).", flush=True)


if __name__ == "__main__":
    unittest.main()
