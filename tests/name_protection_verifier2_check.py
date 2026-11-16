"""PR #388 re-check: counterexamples that fail at c424b47e.

Only temporary HOME/project folders and fake contents. Reuse round one's
isolated imports/configuration; leave product code and existing tests intact.
Run with PYTHONPATH=. and the task's memory-limited systemd scope.
"""
from __future__ import annotations

import json
import shlex
import sys
import unittest
from unittest.mock import patch

import name_protection_verifier_check as verifier
from jarvis import runtime
from jarvis.tools import dispatch, secrets, shell


class Counterexamples(unittest.TestCase):
    setUp = verifier.Counterexamples.setUp

    def test_raw_commands_keep_unicode_casefold_equivalent_credentials(self):
        """ASCII-only IGNORECASE drops true casefold equivalents along with lookalikes."""
        # Unlike dotted/dotless I, long s and Kelvin sign fold to ASCII s/k.
        for name in ("ſpotify_token.json", "onshape_Keys.json"):
            path = self.project / name
            path.write_text(verifier.MARKER + "\n")
            self.assertTrue(secrets.is_credential_name(name))
            code = f"print(open({str(path)!r}).read())"
            command = shlex.join([sys.executable, "-c", code])
            with self.subTest(name=name, reader="protected_in_command"):
                self.assertIsNotNone(secrets.protected_in_command(command),
                                     "raw scanning must agree with the credential-name rule")
            with self.subTest(name=name, reader="approved run_command via dispatch"):
                # Explicitly approve this fixture-only read. Approval is not
                # consent to print credentials; retain the real output scrubber.
                with runtime.tools_bound({"run_command"}):
                    out = dispatch(
                        "run_command",
                        json.dumps({"command": command, "reason": "hermetic verifier fixture"}),
                        approve=lambda *_: True,
                    ).text
                self.assertNotIn(verifier.MARKER, out,
                                 f"a casefold-protected credential reached a tool result: {out}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
