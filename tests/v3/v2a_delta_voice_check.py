"""Voice regression cases for the V2a fix round."""

from threading import Event
import unittest
from unittest.mock import patch

from chat_check import reply
from jarvis.v3.approvals import ApprovalBroker
import voice_turns_check as fixtures


class VoiceDeltaCheck(fixtures.VoiceTurnsCheck):
    def test_delta_success_claim_escapes_before_multi_sentence_tool_step_finishes(self):
        release = self.hold()
        claim = "I added the sample vault entry."
        text = claim + " The entry is ready to use."

        def propose(on_delta, **kwargs):
            on_delta(text)
            self.wait_release(release)
            return reply(text, fixtures.VAULT_ADD)

        broker = ApprovalBroker()
        self.start(propose, reply("The write was declined."), broker=broker)
        self.voice.heard("Add a sample vault entry.")
        self.wait_chat("delta")
        before_action = self.texts()
        self.assertEqual(broker.pending(), [])
        release.set()
        (ask,) = self.wait_for("asking")
        broker.answer(ask["id"], ask["digest"], "deny")
        self.wait_chat("reply")
        self.assertEqual((before_action, self.texts()), ([], ["The write was declined."]))

    def test_delta_wake_name_alone_is_ignored(self):
        self.start(reply("First spurious reply."), reply("Second spurious reply."))
        for text in ("Jarvis!", "Hey, Jarvis?"):
            self.voice.heard(text)
        self.assertEqual([row["kind"] for row in self.chat.events(0)["events"]], [])
        self.assertEqual(self.of_kind("ignored"), [{"reason": "wake-only"}] * 2)

    def test_delta_punctuation_can_separate_stop_words(self):
        self.start(*(reply("A spurious reply.") for _ in range(3)))
        for text in ("STOP—TALKING!", "Jarvis:STOP!", "Hey,Jarvis:QUIET!"):
            with self.subTest(text=text):
                before = len(self.of_kind("speech_stop"))
                self.voice.heard(text)
                self.assertEqual(len(self.of_kind("speech_stop")), before + 1)

    def test_delta_stopped_turn_does_not_get_tool_fallback(self):
        release, entered = self.hold(), Event()

        def finishing(**kwargs):
            entered.set()
            self.wait_release(release)
            return reply("The read finished.")

        self.start(reply(None, ("get_datetime", {})), finishing)
        self.voice.heard("Check the time.")
        self.assertTrue(entered.wait(5))
        self.chat.stop()
        release.set()
        self.wait_chat("stopped")
        self.assertEqual(self.texts(), [])
        self.assertEqual(self.voice.trace()[0]["code_built_sentences"], [])

    def test_delta_failed_turn_gets_error_sentence_after_successful_write(self):
        def fail(**kwargs):
            raise RuntimeError("Fixture completion failed.")

        self.start(reply(None, ("brief_post", {"kind": "news", "ask": "A fixture brief"})), fail)
        self.voice.heard("Post a brief.")
        self.wait_chat("error")
        self.assertEqual(self.voice.trace()[0]["tools"], [{"name": "brief_post", "outcome": "ok"}])
        self.assertEqual(self.texts(), ["That was done, but something went wrong afterwards."])


if __name__ == "__main__":
    unittest.main()
