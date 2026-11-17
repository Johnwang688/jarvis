"""Free Claude catalog checks: fake SDK only, disposable paths, no prompts."""
from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jarvis import config, protected_state
from jarvis.tools import files
from jarvis.v2 import claude_catalog as C, daemon as D, hud_api as H, router as R, thread_model as TM
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
        R.CLAUDE_CATALOG_STOPPED.clear()
        listeners = patch.object(R, "_claude_catalog_listeners", [])
        listeners.start()
        self.addCleanup(listeners.stop)
        self.addCleanup(self.captured)

    def captured(self):
        """Drain the background capture before assertions or fixture teardown."""
        with R._CLAUDE_QUEUE_LOCK:
            worker = R._claude_capture_worker
        if worker is not None:
            worker.join(5)
            self.assertFalse(worker.is_alive(), "session capture did not finish")

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

    def ledger(self):
        ledger = H.HUDLedger(self.root / "ledger-stores")
        self.addCleanup(lambda: ledger.stop_claude(5))
        return ledger


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
        self.assertEqual((C.MAX_ROWS, C.MAX_MODELS, C.MAX_NAME, C.MAX_EFFORTS, C.MAX_CACHE_BYTES),
                         (2000, 100, 64, 16, 512 * 1024))

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
        table = R.set_claude_models(ROWS)
        self.assertEqual(R.CLI_MODELS["claude"], table)
        self.assertNotIn("claude-fable-5-1", table, "never union with fallback")
        path = config.CLAUDE_CATALOG_PATH
        body = path.read_bytes()
        payload = json.loads(body)
        self.assertEqual(payload["version"], C.CACHE_VERSION)
        self.assertIn("saved_at", payload)
        self.assertEqual(C.load(), table)
        with patch.object(C, "save") as save:
            self.assertEqual(R.set_claude_models(ROWS), table)
            save.assert_not_called()
        self.assertEqual(path.read_bytes(), body)
        with self.assertRaises(ValueError):
            R.set_claude_models([{"value": "not-claude"}])
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
            self.assertEqual(R.set_claude_models(ROWS), C.parse(ROWS))
        self.assertFalse(config.CLAUDE_CATALOG_PATH.exists())

    def test_saved_choices_degrade_and_return_without_rewrite(self):
        TM.set_provider_default("claude", "claude-fable-5-2", "max")
        saved = config.PROVIDER_DEFAULTS_PATH.read_bytes()
        R.set_claude_models([ROWS[1]])
        self.assertNotEqual(TM.default_choice(P.CLAUDE)[0], "claude-fable-5-2")
        self.assertEqual(config.PROVIDER_DEFAULTS_PATH.read_bytes(), saved)
        R.set_claude_models(ROWS)
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

    def test_metadata_options_private_cwd_cleanup_environment_and_no_prompt(self):
        client = self.client({"models": ROWS})
        seen = []

        def factory(options):
            seen.append(options)
            cwd = Path(options.cwd)
            self.assertTrue(cwd.is_dir())
            self.assertEqual(stat.S_IMODE(cwd.stat().st_mode), 0o700)
            self.assertEqual(list(cwd.iterdir()), [])
            self.assertEqual(options.tools, [])
            self.assertEqual(options.setting_sources, [])
            self.assertEqual(options.mcp_servers, {})
            self.assertTrue(options.strict_mcp_config)
            self.assertFalse(options.hooks)
            self.assertIsNone(options.can_use_tool)
            self.assertEqual(options.allowed_tools, [])
            self.assertEqual(options.cli_path, "/fake/claude")
            self.assertEqual(options.env, {})  # ordinary sessions inherit the environment too
            self.assertIsNone(options.model)
            return client
        with patch.object(claude, "resolve_cli", return_value="/fake/claude"), patch.object(claude, "_client_factory", side_effect=factory):
            self.assertEqual(claude.ClaudeProvider().model_catalog(), ROWS)
        self.assertFalse(Path(seen[0].cwd).exists())
        self.assertEqual((client.connects, client.disconnects, client.queries), (1, 1, 0))
        self.assertFalse(config.CLAUDE_CATALOG_PATH.exists(), "the worker's install gate owns the save")

    def test_metadata_error_disconnects_and_removes_cwd(self):
        client = self.client(error=RuntimeError("private"))
        seen = []
        with patch.object(claude, "resolve_cli", return_value=None), patch.object(claude, "_client_factory", side_effect=lambda o: (seen.append(o), client)[1]):
            self.assertIsNone(claude.ClaudeProvider().model_catalog())
        self.assertEqual(client.disconnects, 1)
        self.assertFalse(Path(seen[0].cwd).exists())

    def test_metadata_disconnect_is_awaited_after_success_and_stalled_connect(self):
        self.assertEqual(claude.CLAUDE_METADATA_CONNECT_READ_TIMEOUT_S, 28)
        self.assertGreaterEqual(claude.CLAUDE_METADATA_DISCONNECT_TIMEOUT_S, 25)
        for stalled in (False, True):
            with self.subTest(stalled=stalled):
                client = self.client({"models": ROWS})
                finished, cancelled, seen = [], [], []

                async def connect():
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.append("connect")

                async def disconnect():
                    client.disconnects += 1
                    self.assertTrue(Path(seen[0].cwd).is_dir())
                    try:
                        await asyncio.sleep(3)
                        self.assertTrue(Path(seen[0].cwd).is_dir())
                        finished.append(1)
                    except asyncio.CancelledError:
                        cancelled.append("disconnect")
                        raise
                if stalled:
                    client.connect = connect
                client.disconnect = disconnect
                with patch.object(claude, "CLAUDE_METADATA_CONNECT_READ_TIMEOUT_S", 0.03), patch.object(claude, "resolve_cli", return_value=None), patch.object(claude, "_client_factory", side_effect=lambda o: (seen.append(o), client)[1]):
                    started = time.monotonic()
                    result = claude.ClaudeProvider().model_catalog()
                    elapsed = time.monotonic() - started
                self.assertEqual(result, None if stalled else ROWS)
                self.assertEqual(finished, [1], "disconnect must complete, not be cancelled")
                self.assertEqual(cancelled, ["connect"] if stalled else [])
                self.assertGreaterEqual(elapsed, 3)
                self.assertLess(elapsed, 4, "the connect stall remains bounded")
                self.assertEqual((client.disconnects, client.queries), (1, 0))
                self.assertFalse(Path(seen[0].cwd).exists())

    def test_metadata_timeout_disconnects_without_query(self):
        client = self.client()
        cancelled = []

        async def slow():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(1)
        client.connect = slow
        with patch.object(claude, "CLAUDE_METADATA_CONNECT_READ_TIMEOUT_S", 0.02), patch.object(claude, "resolve_cli", return_value=None), patch.object(claude, "_client_factory", return_value=client):
            started = time.monotonic()
            self.assertIsNone(claude.ClaudeProvider().model_catalog())
            self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(cancelled, [1])
        self.assertEqual((client.disconnects, client.queries), (1, 0))

    def test_session_capture_never_waits_for_install_and_coalesces(self):
        entered, release = threading.Event(), threading.Event()
        calls, workers = [], []

        def install(rows):
            calls.append(rows)
            workers.append(threading.current_thread())
            entered.set()
            release.wait(1)
            raise OSError("private disk failure")

        with patch.object(R, "set_claude_models", side_effect=install):
            try:
                started = time.monotonic()
                asyncio.run(claude._connect_session(self.client({"models": ROWS})))
                self.assertLess(time.monotonic() - started, 0.3)
                self.assertTrue(entered.wait(1))
                for i in range(40):
                    newest = [{"value": f"claude-new-{i}"}]
                    asyncio.run(claude._connect_session(self.client({"models": newest})))
                self.assertEqual(calls, [ROWS], "storm must not start more installers")
            finally:
                release.set()
                self.captured()
        self.assertEqual(calls, [ROWS, newest])
        self.assertTrue(all(w.daemon and w is workers[0] for w in workers))

    def test_session_capture_blocked_config_lock_and_after_stop_save_nothing(self):
        ledger = self.ledger()
        before = copy.deepcopy(R.CLI_MODELS["claude"])
        with R._CONFIG_LOCK:
            started = time.monotonic()
            asyncio.run(claude._connect_session(self.client({"models": ROWS})))
            self.assertLess(time.monotonic() - started, 0.3)
            ledger.stop_claude(0)
        self.captured()
        asyncio.run(claude._connect_session(self.client({"models": ROWS})))
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


