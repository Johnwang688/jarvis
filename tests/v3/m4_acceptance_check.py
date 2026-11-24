"""Orchestrator-owned acceptance tests for M4 (implementers: do not edit; if one is wrong, stop and say why).

Contract
  jarvis/v3/brain_mcp.py     a stdio MCP server (newline-delimited JSON-RPC 2.0, the protocol versions v2's adapter speaks) over v3's own tools; no v2 tool registry
    PROFILE = memory_list memory_read memory_add memory_patch memory_archive memory_search session_search skill_view skill_manage (a tuple, exactly these, sorted)
    Brain(*, vault=None, index=None, store=None, session=None)     index is a path or None; one MemoryTools and one SkillTools per Brain (one per server process); actor "foreground"
      list_tools() -> {"tools": [...]} exactly PROFILE: name, description, inputSchema (object, additionalProperties false, required); call(name, arguments) -> {"content": [{"type": "text", "text"}], "isError"}
      A name outside PROFILE is refused before its arguments are looked at. Arguments are checked against the schema (types, unknown, missing) before anything runs. Results are capped at 32,000 characters and scrubbed for display.
    serve(source, sink, brain); main()      the protocol pipe holds only JSON-RPC; tool chatter goes to stderr, which never holds an argument or a result
  jarvis/v3/brain_install.py
    install(*, home=None, out=print) / status(...) / uninstall(...) -> int (0 ok; status: 0 installed and unchanged, 1 otherwise)
      Claude: <home>/.claude.json "mcpServers"."jarvis-brain"; Codex: <home>/.codex/config.toml [mcp_servers.jarvis-brain]. Command: the repo venv's python, args ["-m", "jarvis", "brain", "mcp"], env PYTHONPATH = the repo.
      Backs up first (<file>.bak-<stamp>, same mode), atomic, mode kept, never through a link, refuses a file it cannot parse or one that changed while it worked. Idempotent (a second install changes nothing, no new backup).
      uninstall after install restores the exact bytes when nothing else changed, and otherwise removes only its own entry. A consumer whose folder/file does not exist is skipped (Codex: no ~/.codex) or made only for Claude when ~/.claude exists.
  `jarvis brain mcp|install|status|uninstall` in jarvis/v3/cli.py
"""
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import tools
from jarvis.v3 import brain_install, brain_mcp, imports
from jarvis.v3.brain_mcp import Brain
from jarvis.v3.index import Index
from jarvis.v3.skill_store import SkillStore
from jarvis.v3.vault import Vault

REPO = Path(__file__).resolve().parents[2]
PLANTED = "ghp" + "_" + "B7r8" * 9
PROFILE = ("memory_add", "memory_archive", "memory_list", "memory_patch", "memory_read", "memory_search", "session_search", "skill_manage", "skill_view")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        self.vault = Vault(self.tmp / "v3" / "vault")
        self.index_path = self.tmp / "v3" / "index.sqlite"
        self.store = SkillStore(self.tmp / "v3" / "skills", ledger=self.tmp / "v3" / "ledger", links=[self.tmp / "c", self.tmp / "x"], owner_roots=[self.tmp / "own"])
        self.b = self.brain()

    def brain(self):
        return Brain(vault=self.vault, index=self.index_path, store=self.store, session="s1")

    def call(self, tool_name, b=None, **args):
        r = (b or self.b).call(tool_name, args)
        self.assertEqual(set(r), {"content", "isError"})
        (c,) = r["content"]
        self.assertEqual(c["type"], "text")
        return r["isError"], c["text"]


