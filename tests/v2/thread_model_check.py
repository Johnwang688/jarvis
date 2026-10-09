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
                                PROVIDER_DEFAULTS_PATH=root / "provider_defaults.json",
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

    def test_an_effort_on_a_default_thread_follows_the_default_model(self):
        """A4 amendment: the effort is stored on its own, the model stays
        default, and the effort is clamped to whatever the default is now —
        nothing at all for a default with no reasoning control."""
        self.roster("moonshotai/kimi-k3", "plain/no-reasoning", "cat/only-max",
                    selected="openai/gpt-5.6-luna")
        self.assertEqual(tm.check(P.FAST, None, "max"), (None, "max"), "an effort alone pins nothing")
        thread = Thread("abcdef01", "p", Role.CHAT, P.FAST, effort="max")
        self.assertEqual(tm.effective(thread), ("openai/gpt-5.6-luna", "max"))
        self.roster("moonshotai/kimi-k3", "plain/no-reasoning", "cat/only-max",
                    selected="moonshotai/kimi-k3")
        self.assertEqual(tm.effective(thread), ("moonshotai/kimi-k3", "medium"), "clamped down to the ladder")
        self.assertEqual(thread.effort, "max", "the stored choice is not rewritten")
        thread.effort = "low"
        self.assertEqual(tm.effective(thread), ("moonshotai/kimi-k3", "low"))
        self.roster("moonshotai/kimi-k3", "plain/no-reasoning", "cat/only-max",
                    selected="plain/no-reasoning")
        self.assertEqual(tm.effective(thread), ("plain/no-reasoning", None), "no ladder, no effort")
        self.roster("moonshotai/kimi-k3", "plain/no-reasoning", "cat/only-max", selected="cat/only-max")
        self.assertEqual(tm.effective(thread), ("cat/only-max", "max"), "nothing below: the nearest above")
        with self.assertRaises(tm.ChoiceRefused):
            tm.check(P.FAST, None, "low")      # the default of the moment does not offer it
        # Claude and Codex follow their own defaults the same way.
        self.assertEqual(tm.effective(Thread("abcdef02", "p", Role.CHAT, P.CLAUDE, effort="low")),
                         ("claude-opus-5-5", "low"))
        self.assertEqual(tm.effective(Thread("abcdef03", "p", Role.CHAT, P.CODEX, effort="max")),
                         ("gpt-6-astra", "xhigh"))
        # An explicit model still pins: the default moving does not move it.
        pinned = Thread("abcdef04", "p", Role.CHAT, P.FAST, model="moonshotai/kimi-k3", effort="low")
        self.roster("moonshotai/kimi-k3", selected="")
        self.assertEqual(tm.effective(pinned), ("moonshotai/kimi-k3", "low"))

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

    def test_the_hud_default_for_claude_and_codex(self):
        """2026-10-08: a HUD-chosen default per CLI provider. It moves every
        default thread (and re-clamps an effort-only one), never a pinned one;
        reset returns to the built-in default; the Codex one never touches
        routing; a stale or corrupt file degrades to the built-in default."""
        default = Thread("abcdef01", "p", Role.CHAT, P.CLAUDE)
        effort_only = Thread("abcdef02", "p", Role.CHAT, P.CLAUDE, effort="max")
        pinned = Thread("abcdef03", "p", Role.CHAT, P.CLAUDE, model="claude-opus-5-5")
        self.assertEqual((tm.default_source(P.CLAUDE), tm.default_source(P.CODEX)), ("built-in", "routing"))
        self.assertEqual(tm.set_provider_default("claude", "claude-sonnet-5-5"), ("claude-sonnet-5-5", None))
        self.assertEqual(tm.default_choice(P.CLAUDE), ("claude-sonnet-5-5", "high"))
        self.assertEqual(tm.default_source(P.CLAUDE), "hud")
        self.assertEqual(tm.effective(default), ("claude-sonnet-5-5", "high"))
        self.assertEqual(tm.effective(effort_only), ("claude-sonnet-5-5", "max"))
        self.assertEqual(tm.effective(pinned), ("claude-opus-5-5", "high"), "a pin ignores the default")
        tm.set_provider_default(P.CLAUDE, " claude-sonnet-5-5 ", "Medium")
        self.assertEqual(tm.effective(default), ("claude-sonnet-5-5", "medium"))
        # A default with no reasoning control: an effort-only thread drops it,
        # and an effort chosen now is checked against that default.
        tm.set_provider_default("claude", "claude-haiku-4-5")
        self.assertEqual(tm.effective(effort_only), ("claude-haiku-4-5", None))
        self.assertEqual(effort_only.effort, "max", "the stored choice is not rewritten")
        with self.assertRaises(tm.ChoiceRefused):
            tm.check(P.CLAUDE, None, "low")
        # Codex: the HUD default wins over routing for chat threads only.
        config.ROUTING_PATH.write_text(json.dumps({"models": {"orchestrator": {"codex": "gpt-5.6-sol/default"}}}))
        routing = config.ROUTING_PATH.read_bytes()
        tm.set_provider_default("codex", "gpt-5.6-terra", "low")
        self.assertEqual(tm.default_choice(P.CODEX), ("gpt-5.6-terra", "low"))
        self.assertEqual(router.model_settings("orchestrator", "codex"), ("gpt-5.6-sol", None))
        self.assertEqual(config.ROUTING_PATH.read_bytes(), routing, "routing.json is never rewritten")
        described = tm.describe()["providers"]
        self.assertEqual((described["codex"]["default"], described["codex"]["default_source"],
                          described["codex"]["hud_default"], described["codex"]["builtin"]),
                         ("gpt-5.6-terra", "hud", {"model": "gpt-5.6-terra", "effort": "low"},
                          {"model": "gpt-5.6-sol", "effort": "high"}))
        # Reset, per provider.
        self.assertIsNone(tm.set_provider_default("codex", ""))
        self.assertEqual((tm.default_choice(P.CODEX), tm.default_source(P.CODEX)),
                         (("gpt-5.6-sol", "high"), "routing"))
        self.assertEqual(tm.default_source(P.CLAUDE), "hud", "resetting Codex leaves Claude alone")
        tm.set_provider_default("claude", None)
        self.assertEqual((tm.default_choice(P.CLAUDE), tm.default_source(P.CLAUDE)),
                         (("claude-opus-5-5", "high"), "built-in"))
        # Refused, never stored: the file is what it was.
        tm.set_provider_default("claude", "claude-sonnet-5-5")
        before = config.PROVIDER_DEFAULTS_PATH.read_bytes()
        for args, words in ((("fast", "openai/gpt-5.6-luna"), "Model picker"),
                            (("nope", "x"), "not a provider"),
                            ((["claude"], "x"), "not a provider"),
                            (("claude", "claude-opus-9"), "not a Claude model"),
                            (("codex", "claude-opus-5-5"), "not a Codex model"),
                            (("codex", "gpt-5.6-sol", "max"), "does not offer 'max'"),
                            (("codex", "gpt-5.6-sol", "turbo"), "not a reasoning effort"),
                            (("claude", "claude-haiku-4-5", "low"), "no reasoning effort"),
                            (("claude", "", "low"), "takes no effort"),
                            (("claude", 7), "must be a string"),
                            (("claude", "claude-opus-5-5", 3), "must be a string or null")):
            with self.assertRaises(tm.ChoiceRefused, msg=args) as caught:
                tm.set_provider_default(*args)
            self.assertIn(words, str(caught.exception), args)
        self.assertEqual(config.PROVIDER_DEFAULTS_PATH.read_bytes(), before)
        # A stored model Jarvis no longer knows is said, not sent.
        config.PROVIDER_DEFAULTS_PATH.write_text(json.dumps({"claude": {"model": "claude-opus-9", "effort": None}}))
        self.assertEqual((tm.default_choice(P.CLAUDE), tm.default_source(P.CLAUDE)),
                         (("claude-opus-5-5", "high"), "built-in"))
        self.assertIn("claude-opus-9 is not a Claude model", tm.describe()["providers"]["claude"]["note"])
        # A corrupt or non-UTF-8 file degrades to the built-in default too.
        for raw in (b"{ not json", b'{"claude": {"model": "\xff"}}', b"[]"):
            config.PROVIDER_DEFAULTS_PATH.write_bytes(raw)
            self.assertEqual(tm.default_choice(P.CLAUDE), ("claude-opus-5-5", "high"), raw)
        # Written atomically: the mode is kept and no temp file is left behind.
        config.PROVIDER_DEFAULTS_PATH.chmod(0o640)
        tm.set_provider_default("claude", "claude-sonnet-5-5")
        self.assertEqual(config.PROVIDER_DEFAULTS_PATH.stat().st_mode & 0o777, 0o640)
        self.assertEqual([p.name for p in config.PROVIDER_DEFAULTS_PATH.parent.iterdir()
                          if p.name.startswith(".provider_defaults-")], [])

    def test_only_owner_chat_threads_have_a_choice(self):
        self.assertTrue(tm.is_chat(Thread("abcdef01", "p", Role.CHAT, P.FAST)))
        self.assertFalse(tm.is_chat(Thread("abcdef01", "p", Role.CHAT, P.FAST, task_id="12345678")))
        self.assertFalse(tm.is_chat(Thread("abcdef01", "p", Role.IMPLEMENTER, P.CODEX)))


