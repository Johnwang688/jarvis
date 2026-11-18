"""Free speed capability, choice and Claude SDK option checks. No CLI or network."""
import inspect
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jarvis import config
from jarvis.v2.daemon import APIError, Daemon, DaemonError, _Session
from jarvis.v2.stores import Stores
from jarvis.v2 import claude_catalog as C, codex_catalog as X, router, thread_model as TM
from jarvis.v2.model import ProviderName as P, Role, Thread, from_json, to_json
from jarvis.v2.provider import Brief
from jarvis.v2.providers import claude


class Catalog(unittest.TestCase):
    def test_codex_tiers_clean_cap_dedupe_slug_and_roundtrip(self):
        tiers = [None, {}, {"id": "bad id"}, {"id": "x\n"}, {"id": 3},
                 {"id": "priority", "name": "Fast\n\x00‮ mode", "description": "More\nusage\x00"},
                 {"id": "priority", "name": "duplicate"},
                 {"id": "ultrafast", "name": "N" * 999, "description": "D" * 999}]
        tiers += [{"id": f"tier-{i}", "name": None, "description": []} for i in range(12)]
        table = X.parse([{"id": "gpt-test", "serviceTiers": tiers,
                          "additionalSpeedTiers": [{"id": "deprecated"}]}])
        speeds = table["gpt-test"]["speeds"]
        self.assertEqual(len(speeds), 8)
        self.assertEqual(speeds[0], ("priority", "Fast mode", "More usage"))
        self.assertEqual(speeds[1], ("ultrafast", "N" * (X.MAX_NAME - 1) + "…", "D" * (X.MAX_DESCRIPTION - 1) + "…"))
        self.assertEqual(speeds[2], ("tier-0", "tier-0", ""))
        self.assertEqual(X.parse(X.rows_of(table)), table)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "catalog.json"
            self.assertTrue(X.save(table, path))
            self.assertEqual(X.load(path), table)
        for raw in (None, {}, 1, "fast"):
            self.assertEqual(X.parse([{"id": "m", "serviceTiers": raw}])["m"]["speeds"], ())
        self.assertEqual(X.parse([{"id": "old", "additionalSpeedTiers": tiers}])["old"]["speeds"], ())

    def test_claude_requires_exact_true_and_cache_roundtrip(self):
        for supports in (True, False, 1, "true", None, [], {}):
            table = C.parse([{"value": "claude-test", "supportsFastMode": supports}])
            self.assertEqual(table["claude-test"]["speeds"], C.FAST_SPEED if supports is True else ())
            self.assertEqual(C.parse(C.rows_of(table)), table)
            with tempfile.TemporaryDirectory() as root:
                path = Path(root) / "catalog.json"
                self.assertTrue(C.save(table, path))
                self.assertEqual(C.load(path), table)
        self.assertEqual(C.parse([{"value": "claude-old"}])["claude-old"]["speeds"], ())

    def test_worst_case_catalogs_save_and_load_within_read_cap(self):
        # Astral characters cost 12 bytes in ensure_ascii JSON; CJK costs 6.
        for text in ("界", "😀"):
            tiers = [{"id": f"t{i}".ljust(64, "x"), "name": text * X.MAX_NAME,
                      "description": text * X.MAX_DESCRIPTION} for i in range(X.MAX_SPEEDS)]
            tables = (
                (X, X.parse([{"id": f"m{i}".ljust(64, "x"), "displayName": text * X.MAX_NAME,
                              "serviceTiers": tiers, "inputModalities": ["text", "image"],
                              "supportedReasoningEfforts": list(router.CODEX_EFFORT_LADDER),
                              "defaultReasoningEffort": "ultra"} for i in range(X.MAX_MODELS)])),
                (C, C.parse([{"value": f"claude-{i}".ljust(64, "x"),
                              "displayName": "Claude " + text * (C.MAX_NAME - 7),
                              "supportsFastMode": True, "supportsEffort": True,
                              "supportedEffortLevels": list(router._CLAUDE_EFFORTS)}
                             for i in range(C.MAX_MODELS)])),
            )
            for module, table in tables:
                with self.subTest(catalog=module.__name__, text=text), tempfile.TemporaryDirectory() as root:
                    path = Path(root) / "catalog.json"
                    self.assertTrue(module.save(table, path))
                    self.assertLessEqual(path.stat().st_size, module.MAX_CACHE_BYTES)
                    self.assertEqual(module.load(path), table)


