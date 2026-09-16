"""MCP integration checks over real subprocess stdin/stdout, with fake I/O."""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest

from jarvis import tools
from jarvis.v2 import mcp

REPO = Path(__file__).resolve().parents[2]
SECRET = "planted-wp8-credential-123456789"

# Python's normal startup hook patches only the child process. Production has
# no fake-tool flags or alternative transport. Every child runs -m jarvis mcp.
STARTUP = '''
import dataclasses
import os
from pathlib import Path
import time
from jarvis import config, tools
from jarvis.tools import gmail
root = Path.cwd()
for key in ("GOOGLE_TOKEN_PATH", "ONSHAPE_TOKEN_PATH", "DISCORD_TOKEN_PATH", "SPOTIFY_TOKEN_PATH"):
    setattr(config, key, root / key)
for name, group in list(tools.GROUPS.items()):
    tools.GROUPS[name] = dataclasses.replace(group, available=lambda: os.environ.get("TEST_GROUPS") == "yes")
def forbidden(*args, **kwargs):
    (root / "transport-reached").write_text("bad")
    raise AssertionError("Gmail transport must not run")
gmail._request = forbidden
mode = os.environ.get("TEST_TOOL", "")
if mode == "slow":
    def slow():
        print("tool chatter before timeout", flush=True)
        time.sleep(0.6)
        (root / "completed").write_text("finished")
        print("tool chatter after timeout", flush=True)
        os.write(1, b"native chatter after timeout\\n")
        return "late result"
    tools.REGISTRY["get_datetime"].func = slow
elif mode == "image":
    tools.REGISTRY["get_datetime"].func = lambda: tools.ToolResult("caption", "aW1hZ2U=", "image/png")
elif mode == "error":
    def fail():
        raise RuntimeError("planted-wp8-credential-123456789")
    tools.REGISTRY["get_datetime"].func = fail
'''


class PipeClient:
    def __init__(self, root, *, mode="", groups=False, command=None, extra_env=None):
        (root / "sitecustomize.py").write_text(STARTUP)
        (root / ".env").write_text(f"FAKE_KEY={SECRET}\n")
        (root / "memory").mkdir(exist_ok=True)
        (root / "memory/note.md").write_text(f"WP8 needle: {SECRET}\n")
        self.stderr = tempfile.TemporaryFile()
        env = dict(os.environ, HOME=str(root), PYTHONPATH=os.pathsep.join([str(root), str(REPO)]),
                   JARVIS_MEMORY=str(root / "memory"), MCP_TOOL_TIMEOUT="0.1" if mode == "slow" else "5",
                   TEST_TOOL=mode, TEST_GROUPS="yes" if groups else "no")
        env.update(extra_env or {})
        self.proc = subprocess.Popen(command or [sys.executable, "-m", "jarvis", "mcp"],
                                     cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self.stderr)
        self.counter = 0

    def send(self, message):
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        self.proc.stdin.flush()

    def read(self):
        with selectors.DefaultSelector() as selector:
            selector.register(self.proc.stdout, selectors.EVENT_READ)
            if not selector.select(10):
                raise AssertionError("MCP response timed out")
        line = self.proc.stdout.readline()
        if not line:
            self.stderr.seek(0)
            raise AssertionError(f"server exited: {self.stderr.read().decode()}")
        self.last_line = line.decode()
        return json.loads(line)

    def request(self, method, params=None):
        self.counter += 1
        self.send({"jsonrpc": "2.0", "id": self.counter, "method": method, "params": params or {}})
        response = self.read()
        assert response["id"] == self.counter, response
        return response

    def call(self, name, arguments=None):
        return self.request("tools/call", {"name": name, "arguments": arguments or {}})["result"]

    def initialize(self, version="2025-06-18"):
        result = self.request("initialize", {"protocolVersion": version, "capabilities": {},
                                             "clientInfo": {"name": "test", "version": "1"}})["result"]
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.request("ping")  # notifications must not leave a response in the pipe
        return result

    def close(self):
        if self.proc.stdin and not self.proc.stdin.closed:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
            raise AssertionError("MCP failed to stop on EOF")
        finally:
            self.proc.stdout.close()
            self.stderr.close()


