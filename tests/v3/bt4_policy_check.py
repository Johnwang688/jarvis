"""Compare the BT4 policy with the origin/main snapshot supplied by the test runner."""

from itertools import product
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from bt3_policy_check import BT4_KINDS
from r1a_policy_grid_check import baseline_policy, fact_grid
from jarvis.v3 import policy


MISSING = object()
BOOLEANS = (True, False, MISSING, 0, "false", None)


def optional(name, value):
    return {} if value is MISSING else {name: value}


class ActionPolicy(unittest.TestCase):
    def setUp(self):
        self.path = Path(self.enterContext(TemporaryDirectory())) / "policy.json"
        self.after = policy.Policy(self.path)

    def test_legacy_before_and_after(self):
        main = baseline_policy(self)
        kinds = (main.DEFAULTS.keys() | main.OTHER_PEOPLE | {"browser.future", "share.doc", "fixture.unknown"}
                 ) - BT4_KINDS
        self.assertEqual(main.DEFAULTS.keys() - BT4_KINDS, policy.DEFAULTS.keys() - BT4_KINDS)
        for kind, level in [("browser.follow", "L3"), ("browser.open", "L1"), ("mail.send", "L2")]:
            policy.set_rule(self.path, kind, level)
        after_file = self.after._snapshot()
        before_file = main.Policy(self.path)._snapshot()
        grids = [(main._empty_file(), policy._empty_file()), (before_file, after_file)]
        cells = 0
        for facts in fact_grid(sorted(kinds)):
            for address in BOOLEANS:
                proposed = {**facts, **optional("owner_address", address)}
                for before, after in grids:
                    self.assertEqual(main._decide(before, facts["kind"], proposed),
                                     policy._decide(after, facts["kind"], proposed), proposed)
                    cells += 1
        print(f"BT4 before/after policy table: {cells:,} existing-kind verdicts unchanged.", flush=True)

    def test_new_kinds(self):
        cells = 0
        for base, address in product(fact_grid(sorted(BT4_KINDS)), BOOLEANS):
            kind = base["kind"]
            proposed = {**base, **optional("owner_address", address)}
            self.assertEqual(self.after.explain(proposed), {"level": "L2", "why": f"default:{kind}"}, proposed)
            self.assertFalse(policy.taint_exempt(kind, proposed), proposed)
            cells += 1
        print(f"BT4 new-kind policy table: {cells:,} verdicts checked.", flush=True)

    def test_browser_floors_allow_ask_or_block(self):
        for kind in BT4_KINDS:
            for level in ("L0", "L1"):
                with self.assertRaises(ValueError):
                    policy.set_rule(self.path, kind, level)
            policy.set_rule(self.path, kind, "L3")
            self.assertEqual(self.after.level_for({"kind": kind}), "L3")

    def test_existing_policy_with_an_unknown_clause_loads_like_main(self):
        main = baseline_policy(self)
        # An earlier draft treated this arbitrary policy key as a boolean.
        # Existing files accept primitive values for unknown keys.
        self.assertEqual(policy._FACT_TYPES, main._FACT_TYPES)
        for value in (True, False, 0, "false", None):
            with self.subTest(value=value):
                data = main._empty_file()
                data["rules"] = [{"kind": "browser.follow", "level": "L3", "when": {"signed_site": value}}]
                self.path.write_text(json.dumps(data))
                self.path.chmod(0o600)
                self.assertEqual(main.Policy(self.path)._snapshot(), data)
                self.assertEqual(self.after._snapshot(), data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
