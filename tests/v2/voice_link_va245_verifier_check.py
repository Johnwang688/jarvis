"""PR #394 counterexamples only; each test fails on the reviewed HEAD.

Hermetic: the existing harness supplies temporary HOME/config, fake providers,
and ephemeral loopback listeners. No real models, TTS, devices or network.
Run with PYTHONPATH=. and the shared venv, inside the mandated systemd scope.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


impl = load("va245_verifier_impl", "voice_link_va245_impl_check.py")
from jarvis.v2 import voice_link
from jarvis.v2.daemon import Daemon
from jarvis.v2.model import ProviderName as PN, Role
from jarvis.v2.provider import UserMessage
from jarvis.v2.stores import Stores
from windows import voicewire as wire


class References(impl.Base):
    def test_crash_after_create_before_title_does_not_duplicate_chat(self):
        self.start()

        class Crash(BaseException):
            pass

        # open_thread has committed its metadata, but the next persistent write
        # never happens. A process death has exactly this durable state.
        with patch.object(self.d.stores.threads, "set_title", side_effect=Crash):
            with self.assertRaises(Crash):
                self.link._voice_chat._thread()
        created = self.d.stores.threads.list()
        self.assertEqual(len(created), 1)
        self.d.stop()
        other = Daemon(Stores(self.d.stores.root),
                       {p: self.fast if p == PN.FAST else impl.existing.rac.Fake(p) for p in PN},
                       lambda *_: lambda *_: impl.existing.rac.Decision.DENY,
                       0, remote_host=None)
        self.addCleanup(other.stop)
        other.start()
        recovered = other.voice_link._voice_chat._thread()
        self.assertEqual(len(other.stores.threads.list()), 1,
                         "restart created a second chat after create/title crash")
        self.assertEqual(recovered.id, created[0].id,
                         "restart created a second chat after create/title crash")

    def test_reference_to_unrelated_chat_does_not_redirect_voice_question(self):
        self.start()
        voice = self.link._voice_chat._thread()
        project = self.d.stores.projects.inbox()
        unrelated = self.d.open_thread(project.id, "chat", "fast", {})
        self.d.stores.threads.set_title(unrelated.id, "Private unrelated conversation", "owner")
        (self.d.stores.root / "voice-chat.json").write_text(json.dumps(unrelated.id))
        self.client().ask("voice question belongs only in Voice")
        received = [r for r in self.d.stores.threads.read_log(unrelated.id) if r["kind"] == "user"]
        self.assertEqual(len(received), 0, "stored id redirected voice speech into an unrelated chat")
        self.assertEqual(json.loads((self.d.stores.root / "voice-chat.json").read_text()), voice.id)


class AnswerFraming(impl.Base):
    def reject_oversize(self, *, delta):
        self.start()
        client = self.client()
        # JSON's ASCII escaping makes the actual line exceed MAX_LINE. This
        # exercises serialized bytes, not a guessed character-count limit.
        answer = "\u2603" * (wire.MAX_LINE // 6 + 1)

        def asker(_text, on_delta):
            if delta:
                on_delta(answer)
                return "done"
            return answer

        self.link.asker = asker
        with self.assertRaises(wire.LinkError) as caught:
            client.ask("a very long answer")
        self.assertEqual(str(caught.exception), voice_link.ASK_FAILED,
                         "oversize answer must produce the fixed error within the line cap")
        client.ping()  # The rejected response must leave framing synchronized.

    def test_oversize_final_answer_is_a_fixed_error(self):
        self.reject_oversize(delta=False)

    def test_oversize_delta_is_a_fixed_error(self):
        self.reject_oversize(delta=True)


class Authority(impl.existing.rac.Base):
    def test_voice_does_not_activate_claude_bypass(self):
        # The via is accepted by daemon.VIAS, including on an owner's Claude
        # chat. Use the real provider gate with the existing fake SDK client.
        cp = load("va245_verifier_claude", "claude_provider_check.py")
        provider = cp.claude.ClaudeProvider()
        with cp._temp_config(), cp.fake([cp.Hook("Bash", {"command": "ls"}), cp.result_message()]):
            handle = provider.start(cp.thread(), cp.brief(role=Role.CHAT), cp.allow)
            try:
                provider.set_bypass(handle, True)
                list(provider.send(handle, UserMessage("run ls", origin="owner", via="voice",
                                                       desk=False, spoken=True)))
                result = cp.FakeClient.instances[0].hook_results[0]
                decision = (result.get("hookSpecificOutput") or {}).get("permissionDecision")
                self.assertNotEqual(decision, "allow", "voice speech activated the owner's Bypass switch")
            finally:
                provider.close(handle)


if __name__ == "__main__":
    unittest.main(verbosity=2)
