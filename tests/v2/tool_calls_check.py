"""TC-1 checks: safe labels, durable events and opt-in history; no live brains."""
from __future__ import annotations

import atexit
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

# Set HOME before config is imported, so the standalone check is hermetic too.
_HOME = tempfile.TemporaryDirectory(prefix="tc1-home-")
_HOME_ENV = patch.dict(os.environ, {"HOME": _HOME.name})
_HOME_ENV.start()
atexit.register(_HOME.cleanup)
atexit.register(_HOME_ENV.stop)

from jarvis.tools import secrets
from jarvis.v2 import tool_calls
from jarvis.v2.provider import Event, EventKind as K
import hud_backend_check as backend


_CWD = tempfile.TemporaryDirectory(prefix="tc1-cwd-")
atexit.register(_CWD.cleanup)


def setUpModule():
    # Every class, History's real backend included, runs with the process in a
    # temp folder and the scrub's checkout root there too, so no search ever
    # reaches the owner's checkout or home (delta review of PR #84).
    global _PREVIOUS, _ROOT
    _PREVIOUS = Path.cwd()
    os.chdir(_CWD.name)
    _ROOT = patch.object(secrets, "_JARVIS_ROOT", Path(_CWD.name))
    _ROOT.start()


def tearDownModule():
    _ROOT.stop()
    os.chdir(_PREVIOUS)