class Profile(Base):
    def test_exactly(self):
        self.assertEqual(tuple(brain_mcp.PROFILE), PROFILE)
        listed = self.b.list_tools()["tools"]
        self.assertEqual(tuple(t["name"] for t in listed), PROFILE)
        for t in listed:
            self.assertGreater(len(t["description"]), 20, t["name"])
            s = t["inputSchema"]
            self.assertEqual((s["type"], s["additionalProperties"]), ("object", False), t["name"])
            for p, d in s["properties"].items():
                self.assertIn(d.get("type"), ("string", "integer", "boolean"), (t["name"], p))
                self.assertTrue(d.get("description"), (t["name"], p))
        by = {t["name"]: t["inputSchema"] for t in listed}
        self.assertEqual(set(by["memory_add"]["required"]), {"folder", "name", "description", "body", "source"})
        self.assertEqual(set(by["skill_manage"]["required"]), {"action", "name"})
        self.assertEqual(set(by["skill_manage"]["properties"]), {"action", "name", "description", "body", "path", "old", "new", "content"})
        self.assertEqual(set(by["session_search"]["properties"]), {"query", "mode", "doc", "around", "limit"})
        self.assertLess(len(json.dumps(listed)), 12_000)

    def test_outside_is_refused_before_arguments(self):
        class Poison(dict):
            def __iter__(self):
                raise AssertionError("arguments were read")
            items = keys = values = get = __getitem__ = __len__ = __iter__
        for name in ("run_command", "read_file", "calendar_create", "memory_write", "memory_delete", "_redacted", "__init__", "flags", "skill_manage ", "Memory_Read", ""):
            r = self.b.call(name, Poison())
            self.assertTrue(r["isError"], name)

    def test_arguments_are_checked_first(self):
        for name, args in (("memory_read", {}), ("memory_read", {"name": 5}), ("memory_read", {"name": "a", "x": 1}), ("session_search", {"limit": "5"}), ("session_search", {"limit": True}), ("session_search", {"limit": 1.5}),
                           ("memory_add", {"folder": "facts"}), ("skill_manage", {"action": "create"}), ("memory_list", {"folder": None}), ("memory_list", []), ("memory_list", "x")):
            with patch.object(Vault, "entries", side_effect=AssertionError("ran")), patch.object(Vault, "read", side_effect=AssertionError("ran")):
                self.assertTrue(self.b.call(name, args)["isError"], (name, args))


class Works(Base):
    def test_memory(self):
        self.assertFalse(self.call("memory_add", folder="facts", name="tea", description="a drink", body="John prefers tea.", source="owner")[0])
        self.assertIn("John prefers tea.", self.call("memory_read", name="tea")[1])
        self.assertFalse(self.call("memory_patch", name="tea", old="tea", new="mint tea")[0])
        self.assertIn("facts/tea", self.call("memory_search", query="mint")[1])
        err, text = self.call("memory_patch", b=self.brain(), name="tea", old="mint", new="x")     # another server process has not read it
        self.assertTrue(err and text.startswith("Error:"))
        self.assertFalse(self.call("memory_archive", name="tea")[0])

    def test_skills(self):
        self.assertFalse(self.call("skill_manage", action="create", name="release-steps", description="How to release.", body="# Steps\n\n1. Test.\n")[0])
        self.assertIn("Test.", self.call("skill_view", name="release-steps")[1])
        self.assertEqual(self.store.records()[-1]["actor"], "foreground")
        self.assertEqual(self.store.records()[-1]["session"], "s1")
        self.assertTrue(self.call("skill_manage", action="create", name="x-y", description="d", body="b", session="forged")[0])
        self.assertTrue(self.call("skill_manage", action="create", name="x-y", description="d", body="b", tainted=False)[0])

    def test_sessions(self):
        with Index(self.index_path) as i:
            i.add("claude", "a", title="Quokka", ts=1.0, messages=[("user", "the quokka build broke", None)])
        self.assertIn("quokka", self.call("session_search", query="quokka")[1])
        self.assertIsInstance(self.call("session_search", b=Brain(vault=self.vault, index=self.tmp / "none.sqlite", store=self.store), query="x")[1], str)

    def test_bounded_and_scrubbed(self):
        with patch.object(Vault, "read", side_effect=RuntimeError(PLANTED)):
            err, text = self.call("memory_read", name="tea")
        self.assertTrue(err)
        self.assertNotIn(PLANTED, text)
        with patch("jarvis.v3.tools.memory.MemoryTools.memory_list", return_value="x" * 200_000):
            self.assertLessEqual(len(self.call("memory_list")[1]), 32_100)
        with patch("jarvis.v3.tools.memory.MemoryTools.memory_list", return_value=f"a {PLANTED} b"):
            self.assertNotIn(PLANTED, self.call("memory_list")[1])


