"""M4 regressions for lost Claude updates and disabled Codex registrations."""

from pathlib import Path
import tempfile
import tomllib
import unittest

from jarvis.v3 import brain_install


class InstallerSafety(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.claude = self.home / ".claude.json"
        self.claude.write_text('{"numStartups": 10}\n', encoding="utf-8")
        self.codex = self.home / ".codex" / "config.toml"
        self.codex.parent.mkdir()
        self.codex.write_text('model = "example-model"\n', encoding="utf-8")
        self.output = []

    def run_action(self, action):
        return action(home=self.home, out=self.output.append)

    def test_codex_repair_preserves_owner_options(self):
        self.assertEqual(self.run_action(brain_install.install), 0, self.output)
        text = self.codex.read_text(encoding="utf-8")
        text = text.replace("[mcp_servers.jarvis-brain]\n",
                            "[mcp_servers.jarvis-brain]\n"
                            "enabled = false\ntool_timeout_sec = 120\n", 1)
        self.codex.write_text(text, encoding="utf-8")
        self.assertEqual(self.run_action(brain_install.status), 1, self.output)
        self.run_action(brain_install.install)

        config = tomllib.loads(self.codex.read_text(encoding="utf-8"))
        entry = config["mcp_servers"]["jarvis-brain"]
        self.assertIs(entry.get("enabled"), False)
        self.assertEqual(entry.get("tool_timeout_sec"), 120)


if __name__ == "__main__":
    unittest.main()