class Labels(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="tc1-labels-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        home = self.root / "home"
        home.mkdir()
        env = patch.dict(os.environ, {"HOME": str(home)})
        env.start()
        self.addCleanup(env.stop)
        # The process is elsewhere: the project cannot be found by the old
        # scrub's cwd search. Exercise the real search rather than patch it.
        process = self.root / "daemon"
        process.mkdir()
        project = self.root / "project"
        project.mkdir()
        previous = Path.cwd()
        os.chdir(process)
        self.addCleanup(os.chdir, previous)
        checkout = patch.object(secrets, "_JARVIS_ROOT", process)
        checkout.start()
        self.addCleanup(checkout.stop)
        self.project = project
        from jarvis import config
        for name in ("GOOGLE_TOKEN_PATH", "ONSHAPE_TOKEN_PATH", "DISCORD_TOKEN_PATH", "SPOTIFY_TOKEN_PATH"):
            guard = patch.object(config, name, self.root / (name + ".json"))
            guard.start()
            self.addCleanup(guard.stop)

    def test_label_table_and_never_read_fields(self):
        cwd = str(self.root)
        cases = [
            ("Bash", {"command": "pytest -q", "cmd": "ignored"}, "pytest -q"),
            ("shell", {"cmd": "pwd"}, "pwd"),
            ("Write", {"file_path": cwd + "/src/a.py", "content": "PRIVATE WRITE"}, "src/a.py"),
            ("Edit", {"file_path": cwd + "/a.py", "old_string": "PRIVATE OLD", "new_string": "PRIVATE NEW"}, "a.py"),
            ("NotebookEdit", {"notebook_path": cwd + "/work.ipynb"}, "work.ipynb"),
            ("Read", {"path": cwd + "/a/../b"}, "b"),
            ("Read", {"path": cwd + "-outside/a"}, cwd + "-outside/a"),
            ("apply_patch", {"paths": [cwd + "/a", "b"], "changes": [{"diff": "PRIVATE DIFF"}]}, "a, b"),
            ("Grep", {"pattern": "TODO"}, "TODO"),
            ("Grep", {"path": "src", "pattern": "TODO"}, "TODO in src"),
            ("Grep", {"path": cwd + "/src", "pattern": "TODO"}, "TODO in src"),
            ("fetch_page", {"url": "https://example.invalid"}, "https://example.invalid"),
            ("search", {"query": "a query"}, "a query"),
            ("Task", {"description": "check tests"}, "check tests"),
            ("terminal_read", {"terminal": "term-1"}, "term-1"),
            ("tool", {"name": "a name"}, "a name"),
            ("compact", {}, "compact"),
            ("Bash", {"command": "", "path": "ignored"}, ""),
            ("tool", None, "tool"),
            ("tool", {"command": "`a`\n\tb\u202e\u200bc"}, "a b c"),
            ("tool", {"command": "x" * 200}, "x" * 119 + "…"),
        ]
        for name, args, expected in cases:
            with self.subTest(name=name, args=args):
                self.assertEqual(tool_calls.label(name, args, cwd), expected)

        class NoPayload(dict):
            def __contains__(self, key):
                if key in forbidden:
                    raise AssertionError("read forbidden field " + key)
                return super().__contains__(key)

            def __getitem__(self, key):
                if key in forbidden:
                    raise AssertionError("read forbidden field " + key)
                return super().__getitem__(key)

            def get(self, key, *default):
                return self[key] if key in self else (default[0] if default else None)

        forbidden = ("content", "old_string", "new_string", "edits", "changes")
        for key in forbidden:
            self.assertEqual(tool_calls.label("fallback", NoPayload({key: "PRIVATE PAYLOAD"}), cwd), "fallback")

    def test_secret_scrub_precedes_clean_and_cap(self):
        token = "fake-secret-token-" + "z" * 160
        (self.project / ".env.production").write_text("API_KEY=" + token + "\n")
        self.assertNotIn(self.project, secrets._search_dirs())
        self.assertEqual(secrets.scrub("echo " + token), "echo " + token)
        value = tool_calls.label("Bash", {"command": "echo " + token}, str(self.project))
        self.assertEqual(value, "echo " + secrets.REDACTED)
        self.assertNotIn("fake-secret", value)

    def test_long_tokens_stay_linear(self):
        # Delta review of PR #84: the unbounded scheme regex took 1 s on a 64K
        # token (and held the GIL); a label judges only its head now.
        import time
        for size in (100_000, 1_000_000):
            command = "ab12" * (size // 4)
            start = time.perf_counter()
            tool_calls.label("Bash", {"command": command}, "")
            self.assertLess(time.perf_counter() - start, 0.5, size)

    def test_a_parent_folders_env_value_is_withheld(self):
        (self.project / ".env.local").write_text("API_KEY=fakeparentvalue123\n")
        sub = self.project / "sub"
        sub.mkdir()
        got = tool_calls.label("Bash", {"command": "echo fakeparentvalue123"}, str(sub))
        self.assertNotIn("fakeparentvalue123", got)

    def test_credential_shapes(self):
        token = "sk-" + "or-v1-" + "a1" * 25
        other = "gh" + "p_" + "z" * 36
        value = tool_calls.label("Bash", {"command": "curl -H 'Authorization: Bearer " + token + "' " + other}, str(self.project))
        self.assertEqual(value, "curl -H 'Authorization: Bearer [redacted]' [redacted]")

    def test_urls_drop_userinfo_query_and_fragment(self):
        urls = ("https://user:pass@host:8443/x?access_token=fake-query#fake-fragment",
                "https://host/x?access_token=fake-query", "https://user:pass@host/x",
                "https://host/x#fake-fragment", "https://user:pass@[::1]:8443/x?q=fake#frag")
        expected = ("https://host:8443/x", "https://host/x", "https://host/x",
                    "https://host/x", "https://[::1]:8443/x")
        for url, safe in zip(urls, expected):
            with self.subTest(url=url):
                self.assertEqual(tool_calls.label("fetch", {"url": url}, str(self.project)), safe)
                self.assertEqual(tool_calls.label("Bash", {"command": "curl '" + url + "'"}, str(self.project)), "curl '" + safe + "'")
        self.assertEqual(tool_calls.label("fetch", {"url": "/x?access_token=fake#frag"}, str(self.project)), "/x")

    def test_quoted_split_limit_is_documented(self):
        token = "fake-secret-token-value"
        (self.project / ".env.production").write_text("API_KEY=" + token + "\n")
        command = "echo fake-secret-'token-value'"
        self.assertEqual(tool_calls.label("Bash", {"command": command}, str(self.project)), command)
        checkout = Path(__file__).resolve().parents[2]
        for path in (checkout / "CLAUDE.md", checkout / "docs/hud-api.md"):
            self.assertTrue("A value split by shell quoting can still appear in a label" in path.read_text(),
                            str(path) + " must state the quoted-value limit")


class History(unittest.TestCase):
    # Reuse the real port-0 backend fixture without inheriting its whole suite.
    setUp = backend.Backend.setUp
    request = backend.Backend.request
    thread = backend.Backend.thread
    events_all = backend.Backend.events_all

    test_transcript_without_tools_golden_bytes = backend.Backend.test_transcript_without_tools_golden_bytes

    def append(self, thread, kind, data, turn="old", at="2026-10-12T12:00:00Z"):
        self.stores.threads._append(thread.id, "log.jsonl", dict(kind=kind, data=data, turn_id=turn, at=at))

    def history(self, thread):
        return self.request("GET", f"/threads/{thread.id}/transcript?tools=1")["messages"]

    def test_order_outcomes_and_restart(self):
        thread = self.thread()
        self.append(thread, "user", {"text": "go"})
        self.append(thread, "tool_started", {"call_id": "same", "name": "Write", "args": {"file_path": str(self.project_root / "a"), "content": "PRIVATE ARGS"}})
        self.append(thread, "user", {"text": "steer", "message_id": "s", "steer": True})
        self.append(thread, "tool_started", {"call_id": "compact", "name": "compact", "args": {}})
        self.append(thread, "tool_started", {"call_id": "old-running", "name": "old unfinished", "args": {}})
        self.append(thread, "user", {"text": "queued", "message_id": "q", "queued": True}, "new")
        self.append(thread, "queued_started", {"message_id": "q"}, "new")
        self.append(thread, "tool_started", {"call_id": "same", "name": "shell", "args": {}, "label": "saved"}, "new")
        self.append(thread, "tool_started", {"call_id": "running", "name": "Read", "args": {}}, "new")
        self.append(thread, "text", {"text": "reply"}, "new")
        self.append(thread, "tool_finished", {"call_id": "same", "ok": False, "outcome": "denied", "summary": "PRIVATE SUMMARY"})
        self.append(thread, "tool_finished", {"call_id": "compact", "ok": True})
        self.append(thread, "tool_finished", {"call_id": "same", "ok": False, "summary": "PRIVATE SUMMARY"}, "new")
        self.append(thread, "tool_finished", {"call_id": "orphan", "ok": True}, "new")
        session = self.daemon._sessions[thread.id]
        session.turn_id, session.worker = "new", SimpleNamespace()
        rows = self.history(thread)
        self.assertEqual([r["role"] for r in rows], ["user", "tool", "user", "tool", "tool", "user", "tool", "tool", "assistant"])
        self.assertEqual(rows[2]["mark"], "steering")
        self.assertNotIn("mark", rows[5])
        calls = [r for r in rows if r["role"] == "tool"]
        self.assertEqual([r["outcome"] for r in calls], ["denied", "ok", "stopped", "failed", "running"])
        self.assertEqual([r["label"] for r in calls], ["a", "compact", "old unfinished", "saved", "Read"])
        self.assertTrue(all("finished_at" in calls[i] for i in (0, 1, 3)))
        self.assertNotIn("finished_at", calls[2])
        self.assertNotIn("finished_at", calls[-1])
        import json
        wire = json.dumps(rows)
        for forbidden in ('"args"', '"summary"', "PRIVATE ARGS", "PRIVATE SUMMARY"):
            self.assertNotIn(forbidden, wire)
        session.worker = None
        self.assertEqual(self.history(thread)[-2]["outcome"], "stopped")
        self.daemon._sessions.pop(thread.id)
        restarted = self.history(thread)
        self.assertEqual(restarted[-2]["outcome"], "stopped")
        self.assertEqual(restarted[1]["label"], "a")
        self.request("GET", f"/threads/{thread.id}/transcript?tools=0", status=400)
        self.request("GET", f"/threads/{thread.id}/transcript?tools=1&tools=1", status=400)

    def test_old_labels_bounded_and_saved_labels_reused(self):
        thread = self.thread()
        for i in range(503):
            data = {"call_id": str(i), "name": "Read", "args": {"path": str(self.project_root / str(i))}}
            if i == 502:
                data["label"] = "persisted"
            self.append(thread, "tool_started", data)
        with patch.object(tool_calls, "label", wraps=tool_calls.label) as derive:
            rows = self.history(thread)
        self.assertEqual(derive.call_count, 499)
        self.assertEqual([r["label"] for r in rows[:4]], ["Read", "Read", "Read", "3"])
        self.assertEqual(rows[-1]["label"], "persisted")
        self.assertEqual(len(rows), 503)

    def test_names_are_cleaned(self):
        thread = self.thread()
        self.append(thread, "tool_started", {"call_id": "c", "name": "`Bash`\n\u202e" + "x" * 60, "label": "saved"})
        self.assertEqual(self.history(thread)[0]["name"], "Bash " + "x" * 34 + "…")

    def test_label_exception_falls_back_without_logging(self):
        thread = self.thread()
        session = self.daemon._sessions[thread.id]
        event = Event(K.TOOL_STARTED, thread.id, {"call_id": "c", "name": "`Bash`\n\u202e", "args": {}})
        with patch.object(tool_calls, "label", side_effect=RuntimeError("PRIVATE exception")), patch("logging.Logger._log") as log:
            self.daemon._record(session, event)
        log.assert_not_called()
        logged = self.stores.threads.read_log(thread.id)[-1]
        self.assertEqual(logged["data"]["label"], "Bash")
        self.assertEqual([r for r in self.events_all() if r["kind"] == "tool_started"], [logged])

    def test_record_labels_log_and_bus_once_outside_lock(self):
        thread = self.thread()
        session = self.daemon._sessions[thread.id]
        session.turn_id = "turn"
        entered, release, acquired = threading.Event(), threading.Event(), threading.Event()
        errors = []

        def blocking_scrub(text):
            entered.set()
            if not release.wait(3):
                raise AssertionError("scrub was not released")
            return text

        def record():
            try:
                self.daemon._record(session, event)
            except BaseException as exc:
                errors.append(exc)

        def acquire():
            with self.daemon._lock:
                acquired.set()

        event = Event(K.TOOL_STARTED, thread.id, {"call_id": "c", "name": "Write", "args": {"file_path": str(self.project_root / "a")}})
        with patch.object(secrets, "scrub", side_effect=blocking_scrub) as scrub:
            writer = threading.Thread(target=record)
            writer.start()
            try:
                self.assertTrue(entered.wait(2))
                reader = threading.Thread(target=acquire)
                reader.start()
                try:
                    self.assertTrue(acquired.wait(0.5), "label computation held daemon._lock")
                finally:
                    release.set()
                    reader.join(3)
            finally:
                release.set()
                writer.join(3)
            self.assertEqual(scrub.call_count, 1)
        self.assertEqual(errors, [])
        logged = self.stores.threads.read_log(thread.id)[-1]
        bus = [r for r in self.events_all() if r["kind"] == "tool_started"]
        self.assertEqual(bus, [logged])
        self.assertEqual(logged["data"]["label"], "a")
        self.assertNotIn("label", event.data)


if __name__ == "__main__":
    unittest.main()
