# A4 verifier r1 test (orchestrator-owned; the fixer must not edit this file).
"""A foreground v3 daemon also holds resources needed by daemon2."""

from argparse import Namespace
import subprocess
import unittest
from unittest.mock import patch

from a4_acceptance_check import World
from jarvis import __main__, config
from jarvis.v2 import daemon
from jarvis.v3 import switch


class ForegroundHud(unittest.TestCase):
    def test_hud_does_not_start_v2_while_foreground_v3_is_running(self):
        world = World(v2_active=False, v3_active=False)
        env = world.env()
        env.v3_status = lambda: {"service": "jarvis-agentd", "version": "v3"}
        env.port_free = lambda port: port != config.V3_API_PORT
        args = Namespace(action="open", no_window=True)

        with (patch.object(config, "HUD_DEV", True),
              patch.object(switch, "default_env", return_value=env),
              patch.object(switch, "to_v2", wraps=switch.to_v2) as to_v2,
              patch.object(daemon, "is_running", side_effect=(False, True)),
              patch.object(subprocess, "Popen") as launch):
            result = __main__.cmd_hud(args)

        to_v2.assert_not_called()
        launch.assert_not_called()
        self.assertNotEqual(result, 0)


if __name__ == "__main__":
    unittest.main()
