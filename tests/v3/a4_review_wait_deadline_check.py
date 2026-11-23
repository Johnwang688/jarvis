# A4 verifier r1 test (orchestrator-owned; the fixer must not edit this file).
"""An oversleep cannot start another potentially blocking poll."""

import unittest

from a4_acceptance_check import World
from jarvis.v3 import switch


class WaitDeadline(unittest.TestCase):
    def test_oversleep_does_not_issue_another_systemctl_query(self):
        world = World(v2_active=False, v3_active=True)
        env = world.env()
        real = env.systemctl
        source_queries = []

        def systemctl(*args):
            if args == ("is-active", switch.V3_UNIT):
                source_queries.append(world.now)
            return real(*args)

        started = world.now
        env.systemctl = systemctl
        env.sleep = lambda delay: world.sleep(delay + 0.01)

        self.assertEqual(switch.stop_v3(env, wait=0.1), 1)
        self.assertTrue(source_queries)
        self.assertLessEqual(max(source_queries), started + 0.1, source_queries)


if __name__ == "__main__":
    unittest.main()
