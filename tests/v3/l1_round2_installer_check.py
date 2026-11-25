import json
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

from jarvis.v3 import brain_install


class InstallerRoundTwo(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.settings = self.home / ".claude/settings.json"
        self.codex = self.home / ".codex/config.toml"
        self.settings.parent.mkdir()
        self.codex.parent.mkdir()
        self.settings.write_bytes(b'{"hooks":{"Stop":[]}}')
        self.original = b'# owner\r\n[profiles.example]\r\nmodel = "first"\r\n'
        self.codex.write_bytes(self.original)

    def run_action(self, action):
        messages = []
        self.assertEqual(action(home=self.home, out=messages.append), 0, messages)

    def test_reinstall_preserves_owner_hook_with_similar_command_in_matcher(self):
        script = self.home / "owner tools/jarvis/v3/hooks/turn_end.py"
        script.parent.mkdir(parents=True)
        script.write_text("# Owner's notifier\n")
        command = shlex.join(["/usr/bin/python3", "-I", "-S", str(script), "claude"])
        group = {"matcher": "example", "hooks": [
            {"type": "command", "command": command, "timeout": 15},
        ]}
        before = json.dumps({"hooks": {"Stop": [group]}}, indent=2).encode()
        self.settings.write_bytes(before)

        self.run_action(brain_install.install)
        with self.subTest(action="install"):
            self.assertEqual(json.loads(self.settings.read_bytes())["hooks"]["Stop"][0], group)
        self.run_action(brain_install.uninstall)
        with self.subTest(action="uninstall"):
            self.assertEqual(self.settings.read_bytes(), before)

    def test_uninstall_after_checkout_move_removes_old_notify_and_mcp(self):
        previous = self.home / "previous checkout/jarvis/v3/brain_install.py"
        with patch.object(brain_install, "__file__", str(previous)):
            self.run_action(brain_install.install)
        installed = self.codex.read_bytes()
        self.codex.write_bytes(installed.replace(b'"first"', b'"second"'))

        self.run_action(brain_install.uninstall)

        self.assertEqual(self.codex.read_bytes(), self.original.replace(b'"first"', b'"second"'))


if __name__ == "__main__":
    unittest.main()
