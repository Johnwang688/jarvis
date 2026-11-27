"""Orchestrator-owned acceptance tests for I1 (implementers: do not edit; if one is wrong, stop and say why).

Contract: the decision-rights policy (plan §13, approved by the owner 2026-11-24 with two explicit lines: sending mail asks, drafting does not).
  jarvis/v3/policy.py
    LEVELS = ("L0", "L1", "L2", "L3")       L0 do · L1 do and tell · L2 ask · L3 never (owner only)
    DEFAULTS: dict[kind, level]              the approved table (see Table below); a kind is "area.verb", lowercase
    Policy(path=None)                        path default config.V3_POLICY_FILE, read on every call (contract 1.9); never written by it
      level_for(proposal) -> level           proposal: {"kind": str, "tainted": bool, ...facts}; never raises: anything it cannot judge is L2
      explain(proposal) -> {"level", "why"}  why names the rule that decided: "hard:<name>", "file:<index>", "default:<kind>", "default-level", "malformed-file"
    Order: a file rule for the kind (first match whose `when` facts all equal the proposal's) else DEFAULTS[kind] else the file's
    default_level (L2 when absent). THEN the hard rules, which only ever raise a level and which no file can change:
      hard:never         kinds in NEVER (credentials, the policy itself, the gate, schoolwork submissions, deleting others' data) -> L3
      hard:tainted       taint true, missing or invalid -> at least L2 for actions subject to the taint floor
      hard:other-people  kinds in OTHER_PEOPLE (mail.send, mail.reply, mail.forward, calendar.invite, calendar.respond, share.*) -> at least L2;
                         also any calendar proposal whose attendees is not exactly the int 0 (missing, a bool, negative, a string: all ask)
      hard:money         kinds in MONEY, or proposal money truthy -> at least L2
      hard:owner-event   a calendar change or delete with owner_event not exactly False -> at least L2
      hard:unknown-kind  a kind in neither DEFAULTS nor a hard set can be at most as permissive as L2, whatever the file says
    A file that is missing is the defaults. A file that is malformed in any way (not JSON, wrong types, unknown level, version != 1,
    a link, not owner-only, over 256 KiB) makes every proposal at least L2 ("malformed-file"); NEVER kinds stay L3.
    default_level below L2 in the file is ignored (L2).
    set_rule(path, kind, level, when=None)   the owner CLI's writer: validates, writes atomically (0600), refuses a rule the hard
                                             rules would override (so the file never claims more than it grants), never called by an agent tool
  jarvis policy show | jarvis policy set <kind> <level>     owner-run CLI in jarvis/v3/cli.py
  Every level still needs an approval record at execution (H10): this module only classifies; nothing here executes.
"""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from jarvis.v3 import policy
from jarvis.v3.policy import DEFAULTS, Policy

TABLE = {
    "L0": ["mail.file", "mail.label", "mail.archive", "mail.draft", "vault.update", "research.start", "investigation.readonly", "calendar.block"],
    "L1": ["calendar.block_move", "pr.draft_open", "thread.fix_test", "calendar.add_due_date"],
    "L2": ["mail.send", "mail.reply", "calendar.respond", "calendar.invite", "calendar.event_change", "calendar.event_delete", "money.spend", "pr.merge", "thread.start", "thread.send"],
    "L3": ["credential.read", "credential.write", "policy.write", "gate.change", "school.submit", "data.delete_others"],
}


def prop(kind, **facts):
    base = {"kind": kind, "tainted": False}
    if kind.startswith("calendar."):
        base.update(attendees=0, owner_event=False)
    return {**base, **facts}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        self.path = self.tmp / "policy.json"
        self.p = Policy(self.path)

    def write(self, data, mode=0o600):
        self.path.write_text(data if isinstance(data, str) else json.dumps(data))
        self.path.chmod(mode)

    def level(self, kind, **facts):
        return self.p.level_for(prop(kind, **facts))


