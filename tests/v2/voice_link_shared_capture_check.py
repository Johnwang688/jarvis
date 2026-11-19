"""Real paired voice sockets and fabricated PCM only. No device or transcript storage."""
import concurrent.futures
import threading
import time
from unittest.mock import Mock, patch

from tests.v2.voice_link_va245_check import Case, KEY
from windows import voicewire as wire
from jarvis.v2.daemon import APIError


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for the voice link")


class SharedCapture(Case):
    def setUp(self):
        super().setUp()
        self.stop_requested = threading.Event()
        self.released = threading.Event()
        self.instance = wire.new_nonce()
        self.client.ping(wake="ready", instance=self.instance)

    def connect_relay(self):
        self.relay = wire.MicrophoneRelay(self.client, lambda: "ready", self.stop_requested, self.released)
        self.relay.start()
        self.addCleanup(self.relay.thread.join, 4)
        self.addCleanup(self.relay.close)
        wait_for(lambda: self.link.agent_status()["shared_mic"])

    def test_demand_only_fanout_while_control_is_occupied(self):
        self.connect_relay()
        self.assertIsNone(self.relay.frames)
        self.relay.offer(bytes(wire.MIC_PACKET_BYTES))
        busy, finish = threading.Event(), threading.Event()
        def ask(*_):
            busy.set()
            finish.wait(3)
            return "done"
        self.link.asker = ask
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(self.client.ask, "synthetic question")
            try:
                self.assertTrue(busy.wait(1))
                first = self.link.subscribe_microphone()
                second = self.link.subscribe_microphone()
                wait_for(lambda: self.relay.frames is not None)
                pcm = bytes(range(100)) * 32
                self.relay.offer(pcm)
                self.assertEqual(first.get(timeout=1), pcm)
                self.assertEqual(second.get(timeout=1), pcm)
                self.link.unsubscribe_microphone(first)
                self.link.unsubscribe_microphone(second)
                wait_for(lambda: self.relay.frames is None)
            finally:
                finish.set()
            self.assertEqual(future.result(2), "done")

    def test_stop_flushes_pending_sound_and_waits_for_release(self):
        self.connect_relay()
        q = self.link.subscribe_microphone()
        wait_for(lambda: self.relay.frames is not None)
        self.relay.offer(bytes(wire.MIC_PACKET_BYTES))
        wait_for(lambda: not q.empty())
        with concurrent.futures.ThreadPoolExecutor() as pool:
            stop = pool.submit(self.link.stop_windows_agent)
            self.assertTrue(self.stop_requested.wait(2))
            self.assertIsNone(q.get(timeout=1))
            self.assertFalse(stop.done())
            self.assertEqual(self.link.agent_status()["state"], "stopping")
            self.client.close()  # Main request can exit before WASAPI cleanup.
            self.released.set()
            stop.result(2)
        self.assertTrue(self.link.agent_status()["hud_mic_paused"])
        self.assertEqual(self.link.agent_status()["state"], "off")

    def test_stop_before_control_disconnect_reaches_live_side_socket(self):
        peer = wire.Client("127.0.0.1", self.link.port, KEY)
        peer.connect(microphone_session=self.client.session)
        self.addCleanup(peer.close)
        peer._stream.write(b"PR")
        peer._stream.flush()
        self.assertEqual(peer._stream.read(1), b"I")
        with concurrent.futures.ThreadPoolExecutor() as pool:
            stop = pool.submit(self.link.stop_windows_agent)
            wait_for(lambda: self.link.agent_status()["state"] == "stopping")
            self.client.close()
            wait_for(lambda: self.link._active is None)
            peer._stream.write(b"PR")
            peer._stream.flush()
            self.assertEqual(peer._stream.read(1), b"X")
            peer._stream.write(b"X")
            peer._stream.flush()
            stop.result(2)
        self.assertEqual(self.link.agent_status()["state"], "off")

    def test_tracked_child_exit_releases_unanswered_stop(self):
        self.client.close()
        wait_for(lambda: self.link._active is None)
        self.link._launched_agent = Mock()
        self.link._launched_agent.poll.return_value = 0
        self.link._launched_instance = self.link._stopped_instance = self.instance
        self.link._release = threading.Event()
        self.link._hud_mic_paused = True
        self.assertEqual(self.link.agent_status()["state"], "off")
        self.link.stop_windows_agent()

    def test_owner_can_start_again_after_untracked_stop_ack_lost(self):
        self.client.close()
        wait_for(lambda: self.link._active is None)
        self.link._release = threading.Event()
        self.link._stopped_instance = self.instance
        self.link._hud_mic_paused = True
        self.assertEqual(self.link.agent_status()["state"], "stop_unconfirmed")
        def reconnect():
            self.client.connect()
            self.client.ping(wake="ready", instance=self.instance)
            return Mock()
        with patch.object(self.link, "_launch_windows_process", side_effect=reconnect):
            self.link.start_windows_agent()
        self.assertFalse(self.link.agent_status()["hud_mic_paused"])
        self.assertEqual(self.link.agent_status()["state"], "ready")

    def test_replacement_and_invalid_producers_never_displace_control(self):
        self.connect_relay()
        q = self.link.subscribe_microphone()
        impostor = wire.Client("127.0.0.1", self.link.port, b"x" * 32)
        with self.assertRaises(wire.NotJarvis):
            impostor.connect(microphone_session=self.client.session)
        with self.assertRaises(wire.NotJarvis):
            impostor.connect()
        self.assertTrue(self.link.agent_status()["shared_mic"])
        stale = self.client.session
        self.client.connect()
        self.client.ping(wake="ready", instance=wire.new_nonce())
        self.assertIsNone(q.get(timeout=1))
        old = wire.Client("127.0.0.1", self.link.port, KEY)
        try:
            old.connect(microphone_session=stale)
            try:
                old._stream.write(b"PR")
                old._stream.flush()
                self.assertEqual(old._stream.read(1), b"")
            except OSError:
                pass
        finally:
            old.close()
        self.assertFalse(self.link.agent_status()["shared_mic"])
        self.client.ping()
        with self.assertRaises(APIError):
            self.link.subscribe_microphone()

    def test_slow_reader_is_bounded_and_device_loss_closes_it(self):
        peer = wire.Client("127.0.0.1", self.link.port, KEY)
        peer.connect(microphone_session=self.client.session)
        self.addCleanup(peer.close)
        def exchange(frame):
            peer._stream.write(frame)
            peer._stream.flush()
            return peer._stream.read(1)
        self.assertEqual(exchange(b"PR"), b"I")
        q = self.link.subscribe_microphone()
        for n in range(12):
            self.assertEqual(exchange(b"AR" + bytes([n]) * wire.MIC_PACKET_BYTES), b"A")
        self.assertLessEqual(q.qsize(), 4)
        self.assertGreaterEqual(q.get(timeout=1)[0], 8)
        self.assertEqual(exchange(b"PM"), b"I")
        self.assertIsNone(q.get(timeout=1))
        self.assertFalse(self.link.agent_status()["shared_mic"])
        self.assertEqual(self.link.agent_status()["state"], "microphone_unavailable")

