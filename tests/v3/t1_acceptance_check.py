"""Orchestrator-owned acceptance tests for T1 (implementers: do not edit; if one is wrong, stop and say why).

Contract: a thin client over T3 Code's documented `t3 thread` CLI. Shapes and exit codes: docs/plans/2026-11-24-v3-spike-findings.md (S3)
and tests/v3/fixtures/t3/thread_cli_shapes.json. Only the CLI: never T3's WebSocket, database or files.
  jarvis/v3/t3_client.py
    T3Error(Exception)   .kind in {"missing", "arguments", "failed", "shape", "timeout"}; str() is one line the owner can read, never the prompt
    resolve(*, home=None, env=None) -> Path
        env JARVIS_T3_BIN when set (must be an executable regular file, else "missing"); otherwise the most recently modified
        executable regular file <home>/.t3/wsl-runtime/<dir>/t3. Nothing found -> T3Error("missing"). Resolved at call time, never cached.
    T3(binary=None, *, timeout=60)                                   binary None -> resolve() on every call
      start(project, message, *, provider, model, title=None, worktree=None, branch=None) -> Run(thread_id, run_id, status)
      send(thread_id, message) -> Run
      show(thread_id, *, messages=4) -> State(thread_id, status, waiting_on_owner, pending_requests, title, provider, model, messages)
                                                                     messages: list of (role, text)
      list(project) -> list[State]                                   messages empty
      wait(thread_id, *, timeout, poll_s=2.0, sleep=time.sleep, clock=time.monotonic) -> State
                                                                     polls show() while the status is a running one and returns on a
                                                                     settled one (SETTLED = idle, waiting, completed, interrupted, failed,
                                                                     cancelled, rolled_back); a status in neither set is T3Error("shape"):
                                                                     an unknown status never reads as finished.
                                                                     a waiting thread returns with waiting_on_owner True; timeout -> T3Error("timeout")
    Every call: `<t3> thread <verb> --json ...`, no shell, the message on stdin as `-` (never in argv), a minimal environment
    (PATH, HOME, T3CODE_HOME and LANG only), a timeout. Exit 0 and exit 2 are answers (2 = waiting on the owner); anything else is
    T3Error("failed"). JSON that is missing a key the client uses, or has it with another type, is T3Error("shape").
    RUNNING = {"queued", "preparing", "starting", "running"}. provider and model are required: a start without them is refused
    before anything runs (a project with no default model would fail anyway).
"""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from jarvis.v3 import t3_client
from jarvis.v3.t3_client import T3, T3Error, resolve

REPO = Path(__file__).resolve().parents[2]
SHAPES = json.loads((REPO / "tests" / "v3" / "fixtures" / "t3" / "thread_cli_shapes.json").read_text())
FAKE = '''#!%s -I
import json, os, sys
plan = json.load(open(os.environ["HOME"] + "/plan.json"))
verb = sys.argv[2] if len(sys.argv) > 2 else ""
step = plan.get(verb)
if isinstance(step, list):
    step = step.pop(0) if len(step) > 1 else step[0]
    json.dump(plan, open(os.environ["HOME"] + "/plan.json", "w"))
with open(os.environ["HOME"] + "/calls.jsonl", "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read() if "-" in sys.argv else None, "env": sorted(os.environ)}) + "\\n")
if step is None:
    sys.stderr.write("unknown command\\n"); sys.exit(1)
sys.stdout.write(step.get("out", "")); sys.stderr.write(step.get("err", "")); sys.exit(step.get("exit", 0))
''' % sys.executable


def thread(status="completed", **kw):
    row = {"threadId": "mcp:t-1", "status": status, "title": "A title", "providerInstanceId": "codex", "model": "gpt-6.2-sol",
           "pendingRequestCount": 0, "projectId": "p-1", "latestRunId": "run:1", "archived": False}
    return {**row, **kw}


class Base(unittest.TestCase):
    def setUp(self):
        self.home = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        self.bin = self.make("a")
        self.t3 = T3(self.bin, timeout=20)
        self.old = dict(os.environ)
        os.environ.update({"HOME": str(self.home), "SECRET_TOKEN_FOR_TEST": "do-not-pass"})
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.old)))

    def make(self, name, mtime=None):
        path = self.home / ".t3" / "wsl-runtime" / name / "t3"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(FAKE)
        path.chmod(0o700)
        if mtime:
            os.utime(path, (mtime, mtime))
        return path

    def plan(self, **steps):
        plan = {verb: [self.norm(s) for s in step] if isinstance(step, list) else self.norm(step) for verb, step in steps.items()}
        (self.home / "plan.json").write_text(json.dumps(plan))

    @staticmethod
    def norm(step):
        return {**step, "out": json.dumps(step["out"]) if not isinstance(step.get("out", ""), str) else step.get("out", "")}

    def calls(self):
        path = self.home / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def fails(self, kind, fn, *args, **kw):
        with self.assertRaises(T3Error) as caught:
            fn(*args, **kw)
        self.assertEqual(caught.exception.kind, kind, str(caught.exception))
        self.assertNotIn("\n", str(caught.exception))
        return caught.exception


