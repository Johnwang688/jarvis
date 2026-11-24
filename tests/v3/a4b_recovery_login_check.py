# A4b verifier r1 test (orchestrator-owned; the fixer must not edit this file).
"""Systemd's unit-file states and query errors during rollback."""

from subprocess import CompletedProcess
import unittest

from a4_acceptance_check import World
from jarvis.v3 import switch


class LoginRecovery(unittest.TestCase):
    def test_source_is_reenabled_after_disable_took_effect(self):
        source, target = switch.V2_UNIT, switch.V3_UNIT
        for initial_state, exit_code in (
            # enabled-runtime is a documented limit: runtime enablement ends with the session.
            ("static", 0), ("linked", 1),
            ("masked-runtime", 1), ("indirect", 0),
        ):
            with self.subTest(target_state=initial_state):
                world = World()
                env = world.env()
                target_state = initial_state

                def command(*args):
                    nonlocal target_state
                    if args == ("is-enabled", target):
                        code = {"disabled": 1, "not-found": 4}.get(target_state, exit_code)
                        return CompletedProcess(args, code, target_state + "\n", "")
                    if args == ("disable", source):
                        world.systemctl(*args)
                        return CompletedProcess(args, 1, "", "Failed to reload daemon: Connection reset by peer")
                    if args[0] == "disable" and args[-1] == target:
                        if target_state == "enabled-runtime" and "--runtime" in args:
                            target_state = "disabled"
                        elif target_state == "linked" and "--runtime" not in args:
                            target_state = "not-found"
                        return CompletedProcess(args, 0, "", "")
                    return world.systemctl(*args)

                env.systemctl = command
                self.assertEqual(switch.to_v3(env, wait=3), 1)
                self.assertTrue(world._active(source))
                self.assertEqual((world.enabled[source], target_state == "enabled-runtime"),
                                 (True, False), "\n".join(world.lines))

    def test_empty_enablement_output_does_not_report_runtime_rollback_failed(self):
        world = World(v3_installed=False)
        env = world.env()
        source = switch.V2_UNIT

        def command(*args):
            if args == ("is-enabled", source):
                return CompletedProcess(args, 1, "", "Failed to get unit file state: Connection reset by peer")
            return world.systemctl(*args)

        env.systemctl = command
        self.assertEqual(switch.to_v3(env, wait=3), 1)
        self.assertTrue(world._active(source))
        self.assertTrue(world.enabled[source])
        self.assertFalse(world.enabled[switch.V3_UNIT])
        self.assertNotIn(f"Rollback to {source} failed.", world.lines)


if __name__ == "__main__":
    unittest.main()
