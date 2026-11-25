"""Exercise W1a with private temporary state and fake T3 clients."""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from jarvis import config
from jarvis.v3 import cli, t3_client, work
from jarvis.v3.work import Ledger, Project, Registry, Thread, WorkError, WorkService


class Work(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.home = root / "home"
        self.home.mkdir()
        self.v3 = root / "state"
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home), "JARVIS_V3_DIR": str(self.v3)}))
        self.enterContext(patch.object(config, "V3_DIR", self.v3))
        self.folder = root / "project"
        self.folder.mkdir()
        self.other = root / "other-project"
        self.other.mkdir()
        self.registry_path = self.v3 / "work" / "projects.json"
        self.ledger_path = self.v3 / "work" / "threads.jsonl"
        self.registry = Registry(self.registry_path)
        self.ledger = Ledger(self.ledger_path)
        self.t3 = Mock(spec=t3_client.T3)
        self.t3.start.return_value = t3_client.Run("thread-1", "run-1", "running")
        self.token = Mock(return_value="a1b2")
        self.service = WorkService(t3=self.t3, clock=lambda: 12.5, token=self.token)

    def add(self, name="demo", path=None, **fields):
        return self.registry.add(name, self.folder if path is None else path,
                                 **{"provider": "codex", "model": "demo-model", "base": "main", **fields})

    def record(self, thread_id, project, at):
        self.ledger.record_start(thread_id=thread_id, run_id="run-1", project=project,
                                 title="Test work", branch=None, brief_sha256="a" * 64, at=at)

    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (patch.object(t3_client, "T3", return_value=self.t3),
              contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr)):
            code = cli.main(["work", *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_registry_round_trip_and_modes(self):
        self.assertEqual(self.registry.list(), [])
        self.assertFalse(self.registry_path.parent.exists())
        z = self.add("z")
        alias = self.home / "project-link"
        alias.symlink_to(self.other)
        a = self.add("a", alias, base=None)
        self.assertEqual(a, Project("a", str(self.other), "codex", "demo-model", None))
        reopened = Registry(self.registry_path)
        self.assertEqual(reopened.list(), [a, z])
        self.assertEqual(reopened.get("a"), a)
        self.assertEqual(stat.S_IMODE(self.registry_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.registry_path.parent.stat().st_mode), 0o700)
        reopened.remove("a")
        self.assertEqual(self.registry.list(), [z])
        with self.assertRaises(WorkError):
            self.registry.get("a")

    def test_registry_mid_write_failure_preserves_old_file(self):
        project = self.add()
        old = self.registry_path.read_bytes()
        old_inode = self.registry_path.stat().st_ino
        fdopen = os.fdopen

        @contextlib.contextmanager
        def fail_midway(fd, mode):
            with fdopen(fd, mode) as stream:
                if mode != "wb":
                    yield stream
                    return
                write = stream.write

                def partial_write(data):
                    write(data[:len(data) // 2])
                    stream.flush()
                    self.assertEqual(self.registry_path.read_bytes(), old)
                    raise OSError("fake disk failure")

                with patch.object(stream, "write", side_effect=partial_write):
                    yield stream

        with patch.object(work.os, "fdopen", side_effect=fail_midway), self.assertRaises(WorkError):
            self.add("other")
        self.assertEqual(self.registry_path.read_bytes(), old)
        self.assertEqual(self.registry_path.stat().st_ino, old_inode)
        self.assertEqual(self.registry.list(), [project])
        self.assertEqual(list(self.registry_path.parent.iterdir()), [self.registry_path])
        self.add("other")
        self.assertNotEqual(self.registry_path.stat().st_ino, old_inode)

    def test_refused_project_paths(self):
        regular = self.home / "regular"
        regular.touch()
        paths = [Path("relative"), self.home / "missing", regular, Path("/"), self.home]
        for root in (self.v3, *(self.home / part for part in (
            ".config/jarvis", ".ssh", ".gnupg", ".claude", ".codex", ".t3",
        ))):
            child = root / "private"
            child.mkdir(parents=True)
            paths.extend((root, child))
        for index, target in enumerate(paths[3:]):
            link = self.home / f"link-{index}"
            link.symlink_to(target)
            paths.append(link)
        for path in paths:
            with self.subTest(path=path), self.assertRaises(WorkError):
                self.add(path=path)
        self.assertFalse(self.registry_path.exists())

    def test_refuses_path_that_moves_between_resolves(self):
        with patch.object(Path, "resolve", side_effect=(self.folder, self.other)), self.assertRaises(WorkError):
            self.add()

    def test_refused_project_fields(self):
        for value in ("", "-bad", "bad\x01name", "a" * 33, "Upper", "under_score"):
            with self.subTest(field="name", value=value), self.assertRaises(WorkError):
                self.add(name=value)
        for field in ("provider", "model", "base"):
            for value in ("", " \t ", "-bad", "bad\x01name", "a" * 257, "é" * 129):
                with self.subTest(field=field, value=value), self.assertRaises(WorkError):
                    self.add(**{field: value})
        self.assertFalse(self.registry_path.exists())

    def test_project_limit(self):
        for index in range(32):
            self.add(f"project-{index}")
        with self.assertRaises(WorkError):
            self.add("project-32")
        self.assertEqual(len(self.registry.list()), 32)

    def test_bad_registry_fails_closed_at_every_entry_point(self):
        self.add()
        plan = self.service.plan_start("demo", "Fix example", "Example brief")
        good = self.registry_path.read_bytes()
        target = self.home / "registry-target"
        target.write_bytes(good)
        target.chmod(0o600)
        fstat = os.fstat

        def foreign_stat(fd):
            info = fstat(fd)
            if os.path.samestat(info, self.registry_path.stat()):
                return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1, st_nlink=info.st_nlink)
            return info

        for state in ("malformed-json", "malformed-entry", "duplicate-key", "oversized", "symlinked", "foreign"):
            with self.subTest(state=state):
                self.registry_path.unlink()
                file = {
                    "malformed-json": b"{",
                    "malformed-entry": good.replace(b'"model": "demo-model"', b'"model": []'),
                    "duplicate-key": good.replace(b'"v": 1', b'"v": 1, "v": 1'),
                    "oversized": good + b" " * (256 * 1024),
                    "foreign": good,
                }
                if state == "symlinked":
                    self.registry_path.symlink_to(target)
                else:
                    self.registry_path.write_bytes(file[state])
                    self.registry_path.chmod(0o600)
                stat_patch = (patch.object(work.os, "fstat", side_effect=foreign_stat)
                              if state == "foreign" else contextlib.nullcontext())
                with stat_patch:
                    for method, args in (
                        (self.registry.list, ()),
                        (self.registry.get, ("demo",)),
                        (self.service.plan_start, ("demo", "Fix example", "Example brief")),
                        (self.service.start, (plan,)),
                    ):
                        with self.subTest(method=method.__name__), self.assertRaises(WorkError):
                            method(*args)
        self.assertEqual(self.t3.mock_calls, [])
        self.assertFalse(self.ledger_path.exists())

    def test_plan_validates_without_calling_t3_or_creating_ledger(self):
        self.add()
        with self.assertRaises(WorkError):
            self.service.plan_start("unknown", "Example", "Example brief")
        for title in ("", "a" * 81, "a\nb", "abc\x1f", "abc\u202e", "-test", "🧪" * 65):
            with self.subTest(title=title), self.assertRaises(WorkError):
                self.service.plan_start("demo", title, "Example brief")
        for brief in ("", " \t\n ", "a" * 16_001, "bad\x00brief", "\ud800"):
            with self.subTest(length=len(brief)), self.assertRaises(WorkError):
                self.service.plan_start("demo", "Example", brief)
        for title, brief in (("  Fix example  ", "  line one\nline two  "), ("🧪" * 64, "é" * 16_000),
                             ("Example café", "\tExample brief")):
            plan = self.service.plan_start("demo", title, brief)
            self.assertEqual(t3_client._arg(plan["title"]), title.strip())
            self.assertEqual(t3_client._message(plan["brief"]), brief.encode("utf-8"))
        self.assertEqual(self.t3.mock_calls, [])
        self.assertFalse(self.ledger_path.exists())

    def test_plan_branch(self):
        self.add()
        self.add("direct", base=None)
        cases = (
            ("demo", "  Fix / EXAMPLE!!  ", True, "jarvis/fix-example-a1b2"),
            ("demo", "🧪!?", True, "jarvis/work-a1b2"),
            ("demo", "X" * 80, True, f"jarvis/{'x' * 40}-a1b2"),
            ("demo", "Fix example", False, None),
            ("direct", "Fix example", True, None),
        )
        for project, title, worktree, branch in cases:
            with self.subTest(title=title, project=project, worktree=worktree):
                self.assertEqual(self.service.plan_start(project, title, "Brief", worktree=worktree)["branch"], branch)
        self.assertEqual(self.token.call_args_list, [call(2)] * 3)

    def test_start_calls_t3_once_and_records_only_the_brief_digest(self):
        self.add()
        brief = "fake-private-brief\nwith Unicode 🧪"
        digest = hashlib.sha256(brief.encode("utf-8")).hexdigest()
        for branch in ("jarvis/fix-example-a1b2", None):
            with self.subTest(branch=branch):
                self.t3.reset_mock()
                self.ledger_path.unlink(missing_ok=True)
                plan = self.service.plan_start("demo", "Fix example", brief, worktree=branch is not None)
                thread = self.service.start(plan)
                options = {} if branch is None else {"worktree": "main", "branch": branch}
                self.assertEqual(self.t3.mock_calls, [
                    call.start(str(self.folder), brief, provider="codex", model="demo-model",
                               title="Fix example", **options),
                ])
                self.assertEqual(thread, Thread("thread-1", "demo", "Fix example", 12.5, branch,
                                                digest, "started", False, 12.5))
                self.assertEqual(self.ledger.threads(), [thread])
                self.assertEqual(stat.S_IMODE(self.ledger_path.stat().st_mode), 0o600)
                raw = self.ledger_path.read_text()
                self.assertNotIn("fake-private-brief", raw)
                self.assertEqual([json.loads(line) for line in raw.splitlines()], [
                    dict(v=1, kind="start", thread_id="thread-1", run_id="run-1", project="demo",
                         title="Fix example", branch=branch, brief_sha256=digest, at=12.5),
                ])

    def test_start_refuses_stale_registry_and_changed_plan_shape(self):
        self.add()
        plan = self.service.plan_start("demo", "Fix example", "Example brief")
        for bad in ({**plan, "extra": 1}, {key: value for key, value in plan.items() if key != "base"},
                    {**plan, "branch": "other/fix-example-a1b2"}, {**plan, "brief": "   "}):
            with self.subTest(keys=bad.keys()), self.assertRaises(WorkError):
                self.service.start(bad)
        self.registry.remove("demo")
        with self.assertRaises(WorkError):
            self.service.start(plan)
        for field, value in (("path", self.other), ("provider", "claude"), ("model", "other"), ("base", "trunk")):
            self.add(**{field: value})
            try:
                with self.subTest(field=field), self.assertRaises(WorkError):
                    self.service.start(plan)
            finally:
                self.registry.remove("demo")
        self.assertEqual(self.t3.mock_calls, [])
        self.assertFalse(self.ledger_path.exists())

    def test_t3_failure_leaves_no_start_line(self):
        self.add()
        error = t3_client.T3Error("failed")
        self.t3.start.side_effect = error
        plan = self.service.plan_start("demo", "Fix example", "Example brief")
        with self.assertRaises(WorkError) as caught:
            self.service.start(plan)
        self.assertEqual(str(caught.exception), str(error))
        self.assertFalse(self.ledger_path.exists())

    def test_ledger_failure_after_t3_start_names_thread(self):
        self.add()
        plan = self.service.plan_start("demo", "Fix example", "Example brief")
        with patch.object(work.os, "write", side_effect=OSError("fake disk full")):
            with self.assertRaisesRegex(WorkError, "thread-1"):
                self.service.start(plan)
        self.t3.start.assert_called_once()
        self.assertEqual(self.ledger.threads(), [])

    def test_show_and_send_require_a_ledger_thread(self):
        self.record("thread-1", "demo", 1)
        state = t3_client.State("thread-1", "completed", False, 0, "Example", "codex", "demo-model", [])
        self.t3.show.return_value = state
        self.t3.send.return_value = t3_client.Run("thread-1", "run-2", "queued")
        for method, args in ((self.service.show, ()), (self.service.send, ("Example",))):
            with self.assertRaises(WorkError) as caught:
                method("real-but-unregistered-thread", *args)
            self.assertEqual(str(caught.exception), "That is not a thread Jarvis started.")
        for text in ("", "   ", "a" * 16_001, "bad\x00message"):
            with self.assertRaises(WorkError):
                self.service.send("thread-1", text)
        self.assertEqual(self.t3.mock_calls, [])
        self.assertIs(self.service.show("thread-1", messages=13), state)
        self.assertIs(self.service.send("thread-1", "  Example\nmessage  "), self.t3.send.return_value)
        self.assertEqual(self.t3.mock_calls, [
            call.show("thread-1", messages=13),
            call.send("thread-1", "  Example\nmessage  "),
        ])

    def test_ledger_folding_order_filter_and_bad_lines(self):
        with self.assertRaises(WorkError):
            self.ledger.record_status("unknown", status="waiting", waiting_on_owner=True, at=0)
        self.assertFalse(self.ledger_path.exists())
        self.record("older", "demo", 1)
        unknown = dict(v=1, kind="status", thread_id="unknown", status="waiting", waiting_on_owner=True, at=2)
        with self.ledger_path.open("ab") as stream:
            stream.write(json.dumps(unknown).encode() + b"\n")
            stream.write(b'{"v": 1, malformed')
        self.record("newer", "other", 3)
        self.ledger.record_status("older", status="waiting", waiting_on_owner=True, at=4)
        newer, older = self.service.threads()
        self.assertEqual((newer.thread_id, older.thread_id), ("newer", "older"))
        self.assertEqual((newer.status, newer.waiting_on_owner, newer.updated_at), ("started", False, 3))
        self.assertEqual((older.status, older.waiting_on_owner, older.updated_at), ("waiting", True, 4))
        self.ledger.record_status("older", status="completed", waiting_on_owner=False, at=5)
        [thread] = self.service.threads("demo")
        self.assertEqual((thread.thread_id, thread.status, thread.waiting_on_owner, thread.updated_at),
                         ("older", "completed", False, 5))
        with self.assertRaises(WorkError):
            self.ledger.record_status("unknown", status="completed", waiting_on_owner=False, at=6)

    def test_work_errors_and_cli_output_never_include_private_text(self):
        self.add()
        self.record("thread-1", "demo", 1)
        marker = "fake-private-marker"
        binary = self.home / "fake-t3"
        binary.write_text(
            f"#!{sys.executable} -I\nimport sys\n"
            f"print({marker!r})\nprint({marker!r}, file=sys.stderr)\nraise SystemExit(1)\n",
            encoding="utf-8",
        )
        binary.chmod(0o700)
        service = WorkService(t3=t3_client.T3(binary))
        plan = service.plan_start("demo", "Example", marker)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            for method, args in ((service.start, (plan,)), (service.send, ("thread-1", marker)),
                                 (service.send, ("thread-1", marker + "\x00"))):
                with self.assertRaises(WorkError) as caught:
                    method(*args)
                self.assertNotIn(marker, str(caught.exception))
            with patch.dict(os.environ, {"JARVIS_T3_BIN": str(binary)}):
                self.assertEqual(cli.main(["work", "add", "other", str(self.other),
                                           "--provider", "codex", "--model", "demo-model"]), 0)
            self.assertEqual(cli.main(["work", "list"]), 0)
        self.assertNotIn(marker, stdout.getvalue() + stderr.getvalue())
        self.assertNotIn(marker, self.ledger_path.read_text())

    def test_work_import_does_not_load_model_stack(self):
        script = self.home / "import_work.py"
        script.write_text(
            "import sys\n"
            "import jarvis.v3.work\n"
            "blocked = {'jarvis.llm', 'jarvis.agent', 'openai', 'anthropic'}\n"
            "assert not blocked.intersection(sys.modules), blocked.intersection(sys.modules)\n",
            encoding="utf-8",
        )
        result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cli_add_list_and_no_t3(self):
        flags = ("--provider", "codex", "--model", "demo-model")
        self.assertEqual(self.run_cli("add", "demo", str(self.folder), *flags, "--base", "main")[0], 0)
        self.assertEqual(self.t3.mock_calls, [call.project_add(str(self.folder), title="demo")])
        self.record("thread-1", "demo", 1)
        code, output, error = self.run_cli("list")
        self.assertEqual((code, error), (0, ""))
        for value in ("demo", str(self.folder), "codex", "demo-model", "main", "Threads: 1"):
            self.assertIn(value, output)
        self.t3.reset_mock()
        self.assertEqual(self.run_cli("add", "other", str(self.other), *flags, "--no-t3")[0], 0)
        self.assertEqual(self.t3.mock_calls, [])
        self.assertIn("other", self.run_cli("list")[1])

    def test_cli_t3_failure_warns_and_keeps_registration(self):
        failure = t3_client.T3Error("failed")
        self.t3.project_add.side_effect = failure
        code, _, error = self.run_cli("add", "demo", str(self.folder), "--provider", "codex", "--model", "demo-model")
        self.assertEqual(code, 0)
        self.assertIn(f"Warning: {failure}", error)
        self.assertEqual(self.registry.get("demo").path, str(self.folder))

    def test_cli_reports_registry_errors_and_removes_known_project(self):
        with self.assertRaises(WorkError) as caught:
            self.add(path="relative")
        code, _, error = self.run_cli("add", "demo", "relative", "--provider", "codex", "--model", "demo-model")
        self.assertEqual(code, 1)
        self.assertIn(str(caught.exception), error)
        self.assertEqual(self.run_cli("remove", "unknown")[0], 1)
        self.add()
        self.record("thread-1", "demo", 1)
        self.assertEqual(self.run_cli("remove", "demo")[0], 0)
        self.assertEqual(self.registry.list(), [])
        self.assertEqual(self.ledger.get("thread-1").project, "demo")
        self.assertEqual(self.t3.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
