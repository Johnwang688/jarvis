"""Check sentence completion through the four-argument session callback."""

from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest

import numpy as np

from jarvis.v3.voice.link import VoiceLink
from jarvis.v3.voice import wire
from voice_link_check import Helper, KEY_TEXT, Session


class AudioCompletionCheck(unittest.TestCase):
    def test_first_chunk_of_a_split_sentence_is_not_final(self):
        with ExitStack() as stack:
            key = Path(stack.enter_context(tempfile.TemporaryDirectory())) / "fake-pairing"
            key.touch(mode=0o600)
            key.write_bytes(KEY_TEXT)
            session = Session()
            link = VoiceLink(session, port=0, key_path=key)
            stack.callback(link.close)
            self.assertTrue(link.start())
            peer = Helper(stack, link.address).authenticate()
            peer.send(wire.PING, b"ready123")
            self.assertEqual(peer.read(), (wire.PONG, b"ready123"))

            first = np.zeros(3200, dtype="<i2")
            tail = np.ones(100, dtype="<i2")
            link.on_event("say", id="fake-sentence", text="A split sentence.")
            link.on_audio("fake-sentence", first, 16000, True)
            link.on_audio("fake-sentence", tail, 16000, False)

            kind, say = peer.read()
            self.assertEqual(kind, wire.EVENT)
            self.assertEqual(say["id"], "fake-sentence")
            kind, (index, flags, rate, samples) = peer.read()
            self.assertEqual((kind, index, flags, rate), (wire.AUDIO_OUT, say["n"], wire.FIRST, 16000))
            self.assertEqual(samples, first.tobytes())


if __name__ == "__main__": unittest.main()
