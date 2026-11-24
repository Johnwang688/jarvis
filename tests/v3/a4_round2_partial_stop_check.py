# A4 verifier r2 test (orchestrator-owned; the fixer must not edit this file).
"""A failed stop can have already brought the source daemon down."""

from subprocess import CompletedProcess, TimeoutExpired
import unittest

from a4_acceptance_check import World
from jarvis.v3 import switch


class PartialStop(unittest.TestCase):
    def test_failed_stop_restores_source_if_it_went_down(self):
        for source, target, operation in (
            (switch.V2_UNIT, switch.V3_UNIT, switch.to_v3),
            (switch.V3_UNIT, switch.V2_UNIT, switch.to_v2),
        ):
            for timed_out in (False, True):
                with self.subTest(source=source, timed_out=timed_out):
                    world = World(v2_active=source == switch.V2_UNIT,
                                  v3_active=source == switch.V3_UNIT)
                    env = world.env()

                    def systemctl(*args):
                        result = world.systemctl(*args)
                        if args == ("stop", source):
                            world.sleep(2)
                            world._active(source)
                            if timed_out:
                                raise TimeoutExpired(args, 90)
                            return CompletedProcess(args, 1, "", "stop job failed")
                        return result

                    env.systemctl = systemctl
                    self.assertEqual(operation(env), 1)
                    self.assertFalse(world._active(target))
                    self.assertTrue(world.enabled[source])
                    self.assertTrue(world._active(source), "\n".join(world.lines))


if __name__ == "__main__":
    unittest.main()
