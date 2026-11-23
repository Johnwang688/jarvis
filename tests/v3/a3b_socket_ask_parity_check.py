# A3b verifier r1 test (orchestrator-owned; the fixer must not edit this file).
import unittest

from jarvis import protected_state, rules


class SocketAskParity(unittest.TestCase):
    def test_socket_filter_cost_has_the_same_verdict(self):
        v2 = "ss -ltnp | grep -E '.*:84[07][57].*' | xargs kill"
        v3 = "ss -ltnp | grep -E '.*:84[17][07].*' | xargs kill"
        self.assertEqual(rules.decide(v2).decision, rules.ASK)
        self.assertIsNotNone(protected_state.kill_unsure(v2))
        verdict = rules.decide(v3)
        self.assertEqual(verdict.decision, rules.ASK, verdict.reason)

    def test_existing_daemon2_socket_ask_keeps_its_reason(self):
        command = "ss -ltnp | grep -E '.{0,16}:84[07][57].{0,32}' | xargs kill"
        expected = (
            "it is too complex to judge whether a `grep` over sockets feeding a `kill` "
            "matches the v2 daemon (`jarvis daemon2`, the process that holds the owner's "
            "approvals): matching its pattern would cost more than is judged here, since "
            "Python's `re` cannot be stopped mid-match. Asked, never covered by an allowlist entry"
        )
        verdict = rules.decide(command)
        self.assertEqual((verdict.decision, verdict.reason), (rules.ASK, expected))


if __name__ == "__main__":
    unittest.main()
