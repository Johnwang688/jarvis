import tempfile
from pathlib import Path
from threading import Event
import unittest
from unittest.mock import patch

from chat_check import Model, reply
from voice_turns_check import VAULT_ADD
from jarvis import config, llm
from jarvis.v3.approvals import ApprovalBroker
from jarvis.v3.chat import ChatService
from jarvis.v3.voice.sentences import Sentences
from jarvis.v3.voice.turns import VoiceTurns


class VoiceReview(unittest.TestCase):
    def start(self, *steps, approval=False, on_voice=None):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.multiple(
            config, V3_DIR=root, V3_POLICY_FILE=root / "no-policy.json",
            GOOGLE_TOKEN_PATH=root / "no-google-token.json",
        ))
        live = self.enterContext(patch.object(llm, "chat", side_effect=AssertionError("live model")))
        self.addCleanup(live.assert_not_called)
        self.broker = ApprovalBroker() if approval else None
        if self.broker is not None:
            self.addCleanup(self.broker.shutdown)
        self.model = Model(*steps)
        self.chat = ChatService(root / "chats", completion=self.model, approvals=self.broker)
        self.addCleanup(self.chat.close)
        self.voice_events = []
        self.reply_ready = Event()
        self.ask_ready = Event()

        def on_event(kind, **fields):
            self.voice_events.append((kind, fields))
            if on_voice is not None:
                on_voice(kind, fields)
            if kind == "asking":
                self.ask_ready.set()

        subscribe = self.chat.subscribe

        def subscribe_voice(callback):
            def observe(event):
                callback(event)
                if event["kind"] == "reply":
                    self.reply_ready.set()
            return subscribe(observe)

        # Wait for voice's reply delivery rather than for an independent
        # subscriber, which may process the same event first.
        with patch.object(self.chat, "subscribe", side_effect=subscribe_voice):
            self.voice = VoiceTurns(self.chat, on_event=on_event)
        self.addCleanup(self.voice.close)

    def test_success_claim_is_not_spoken_before_approval(self):
        claim = "I added the sample vault entry."

        def propose(on_delta, **kwargs):
            on_delta(claim)
            return reply(claim, VAULT_ADD)

        self.start(propose, reply("The write was declined."), approval=True)
        self.voice.heard("Add a sample vault entry.")
        self.assertTrue(self.ask_ready.wait(5))
        (card,) = self.broker.pending()
        self.assertEqual(card["tool"], "vault_add")
        self.assertEqual([fields["text"] for kind, fields in self.voice_events if kind == "say"], [])

    def test_answered_card_is_not_announced_during_spoken_steer(self):
        pending = Event()
        answered = []

        def on_voice(kind, fields):
            if kind == "state" and fields["state"] == "thinking" and not answered:
                (card,) = self.broker.pending()
                self.broker.answer(card["id"], card["digest"], "deny")
                answered.append(card["id"])

        self.start(reply(None, VAULT_ADD), reply("The write was declined."),
                   approval=True, on_voice=on_voice)
        self.addCleanup(self.chat.subscribe(
            lambda event: pending.set() if event["kind"] == "approval_pending" else None
        ))
        self.chat.send("Add a sample vault entry.")
        self.assertTrue(pending.wait(5))
        self.voice.heard("Was the entry added?")
        self.assertTrue(self.reply_ready.wait(5))
        self.assertEqual(len(answered), 1)
        self.assertEqual(self.broker.pending(), [])
        self.assertEqual([fields["id"] for kind, fields in self.voice_events if kind == "asking"], [])

    def test_stop_with_wake_name_and_punctuation_is_a_command(self):
        self.start(reply("A fixture answer."))
        self.voice.heard("  JARVIS:  STOP   TALKING!  ")
        self.assertEqual(self.chat.events(0)["events"], [])
        self.assertEqual([kind for kind, _ in self.voice_events], ["speech_stop"])

    def test_tool_result_is_reported_when_final_completion_is_empty(self):
        self.start(reply(None, ("get_datetime", {})), reply(None))
        self.voice.heard("Check the time.")
        self.assertTrue(self.reply_ready.wait(5))
        self.assertEqual(self.voice.trace()[0]["tools"], [{"name": "get_datetime", "outcome": "ok"}])
        self.assertTrue(any(kind == "say" for kind, _ in self.voice_events),
                        "The tool ran but voice handed out no spoken report.")

    def test_typed_marker_does_not_get_spoken_identity_instructions(self):
        self.start(reply("A fixture answer."))
        self.chat.send("[spoken] Show the answer as a Markdown table.")
        self.assertTrue(self.reply_ready.wait(5))
        prompt = self.model.seen[0][0]["content"]
        self.assertFalse("were spoken aloud by the owner and transcribed" in prompt
                         or "without markdown or lists" in prompt,
                         "The typed turn received the spoken identity and formatting instructions.")


if __name__ == "__main__":
    unittest.main()
