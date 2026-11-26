from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from jarvis import rules
from voice_helper_check import FAKE_KEY
from windows import v3voice_client as client


class LinkLifecycle(unittest.TestCase):
    def test_backoff_increases_when_authenticated_sessions_end_immediately(self):
        key_path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "voice-link.key"
        key_path.write_bytes(FAKE_KEY)
        connected = Mock()
        transport = client.Client("127.0.0.1", 12345, key_path, canceller=False,
                                  on_connect=connected, on_disconnect=Mock(), on_event=Mock(),
                                  on_audio=Mock(), on_state=Mock())
        delays = []

        def wait(delay):
            delays.append(delay)
            return len(delays) == 4

        with patch.object(client.socket, "create_connection"), \
                patch.object(transport, "_handshake", return_value=bytes(32)), \
                patch.object(client, "_send"), \
                patch.object(transport, "_read", side_effect=EOFError), \
                patch.object(transport._stop, "wait", side_effect=wait):
            transport.run()

        self.assertEqual(connected.call_count, 4)
        self.assertEqual(delays, [0.5, 1, 2, 4])

    def test_owner_command_verdicts_match_unknown_subcommands(self):
        for prefix in ("jarvis", "python -m jarvis", "python -m jarvis.v3.cli"):
            baseline = rules.decide(f"{prefix} example-unknown")
            for action in ("start", "setup", "stop"):
                with self.subTest(prefix=prefix, action=action):
                    verdict = rules.decide(f"{prefix} voice {action}")
                    self.assertEqual(verdict, baseline)
                    self.assertIn(verdict.decision, (rules.ASK, rules.DENY))


if __name__ == "__main__":
    unittest.main()
