# Orchestrator test (A4b review r1: deleting the preflight passed every suite).
"""An occupied target port is found before the source is stopped."""

import unittest

from a4_acceptance_check import World
from jarvis import config
from jarvis.v3 import switch


class Preflight(unittest.TestCase):
    def test_source_is_never_touched_when_the_target_port_is_taken(self):
        world = World()
        env = world.env()
        env.port_free = lambda port: port != config.V3_API_PORT and world.port_free(port)
        self.assertEqual(switch.to_v3(env, wait=3), 1)
        self.assertEqual(world.verbs(), [], "nothing was stopped, disabled, enabled or started")
        self.assertIn(str(config.V3_API_PORT), "\n".join(world.lines))


if __name__ == "__main__":
    unittest.main()
