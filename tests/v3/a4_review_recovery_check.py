# A4 verifier r1 test (orchestrator-owned; the fixer must not edit this file).
"""An unsuccessful handoff can restore the healthy source unit."""

from subprocess import CompletedProcess
import unittest

from a4_acceptance_check import World
from jarvis.v3 import switch


class Recovery(unittest.TestCase):
    def test_source_survives_recoverable_switch_failures(self):
        for source, target, operation in (
            (switch.V2_UNIT, switch.V3_UNIT, switch.to_v3),
            (switch.V3_UNIT, switch.V2_UNIT, switch.to_v2),
        ):
            for failure in ("disable", "enable", "startup timeout"):
                with self.subTest(source=source, failure=failure):
                    world = World(
                        v2_active=source == switch.V2_UNIT,
                        v3_active=source == switch.V3_UNIT,
                        v2_boots=source == switch.V2_UNIT or failure != "startup timeout",
                        v3_boots=source == switch.V3_UNIT or failure != "startup timeout",
                    )
                    env = world.env()
                    real = env.systemctl

                    def systemctl(*args):
                        if ((failure == "disable" and args == ("disable", source))
                                or (failure == "enable" and args == ("enable", "--now", target))):
                            return CompletedProcess(args, 1, "", "injected systemctl failure")
                        return real(*args)

                    env.systemctl = systemctl
                    self.assertEqual(operation(env, wait=3), 1)
                    world.sleep(3)
                    self.assertFalse(world._active(target))
                    self.assertTrue(world._active(source), "\n".join(world.lines))
                    self.assertTrue(world.enabled[source])


if __name__ == "__main__":
    unittest.main()
