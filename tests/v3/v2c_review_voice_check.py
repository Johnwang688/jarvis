"""Reproduce voice adapter boundary regressions with the in-process SDK fake."""

import os
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from jarvis import config, llm
from jarvis.v3 import chat, claude_jobs, daemon, t3_client
from jarvis.v3.voice import claude_brain
from voice_brain_check import Factory, Recorder, Work, delta, frame, result, script, stream_text, tool_block


class VoiceReview(unittest.TestCase):
    def setUp(self):
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"HOME": str(home)}, clear=True))
        self.enterContext(patch.object(config, "GOOGLE_TOKEN_PATH", home / "absent-token"))
        self.enterContext(patch.object(claude_jobs, "resolve_native_cli", return_value="/example/bin/claude"))
        self.enterContext(patch.object(claude_jobs, "cli_version_status", return_value="ok"))
        for target in (patch.object(llm, "chat"), patch("subprocess.Popen")):
            blocked = self.enterContext(target)
            blocked.side_effect = AssertionError("Fixture must stay in process")
            self.addCleanup(blocked.assert_not_called)
        self.data = home / "voice"
        self.factory = Factory()
        self.events = Recorder()

    def start(self, **options):
        brain = claude_brain.ClaudeVoiceBrain(self.data, client_factory=self.factory, **options)
        self.addCleanup(brain.close)
        brain.subscribe(self.events)
        return brain

    def send(self, brain, text):
        return brain.send(text, steer=True, input_id=text, fresh_after=600)

    def test_barge_in_completes_while_stopped_read_is_still_blocked(self):
        entered, release = Event(), Event()
        brain = self.start()
        dispatch = brain._dispatch

        def blocked_read(session, turn, name, args):
            entered.set()
            if not release.wait(10):
                raise AssertionError("Fixture was not released")
            return dispatch(session, turn, name, args)

        self.factory.scripts.extend((script(("", [("get_datetime", {})])), script()))
        with (patch.object(brain, "_dispatch", blocked_read),
              patch.object(claude_brain, "INTERRUPT_TIMEOUT", 0.05)):
            try:
                first = self.send(brain, "First utterance")
                self.assertTrue(entered.wait(5))
                second = self.send(brain, "Replacement utterance")
                self.assertTrue(self.factory.clients[0].interrupt_seen.wait(5))
                with self.events.changed:
                    completed = self.events.changed.wait_for(
                        lambda: "reply" in self.events.kinds(second), timeout=2)
                    observed = self.events.kinds(first), self.events.kinds(second)
                self.assertTrue(completed, f"Replacement never ran while the first read was blocked: {observed}")
            finally:
                release.set()

    def test_late_frames_for_an_already_closed_tool_message_do_not_consume_a_second_step(self):
        brain = self.start()
        preamble = "The fixture change has already been made."
        answer = "The fixture read completed."

        async def play(client):
            # All frames for this message arrive after the SDK callback returns.
            await client.call("get_datetime", {})
            yield frame("message_start", message={"id": "tool-step"})
            yield delta(preamble)
            yield tool_block("get_datetime")
            yield frame("message_stop")
            for message in stream_text(answer):
                yield message
            yield result(None)

        self.factory.scripts.append(play)
        with (patch.object(claude_brain, "STEP_END_TIMEOUT", 0.02),
              patch.object(claude_brain, "MAX_STEPS", 3)):
            turn = self.send(brain, "Read the fixture")
            self.assertEqual(self.events.wait("reply", turn=turn)["text"], answer)
        self.assertEqual(self.events.kinds(turn),
                         ["user", "connected", "step_end", "tool_start", "tool_end", "delta", "step_end", "reply"])
        self.assertEqual(self.events.wait("delta", turn=turn)["text"], answer)

    def test_empty_message_does_not_disarm_first_token_timeout(self):
        brain = self.start()

        async def play(client):
            yield frame("message_start")
            yield frame("message_stop")
            await client.interrupted.wait()
            yield result()

        self.factory.scripts.append(play)
        with patch.object(claude_brain, "FIRST_TOKEN_TIMEOUT", 0.05):
            turn = self.send(brain, "Wait for a first token")
            with self.events.changed:
                expired = self.events.changed.wait_for(lambda: "error" in self.events.kinds(turn), timeout=2)
            self.assertTrue(expired, f"An empty step left the turn running: {self.events.kinds(turn)}")
        self.assertEqual(self.events.wait("error", turn=turn)["reason"], "first_token_timeout")

    def test_daemon_voice_respects_typed_chat_without_an_approval_broker(self):
        typed = chat.ChatService(self.data / "chats")
        self.addCleanup(typed.close)
        instance = daemon.Daemon(port=0, data_dir=self.data, jobs=(), phone_host=None, chat=typed)
        self.addCleanup(instance.stop)
        brain = instance._voice_brain
        with patch.object(brain, "_client_factory", self.factory):
            brain.warm()
        allowed = set(self.factory.clients[-1].options.allowed_tools)
        expected = {"mcp__jarvis__" + name for name in chat.available_tools(gated=False)}
        self.assertEqual(allowed, expected)


if __name__ == "__main__":
    unittest.main()
