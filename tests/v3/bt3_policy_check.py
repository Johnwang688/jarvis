"""Pin the complete BT3 default-policy taint table."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from jarvis.v3 import policy


# Columns are tainted=True, tainted=False, and taint missing. For existing
# kinds these rows were generated from origin/main's policy.py.
EXPECTED = {
    'app.open': (('L2', 'hard:tainted'), ('L0', 'default'), ('L2', 'hard:tainted')),
    'calendar.add_due_date': (('L2', 'hard:tainted'), ('L1', 'default'), ('L2', 'hard:tainted')),
    'calendar.block': (('L2', 'hard:tainted'), ('L0', 'default'), ('L2', 'hard:tainted')),
    'calendar.block_move': (('L2', 'hard:tainted'), ('L1', 'default'), ('L2', 'hard:tainted')),
    'calendar.event_change': (('L2', 'default'), ('L2', 'default'), ('L2', 'default')),
    'calendar.event_delete': (('L2', 'default'), ('L2', 'default'), ('L2', 'default')),
    'calendar.invite': (('L2', 'default'), ('L2', 'default'), ('L2', 'default')),
    'calendar.respond': (('L2', 'default'), ('L2', 'default'), ('L2', 'default')),
    'credential.read': (('L3', 'hard:never'), ('L3', 'hard:never'), ('L3', 'hard:never')),
    'credential.write': (('L3', 'hard:never'), ('L3', 'hard:never'), ('L3', 'hard:never')),
    'data.delete_others': (('L3', 'hard:never'), ('L3', 'hard:never'), ('L3', 'hard:never')),
    'fixture.unknown': (('L2', 'default-level'), ('L2', 'default-level'), ('L2', 'default-level')),
    'folder.open': (('L2', 'hard:tainted'), ('L0', 'default'), ('L2', 'hard:tainted')),
    'gate.change': (('L3', 'hard:never'), ('L3', 'hard:never'), ('L3', 'hard:never')),
    'investigation.readonly': (('L2', 'hard:tainted'), ('L0', 'default'), ('L2', 'hard:tainted')),
    'link.open': (('L2', 'hard:tainted'), ('L1', 'default'), ('L2', 'hard:tainted')),
    'mail.archive': (('L2', 'hard:tainted'), ('L0', 'default'), ('L2', 'hard:tainted')),
    'mail.draft': (('L2', 'hard:tainted'), ('L0', 'default'), ('L2', 'hard:tainted')),
    'mail.file': (('L2', 'hard:tainted'), ('L0', 'default'), ('L2', 'hard:tainted')),
    'mail.label': (('L2', 'hard:tainted'), ('L0', 'default'), ('L2', 'hard:tainted')),
    'mail.reply': (('L2', 'default'), ('L2', 'default'), ('L2', 'default')),
    'mail.send': (('L2', 'default'), ('L2', 'default'), ('L2', 'default')),
    'money.spend': (('L2', 'default'), ('L2', 'default'), ('L2', 'default')),
    'policy.write': (('L3', 'hard:never'), ('L3', 'hard:never'), ('L3', 'hard:never')),
    'pr.draft_open': (('L2', 'hard:tainted'), ('L1', 'default'), ('L2', 'hard:tainted')),
    'pr.merge': (('L2', 'default'), ('L2', 'default'), ('L2', 'default')),
    'research.start': (('L2', 'hard:tainted'), ('L0', 'default'), ('L2', 'hard:tainted')),
    'school.submit': (('L3', 'hard:never'), ('L3', 'hard:never'), ('L3', 'hard:never')),
    'thread.fix_test': (('L2', 'hard:tainted'), ('L1', 'default'), ('L2', 'hard:tainted')),
    'thread.send': (('L2', 'default'), ('L2', 'default'), ('L2', 'default')),
    'thread.start': (('L2', 'default'), ('L2', 'default'), ('L2', 'default')),
    'vault.archive': (('L2', 'default'), ('L2', 'default'), ('L2', 'default')),
    'vault.update': (('L2', 'hard:tainted'), ('L0', 'default'), ('L2', 'hard:tainted')),
    'browser.follow': (('L0', 'default'), ('L0', 'default'), ('L0', 'default')),
    'browser.open': (('L2', 'hard:tainted'), ('L0', 'default'), ('L2', 'hard:tainted')),
    'browser.read': (('L0', 'default'), ('L0', 'default'), ('L0', 'default')),
    'browser.sign_in': (('L1', 'default'), ('L1', 'default'), ('L1', 'default')),
}


class BrowserPolicyCheck(unittest.TestCase):
    def setUp(self):
        self.path = Path(self.enterContext(TemporaryDirectory())) / "policy.json"
        self.policy = policy.Policy(self.path)

    def test_before_and_after(self):
        self.assertEqual(EXPECTED.keys(), policy.DEFAULTS.keys() | {"fixture.unknown"})
        for kind, expected in EXPECTED.items():
            facts = {"kind": kind, "money": False}
            if kind.startswith("calendar."):
                facts.update(attendees=0, owner_event=False)
            for taint, (level, why) in zip(({"tainted": True}, {"tainted": False}, {}), expected):
                with self.subTest(kind=kind, taint=taint):
                    why = f"default:{kind}" if why == "default" else why
                    self.assertEqual(self.policy.explain({**facts, **taint}), dict(level=level, why=why))

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