class Defaults(Base):
    def test_the_approved_table(self):
        for level, kinds in TABLE.items():
            for kind in kinds:
                self.assertEqual(self.level(kind), level, kind)
                self.assertEqual(DEFAULTS.get(kind, "L3" if level == "L3" else None), level, kind)
        self.assertEqual((self.level("mail.send"), self.level("mail.draft")), ("L2", "L0"))        # the owner's two explicit lines
        self.assertEqual(policy.LEVELS, ("L0", "L1", "L2", "L3"))
        self.assertEqual(self.p.explain(prop("mail.draft"))["why"], "default:mail.draft")

    def test_unknown_and_unjudgeable_proposals_ask(self):
        self.assertEqual(self.level("garden.water"), "L2")
        for bad in (None, "mail.draft", [], {}, {"kind": 5}, {"kind": ""}, {"kind": "Mail.Draft", "tainted": False}, {"kind": "mail.draft\n", "tainted": False},
                    {"kind": "vault.update"}, {"kind": "vault.update", "tainted": "no"}, {"kind": "vault.update", "tainted": 0}):
            self.assertIn(self.p.level_for(bad), ("L2", "L3"), repr(bad))
        self.assertEqual(self.p.level_for({"kind": "mail.draft", "tainted": False}), "L0")


class Hard(Base):
    PERMISSIVE = {"version": 1, "default_level": "L0",
                  "rules": [{"kind": k, "level": "L0"} for k in ("mail.send", "mail.reply", "mail.forward", "calendar.invite", "calendar.respond", "share.doc", "money.spend",
                                                              "credential.read", "policy.write", "gate.change", "school.submit", "data.delete_others",
                                                              "calendar.event_change", "calendar.event_delete", "calendar.block", "vault.update", "garden.water")]}

    def test_a_permissive_file_cannot_lower_a_hard_rule(self):
        self.write(self.PERMISSIVE)
        for kind in ("mail.send", "mail.reply", "mail.forward", "calendar.invite", "calendar.respond", "share.doc"):
            self.assertEqual(self.level(kind), "L2", kind)
            self.assertEqual(self.p.explain(prop(kind))["why"], "hard:other-people")
        self.assertEqual(self.level("money.spend"), "L2")
        self.assertEqual(self.level("vault.update", money=True), "L2")
        for kind in TABLE["L3"]:
            self.assertEqual(self.level(kind), "L3", kind)
            self.assertEqual(self.p.explain(prop(kind))["why"], "hard:never")
        self.assertEqual(self.level("garden.water"), "L2")                       # an unknown kind is never below ask
        self.assertEqual(self.p.explain(prop("garden.water"))["why"], "hard:unknown-kind")
        self.assertEqual(self.level("vault.update"), "L0")                       # a file may still set what the hard rules leave alone

    def test_taint_floor_has_explicit_exemptions(self):
        self.write(self.PERMISSIVE)
        for kind in ("vault.update", "research.start", "garden.water"):
            self.assertEqual(self.level(kind, tainted=True), "L2", kind)
            self.assertEqual(self.p.explain(prop(kind, tainted=True))["why"], "hard:tainted")
        for kind in ("mail.draft", "calendar.block", "mail.file"):
            self.assertEqual(self.level(kind, tainted=True), "L0", kind)
        self.assertEqual(self.level("school.submit", tainted=True), "L3")

    def test_calendar_facts(self):
        self.write(self.PERMISSIVE)
        self.assertEqual(self.level("calendar.block"), "L0")
        for facts in ({"attendees": 1}, {"attendees": "0"}, {"attendees": None}, {"attendees": True}, {"attendees": -1}):
            self.assertEqual(self.level("calendar.block", **facts), "L2", facts)
        missing = {"kind": "calendar.block", "tainted": False, "owner_event": False}
        self.assertEqual(self.p.level_for(missing), "L2")
        for kind in ("calendar.event_change", "calendar.event_delete"):
            self.assertEqual(self.level(kind, owner_event=True), "L2")
            self.assertEqual(self.level(kind, owner_event=None), "L2")
            self.assertEqual(self.level(kind, owner_event=False), "L0")          # Jarvis's own event, and the file allows it
        self.assertEqual(self.p.explain(prop("calendar.event_delete", owner_event=True))["why"], "hard:owner-event")