class Calls(Base):
    def test_start(self):
        self.plan(start={"out": {"threadId": "mcp:t-1", "runId": "run:1", "status": "running", "reply": None, "error": None}})
        run = self.t3.start("/work/project", "Fix the failing test.\nSecond line", provider="codex", model="gpt-6.2-sol", title="Fix test", worktree="main", branch="jarvis/fix")
        self.assertEqual((run.thread_id, run.run_id, run.status), ("mcp:t-1", "run:1", "running"))
        (call,) = self.calls()
        argv = call["argv"]
        self.assertEqual(argv[:2], ["thread", "start"])
        self.assertIn("--json", argv)
        self.assertNotIn("--wait", argv)
        for flag, value in (("--provider", "codex"), ("--model", "gpt-6.2-sol"), ("--title", "Fix test"), ("--worktree", "main"), ("--branch", "jarvis/fix")):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertEqual(argv[-2:], ["/work/project", "-"])
        self.assertEqual(call["stdin"], "Fix the failing test.\nSecond line")
        self.assertNotIn("Fix the failing test", " ".join(argv))
        self.assertLessEqual(set(call["env"]), {"PATH", "HOME", "T3CODE_HOME", "LANG"})

    def test_start_needs_a_provider_and_a_model(self):
        self.plan(start={"out": {}})
        for kw in ({}, {"provider": "codex"}, {"model": "x"}, {"provider": "", "model": "x"}, {"provider": "codex", "model": "--help"}, {"provider": "-x", "model": "m"}):
            with self.assertRaises((T3Error, TypeError, ValueError)):
                self.t3.start("/work/project", "hello", **kw)
        for project, message in (("", "hi"), ("--help", "hi"), ("/work/project", ""), ("/work/project", "a\x00b")):
            with self.assertRaises((T3Error, ValueError)):
                self.t3.start(project, message, provider="codex", model="m")
        self.assertEqual(self.calls(), [])

    def test_send_show_list(self):
        self.plan(send={"out": {"threadId": "mcp:t-1", "runId": "run:2", "messageId": "m-1", "delivery": "queued", "status": "running", "link": "x"}},
                  show={"out": {"thread": thread("waiting", pendingRequestCount=1), "messages": [{"role": "user", "runId": "run:1", "text": "hello"}, {"role": "assistant", "runId": "run:1", "text": "Which colour?"}]}},
                  list={"out": [thread(), thread("running", threadId="mcp:t-2")]})
        self.assertEqual(self.t3.send("mcp:t-1", "more").run_id, "run:2")
        state = self.t3.show("mcp:t-1", messages=2)
        self.assertEqual((state.thread_id, state.status, state.waiting_on_owner, state.pending_requests, state.provider, state.model, state.title),
                         ("mcp:t-1", "waiting", True, 1, "codex", "gpt-6.2-sol", "A title"))
        self.assertEqual(state.messages, [("user", "hello"), ("assistant", "Which colour?")])
        rows = self.t3.list("/work/project")
        self.assertEqual([(r.thread_id, r.status, r.waiting_on_owner) for r in rows], [("mcp:t-1", "completed", False), ("mcp:t-2", "running", False)])
        send, show, listed = (c["argv"] for c in self.calls())
        self.assertEqual((send[:2], send[-2:]), (["thread", "send"], ["mcp:t-1", "-"]))
        self.assertEqual(show[show.index("--messages") + 1], "2")
        self.assertEqual(listed[listed.index("--project") + 1], "/work/project")
        for bad in ("", "--help", "a b\nc", "x" * 500):
            with self.assertRaises((T3Error, ValueError)):
                self.t3.show(bad)

    def test_exit_two_is_waiting_not_a_failure(self):
        self.plan(start={"exit": 2, "out": {"threadId": "mcp:t-1", "runId": "run:1", "status": "waiting", "reply": None, "error": None}})
        self.assertEqual(self.t3.start("/work/project", "hi", provider="codex", model="m").status, "waiting")

    def test_wait_polls_until_done_or_waiting(self):
        self.plan(show=[{"out": {"thread": thread("running"), "messages": []}}, {"out": {"thread": thread("running"), "messages": []}},
                        {"out": {"thread": thread("completed"), "messages": [{"role": "assistant", "runId": "r", "text": "done"}]}}])
        naps, tick = [], iter(range(0, 1000, 2))
        state = self.t3.wait("mcp:t-1", timeout=60, poll_s=2.0, sleep=naps.append, clock=lambda: next(tick))
        self.assertEqual((state.status, len(self.calls()), naps), ("completed", 3, [2.0, 2.0]))
        self.plan(show={"out": {"thread": thread("waiting", pendingRequestCount=2), "messages": []}})
        self.assertTrue(self.t3.wait("mcp:t-1", timeout=60, sleep=naps.append, clock=lambda: next(tick)).waiting_on_owner)
        self.plan(show={"out": {"thread": thread("running"), "messages": []}})
        self.fails("timeout", self.t3.wait, "mcp:t-1", timeout=5, poll_s=2.0, sleep=naps.append, clock=lambda: next(tick))
        self.assertEqual(t3_client.RUNNING, {"queued", "preparing", "starting", "running"})


