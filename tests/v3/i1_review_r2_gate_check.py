"""I1 review round 2: inline-Python spellings of the policy writer that the gate
still asks (so an allowlist entry for `python`, or mode "all", runs them) although
the same spelling of `jarvis hud install` is DENY."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import config, permissions, rules

POLICY = {
    "importlib_variable": "python -c 'import importlib; m = importlib.import_module(\"jarvis.v3.policy\"); m.set_rule(None, \"pr.merge\", \"L0\")'",
    "dunder_import": "python -c 'p = __import__(\"jarvis.v3.policy\", fromlist=[\"x\"]); p.set_rule(None, \"pr.merge\", \"L0\")'",
    "runpy_cli": "python -c 'import sys, runpy; sys.argv = [\"jarvis\", \"policy\", \"set\", \"pr.merge\", \"L0\"]; runpy.run_module(\"jarvis\", run_name=\"__main__\")'",
    "argv_main": "python -c 'import sys; sys.argv = [\"jarvis\", \"policy\", \"set\", \"pr.merge\", \"L0\"]; from jarvis.__main__ import main; main()'",
}
HUD = {
    "importlib_variable": "python -c 'import importlib; m = importlib.import_module(\"jarvis.v2.hud_install\"); m.install()'",
    "dunder_import": "python -c 'p = __import__(\"jarvis.v2.hud_install\", fromlist=[\"x\"]); p.install()'",
    "runpy_cli": "python -c 'import sys, runpy; sys.argv = [\"jarvis\", \"hud\", \"install\"]; runpy.run_module(\"jarvis\", run_name=\"__main__\")'",
    "argv_main": "python -c 'import sys; sys.argv = [\"jarvis\", \"hud\", \"install\"]; from jarvis.__main__ import main; main()'",
}


class InlinePythonPolicyWriter(unittest.TestCase):
    def test_spellings_refused_for_hud_install_are_refused_for_policy_set(self):
        for name, command in POLICY.items():
            with self.subTest(name=name):
                self.assertEqual(permissions.static_verdict(HUD[name]).decision, rules.DENY)  # the precedent
                self.assertEqual(permissions.static_verdict(command).decision, rules.DENY)

    def test_a_python_allowlist_entry_does_not_cover_them(self):
        tmp = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        allow = tmp / "allowlist.json"
        allow.write_text(json.dumps([{"tool": "run_command", "prefix": "python"}]))
        with patch.object(config, "ALLOWLIST_PATH", allow):
            for name, command in POLICY.items():
                with self.subTest(name=name):
                    self.assertFalse(permissions.allows("run_command", {"command": command}))


if __name__ == "__main__":
    unittest.main()
