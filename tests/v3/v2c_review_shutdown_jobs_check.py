"""Exercise shutdown and scheduled-job parity without a listener or subprocess."""

from pathlib import Path
import tempfile
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from jarvis.v3 import claude_jobs, daemon


class ShutdownAndJobsReview(unittest.TestCase):
    def test_daemon_finishes_shutdown_even_if_voice_close_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            for failing_component in ("turns", "brain"):
                with self.subTest(failing_component=failing_component):
                    instance = daemon.Daemon(port=0, data_dir=Path(directory), jobs=(), phone_host=None)
                    instance._server = server = Mock()
                    with (patch.object(instance._voice_turns, "close") as turns_close,
                          patch.object(instance._voice_brain, "close") as brain_close,
                          patch.object(instance, "_stop_jobs") as stop_jobs,
                          patch.object(instance._chat, "close") as chat_close):
                        failing_close = turns_close if failing_component == "turns" else brain_close
                        failing_close.side_effect = RuntimeError("Fixture close failure")
                        try:
                            instance.stop()
                        except RuntimeError:
                            pass
                        stop_jobs.assert_called_once()
                        brain_close.assert_called_once()
                        chat_close.assert_called_once()
                        server.shutdown.assert_called_once()
                        server.server_close.assert_called_once()

    def test_version_probe_oserror_preserves_job_outcome_when_cancelled_or_late(self):
        with tempfile.TemporaryDirectory() as directory:
            for condition in ("cancelled", "past deadline"):
                with self.subTest(condition=condition):
                    cancel = Event()
                    tick = 100.0

                    def failed_probe(argv, **kwargs):
                        nonlocal tick
                        self.assertEqual(argv, ["/example/bin/claude", "--version"])
                        if condition == "cancelled":
                            cancel.set()
                        else:
                            tick += 2
                        raise OSError("Fixture version probe could not launch")

                    with (patch.object(claude_jobs, "time", SimpleNamespace(monotonic=lambda: tick)),
                          patch.object(claude_jobs, "_capture", side_effect=failed_probe) as capture):
                        outcome = claude_jobs.run_job("fixture", "Fixture prompt", model="haiku", cwd=directory,
                                                      timeout=1, cancel=cancel, cli="/example/bin/claude")
                    capture.assert_called_once()
                    self.assertEqual((outcome.outcome, outcome.error),
                                     ("error", f"Claude CLI version {claude_jobs.MIN_CLI} or newer is required."))


if __name__ == "__main__":
    unittest.main()