class Errors(Base):
    def test_failures_are_typed_and_one_line(self):
        self.plan(show={"exit": 1, "err": "Thread not found: secret prompt text\nsecond line\n"})
        err = self.fails("failed", self.t3.show, "mcp:nope")
        self.assertLess(len(str(err)), 300)
        self.plan(show={"exit": 139, "out": "", "err": ""})
        self.fails("failed", self.t3.show, "mcp:t-1")

    def test_unknown_shapes(self):
        good = {"thread": thread(), "messages": []}
        for out in ("", "not json", "[]", "null", json.dumps({"thread": {}}), json.dumps({"thread": thread(status=5), "messages": []}),
                    json.dumps({"thread": thread(), "messages": [{"role": "user"}]}), json.dumps({"thread": thread(), "messages": "x"}),
                    json.dumps({**good, "thread": {k: v for k, v in thread().items() if k != "threadId"}}), json.dumps({"thread": thread(pendingRequestCount="1"), "messages": []})):
            self.plan(show={"out": out})
            self.fails("shape", self.t3.show, "mcp:t-1")
        self.plan(show={"out": {**good, "thread": thread(newField={"x": 1}), "extra": True}})
        self.assertEqual(self.t3.show("mcp:t-1").status, "completed")                 # new keys are fine
        self.plan(start={"out": {"threadId": "mcp:t-1", "status": "running"}}, list={"out": {"threads": []}})
        self.fails("shape", self.t3.start, "/work/project", "hi", provider="codex", model="m")
        self.fails("shape", self.t3.list, "/work/project")

    def test_the_fixture_shapes_are_what_the_client_reads(self):
        for key in ("threadId", "runId", "status"):
            self.assertIn(key, SHAPES["thread_start_wait"])
            self.assertIn(key, SHAPES["thread_send"])
        for key in ("threadId", "status", "title", "providerInstanceId", "model", "pendingRequestCount"):
            self.assertIn(key, SHAPES["thread_show"]["thread"])
        self.assertEqual(set(SHAPES["thread_show"]["messages"][0]), {"role", "runId", "text"})
        self.assertEqual(SHAPES["exit_codes"]["waiting_for_an_answer"], 2)

    def test_a_hung_cli_times_out(self):
        slow = self.home / "slow"
        slow.write_text("#!/bin/sh\nsleep 30\n")
        slow.chmod(0o700)
        self.fails("timeout", T3(slow, timeout=0.5).show, "mcp:t-1")


class Resolve(Base):
    def test_newest_runtime_wins(self):
        old, new = self.make("sha256-old", mtime=1_700_000_000), self.make("sha256-new", mtime=1_800_000_000)
        os.utime(self.bin, (1_600_000_000, 1_600_000_000))
        self.assertEqual(resolve(home=self.home, env={}), new)
        (self.home / ".t3" / "wsl-runtime" / "sha256-newer").mkdir()
        broken = self.home / ".t3" / "wsl-runtime" / "sha256-newer" / "t3"
        broken.write_text("not executable")
        broken.chmod(0o600)
        self.assertEqual(resolve(home=self.home, env={}), new)                         # not executable: skipped
        (self.home / ".t3" / "wsl-runtime" / "sha256-link").mkdir()
        (self.home / ".t3" / "wsl-runtime" / "sha256-link" / "t3").symlink_to(self.home / "elsewhere")
        self.assertEqual(resolve(home=self.home, env={}), new)
        self.assertEqual(resolve(home=self.home, env={"JARVIS_T3_BIN": str(old)}), old)
        self.fails("missing", resolve, home=self.home, env={"JARVIS_T3_BIN": str(self.home / "nope")})
        self.fails("missing", resolve, home=self.home / "empty", env={})

    def test_resolved_per_call(self):
        client = T3(timeout=20)
        self.plan(list={"out": []})
        self.assertEqual(client.list("/work/project"), [])
        for path in (self.home / ".t3" / "wsl-runtime").glob("*/t3"):
            path.unlink()
        self.fails("missing", client.list, "/work/project")                            # T3 updated itself away: no stale path


if __name__ == "__main__":
    unittest.main()
