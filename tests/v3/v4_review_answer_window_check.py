"""The playback deadline applies to speech, independent of decode latency."""

from threading import Event
import unittest
from unittest.mock import patch

import voice_approving_check as fixtures
from voice_session_check import QUIET


class AnswerWindowCheck(fixtures.VoiceApprovalFlow):
    def test_denial_spoken_in_window_survives_late_decode(self):
        card = self.request()
        self.played(card)
        release, processed = Event(), Event()
        handle = self.session._handle_hearing

        def observe(kind, capture, fields):
            handle(kind, capture, fields)
            if kind == "speech_end" and capture.context.asking == card["id"]:
                processed.set()

        self.enterContext(patch.object(self.session, "_handle_hearing", observe))
        self.addCleanup(release.set)
        self.asr.hold = release
        self.now += 14
        self.utter("no")
        self.asr.calls.wait(lambda rows: len(rows) == 2)
        self.frames(25, QUIET, QUIET)
        self.now += 1
        release.set()

        self.assertTrue(processed.wait(3))
        self.assertEqual(self.rows()[-1]["state"], "denied")


if __name__ == "__main__":
    unittest.main()
