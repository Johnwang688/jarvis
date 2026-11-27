"""Pin the complete BT3 default-policy taint table."""

from itertools import product
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from jarvis.v3 import policy


MISSING = object()
TAINT_AND_ADDRESS = (True, False, MISSING)
OTHER_FACTS = {
    "attendees": (0, 1, MISSING),
    "owner_event": (False, True, MISSING),
    "money": (False, True, MISSING),
}

# Generated from origin/main 27980dd9 via git show. Each row groups kinds
# with identical verdicts. Columns are tainted or missing, then clean.
# The 27 characters in each column follow product(*OTHER_FACTS.values()).
# All three owner_address states gave identical verdicts in the baseline.
BEFORE = (
    ("TTTTTTTTTTTTTTTTTTTTTTTTTTT", "0M00M00M00M00M00M00M00M00M0",
     ("app.open", "browser.open", "folder.open", "investigation.readonly", "mail.archive",
      "mail.draft", "mail.file", "mail.label", "research.start", "vault.update",)),
    ("0M00M00M00M00M00M00M00M00M0", "0M00M00M00M00M00M00M00M00M0",
     ("browser.follow", "browser.read",)),
    ("UUUUUUUUUUUUUUUUUUUUUUUUUUU", "UUUUUUUUUUUUUUUUUUUUUUUUUUU",
     ("browser.future", "fixture.unknown", "mail.forward", "share.doc",)),
    ("1M11M11M11M11M11M11M11M11M1", "1M11M11M11M11M11M11M11M11M1",
     ("browser.sign_in",)),
    ("TTTTTTTTTTTTTTTTTTTTTTTTTTT", "1M11M11M1PPPPPPPPPPPPPPPPPP",
     ("calendar.add_due_date",)),
    ("TTTTTTTTTTTTTTTTTTTTTTTTTTT", "0M00M00M0PPPPPPPPPPPPPPPPPP",
     ("calendar.block",)),
    ("TTTTTTTTTTTTTTTTTTTTTTTTTTT", "1M1EMEEMEPPPPPPPPPPPPPPPPPP",
     ("calendar.block_move",)),
    ("222222222222222222222222222", "222222222222222222222222222",
     ("calendar.event_change", "calendar.event_delete", "calendar.invite", "calendar.respond",
      "mail.reply", "mail.send", "money.spend", "pr.merge", "thread.send", "thread.start",
      "vault.archive",)),
    ("NNNNNNNNNNNNNNNNNNNNNNNNNNN", "NNNNNNNNNNNNNNNNNNNNNNNNNNN",
     ("credential.read", "credential.write", "data.delete_others", "gate.change", "policy.write",
      "school.submit",)),
    ("TTTTTTTTTTTTTTTTTTTTTTTTTTT", "1M11M11M11M11M11M11M11M11M1",
     ("link.open", "pr.draft_open", "thread.fix_test",)),
)

HARD_REASONS = {
    "T": ("L2", "hard:tainted"),
    "P": ("L2", "hard:other-people"),
    "M": ("L2", "hard:money"),
    "E": ("L2", "hard:owner-event"),
    "U": ("L2", "default-level"),
    "N": ("L3", "hard:never"),
}
NEW_EXEMPT = frozenset({
    "app.open", "folder.open", "calendar.block", "calendar.block_move", "calendar.add_due_date",
    "mail.file", "mail.label", "mail.archive", "mail.draft",
})
BT4_KINDS = {"browser.click", "browser.submit", "browser.type"}
ADDRESS_KINDS = frozenset({"browser.open", "link.open"})


def verdict(kind, code):
    level, why = (f"L{code}", f"default:{kind}") if code.isdigit() else HARD_REASONS[code]
    return {"level": level, "why": why}


