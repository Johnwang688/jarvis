# A3b verifier r1 test, rewritten by the orchestrator (orchestrator-owned).
#
# It pinned ASK for all, measured against the A3a commit, whose port list had
# already grown by 8410 and so priced these patterns over the budget. The rule
# to keep is live main's (9e63b514), and v3 is never judged more loosely.
import unittest

from jarvis import protected_state, rules

RANK = {rules.ALLOW: 0, rules.ASK: 1, rules.DENY: 2}


class SocketFilters(unittest.TestCase):
    def test_daemon2_keeps_live_mains_verdict_and_v3_is_no_looser(self):
        for v2, v3, main in (
                ("ss -ltnp | grep -E '.*:84[07][57].*' | xargs kill",
                 "ss -ltnp | grep -E '.*:84[17][07].*' | xargs kill", rules.ASK),
                ("ss -ltnp | grep -E '.{0,16}:84[07][57].{0,32}' | xargs kill",
                 "ss -ltnp | grep -E '.{0,16}:84[17][07].{0,32}' | xargs kill", rules.DENY),
                ("kill $(ss -ltnp | grep -E '.{0,27}:84.5.{0,27}')",
                 "kill $(ss -ltnp | grep -E '.{0,27}:841.{0,27}')", rules.DENY)):
            with self.subTest(v2=v2):
                self.assertEqual(rules.decide(v2).decision, main)
                self.assertGreaterEqual(RANK[rules.decide(v3).decision], RANK[main])
                self.assertEqual(bool(protected_state.owner_op(v2)), main == rules.DENY)


if __name__ == "__main__":
    unittest.main()