class StartupAndPolling(Case):
    def test_idle_hotkey_scan_only_reads_right_ctrl(self):
        from windows.voice_agent import Agent, VK_RCONTROL
        agent = object.__new__(Agent)
        checks = []
        agent.done = Mock()
        agent.done.is_set.side_effect = [False] * 20 + [True]
        agent.user32 = Mock()
        agent.user32.GetAsyncKeyState.side_effect = lambda code: checks.append(code) or 0
        agent.finish_capture = Mock()
        agent.poll()
        self.assertEqual(checks, [VK_RCONTROL] * 20)

    def test_app_index_built_once_while_network_can_connect(self):
        from windows.voiceactions import Actions, LaunchStatus
        waiting, ready = threading.Event(), threading.Event()
        def index(*args, **kwargs):
            waiting.set()
            ready.wait(3)
            return Mock(returncode=0, stdout='[{"Name":"Google Chrome","AppID":"test"}]')
        runner, launch = Mock(side_effect=index), Mock()
        desktop = Mock()
        desktop.foreground_window.side_effect = [None, (1, "New Tab - Google Chrome", "chrome.exe")]
        actions = Actions(desktop=desktop, runner=runner, launcher=launch, warm_in_background=True)
        self.assertTrue(waiting.wait(1))
        with concurrent.futures.ThreadPoolExecutor() as pool:
            opened = pool.submit(actions.open_app, "Chrome")
            self.client.ping()  # The index is deliberately still pending.
            ready.set()
            self.assertEqual(opened.result(1), LaunchStatus.LAUNCHED)
        runner.assert_called_once()
        launch.assert_called_once()
