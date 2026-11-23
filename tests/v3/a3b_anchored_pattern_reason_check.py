# A3b verifier r1 test (orchestrator-owned; the fixer must not edit this file).
import unittest

from jarvis import rules


class AnchoredPatternReason(unittest.TestCase):
    def test_quoted_end_anchor_names_the_selected_daemon(self):
        for template in (
            "pkill -f '{word}$'",
            "pgrep --full '{word}$' | xargs kill",
            "ps aux | grep -E '{word}$' | xargs kill",
        ):
            for word, version in (("daemon2", "v2"), ("agentd", "v3")):
                command = template.format(word=word)
                with self.subTest(command=command):
                    verdict = rules.decide(command)
                    self.assertEqual(verdict.decision, rules.DENY, verdict.reason)
                    self.assertIn(f"{version} daemon", verdict.reason)
                    self.assertIn(f"`{word}`", verdict.reason)


if __name__ == "__main__":
    unittest.main()