class Protocol(Base):
    def rpc(self, *messages):
        out = io.StringIO()
        brain_mcp.serve(io.BytesIO(b"".join((m if isinstance(m, bytes) else json.dumps(m).encode()) + b"\n" for m in messages)), out, self.b)
        return [json.loads(l) for l in out.getvalue().splitlines()]

    def test_handshake_list_call(self):
        r = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "x", "version": "1"}}},
                     {"jsonrpc": "2.0", "method": "notifications/initialized"}, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                     {"jsonrpc": "2.0", "id": "3", "method": "tools/call", "params": {"name": "memory_list", "arguments": {}}}, {"jsonrpc": "2.0", "id": 4, "method": "ping"},
                     {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "memory_list"}})
        self.assertEqual([m["id"] for m in r], [1, 2, "3", 4, 5])
        self.assertEqual((r[0]["result"]["protocolVersion"], r[0]["result"]["serverInfo"]["name"]), ("2025-06-18", "jarvis-brain"))
        self.assertEqual(len(r[1]["result"]["tools"]), 9)
        self.assertFalse(r[2]["result"]["isError"])

    def test_bad_messages(self):
        r = self.rpc(b"{", b"[]", b"null", b'{"jsonrpc":"2.0","id":1,"method":"nope"}', b'{"jsonrpc":"1.0","id":1,"method":"ping"}', b'{"jsonrpc":"2.0","id":NaN,"method":"ping"}', b"\xff\xfe", b"[" * 100_000,
                     b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":5}}', b'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":[]}', b'{"jsonrpc":"2.0","id":9,"method":"ping"}')
        self.assertEqual(r[-1], {"jsonrpc": "2.0", "id": 9, "result": {}})
        self.assertEqual(len(r), 11)
        self.assertTrue(all("error" in m for m in r[:-1]), r)
        self.assertEqual(self.rpc(b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"memory_list","arguments":{}}}'), [])      # a notification runs nothing

    def test_a_huge_line_is_refused_not_held(self):
        r = self.rpc(b'{"jsonrpc":"2.0","id":1,"method":"ping","x":"' + b"a" * (20 * 1024 * 1024) + b'"}', {"jsonrpc": "2.0", "id": 2, "method": "ping"})
        self.assertEqual(r[-1]["id"], 2)
        self.assertIn("error", r[0])

    def test_real_process(self):
        env = {"PATH": os.environ["PATH"], "HOME": str(self.tmp / "h"), "PYTHONPATH": str(REPO), "JARVIS_V3_DIR": str(self.tmp / "p")}
        (self.tmp / "h").mkdir()
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "memory_add", "arguments": {"folder": "facts", "name": "k", "description": "d", "body": f"private-words {PLANTED}", "source": "owner"}}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "memory_list", "arguments": {}}}]
        p = subprocess.run([sys.executable, "-m", "jarvis", "brain", "mcp"], input="".join(json.dumps(m) + "\n" for m in msgs), capture_output=True, text=True, env=env, cwd=self.tmp, timeout=60)
        rows = [json.loads(l) for l in p.stdout.splitlines()]
        self.assertEqual([r["id"] for r in rows], [1, 2, 3], p.stderr[-400:])
        self.assertTrue(rows[1]["result"]["isError"])
        self.assertNotIn(PLANTED, p.stdout + p.stderr)
        self.assertNotIn("private-words", p.stderr)

    def test_light_and_v2_untouched(self):
        before = set(tools.REGISTRY)
        code = "import sys, jarvis.v3.brain_mcp; print([m for m in ('jarvis.llm','jarvis.agent','jarvis.v2.daemon','claude_agent_sdk','jarvis.browser') if m in sys.modules])"
        out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env={**os.environ, "PYTHONPATH": str(REPO)}, capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(out, "[]")
        self.assertEqual(set(tools.REGISTRY), before)
        self.assertEqual(imports.violations(REPO / "jarvis/v3"), [])


