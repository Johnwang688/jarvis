"""Orchestrator-owned acceptance tests for K2 (implementers: do not edit; if one is wrong, stop and say why).

Contract: jarvis/v3/claude_jobs.py
  run_job(name, prompt, *, model, tools=(), mcp_servers=None, cwd, timeout, env_extra=None, review=False,
          cancel: threading.Event | None = None, cli: str | None = None) -> JobResult
  JobResult (frozen): name, model, ok: bool, outcome: "ok"|"error"|"timeout"|"cancelled", text: str, error: str,
          input_tokens, output_tokens (ints; input includes both cache counts), cost_usd: float|None, seconds: float, session_id: str|None
  Expected failures are results (ok False, a one-line `error` the owner can read), never exceptions. Bad arguments raise ValueError.
  ALLOWED_TOOLS: frozenset[str]    built-in tools a job may be given: {"Read", "Grep", "Glob"}. Plus any "mcp__<server>__<tool>" whose
                                   <server> is in `mcp_servers`. Anything else (Bash, Write, Edit, WebFetch, Task, "*", "Bash(ls)") -> ValueError.
  MIN_CLI = "2.1.325"              older, or unreadable -> outcome "error" and nothing runs
  MODELS: frozenset[str]           {"haiku", "sonnet", "opus"}; anything else -> ValueError
argv (after the CLI path), in any order but exactly these:
  -p --output-format json --model M --permission-mode dontAsk --strict-mcp-config --mcp-config FILE
  --setting-sources= (one empty value) --no-session-persistence --disable-slash-commands --tools CSV-of-builtins ("" when none)
  --allowedTools CSV (omitted when there are none)
The prompt goes on stdin, never argv. The MCP file: 0600 in a 0700 folder under config.V3_DIR/jobs, removed afterwards, whatever happened.
The environment is built from a keep list: nothing of the daemon's own keys reaches the child.
"""
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import config
from jarvis.v3 import claude_jobs, imports
from jarvis.v3.claude_jobs import run_job

REPO = Path(__file__).resolve().parents[2]
PLANTED = "ghp" + "_" + "Z9y8" * 9


