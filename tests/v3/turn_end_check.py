"""L1 race, resource-bound and config preservation regressions."""

from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Barrier
import time
import tomllib
import unittest
from unittest.mock import patch

from jarvis.v3 import brain_install, review_queue
from jarvis.v3.hooks import turn_end


FIXTURES = Path(__file__).parent / "fixtures" / "hooks"
CLAUDE = json.loads((FIXTURES / "claude_stop.json").read_bytes())
CODEX = json.loads((FIXTURES / "codex_turn_complete.json").read_bytes())
TOOL = b'{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","name":"Bash"}]}}\n'


class QueueChecks(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.home = self.root / "home"
        self.v3 = self.root / "v3"
        self.queue = self.v3 / "review-queue"
        self.state = self.v3 / "review-state"
        self.env = {"HOME": str(self.home), "JARVIS_V3_DIR": str(self.v3)}
        self.projects = self.home / ".claude/projects"
        self.projects.mkdir(parents=True)
        self.transcript = self.projects / "session.jsonl"
        self.transcript.write_bytes(TOOL * 10)
        self.payload = {**CLAUDE, "transcript_path": str(self.transcript)}

    def fire(self, changes=None):
        return turn_end.main(["turn_end.py", "claude"],
                             io.StringIO(json.dumps({**self.payload, **(changes or {})})),
                             self.env)

    def tick(self, **kwargs):
        kwargs.setdefault("claude_projects", self.projects)
        return review_queue.Consumer(self.queue, self.state, **kwargs).tick()

    def test_parallel_publication_and_cap(self):
        def run_two():
            barrier = Barrier(2)

            def worker(i):
                barrier.wait(timeout=5)
                return self.fire({"session_id": f"session-{i}"})

            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(list(pool.map(worker, range(2))), [0, 0])

        run_two()
        records = [json.loads(path.read_bytes()) for path in self.queue.iterdir()]
        self.assertEqual({item["session_id"] for item in records}, {"session-0", "session-1"})
        for i in range(turn_end.MAX_ITEMS - 3):
            (self.queue / f"{i:04d}.json").write_text("{}")
        run_two()
        self.assertEqual(len(list(self.queue.iterdir())), turn_end.MAX_ITEMS)

    def test_unicode_extra_fields_and_last_argv(self):
        cwd = str(self.root / "a project 🐙")
        self.fire({"cwd": cwd, "extra": {"private": "Ω should stay in the payload"}})
        payload = json.dumps({**CODEX, "cwd": cwd, "last-assistant-message": "私だけ"}, ensure_ascii=False)
        self.assertEqual(turn_end.main(["turn_end.py", "codex", "unused", payload], io.StringIO(), self.env), 0)
        for path in self.queue.iterdir():
            record = json.loads(path.read_bytes())
            self.assertEqual(record["cwd"], cwd)
            self.assertEqual(set(record), {"cwd", "provider", "session_id", "transcript", "ended_at"})

    def test_no_home_and_read_only_queue(self):
        stream = io.StringIO(json.dumps(self.payload))
        self.assertEqual(turn_end.main(["turn_end.py", "claude"], stream, {}), 0)
        self.assertEqual(stream.tell(), 0)
        self.assertFalse(self.v3.exists())
        self.queue.mkdir(parents=True)
        self.queue.chmod(0o500)
        try:
            self.assertEqual(self.fire(), 0)
            self.assertEqual(list(self.queue.iterdir()), [])
        finally:
            self.queue.chmod(0o700)

    def test_a_pipe_that_stays_open_does_not_block_or_print(self):
        env = {**self.env, "PATH": os.defpath}
        with subprocess.Popen([sys.executable, "-I", "-S", turn_end.__file__, "claude"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env) as hook:
            hook.stdin.write(b"{")
            hook.stdin.flush()
            self.assertEqual(hook.wait(timeout=3), 0)
            self.assertEqual((hook.stdout.read(), hook.stderr.read()), (b"", b""))
        self.assertFalse(self.queue.exists())

    def test_a_hook_publishing_during_a_count_is_left_for_the_next_tick(self):
        self.fire({"session_id": "first"})

        def count_and_notify(provider, transcript):
            self.fire({"session_id": "second"})
            return review_queue.count(provider, transcript)

        self.assertEqual([r["session_id"] for r in self.tick(count=count_and_notify)], ["first"])
        self.assertEqual(len(list(self.queue.iterdir())), 1)
        self.assertEqual(self.tick(), [])  # A second session ID names the same reviewed file.
        self.assertEqual(list(self.queue.iterdir()), [])

    def test_wrong_shaped_state_resets_the_review_baseline(self):
        self.fire()
        self.assertEqual([r["kind"] for r in self.tick()], ["skill"])
        (state,) = [path for path in self.state.iterdir() if not path.name.startswith(".")]
        for malformed in ([0, 0], {"skill": True, "memory": "9"}, {"tool_steps": 10}):
            state.write_text(json.dumps(malformed))
            self.fire()
            self.assertEqual([r["kind"] for r in self.tick()], ["skill"])

    def test_a_crash_after_removing_an_item_replays_its_review(self):
        self.fire()
        real_remove = review_queue._remove

        def remove_then_fail(directory, name):
            real_remove(directory, name)
            raise OSError("simulated stop after unlink")

        with patch.object(review_queue, "_remove", side_effect=remove_then_fail):
            with self.assertRaises(OSError):
                self.tick()
        self.assertEqual(list(self.queue.iterdir()), [])
        self.assertEqual([r["kind"] for r in self.tick()], ["skill"])
        self.assertEqual(self.tick(), [])

    def test_a_growth_during_count_is_excluded_from_the_snapshot(self):
        original = review_queue._batch

        def grow_then_read(*args):
            with self.transcript.open("ab") as stream:
                stream.write(TOOL)
            return original(*args)

        with patch.object(review_queue, "_batch", side_effect=grow_then_read):
            self.assertEqual(review_queue.count("claude", self.transcript), (10, 0))
        self.assertEqual(review_queue.count("claude", self.transcript), (11, 0))

    def test_huge_transcript_is_bounded_and_a_large_count_resumes(self):
        with self.transcript.open("ab") as stream:
            stream.truncate(4 << 30)
        started = time.monotonic()
        with self.assertRaisesRegex(ValueError, "byte budget"):
            review_queue.count("claude", self.transcript)
        self.assertLess(time.monotonic() - started, 2.0)

        self.transcript.write_bytes(TOOL * 20)
        self.fire()
        with patch.object(review_queue, "_SCAN_BYTES", len(TOOL) * 10):
            self.assertEqual(self.tick(), [])
            self.assertEqual(len(list(self.queue.iterdir())), 1)
            self.assertEqual([r["tool_steps"] for r in self.tick()], [20])
        self.assertEqual(list(self.queue.iterdir()), [])

    def test_protected_ancestors_and_links_are_skipped_before_count(self):
        hidden = self.home / ".SSH"
        hidden.mkdir(parents=True)
        secret = hidden / "rollout.jsonl"
        secret.write_bytes(TOOL * 10)
        env_dir = self.home / ".ENV.Production "
        env_dir.mkdir()
        (env_dir / self.transcript.name).write_bytes(TOOL * 10)
        alias = self.home / "link"
        alias.symlink_to(hidden)
        for i, path in enumerate((secret, alias / secret.name, env_dir / self.transcript.name)):
            self.fire({"transcript_path": str(path), "session_id": f"skip-{i}"})
        with patch.dict(os.environ, {"HOME": str(self.home)}):
            self.assertEqual(self.tick(claude_projects=self.home,
                                       count=lambda *_: self.fail("credential path was read")), [])
        self.assertEqual(list(self.queue.iterdir()), [])


class ConfigChecks(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.settings = self.home / ".claude" / "settings.json"
        self.settings.parent.mkdir()
        self.codex = self.home / ".codex" / "config.toml"
        self.codex.parent.mkdir()
        self.codex.write_bytes(b'owner = "example"\n')
        self.mcp = self.home / ".claude.json"
        self.mcp.write_bytes(b"{}\n")
        self.messages = []

    def action(self, func, result=0):
        self.assertEqual(func(home=self.home, out=self.messages.append), result, self.messages)

    def test_stop_install_preserves_bom_crlf_mode_and_owner_entries(self):
        own = b'{"matcher":"", "hooks":[{"type":"command", "command":"owner-command"}]}'
        before = b'\xef\xbb\xbf{\r\n  "hooks": {\r\n    "Stop": [' + own + b']\r\n  },\r\n  "theme": "dark"\r\n}\r\n'
        self.settings.write_bytes(before)
        self.settings.chmod(0o640)
        self.action(brain_install.install)
        installed = self.settings.read_bytes()
        self.assertTrue(installed.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\n", installed.replace(b"\r\n", b""))
        self.assertIn(own, installed)
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o640)
        data = json.loads(installed)
        self.assertEqual(data["hooks"]["Stop"][0], json.loads(own))
        args = shlex.split(data["hooks"]["Stop"][1]["hooks"][0]["command"])
        self.assertEqual(args[1:], ["-I", "-S", str(Path(turn_end.__file__).resolve()), "claude"])
        self.action(brain_install.uninstall)
        self.assertEqual(self.settings.read_bytes(), before)

    def test_uninstall_after_an_owner_edit_keeps_their_extra_stop_command(self):
        self.settings.write_text('{"hooks":{"Stop":[]}}')
        self.action(brain_install.install)
        installed = json.loads(self.settings.read_bytes())
        owner = {"type": "command", "command": "owner-command"}
        installed["hooks"]["Stop"][0]["hooks"].append(owner)
        installed["theme"] = "changed"
        self.settings.write_text(json.dumps(installed, indent=2) + "\n")
        self.action(brain_install.uninstall)
        self.assertEqual(json.loads(self.settings.read_bytes()), {"hooks": {"Stop": [{"hooks": [owner]}]}, "theme": "changed"})

    def test_invalid_or_linked_settings_refuse_all_edits(self):
        for raw in (b'{invalid json', b'{"hooks":{"Stop":true}}'):
            self.settings.write_bytes(raw)
            before = [p.read_bytes() for p in (self.settings, self.codex, self.mcp)]
            self.action(brain_install.install, 1)
            self.assertEqual([p.read_bytes() for p in (self.settings, self.codex, self.mcp)], before)
            self.assertEqual(list(self.home.rglob("*.bak-*")), [])
        self.settings.unlink()
        self.settings.symlink_to(self.mcp)
        self.action(brain_install.install, 1)
        self.assertTrue(self.settings.is_symlink())
        self.assertEqual(self.mcp.read_bytes(), b"{}\n")

    def test_notify_with_table_like_multiline_owner_text_and_later_edits(self):
        before = b'owner = """\n[not.a.table]\n"""\n\n[profiles.example]\nmodel = "first"\n'
        self.codex.write_bytes(before)
        self.action(brain_install.install)
        installed = self.codex.read_bytes()
        parsed = tomllib.loads(installed.decode())
        self.assertEqual(parsed["owner"], "[not.a.table]\n")
        self.assertEqual(parsed["notify"][1:], ["-I", "-S", str(Path(turn_end.__file__).resolve()), "codex"])
        self.assertIn(before, installed)
        changed = before.replace(b'"first"', b'"second"')
        self.codex.write_bytes(installed.replace(before, changed))
        self.action(brain_install.uninstall)
        self.assertEqual(self.codex.read_bytes(), changed)


if __name__ == "__main__":
    unittest.main()
