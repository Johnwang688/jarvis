"""Free checks for a chat thread's model and effort (decisions 2026-10-06, A).

No API calls: the OpenRouter catalog is a fixture, the roster and routing file
live in a temp directory, and nothing reads the owner's models.json.

Run:  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python tests/v2/thread_model_check.py
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis import config, models  # noqa: E402
from jarvis.v2 import router, thread_model as tm  # noqa: E402
from jarvis.v2.model import ProviderName as P, Role, Thread  # noqa: E402


def info(model_id, efforts=(), vision=True):
    return models.ModelInfo(id=model_id, name=model_id, efforts=tuple(efforts), vision=vision)


CATALOG = {m.id: m for m in (
    info("openai/gpt-5.6-luna", ("max", "high", "medium", "low")),
    info("deepseek/deepseek-v4-flash-0731", ("high", "medium", "low"), vision=False),
    info("moonshotai/kimi-k3", ("medium", "low")),
    info("plain/no-reasoning", ()),
    info("cat/only-max", ("max",)),
)}


class ThreadModel(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name, value in dict(MODELS_PATH=root / "models.json", ROUTING_PATH=root / "routing.json",
                                MODEL_CACHE_PATH=root / "catalog.json").items():
            guard = patch.object(config, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        for name, fn in (("cached_info", lambda m: CATALOG.get(m)), ("find", lambda m: CATALOG.get(m)),
                         ("catalog", lambda refresh=False: list(CATALOG.values()))):
            guard = patch.object(models, name, side_effect=fn)
            guard.start()
            self.addCleanup(guard.stop)
        self.default = config.TIERS["orchestrator"]

    def roster(self, *ids, selected="", efforts=None):
        config.MODELS_PATH.write_text(json.dumps({"models": [self.default, *ids], "selected": selected,
                                                  "efforts": efforts or {}}))

    def test_effort_default_is_high_within_the_models_ladder(self):
        """A4: high for every model, clamped to its own ladder; none for a
        model with no control; the roster's pin wins on OpenRouter."""
        self.roster("moonshotai/kimi-k3", "plain/no-reasoning", "cat/only-max",
                    efforts={"moonshotai/kimi-k3": "low"})
        self.assertEqual(tm.default_effort(P.FAST, "openai/gpt-5.6-luna"), "high")
        self.assertEqual(tm.default_effort(P.FAST, "moonshotai/kimi-k3"), "low", "the roster pin is the default")
        self.roster("moonshotai/kimi-k3")
        self.assertEqual(tm.default_effort(P.FAST, "moonshotai/kimi-k3"), "medium", "high clamps down, never up")
        self.assertIsNone(tm.default_effort(P.FAST, "plain/no-reasoning"))
        self.assertEqual(tm.default_effort(P.FAST, "cat/only-max"), "max", "no level below: the nearest above")
        self.assertEqual(tm.default_effort(P.FAST, "unknown/cold-cache"), "high", "a cold catalog sends it as asked")
        self.assertEqual(tm.default_effort(P.CLAUDE, "claude-opus-5-5"), "high")
        self.assertIsNone(tm.default_effort(P.CLAUDE, "claude-haiku-4-5"))
        self.assertEqual(tm.default_effort(P.CODEX, "gpt-5.6-sol"), "high")

    def test_defaults_per_provider(self):
        """A5: OpenRouter follows the global picker (read, not hard-coded);
        Claude is Opus 5.5 at high; Codex is its routing default."""
        self.roster("deepseek/deepseek-v4-flash-0731")
        self.assertEqual(tm.default_choice(P.FAST), (self.default, tm.default_effort(P.FAST, self.default)))
        self.roster("deepseek/deepseek-v4-flash-0731", selected="deepseek/deepseek-v4-flash-0731")
        self.assertEqual(tm.default_choice(P.FAST), ("deepseek/deepseek-v4-flash-0731", "high"))
        self.assertEqual(tm.default_choice(P.CLAUDE), ("claude-opus-5-5", "high"))
        self.assertEqual(tm.default_choice(P.CODEX), ("gpt-6-astra", "xhigh"))
        config.ROUTING_PATH.write_text(json.dumps({"models": {"orchestrator": {"codex": "gpt-5.6-sol/default"}}}))
        self.assertEqual(tm.default_choice(P.CODEX), ("gpt-5.6-sol", "high"))

    def test_effective_choice(self):
        self.roster("moonshotai/kimi-k3", selected="")
        thread = Thread("abcdef01", "p", Role.CHAT, P.FAST)
        self.assertEqual(tm.effective(thread), (self.default, "high"))
        thread.model = "moonshotai/kimi-k3"
        self.assertEqual(tm.effective(thread), ("moonshotai/kimi-k3", "medium"))
        thread.effort = "low"
        self.assertEqual(tm.effective(thread), ("moonshotai/kimi-k3", "low"))

    def test_check_refuses_with_the_reason(self):
        self.roster("moonshotai/kimi-k3")
        self.assertEqual(tm.check(P.FAST, "moonshotai/kimi-k3", "low"), ("moonshotai/kimi-k3", "low"))
        self.assertEqual(tm.check(P.FAST, None, None), (None, None))
        self.assertEqual(tm.check(P.FAST, "  ", ""), (None, None))
        cases = [
            ((P.FAST, "openai/gpt-5.6-luna-not", None), "supports tool calling"),
            ((P.FAST, "deepseek/deepseek-v4-flash-0731", None), "not on your model roster"),
            ((P.FAST, "moonshotai/kimi-k3", "high"), "does not offer 'high'"),
            ((P.FAST, "moonshotai/kimi-k3", "turbo"), "not a reasoning effort"),
            ((P.CLAUDE, "claude-opus-9", None), "not a Claude model"),
            ((P.CLAUDE, "claude-haiku-4-5", "low"), "no reasoning effort"),
            ((P.CODEX, "moonshotai/kimi-k3", None), "not a Codex model"),
            ((P.CODEX, "gpt-5.6-sol", "max"), "does not offer 'max'"),
            ((P.FAST, 42, None), "must be a string"),
        ]
        for args, reason in cases:
            with self.assertRaises(tm.ChoiceRefused, msg=args) as caught:
                tm.check(*args)
            self.assertIn(reason, str(caught.exception))

    def test_a_pin_survives_leaving_the_roster(self):
        """A3: a model already pinned on a thread stays usable — an effort
        change on it is not a new choice of model."""
        self.roster()
        self.assertEqual(tm.check(P.FAST, "moonshotai/kimi-k3", "low", current="moonshotai/kimi-k3"),
                         ("moonshotai/kimi-k3", "low"))
        with self.assertRaises(tm.ChoiceRefused):
            tm.check(P.FAST, "moonshotai/kimi-k3", "low", current=None)

    def test_cli_lists_come_from_the_routers_table(self):
        """A2: Claude and Codex list the models the router's own checks use."""
        described = tm.describe()["providers"]
        self.assertEqual([m["id"] for m in described["claude"]["models"]], list(router.CLI_MODELS["claude"]))
        self.assertEqual([m["id"] for m in described["codex"]["models"]], list(router.CLI_MODELS["codex"]))
        self.assertEqual(described["claude"]["default"], "claude-opus-5-5")
        self.assertEqual(described["fast"]["label"], "OpenRouter")
        # The router's vision filter reads the same table.
        self.assertTrue(all(router.CLI_MODELS["codex"][m]["vision"] for m in
                            ("gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5")))

    def test_only_owner_chat_threads_have_a_choice(self):
        self.assertTrue(tm.is_chat(Thread("abcdef01", "p", Role.CHAT, P.FAST)))
        self.assertFalse(tm.is_chat(Thread("abcdef01", "p", Role.CHAT, P.FAST, task_id="12345678")))
        self.assertFalse(tm.is_chat(Thread("abcdef01", "p", Role.IMPLEMENTER, P.CODEX)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