class File(Base):
    def test_rules_and_when(self):
        self.write({"version": 1, "default_level": "L3",
                    "rules": [{"kind": "calendar.block", "level": "L2", "when": {"attendees": 0, "owner_event": True}},
                              {"kind": "calendar.block", "level": "L1"}, {"kind": "mail.file", "level": "L2"}, {"kind": "pr.draft_open", "level": "L0"}]})
        self.assertEqual(self.level("calendar.block"), "L1")
        self.assertEqual(self.p.explain(prop("calendar.block"))["why"], "file:1")
        self.assertEqual(self.level("mail.file"), "L2")                          # a file may always be stricter
        self.assertEqual(self.level("pr.draft_open"), "L0")
        self.assertEqual(self.level("garden.water"), "L3")                       # default_level may be stricter than ask
        self.assertEqual(self.level("mail.draft"), "L0")                         # an approved default still applies
        self.write({"version": 1, "rules": []})
        self.assertEqual(self.level("garden.water"), "L2")
        self.write({"version": 1, "default_level": "L0", "rules": []})
        self.assertEqual(self.level("garden.water"), "L2")

    def test_a_malformed_file_fails_closed(self):
        bad = ["{", "[]", "null", json.dumps({"version": 2, "rules": []}), json.dumps({"version": 1, "rules": "x"}), json.dumps({"version": 1, "rules": [{"kind": "mail.file"}]}),
               json.dumps({"version": 1, "rules": [{"kind": "mail.file", "level": "L9"}]}), json.dumps({"version": 1, "rules": [{"kind": 5, "level": "L0"}]}),
               json.dumps({"version": 1, "rules": [{"kind": "mail.file", "level": "L0", "when": "x"}]}), json.dumps({"version": 1, "rules": [], "default_level": "low"}),
               json.dumps({"version": 1, "rules": [{"kind": "mail.file", "level": "L0", "extra": 1}]}), "\x00", " " * (300 * 1024)]
        for text in bad:
            self.write(text)
            self.assertEqual(self.level("mail.draft"), "L2", text[:50])
            self.assertEqual(self.p.explain(prop("mail.draft"))["why"], "malformed-file")
            self.assertEqual(self.level("school.submit"), "L3")
        self.write({"version": 1, "rules": []}, mode=0o644)                      # readable by others: not trusted
        self.assertEqual(self.level("mail.draft"), "L2")
        self.path.unlink()
        target = self.tmp / "elsewhere.json"
        target.write_text(json.dumps({"version": 1, "rules": [{"kind": "pr.merge", "level": "L0"}]}))
        target.chmod(0o600)
        self.path.symlink_to(target)
        self.assertEqual(self.level("mail.draft"), "L2")
        self.path.unlink()
        self.assertEqual(self.level("mail.draft"), "L0")                         # no file: the approved defaults

    def test_the_owner_writer(self):
        policy.set_rule(self.path, "pr.draft_open", "L0")
        policy.set_rule(self.path, "mail.file", "L2")
        self.assertEqual((self.level("pr.draft_open"), self.level("mail.file")), ("L0", "L2"))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        policy.set_rule(self.path, "pr.draft_open", "L1")                        # replaces, does not pile up
        self.assertEqual(self.level("pr.draft_open"), "L1")
        self.assertEqual(len([r for r in json.loads(self.path.read_text())["rules"] if r["kind"] == "pr.draft_open"]), 1)
        before = self.path.read_bytes()
        for kind, level in (("mail.send", "L0"), ("mail.send", "L1"), ("school.submit", "L2"), ("garden.water", "L1"), ("money.spend", "L0"),
                            ("mail.file", "L9"), ("Mail.File", "L0"), ("", "L0"), ("mail.file", None)):
            with self.assertRaises(ValueError, msg=(kind, level)):
                policy.set_rule(self.path, kind, level)
        self.assertEqual(self.path.read_bytes(), before)
        policy.set_rule(self.path, "mail.send", "L3")                            # stricter is always allowed
        self.assertEqual(self.level("mail.send"), "L3")
        self.write("{")
        with self.assertRaises(ValueError):
            policy.set_rule(self.path, "mail.file", "L1")                        # a broken file is repaired by the owner, not overwritten
        self.assertEqual(self.path.read_text(), "{")

    def test_no_agent_surface(self):
        source = Path(policy.__file__).read_text()
        for word in ("jarvis.tools", "@tool", "brain_mcp", "jarvis.v2"):
            self.assertNotIn(word, source)
        from jarvis.v3 import brain_mcp
        self.assertFalse([name for name in brain_mcp.PROFILE if "policy" in name])


if __name__ == "__main__":
    unittest.main()
