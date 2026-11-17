"""VA-7 regression checks. Temporary HOME, fake Win32/Tk/backends, socket pairs only."""
from __future__ import annotations

import ctypes
import logging
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ["HOME"] = tempfile.mkdtemp(prefix="va7-impl-home-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows import voice_agent as agent, voiceflow, voicelog, voicewire as wire
from jarvis import voice_agent_ctl as ctl, voice_link_setup
from jarvis.v2 import voice_link

SENTINEL = "purple elephant 4417"
KEY = bytes(range(32))
AGENT_LINE = 'pythonw.exe C:\\repo\\windows\\voice_agent.py'


class Logs(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bridge = Path(tmp.name)
        self.logger = voicelog.setup(self.bridge / "logs")
        self.addCleanup(self.close_log)

    def close_log(self):
        for handler in list(self.logger.handlers):
            handler.close()
            self.logger.removeHandler(handler)

    def log_text(self):
        for handler in self.logger.handlers:
            handler.flush()
        return (self.bridge / "logs" / voicelog.LOG_NAME).read_text()

    def bare(self):
        a = agent.Agent.__new__(agent.Agent)
        a.bridge = self.bridge
        a.done = threading.Event()
        a.recording = threading.Event()
        a.capture = None
        a.generation = 0
        a.generation_lock = threading.Lock()
        a.player, a.overlay, a.root, a.wake = Mock(), Mock(), Mock(), Mock()
        a.finish_capture = Mock()
        return a

    def test_poll_logs_real_start_capture_exception(self):
        a = self.bare()
        a.user32 = SimpleNamespace(GetAsyncKeyState=lambda key: 0x8000 if key == agent.VK_RCONTROL else 0)

        def fail():
            a.done.set()
            raise OSError("PortAudio -9999 WdmSyncIoctl 0x490")

        a.start_capture = fail
        records = Records()
        self.logger.addHandler(records)
        self.addCleanup(self.logger.removeHandler, records)
        a.poll()
        text = self.log_text()
        self.assertIn("Microphone unavailable", text)
        self.assertIn("Traceback", text)
        self.assertIn("OSError: PortAudio -9999 WdmSyncIoctl 0x490", text)
        poll_errors = [r for r in records.records if r.funcName == "poll" and r.exc_info]
        self.assertEqual(len(poll_errors), 1)
        self.assertIs(poll_errors[0].exc_info[0], OSError)
        a.overlay.set_state.assert_called_with("error", "Microphone unavailable")
        a.finish_capture.assert_called_with(cancel=True)

    def test_stop_file_uses_tk_after_and_shared_shutdown(self):
        a = self.bare()
        a.recording.set()
        callbacks = []
        a.root.after.side_effect = lambda delay, callback: callbacks.append((delay, callback))
        a.root.mainloop.side_effect = lambda: self.drive_stop(a, callbacks)
        listener = Mock(reason="")
        listener.start.return_value = True
        with patch.object(agent.threading, "Thread"), patch.object(agent.voicewake, "WakeListener", return_value=listener):
            a.run()
        self.assertTrue(a.done.is_set())
        self.assertFalse((self.bridge / ctl.STOP_FILE).exists())
        a.root.quit.assert_called_once()
        a.player.stop.assert_called_once()
        listener.stop.assert_called_once()
        a.finish_capture.assert_called_once_with(cancel=True)

    def drive_stop(self, a, callbacks):
        delay, callback = callbacks.pop(0)
        self.assertEqual(delay, 500)
        callback()  # absent stop file reschedules on Tk
        delay, callback = callbacks.pop(0)
        self.assertEqual(delay, 500)
        (self.bridge / ctl.STOP_FILE).touch()
        callback()
        self.assertEqual(callbacks, [])

    def test_main_removes_only_its_own_pid_even_on_failure(self):
        pidfile = self.bridge / ctl.PID_FILE
        for replacement in (None, "999999"):
            with self.subTest(replacement=replacement):
                def run():
                    self.assertEqual(pidfile.read_text(), str(os.getpid()))
                    if replacement:
                        pidfile.write_text(replacement)
                    raise RuntimeError("startup failed")
                kernel = Mock()
                with patch.object(sys, "argv", ["voice_agent.py", "127.0.0.1", "8409", str(self.bridge / "voice-link.key")]), \
                     patch.object(agent, "_mutex", return_value=(kernel, 42)), \
                     patch.object(agent, "Agent") as cls:
                    cls.return_value.run.side_effect = run
                    with self.assertRaisesRegex(RuntimeError, "startup failed"):
                        agent.main()
                kernel.CloseHandle.assert_called_once_with(42)
                self.assertEqual(pidfile.read_text() if pidfile.exists() else None, replacement)

    def test_second_instance_mutex_exits_without_constructing_agent(self):
        kernel = SimpleNamespace(CreateMutexW=Mock(return_value=77), GetLastError=Mock(return_value=183), CloseHandle=Mock())
        with patch.object(ctypes, "windll", SimpleNamespace(kernel32=kernel), create=True), \
             patch.object(sys, "argv", ["voice_agent.py", "127.0.0.1", "8409", str(self.bridge / "voice-link.key")]), \
             patch.object(agent, "Agent") as cls:
            self.assertEqual(agent.main(), 0)
        cls.assert_not_called()
        kernel.CreateMutexW.assert_called_once_with(None, False, "Local\\JarvisVoiceAgent")
        kernel.CloseHandle.assert_called_once_with(77)
        self.assertFalse((self.bridge / ctl.PID_FILE).exists())
        self.assertIn("another voice agent is running", self.log_text())

    def test_flow_logs_metrics_without_speech_answer_or_clipboard(self):
        client, typer, actions, ui, player = [Mock() for _ in range(5)]
        client.dictate.return_value = {"text": SENTINEL}
        client.route.return_value = {"kind": "read_selection"}
        actions.read_selection.return_value = SENTINEL
        client.say.side_effect = RuntimeError(SENTINEL)
        voiceflow.Flow(client, typer, actions, ui, player).on_clip(b"audio", "wake")
        text = self.log_text()
        self.assertNotIn(SENTINEL, text)
        self.assertIn("RuntimeError", text)
        self.assertIn("Traceback", text)
        for field in ("source=wake", "route=read_selection", "transcribe_ms=", "route_ms=", "ask_ms=", "outcome=error"):
            self.assertIn(field, text)


class Control(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bridge = Path(tmp.name)
        (self.bridge / "venv/Scripts").mkdir(parents=True)
        (self.bridge / "venv/Scripts/pythonw.exe").touch()
        (self.bridge / "voice-link.key").touch()

    def test_start_argv_detachment_and_wait_for_own_pid(self):
        def launch(*args, **kwargs):
            (self.bridge / ctl.PID_FILE).write_text("71")
        popen = Mock(side_effect=launch)
        out = []
        with patch.object(ctl.desktop_setup, "_win_path", side_effect=lambda path: "WIN:" + path):
            self.assertEqual(ctl.start(self.bridge, popen=popen, query=lambda pid: AGENT_LINE, wait=0, echo=out.append), 0)
        argv, = popen.call_args.args
        self.assertEqual(argv[:5], ["cmd.exe", "/c", "start", "", "/b"])
        self.assertTrue(argv[5].endswith("venv/Scripts/pythonw.exe"))
        self.assertTrue(argv[6].endswith("windows/voice_agent.py"))
        self.assertEqual(argv[7:9], ["127.0.0.1", str(ctl.config.VOICE_LINK_PORT)])
        self.assertEqual(popen.call_args.kwargs, dict(cwd="/mnt/c", stdin=ctl.subprocess.DEVNULL,
                         stdout=ctl.subprocess.DEVNULL, stderr=ctl.subprocess.DEVNULL, start_new_session=True))
        self.assertIn("pid 71", "\n".join(out))
        self.assertIn(str(self.bridge / ctl.LOG_FILE), "\n".join(out))

    def test_force_stop_rechecks_original_pid_before_kill(self):
        (self.bridge / ctl.PID_FILE).write_text("31")
        answers = iter([AGENT_LINE, AGENT_LINE, "notepad.exe"])
        kill = Mock()
        self.assertEqual(ctl.stop(self.bridge, query=lambda pid: next(answers), kill=kill, wait=0, echo=lambda _: None), 0)
        kill.assert_not_called()

    def test_status_limits_warning_tail_and_reads_daemon(self):
        path = self.bridge / ctl.LOG_FILE
        path.parent.mkdir()
        path.write_text("\n".join(f"date WARNING thread warning-{i}" for i in range(10)))
        out = []
        self.assertEqual(ctl.status(self.bridge, echo=out.append, daemon_status=lambda: {"status": "Listening."}), 0)
        self.assertEqual(sum("WARNING" in line for line in out), 5)
        self.assertIn("Listening.", "\n".join(out))

    def test_setup_and_controls_share_bridge_location_check(self):
        with patch.object(voice_link_setup.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=str(self.bridge))), \
             patch.object(voice_link_setup, "windows_folder_closed", return_value=True) as closed:
            self.assertEqual(voice_link_setup.bridge_dir(), self.bridge)
            closed.assert_called_once_with(self.bridge)
        with patch.object(voice_link_setup.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=str(self.bridge))), \
             patch.object(voice_link_setup, "windows_folder_closed", return_value=False):
            with self.assertRaises(ValueError):
                voice_link_setup.bridge_dir()

    def test_follow_retries_missing_log_and_stops_on_ctrl_c(self):
        out = []
        path = self.bridge / ctl.LOG_FILE

        def append():
            path.parent.mkdir()
            path.write_text("new operation\n")

        calls = 0

        def tick(_seconds):
            nonlocal calls
            calls += 1
            if calls == 1:
                append()
            else:
                raise KeyboardInterrupt

        with patch.object(ctl.time, "sleep", side_effect=tick):
            self.assertEqual(ctl.logs(self.bridge, n=2, follow=True, echo=out.append,
                                      journal=lambda n: ["voice journal"]), 0)
        self.assertIn("new operation", out)
        self.assertIn("voice journal", out)

    def test_cli_dispatches_all_verbs_and_log_flags(self):
        from jarvis.__main__ import build_parser
        parser = build_parser()
        for verb in ("setup", "start", "stop", "status", "logs"):
            with self.subTest(verb=verb):
                argv = ["voice-link", verb] + (["-n", "7", "-f"] if verb == "logs" else [])
                args = parser.parse_args(argv)
                module = voice_link_setup if verb == "setup" else ctl
                with patch.object(voice_link_setup, "bridge_dir", return_value=self.bridge), \
                     patch.object(module, verb, return_value=0) as call:
                    self.assertEqual(args.func(args), 0)
                    if verb == "setup":
                        call.assert_called_once_with()
                    elif verb == "logs":
                        call.assert_called_once_with(self.bridge, n=7, follow=True)
                    else:
                        call.assert_called_once_with(self.bridge)


class Records(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class DaemonLogging(unittest.TestCase):
    def setUp(self):
        self.root_records = Records()
        logging.getLogger().addHandler(self.root_records)
        self.addCleanup(logging.getLogger().removeHandler, self.root_records)
        self.link = voice_link.VoiceLink(None, port=0, route_model=Mock(), asker=Mock(), speaker=Mock())
        self.link._server = object()
        self.link._key = KEY

    def connection(self, bad=False):
        # Run the real protocol over AF_UNIX socketpair; no listener, daemon,
        # Windows process, microphone or external network is started.
        server, peer = socket.socketpair()
        self.link._pending += 1
        self.link._connections.add(server)
        worker = threading.Thread(target=self.link._serve, args=(server, self.link._server, KEY))
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(peer.close)
        if bad:
            stream = peer.makefile("rwb")
            self.addCleanup(stream.close)
            wire.send_json(stream, {"hello": SENTINEL, "nonce_a": SENTINEL})
            self.assertEqual(stream.readline(), b"")
            worker.join(2)
            self.assertFalse(worker.is_alive())
            return None
        client = wire.Client("unused", 0, KEY)
        self.addCleanup(client.close)
        with patch.object(wire.socket, "create_connection", return_value=peer):
            client.connect()
        client.ping()  # handshake is complete before the log assertion begins
        return client

    def check_private(self):
        for record in self.root_records.records:
            self.assertNotIn(SENTINEL, record.getMessage())
            self.assertNotIn(SENTINEL, logging.Formatter().format(record))

    def test_route_fallback_logs_one_warning_without_speech(self):
        client = self.connection()
        self.link.route_model.side_effect = RuntimeError("routing failed for " + SENTINEL)
        with self.assertLogs("jarvis.v2.voice_link", level="WARNING") as captured:
            logging.getLogger("jarvis.v2.voice_link").propagate = True
            self.assertEqual(client.route(SENTINEL, "hotkey")["kind"], "dictate")
        self.assertEqual(len(captured.records), 1)
        self.assertIn("op=route source=hotkey", captured.output[0])
        self.assertIn("RuntimeError", captured.output[0])
        self.assertNotIn(SENTINEL, str(captured.output))
        self.assertEqual(sum(r.levelno == logging.WARNING for r in self.root_records.records), 1)
        self.check_private()

    def test_ask_failure_logs_one_warning_without_speech(self):
        client = self.connection()
        self.link.asker.side_effect = ValueError("ask failed for " + SENTINEL)
        with self.assertLogs("jarvis.v2.voice_link", level="WARNING") as captured:
            logging.getLogger("jarvis.v2.voice_link").propagate = True
            with self.assertRaises(wire.LinkError):
                client.ask(SENTINEL)
        self.assertEqual(len(captured.records), 1)
        self.assertIn("op=ask", captured.output[0])
        self.assertIn("ValueError", captured.output[0])
        self.assertNotIn(SENTINEL, str(captured.output))
        self.assertEqual(sum(r.levelno == logging.WARNING for r in self.root_records.records), 1)
        self.check_private()

    def test_bad_handshake_logs_one_warning_without_peer_data(self):
        with self.assertLogs("jarvis.v2.voice_link", level="WARNING") as captured:
            logging.getLogger("jarvis.v2.voice_link").propagate = True
            self.connection(bad=True)
        self.assertEqual(len(captured.records), 1)
        self.assertIn("handshake failed", captured.output[0])
        self.assertNotIn(SENTINEL, str(captured.output))
        self.assertEqual(sum(r.levelno == logging.WARNING for r in self.root_records.records), 1)
        self.check_private()

    def test_dictate_and_say_errors_keep_text_out_of_root_records(self):
        client = self.connection()
        self.link.transcribe = Mock(side_effect=RuntimeError(SENTINEL))
        self.link.speaker.side_effect = OSError("cannot speak " + SENTINEL)
        with self.assertLogs("jarvis.v2.voice_link", level="WARNING") as captured:
            logging.getLogger("jarvis.v2.voice_link").propagate = True
            with self.assertRaises(wire.LinkError):
                client.dictate(wire.wav_bytes(b"\0\0" * 800))
            with self.assertRaises(wire.LinkError):
                client.say(SENTINEL)
        self.assertEqual(len(captured.records), 2)
        self.assertIn("op=dictate source=hotkey", captured.output[0])
        self.assertIn("op=say source=hotkey", captured.output[1])
        self.assertNotIn(SENTINEL, str(captured.output))
        self.check_private()

    def test_invalid_route_response_logs_one_warning_and_preserves_speech(self):
        client = self.connection()
        self.link.route_model.return_value = SENTINEL
        with self.assertLogs("jarvis.v2.voice_link", level="WARNING") as captured:
            logging.getLogger("jarvis.v2.voice_link").propagate = True
            self.assertEqual(client.route(SENTINEL, "wake"), {"kind": "ask", "text": SENTINEL, "target": ""})
        self.assertEqual(len(captured.records), 1)
        self.assertNotIn(SENTINEL, str(captured.output))
        self.check_private()

    def test_partial_utterance_in_exception_is_redacted(self):
        client = self.connection()
        self.link.route_model.side_effect = RuntimeError("Cannot route: " + SENTINEL)
        with self.assertLogs("jarvis.v2.voice_link", level="WARNING") as captured:
            logging.getLogger("jarvis.v2.voice_link").propagate = True
            client.route("Jarvis please " + SENTINEL + " now", "hotkey")
        self.assertEqual(len(captured.records), 1)
        self.assertNotIn(SENTINEL, str(captured.output))
        self.check_private()


if __name__ == "__main__":
    unittest.main(verbosity=2)
