"""Recognition, audio trimming and lazy-load regressions with fake engines."""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unicodedata
import unittest
from unittest.mock import Mock, patch

import numpy as np

from jarvis.v3.voice import engines, hearing


def tone(sample_count, amplitude):
    return (amplitude * np.sin(np.arange(sample_count) * np.pi / 8)).astype(np.float32)


class EngineReview(unittest.TestCase):
    def whisper_returning(self, text):
        backend = Mock()
        backend.transcribe.return_value = ([SimpleNamespace(text=text)], None)
        engine = engines.WhisperGpu()
        self.enterContext(patch.object(engine, "_load", return_value=backend))
        self.addCleanup(engine.close)
        return engine, backend

    def test_normal_level_short_answers_survive(self):
        for text in ("yes", "no", "stop"):
            with self.subTest(text=text):
                engine, _ = self.whisper_returning(text)
                self.assertEqual(engine.transcribe(tone(6400, 0.2), names=()), text)

    def test_kokoro_preserves_quiet_audio_at_the_edge_of_louder_speech(self):
        rate = 24_000
        quiet_attack = tone(rate // 8, 0.006)
        audible_body = tone(rate // 4, 0.2)
        signal = np.concatenate((quiet_attack, audible_body))
        pad = np.zeros(rate // 5, dtype=np.float32)
        backend = SimpleNamespace(create=lambda *args, **kwargs: (np.concatenate((pad, signal, pad)), rate))
        voice = engines.KokoroVoice(device="cpu")
        with patch.object(voice, "_load", return_value=backend):
            pcm, _ = voice.synthesize("Example sentence.")
        first, last = np.flatnonzero(pcm)[[0, -1]]
        expected = np.rint(signal * 32767).astype(np.int16)
        first_expected, last_expected = np.flatnonzero(expected)[[0, -1]]
        np.testing.assert_array_equal(pcm[first:last + 1], expected[first_expected:last_expected + 1])

    def test_hearing_construction_does_not_initialize_silero(self):
        detector = Mock()
        with patch.object(hearing, "SileroVad", detector):
            stream = hearing.Hearing(Mock(), on_event=Mock())
            self.addCleanup(stream.close)
            detector.assert_not_called()

    def test_optional_gpu_group_preserves_existing_default_sdk_dependency(self):
        try:
            import tomllib
        except ImportError:
            self.skipTest("TOML parser requires Python 3.11")
        project = Path(__file__).resolve().parents[2] / "pyproject.toml"
        dependencies = tomllib.loads(project.read_text())["project"]["dependencies"]
        self.assertTrue(any(item.startswith("claude-agent-sdk") for item in dependencies), dependencies)


if __name__ == "__main__":
    unittest.main()
