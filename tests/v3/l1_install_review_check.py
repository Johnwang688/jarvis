import json
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from jarvis.v3 import brain_install
from jarvis.v3.hooks import turn_end


class InstallReview(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.settings = self.home / ".claude/settings.json"
        self.codex = self.home / ".codex/config.toml"
        self.settings.parent.mkdir()
        self.codex.parent.mkdir()
        self.settings.write_bytes(b'{"hooks":{"Stop":[]}}')
        self.codex.write_bytes(b'model = "first"\r\n[profiles.example]\r\nmodel = "second"\r\n')
        self.messages = []

    def run_action(self, action):
        self.assertEqual(action(home=self.home, out=self.messages.append), 0, self.messages)

    def test_uninstall_removes_reformatted_notify_and_keeps_owner_edit(self):
        before = self.codex.read_bytes()
        self.run_action(brain_install.install)
        installed = self.codex.read_bytes()
        changed = installed.replace(b"notify = [", b"notify\t= [")
        self.assertNotEqual(changed, installed)
        self.codex.write_bytes(changed.replace(b'"second"', b'"third"'))

        self.run_action(brain_install.uninstall)

        self.assertEqual(self.codex.read_bytes(), before.replace(b'"second"', b'"third"'))

    def test_reinstall_after_checkout_move_replaces_both_hook_commands(self):
        checkout = self.home / "first checkout"
        interpreter = checkout / ".venv/bin/python"
        interpreter.parent.mkdir(parents=True)
        interpreter.symlink_to(sys.executable)
        script = checkout / "jarvis/v3/hooks/turn_end.py"
        script.parent.mkdir(parents=True)
        shutil.copyfile(turn_end.__file__, script)

        def install_from(repo):
            module = repo / "jarvis/v3/brain_install.py"
            with patch.object(brain_install, "__file__", str(module)):
                with patch.object(brain_install.config, "REPO_ROOT", repo):
                    self.run_action(brain_install.install)

        install_from(checkout)
        moved = self.home / "second checkout"
        checkout.rename(moved)
        install_from(moved)

        expected = [str(moved / ".venv/bin/python"), "-I", "-S",
                    str(moved / "jarvis/v3/hooks/turn_end.py")]
        groups = json.loads(self.settings.read_bytes())["hooks"]["Stop"]
        commands = [shlex.split(hook["command"]) for group in groups for hook in group["hooks"]]
        with self.subTest(provider="claude"):
            self.assertEqual(commands, [expected + ["claude"]])
        with self.subTest(provider="codex"):
            self.assertEqual(tomllib.loads(self.codex.read_text())["notify"], expected + ["codex"])


if __name__ == "__main__":
    unittest.main()