class Install(unittest.TestCase):
    CLAUDE = '{\n  "numStartups": 5,\n  "projects": {"/x": {"mcpServers": {}}},\n  "mcpServers": {"other": {"command": "y"}},\n  "name": "José"\n}'
    CODEX = '# mine\nmodel = "gpt-6.2-sol"   # keep\n\n[mcp_servers.other]\ncommand = "y"\n\n[profiles.x]\nmodel = "z"\n'

    def setUp(self):
        self.home = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        (self.home / ".claude").mkdir()
        (self.home / ".codex").mkdir()
        self.cj, self.ct = self.home / ".claude.json", self.home / ".codex" / "config.toml"
        self.cj.write_text(self.CLAUDE)
        self.ct.write_text(self.CODEX)
        self.cj.chmod(0o600)
        self.ct.chmod(0o600)
        self.out = []

    def run_it(self, fn):
        return fn(home=self.home, out=self.out.append)

    def entries(self):
        return json.loads(self.cj.read_text())["mcpServers"].get("jarvis-brain"), tomllib.loads(self.ct.read_text())["mcp_servers"].get("jarvis-brain")

    def backups(self):
        return sorted(p.name for p in [*self.home.iterdir(), *(self.home / ".codex").iterdir()] if ".bak-" in p.name)

    def test_install(self):
        self.assertEqual(self.run_it(brain_install.status), 1)
        self.assertEqual(self.run_it(brain_install.install), 0)
        for e in self.entries():
            self.assertEqual(e["args"], ["-m", "jarvis", "brain", "mcp"])
            self.assertTrue(e["command"].endswith("/python") or "python" in Path(e["command"]).name)
            self.assertEqual(set(e["env"]), {"PYTHONPATH"})
        c = json.loads(self.cj.read_text())
        self.assertEqual((c["numStartups"], c["mcpServers"]["other"], c["name"], c["projects"]), (5, {"command": "y"}, "José", {"/x": {"mcpServers": {}}}))
        self.assertTrue(self.ct.read_text().startswith(self.CODEX.rstrip("\n")))
        self.assertEqual(stat.S_IMODE(self.cj.stat().st_mode), 0o600)
        self.assertEqual(len(self.backups()), 2)
        self.assertEqual(self.run_it(brain_install.status), 0)
        self.assertTrue(any(str(self.cj) in l for l in self.out) and any(str(self.ct) in l for l in self.out))

    def test_idempotent(self):
        self.run_it(brain_install.install)
        a = (self.cj.read_bytes(), self.ct.read_bytes(), self.backups())
        self.assertEqual(self.run_it(brain_install.install), 0)
        self.assertEqual((self.cj.read_bytes(), self.ct.read_bytes(), self.backups()), a)

    def test_round_trip_is_byte_identical(self):
        self.run_it(brain_install.install)
        self.assertEqual(self.run_it(brain_install.uninstall), 0)
        self.assertEqual((self.cj.read_text(), self.ct.read_text()), (self.CLAUDE, self.CODEX))
        self.assertEqual(self.run_it(brain_install.status), 1)
        self.assertEqual(self.run_it(brain_install.uninstall), 0)

    def test_uninstall_after_other_changes_removes_only_ours(self):
        self.run_it(brain_install.install)
        c = json.loads(self.cj.read_text())
        c["numStartups"] = 6
        self.cj.write_text(json.dumps(c))
        self.ct.write_text(self.ct.read_text().replace('model = "z"', 'model = "w"') + '\n[mcp_servers.later]\ncommand = "q"\n')
        self.assertEqual(self.run_it(brain_install.uninstall), 0)
        self.assertEqual(self.entries(), (None, None))
        self.assertEqual(json.loads(self.cj.read_text())["numStartups"], 6)
        t = tomllib.loads(self.ct.read_text())
        self.assertEqual((t["mcp_servers"]["later"]["command"], t["profiles"]["x"]["model"], t["mcp_servers"]["other"]["command"]), ("q", "w", "y"))
        self.assertIn("# mine", self.ct.read_text())

    def test_drift(self):
        self.run_it(brain_install.install)
        c = json.loads(self.cj.read_text())
        c["mcpServers"]["jarvis-brain"]["command"] = "/tmp/evil"
        self.cj.write_text(json.dumps(c))
        self.assertEqual(self.run_it(brain_install.status), 1)
        self.assertTrue(any("differ" in l.lower() or "changed" in l.lower() for l in self.out), self.out)
        self.assertEqual(self.run_it(brain_install.install), 0)
        self.assertEqual(self.run_it(brain_install.status), 0)

    def test_refusals_change_nothing(self):
        for which, text in (("cj", "{not json"), ("cj", "[]"), ("cj", '{"mcpServers": []}'), ("ct", "= broken"), ("ct", "mcp_servers = 5\n")):
            path = getattr(self, which)
            other = self.ct if which == "cj" else self.cj
            path.write_text(text)
            before = (path.read_bytes(), other.read_bytes())
            self.assertNotEqual(self.run_it(brain_install.install), 0, text)
            self.assertEqual((path.read_bytes(), other.read_bytes()), before, text)
            self.assertEqual(self.backups(), [])
            self.cj.write_text(self.CLAUDE)
            self.ct.write_text(self.CODEX)

    def test_links_and_races(self):
        real = self.home / "real.json"
        real.write_text(self.CLAUDE)
        self.cj.unlink()
        self.cj.symlink_to(real)
        self.assertNotEqual(self.run_it(brain_install.install), 0)
        self.assertEqual(real.read_text(), self.CLAUDE)
        self.cj.unlink()
        self.cj.write_text(self.CLAUDE)
        original = os.replace

        def racing(src, dst, *a, **k):
            return original(src, dst, *a, **k)
        fsync = os.fsync

        def write_meanwhile(fd):
            if self.cj.read_text() == self.CLAUDE:
                self.cj.write_text(self.CLAUDE.replace("5", "7", 1))
            return fsync(fd)
        with patch.object(os, "fsync", write_meanwhile):
            self.assertNotEqual(self.run_it(brain_install.install), 0)
        self.assertEqual(json.loads(self.cj.read_text())["numStartups"], 7)

    def test_missing_consumers(self):
        self.ct.unlink()
        (self.home / ".codex").rmdir()
        self.cj.unlink()
        self.assertEqual(self.run_it(brain_install.install), 0)
        self.assertFalse((self.home / ".codex").exists())
        self.assertIn("jarvis-brain", json.loads(self.cj.read_text())["mcpServers"])
        self.assertEqual(stat.S_IMODE(self.cj.stat().st_mode), 0o600)
        self.assertEqual(self.run_it(brain_install.uninstall), 0)
        self.assertTrue(not self.cj.exists() or "jarvis-brain" not in self.cj.read_text())

    def test_codex_without_a_config_file(self):
        self.ct.unlink()
        self.assertEqual(self.run_it(brain_install.install), 0)
        self.assertIn("jarvis-brain", tomllib.loads(self.ct.read_text())["mcp_servers"])
        self.run_it(brain_install.uninstall)
        self.assertTrue(not self.ct.exists() or self.ct.read_text().strip() == "")

    def test_cli(self):
        from jarvis.v3 import cli
        import argparse
        parser = argparse.ArgumentParser()
        cli.add_commands(parser.add_subparsers(dest="command"))
        for action in ("mcp", "install", "status", "uninstall"):
            self.assertTrue(callable(parser.parse_args(["brain", action]).func))


if __name__ == "__main__":
    unittest.main()
