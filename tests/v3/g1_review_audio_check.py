from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from jarvis import config
from jarvis.v3.voice import gpu_voice
from jarvis.v3.voice.session import VoiceSession
from jarvis.v3.voice.turns import VoiceTurns
from voice_link_check import until
from voice_session_check import Brain, RATE, Transcriber, Voice


WORKER = r"""
import json
from pathlib import Path
import struct
import sys
import time

root = Path(__file__).parent
out = sys.stdout.buffer

def reply(request_id, samples, rate=24000):
    header = {"id": request_id, "ok": True, "rate": rate, "samples": len(samples)}
    out.write(json.dumps(header).encode() + b"\n")
    out.write(struct.pack(f"<{len(samples)}f", *samples))
    out.flush()

out.write(b'{"ready":true}\n')
out.flush()
for line in sys.stdin.buffer:
    request = json.loads(line)
    request_id, text = request["id"], request["text"]
    if text == "Empty result.":
        reply(request_id, [])
    elif text == "Silent result.":
        reply(request_id, [0.0] * 600)
    elif text == "Invalid rate.":
        reply(request_id, [0.25] * 200, rate=5)
    elif text == "Out of range.":
        reply(request_id, [12.0] * 600)
    elif text == "Second reply.":
        continue
    else:
        reply(request_id, [0.25] * 600)
    if text == "First reply.":
        while not (root / "release").exists():
            time.sleep(0.005)
        reply(request_id + 1, [0.25] * 600)
        (root / "buffered").touch()
"""


class AudioFallbackCheck(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        python = self.root / "fixture" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        (python.parent.parent / gpu_voice._INSTALLED).touch()
        script = self.root / "worker.py"
        script.write_text(WORKER)
        self.enterContext(patch.object(config, "V3_VOICE_GPU_PYTHON", python))
        self.enterContext(patch.object(gpu_voice, "_WORKER", script))
        self.best = gpu_voice.KokoroWorkerVoice(data_dir=self.root)
        self.addCleanup(self.best.close)
        self.cpu = Voice("fixture-cpu")
        turns = VoiceTurns(Brain(), on_event=lambda *_args, **_kwargs: None)
        self.addCleanup(turns.close)
        self.session = VoiceSession(
            turns, transcriber=Transcriber(), voices=(self.cpu, self.best), names=tuple,
        )
        self.addCleanup(self.session.close)
        self.session._warm_best()
        self.assertTrue(self.session.status()["gpu_warm"])

    def assert_falls_back(self, text):
        pcm, rate = self.session._synthesize(text)
        self.assertEqual(self.cpu.calls.rows, [text])
        self.assertGreater(len(pcm), 0)
        self.assertEqual(rate, RATE)
        self.assertFalse(self.session.status()["gpu_warm"])

    def test_empty_worker_reply_falls_back_for_same_sentence(self):
        self.assert_falls_back("Empty result.")

    def test_all_zero_worker_reply_falls_back_for_same_sentence(self):
        self.assert_falls_back("Silent result.")

    def test_implausible_sample_rate_falls_back(self):
        self.assert_falls_back("Invalid rate.")

    def test_out_of_range_pcm_falls_back_instead_of_saturating(self):
        self.assert_falls_back("Out of range.")

    def test_unsolicited_frame_queued_between_requests_is_discarded(self):
        self.session._synthesize("First reply.")
        (self.root / "release").touch()
        until(lambda: (self.root / "buffered").exists())
        self.assert_falls_back("Second reply.")


if __name__ == "__main__":
    unittest.main()
