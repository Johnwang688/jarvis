"""Orchestrator-owned acceptance tests for L1 (implementers: do not edit; if one is wrong, stop and say why).

Contract: turn-end hooks -> review queue (contract 1.5). Payload shapes: docs/plans/2026-11-24-v3-spike-findings.md (S5).
  jarvis/v3/hooks/turn_end.py     stdlib only; runs as a plain script: `python turn_end.py claude` (payload on stdin, Claude's Stop hook)
                                  or `python turn_end.py codex <json>` (payload as the LAST argument, Codex's notify program)
    main(argv, stdin, env) -> 0   always 0, never raises, prints nothing. Writes at most one file REVIEW_QUEUE_DIR/<ts>-<rand>.json (0600,
                                  folder 0700, temp + rename): {"provider", "session_id", "transcript", "cwd", "ended_at"} and nothing else:
                                  never the assistant's or the user's message text. Queue folder: env JARVIS_V3_DIR/review-queue, else
                                  ~/.local/share/jarvis/v3/review-queue. Writes nothing when JARVIS_JOB is set (Jarvis's own jobs), when the
                                  payload is malformed, when Claude's stop_hook_active is true, or when the queue already holds 500 items.
                                  claude: transcript = payload transcript_path. codex: transcript = None (the consumer finds it).
  jarvis/v3/review_queue.py
    SKILL_STEPS = 25; MEMORY_TURNS = 10; MAX_AGE_S = 7 * 86400
    count(provider, transcript) -> (tool_steps, user_turns)      whole transcript so far; tolerant of torn or foreign lines
    Consumer(queue=None, state=None, *, clock=time.time, codex_sessions=None, claude_projects=None, count=count)
      A transcript is read only when it lies inside its provider's session folder (claude_projects, default ~/.claude/projects;
      codex_sessions, default ~/.codex/sessions): a hook payload's path is a claim, not a permission.
      tick() -> list of {"kind": "skill"|"memory", "provider", "session_id", "transcript", "tool_steps", "user_turns"}
        Reads every queue item, oldest first, and deletes it. Per session it keeps the counts at its last review of each kind
        (state/<name>.json, 0600); a skill review is due at >= SKILL_STEPS tool steps since the last skill review, a memory review at
        >= MEMORY_TURNS user turns since the last memory review. Several items for one session in one tick give at most one of each.
        Dropped silently: items older than MAX_AGE_S, a transcript that is missing, not a regular file, or under a credential path
        (jarvis.credential_patterns / the shared env-name helpers), a wrong-shaped item, a codex item whose session file is not found
        under codex_sessions (default ~/.codex/sessions; matched by thread id in the file name).
  jarvis brain install / uninstall / status also manage the two hooks: a `Stop` entry in ~/.claude/settings.json and a top-level
  `notify` in ~/.codex/config.toml, both running this script by absolute path with the repo's interpreter. Everything else in those
  files is kept byte for byte; an existing `notify` that is not ours is never replaced (install says so and still installs the rest).
"""
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from jarvis.v3 import brain_install, review_queue
from jarvis.v3.hooks import turn_end
from jarvis.v3.review_queue import Consumer

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "v3" / "fixtures" / "hooks"
CLAUDE = json.loads((FIXTURES / "claude_stop.json").read_text())
CODEX = json.loads((FIXTURES / "codex_turn_complete.json").read_text())


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        self.v3 = self.tmp / "v3"
        self.queue = self.v3 / "review-queue"
        self.env = {"JARVIS_V3_DIR": str(self.v3), "HOME": str(self.tmp / "home")}
        self.now = time.time()          # the hook stamps items with the wall clock

    def fire(self, provider, payload, env=None):
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        argv = ["turn_end.py", provider] + ([raw] if provider == "codex" else [])
        return turn_end.main(argv, io.StringIO("" if provider == "codex" else raw), {**self.env, **(env or {})})

    def items(self):
        return [json.loads(p.read_text()) for p in sorted(self.queue.iterdir())] if self.queue.exists() else []

    def transcript(self, name="t.jsonl", *, tools=0, turns=0, provider="claude"):
        path = self.tmp / "transcripts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for i in range(turns):
            lines.append({"type": "user", "message": {"role": "user", "content": f"question {i}"}} if provider == "claude"
                         else {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"question {i}"}]}})
        for i in range(tools):
            lines.append({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": f"c{i}", "name": "Bash", "input": {}}]}} if provider == "claude"
                         else {"type": "response_item", "payload": {"type": "function_call", "name": "shell", "call_id": f"c{i}", "arguments": "{}"}})
        path.write_text("".join(json.dumps(line) + "\n" for line in lines))
        return path

    def consumer(self, **kw):
        return Consumer(self.queue, self.v3 / "review-state", clock=lambda: self.now, codex_sessions=self.tmp / "codex-sessions",
                        claude_projects=self.tmp / "transcripts", **kw)

    def enqueue(self, path, session="s1", provider="claude", age=0.0):
        self.fire(provider, {**CLAUDE, "session_id": session, "transcript_path": str(path)} if provider == "claude" else {**CODEX, "thread-id": session})
        newest = max(self.queue.iterdir(), key=lambda p: p.stat().st_mtime_ns)
        item = json.loads(newest.read_text())
        if age:
            item["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now - age))
            newest.write_text(json.dumps(item))
        return newest


class Hook(Base):
    def test_fixture_payloads_parse(self):
        self.assertEqual(self.fire("claude", CLAUDE), 0)
        self.assertEqual(self.fire("codex", CODEX), 0)
        a, b = sorted(self.items(), key=lambda i: i["provider"])
        self.assertEqual(set(a), {"provider", "session_id", "transcript", "cwd", "ended_at"})
        self.assertEqual((a["provider"], a["session_id"], a["transcript"], a["cwd"]), ("claude", CLAUDE["session_id"], CLAUDE["transcript_path"], CLAUDE["cwd"]))
        self.assertEqual((b["provider"], b["session_id"], b["transcript"], b["cwd"]), ("codex", CODEX["thread-id"], None, CODEX["cwd"]))
        raw = "".join(p.read_text() for p in self.queue.iterdir())
        for private in (CLAUDE["last_assistant_message"], CODEX["last-assistant-message"], CODEX["input-messages"][0]):
            self.assertNotIn(private, raw)
        self.assertEqual(stat.S_IMODE(self.queue.stat().st_mode), 0o700)
        self.assertTrue(all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in self.queue.iterdir()))
        self.assertEqual([p.name for p in self.queue.iterdir() if not p.name.endswith(".json")], [])

    def test_malformed_payloads_write_nothing(self):
        bad = ["", "{", "[]", "null", "3", '"x"', json.dumps({**CLAUDE, "session_id": 7}), json.dumps({**CLAUDE, "transcript_path": "relative.jsonl"}),
               json.dumps({k: v for k, v in CLAUDE.items() if k != "session_id"}), json.dumps({**CLAUDE, "session_id": "a/../b"}),
               json.dumps({**CLAUDE, "session_id": "x" * 500}), json.dumps({**CLAUDE, "cwd": "x\x00y"}), "\ud800", "x" * (2 << 20)]
        for raw in bad:
            self.assertEqual(self.fire("claude", raw), 0, raw[:40])
        for raw in ("", "{", json.dumps({**CODEX, "type": "something-else"}), json.dumps({k: v for k, v in CODEX.items() if k != "thread-id"})):
            self.assertEqual(self.fire("codex", raw), 0)
        self.assertEqual(turn_end.main(["turn_end.py"], io.StringIO(json.dumps(CLAUDE)), self.env), 0)
        self.assertEqual(turn_end.main(["turn_end.py", "gemini"], io.StringIO(json.dumps(CLAUDE)), self.env), 0)
        self.assertEqual(turn_end.main(["turn_end.py", "codex"], io.StringIO(""), self.env), 0)
        self.assertEqual(self.items(), [])

    def test_recursion_guards(self):
        self.fire("claude", CLAUDE, env={"JARVIS_JOB": "reviewer"})
        self.fire("codex", CODEX, env={"JARVIS_JOB": "1"})
        self.fire("claude", {**CLAUDE, "stop_hook_active": True})
        self.assertEqual(self.items(), [])

    def test_a_full_or_broken_queue_never_fails_the_session(self):
        self.queue.mkdir(parents=True)
        for i in range(500):
            (self.queue / f"{i:04}.json").write_text("{}")
        self.assertEqual(self.fire("claude", CLAUDE), 0)
        self.assertEqual(len(list(self.queue.iterdir())), 500)
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        other = {**self.env, "JARVIS_V3_DIR": str(self.tmp / "v3b")}
        (self.tmp / "v3b").mkdir()
        (self.tmp / "v3b" / "review-queue").symlink_to(elsewhere)
        self.assertEqual(turn_end.main(["turn_end.py", "claude"], io.StringIO(json.dumps(CLAUDE)), other), 0)
        self.assertEqual(list(elsewhere.iterdir()), [])                 # a linked queue folder is not followed
        blocked = {**self.env, "JARVIS_V3_DIR": "/proc/nonexistent/v3"}
        self.assertEqual(turn_end.main(["turn_end.py", "claude"], io.StringIO(json.dumps(CLAUDE)), blocked), 0)

    def test_fast_stdlib_only_and_runs_as_a_script(self):
        started = time.perf_counter()
        for _ in range(20):
            self.fire("claude", CLAUDE)
        self.assertLess((time.perf_counter() - started) / 20, 0.05)
        script = Path(turn_end.__file__)
        code = ("import sys, runpy; before = set(sys.modules); sys.argv = [%r, 'claude']\n"
                "try:\n    runpy.run_path(%r, run_name='__main__')\nexcept SystemExit as e:\n    assert not e.code, e.code\n"
                "new = sorted(m for m in set(sys.modules) - before if m.split('.')[0] in ('jarvis', 'httpx', 'openai', 'anthropic'))\n"
                "print(new)") % (str(script), str(script))
        out = subprocess.run([sys.executable, "-I", "-c", code], input=json.dumps(CLAUDE), capture_output=True, text=True, timeout=30,
                             env={"JARVIS_V3_DIR": str(self.v3), "HOME": str(self.tmp / "home"), "PATH": os.environ.get("PATH", "")})
        self.assertEqual((out.returncode, out.stdout.strip(), out.stderr), (0, "[]", ""))
        self.assertEqual(len(self.items()), 21)


class Consume(Base):
    def test_thresholds(self):
        path = self.transcript(tools=24, turns=9)
        self.enqueue(path)
        self.assertEqual(self.consumer().tick(), [])
        self.assertEqual(self.items(), [])                              # consumed either way
        self.transcript(tools=25, turns=9)
        self.enqueue(path)
        out = self.consumer().tick()
        self.assertEqual([(r["kind"], r["tool_steps"], r["user_turns"], r["session_id"], r["transcript"]) for r in out], [("skill", 25, 9, "s1", str(path))])
        self.transcript(tools=49, turns=10)
        self.enqueue(path)
        self.assertEqual([r["kind"] for r in self.consumer().tick()], ["memory"])
        self.transcript(tools=50, turns=19)
        self.enqueue(path)
        self.assertEqual([r["kind"] for r in self.consumer().tick()], ["skill"])
        self.assertEqual((review_queue.SKILL_STEPS, review_queue.MEMORY_TURNS), (25, 10))

    def test_sessions_are_counted_apart_and_once_per_tick(self):
        a, b = self.transcript("a.jsonl", tools=25, turns=12), self.transcript("b.jsonl", tools=3, turns=11)
        for _ in range(3):
            self.enqueue(a, "sa")
        self.enqueue(b, "sb")
        out = sorted((r["session_id"], r["kind"]) for r in self.consumer().tick())
        self.assertEqual(out, [("sa", "memory"), ("sa", "skill"), ("sb", "memory")])

    def test_dropped_items(self):
        good = self.transcript(tools=25)
        self.enqueue(good, "old", age=8 * 86400)
        self.enqueue(self.tmp / "missing.jsonl", "gone")
        secret = self.tmp / "transcripts" / ".env"
        secret.write_text(good.read_text())
        self.enqueue(secret, "cred")
        folder = self.tmp / "transcripts" / "dir.jsonl"
        folder.mkdir()
        self.enqueue(folder, "dir")
        link = self.tmp / "transcripts" / "link.jsonl"
        link.symlink_to(secret)
        self.enqueue(link, "link")
        (self.queue / "0000-junk.json").write_text("{")
        (self.queue / "0001-shape.json").write_text(json.dumps({"provider": "claude", "session_id": 5}))
        self.assertEqual(self.consumer().tick(), [])
        self.assertEqual(self.items(), [])
        self.enqueue(good, "fresh", age=6 * 86400)
        self.assertEqual([r["session_id"] for r in self.consumer().tick()], ["fresh"])

    def test_codex_sessions_are_found_by_thread_id(self):
        thread = CODEX["thread-id"]
        day = self.tmp / "codex-sessions" / "2030" / "01" / "15"
        day.mkdir(parents=True)
        source = self.transcript("c.jsonl", tools=25, turns=2, provider="codex")
        target = day / f"rollout-2030-01-15T10-00-00-{thread}.jsonl"
        target.write_text(source.read_text())
        self.enqueue(None, thread, provider="codex")
        out = self.consumer().tick()
        self.assertEqual([(r["kind"], r["provider"], r["transcript"], r["tool_steps"]) for r in out], [("skill", "codex", str(target), 25)])
        self.enqueue(None, "00000000-0000-7000-8000-00000000ffff", provider="codex")
        self.assertEqual(self.consumer().tick(), [])

    def test_count_tolerates_torn_lines(self):
        path = self.transcript(tools=4, turns=3)
        with open(path, "a") as f:
            f.write('{"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c0", "content": "ok"}]}}\n')   # a tool result is not a user turn
            f.write("not json\n[1, 2]\n{\"type\": \"assistant\", \"mess")
        self.assertEqual(review_queue.count("claude", path), (4, 3))
        self.assertEqual(review_queue.count("claude", self.tmp / "nope.jsonl"), (0, 0))


class Install(unittest.TestCase):
    def setUp(self):
        self.home = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        (self.home / ".claude").mkdir()
        (self.home / ".codex").mkdir()
        self.settings, self.codex, self.cj = self.home / ".claude" / "settings.json", self.home / ".codex" / "config.toml", self.home / ".claude.json"
        self.cj.write_text('{"mcpServers": {}}\n')
        self.settings.write_text('{\n  "theme": "dark",\n  "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "mine"}]}]}\n}\n')
        self.codex.write_text('# mine\nmodel = "gpt-6.2-sol"\n\n[profiles.x]\nmodel = "z"\n')
        self.out = []

    def do(self, fn):
        return fn(home=self.home, out=self.out.append)

    def test_install_and_uninstall_keep_everything_else(self):
        before = (self.settings.read_bytes(), self.codex.read_bytes())
        self.assertEqual(self.do(brain_install.install), 0)
        settings = json.loads(self.settings.read_text())
        self.assertEqual(settings["theme"], "dark")
        self.assertEqual(settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"], "mine")
        (entry,) = settings["hooks"]["Stop"]
        command = entry["hooks"][0]["command"]
        self.assertIn(str(Path(turn_end.__file__).resolve()), command)
        self.assertTrue(command.rstrip().endswith("claude"))
        text = self.codex.read_text()
        notify = next(line for line in text.splitlines() if line.startswith("notify"))
        self.assertIn(str(Path(turn_end.__file__).resolve()), notify)
        self.assertIn('"codex"', notify)
        self.assertLess(text.index("notify"), text.index("[profiles.x]"))        # a top-level key, before any table
        self.assertIn('model = "gpt-6.2-sol"', text)
        self.assertEqual(self.do(brain_install.status), 0)
        self.assertEqual(self.do(brain_install.install), 0)                        # twice is once
        self.assertEqual(len(json.loads(self.settings.read_text())["hooks"]["Stop"]), 1)
        self.assertEqual(self.codex.read_text().count("notify"), 1)
        self.assertEqual(self.do(brain_install.uninstall), 0)
        self.assertEqual((self.settings.read_bytes(), self.codex.read_bytes()), before)
        self.assertNotEqual(self.do(brain_install.status), 0)

    def test_an_owner_notify_is_never_replaced(self):
        self.codex.write_text('notify = ["my-notifier"]\nmodel = "gpt-6.2-sol"\n')
        before = self.codex.read_bytes()
        self.do(brain_install.install)
        self.assertIn(b'notify = ["my-notifier"]', self.codex.read_bytes())
        self.assertEqual(self.codex.read_text().count("notify ="), 1)
        self.assertTrue(any("notify" in str(line) for line in self.out), self.out)
        self.assertIn("Stop", json.loads(self.settings.read_text())["hooks"])      # the rest is still installed
        self.do(brain_install.uninstall)
        self.assertIn(b'notify = ["my-notifier"]', self.codex.read_bytes())

    def test_missing_settings_file_is_created_and_removed(self):
        self.settings.unlink()
        self.assertEqual(self.do(brain_install.install), 0)
        self.assertIn("Stop", json.loads(self.settings.read_text())["hooks"])
        self.assertEqual(stat.S_IMODE(self.settings.stat().st_mode), 0o600)
        self.do(brain_install.uninstall)
        self.assertFalse(self.settings.exists() and "turn_end" in self.settings.read_text())


if __name__ == "__main__":
    unittest.main()
