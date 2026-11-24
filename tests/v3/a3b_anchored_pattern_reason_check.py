# A3b verifier r1 test, rewritten by the orchestrator (orchestrator-owned).
#
# It used to require the *reason* for a quoted `agentd$` to name the v3 daemon.
# Meeting that led a fixer to treat a bare `$` as judgeable, which weakened 584
# daemon2 vectors (tests/v3/daemon2_golden_check.py). The wording is cosmetic;
# the refusal is not. A pattern holding `$` stays unknowable, and is refused.
import unittest

from jarvis import rules


class AnchoredPattern(unittest.TestCase):
    def test_quoted_end_anchor_is_refused_for_both_daemons(self):
        for template in (
            "pkill -f '{word}$'",
            "pgrep --full '{word}$' | xargs kill",
            "ps aux | grep -E '{word}$' | xargs kill",
        ):
            for word in ("daemon2", "agentd"):
                command = template.format(word=word)
                with self.subTest(command=command):
                    self.assertEqual(rules.decide(command).decision, rules.DENY)


if __name__ == "__main__":
    unittest.main()
