"""Regression checks for the brain MCP transport and installer."""

from contextlib import suppress
import inspect
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import typing
import unittest
from unittest.mock import Mock, patch

from jarvis.v3 import brain_install, brain_mcp
from jarvis.v3.tools.memory import MemoryTools
from jarvis.v3.tools.session_search import session_search
from jarvis.v3.tools.skills import SkillTools


REPO = Path(__file__).resolve().parents[2]
PING = b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'


def tool_request(request_id, name, arguments):
    message = {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
               "params": {"name": name, "arguments": arguments}}
    return (json.dumps(message, ensure_ascii=False) + "\n").encode()


class BrainChecks(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.brain = brain_mcp.Brain()

    def run_server(self, args, requests):
        env = {"HOME": str(self.home), "JARVIS_V3_DIR": str(self.home / "v3"),
               "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, *args], input=requests, capture_output=True,
                                env=env, cwd=self.home, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_schemas_follow_tool_signatures(self):
        methods = {name: method for cls in (MemoryTools, SkillTools)
                   for name, method in inspect.getmembers(cls, inspect.isfunction)
                   if name.startswith(("memory_", "skill_"))}
        methods["session_search"] = session_search
        listing = {tool["name"]: tool["inputSchema"] for tool in self.brain.list_tools()["tools"]}
        self.assertEqual(listing.keys(), methods.keys())
        schema_types = {"string": str, "integer": int, "boolean": bool}
        for name, method in methods.items():
            with self.subTest(tool=name):
                hints = typing.get_type_hints(method, include_extras=True)
                parameters, required = {}, set()
                for param in inspect.signature(method).parameters.values():
                    if param.name == "self" or (name == "session_search" and param.name == "index"):
                        continue
                    hint = hints[param.name]
                    if param.kind is inspect.Parameter.VAR_KEYWORD:
                        self.assertIs(typing.get_origin(hint), typing.Unpack)
                        (fields,) = typing.get_args(hint)
                        parameters.update(typing.get_type_hints(fields, include_extras=True))
                        required.update(fields.__required_keys__)
                    else:
                        parameters[param.name] = hint
                        if param.default is inspect.Parameter.empty:
                            required.add(param.name)
                schema = listing[name]
                properties = {key: schema_types[value["type"]]
                              for key, value in schema["properties"].items()}
                self.assertEqual(properties,
                                 {key: typing.get_args(hint)[0] for key, hint in parameters.items()})
                self.assertEqual(set(schema["required"]), required)

    def test_outside_profile_skips_argument_validation(self):
        with patch.object(brain_mcp, "_valid") as validate:
            result = self.brain.call("calendar_create", {"title": None})
        self.assertTrue(result["isError"])
        self.assertIn("outside the brain profile", result["content"][0]["text"])
        validate.assert_not_called()

    def test_schema_rejects_invalid_arguments_before_dispatch(self):
        with patch.object(self.brain, "_search", return_value="matched") as search:
            for arguments in ({"limit": True}, {"limit": "2"}, {"index": "fixture"}, []):
                with self.subTest(arguments=arguments):
                    self.assertTrue(self.brain.call("session_search", arguments)["isError"])
            search.assert_not_called()
            self.assertFalse(self.brain.call("session_search", {"limit": 2})["isError"])
            search.assert_called_once_with(limit=2)

    def test_pipelined_cli_requests_round_trip_unicode(self):
        body = "Café, 雪, and 🐙."
        requests = tool_request("add", "memory_add", {
            "folder": "facts", "name": "fixture", "description": "Unicode sample",
            "body": body, "source": "fixture",
        }) + tool_request("read-é", "memory_read", {"name": "fixture"})
        result = self.run_server(["-m", "jarvis", "brain", "mcp"], requests)
        added, read = map(json.loads, result.stdout.splitlines())
        self.assertEqual([added["id"], read["id"]], ["add", "read-é"])
        self.assertFalse(added["result"]["isError"])
        self.assertFalse(read["result"]["isError"])
        self.assertIn(body, read["result"]["content"][0]["text"])
        self.assertEqual(result.stderr, b"")

    def test_eof_mid_line_cannot_dispatch(self):
        brain = Mock()
        sink = io.StringIO()
        partial = tool_request(2, "memory_read", {"name": "fixture"})[:-5]
        brain_mcp.serve(io.BytesIO(PING + partial), sink, brain)
        ping, error = map(json.loads, sink.getvalue().splitlines())
        self.assertEqual(ping, {"jsonrpc": "2.0", "id": 1, "result": {}})
        self.assertIsNone(error["id"])
        self.assertEqual(error["error"]["code"], -32700)
        brain.call.assert_not_called()

    def test_closed_sink_stops_before_next_request(self):
        call = tool_request(2, "memory_list", {})
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        sink = os.fdopen(write_fd, "w", encoding="utf-8", buffering=1)
        try:
            brain = Mock()
            brain_mcp.serve(io.BytesIO(PING + call), sink, brain)
            brain.call.assert_not_called()
        finally:
            # The writer retries its buffered data when closed.
            with suppress(BrokenPipeError):
                sink.close()

    def test_stdout_is_reserved_for_protocol(self):
        code = """
import os
import sys
from jarvis.v3 import brain_mcp

def noisy_tool(self, name, arguments):
    print("fixture argument", flush=True)
    os.write(sys.stdout.fileno(), b"fixture native stdout\\n")
    print("fixture result", file=sys.stderr, flush=True)
    return {"content": [{"type": "text", "text": "reply"}], "isError": False}

brain_mcp.Brain.call = noisy_tool
sys.exit(brain_mcp.main())
"""
        result = self.run_server(["-c", code], tool_request(1, "memory_list", {}))
        (reply,) = result.stdout.splitlines()
        self.assertEqual(json.loads(reply)["result"]["content"][0]["text"], "reply")
        self.assertEqual(result.stderr, b"jarvis-brain suppressed unexpected tool output.\n")


class InstallerChecks(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.claude = self.home / ".claude.json"
        self.codex = self.home / ".codex" / "config.toml"
        self.codex.parent.mkdir()
        self.claude.write_bytes(b'{"mcpServers": {}}\n')
        self.codex.write_bytes(b'model = "fixture"\n')

    def run_installer(self, action):
        messages = []
        status = action(home=self.home, out=messages.append)
        return status, messages

    def test_odd_paths_and_exact_round_trip_twice(self):
        repo = self.home / 'fake "checkout" \\ tab\t DEL\x7f 🐙'
        python = repo / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.touch()
        for codex_before in (b'# comment\r\nmodel = "fixture"\r\n', b'model = "fixture"'):
            with self.subTest(codex_before=codex_before):
                claude_before = b'{\r\n  "mcpServers": {}\r\n}'
                self.claude.write_bytes(claude_before)
                self.codex.write_bytes(codex_before)
                with patch.object(brain_install.config, "REPO_ROOT", repo):
                    status, messages = self.run_installer(brain_install.install)
                    self.assertEqual(status, 0, messages)
                    installed = self.codex.read_bytes()
                    self.assertTrue(installed.startswith(codex_before))
                    if b"\r\n" in codex_before:
                        self.assertNotIn(b"\n", installed.replace(b"\r\n", b""))
                    entry = tomllib.loads(installed.decode())["mcp_servers"]["jarvis-brain"]
                    self.assertEqual(entry["command"], str(python))
                    self.assertEqual(entry["env"], {"PYTHONPATH": str(repo)})
                    for _ in range(2):
                        status, messages = self.run_installer(brain_install.uninstall)
                        self.assertEqual(status, 0, messages)
                        self.assertEqual(self.claude.read_bytes(), claude_before)
                        self.assertEqual(self.codex.read_bytes(), codex_before)

    def test_hardlinked_config_refused_before_either_write(self):
        before = self.claude.read_bytes(), self.codex.read_bytes()
        alias = self.home / "linked.toml"
        os.link(self.codex, alias)
        status, messages = self.run_installer(brain_install.install)
        self.assertEqual(status, 1, messages)
        self.assertEqual((self.claude.read_bytes(), self.codex.read_bytes()), before)
        self.assertEqual(alias.read_bytes(), before[1])

    def test_race_after_backup_refused_before_rename(self):
        codex_before = self.codex.read_bytes()
        changed = b'{"ownerSetting": 2}\n'
        backup = brain_install._backup

        def change_after_backup(target, after, mode):
            backup(target, after, mode)
            if target.path == self.claude:
                self.claude.write_bytes(changed)

        with patch.object(brain_install, "_backup", side_effect=change_after_backup):
            status, messages = self.run_installer(brain_install.install)
        self.assertEqual(status, 1, messages)
        self.assertEqual(self.claude.read_bytes(), changed)
        self.assertEqual(self.codex.read_bytes(), codex_before)


if __name__ == "__main__":
    unittest.main()
