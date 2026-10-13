"""Hermetic Browser review request-wire checks; no model/network/real state."""
from __future__ import annotations
import atexit
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

_ROOT = Path(tempfile.mkdtemp(prefix="browser-review-wire-"))
atexit.register(shutil.rmtree, _ROOT, True)
os.environ["HOME"] = str(_ROOT)
os.environ["XDG_CONFIG_HOME"] = str(_ROOT / "config")
os.environ["XDG_CACHE_HOME"] = str(_ROOT / "cache")
os.environ["OPENROUTER_API_KEY"] = "sk-dummy-not-a-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from jarvis import config, llm, models
from jarvis.tools import secrets
from jarvis.v2.browser import act_review as review, act_rules as rules

class Wire(unittest.TestCase):
    def setUp(self):
        self.calls = []
        def post(url, **kwargs):
            payload = kwargs["json"]
            self.calls.append(payload)
            body = {"model": payload["model"], "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"verdict": "allow", "category": "ordinary", "reason": "Read the requested article."})}}], "usage": {"cost": 0}}
            return SimpleNamespace(status_code=200, json=lambda: body)
        for guard in (patch.object(llm, "_client", SimpleNamespace(post=post)),
                      patch.object(config, "api_key", lambda: "sk-dummy-not-a-key"),
                      patch.object(config, "CHAT_PROVIDER", ""),
                      patch.object(secrets, "secret_values", lambda *a, **kw: ())):
            guard.start(); self.addCleanup(guard.stop)
        review.clear_cache(); self.addCleanup(review.clear_cache)

    def judged(self, model):
        facts = rules.Facts(action="click", url="https://fixture.invalid/", title="Synthetic", context="An inert article.", target=rules.Element(tag="a", name="Next page"))
        with patch.object(models, "tier", lambda _: model):
            verdict = review.judge(facts, "Read the next article.")
        self.assertEqual(verdict.verdict, "allow")
        self.assertEqual(len(self.calls), 1)
        return self.calls[0]

    def test_haiku_omits_sampling_and_disables_thinking_on_actual_wire(self):
        payload = self.judged("anthropic/claude-haiku-5.5")
        self.assertNotIn("temperature", payload)
        self.assertEqual(payload["reasoning"], {"effort": "none"})
        self.assertEqual(payload["max_tokens"], 150)
        self.assertNotIn("tools", payload)

    def test_other_model_retains_its_request_shape(self):
        payload = self.judged("openai/gpt-5.6-luna")
        self.assertEqual(payload["temperature"], 0.0)
        self.assertNotIn("reasoning", payload)

    def test_mandatory_thinking_model_is_not_disabled(self):
        payload = self.judged("anthropic/claude-sonnet-5.5")
        self.assertNotIn("reasoning", payload)
        self.assertEqual(payload["temperature"], 0.0)

    def test_general_chat_keeps_default_and_explicit_zero(self):
        for kwargs in ({}, {"temperature": 0.0}):
            llm.chat("fixture/model", [{"role": "user", "content": "synthetic"}], stream=False, max_retries=1, **kwargs)
        self.assertEqual([p["temperature"] for p in self.calls], [0.2, 0.0])
        self.assertTrue(all("reasoning" not in p for p in self.calls))

if __name__ == "__main__":
    unittest.main(verbosity=2)
