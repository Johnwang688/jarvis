"""M4 regressions for responsiveness and private stdio diagnostics."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


def request(message_id, method, **params):
    message = {"jsonrpc": "2.0", "id": message_id, "method": method, "params": params}
    return (json.dumps(message) + "\n").encode()


class Stdio(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.env = {"HOME": str(self.home), "PATH": os.environ["PATH"],
                    "PYTHONPATH": os.environ.get("PYTHONPATH", str(Path(__file__).resolve().parents[2])),
                    "PYTHONDONTWRITEBYTECODE": "1", "JARVIS_V3_DIR": str(self.home / "state")}

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.kill()

    def test_tool_system_exit_stays_inside_stdio_boundary(self):
        secret = "fake-tool-exception-value"
        script = self.home / "tool_fixture.py"
        script.write_text(
            "import os, sys\n"
            "from jarvis.v3 import brain_mcp\n"
            "def tool(self, folder=''):\n"
            "    print(folder, file=sys.stderr, flush=True)\n"
            "    os.write(1, b'fake-private-result\\n')\n"
            f"    raise SystemExit({secret!r})\n"
            "brain_mcp.MemoryTools.memory_list = tool\n"
            "sys.exit(brain_mcp.main())\n", encoding="utf-8")
        wire = self.home / "requests.jsonl"
        wire.write_bytes(request(1, "tools/call", name="memory_list",
                                 arguments={"folder": "fake-private-argument"}) + request(2, "ping"))
        with wire.open("rb") as source:
            process = subprocess.run([sys.executable, str(script)], stdin=source,
                                     capture_output=True, env=self.env, cwd=self.home, timeout=5)

        for private in (secret, "fake-private-argument", "fake-private-result"):
            self.assertNotIn(private.encode(), process.stdout + process.stderr)
        self.assertEqual(process.returncode, 0)
        rejected, ping = (json.loads(line) for line in process.stdout.splitlines())
        self.assertEqual(rejected["id"], 1)
        self.assertTrue(rejected["result"]["isError"])
        self.assertEqual(ping, {"jsonrpc": "2.0", "id": 2, "result": {}})


if __name__ == "__main__":
    unittest.main()