class MCPCheck(unittest.TestCase):
    def client(self, **kwargs):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        client = PipeClient(self.root, **kwargs)
        self.addCleanup(client.close)
        return client

    def test_protocols_and_schema_from_registry(self):
        for version in ("2025-06-18", "2024-11-05"):
            with self.subTest(version=version):
                client = self.client(groups=True)
                result = client.initialize(version)
                self.assertEqual(result["protocolVersion"], version)
                self.assertEqual(result["capabilities"], {"tools": {}})
                listed = client.request("tools/list")["result"]["tools"]
                self.assertEqual({t["name"] for t in listed}, mcp.MCP_TOOLS)
                for entry in listed:
                    spec = tools.REGISTRY[entry["name"]].spec()["function"]
                    self.assertEqual(entry, {"name": spec["name"], "description": spec["description"],
                                             "inputSchema": spec["parameters"]})

    def test_datetime_memory_and_secret_scrub(self):
        client = self.client()
        client.initialize()
        clock = client.call("get_datetime")
        self.assertFalse(clock["isError"])
        self.assertTrue(clock["content"][0]["text"])
        found = client.call("memory_search", {"query": "needle"})
        self.assertFalse(found["isError"])
        self.assertIn("WP8 needle", found["content"][0]["text"])
        self.assertNotIn(SECRET, client.last_line)
        self.assertIn("redacted", client.last_line)
        error = client.call("memory_search")
        self.assertTrue(error["isError"])
        self.assertIn("missing required", error["content"][0]["text"])

    def test_dangerous_denied_before_transport(self):
        client = self.client()
        client.initialize()
        result = client.call("gmail_send", {"to": "nobody@example.com", "subject": "Test", "body": "no send"})
        self.assertTrue(result["isError"])
        self.assertIn("approval routing for jarvis-mcp lands in WP5", result["content"][0]["text"])
        self.assertFalse((self.root / "transport-reached").exists())

    def test_unavailable_groups_and_unexposed_tools(self):
        client = self.client()
        client.initialize()
        names = {t["name"] for t in client.request("tools/list")["result"]["tools"]}
        grouped = {n for n in mcp.MCP_TOOLS if tools.group_of(n)}
        self.assertTrue(grouped)
        self.assertEqual(names, mcp.MCP_TOOLS - grouped)
        for name in ("spotify_play", "run_command", "invented"):
            self.assertTrue(client.call(name)["isError"])

    def test_protocol_errors_survive_and_notifications_are_silent(self):
        client = self.client()
        client.initialize()
        self.assertEqual(client.request("unknown")["error"]["code"], -32601)
        for bad_json in (b"{bad json\n", b"NaN\n", b"\xff\n"):
            client.proc.stdin.write(bad_json)
            client.proc.stdin.flush()
            self.assertEqual(client.read()["error"]["code"], -32700)
        client.send([])
        self.assertEqual(client.read()["error"]["code"], -32600)
        self.assertEqual(client.request("tools/call", {"name": "memory_search", "arguments": []})["error"]["code"], -32602)
        client.send({"jsonrpc": "2.0", "method": "unknown"})
        client.send({"jsonrpc": "2.0", "method": "tools/call", "params": {
            "name": "memory_write", "arguments": {"name": "notification", "content": "no"}}})
        self.assertEqual(client.request("ping")["result"], {})
        self.assertFalse((self.root / "memory/notification.md").exists())

    def test_timeout_thread_continues_and_stdout_stays_clean(self):
        client = self.client(mode="slow")
        client.initialize()
        start = time.monotonic()
        result = client.call("get_datetime")
        self.assertTrue(result["isError"])
        self.assertIn("timed out", result["content"][0]["text"])
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertFalse((self.root / "completed").exists())
        self.assertEqual(client.request("ping")["result"], {})
        time.sleep(0.65)
        self.assertTrue((self.root / "completed").exists())
        self.assertEqual(client.request("ping")["result"], {})
        client.stderr.seek(0)
        self.assertIn(b"native chatter", client.stderr.read())

    def test_eof_does_not_join_timed_out_worker(self):
        client = self.client(mode="slow")
        client.initialize()
        client.call("get_datetime")
        start = time.monotonic()
        client.proc.stdin.close()
        client.proc.wait(timeout=0.4)
        self.assertLess(time.monotonic() - start, 0.4)

    def test_images_and_scrubbed_exceptions(self):
        client = self.client(mode="image")
        client.initialize()
        result = client.call("get_datetime")
        self.assertFalse(result["isError"])
        self.assertEqual(result["content"][1], {"type": "image", "data": "aW1hZ2U=", "mimeType": "image/png"})
        client = self.client(mode="error")
        client.initialize()
        self.assertTrue(client.call("get_datetime")["isError"])
        self.assertNotIn(SECRET, client.last_line)

    def test_import_rejects_missing_registry_name(self):
        command = [sys.executable, "-c", "from jarvis import tools; del tools.REGISTRY['memory_search']; import jarvis.v2.mcp"]
        result = subprocess.run(command, cwd=REPO, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MCP_TOOLS names tools that are not registered: memory_search", result.stderr)

    def test_config_snippets_absolute_and_launch_from_other_cwd(self):
        result = subprocess.run([sys.executable, "-m", "jarvis", "mcp", "config"],
                                cwd=REPO, capture_output=True, text=True, check=True)
        claude, codex = result.stdout.split("\nCodex (config.toml):\n")
        entry = json.loads(claude.split(":\n", 1)[1])["mcpServers"]["jarvis"]
        toml_entry = tomllib.loads(codex)["mcp_servers"]["jarvis"]
        self.assertEqual(entry, toml_entry)
        self.assertEqual(entry["command"], str(Path(sys.executable).absolute()))
        self.assertEqual(entry["args"], ["-m", "jarvis", "mcp"])
        client = self.client(command=[entry["command"], *entry["args"]], extra_env=entry["env"])
        self.assertEqual(client.initialize()["serverInfo"]["name"], "jarvis-mcp")


if __name__ == "__main__":
    unittest.main()
