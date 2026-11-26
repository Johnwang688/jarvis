"""A malformed saved wait must not invalidate the other voice settings."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from jarvis import config
from jarvis.v3.voice import settings


class SavedVoiceSettingsCheck(unittest.TestCase):
    def test_invalid_saved_wait_preserves_speed_and_follow_up(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.multiple(
            config, V3_KOKORO_SPEED=1.35, V3_FOLLOW_UP_S=8, V3_END_SILENCE_MS=500,
        ))
        path = root / "voice" / "settings.json"
        path.parent.mkdir()
        legacy = '{"speed":1.6,"follow_up_s":13}'
        path.write_text(legacy, encoding="utf-8")
        expected = settings.load(root)
        self.assertEqual((expected.speed, expected.follow_up_s, expected.end_silence_ms), (1.6, 13, 500))

        for invalid in ("299", "1001", "400.5", '"400"', "true", "NaN"):
            with self.subTest(invalid=invalid):
                path.write_text(legacy[:-1] + ',"end_silence_ms":' + invalid + "}", encoding="utf-8")
                self.assertEqual(settings.load(root), expected)


if __name__ == "__main__":
    unittest.main()