def alive(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(dir="/var/tmp"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.cli = self.bin / "claude"
        shutil.copy(REPO / "tests/v3/fakes/fake_claude.py", self.cli)
        self.cli.chmod(0o755)
        self.control()
        self.cwd = self.tmp / "work"
        self.cwd.mkdir()
        p = patch.object(config, "V3_DIR", self.tmp / "v3")
        p.start()
        self.addCleanup(p.stop)

    def control(self, **kw):
        (self.bin / "control.json").write_text(json.dumps(kw))

    def seen(self):
        return json.loads((self.bin / "seen.json").read_text())

    def run_job(self, prompt="hello", **kw):
        args = {"model": "haiku", "cwd": self.cwd, "timeout": 20, "cli": str(self.cli)}
        args.update(kw)
        return run_job(args.pop("name", "reviewer"), prompt, **args)

    def leftovers(self):
        jobs = self.tmp / "v3" / "jobs"
        return sorted(str(p) for p in jobs.rglob("*") if p.is_file()) if jobs.exists() else []


class Argv(Base):
    def test_exact_argv_without_tools(self):
        r = self.run_job()
        self.assertTrue(r.ok, r)
        argv = self.seen()["argv"]
        path = argv[argv.index("--mcp-config") + 1]
        want = ["-p", "--output-format", "json", "--model", "haiku", "--permission-mode", "dontAsk", "--strict-mcp-config",
                "--mcp-config", path, "--setting-sources=", "--no-session-persistence", "--disable-slash-commands", "--tools", ""]
        self.assertEqual(sorted(argv), sorted(want))
        for flag, value in (("--output-format", "json"), ("--model", "haiku"), ("--permission-mode", "dontAsk"), ("--tools", "")):
            self.assertEqual(argv[argv.index(flag) + 1], value)

    def test_tools(self):
        servers = {"brain": {"command": "x", "args": []}}
        self.run_job(tools=("Read", "mcp__brain__memory_add", "Grep"), mcp_servers=servers)
        argv = self.seen()["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep")
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Read,mcp__brain__memory_add,Grep")
        self.assertEqual(json.loads(self.seen()["mcp"]["text"]), {"mcpServers": servers})

    def test_refused_arguments_run_nothing(self):
        servers = {"brain": {"command": "x"}}
        bad = [dict(tools=(t,), mcp_servers=servers) for t in
               ("Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch", "Task", "Agent", "Skill", "*", "Bash(ls:*)", "Read(/etc/**)",
                "mcp__other__x", "mcp__brain", "mcp__brain__*", "mcp__*", "mcp__brain__a,Bash", "Read Bash", "", " Read", "read", "default", "--x")]
        bad += [dict(model=m) for m in ("", "gpt", "haiku --x", "claude-haiku-5-5", None)]
        bad += [dict(name=n) for n in ("", "a b", "../x", "A" * 80, "a\nb", "-x")]
        bad += [dict(prompt=p) for p in ("", None, b"x", "a\x00b")]
        bad += [dict(timeout=t) for t in (0, -1, None, float("nan"), float("inf"))]
        bad += [dict(cwd=c) for c in (self.tmp / "missing", self.cli, "relative")]
        bad += [dict(mcp_servers=s) for s in ([], {"a b": {}}, {"a__b": {}}, {"": {}}, {"x": "y"})]
        bad += [dict(env_extra=e) for e in ({"PATH": "/x"}, {"ANTHROPIC_API_KEY": "k"}, {"LD_PRELOAD": "x"}, {"JARVIS_JOB": "x"}, {"JARVIS_REVIEWER": "0"},
                                            {"JARVIS_X": 1}, {"JARVIS_X": "a\x00"}, {"CLAUDE_CONFIG_DIR": "/x"})]
        for kw in bad:
            with self.subTest(kw=repr(kw)[:90]):
                with self.assertRaises(ValueError):
                    self.run_job(**kw)
        self.assertFalse((self.bin / "seen.json").exists())
        self.assertEqual(self.leftovers(), [])

    def test_the_prompt_is_stdin_only(self):
        prompt = "secret-ish prompt é\n--dangerous\n" * 3
        self.run_job(prompt)
        seen = self.seen()
        self.assertEqual(seen["stdin"], prompt)
        self.assertNotIn("secret-ish", json.dumps(seen["argv"]) + json.dumps(seen["env"]))

    def test_a_large_prompt_does_not_deadlock(self):
        prompt = "word " * 400_000
        self.assertTrue(self.run_job(prompt).ok)
        self.assertEqual(len(self.seen()["stdin"]), len(prompt))

    def test_cwd(self):
        self.run_job()
        self.assertEqual(os.path.realpath(self.seen()["cwd"]), os.path.realpath(self.cwd))


class Env(Base):
    def test_only_the_keep_list_and_the_guards(self):
        dirty = {"OPENROUTER_API_KEY": "k", "ANTHROPIC_API_KEY": "k", "ANTHROPIC_BASE_URL": "u", "CLAUDE_CODE_OAUTH_TOKEN": "t", "GITHUB_TOKEN": "t",
                 "JARVIS_V3_PORT": "1", "PYTHONPATH": "/x", "LD_PRELOAD": "", "NODE_OPTIONS": "--require x", "CLAUDECODE": "1", "SSH_AUTH_SOCK": "/s",
                 "CLAUDE_CODE_ENTRYPOINT": "sdk", "DBUS_SESSION_BUS_ADDRESS": "x", "LC_ALL": "C.UTF-8", "RANDOM_THING": "1"}
        with patch.dict(os.environ, dirty):
            self.run_job(env_extra={"JARVIS_SESSION": "abc"})
        env = self.seen()["env"]
        for key in dirty:
            if key != "LC_ALL":
                self.assertIn(env.get(key, ""), ("",), key)
        self.assertEqual(env["LC_ALL"], "C.UTF-8")
        self.assertEqual(env["HOME"], os.environ["HOME"])
        self.assertEqual(env["JARVIS_JOB"], "reviewer")
        self.assertEqual(env["JARVIS_SESSION"], "abc")
        self.assertEqual(env.get("JARVIS_REVIEWER", ""), "")
        self.assertEqual(env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"], "1")

    def test_review_jobs_say_so(self):
        with patch.dict(os.environ, {"JARVIS_JOB": "outer", "JARVIS_REVIEWER": "0"}):
            self.run_job(review=True)
        self.assertEqual((self.seen()["env"]["JARVIS_JOB"], self.seen()["env"]["JARVIS_REVIEWER"]), ("reviewer", "1"))


class McpFile(Base):
    def test_private_and_removed(self):
        self.run_job(mcp_servers={"brain": {"command": "x", "env": {"T": "v"}}})
        mcp = self.seen()["mcp"]
        self.assertEqual((mcp["mode"], mcp["dirmode"]), (0o600, 0o700))
        self.assertTrue(mcp["path"].startswith(str(self.tmp / "v3" / "jobs") + "/"))
        self.assertFalse(os.path.exists(mcp["path"]))
        self.assertEqual(self.leftovers(), [])

    def test_removed_after_every_kind_of_failure(self):
        for control, kw in (({"exit": 3}, {}), ({"raw": "nope"}, {}), ({"sleep": 30}, {"timeout": 0.5}), ({"version": "1.0.0"}, {})):
            self.control(**control)
            self.assertFalse(self.run_job(**kw).ok)
            self.assertEqual(self.leftovers(), [])
        self.assertFalse(self.run_job(cli=str(self.tmp / "missing")).ok)
        self.assertEqual(self.leftovers(), [])

    def test_a_linked_jobs_folder_is_refused(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (self.tmp / "v3").mkdir()
        (self.tmp / "v3" / "jobs").symlink_to(elsewhere)
        self.assertFalse(self.run_job(mcp_servers={"b": {"command": "x"}}).ok)
        self.assertEqual(list(elsewhere.iterdir()), [])


class Results(Base):
    def test_success(self):
        r = self.run_job()
        self.assertEqual((r.ok, r.outcome, r.text, r.error, r.name, r.model, r.session_id), (True, "ok", "done", "", "reviewer", "haiku", "s-1"))
        self.assertEqual((r.input_tokens, r.output_tokens, r.cost_usd), (60, 4, 0.0012))
        self.assertGreater(r.seconds, 0)
        with self.assertRaises(Exception):
            r.ok = False

    def test_failures_are_results(self):
        ok = {"type": "result", "subtype": "success", "is_error": False, "result": "x", "usage": {}}
        for control in ({"exit": 2}, {"raw": ""}, {"raw": "[]"}, {"raw": "{"}, {"raw": "null"}, {"result": {**ok, "is_error": True}},
                        {"result": {**ok, "subtype": "error_max_turns"}}, {"result": {**ok, "type": "other"}}, {"result": {**ok, "result": 5}},
                        {"result": {k: v for k, v in ok.items() if k != "is_error"}}, {"result": ok, "exit": 1}):
            with self.subTest(control=repr(control)[:80]):
                self.control(**control)
                r = self.run_job()
                self.assertEqual((r.ok, r.outcome), (False, "error"))
                self.assertTrue(r.error and "\n" not in r.error and len(r.error) < 400, r.error)

    def test_a_failed_run_still_reports_what_it_spent(self):
        self.control(result={"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "", "total_cost_usd": 0.5,
                             "usage": {"input_tokens": 7, "output_tokens": 2}})
        r = self.run_job()
        self.assertEqual((r.ok, r.input_tokens, r.output_tokens, r.cost_usd), (False, 7, 2, 0.5))

    def test_odd_usage_is_never_trusted(self):
        for usage in (None, [], {"input_tokens": "9", "output_tokens": -4}, {"input_tokens": True, "output_tokens": 1.5}, {"input_tokens": 10 ** 30}):
            self.control(result={"type": "result", "subtype": "success", "is_error": False, "result": "x", "usage": usage, "total_cost_usd": "free"})
            r = self.run_job()
            self.assertTrue(r.ok)
            self.assertTrue(all(type(n) is int and 0 <= n < 10 ** 12 for n in (r.input_tokens, r.output_tokens)), (usage, r))
            self.assertIsNone(r.cost_usd)

    def test_version_floor(self):
        self.assertEqual(claude_jobs.MIN_CLI, "2.1.325")
        for version, ok in (("2.1.324 (Claude Code)", False), ("2.0.999", False), ("1.9.9", False), ("banana", False), ("", False),
                            ("2.1.325 (Claude Code)", True), ("2.1.1000", True), ("3.0.0 (Claude Code)", True)):
            with self.subTest(version=version):
                (self.bin / "seen.json").unlink(missing_ok=True)
                self.control(version=version)
                r = self.run_job()
                self.assertEqual(r.ok, ok, r)
                self.assertEqual((self.bin / "seen.json").exists(), ok)

    def test_no_cli(self):
        with patch.object(claude_jobs, "resolve_cli", return_value=None):
            r = self.run_job(cli=None)
        self.assertEqual((r.ok, r.outcome), (False, "error"))

    def test_the_default_cli_is_the_providers(self):
        with patch.object(claude_jobs, "resolve_cli", return_value=str(self.cli)) as resolve:
            self.assertTrue(self.run_job(cli=None).ok)
        resolve.assert_called_once_with()
        from jarvis.v2.providers import claude
        self.assertIn("jarvis.v2.providers.claude", imports.ALLOWED_V2)
        self.assertEqual(imports.violations(REPO / "jarvis/v3"), [])
        with patch.object(claude, "resolve_cli", return_value="/sentinel"):
            self.assertEqual(claude_jobs.resolve_cli(), "/sentinel")


class Stopping(Base):
    def wait(self, test, seconds=10):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if test():
                return True
            time.sleep(0.05)
        return False

    def test_timeout_kills_the_whole_group(self):
        for ignore in (False, True):
            with self.subTest(ignore_term=ignore):
                (self.bin / "seen.json").unlink(missing_ok=True)
                self.control(sleep=600, grandchild=True, ignore_term=ignore)
                start = time.monotonic()
                r = self.run_job(timeout=2)
                self.assertLess(time.monotonic() - start, 12)
                self.assertEqual((r.ok, r.outcome), (False, "timeout"))
                seen = self.seen()
                self.assertTrue(self.wait(lambda: not alive(seen["pid"]) and not alive(seen["grandchild"])))

    def test_a_grandchild_does_not_outlive_a_finished_job(self):
        self.control(grandchild=True)
        r = self.run_job(timeout=10)
        self.assertEqual(r.outcome, "ok")
        self.assertLess(r.seconds, 8)
        self.assertTrue(self.wait(lambda: not alive(self.seen()["grandchild"])))

    def test_cancel(self):
        self.control(sleep=600, grandchild=True)
        cancel = threading.Event()
        threading.Timer(1.0, cancel.set).start()
        r = self.run_job(cancel=cancel, timeout=60)
        self.assertEqual((r.ok, r.outcome), (False, "cancelled"))
        self.assertTrue(self.wait(lambda: not alive(self.seen()["pid"]) and not alive(self.seen()["grandchild"])))

    def test_cancelled_before_it_starts(self):
        cancel = threading.Event()
        cancel.set()
        self.assertEqual(self.run_job(cancel=cancel).outcome, "cancelled")
        self.assertFalse((self.bin / "seen.json").exists())

    def test_endless_output_is_cut_off(self):
        self.control(flood=200 * 1024 * 1024)
        start = time.monotonic()
        r = self.run_job(timeout=60)
        self.assertEqual((r.ok, r.outcome), (False, "error"))
        self.assertLess(time.monotonic() - start, 30)

    def test_it_never_signals_its_own_group(self):
        code = ("import os,sys; sys.path.insert(0, %r); from pathlib import Path; from jarvis import config; config.V3_DIR=Path(%r); "
                "from jarvis.v3.claude_jobs import run_job; print(run_job('a','b',model='haiku',cwd=Path(%r),timeout=1,cli=%r).outcome)")
        self.control(sleep=600)
        out = subprocess.run([sys.executable, "-c", code % (str(REPO), str(self.tmp / "v3"), str(self.cwd), str(self.cli))],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "timeout"), out.stderr)


class Privacy(Base):
    def test_nothing_private_reaches_the_log_or_the_error(self):
        self.control(exit=1, stderr=f"boom {PLANTED} at /x\n" * 50)
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("jarvis.anchor").debug("anchor")
            r = self.run_job(f"transcript says {PLANTED} and private-words", mcp_servers={"b": {"command": "x", "env": {"K": PLANTED}}})
        text = "\n".join(logs.output) + r.error + repr(r)
        self.assertNotIn(PLANTED, text)
        self.assertNotIn("private-words", text)

    def test_the_answer_is_not_in_repr(self):
        self.control(result={"type": "result", "subtype": "success", "is_error": False, "result": "private-answer", "usage": {}})
        r = self.run_job()
        self.assertEqual(r.text, "private-answer")
        self.assertNotIn("private-answer", repr(r))

    def test_importing_it_starts_nothing_and_stays_light(self):
        code = ("import sys, jarvis.v3.claude_jobs; "
                "print([m for m in ('claude_agent_sdk','jarvis.v2.daemon','jarvis.agent','jarvis.llm') if m in sys.modules])")
        out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env={**os.environ, "PYTHONPATH": str(REPO)}, capture_output=True,
                             text=True, check=True)
        self.assertEqual(out.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
