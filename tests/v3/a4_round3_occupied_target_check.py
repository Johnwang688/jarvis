# A4 verifier r3 test (orchestrator-owned; the fixer must not edit this file).
"""A permanently occupied target port must leave the source available."""

import unittest

from a4_acceptance_check import World
from jarvis import config
from jarvis.v3 import switch


class OccupiedTarget(unittest.TestCase):
    def test_unrelated_v3_listener_does_not_leave_both_daemons_down(self):
        world = World(v3_boots=False)
        env = world.env()
        env.port_free = lambda port: (
            port != config.V3_API_PORT and world.port_free(port)
        )

        self.assertEqual(switch.to_v3(env, wait=3), 1)
        self.assertTrue(world._active(switch.V2_UNIT), "\n".join(world.lines))
        self.assertTrue(world.enabled[switch.V2_UNIT])


if __name__ == "__main__":
    unittest.main()