class Refresh(Fixture):
    def test_ttl_success_failure_and_unchanged_notification(self):
        self.assertEqual(H.CLAUDE_METADATA_TTL_S, 6 * 3600)
        for result in (ROWS, [], RuntimeError("private")):
            ledger = self.ledger()
            now, calls, changes = [1.0], [], []
            ledger.metadata_clock = lambda: now[0]

            def read():
                calls.append(1)
                if isinstance(result, Exception):
                    raise result
                return result
            provider = SimpleNamespace(model_catalog=read)
            ledger.listen_claude_catalog(lambda changes=changes: changes.append(1))
            self.assertTrue(ledger.refresh_claude(provider))
            self.assertTrue(ledger.wait_claude_refresh(5))
            self.assertFalse(ledger.refresh_claude(provider))
            now[0] += H.CLAUDE_METADATA_TTL_S - 0.1
            self.assertFalse(ledger.refresh_claude(provider))
            now[0] += 0.1
            self.assertTrue(ledger.refresh_claude(provider))
            self.assertTrue(ledger.wait_claude_refresh(5))
            self.assertEqual(len(calls), 2)
            self.assertEqual(changes, [1] if result == ROWS else [])
        self.assertFalse(self.ledger().refresh_claude(object()))

    def test_one_inflight_and_slot_reserved_before_start(self):
        ledger = self.ledger()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        now = [1.0]
        ledger.metadata_clock = lambda: now[0]

        def read():
            entered.set()
            self.assertTrue(release.wait(5))
            return ROWS
        provider = SimpleNamespace(model_catalog=read)
        start = threading.Thread.start
        reserved = []

        def inspect_start(worker):
            reserved.append(ledger._claude_next)
            self.assertIs(ledger._claude_worker, worker)
            start(worker)
        with patch.object(threading.Thread, "start", inspect_start):
            self.assertTrue(ledger.refresh_claude(provider))
        self.assertEqual(reserved, [1.0 + H.CLAUDE_METADATA_TTL_S])
        self.assertTrue(entered.wait(2))
        now[0] += H.CLAUDE_METADATA_TTL_S + 1
        self.assertFalse(ledger.refresh_claude(provider), "inflight still owns slot beyond TTL")
        self.assertFalse(ledger.wait_claude_refresh(0))
        release.set()
        self.assertTrue(ledger.wait_claude_refresh(5))

    def test_stop_before_save_and_daemon_shutdown_wiring(self):
        ledger = self.ledger()
        before = copy.deepcopy(R.CLI_MODELS["claude"])
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        changes = []

        def read():
            entered.set()
            release.wait(5)
            return ROWS
        provider = SimpleNamespace(model_catalog=read)
        ledger.listen_claude_catalog(lambda: changes.append(1))
        ledger.refresh_claude(provider)
        self.assertTrue(entered.wait(2))
        fake_daemon = SimpleNamespace(router=SimpleNamespace(ledger=ledger))
        D.Daemon._stop_claude_metadata(fake_daemon, time.monotonic())
        self.assertFalse(ledger.refresh_claude(provider))
        release.set()
        self.assertTrue(ledger.wait_claude_refresh(5))
        self.assertEqual(R.CLI_MODELS["claude"], before)
        self.assertFalse(config.CLAUDE_CATALOG_PATH.exists())
        self.assertEqual(changes, [])

    def test_thread_start_and_notification_errors_are_contained(self):
        ledger = self.ledger()
        provider = SimpleNamespace(model_catalog=lambda: ROWS)
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("private")):
            self.assertFalse(ledger.refresh_claude(provider))
        self.assertTrue(ledger.wait_claude_refresh(0))
        ledger._claude_next = None
        def bad_notification():
            raise RuntimeError("private")
        ledger.listen_claude_catalog(bad_notification)
        self.assertTrue(ledger.refresh_claude(provider))
        self.assertTrue(ledger.wait_claude_refresh(5))
        self.assertEqual(R.CLI_MODELS["claude"], C.parse(ROWS))

    def test_stop_timeout_includes_gate_wait_during_install(self):
        ledger = self.ledger()
        entered, release = threading.Event(), threading.Event()

        def save(_):
            entered.set()
            release.wait(0.8)
            return True

        with patch.object(C, "save", side_effect=save):
            try:
                self.assertTrue(ledger.refresh_claude(SimpleNamespace(model_catalog=lambda: ROWS)))
                self.assertTrue(entered.wait(2))
                started = time.monotonic()
                self.assertFalse(ledger.stop_claude(0))
                self.assertLess(time.monotonic() - started, 0.2)
                self.assertTrue(R.CLAUDE_CATALOG_STOPPED.is_set())
                started = time.monotonic()
                self.assertFalse(ledger.stop_claude())
                self.assertLess(time.monotonic() - started, 0.2)
                started = time.monotonic()
                self.assertFalse(ledger.stop_claude(0.05))
                self.assertLess(time.monotonic() - started, 0.2)
            finally:
                release.set()
                self.assertTrue(ledger.wait_claude_refresh(5))


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
