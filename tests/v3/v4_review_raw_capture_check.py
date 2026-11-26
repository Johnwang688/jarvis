"""Raw-only samples must not reach the approval transcriber."""

import unittest

import voice_approving_check as fixtures
from jarvis.v3.voice.session import ECHO_TAIL_S
from voice_session_check import CANCELLED, QUIET, RAW


class RawCaptureCheck(fixtures.VoiceApprovalFlow):
    def test_keyed_approval_decode_excludes_raw_only_signal(self):
        card = self.request()
        self.played(card)
        self.now += ECHO_TAIL_S + 0.01
        self.asr.lines.append("")
        self.session.key(True)
        self.frames(8, RAW, QUIET)
        self.frames(8, CANCELLED, CANCELLED)
        self.session.key(False)

        self.asr.calls.wait(lambda rows: len(rows) == 2)
        peak, _ = self.asr.calls.rows[1]
        self.assertAlmostEqual(peak, float(abs(CANCELLED).max()))


if __name__ == "__main__":
    unittest.main()
