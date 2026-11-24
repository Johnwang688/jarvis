# A4 verifier r3 test (orchestrator-owned; the fixer must not edit this file).
"""A slow unit-state query can exhaust the budget inside a poll."""

from subprocess import CompletedProcess
import unittest

from a4_acceptance_check import World
from jarvis.v3 import switch


class PollBudget(unittest.TestCase):
    def test_shutdown_does_not_probe_port_after_unit_query_used_deadline(self):
        world = World(v2_active=False, v3_active=True)
        env = world.env()
        port_probe_times = []

        def systemctl(*args):
            if args == ("is-active", switch.V3_UNIT):
                world.sleep(0.2)
                return CompletedProcess(args, 3, "inactive\n", "")
            return world.systemctl(*args)

        def port_free(_port):
            port_probe_times.append(world.now)
            return True

        env.systemctl = systemctl
        env.port_free = port_free
        self.assertEqual(switch.stop_v3(env, wait=0.1), 1)
        self.assertEqual(port_probe_times, [])


if __name__ == "__main__":
    unittest.main()