class Choices(unittest.TestCase):
    def setUp(self):
        table = {"claude-a": {"name": "A", "efforts": (), "vision": True, "speeds": C.FAST_SPEED},
                 "claude-b": {"name": "B", "efforts": (), "vision": True, "speeds": ()}}
        context = patch.dict(router.CLI_MODELS, {"claude": table})
        context.start()
        self.addCleanup(context.stop)

    def test_speed_validation_current_model_and_reset(self):
        self.assertEqual(TM.check_speed(P.CLAUDE, "claude-a", "fast"), "fast")
        for reset in (None, "", "  "):
            self.assertIsNone(TM.check_speed(P.CLAUDE, "claude-a", reset))
        for model, speed in (("claude-b", "fast"), ("claude-a", "priority"), ("claude-a", True)):
            with self.assertRaises(TM.ChoiceRefused) as error:
                TM.check_speed(P.CLAUDE, model, speed)
            self.assertIn(model, str(error.exception))
        with patch.object(TM, "default_model", return_value="claude-b"):
            with self.assertRaises(TM.ChoiceRefused):
                TM.check_speed(P.CLAUDE, None, "fast")
        self.assertEqual(TM.speeds_of(P.FAST, "anything"), ())

    def test_codex_tiers_are_model_specific(self):
        table = X.parse([{"id": "gpt-future", "serviceTiers": [
            {"id": "priority", "name": "Fast", "description": "2x speed, increased usage"},
            {"id": "future", "name": "Future", "description": "Catalog supplied"}]}, {"id": "gpt-plain"}])
        with patch.dict(router.CLI_MODELS, {"codex": table}):
            self.assertEqual(TM.check_speed(P.CODEX, "gpt-future", "future"), "future")
            with self.assertRaises(TM.ChoiceRefused):
                TM.check_speed(P.CODEX, "gpt-plain", "priority")

    def test_turn_downgrade_preserves_saved_choice_and_describe(self):
        thread = Thread("abcdef01", "p", Role.CHAT, P.CLAUDE, model="claude-a", speed="fast", speed_model="claude-a")
        self.assertEqual(TM.effective_speed(thread, "claude-a"), "fast")
        with patch.object(router, "warn_once") as note, patch.dict(router.CLI_MODELS["claude"]["claude-b"], speeds=C.FAST_SPEED):
            self.assertIsNone(TM.effective_speed(thread, "claude-b"))
        self.assertIn("using standard speed", note.call_args[0][0])
        self.assertEqual(thread.speed, "fast")
        self.assertEqual(from_json(Thread, to_json(thread)).speed, "fast")
        self.assertIsNone(from_json(Thread, {k: v for k, v in to_json(thread).items() if k != "speed"}).speed)
        with patch.object(TM, "_read_defaults", return_value={}):
            rows = TM.describe()["providers"]["claude"]["models"]
        self.assertEqual(rows[0]["speeds"], list(C.FAST_SPEED))
        self.assertEqual(rows[1]["speeds"], [])

    def test_legacy_saved_speed_is_dormant_until_owner_reselects(self):
        thread = Thread("abcdef01", "p", Role.CHAT, P.CLAUDE, model="claude-a", speed="fast")
        with patch.object(router, "warn_once"):
            self.assertIsNone(TM.effective_speed(thread, "claude-a"))
        self.assertEqual((thread.speed, thread.speed_model), ("fast", None))
        legacy = to_json(thread)
        legacy.pop("speed_model")
        self.assertIsNone(from_json(Thread, legacy).speed_model)

    def test_default_following_speed_is_never_effective_even_at_its_bound_model(self):
        thread = Thread("abcdef01", "p", Role.CHAT, P.CLAUDE, speed="fast", speed_model="claude-a")
        with patch.object(router, "warn_once"):
            self.assertIsNone(TM.effective_speed(thread, "claude-a"))
        self.assertEqual((thread.model, thread.speed, thread.speed_model), (None, "fast", "claude-a"))

    def options(self, speed, model="claude-a", settings=None):
        session = SimpleNamespace(mcp_config=None, isolated=False, stderr=[])
        kwargs = {} if settings is None else {"settings": json.dumps(settings)}
        with patch.object(claude, "_cli_options", return_value=kwargs):
            return claude.ClaudeProvider()._options(
                Brief(Role.CHAT, "/tmp", model=model, speed=speed), session, resume=False, session_id="test")

    def test_claude_fastmode_only_when_fast_and_supported(self):
        self.assertEqual(json.loads(self.options("fast").settings), {"fastMode": True})
        # Standard is explicit too, so Claude Code's own settings files can never
        # turn fast mode on behind the HUD's back (follow-up 1, 2026-11-17).
        for speed, model in ((None, "claude-a"), ("priority", "claude-a"), ("fast", "claude-b")):
            self.assertEqual(json.loads(self.options(speed, model).settings), {"fastMode": False})

    def test_claude_settings_merge_preserves_other_options(self):
        options = self.options("fast", settings={"theme": "dark", "nested": {"keep": True}})
        self.assertEqual(json.loads(options.settings),
                         {"theme": "dark", "nested": {"keep": True}, "fastMode": True})
        # A supported Fast choice overrides an incoming `false` too (review of #411).
        self.assertEqual(json.loads(self.options("fast", settings={"fastMode": False, "theme": "dark"}).settings),
                         {"theme": "dark", "fastMode": True})
        self.assertEqual(json.loads(self.options(None, settings={"theme": "dark"}).settings),
                         {"theme": "dark", "fastMode": False})
        for speed, model in ((None, "claude-a"), ("fast", "claude-b")):
            self.assertEqual(json.loads(self.options(speed, model, settings={"fastMode": True, "theme": "dark"}).settings),
                             {"theme": "dark", "fastMode": False})


