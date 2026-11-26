"""Windows helper checks using synthetic audio, fake devices and ephemeral loopback."""

from contextlib import contextmanager, nullcontext, redirect_stderr, redirect_stdout
import builtins
import http.client
import importlib
import io
import json
from pathlib import Path
from queue import Empty, Queue
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from jarvis import config
from jarvis.__main__ import build_parser
from jarvis.v3 import cli, daemon, owner_routes
from jarvis.v3.voice import installation, link, wire
from jarvis.v3.voice.session import VoiceSession
from jarvis.v3.voice.turns import VoiceTurns
from voice_session_check import Brain, Detector, Transcriber
from windows import v3voice_audio as audio, v3voice_client as client
from windows import v3voice_key as key, v3voice_main as main


FAKE_KEY = b"2b" * 32
SECRET_WORDS = "synthetic words unique to the spoken event"
DEVICE_WORDS = "synthetic device name that must stay private"


def until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for helper state.")
        time.sleep(0.002)


class FakeDevices:
    def __init__(self):
        self.opened = [0, 0]
        self.closed = [0, 0]
        self.fault = None
        self.writes = []
        self.heard = []
        self.discarded = []
        self.before_write = lambda _: None
        self.drain = lambda cancelled: not cancelled()

    @contextmanager
    def recorder(self, reference):
        self.opened[reference] += 1
        count = 0

        def read():
            nonlocal count
            time.sleep(0.003)
            if not reference and self.fault is not None:
                raise self.fault
            count += 1
            return np.full(audio.DEVICE_STEP, (count + 100 * reference) / 1000, dtype=np.float32)

        try:
            yield SimpleNamespace(read=read)
        finally:
            self.closed[reference] += 1

    @contextmanager
    def player(self):
        pending = []

        def write(samples):
            self.before_write(samples)
            written = samples.copy()
            self.writes.append(written)
            if np.any(written):
                pending.append(written)
            time.sleep(0.002)

        def drain(cancelled):
            if not self.drain(cancelled):
                return False
            self.heard.extend(pending)
            pending.clear()
            return True

        try:
            yield SimpleNamespace(write=write, drain=drain)
        finally:
            self.discarded.extend(pending)


