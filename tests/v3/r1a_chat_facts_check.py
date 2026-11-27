"""Verify chat policy facts and the approval cards they produce."""

import json
import unittest
from unittest.mock import patch

from jarvis.v3.policy import Policy, set_auto
from jarvis.v3.tools.calendar import CalendarTools

from a6b1_acceptance_check import Model, reply
from a6c1_acceptance_check import ADD, CREATE, NOW
from w1b_acceptance_check import Base, FakeWork, SEND, START


PATCH = {"name": "favourite-tea", "old": "green", "new": "oolong"}
CASES = (
    ("vault_add", dict(ADD, name="second-tea"), "vault.update", "L0", "default:vault.update"),
    ("calendar_create", CREATE, "calendar.block", "L0", "default:calendar.block"),
    ("calendar_update", {"event_id": "e1", "title": "Updated"}, "calendar.event_change", "L1",
     "owner-request:calendar.event_change"),
    ("work_start", START, "thread.start", "L1", "owner-request:thread.start"),
    ("work_send", SEND, "thread.send", "L1", "owner-request:thread.send"),
    ("vault_archive", {"name": "favourite-tea"}, "vault.archive", "L2", "default:vault.archive"),
    ("calendar_delete", {"event_id": "e1"}, "calendar.event_delete", "L2", "default:calendar.event_delete"),
)


class ChatFacts(Base):
    def test_auto_off_cards_keep_the_level_and_log_the_call_time_verdict(self):
        set_auto(self.policy_file, False)
        self.token.write_text("fake-token")
        vault = self.vault()
        vault.add(**ADD)
        original = vault.read(ADD["name"])
        self.model, work = Model(), FakeWork()
        svc = self.service(self.model, approvals=self.broker, work=work,
                           calendar=lambda approve=None: CalendarTools(request=self.cal, now=lambda: NOW,
                                                                      approve=approve))
        cases = [(False, *case) for case in CASES] + [(True, *case) for case in CASES]
        cases.append((True, "vault_patch", PATCH, "vault.update", "L0", "default:vault.update"))
        with patch.object(Policy, "explain_with_auto", autospec=True,
                          side_effect=Policy.explain_with_auto) as explain:
            for tainted, name, args, kind, level, why in cases:
                with self.subTest(tainted=tainted, name=name):
                    calls = []
                    if tainted and name == CASES[0][0]:
                        self.assertIs(svc.snapshot()["outside_text"], False)
                        calls.append(("index_search", {}))  # Same assistant step as the gated call.
                    if name == "vault_patch":
                        calls.append(("vault_read", {"name": ADD["name"]}))
                    calls.append((name, args))
                    self.model.steps.extend((reply(calls=calls), reply("Done.")))
                    svc.send("Try the fixture action.")
                    item = self.next_card()
                    expected = (("L2", "hard:tainted") if tainted and kind != "calendar.block"
                                else (level, "auto-off" if level < "L2" else why))
                    try:
                        snap = svc.snapshot()
                        self.assertEqual((snap["state"], snap["approvals"], snap["outside_text"]),
                                         ("running", 1, tainted))
                        self.assertEqual((item["tool"], item["level"], item["why"]), (name, *expected))
                        facts = {"kind": kind, "tainted": tainted}
                        if not tainted:
                            facts["owner_request"] = True
                        if name.startswith("calendar_"):
                            facts.update(attendees=0, owner_event=False)
                        self.assertEqual(explain.call_args.args[1], facts)
                        self.assertEqual(self.cal.writes(), [])
                        self.assertEqual(work.start_calls + work.t3, [])
                        self.assertEqual(self.notes(), ["facts/favourite-tea.md"])
                        self.assertEqual(vault.read(ADD["name"]), original)
                        self.assertEqual(self.states(item["id"]), ["pending"])
                    finally:
                        self.assertEqual(self.broker.answer(item["id"], item["digest"], "deny"), "denied")
                        self.assertEqual(self.wait_idle(svc), "idle")
                    self.assertEqual(self.states(item["id"]), ["pending", "denied"])
                    rows = [json.loads(line) for line in self.log.read_text().splitlines()]
                    logged = (("L2", "hard:tainted" if kind == "vault.update" else f"default:{kind}")
                              if tainted and kind != "calendar.block" else (level, why))
                    self.assertEqual([(row["level"], row["why"]) for row in rows if row["id"] == item["id"]],
                                     [logged, logged])


if __name__ == "__main__":
    unittest.main()