class LockedWrites(unittest.TestCase):
    """Real thread store and daemon write/turn methods; no listeners or providers."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for key, value in {"V2_DATA_DIR": root / "data", "PROVIDER_DEFAULTS_PATH": root / "defaults.json"}.items():
            guard = patch.object(config, key, value)
            guard.start()
            self.addCleanup(guard.stop)
        table = {m: {"name": m, "efforts": ("high", "low"), "vision": True,
                     "speeds": (("ultrafast", "Ultrafast", "more quota"),)} for m in ("gpt-a", "gpt-b")}
        guard = patch.dict(router.CLI_MODELS, {"codex": table})
        guard.start()
        self.addCleanup(guard.stop)
        self.stores = Stores()
        self.project = self.stores.projects.create("Test", str(root))
        self.thread = self.stores.threads.create(self.project.id, Role.CHAT, P.CODEX,
                                                model="gpt-a", speed="ultrafast", speed_model="gpt-a")
        self.stores.threads.save(self.thread)
        self.daemon = object.__new__(Daemon)
        self.daemon.stores = self.stores
        self.daemon._lock = threading.RLock()
        self.daemon._active = lambda: None
        self.daemon.providers = {P.CODEX: SimpleNamespace(set_model=lambda *a, **k: None)}
        self.daemon._sessions = {}
        self.daemon.bus = SimpleNamespace(publish=lambda event: None)
        self.daemon.thread_json = to_json

    def write(self, body, **kw):
        return self.daemon.set_thread_model(self.thread.id, body, **kw)

    def saved(self):
        return self.stores.threads.get(self.thread.id)

    def queue_write(self, body):
        # Suspend immediately before lock acquisition. An implementation that
        # derived speed or authorized from a pre-lock read has already done so.
        queued, release = threading.Event(), threading.Event()
        lock = self.daemon._lock
        class Gate:
            def __enter__(gate):
                if threading.current_thread().name == "non-owner":
                    queued.set()
                    if not release.wait(5):
                        raise AssertionError("owner did not release the queued write")
                return lock.__enter__()
            def __exit__(gate, *args):
                return lock.__exit__(*args)
        self.daemon._lock = Gate()
        results = []
        def refuse_owner():
            raise APIError(403, "owner required")
        def run():
            try:
                results.append(self.write(body, owner_check=refuse_owner))
            except Exception as exc:
                results.append(exc)
        worker = threading.Thread(target=run, name="non-owner")
        worker.start()
        self.addCleanup(lambda: (release.set(), worker.join(5)))
        self.assertTrue(queued.wait(5))
        return release, worker, results

    def test_non_owner_effort_cannot_restore_owner_speed_reset(self):
        release, worker, results = self.queue_write({"effort": "low"})
        self.write({"speed": None, "speed_model": "gpt-a"})
        release.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], dict)
        self.assertEqual(self.saved().effort, "low")
        self.assertEqual((self.saved().speed, self.saved().speed_model), (None, None))

    def test_model_authorization_uses_locked_record(self):
        self.write({"speed": None, "speed_model": "gpt-a"})
        release, worker, results = self.queue_write({"model": "gpt-b"})
        self.write({"speed": "ultrafast", "speed_model": "gpt-a"})
        release.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], APIError)
        self.assertEqual(results[0].status, 403)
        self.assertEqual((self.saved().model, self.saved().speed_model), ("gpt-a", "gpt-a"))

    def test_default_change_cannot_reactivate_dormant_tier_and_owner_reselects(self):
        thread = self.saved()
        thread.model = None
        self.stores.threads.save(thread)
        TM.set_provider_default("codex", "gpt-a")
        router.CLI_MODELS["codex"]["gpt-a"]["speeds"] = ()
        with patch.object(router, "warn_once"):
            self.assertIsNone(TM.effective_speed(self.saved(), TM.effective(self.saved())[0]))
            TM.set_provider_default("codex", "gpt-b")
            self.assertEqual(TM.effective(self.saved())[0], "gpt-b")
            self.assertIsNone(TM.effective_speed(self.saved(), "gpt-b"))
        self.assertEqual((self.saved().speed, self.saved().speed_model), ("ultrafast", "gpt-a"))
        self.write({"speed": "ultrafast", "speed_model": "gpt-b"})
        self.assertEqual(self.saved().speed_model, "gpt-b")
        self.assertEqual(TM.effective_speed(self.saved(), "gpt-b"), "ultrafast")

    def test_owner_model_write_rebinds_compatible_speed_and_clears_incompatible_pair(self):
        self.write({"model": "gpt-b"})
        self.assertEqual((self.saved().speed, self.saved().speed_model), ("ultrafast", "gpt-b"))
        router.CLI_MODELS["codex"]["gpt-a"]["speeds"] = ()
        self.write({"model": "gpt-a"})
        self.assertEqual((self.saved().speed, self.saved().speed_model), (None, None))

    def test_owner_model_write_does_not_bind_legacy_speed(self):
        thread = self.saved()
        thread.speed_model = None
        self.stores.threads.save(thread)
        self.write({"model": "gpt-b"})
        self.assertEqual((self.saved().model, self.saved().speed, self.saved().speed_model),
                         ("gpt-b", "ultrafast", None))
        with patch.object(router, "warn_once"):
            self.assertIsNone(TM.effective_speed(self.saved(), "gpt-b"))

    def test_follow_default_clears_both_speed_fields(self):
        TM.set_provider_default("codex", "gpt-a")
        self.write({"model": None})
        self.assertEqual((self.saved().model, self.saved().speed, self.saved().speed_model), (None, None, None))

    def test_speed_write_pins_once_preserves_effort_and_standard_never_pins(self):
        thread = self.saved()
        thread.model, thread.effort = None, "low"
        self.stores.threads.save(thread)
        with patch.object(TM, "default_choice", return_value=("gpt-a", "high")) as resolve:
            for reset in (None, "", "  "):
                self.write({"speed": reset, "speed_model": "gpt-a"})
                self.assertIsNone(self.saved().model)
            with self.assertRaises(APIError) as error:
                self.write({"speed": "ultrafast", "speed_model": "gpt-b"})
            self.assertEqual(error.exception.status, 409)
            resolve.reset_mock()
            self.write({"speed": "ultrafast", "speed_model": "gpt-a"})
            resolve.assert_called_once()
        self.assertEqual((self.saved().model, self.saved().effort, self.saved().speed_model), ("gpt-a", "low", "gpt-a"))
        self.assertEqual(TM.effective_speed(self.saved(), "gpt-a"), "ultrafast")

    def test_creation_speed_resolves_once_under_lock_and_pins_the_validated_model(self):
        self.daemon._open = lambda *args: None
        self.daemon._lifecycle = lambda *args: None
        def resolve(provider):
            self.assertTrue(self.daemon._lock._is_owned())
            return "gpt-a"
        with patch.object(TM, "default_model", side_effect=resolve) as default:
            opened = self.daemon.open_thread(self.project.id, Role.CHAT, P.CODEX,
                                             {"speed": "ultrafast", "effort": "low"})
            default.assert_called_once_with(P.CODEX)
        self.assertEqual((opened.model, opened.speed_model, opened.speed, opened.effort),
                         ("gpt-a", "gpt-a", "ultrafast", "low"))
        self.assertEqual(TM.effective_speed(opened, "gpt-a"), "ultrafast")

    def test_creation_standard_does_not_pin_and_stale_binding_is_refused(self):
        self.daemon._open = lambda *args: None
        self.daemon._lifecycle = lambda *args: None
        TM.set_provider_default("codex", "gpt-a")
        for reset in (None, "", "  "):
            opened = self.daemon.open_thread(self.project.id, Role.CHAT, P.CODEX, {"speed": reset})
            self.assertEqual((opened.model, opened.speed, opened.speed_model), (None, None, None))
        with self.assertRaises(APIError) as error:
            self.daemon.open_thread(self.project.id, Role.CHAT, P.CODEX,
                                    {"speed": "ultrafast", "speed_model": "gpt-b"})
        self.assertEqual(error.exception.status, 409)

    def test_speed_model_precondition_rejects_old_model_even_with_same_tier(self):
        self.write({"model": "gpt-b"})
        before = to_json(self.saved())
        for speed in ("ultrafast", None):
            with self.assertRaises(APIError) as error:
                self.write({"speed": speed, "speed_model": "gpt-a"})
            self.assertEqual(error.exception.status, 409)
            self.assertEqual(to_json(self.saved()), before)
        with self.assertRaises(APIError) as error:
            self.write({"speed": "ultrafast"})
        self.assertEqual(error.exception.status, 409)

    def test_reconnect_rollback_uses_the_snapshot_applied_to_provider(self):
        self.write({"speed": None, "speed_model": "gpt-a"})
        session = _Session(self.saved(), Brief(Role.CHAT, "/tmp"), SimpleNamespace())
        session.applied = session.announced = ("gpt-a", "high")
        session.applied_record = ("gpt-a", None)
        self.daemon._sessions[self.thread.id] = session
        self.write({"speed": "ultrafast", "speed_model": "gpt-a"})
        applied = []
        def reconnect(handle, model, effort, *, speed=None):
            applied.append(speed)
            # The owner saves Standard while the successful Fast reconnect is out of lock.
            self.write({"speed": None, "speed_model": "gpt-a"})
        session.provider.set_model = reconnect
        self.daemon._apply_choice(session)
        self.assertEqual(applied, ["ultrafast"])
        self.assertIsNone(self.saved().speed)
        self.assertEqual((session.applied_record_speed, session.applied_record_speed_model), ("ultrafast", "gpt-a"))
        def fail_reconnect(*args, **kwargs):
            raise ValueError("failed; provider restored the previous Fast client")
        session.provider.set_model = fail_reconnect
        self.daemon._system_line = lambda *a, **k: None
        self.daemon._switch_ended_subagents = lambda session: None
        self.daemon._apply_choice(session)
        self.assertEqual(session.applied_speed, "ultrafast")
        # Round-3 review, HIGH 2: the owner's Standard stands; a rollback never
        # writes speed back, and the provider still on the paid tier gets no message.
        self.assertEqual((self.saved().speed, self.saved().speed_model), (None, None))
        retired = []
        self.daemon._retire_drive = retired.append
        with self.assertRaises(DaemonError):
            self.daemon._refuse_unasked_tier(session)
        self.assertTrue(session.lost, "the session closes so the next message resumes from the record")
        self.assertEqual(retired, [session])

    def _refusing_session(self, thread, applied, applied_speed):
        session = _Session(thread, Brief(Role.CHAT, "/tmp"), SimpleNamespace())
        session.applied = session.announced = applied
        session.applied_speed = applied_speed
        session.applied_record = (thread.model, thread.effort)
        session.applied_record_speed, session.applied_record_speed_model = thread.speed, thread.speed_model
        def refuse(*args, **kwargs):
            raise ValueError("refused")
        session.provider.set_model = refuse
        self.daemon._sessions[thread.id] = session
        self.daemon._system_line = lambda *a, **k: None
        self.daemon._switch_ended_subagents = lambda session: None
        return session

    def test_rollback_pin_never_activates_a_dormant_legacy_tier(self):
        """Round-3 review, HIGH 1: a non-owner default change refused by the
        provider made rollback pin the bound model and wake the tier."""
        thread = self.saved()
        thread.model, thread.effort = None, None
        thread.speed, thread.speed_model = "ultrafast", "gpt-a"      # legacy, dormant
        self.stores.threads.save(thread)
        session = self._refusing_session(self.saved(), ("gpt-a", "high"), None)
        with patch.object(TM, "default_choice", return_value=("gpt-b", "high")):
            self.daemon._apply_choice(session)
        saved = self.saved()
        self.assertEqual(saved.model, "gpt-a", "rollback still pins what the provider runs")
        self.assertIsNone(TM.effective_speed(saved, "gpt-a"))
        self.assertEqual((saved.speed, saved.speed_model), (None, None))
        self.daemon._refuse_unasked_tier(session)     # standard on both sides: sends

    def test_rollback_pin_clears_speed_so_a_catalog_refresh_cannot_wake_it(self):
        """Round-4 review, HIGH 1: the tier was unavailable at rollback, the
        pin kept the pair, and a later catalog refresh made it effective."""
        thread = self.saved()
        thread.model, thread.effort = None, None
        thread.speed, thread.speed_model = "ultrafast", "gpt-a"
        self.stores.threads.save(thread)
        tierless = {m: {**e, "speeds": ()} for m, e in router.CLI_MODELS["codex"].items()}
        session = self._refusing_session(self.saved(), ("gpt-a", "high"), None)
        with patch.dict(router.CLI_MODELS, {"codex": tierless}), \
                patch.object(TM, "default_choice", return_value=("gpt-b", "high")):
            self.daemon._apply_choice(session)
        saved = self.saved()
        self.assertEqual(saved.model, "gpt-a")
        self.assertEqual((saved.speed, saved.speed_model), (None, None))
        # The catalog offers the tier again: still nothing to wake.
        self.assertIsNone(TM.effective_speed(saved, "gpt-a"))

    def test_steer_onto_an_unasked_paid_tier_waits_for_the_next_turn(self):
        """Round-4 review, HIGH 3: a steer reached a Fast client after the
        owner saved Standard."""
        self.write({"speed": None, "speed_model": "gpt-a"})
        session = _Session(self.saved(), Brief(Role.CHAT, "/tmp"), SimpleNamespace())
        session.applied, session.applied_speed = ("gpt-a", "high"), "ultrafast"
        steered, queued = [], []
        provider = SimpleNamespace(steer=lambda handle, message: steered.append(message))
        self.daemon._queues = {}
        self.daemon._enqueue = lambda session, waiting, message, data: queued.append(message) or message
        self.daemon._queued_reply = lambda session, item: {"status": "queued"}
        self.daemon._drain = lambda thread_id: None
        session.worker = object()
        self.daemon._log_user = lambda *a, **k: None
        message = SimpleNamespace(text="more please", desk=False, origin="owner")
        reply = self.daemon._steer(session, provider, object(), message, {})
        self.assertEqual(steered, [], "nothing reaches the possibly-paid client")
        self.assertEqual((reply, queued), ({"status": "queued"}, [message]))

    def test_the_turn_guards_send_and_compaction(self):
        """Round-4 review: removing the guard call from `_turn` survived the
        unit tests, so its placement is pinned here."""
        source = inspect.getsource(Daemon._turn)
        send = source.index("source = session.provider.send(")
        compact = source.index("source = session.provider.compact(")
        guards = [i for i in range(len(source)) if source.startswith("self._refuse_unasked_tier(session)", i)]
        self.assertTrue(any(i < send and i > source.index("self._apply_choice(session)") for i in guards))
        self.assertTrue(any(send < i < compact for i in guards))

    def test_explicit_effort_reset_is_honoured_when_pinning_for_speed(self):
        thread = self.saved()
        thread.model, thread.effort, thread.speed, thread.speed_model = None, "low", None, None
        self.stores.threads.save(thread)
        with patch.object(TM, "default_choice", return_value=("gpt-a", "high")):
            self.write({"effort": None, "speed": "ultrafast", "speed_model": "gpt-a"})
        self.assertEqual((self.saved().model, self.saved().effort), ("gpt-a", "high"))

    def test_speed_pick_on_a_default_chat_keeps_its_inherited_effort(self):
        """Round-3 review, MED: pinning for a speed kept effort null, so the
        next turn ran at the model's default instead of the inherited one."""
        thread = self.saved()
        thread.model, thread.effort, thread.speed, thread.speed_model = None, None, None, None
        self.stores.threads.save(thread)
        with patch.object(TM, "default_choice", return_value=("gpt-a", "low")):
            self.write({"speed": "ultrafast", "speed_model": "gpt-a"})
        self.assertEqual((self.saved().model, self.saved().effort), ("gpt-a", "low"))

    def test_unpin_with_a_paid_speed_is_refused_and_standard_unpin_checks_the_new_default(self):
        with self.assertRaises(APIError) as error:
            self.write({"model": None, "speed": "ultrafast", "speed_model": "gpt-a"})
        self.assertEqual(error.exception.status, 400)
        self.assertEqual((self.saved().model, self.saved().speed), ("gpt-a", "ultrafast"))
        with patch.object(TM, "default_choice", return_value=("gpt-b", "high")):
            self.write({"model": None, "speed": None, "speed_model": "gpt-a"})
        self.assertEqual((self.saved().model, self.saved().speed, self.saved().speed_model), (None, None, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
