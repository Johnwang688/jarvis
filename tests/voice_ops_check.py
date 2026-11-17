"""VA-7 ops acceptance (orchestrator-owned): voice logs and `jarvis voice-link start|stop|status|logs`.

Owner asks (2026-11-17): log failures somewhere traceable, start the Windows
agent from the HUD terminal instead of pasting a long cmd line. Agents may run
the verbs with an approval card (every `jarvis` CLI run already asks).
Fakes only: no Windows, no processes, no real bridge folder.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="jarvis-va7-home-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows import voicelog  # noqa: E402
from jarvis import voice_agent_ctl as ctl  # noqa: E402

SENTINEL = "purple elephant 4417"


class Log(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="va7-log-")) / "logs"

    def tearDown(self):
        logger = logging.getLogger("jarvis.voice")
        for h in list(logger.handlers):
            h.close()
            logger.removeHandler(h)

    def test_setup_is_idempotent_rotating_and_in_the_folder(self):
        a = voicelog.setup(self.dir)
        b = voicelog.setup(self.dir)
        self.assertIs(a, b)
        self.assertEqual(a.name, "jarvis.voice")
        handlers = [h for h in a.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
        self.assertEqual(len(handlers), 1)
        self.assertEqual((handlers[0].maxBytes, handlers[0].backupCount), (1_000_000, 3))
        a.warning("hello")
        handlers[0].flush()
        self.assertIn("hello", (self.dir / voicelog.LOG_NAME).read_text(encoding="utf-8"))

    def test_uncaught_thread_errors_reach_the_log(self):
        logger = voicelog.setup(self.dir)

        def boom():
            raise ValueError("kaboom-xyz")

        t = threading.Thread(target=boom, name="voice-test")
        t.start()
        t.join()
        for h in logger.handlers:
            h.flush()
        text = (self.dir / voicelog.LOG_NAME).read_text(encoding="utf-8")
        self.assertIn("kaboom-xyz", text)
        self.assertIn("Traceback", text)


class Bridge:
    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="va7-bridge-"))
        (self.dir / "venv" / "Scripts").mkdir(parents=True)
        (self.dir / "venv" / "Scripts" / "pythonw.exe").write_text("")
        (self.dir / "voice-link.key").write_text("k")

    def pid(self, value):
        (self.dir / ctl.PID_FILE).write_text(str(value))


AGENT_LINE = r'"C:\b\venv\Scripts\pythonw.exe" "\\wsl.localhost\Ubuntu\x\windows\voice_agent.py" 127.0.0.1 8409 "C:\b\voice-link.key"'


class Running(unittest.TestCase):
    def test_no_pid_file_stale_pid_and_foreign_process_are_not_running(self):
        b = Bridge()
        self.assertIsNone(ctl.running(b.dir, query=Mock(return_value=AGENT_LINE)))
        b.pid("not a number")
        self.assertIsNone(ctl.running(b.dir, query=Mock(return_value=AGENT_LINE)))
        b.pid(4242)
        self.assertIsNone(ctl.running(b.dir, query=Mock(return_value=None)))
        self.assertIsNone(ctl.running(b.dir, query=Mock(return_value='"C:\\x\\notepad.exe"')))
        self.assertEqual(ctl.running(b.dir, query=Mock(return_value=AGENT_LINE)), 4242)


class Start(unittest.TestCase):
    def test_already_running_starts_nothing(self):
        b = Bridge()
        b.pid(7)
        popen = Mock()
        self.assertEqual(ctl.start(b.dir, popen=popen, query=Mock(return_value=AGENT_LINE),
                                   wait=0), 0)
        popen.assert_not_called()

    def test_unpaired_starts_nothing(self):
        b = Bridge()
        (b.dir / "voice-link.key").unlink()
        popen = Mock()
        self.assertEqual(ctl.start(b.dir, popen=popen, query=Mock(return_value=None), wait=0), 1)
        popen.assert_not_called()

    def test_starts_the_windowless_agent_detached(self):
        b = Bridge()
        popen = Mock()
        ctl.start(b.dir, popen=popen, query=Mock(return_value=None), wait=0)
        popen.assert_called_once()
        argv = popen.call_args.args[0]
        joined = " ".join(argv)
        self.assertEqual(Path(argv[0]).name.lower(), "cmd.exe")
        self.assertIn("start", [a.lower() for a in argv])
        self.assertIn("pythonw.exe", joined)
        self.assertIn("voice_agent.py", joined)
        self.assertIn("voice-link.key", joined)
        self.assertNotIn("python.exe\"", joined)


class Stop(unittest.TestCase):
    def test_not_running_is_a_no_op(self):
        b = Bridge()
        kill = Mock()
        self.assertEqual(ctl.stop(b.dir, query=Mock(return_value=None), kill=kill, wait=0), 0)
        kill.assert_not_called()
        self.assertFalse((b.dir / ctl.STOP_FILE).exists())

    def test_asks_the_agent_to_stop_first(self):
        b = Bridge()
        b.pid(9)
        answers = iter([AGENT_LINE, None, None, None])
        kill = Mock()
        rc = ctl.stop(b.dir, query=lambda pid: next(answers), kill=kill, wait=1.0,
                      sleep=lambda s: None)
        self.assertEqual(rc, 0)
        kill.assert_not_called()

    def test_force_kills_only_a_verified_agent_that_did_not_stop(self):
        b = Bridge()
        b.pid(11)
        kill = Mock()
        rc = ctl.stop(b.dir, query=Mock(return_value=AGENT_LINE), kill=kill, wait=0.01,
                      sleep=lambda s: None)
        kill.assert_called_once_with(11)
        self.assertEqual(rc, 0)


class Logs(unittest.TestCase):
    def test_prints_the_agent_log_tail(self):
        b = Bridge()
        (b.dir / "logs").mkdir()
        (b.dir / "logs" / "voice-agent.log").write_text(
            "\n".join(f"line {i}" for i in range(100)) + "\n", encoding="utf-8")
        out = []
        self.assertEqual(ctl.logs(b.dir, n=5, echo=out.append, journal=lambda n: []), 0)
        text = "\n".join(out)
        self.assertIn("line 99", text)
        self.assertNotIn("line 90", text)


if __name__ == "__main__":
    unittest.main()
