"""A replacement helper still gets its own warm-up and actionable error."""

from threading import Event, current_thread
import unittest
from unittest.mock import patch

from jarvis.v3.voice.brain import VoiceSessionError
from jarvis.v3.voice.session import VoiceSession
from jarvis.v3.voice.turns import VoiceTurns
from voice_session_check import Brain, Detector, Rows, Transcriber


class ReconnectCheck(unittest.TestCase):
    def test_replacement_helper_gets_setup_failure_after_previous_retry_is_stopped(self):
        brain, errors = Brain(), Rows()
        retry_waiting = Event()
        attempts = iter((VoiceSessionError("connect_timeout"), VoiceSessionError("not_signed_in")))

        def warm():
            raise next(attempts)

        brain.warm = warm
        turns = VoiceTurns(brain, clock=lambda: 100, on_event=lambda *_args, **_fields: None)
        self.addCleanup(turns.close)
        session = VoiceSession(turns, transcriber=Transcriber(), voices=(None, None),
                               names=tuple, quiet=lambda: False, clock=lambda: 100,
                               detector=Detector())
        self.addCleanup(session.close)
        original_wait = session._changed.wait

        def wait(timeout=None):
            if current_thread().name == "voice-warm-brain":
                retry_waiting.set()
                timeout = None
            return original_wait(timeout)

        def on_event(kind, **fields):
            if kind == "error":
                errors.add(fields["reason"])

        self.enterContext(patch.object(session._changed, "wait", side_effect=wait))
        session.attach(on_event, None)
        session.start()
        self.assertTrue(retry_waiting.wait(3), "the first helper never reached backoff")
        session.stop()
        session.detach()
        session.attach(on_event, None)
        session.start()
        errors.wait(bool)
        self.assertEqual(errors.rows, ["brain_not_signed_in"])
        self.assertEqual(session.status()["problem"]["code"], "brain_not_signed_in")


if __name__ == "__main__":
    unittest.main()
