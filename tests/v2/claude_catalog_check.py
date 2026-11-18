"""Free Claude catalog checks: fake SDK only, disposable paths, no prompts."""
from __future__ import annotations

import asyncio
import copy
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jarvis import config, protected_state
from jarvis.tools import files
from jarvis.v2 import codex_catalog as X
from jarvis.v2 import claude_catalog as C, daemon as D, router as R, thread_model as TM
from jarvis.v2.model import ProviderName as P, Role, Thread
from jarvis.v2.provider import Brief, Decision
from jarvis.v2.providers import claude
from jarvis.v2.permissions import denied_file, protected_paths


ROWS = [
    {"value": "default", "resolvedModel": "claude-opus-5-5", "displayName": "Default (recommended)"},
    {"value": "opus", "resolvedModel": "claude-opus-5-5", "displayName": "Opus 5.5",
     "supportsEffort": True, "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"]},
    {"value": "fable", "resolvedModel": "claude-fable-5-2", "displayName": "Fable 5.2",
     "supportsEffort": True, "supportedEffortLevels": ["high", "max"]},
    {"value": "claude-haiku-4-5-20251001", "resolvedModel": "claude-haiku-4-5-20251001",
     "displayName": "Haiku 4.5", "supportsThinkingOff": True},
]


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="claude-catalog-check-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        env = patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        for name, value in {
            "CLAUDE_CATALOG_PATH": self.root / "claude-models.json",
            "CODEX_CATALOG_PATH": self.root / "codex-models.json",
            "ALLOWLIST_PATH": self.home / ".config" / "jarvis" / "allowlist.json",
            "MODELS_PATH": self.root / "models.json",
            "ROUTING_PATH": self.root / "routing.json",
            "PROVIDER_DEFAULTS_PATH": self.root / "provider_defaults.json",
            "DISCORD_GUILD_PATH": self.root / "guild.json",
            "V2_DATA_DIR": self.root / "stores",
        }.items():
            guard = patch.object(config, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        old = R.CLI_MODELS["claude"]
        self.addCleanup(lambda: R.CLI_MODELS.__setitem__("claude", old))
        R.CLI_MODELS["claude"] = copy.deepcopy(R.CLAUDE_FALLBACK)
        self.generation = R.claude_catalog_begin()
        self.addCleanup(lambda: R.claude_catalog_end(self.generation))
        listeners = patch.object(R, "_claude_catalog_listeners", [])
        listeners.start()
        self.addCleanup(listeners.stop)
        self.addCleanup(self.captured)

    def captured(self):
        """Drain the background capture before assertions or fixture teardown."""
        with R._CLAUDE_QUEUE_LOCK:
            self.assertTrue(R._CLAUDE_QUEUE_LOCK.wait_for(
                lambda: R._claude_pending is None and not R._claude_busy, 5),
                "session capture did not finish")

    def install(self, rows):
        table = C.parse(rows)
        R.offer_claude_rows(rows)
        self.captured()
        return table

    def client(self, info=None, error=None):
        class Client:
            connects = 0
            disconnects = 0
            queries = 0

            async def connect(self):
                self.connects += 1

            async def get_server_info(self):
                if error:
                    raise error
                return info

            async def disconnect(self):
                self.disconnects += 1

            async def query(self, *args, **kwargs):
                self.queries += 1
                raise AssertionError("catalog must never send a prompt")
        return Client()



class Parse(Fixture):
    def test_sdk_rows_default_skip_resolved_id_first_wins_and_no_alias(self):
        table = C.parse(ROWS + [{"value": "opus", "resolvedModel": "claude-opus-5-5", "displayName": "wrong"}])
        self.assertEqual(list(table), ["claude-opus-5-5", "claude-fable-5-2", "claude-haiku-4-5-20251001"])
        self.assertEqual(table["claude-opus-5-5"]["name"], "Claude Opus 5.5")
        self.assertEqual(table["claude-fable-5-2"]["efforts"], ("high", "max"))
        self.assertEqual(table["claude-haiku-4-5-20251001"]["efforts"], ())
        self.assertTrue(all(e["vision"] is True for e in table.values()))
        self.assertEqual(C.parse(C.rows_of(table)), table)

    def test_unnamed_models_roundtrip_without_gaining_a_prefix(self):
        table = C.parse([{"value": "claude-no-name"}])
        self.assertEqual(table["claude-no-name"]["name"], "claude-no-name")
        self.assertEqual(C.parse(C.rows_of(table)), table)
        self.assertTrue(C.save(table))
        self.assertEqual(C.load(), table)

    def test_id_fallback_only_for_nonstring_resolved(self):
        for resolved in (None, 12, [], {}):
            self.assertEqual(list(C.parse([{"resolvedModel": resolved, "value": "claude-test"}])), ["claude-test"])
        for resolved in ("", "not-claude", "bad id", "claude-bad/id"):
            with self.assertRaises(ValueError):
                C.parse([{"resolvedModel": resolved, "value": "claude-test"}])

    def test_slug_prefix_and_type_refusals(self):
        for model in (None, 12, {}, [], "opus", "gpt-6", "Claude-opus", "xclaude-opus", "/claude-opus",
                      "claude-sp ace", "claude-new\n", "claude-new/../evil", "claude-" + "x" * 58):
            with self.subTest(model=model), self.assertRaises(ValueError):
                C.parse([{"value": model}])
        self.assertIn("claude-a._-123", C.parse([{"value": "claude-a._-123"}]))
        self.assertIn("claude-" + "x" * 57, C.parse([{"value": "claude-" + "x" * 57}]))

    def test_names_cleaned_prefixed_capped_and_fallback(self):
        for display, expected in (("Fable\n5.2\x00", "Claude Fable 5.2"),
                                  ("Claude Fable 5.2", "Claude Fable 5.2"),
                                  (None, "claude-test"), (12, "claude-test"), ("\x00\n", "claude-test")):
            self.assertEqual(C.parse([{"value": "claude-test", "displayName": display}])["claude-test"]["name"], expected)
        for display in ("x" * 1000, "Claude " + "x" * 1000):
            name = C.parse([{"value": "claude-test", "displayName": display}])["claude-test"]["name"]
            self.assertEqual(len(name), C.MAX_NAME)
            self.assertTrue(name.startswith("Claude"))
        self.assertEqual((C.MAX_ROWS, C.MAX_MODELS, C.MAX_NAME, C.MAX_EFFORTS), (2000, 100, 64, 16))
        self.assertEqual(C.MAX_CACHE_BYTES, X.MAX_CACHE_BYTES)
        self.assertLessEqual(C.MAX_CACHE_BYTES, 4 * 1024 * 1024)

    def test_effort_requires_true_filters_words_and_preserves_order(self):
        row = {"value": "claude-test", "supportedEffortLevels": ["max", "high", "max", "ultra", {}, None, "low", "HIGH", "xhigh", "medium"]}
        for flag in (None, False, 1, "true", [], {}):
            self.assertEqual(C.parse([{**row, "supportsEffort": flag}])["claude-test"]["efforts"], ())
        self.assertEqual(C.parse([{**row, "supportsEffort": True}])["claude-test"]["efforts"],
                         ("max", "high", "low", "xhigh", "medium"))
        for levels in (None, "high", {"high": True}):
            self.assertEqual(C.parse([{**row, "supportsEffort": True, "supportedEffortLevels": levels}])["claude-test"]["efforts"], ())
        row.update(supportsEffort=True, supportedEffortLevels=["garbage"] * C.MAX_EFFORTS + ["high"])
        self.assertEqual(C.parse([row])["claude-test"]["efforts"], ())

    def test_empty_garbage_and_row_model_caps(self):
        for rows in (None, {}, "[]", (), [], [None, {}, 42, [], {"value": "default", "resolvedModel": "claude-test"}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                C.parse(rows)
        with self.assertRaises(ValueError):
            C.parse([None] * C.MAX_ROWS + [{"value": "claude-too-late"}])
        rows = [{"value": f"claude-model-{i}"} for i in range(C.MAX_MODELS + 10)]
        self.assertEqual(len(C.parse(rows)), C.MAX_MODELS)
        self.assertNotIn("claude-model-100", C.parse(rows))


class Cache(Fixture):
    def test_roundtrip_install_replace_unchanged_does_not_write(self):
        table = self.install(ROWS)
        self.assertEqual(R.CLI_MODELS["claude"], table)
        self.assertNotIn("claude-fable-5-1", table, "never union with fallback")
        path = config.CLAUDE_CATALOG_PATH
        body = path.read_bytes()
        payload = json.loads(body)
        self.assertEqual(payload["version"], C.CACHE_VERSION)
        self.assertIn("saved_at", payload)
        self.assertEqual(C.load(), table)
        with patch.object(C, "save") as save:
            self.assertEqual(self.install(ROWS), table)
            save.assert_not_called()
        self.assertEqual(path.read_bytes(), body)
        with self.assertRaises(ValueError):
            self.install([{"value": "not-claude"}])
        self.assertEqual(R.CLI_MODELS["claude"], table)
        self.assertEqual(path.read_bytes(), body)
        R.CLI_MODELS["claude"] = {}
        self.assertEqual(R.load_claude_catalog(), table)
        self.assertEqual(R.CLI_MODELS["claude"], table)
        table.clear()
        self.assertTrue(R.CLI_MODELS["claude"], "returned table is a copy")

    def test_reader_hardening_and_bad_cache_leave_table_unchanged(self):
        path = config.CLAUDE_CATALOG_PATH
        self.assertIsNone(C.load())
        before = copy.deepcopy(R.CLI_MODELS["claude"])
        for kind in ("symlink", "fifo", "oversize", "version", "json", "utf8", "empty", "directory"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    path.symlink_to("/dev/zero")
                elif kind == "fifo":
                    os.mkfifo(path)
                elif kind == "oversize":
                    with path.open("wb") as stream:
                        stream.truncate(C.MAX_CACHE_BYTES + 1)
                elif kind == "directory":
                    path.mkdir()
                else:
                    path.write_bytes({"version": json.dumps({"version": 99, "models": ROWS}).encode(),
                                      "json": b"{", "utf8": b"\xff", "empty": b'{"version": 1, "models": []}'}[kind])
                self.assertIsNone(R.load_claude_catalog())
                self.assertEqual(R.CLI_MODELS["claude"], before)
                if kind == "directory":
                    path.rmdir()
                else:
                    path.unlink()

    def test_startup_never_raises_or_starts_a_provider(self):
        self.assertFalse(D.load_claude_catalog_at_start())
        C.save(C.parse(ROWS))
        self.assertTrue(D.load_claude_catalog_at_start())
        for error in (MemoryError(), RuntimeError(), OSError()):
            with patch.object(R, "load_claude_catalog", side_effect=error):
                self.assertFalse(D.load_claude_catalog_at_start())

    def test_failed_save_keeps_installed_table(self):
        with patch.object(C, "_write_bytes", side_effect=OSError("private")):
            self.assertEqual(self.install(ROWS), C.parse(ROWS))
        self.assertFalse(config.CLAUDE_CATALOG_PATH.exists())

    def test_saved_choices_degrade_and_return_without_rewrite(self):
        TM.set_provider_default("claude", "claude-fable-5-2", "max")
        saved = config.PROVIDER_DEFAULTS_PATH.read_bytes()
        self.install([ROWS[1]])
        self.assertNotEqual(TM.default_choice(P.CLAUDE)[0], "claude-fable-5-2")
        self.assertEqual(config.PROVIDER_DEFAULTS_PATH.read_bytes(), saved)
        self.install(ROWS)
        self.assertEqual(TM.default_choice(P.CLAUDE), ("claude-fable-5-2", "max"))
        self.assertEqual(config.PROVIDER_DEFAULTS_PATH.read_bytes(), saved)


class SDK(Fixture):
    def test_session_connect_capture_and_reconnect(self):
        provider = claude.ClaudeProvider()
        clients = []

        def factory(options):
            client = self.client({"models": ROWS})
            clients.append(client)
            return client
        thread = Thread("catalog-thread", "p", Role.CHAT, P.CLAUDE)
        with patch.object(claude, "resolve_cli", return_value="/fake/claude"), patch.object(claude, "_client_factory", side_effect=factory):
            handle = provider.start(thread, Brief(Role.CHAT, str(self.root)), lambda *_: Decision.DENY)
            try:
                self.captured()
                self.assertEqual(R.CLI_MODELS["claude"], C.parse(ROWS))
                R.CLI_MODELS["claude"] = {}
                provider.set_model(handle, "claude-fable-5-2", "high")
                self.captured()
                self.assertEqual(R.CLI_MODELS["claude"], C.parse(ROWS))
            finally:
                provider.close(handle)
        self.assertEqual(len(clients), 2)
        self.assertTrue(all(c.connects == 1 and c.disconnects == 1 and c.queries == 0 for c in clients))

    def test_session_bad_info_does_not_fail_or_change_table(self):
        provider = claude.ClaudeProvider()
        before = copy.deepcopy(R.CLI_MODELS["claude"])
        for info, error in (({}, None), (None, None), ({"models": {}}, None),
                            ({"models": [None, {}, {"value": "alien"}]}, None),
                            (None, RuntimeError("do not log this"))):
            with self.subTest(info=info, error=type(error).__name__):
                client = self.client(info, error)
                with patch.object(claude, "resolve_cli", return_value="/fake/claude"), patch.object(claude, "_client_factory", return_value=client):
                    handle = provider.start(Thread("t", "p", Role.CHAT, P.CLAUDE),
                                            Brief(Role.CHAT, str(self.root)), lambda *_: Decision.DENY)
                    provider.close(handle)
                self.captured()
                self.assertEqual(R.CLI_MODELS["claude"], before)
                self.assertFalse(config.CLAUDE_CATALOG_PATH.exists())

    def test_session_catalog_listener_changes_only_and_contains_errors(self):
        changes = []

        def bad():
            raise RuntimeError("private")
        self.addCleanup(R.add_claude_catalog_listener(bad))
        self.addCleanup(R.add_claude_catalog_listener(lambda: changes.append(1)))
        for _ in range(2):
            asyncio.run(claude._connect_session(self.client({"models": ROWS})))
            self.captured()
        self.assertEqual(changes, [1])


class Worker(Fixture):
    def test_blocked_save_offers_connects_validation_and_lock(self):
        entered, release = threading.Event(), threading.Event()
        saves, workers = [], []
        def save(table):
            saves.append(table)
            workers.append(threading.current_thread())
            entered.set()
            release.wait(5)
        with patch.object(C, "save", side_effect=save):
            try:
                R.offer_claude_rows(ROWS)
                self.assertTrue(entered.wait(2))
                acquired = threading.Event()
                def take_lock():
                    with R._CONFIG_LOCK:
                        acquired.set()
                probe = threading.Thread(target=take_lock, daemon=True)
                probe.start()
                self.assertTrue(acquired.wait(0.5), "save must not hold config lock")
                start = time.monotonic()
                for i in range(20):
                    newest = [{"value": f"claude-new-{i}"}]
                    asyncio.run(claude._connect_session(self.client({"models": newest})))
                for bad in ([], [None, {}], None):
                    R.offer_claude_rows(bad)
                self.assertLess(time.monotonic() - start, 0.5)
                self.assertEqual(saves, [C.parse(ROWS)])
            finally:
                release.set()
                self.captured()
            probe.join(1)
        self.assertEqual(saves, [C.parse(ROWS), C.parse(newest)])
        self.assertEqual(R.CLI_MODELS["claude"], C.parse(newest))
        self.assertTrue(all(w.daemon and w is workers[0] for w in workers))

    def test_ended_generation_cannot_swap_or_revive(self):
        before = copy.deepcopy(R.CLI_MODELS["claude"])
        with patch.object(C, "save") as save:
            with R._CONFIG_LOCK:
                R.offer_claude_rows(ROWS)
                R.claude_catalog_end(self.generation)
                # A new daemon uses begin, never clears a shared stop event.
                self.generation = R.claude_catalog_begin()
            self.captured()
            self.assertEqual(R.CLI_MODELS["claude"], before)
            save.assert_not_called()
            R.offer_claude_rows(ROWS)
            self.captured()
            self.assertEqual(R.CLI_MODELS["claude"], C.parse(ROWS))
            save.assert_called_once()

    def test_generation_checked_again_before_save(self):
        lock = R._CONFIG_LOCK
        generation = self.generation
        class EndAfterSwap:
            def __enter__(self):
                lock.acquire()
            def __exit__(self, *args):
                R.claude_catalog_end(generation)
                lock.release()
        with patch.object(R, "_CONFIG_LOCK", EndAfterSwap()), patch.object(C, "save") as save:
            R.offer_claude_rows(ROWS)
            self.captured()
            self.assertEqual(R.CLI_MODELS["claude"], C.parse(ROWS))
            save.assert_not_called()

    def test_shutdown_returns_during_save_and_old_generation_stays_ended(self):
        entered, release = threading.Event(), threading.Event()
        def save(table):
            entered.set()
            release.wait(5)
        daemon = SimpleNamespace(_claude_catalog_generation=self.generation,
                                 _remove_claude_catalog_listener=None)
        with patch.object(C, "save", side_effect=save):
            try:
                R.offer_claude_rows(ROWS)
                self.assertTrue(entered.wait(2))
                R.offer_claude_rows([{"value": "claude-old-pending"}])
                start = time.monotonic()
                D.Daemon._end_claude_catalog(daemon)
                self.assertLess(time.monotonic() - start, 0.2)
                self.generation = R.claude_catalog_begin()
            finally:
                release.set()
                self.captured()
        self.assertEqual(R.CLI_MODELS["claude"], C.parse(ROWS))

    def test_notification_observes_swap_and_equal_has_no_event(self):
        from jarvis.v2.bus import EventBus
        bus = EventBus()
        events, observed = [], []
        # Use the same listener shape as the daemon, with an in-memory bus.
        bus.observe(events.append)
        def notify():
            observed.append(copy.deepcopy(R.CLI_MODELS["claude"]))
            bus.publish({"kind": "claude_metadata", "data": {"provider": "claude"}})
        self.addCleanup(R.add_claude_catalog_listener(notify))
        self.install(ROWS)
        self.install(ROWS)
        self.assertEqual(observed, [C.parse(ROWS)])
        self.assertEqual(events, [{"kind": "claude_metadata", "data": {"provider": "claude"}}])


    def test_a_connect_that_outlives_its_generation_is_dropped(self):
        """Round-3 review: the session reads the generation before connecting,
        so a slow connect finishing under a replacement daemon cannot install."""
        started, release = threading.Event(), threading.Event()
        old_rows = [{"value": "claude-old-session"}]

        class Slow:
            async def connect(self_inner):
                started.set()
                await asyncio.to_thread(release.wait, 5)

            async def get_server_info(self_inner):
                return {"models": old_rows}
        runner = threading.Thread(target=lambda: asyncio.run(claude._connect_session(Slow())), daemon=True)
        runner.start()
        self.assertTrue(started.wait(2))
        R.claude_catalog_end(self.generation)
        self.generation = R.claude_catalog_begin()
        R.offer_claude_rows(ROWS)
        self.captured()
        release.set()
        runner.join(5)
        self.captured()
        self.assertEqual(R.CLI_MODELS["claude"], C.parse(ROWS))

    def test_no_capture_without_a_generation(self):
        """An embedded or test daemon never begins one, so it never installs or saves."""
        R.claude_catalog_end(self.generation)
        before = copy.deepcopy(R.CLI_MODELS["claude"])
        with patch.object(C, "save") as save:
            asyncio.run(claude._connect_session(self.client({"models": ROWS})))
            R.offer_claude_rows(ROWS)
            self.captured()
        save.assert_not_called()
        self.assertEqual(R.CLI_MODELS["claude"], before)

    def test_an_older_arrival_never_replaces_a_newer_install(self):
        """Round-3 review: order is by arrival, not by parse completion."""
        in_parse, release = threading.Event(), threading.Event()
        old_rows, new_rows = [{"value": "claude-older"}], [{"value": "claude-newer"}]
        real_parse = C.parse

        def parse(rows):
            if rows is old_rows:
                in_parse.set()
                release.wait(5)
            return real_parse(rows)
        with patch.object(C, "parse", side_effect=parse), patch.object(C, "save"):
            older = threading.Thread(target=R.offer_claude_rows, args=(old_rows,), daemon=True)
            older.start()
            self.assertTrue(in_parse.wait(2))
            R.offer_claude_rows(new_rows)
            self.captured()
            release.set()
            older.join(5)
            self.captured()
        self.assertEqual(R.CLI_MODELS["claude"], real_parse(new_rows))

    def test_a_dead_worker_is_replaced_and_a_failed_save_still_notifies(self):
        events = []
        def boom():
            raise SystemExit("listener killed the worker")
        remove = R.add_claude_catalog_listener(boom)
        with redirect_stderr(io.StringIO()):
            R.offer_claude_rows(ROWS)
            deadline = time.monotonic() + 5
            while R._claude_capture_worker is not None and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertIsNone(R._claude_capture_worker, "a dead worker must clear its slot")
        remove()
        self.addCleanup(R.add_claude_catalog_listener(lambda: events.append(1)))
        with patch.object(C, "save", side_effect=OSError("disk")):
            R.offer_claude_rows([{"value": "claude-after-death"}])
            self.captured()
        self.assertEqual(R.CLI_MODELS["claude"], C.parse([{"value": "claude-after-death"}]))
        self.assertEqual(events, [1], "the HUD hears of a swap even when the save fails")


class Protection(Fixture):
    def test_protected_path_parity_direct_symlink_and_shell(self):
        path = config.CLAUDE_CATALOG_PATH
        self.assertIn(path, protected_paths())
        self.assertEqual(protected_paths(), files._protected_state())
        self.assertEqual(protected_paths(), protected_state.protected_paths())
        path.write_text("{}")
        alias = self.root / "alias.json"
        alias.symlink_to(path)
        for target in (path, alias, path.parent / "sub" / ".." / path.name):
            self.assertIsNotNone(denied_file(str(target)))
        for command in (f"echo '{{}}' > {path}", f"ln -sf /dev/zero {path}",
                        f"cp /tmp/x {path}", f"tee {alias}"):
            touch = protected_state.command_touches(command)
            self.assertTrue(touch, command)
        # A changed configured path must invalidate the protected-set cache.
        other = self.root / "elsewhere" / "models.json"
        with patch.object(config, "CLAUDE_CATALOG_PATH", other):
            self.assertIn(other, files._protected_state())
            self.assertIsNotNone(denied_file(str(other)))
        self.assertNotIn(other, files._protected_state())


if __name__ == "__main__":
    unittest.main()
