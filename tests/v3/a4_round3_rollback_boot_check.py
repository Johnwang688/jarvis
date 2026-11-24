# A4 verifier r3 test (orchestrator-owned; the fixer must not edit this file).
"""Rollback must preserve the one-daemon boot configuration."""

from subprocess import CompletedProcess
import unittest

from a4_acceptance_check import World
from jarvis.v3 import switch


class RollbackBoot(unittest.TestCase):
    def test_transient_target_disable_failure_does_not_enable_both_units(self):
        world = World(v3_boots=False)
        env = world.env()
        disable_failed = False

        def systemctl(*args):
            nonlocal disable_failed
            if args == ("disable", switch.V3_UNIT) and not disable_failed:
                disable_failed = True
                return CompletedProcess(args, 1, "", "transient disable failure")
            return world.systemctl(*args)

        env.systemctl = systemctl
        self.assertEqual(switch.to_v3(env, wait=3), 1)
        self.assertTrue(disable_failed)
        self.assertLessEqual(
            sum(world.enabled.values()), 1, "\n".join(world.lines)
        )


if __name__ == "__main__":
    unittest.main()
