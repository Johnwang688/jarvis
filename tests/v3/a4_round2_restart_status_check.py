# A4 verifier r2 test (orchestrator-owned; the fixer must not edit this file).
"""A preflight read failure cannot make an old foreground process fresh."""

import unittest

from a4_acceptance_check import World
from jarvis.v3 import switch


class RestartStatus(unittest.TestCase):
    def test_restart_rejects_old_foreground_status_after_preflight_timeout(self):
        # The restart job is accepted, but the new service cannot bind
        # because the foreground process still holds the v3 port.
        world = World(v2_active=False, v3_active=False, v3_boots=False)
        env = world.env()
        probes = 0

        def v3_status():
            nonlocal probes
            probes += 1
            if probes == 1:
                return None
            return {
                "service": "jarvis-agentd",
                "version": "v3",
                "pid": 1234,
                "started_at": "2026-11-23T12:00:00+00:00",
                "uptime_s": 600 + (world.now - 1000),
            }

        env.v3_status = v3_status
        env.port_free = lambda port: port != switch.config.V3_API_PORT
        self.assertEqual(switch.restart_v3(env, wait=3), 1, "\n".join(world.lines))
        self.assertNotIn(f"{switch.V3_UNIT} restarted.", world.lines)


if __name__ == "__main__":
    unittest.main()
