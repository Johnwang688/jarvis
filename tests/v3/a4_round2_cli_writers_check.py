# A4 verifier r2 test (orchestrator-owned; the fixer must not edit this file).
"""Direct CLI callbacks that change daemon state belong in the owner gate."""

import unittest

from jarvis.protected_state import owner_op
from jarvis.v3 import cli


class CliWriters(unittest.TestCase):
    def test_gate_refuses_direct_calls_to_owner_callbacks(self):
        for callback in (cli.start, cli.stop, cli.restart, cli.install):
            with self.subTest(callback=callback.__name__):
                name = callback.__name__
                # Classification only. The call omits the required argument.
                command = f'python -c "from {cli.__name__} import {name}; {name}()"'
                self.assertIn("decision A-2", owner_op(command) or "")


if __name__ == "__main__":
    unittest.main()
