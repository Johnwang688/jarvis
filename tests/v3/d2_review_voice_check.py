"""Compare D2 with ordinary follow-up and playback under the same fake link."""

import unittest
from unittest.mock import patch

from jarvis.v3.work_watch import Notice
import voice_session_check as voice


class NoticeVoiceReview(unittest.TestCase):
    def fixture(self):
        fixture = voice.VoiceSessionCheck()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with patch("jarvis.v3.voice.session.QuietHours") as quiet:
            quiet.return_value.is_quiet.return_value = False
            fixture.open()
        return fixture

    def test_announcement_follow_up_rejects_audio_the_ordinary_gate_rejects(self):
        ordinary = self.fixture()
        reply = ordinary.begin_reply()
        ordinary.played(reply)
        ordinary.wait_state("listening")
        ordinary.advance(0.31)
        ordinary.utterance("Room conversation", raw=voice.SOFT, cancelled=voice.SOFT)
        ordinary.wait_kind("ignored")

        announced = self.fixture()
        announced.session.announce(Notice("example", "Build fixture", "finished"))
        announced.played(announced.wait_kind("say")["id"])
        announced.wait_state("resting", 2)
        announced.advance(0.31)
        announced.utterance("Room conversation", raw=voice.SOFT, cancelled=voice.SOFT)
        announced.wait_kind("ignored")
        self.assertEqual(announced.of_kind("heard"), [])
        self.assertEqual(ordinary.brain.inputs.rows, ["check the fixture."])
        self.assertEqual(announced.brain.inputs.rows, [])

    def test_announcement_does_not_queue_speech_before_flush_receipt(self):
        fixture = self.fixture()
        sentence = fixture.begin_reply()
        fixture.last_chunks.wait(lambda rows: sentence in rows)
        fixture.session.key(True)
        flush = fixture.wait_kind("flush")
        fixture.session.key(False)
        fixture.wait_state("resting", 2)

        fixture.session.announce(Notice("example", "Build fixture", "finished"))
        fixture.worker_fence()
        fixture.session.playback_stopped(flush["n"])
        self.assertEqual([row["id"] for row in fixture.of_kind("say")], [sentence])
        self.assertIn({"name": "notice"}, fixture.of_kind("earcon"))


if __name__ == "__main__":
    unittest.main()