class TestAudio(unittest.TestCase):
    def test_capture_filter_frequency_alias_rejection_and_continuity(self):
        t = np.arange(audio.DEVICE_RATE) / audio.DEVICE_RATE
        signal = 0.7 * np.sin(2 * np.pi * 1000 * t)
        straight = audio.Resampler(48000, 16000).process(signal)
        streaming = audio.Resampler(48000, 16000)
        split = np.concatenate([streaming.process(chunk) for chunk in np.array_split(signal, 113)])
        np.testing.assert_array_equal(straight, split)
        self.assertEqual(len(split), 16000)
        self.assertEqual(np.argmax(abs(np.fft.rfft(split))), 1000)
        attenuation = audio.Resampler(48000, 16000).process(0.7 * np.sin(2 * np.pi * 11000 * t))
        rms = lambda values: np.sqrt(np.mean(values[160:-160] ** 2))
        self.assertAlmostEqual(rms(split), 0.7 / np.sqrt(2), places=3)
        self.assertLess(20 * np.log10(rms(attenuation) / rms(split)), -30)

    def test_linear_resampling_keeps_phase_at_unaligned_boundaries(self):
        samples = np.sin(np.arange(163) / 19).astype(np.float32)
        expected = audio.Resampler(22050, 48000).process(samples, final=True)
        resampler = audio.Resampler(22050, 48000)
        actual = np.concatenate([resampler.process(samples[:57]), resampler.process(samples[57:135]),
                                 resampler.process(samples[135:], final=True)])
        np.testing.assert_array_equal(actual, expected)

    def test_capture_alignment_bound_error_change_restart_and_raw_only(self):
        devices = FakeDevices()
        canceller = SimpleNamespace(process=lambda near, far: near - far)
        capture = audio.Capture(devices, canceller)
        self.addCleanup(capture.stop)
        capture.start()
        raw, clean, _ = capture.get(1)
        self.assertEqual((raw.shape, clean.shape, raw.dtype, clean.dtype),
                         ((320,), (320,), np.dtype("<i2"), np.dtype("<i2")))
        # A common frame boundary appears in both streams, and the known
        # reference offset remains observable after the initial FIR transient.
        self.assertAlmostEqual(np.flatnonzero(np.diff(raw) > 10)[-1], audio.STEP + 16, delta=2)
        self.assertLess(int(clean[-1]), -3000)
        until(lambda: capture.dropped_frames >= 4)
        with capture._changed:
            self.assertEqual(len(capture._frames), 50)
            self.assertGreater(capture._frames[0][0][-1], raw[-1])
        devices.fault = OSError(DEVICE_WORDS)
        until(lambda: capture.reason is not None and devices.closed == [1, 1])
        self.assertEqual(capture.reason, "Audio capture stopped after a device error.")
        self.assertIsNone(capture.get(0))
        devices.fault = None
        capture.restart()
        self.assertIsNotNone(capture.get(1))
        devices.fault = audio.DeviceChanged()
        until(lambda: devices.closed == [2, 2])
        self.assertEqual(capture.reason, "Audio device changed.")
        with patch.dict(sys.modules, {"livekit": None}):
            self.assertIsNone(audio.Canceller.available())
        devices.fault = None
        raw_only = audio.Capture(devices)
        self.addCleanup(raw_only.stop)
        raw_only.start()
        self.assertFalse(raw_only.canceller)
        self.assertIsNone(raw_only.get(1)[1])
        self.assertIsNotNone(next(level for _, _, level in (raw_only.get(1) for _ in range(6))
                                  if level is not None))

    def test_restart_cannot_release_a_frame_from_the_previous_capture(self):
        capture = audio.Capture(FakeDevices())
        self.addCleanup(capture.stop)
        capture.start()
        generation = capture.generation
        frame = capture.get(1)
        self.assertIsNotNone(frame)
        capture.restart()
        send_audio, send_level = Mock(), Mock()
        self.assertFalse(capture.forward(generation, frame, send_audio, send_level))
        send_audio.assert_not_called()
        send_level.assert_not_called()

    def test_clock_drift_in_both_directions_for_an_hour(self):
        frame = np.zeros(audio.STEP, dtype="<i2")
        for faster in ("mic", "ref"):
            pair = audio._CapturePair()
            for i in range(360000):
                pair.mic.append(frame)
                pair.ref.append(frame)
                if i % 1000 == 0:
                    getattr(pair, faster).append(frame)
                while pair.next() is not None:
                    pass
                if abs(len(pair.mic) - len(pair.ref)) > 3:
                    self.fail("Clock backlog exceeded the correction threshold.")
            self.assertTrue(350 <= (pair.repeats if faster == "mic" else pair.drops) <= 360)
            self.assertEqual(pair.drops if faster == "mic" else pair.repeats, 0)

    def test_apm_reverse_order_delayed_reference_and_recalibration(self):
        operations, stream_delays = [], []

        def process(stream, label):
            self.assertEqual((stream.sample_rate, stream.num_channels, stream.samples_per_channel),
                             (16000, 1, 160))
            operations.append((label, np.frombuffer(stream.data, dtype="<i2")))
            if label == "near":
                stream.data = b"\0" * 320

        fake = SimpleNamespace(AudioFrame=lambda **kwargs: SimpleNamespace(**kwargs),
                               AudioProcessingModule=lambda **_: SimpleNamespace(
                                   set_stream_delay_ms=stream_delays.append,
                                   process_reverse_stream=lambda f: process(f, "far"),
                                   process_stream=lambda f: process(f, "near")))
        now = [0]
        canceller = audio.Canceller(fake, clock=lambda: now[0])
        self.assertEqual(canceller.delay_ms, 60)
        rng = np.random.default_rng(7)
        far = rng.integers(-4000, 4000, size=16000, dtype=np.int16)

        def feed(delay_ms):
            delay = delay_ms * 16
            near = np.concatenate((np.zeros(delay, dtype="<i2"), far[:-delay]))
            for offset in range(0, len(far), 160):
                clean = canceller.process(near[offset:offset + 160], far[offset:offset + 160])
                self.assertFalse(np.any(clean))

        feed(83)
        self.assertAlmostEqual(canceller.delay_ms, 83, delta=5)
        self.assertEqual([name for name, _ in operations], ["far", "near"] * 100)
        self.assertFalse(np.any(operations[0][1]))
        np.testing.assert_array_equal(operations[10][1], far[:160])
        now[0] = 29
        feed(146)
        self.assertAlmostEqual(canceller.delay_ms, 83, delta=5)
        now[0] = 30
        canceller.process(far[:160], far[:160])
        self.assertAlmostEqual(canceller.delay_ms, 146, delta=5)
        self.assertEqual(stream_delays, [10, 10, 10])

    def test_playback_order_final_write_mix_and_rate_change(self):
        devices = FakeDevices()
        played = Queue()
        draining, drained = threading.Event(), threading.Event()
        def drain(cancelled):
            draining.set()
            while not drained.wait(0.002):
                if cancelled():
                    return False
            return not cancelled()
        devices.drain = drain
        playback = audio.Playback(devices, played=lambda i: played.put((i, len(devices.writes))),
                                  flushed=lambda: None)
        self.addCleanup(playback.close)
        self.addCleanup(drained.set)
        def chunk(n, value):
            return np.full(n, value, dtype="<i2").tobytes()
        playback.put(1, wire.FIRST, 24000, chunk(240, 1024))
        playback.put(1, wire.LAST, 24000, chunk(240, 2048))
        playback.put(2, wire.FIRST | wire.LAST, 16000, chunk(160, 4096))
        playback.start()
        self.assertTrue(draining.wait(1))
        self.assertTrue(played.empty())
        drained.set()
        self.assertEqual(played.get(timeout=1), (1, 2))
        self.assertEqual(played.get(timeout=1), (2, 3))
        self.assertAlmostEqual(devices.writes[0][100], 1024 / 32768)
        self.assertAlmostEqual(devices.writes[1][-1], 2048 / 32768)
        self.assertAlmostEqual(devices.writes[2][-1], 4096 / 32768)
        playback.earcon("start")
        playback.put(3, wire.FIRST | wire.LAST, 48000, chunk(5760, 32767))
        _, written = played.get(timeout=1)
        mixed = np.concatenate(devices.writes[written - 12:written])
        self.assertLessEqual(float(mixed.max()), 1)
        self.assertGreater(float(mixed.max() - mixed.min()), 0.01)

    def test_flush_discards_in_flight_remainder_and_device_buffer_before_ack(self):
        devices = FakeDevices()
        entered, resume = threading.Event(), threading.Event()
        played, flushed = [], Queue()

        def before_write(samples):
            if np.any(samples):
                entered.set()
                resume.wait(2)

        devices.before_write = before_write
        playback = audio.Playback(devices, played=played.append,
                                  flushed=lambda: flushed.put((len(devices.writes), len(devices.discarded))))
        self.addCleanup(playback.close)
        self.addCleanup(resume.set)
        for index in (1, 2):
            playback.put(index, 3, 48000, np.full(9600, 16384, dtype="<i2").tobytes())
        playback.start()
        self.assertTrue(entered.wait(1))
        playback.flush()
        self.assertTrue(flushed.empty())
        resume.set()
        self.assertEqual(flushed.get(timeout=1), (1, 1))
        self.assertEqual(devices.heard, [])
        self.assertEqual(played, [])
        self.assertTrue(playback.put(3, wire.FIRST | wire.LAST, 48000, b"\x00\x20" * 480))
        until(lambda: played == [3])
        self.assertEqual(len(devices.heard), 1)

    def test_back_pressure_drops_newest_and_never_reports_incomplete_sentence(self):
        devices = FakeDevices()
        entered, resume = threading.Event(), threading.Event()
        played = Queue()

        def block_write(_):
            entered.set()
            resume.wait(2)

        devices.before_write = block_write
        playback = audio.Playback(devices, played=played.put, flushed=Mock())
        self.addCleanup(playback.close)
        self.addCleanup(resume.set)
        playback.start()
        self.assertTrue(entered.wait(1))

        chunk = b"\x00\x20" * (24000 // 5)
        self.assertTrue(playback.put(0, wire.FIRST, 24000, chunk))
        for _ in range(19):
            self.assertTrue(playback.put(0, 0, 24000, chunk))
        self.assertFalse(playback.put(0, 0, 24000, chunk))
        self.assertEqual(playback._queued_samples, audio.DEVICE_RATE * 4)
        resume.set()
        until(lambda: playback._queued_samples <= audio.DEVICE_RATE * 3)
        self.assertFalse(playback.put(0, wire.LAST, 24000, chunk))
        self.assertTrue(playback.put(1, wire.FIRST | wire.LAST, 24000, chunk))
        self.assertEqual(played.get(timeout=3), 1)
        self.assertTrue(played.empty())


class TestKey(unittest.TestCase):
    def test_physical_hold_shortcut_timeout_and_queue_only(self):
        states = []
        now = [0]
        talk = key.TalkKey(states.append, clock=lambda: now[0])
        self.assertIsNone(key.physical_event(key.RIGHT_CTRL, 0x02, 0x100))
        self.assertIsNone(key.physical_event(key.RIGHT_CTRL, 0x10, 0x100))
        self.assertEqual(key.physical_event(key.RIGHT_CTRL, 0x01, 0x100), (key.RIGHT_CTRL, True))
        talk.enqueue(key.RIGHT_CTRL, 0, 0x100)
        self.assertEqual(states, [])
        talk.drain()
        talk.enqueue(0x41, 0x10, 0x100)
        talk.enqueue(key.RIGHT_CTRL, 0x10, 0x101)
        talk.drain()
        self.assertEqual(states, [True])
        talk.enqueue(0x41, 0, 0x100)
        talk.enqueue(0x41, 0, 0x101)
        talk.enqueue(key.RIGHT_CTRL, 0, 0x100)
        talk.drain()
        self.assertEqual(states, [True, False])
        talk.enqueue(key.RIGHT_CTRL, 0, 0x101)
        talk.enqueue(key.RIGHT_CTRL, 0, 0x104)
        talk.drain()
        now[0] = 119.99
        talk.drain()
        self.assertEqual(states[-1], True)
        now[0] = 120
        talk.drain()
        talk.enqueue(key.RIGHT_CTRL, 0, 0x105)
        talk.enqueue(key.RIGHT_CTRL, 0, 0x100)
        talk.enqueue(key.RIGHT_CTRL, 0, 0x101)
        talk.drain()
        self.assertEqual(states, [True, False, True, False, True, False])

    def test_fake_hook_runs_on_the_worker_and_is_closed(self):
        changes, closed = Queue(), threading.Event()

        @contextmanager
        def open_hook(enqueue):
            incoming = iter([(key.RIGHT_CTRL, 0, 0x100), (key.RIGHT_CTRL, 0, 0x101)])

            def pump():
                event = next(incoming, None)
                if event is not None:
                    enqueue(*event)
                else:
                    time.sleep(0.002)
            try:
                yield pump
            finally:
                closed.set()

        talk = key.TalkKey(changes.put, hook=SimpleNamespace(open=open_hook))
        self.addCleanup(talk.close)
        talk.start()
        self.assertTrue(changes.get(timeout=1))
        self.assertFalse(changes.get(timeout=1))
        talk.close()
        self.assertTrue(closed.is_set())


class Session:
    def __init__(self):
        self.keys, self.plays, self.frames = [], [], []
        self.stopped = self.started = 0
        self.flushes = []

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def feed(self, *samples):
        self.frames.append((time.monotonic(), samples))

    def key(self, down):
        self.keys.append(down)

    def played(self, ident):
        self.plays.append(ident)

    def playback_stopped(self, number):
        self.flushes.append(number)


class TestClient(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.folder / "voice-link.key"
        self.path.write_bytes(FAKE_KEY + b"\n")
        self.path.chmod(0o600)
        self.session = Session()
        self.link = link.VoiceLink(self.session, port=0, key_path=self.path)
        self.assertTrue(self.link.start())
        self.addCleanup(self.link.close)

    def connect(self, *, canceller=True, port=None, events=None, **kwargs):
        self.connected, self.disconnected, self.states, self.audio = [], [], [], []
        self.events = [] if events is None else events
        on_event = kwargs.pop("on_event", self.events.append)
        helper = client.Client("127.0.0.1", self.link.port if port is None else port, self.path,
                               canceller=canceller, on_connect=lambda: self.connected.append(time.monotonic()),
                               on_disconnect=lambda _: self.disconnected.append(time.monotonic()),
                               on_event=on_event, on_audio=lambda *args: self.audio.append(args),
                               on_state=self.states.append, **kwargs)
        run = threading.Thread(target=helper.run)
        run.start()
        self.addCleanup(run.join, 3)
        self.addCleanup(helper.close)
        return helper, run

    def test_wire_round_trip_pacing_and_quit(self):
        hello, pings = [], []
        deliver = self.link._deliver
        def observe(peer, kind, value, now):
            if kind == wire.HELLO:
                hello.append(value)
            if kind == wire.PING:
                pings.append(value)
            deliver(peer, kind, value, now)
        self.enterContext(patch.object(self.link, "_deliver", side_effect=observe))
        events = []

        def event(value):
            events.append(value)
            if value["kind"] == "asking":
                helper.set_card(client.Card(value["id"], value["digest"],
                                            time.monotonic() + value["expires_in"]))
            elif value["kind"] == "quit":
                helper.close()

        helper, run = self.connect(events=events, on_event=event)
        until(lambda: hello and self.connected)
        self.assertTrue(hello[0]["canceller"])
        self.assertIn("talk_key", hello[0]["features"])
        self.assertEqual(hello[0]["version"], client.VERSION)
        raw = np.arange(320, dtype="<i2")
        clean = -raw
        helper.send(wire.KEY, True)
        for _ in range(5):
            helper.send_audio(raw, clean)
        helper.send(wire.KEY, False)
        helper.send(wire.LEVEL, 127)
        until(lambda: len(self.session.frames) == 5 and self.session.keys == [True, False])
        self.assertGreater(self.session.frames[-1][0] - self.session.frames[0][0], 0.07)
        for _, (near, cancelled) in self.session.frames:
            np.testing.assert_array_equal(near, raw)
            np.testing.assert_array_equal(cancelled, clean)
        self.link.on_event("say", id="sentence", text=SECRET_WORDS)
        self.link.on_audio("sentence", raw, 24000, True)
        until(lambda: events and self.audio)
        self.assertEqual(events[0], {"kind": "say", "id": "sentence", "n": 0, "text": SECRET_WORDS})
        self.assertEqual(self.audio, [(0, wire.FIRST | wire.LAST, 24000, raw.tobytes())])
        helper.send(wire.PLAYED, {"id": "sentence"})
        until(lambda: self.session.plays == ["sentence"])
        self.link.on_event("flush", n=42)
        until(lambda: {"kind": "flush", "n": 42} in events)
        helper.send(wire.FLUSHED, 42)
        self.link.on_event("asking", id="card", digest="fake", tool="demo", sentence="Example", expires_in=10)
        until(lambda: helper._card is not None)
        self.assertTrue(helper.send(wire.CLICK, {"id": "card", "digest": "fake", "decision": "deny"}))
        until(lambda: pings and not helper._pings, timeout=4)
        self.assertEqual(self.session.plays, ["sentence"])
        self.assertEqual(self.session.flushes, [42])
        self.assertEqual(self.link.clicks, 1)
        self.assertEqual(self.link.status()["level"], 127)
        self.assertTrue(self.link.request_quit())
        run.join(2)
        self.assertFalse(run.is_alive())
        self.assertEqual(events[-1], {"kind": "quit"})

    def test_refuses_wrong_proof_after_nonce_without_sending_anything_else(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(2)
            helper, run = self.connect(port=listener.getsockname()[1])
            with listener.accept()[0] as peer, peer.makefile("rb") as stream:
                peer.settimeout(2)
                nonce = wire.check_helper_hello(wire.read_json(stream))
                wrong = wire.derive_k3(wire.parse_key(b"e1" * 32))
                peer.sendall(wire.json_line(wire.daemon_hello(wrong, nonce, wire.new_nonce())))
                self.assertEqual(stream.read(), b"")
            run.join(2)
        self.assertFalse(run.is_alive())
        self.assertEqual(self.states, ["That wasn't Jarvis"])
        self.assertEqual(self.connected, [])
        self.assertFalse(helper.send(wire.KEY, True))

    def test_raw_only_reconnect_and_disconnected_audio_drop(self):
        hellos = []
        deliver = self.link._deliver
        def observe(peer, kind, value, now):
            if kind == wire.HELLO:
                hellos.append(value)
            deliver(peer, kind, value, now)
        self.enterContext(patch.object(self.link, "_deliver", side_effect=observe))
        helper, _ = self.connect(canceller=False)
        until(lambda: len(hellos) == 1 and self.connected)
        self.assertFalse(hellos[0]["canceller"])
        raw = np.arange(320, dtype="<i2")
        self.link._active.interrupt()
        until(lambda: len(self.disconnected) == 1)
        helper.send_audio(raw, None)
        until(lambda: len(self.connected) == 2)
        self.assertGreaterEqual(self.connected[1] - self.disconnected[0], 0.49)
        self.assertEqual(self.session.frames, [])
        helper.send_audio(raw, None)
        until(lambda: self.session.frames)
        np.testing.assert_array_equal(self.session.frames[0][1][0], raw)
        self.assertIsNone(self.session.frames[0][1][1])

    def test_both_mac_and_sequence_errors_close_and_reconnect(self):
        helper, _ = self.connect()
        until(lambda: self.link.status().get("helper_version"))
        encode = wire.FrameWriter.encode
        for operation in ("mac", "sequence"):
            def corrupt(writer, kind, payload):
                if operation == "sequence":
                    writer._seq += 1
                data = encode(writer, kind, payload)
                return data[:-1] + bytes((data[-1] ^ 1,)) if operation == "mac" else data
            previous = len(self.disconnected)
            with patch.object(wire.FrameWriter, "encode", corrupt):
                self.link.on_event("partial", text=SECRET_WORDS)
                until(lambda: len(self.disconnected) > previous)
            until(lambda: len(self.connected) == previous + 2 and self.link.status().get("helper_version"))
        self.assertEqual(self.states, ["Voice link rejected a frame."] * 2)
        self.assertEqual(self.events, [])
        helper.close()

    def test_retry_backoff_is_capped(self):
        helper = client.Client("127.0.0.1", self.link.port, self.path, canceller=False,
                               on_connect=Mock(), on_disconnect=Mock(), on_event=Mock(),
                               on_audio=Mock(), on_state=Mock())
        delays = []
        def wait(delay):
            delays.append(delay)
            return len(delays) == 8
        with patch.object(client.socket, "create_connection", side_effect=ConnectionRefusedError), \
                patch.object(helper._stop, "wait", side_effect=wait):
            helper.run()
        self.assertEqual(delays, [0.5, 1, 2, 4, 8, 10, 10, 10])

    def test_only_quick_disconnects_increase_delay_and_stable_session_resets_it(self):
        helper = client.Client("127.0.0.1", self.link.port, self.path, canceller=False,
                               on_connect=Mock(), on_disconnect=Mock(), on_event=Mock(),
                               on_audio=Mock(), on_state=Mock())
        with patch.object(client.socket, "create_connection"), \
                patch.object(helper, "_session", side_effect=[0, 4.99, 5, 5.01, 29.99, 30, 0, 0]), \
                patch.object(helper._stop, "wait", side_effect=[False] * 7 + [True]) as wait:
            helper.run()
        self.assertEqual([call.args[0] for call in wait.call_args_list], [0.5, 1, 2, 4, 4, 0.5, 0.5, 1])

    def test_click_rechecked_after_waiting_for_writable_socket(self):
        for invalidate in ("expire", "replace", "end"):
            with self.subTest(invalidate=invalidate):
                helper = client.Client("127.0.0.1", self.link.port, self.path, canceller=False,
                                       on_connect=Mock(), on_disconnect=Mock(), on_event=Mock(),
                                       on_audio=Mock(), on_state=Mock())
                outbox = helper._outbox = client._Outbox()
                card = client.Card("card", "digest", 110)
                helper.set_card(card)
                click = {"id": "card", "digest": "digest", "decision": "approve"}
                ks = bytes(32)
                frames = wire.FrameWriter(ks, wire.UP)
                sock = Mock()
                sock.send.side_effect = len
                now = Mock(return_value=100)

                def ready(*_):
                    if invalidate == "expire":
                        now.return_value = 110
                    elif invalidate == "replace":
                        helper.set_card(client.Card("card", "digest", 115))
                    else:
                        helper.set_card(None)
                    return [], [sock], []

                with patch.object(client.time, "monotonic", now), \
                        patch.object(client.select, "select", side_effect=ready):
                    self.assertTrue(helper.send(wire.CLICK, click))
                    _, payload = outbox.pop()
                    self.assertTrue(helper.send(wire.LEVEL, 20))
                    self.assertTrue(helper.send(wire.CLICK, click))
                    frames = helper._write_click(sock, frames, payload, card, outbox, 115)
                    if invalidate == "expire":
                        self.assertFalse(helper.send(wire.CLICK, click))
                    else:
                        self.assertEqual(list(outbox.items), [(wire.LEVEL, b"\x14")])

                sock.send.assert_not_called()
                reader = wire.FrameReader(ks, wire.UP)
                self.assertEqual(reader.read(io.BytesIO(frames.encode(wire.LEVEL, b"\x14"))), (wire.LEVEL, 20))

    def test_a_partial_inbound_frame_does_not_extend_liveness(self):
        with socket.socket() as listener, patch.object(client.wire, "LIVENESS_S", 0.25):
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(2)
            helper, _ = self.connect(port=listener.getsockname()[1])
            with listener.accept()[0] as peer, peer.makefile("rb") as stream:
                peer.settimeout(2)
                a = wire.check_helper_hello(wire.read_json(stream))
                d = wire.new_nonce()
                k3 = wire.derive_k3(wire.parse_key(FAKE_KEY))
                peer.sendall(wire.json_line(wire.daemon_hello(k3, a, d)))
                wire.check_helper_proof(k3, a, d, wire.read_json(stream))
                reader = wire.FrameReader(wire.derive_ks(k3, a, d), wire.UP)
                self.assertEqual(reader.read(stream)[0], wire.HELLO)
                peer.sendall(b"\x81\x00")
                self.assertEqual(stream.read(), b"")
            until(lambda: self.disconnected)
            self.assertLess(self.disconnected[0] - self.connected[0], 0.7)
            helper.close()

    def test_unsent_audio_bound_drops_oldest_but_keeps_interleaved_controls(self):
        box = client._Outbox()
        for i in range(80):
            if i % 4 == 0:
                box.put(wire.LEVEL, i)
            box.put(wire.AUDIO, i)
        self.assertEqual((box.audio, box.dropped), (50, 30))
        remaining = [box.pop() for _ in range(len(box.items))]
        self.assertEqual([value for kind, value in remaining if kind == wire.AUDIO], list(range(30, 80)))
        self.assertEqual([value for kind, value in remaining if kind == wire.LEVEL], list(range(0, 80, 4)))


class FakePopup:
    def __init__(self, *, on_click):
        self._on_click = on_click
        self.states = []
        self.closed = False

    def click(self, decision):
        state, _ = self.states[-1]
        self._on_click(decision, state.id, state.digest)

    def set(self, state, *, expires_in):
        self.states.append((state, expires_in))

    def close(self):
        self.closed = True


class TestMain(unittest.TestCase):
    def test_click_uses_callback_fields_and_new_card_or_state_drops_old_click(self):
        transport = Mock()
        helper = main.Helper("127.0.0.1", 1, None, logger=Mock(), devices=FakeDevices(),
                             make_canceller=lambda: None, make_client=Mock(return_value=transport),
                             make_key=Mock(), make_popup=FakePopup)
        helper._connected = True
        event = {"kind": "asking", "id": "card", "digest": "example-digest",
                 "sentence": "Example approval", "expires_in": 10}
        valid = {"id": "card", "digest": "example-digest", "decision": "deny"}
        with patch.object(main.time, "monotonic", return_value=100):
            helper._event(event)
            self.assertEqual(helper._popup.states[-1][1], 10)
            helper._click("deny", "stale-card", valid["digest"])
            helper._click("deny", valid["id"], "stale-digest")
            helper._click("deny")
        with patch.object(main.time, "monotonic", return_value=110):
            helper._click(valid)
        self.assertFalse(helper._clicks)
        helper._send_clicks()
        transport.send.assert_not_called()

        helper._event({k: v for k, v in event.items() if k != "expires_in"})
        self.assertIsNone(helper._popup.states[-1][1])
        self.assertFalse(helper._popup.states[-1][0].buttons)
        self.assertIsNone(helper._target)

        helper._event(event)
        helper._click(valid)
        helper._event(event)
        helper._send_clicks()
        transport.send.assert_not_called()
        helper._click(valid)
        helper._event({"kind": "state", "state": "working"})
        helper._send_clicks()
        transport.send.assert_not_called()

        helper._event(event)
        helper._click(valid)
        helper._send_clicks()
        transport.send.assert_called_once_with(wire.CLICK, valid)

    def test_played_requires_last_chunk_and_an_id_learned_before_completion(self):
        devices, transport = FakeDevices(), Mock()
        helper = main.Helper("127.0.0.1", 1, None, logger=Mock(), devices=devices,
                             make_canceller=lambda: None, make_client=Mock(return_value=transport),
                             make_key=Mock(), make_popup=FakePopup)
        completed = Queue()

        def played(index):
            helper._played(index)
            completed.put(index)

        helper._playback._played = played
        helper._playback.start()
        self.addCleanup(helper._playback.close)
        samples = np.full(audio.DEVICE_STEP, 16384, dtype="<i2").tobytes()
        helper._audio(0, wire.FIRST, audio.DEVICE_RATE, samples)
        until(lambda: any(np.any(chunk) for chunk in devices.writes))
        transport.send.assert_not_called()
        helper._event({"kind": "say", "n": 0, "id": "learned", "text": "Example sentence."})
        helper._audio(0, wire.LAST, audio.DEVICE_RATE, samples)
        self.assertEqual(completed.get(timeout=2), 0)
        transport.send.assert_called_once_with(wire.PLAYED, {"id": "learned"})

        heard = len(devices.heard)
        helper._audio(1, wire.FIRST | wire.LAST, audio.DEVICE_RATE, samples)
        self.assertEqual(completed.get(timeout=2), 1)
        self.assertGreater(len(devices.heard), heard)
        helper._event({"kind": "say", "n": 1, "id": "too-late", "text": "Another sentence."})
        transport.send.assert_called_once_with(wire.PLAYED, {"id": "learned"})

    def test_full_run_wiring_logging_and_cleanup(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        pairing = folder / "voice-link.key"
        pairing.write_bytes(FAKE_KEY)
        devices = FakeDevices()
        writing, release = threading.Event(), threading.Event()

        def before_write(samples):
            if np.mean(samples) > 0.4:
                writing.set()
                release.wait(2)

        devices.before_write = before_write
        stdout, stderr = io.StringIO(), io.StringIO()
        with main.log_file(folder) as logger, redirect_stdout(stdout), redirect_stderr(stderr):
            fake = Mock()
            make_client, make_key = Mock(return_value=fake), Mock()
            helper = main.Helper("127.0.0.1", 1, pairing, logger=logger, devices=devices,
                                 make_canceller=lambda: None, make_client=make_client,
                                 make_key=make_key, make_popup=FakePopup)
            callbacks = make_client.call_args.kwargs
            self.assertFalse(callbacks["canceller"])
            send_key = make_key.call_args.args[0]

            def run():
                callbacks["on_connect"]()
                until(lambda: devices.opened == [1, 1] and fake.send_audio.called)
                callbacks["on_event"]({"kind": "say", "n": 1, "id": "sentence", "text": SECRET_WORDS})
                callbacks["on_audio"](1, 3, 48000, np.full(4800, 16384, dtype="<i2").tobytes())
                try:
                    self.assertTrue(writing.wait(1))
                    send_key(True)
                    fake.send.assert_any_call(wire.KEY, True)
                    self.assertFalse(helper._sentences)
                    callbacks["on_event"]({"kind": "flush", "n": 7})
                    callbacks["on_event"]({"kind": "flush", "n": 9})
                    self.assertFalse(any(call.args[0] == wire.FLUSHED for call in fake.send.call_args_list))
                finally:
                    release.set()
                until(lambda: any(call.args == (wire.FLUSHED, 9) for call in fake.send.call_args_list))
                self.assertEqual([call.args[1] for call in fake.send.call_args_list if call.args[0] == wire.FLUSHED],
                                 [7, 9])
                self.assertTrue(devices.discarded)
                callbacks["on_event"]({"kind": "asking", "id": "card", "digest": "example-digest",
                                       "sentence": SECRET_WORDS, "expires_in": 20})
                self.assertEqual(helper._popup.states[-1][1], 20)
                helper._popup.click("deny")
                expected = (wire.CLICK, {"id": "card", "digest": "example-digest", "decision": "deny"})
                until(lambda: any(call.args == expected for call in fake.send.call_args_list))
                devices.fault = OSError(DEVICE_WORDS)
                until(lambda: helper._popup.states[-1][0].kind == "problem")
                callbacks["on_disconnect"](3)
                self.assertEqual(devices.closed, [1, 1])
                self.assertEqual(helper._popup.states[-1][0].kind, "hidden")
                callbacks["on_connect"]()
                callbacks["on_event"]({"kind": "quit"})
                fake.close.assert_called_once()

            fake.run.side_effect = run
            self.assertEqual(helper.run(), 0)
            self.assertTrue(helper._popup.closed)
            self.assertFalse(any(call.args[0] == wire.PLAYED for call in fake.send.call_args_list))
        logged = (folder / "jarvis-voice.log").read_text() + stdout.getvalue() + stderr.getvalue()
        for private in (SECRET_WORDS, DEVICE_WORDS, FAKE_KEY.decode(), "example-digest"):
            self.assertNotIn(private, logged)
        self.assertIn("Voice link connected.", logged)
        self.assertIn("Voice helper stopped.", logged)
        self.assertNotIn("16384", logged)

    def test_helper_session_round_trip_with_wake_playback_barge_in_and_quit(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        pairing = folder / "fake-pairing"
        pairing.write_bytes(FAKE_KEY)
        pairing.chmod(0o600)
        microphone, holds, acknowledgements, result = Queue(), Queue(), Queue(), Queue()
        quiet = np.zeros(audio.DEVICE_STEP, dtype=np.float32)
        t = np.arange(audio.DEVICE_STEP) / audio.DEVICE_RATE
        tone = (0.5 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)

        class Devices(FakeDevices):
            @contextmanager
            def recorder(self, reference):
                def read():
                    time.sleep(0.010)
                    if not reference:
                        try:
                            return microphone.get_nowait()
                        except Empty:
                            pass
                    return quiet

                yield SimpleNamespace(read=read)

        class ScriptedBrain(Brain):
            def send(self, text, **kwargs):
                super().send(text, **kwargs)
                self.emit("reply", text="The fixture answer is ready.")

        devices = Devices()

        def before_write(samples):
            if np.any(samples):
                try:
                    entered, release = holds.get_nowait()
                except Empty:
                    return
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("Fixture write was not released.")

        devices.before_write = before_write
        brain, asr = ScriptedBrain(), Transcriber()
        turns = VoiceTurns(brain, on_event=Mock())
        self.addCleanup(turns.close)
        voice = SimpleNamespace(name="fixture voice", synthesize=lambda _text: (
            np.full(12000, 8192, dtype=np.int16), 24000))
        session = VoiceSession(turns, transcriber=asr, voices=(voice, None), names=tuple,
                               detector=Detector(), follow_up_s=0)
        self.addCleanup(session.close)
        playback_stopped = session.playback_stopped

        def observe_flush(number):
            playback_stopped(number)
            acknowledgements.put((number, session._pending_flush, bool(devices.discarded)))

        self.enterContext(patch.object(session, "playback_stopped", side_effect=observe_flush))

        def attach(*, on_event, on_audio):
            session.attach(on_event, on_audio)
            return session

        voice_link = link.VoiceLink(attach, port=0, key_path=pairing)
        self.assertTrue(voice_link.start())
        self.addCleanup(voice_link.close)
        make_key = Mock()
        helper = main.Helper("127.0.0.1", voice_link.port, pairing, logger=Mock(), devices=devices,
                             make_canceller=lambda: None, make_key=make_key, make_popup=FakePopup)
        send_key = make_key.call_args.args[0]
        worker = threading.Thread(target=lambda: result.put(helper.run()))
        worker.start()
        self.addCleanup(worker.join, 3)
        self.addCleanup(helper._client.close)
        until(lambda: voice_link.status().get("helper_version") == client.VERSION)
        asr.warmed.wait(bool)

        def speak_request(request):
            entered, release = threading.Event(), threading.Event()
            self.addCleanup(release.set)
            holds.put((entered, release))
            asr.lines.append(f"Jarvis, {request}")
            for _ in range(16):
                microphone.put(tone)
            self.assertTrue(entered.wait(3))
            self.assertEqual(session.status()["state"], "speaking")
            self.assertTrue(any(state.line == "The fixture answer is ready."
                                for state, _ in helper._popup.states))
            return release

        first = speak_request("check the first fixture.")
        self.assertEqual(session.status()["counters"].get("played", 0), 0)
        first.set()
        until(lambda: session.status()["counters"]["played"] == 1 and session.status()["state"] == "resting")
        self.assertTrue(devices.heard)

        second = speak_request("check the second fixture.")
        send_key(True)
        until(lambda: session._pending_flush is not None)
        number = session._pending_flush
        self.assertTrue(acknowledgements.empty())
        second.set()
        self.assertEqual(acknowledgements.get(timeout=2), (number, None, True))
        self.assertEqual(session.status()["counters"]["played"], 1)
        send_key(False)
        self.assertEqual(brain.inputs.rows, ["check the first fixture.", "check the second fixture."])

        self.assertTrue(voice_link.request_quit())
        self.assertEqual(result.get(timeout=2), 0)
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(helper._popup.closed)

    def test_single_instance_checks_mutex_before_reading_key_or_opening_log(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        key_path = folder / "voice-link.key"
        args = ["127.0.0.1", "12345", str(key_path)]
        constructor = Mock()
        mutex = Mock(return_value=nullcontext(False))
        self.assertEqual(main.main(args, mutex=mutex, make_helper=constructor), 0)
        mutex.assert_called_once_with(12345)
        constructor.assert_not_called()
        self.assertEqual(list(folder.iterdir()), [])
        mutex = Mock(return_value=nullcontext(True))
        constructor.return_value.run.return_value = 0
        self.assertEqual(main.main(args, mutex=mutex, make_helper=constructor), 0)
        constructor.assert_called_once()

    def test_windows_modules_import_without_device_libraries_or_jarvis(self):
        original_import = builtins.__import__

        def checked_import(name, *args, **kwargs):
            if name.split(".")[0] in {"soundcard", "livekit", "jarvis"}:
                raise AssertionError(f"Unexpected import of {name}")
            return original_import(name, *args, **kwargs)

        with patch("ctypes.WinDLL", side_effect=AssertionError("Windows API used"), create=True), \
                patch("builtins.__import__", checked_import):
            for path in sorted((Path(__file__).resolve().parents[2] / "windows").glob("v3voice_*.py")):
                name = "windows." + path.stem
                with self.subTest(name=name):
                    module = __import__(name, fromlist=["__name__"])
                    importlib.reload(module)


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.bridge = self.folder / ".fake-bridge"
        self.scripts = self.bridge / "venv" / "Scripts"
        self.scripts.mkdir(parents=True)
        (self.scripts / "python.exe").touch()
        (self.scripts / "pythonw.exe").touch()
        self.wsl_key = self.folder / "voice-link.key"
        self.win_key = self.bridge / "voice-link.key"
        self.enterContext(patch.object(config, "VOICE_LINK_KEY", self.wsl_key))
        self.enterContext(patch.object(config, "VOICE_LINK_PORT", 12345))

    def test_setup_refuses_missing_pairing_then_installs_and_writes_cmd(self):
        output = io.StringIO()

        def convert(args, **kwargs):
            if args[0] == "wslpath":
                name = Path(args[2]).name
                value = str(self.bridge) if args[1] == "-u" else "C:\\Example Path\\" + name
                return SimpleNamespace(stdout=value)
            return SimpleNamespace()

        with patch("jarvis.voice_link_setup.windows_folder_closed", return_value=True), \
                patch("jarvis.desktop_setup._find_python", return_value="python.exe"), \
                patch.object(installation.subprocess, "run", side_effect=convert) as commands, \
                redirect_stdout(output):
            self.wsl_key.write_bytes(FAKE_KEY)
            self.assertEqual(cli.main(["voice", "setup"]), 1)
            self.assertIn("jarvis voice-link setup", output.getvalue())
            self.assertEqual(commands.call_count, 1)
            self.win_key.write_bytes(FAKE_KEY)
            args = build_parser().parse_args(["voice", "setup"])
            self.assertEqual(args.func(args), 0)
        cmd = (self.bridge / "run-jarvis-voice.cmd").read_bytes()
        self.assertIn(b'"C:\\Example Path\\python.exe" "C:\\Example Path\\v3voice_main.py"'
                      b' 127.0.0.1 12345 "C:\\Example Path\\voice-link.key"\r\n', cmd)
        self.assertNotIn(FAKE_KEY, cmd)
        self.assertNotIn(FAKE_KEY.decode(), output.getvalue())
        self.assertEqual(self.win_key.read_bytes(), FAKE_KEY)
        self.assertEqual(commands.call_args.args[0][1:],
                         ["-m", "pip", "install", "soundcard", "numpy", "pillow", "livekit"])

    def test_detached_launcher_uses_the_systemd_interop_fallback(self):
        self.wsl_key.touch()
        self.win_key.touch()
        (self.bridge / "run-jarvis-voice.cmd").touch()
        fallback = Path("/run/example/interop")
        with patch.object(installation, "_bridge", return_value=self.bridge), \
                patch.object(installation, "_script_path", return_value=r"C:\Example\v3voice_main.py"), \
                patch.object(installation, "_convert", return_value=r"C:\Example\voice-link.key"), \
                patch.object(installation, "WSL_INTEROP_SOCKET", fallback), \
                patch.object(Path, "is_socket", autospec=True, side_effect=lambda path: path == fallback), \
                patch.dict(installation.os.environ, {"WSL_INTEROP": ""}), \
                patch.object(installation.subprocess, "Popen") as launch:
            installation.launch(12345)
        args, kwargs = launch.call_args
        self.assertEqual(args[0], [str(self.scripts / "pythonw.exe"), r"C:\Example\v3voice_main.py",
                                  "127.0.0.1", "12345", r"C:\Example\voice-link.key"])
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["env"]["WSL_INTEROP"], "/run/example/interop")

    def test_exact_owner_route_and_cli_status(self):
        path = "/v3/owner/voice/helper"
        self.assertEqual([route for route in owner_routes.ROUTES if route[1] == path], [("POST", path)])
        self.assertTrue(owner_routes.ROUTES[("POST", path)].owner)
        fake_link = Mock()
        fake_link.launch_helper.return_value = True
        fake_link.request_quit.return_value = True
        service = object.__new__(daemon.Daemon)
        service._voice_link = fake_link
        status = {"brain": "claude", "brain_state": "idle", "state": "resting", "turns": 2,
                  "detector": "fixture VAD", "transcriber": {"name": "fixture ASR"},
                  "voice": {"name": "fixture TTS"}, "gpu_warm": True,
                  "link": {"listening": True, "connected": True, "level": 11, "helper_version": "example"}}
        handler = daemon._status_handler(self.folder, voice_status=lambda: status,
                                         voice_helper=service._voice_helper)
        server = daemon.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join, 2)
        self.addCleanup(server.shutdown)
        port = server.server_port
        local = f"http://127.0.0.1:{port}"

        def request(target=path, origin=None, action="start"):
            connection = http.client.HTTPConnection("127.0.0.1", port)
            headers = {"Content-Type": "application/json"}
            if origin is not None:
                headers["Origin"] = origin
            try:
                connection.request("POST", target, json.dumps({"action": action}), headers)
                return connection.getresponse().status
            finally:
                connection.close()

        self.assertEqual(request(origin="http://example.invalid"), 403)
        self.assertEqual(request(), 403)
        self.assertIn(request(path + "?action=start", local), (404, 405))
        self.assertEqual(request(origin=local, action="other"), 400)
        fake_link.launch_helper.assert_not_called()
        fake_link.request_quit.assert_not_called()
        stdout = io.StringIO()
        with patch.object(config, "V3_API_PORT", port), redirect_stdout(stdout):
            for action in ("start", "stop", "status"):
                self.assertEqual(cli.main(["voice", action]), 0)
        fake_link.launch_helper.assert_called_once()
        fake_link.request_quit.assert_called_once()
        for text in ("Started the Windows voice helper.", "Asked the Windows voice helper to quit.",
                     "The Claude voice brain is idle.", "Voice turns started: 2.",
                     "The voice link is listening.", "Microphone level: 11 out of 255.",
                     "The voice session is resting.", "The speech detector reports fixture VAD.",
                     "The transcriber is fixture ASR.", "The speech engine is fixture TTS.",
                     "The GPU voice is warm.", "The Windows voice helper is connected.",
                     "The Windows voice helper reports version example."):
            self.assertIn(text, stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