class BrowserPolicyCheck(unittest.TestCase):
    def setUp(self):
        self.path = Path(self.enterContext(TemporaryDirectory())) / "policy.json"
        self.policy = policy.Policy(self.path)

    def test_before_and_after(self):
        kinds = [kind for _, _, group in BEFORE for kind in group]
        self.assertCountEqual(kinds, (policy.DEFAULTS.keys() - BT4_KINDS) | policy.OTHER_PEOPLE |
                              {"fixture.unknown", "browser.future", "share.doc"})
        self.assertEqual(policy.TAINT_EXEMPT, NEW_EXEMPT | {"browser.read", "browser.follow", "browser.sign_in"})
        changes = clean_cells = cells = 0
        for outside, clean, group in BEFORE:
            for index, values in enumerate(product(*OTHER_FACTS.values())):
                for kind, tainted, address in product(group, TAINT_AND_ADDRESS, TAINT_AND_ADDRESS):
                    facts = {"kind": kind, **{
                        key: value for key, value in zip(OTHER_FACTS, values, strict=True) if value is not MISSING
                    }}
                    if tainted is not MISSING:
                        facts["tainted"] = tainted
                    if address is not MISSING:
                        facts["owner_address"] = address
                    before = verdict(kind, (clean if tainted is False else outside)[index])
                    # The exemptions remove only the taint raise, exposing the same
                    # remaining hard rules as a clean cell with identical facts.
                    exempt = kind in NEW_EXEMPT or (kind in ADDRESS_KINDS and address is True)
                    expected = verdict(kind, clean[index]) if exempt else before
                    with self.subTest(facts=facts):
                        actual = self.policy.explain(facts)
                        self.assertEqual(actual, expected)
                        if tainted is False:
                            self.assertEqual(actual, before)
                    cells += 1
                    changes += actual != before
                    clean_cells += tainted is False
        print(f"BT3 policy table: {cells} cells, {changes} changed verdicts, {clean_cells} clean cells unchanged.",
              flush=True)

    def test_owner_address_must_be_a_boolean(self):
        for kind, value in product(ADDRESS_KINDS, (1, "true", None)):
            with self.subTest(kind=kind, value=value):
                facts = {"kind": kind, "tainted": True, "owner_address": value}
                self.assertEqual(self.policy.explain(facts), {"level": "L2", "why": "hard:tainted"})
                with self.assertRaises(ValueError):
                    policy.set_rule(self.path, kind, "L2", {"owner_address": value})

    def test_owner_rules_cannot_reduce_the_browser_floors(self):
        for kind, level, when in (
            ("browser.sign_in", "L0", {}),
            ("browser.future", "L1", {"tainted": False}),
            ("browser.open", "L0", {"tainted": True}),
            ("browser.open", "L1", {"tainted": True}),
        ):
            with self.subTest(kind=kind, level=level), self.assertRaises(ValueError):
                policy.set_rule(self.path, kind, level, when)
        self.assertFalse(self.path.exists())
        self.assertEqual(policy.set_rule(self.path, "browser.open", "L0"), ("L0", "L0"))
        for taint in ({"tainted": True}, {}):
            self.assertEqual(self.policy.explain({"kind": "browser.open", **taint}),
                             {"level": "L2", "why": "hard:tainted"})
        self.assertEqual(policy.set_rule(self.path, "browser.sign_in", "L2"), ("L1", "L2"))
        self.assertEqual(self.policy.level_for({"kind": "browser.sign_in", "tainted": True}), "L2")
        self.assertEqual(self.policy.level_for({"kind": "browser.read", "tainted": True, "money": True}), "L2")

    def test_hand_written_rules_respect_floors_only_for_browser_kinds(self):
        rules = [{"kind": kind, "level": "L0"} for kind in
                 ("browser.sign_in", "browser.future", "browser.open", "link.open", "vault.archive")]
        self.path.write_text(json.dumps({"version": 1, "rules": rules}))
        self.path.chmod(0o600)
        for taint in ({"tainted": True}, {"tainted": False}, {}):
            with self.subTest(taint=taint):
                self.assertEqual(self.policy.explain({"kind": "browser.sign_in", **taint}),
                                 {"level": "L1", "why": "hard:browser-floor"})
                self.assertEqual(self.policy.level_for({"kind": "browser.future", **taint}), "L2")
                level = "L0" if taint.get("tainted") is False else "L2"
                for kind in ("browser.open", "link.open", "vault.archive"):
                    self.assertEqual(self.policy.level_for({"kind": kind, **taint}), level)


if __name__ == "__main__":
    unittest.main()
