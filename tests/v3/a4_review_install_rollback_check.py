# A4 verifier r1 test (orchestrator-owned; the fixer must not edit this file).
"""A failed reload preserves an existing linked user unit."""

from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
import unittest

from a4_acceptance_check import World
from jarvis.v3 import switch


class InstallRollback(unittest.TestCase):
    def test_reload_failure_preserves_unit_symlink(self):
        env = World().env()
        env.systemctl = lambda *args: CompletedProcess(args, 1, "", "reload failed")
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source = repo / "managed.service"
            source.write_bytes(b"previous unit\n")
            unit = repo / "jarvis-agent.service"
            unit.symlink_to(source.name)

            self.assertEqual(switch.install_unit(
                env, unit_path=unit, python=str(repo / ".venv/bin/python"), repo=repo,
            ), 1)
            self.assertEqual(source.read_bytes(), b"previous unit\n")
            self.assertTrue(unit.is_symlink(), "failed reload replaced the linked unit")
            self.assertEqual(unit.readlink(), Path(source.name))


if __name__ == "__main__":
    unittest.main()