class ProviderLimits(unittest.TestCase):
    """`thread_model.refusal` is the daemon's pre-check and the HUD's greying;
    the providers refuse for themselves at start. The table must say what the
    providers do, or a thread is refused that would have run, or created and
    then orphaned by a provider that will not."""

    def provider_refuses(self, provider, brief):
        from types import SimpleNamespace
        from jarvis.v2.provider import BriefRefused
        from jarvis.v2.providers import claude, codex_config, fastpath
        try:
            if provider == P.CODEX:
                codex_config.validate(brief)
            elif provider == P.CLAUDE:
                try:
                    claude.ClaudeProvider()._options(brief, SimpleNamespace(), resume=False, session_id=None)
                except BriefRefused:
                    raise
                except Exception:
                    pass    # past the refusals: it only needed a real session
            else:
                fastpath.FastPathProvider._toolset(object.__new__(fastpath.FastPathProvider), brief)
        except BriefRefused:
            return True
        return False

    def test_the_table_matches_each_providers_own_refusal(self):
        from jarvis.v2.model import PermissionProfile
        from jarvis.v2.provider import Brief
        with tempfile.TemporaryDirectory() as cwd:
            for provider in P:
                for profile in PermissionProfile:
                    for always_ask in ([], ["make deploy"]):
                        with self.subTest(provider=provider.value, profile=profile.value, always_ask=always_ask):
                            brief = Brief(Role.CHAT, cwd, profile=profile, always_ask=always_ask)
                            why = tm.refusal(provider, profile, always_ask)
                            self.assertEqual(why is not None, self.provider_refuses(provider, brief), why)


if __name__ == "__main__":
    unittest.main(verbosity=2)
