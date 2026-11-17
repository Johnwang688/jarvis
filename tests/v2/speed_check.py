"""Free speed capability, choice and Claude SDK option checks. No CLI or network."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

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
        thread = Thread("abcdef01", "p", Role.CHAT, P.CLAUDE, model="claude-a", speed="fast")
        self.assertEqual(TM.effective_speed(thread, "claude-a"), "fast")
        with patch.object(router, "warn_once") as note:
            self.assertIsNone(TM.effective_speed(thread, "claude-b"))
        self.assertIn("using standard speed", note.call_args[0][0])
        self.assertEqual(thread.speed, "fast")
        self.assertEqual(from_json(Thread, to_json(thread)).speed, "fast")
        self.assertIsNone(from_json(Thread, {k: v for k, v in to_json(thread).items() if k != "speed"}).speed)
        with patch.object(TM, "_read_defaults", return_value={}):
            rows = TM.describe()["providers"]["claude"]["models"]
        self.assertEqual(rows[0]["speeds"], list(C.FAST_SPEED))
        self.assertEqual(rows[1]["speeds"], [])

    def options(self, speed, model="claude-a", settings=None):
        session = SimpleNamespace(mcp_config=None, isolated=False, stderr=[])
        kwargs = {} if settings is None else {"settings": json.dumps(settings)}
        with patch.object(claude, "_cli_options", return_value=kwargs):
            return claude.ClaudeProvider()._options(
                Brief(Role.CHAT, "/tmp", model=model, speed=speed), session, resume=False, session_id="test")

    def test_claude_fastmode_only_when_fast_and_supported(self):
        self.assertEqual(json.loads(self.options("fast").settings), {"fastMode": True})
        for speed, model in ((None, "claude-a"), ("priority", "claude-a"), ("fast", "claude-b")):
            self.assertIsNone(self.options(speed, model).settings)

    def test_claude_settings_merge_preserves_other_options(self):
        options = self.options("fast", settings={"theme": "dark", "nested": {"keep": True}})
        self.assertEqual(json.loads(options.settings),
                         {"theme": "dark", "nested": {"keep": True}, "fastMode": True})
        self.assertEqual(json.loads(self.options(None, settings={"theme": "dark"}).settings), {"theme": "dark"})
        for speed, model in ((None, "claude-a"), ("fast", "claude-b")):
            self.assertEqual(json.loads(self.options(speed, model, settings={"fastMode": True, "theme": "dark"}).settings),
                             {"theme": "dark"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
