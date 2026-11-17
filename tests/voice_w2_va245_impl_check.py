"""W2 implementation checks: fake clock/devices, in-memory PCM, optional real KWS."""
from __future__ import annotations

import builtins
from collections import deque
import hashlib
import importlib.util
import io
import json
import queue
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "windows"))
import voiceui
import voicewake


def wake_imports(**modules):
    """Fake only imports made by voicewake, preserving real dependency imports.

    Restoring all of sys.modules would unload modules first imported by start(),
    including native extensions that cannot safely be imported a second time.
    """
    original = builtins.__import__

    def at_call_site(name, globals=None, locals=None, fromlist=(), level=0):
        if globals is vars(voicewake) and name in modules and level == 0:
            module = modules[name]
            if module is None:
                raise ImportError(name)
            return module
        return original(name, globals, locals, fromlist, level)

    return patch("builtins.__import__", side_effect=at_call_site)


class EndpointAdaptation(unittest.TestCase):
    def feed(self, ep, rms, count):
        return [ep.feed(rms, 1600) for _ in range(count)]

    def test_learns_quiet_floor_and_rejects_background_above_absolute_gate(self):
        ep = voicewake.Endpointer()
        self.feed(ep, 0.008, 10)
        self.assertGreater(ep.noise_floor, 0.006)
        self.assertFalse(ep.spoken)
        self.assertIsNone(ep.feed(0.018, 1600))
        self.assertFalse(ep.spoken, "background below 3x the learned floor is not speech")
        self.assertIsNone(ep.feed(0.08, 1600))
        self.assertTrue(ep.spoken)
        self.assertEqual(self.feed(ep, 0.008, 8)[-1], "end")

    def test_floor_tracks_minimum_including_initial_loud_room(self):
        ep = voicewake.Endpointer()
        self.assertIsNone(ep.feed(0.02, 1600))
        self.assertEqual(ep.noise_floor, 0.02)
        self.assertFalse(ep.spoken)
        self.assertEqual(self.feed(ep, 0.02, 39)[-1], "timeout")

    def test_speech_in_loud_room_ends_on_room_noise(self):
        ep = voicewake.Endpointer()
        self.feed(ep, 0.02, 5)
        self.feed(ep, 0.1, 10)
        self.assertTrue(ep.spoken)
        self.assertEqual(ep.noise_floor, 0.02)
        self.assertIsNone(self.feed(ep, 0.02, 7)[-1])
        self.assertEqual(ep.feed(0.02, 1600), "end")

    def test_louder_speech_does_not_raise_floor_and_quiet_lowers_it(self):
        ep = voicewake.Endpointer(silence_ms=2000)
        self.feed(ep, 0.008, 10)
        floor = ep.noise_floor
        self.feed(ep, 0.2, 20)
        self.assertEqual(ep.noise_floor, floor)
        self.feed(ep, 0.001, 10)
        self.assertLess(ep.noise_floor, 0.003)
        self.assertIsNone(ep.feed(0.012, 1600))
        self.assertEqual(ep.quiet_frames, 0)

    def test_grace_uses_sample_counts_and_preserves_timeout_deadline(self):
        ep = voicewake.Endpointer(grace_ms=250)
        self.assertIsNone(ep.feed(0.2, 4000))
        self.assertFalse(ep.spoken)
        self.assertIsNone(ep.feed(0.2, 1))
        self.assertTrue(ep.spoken, "speech continuing past grace counts immediately")
        ep = voicewake.Endpointer(grace_ms=250)
        ep.feed(0.2, 4000)
        self.assertIsNone(ep.feed(0, 59999))
        self.assertEqual(ep.feed(0, 1), "timeout")

    def test_exact_boundaries_sample_counts_and_terminal_result(self):
        ep = voicewake.Endpointer()
        self.assertIsNone(ep.feed(0, 63999))
        self.assertEqual(ep.feed(0, 1), "timeout")
        self.assertEqual(ep.feed(0.5, 1600), "timeout")
        ep = voicewake.Endpointer(rate=8000)
        ep.feed(0.1, 800)
        self.assertIsNone(ep.feed(0, 6399))
        self.assertEqual(ep.feed(0, 1), "end")
        ep = voicewake.Endpointer(max_ms=250)
        self.assertIsNone(ep.feed(0.1, 3999))
        self.assertEqual(ep.feed(0.1, 1), "max")


class OverlayTiming(unittest.TestCase):
    def setUp(self):
        self.now = 500.0
        self.state = voiceui._DisplayState(lambda: self.now)

    def test_done_and_error_have_relative_deadlines_and_clear_text(self):
        for name, seconds in (("done", 4), ("error", 5)):
            self.state.set(name, "private test text")
            self.now += seconds - 0.001
            self.assertFalse(self.state.expire())
            self.now += 0.001
            self.assertTrue(self.state.expire())
            self.assertEqual((self.state.state, self.state.text, self.state.hide_at), ("hidden", "", None))

    def test_listening_cancels_pending_hide_even_when_its_deadline_is_due(self):
        self.state.set("done")
        self.now += 4
        self.state.set("listening")
        self.assertFalse(self.state.expire())
        self.now += 100
        self.assertFalse(self.state.expire())
        self.assertEqual(self.state.text, "Listening…")
        self.state.set("error")
        self.state.set("hidden")
        self.assertEqual(self.state.state, "hidden")
        self.assertIsNone(self.state.hide_at)

    def test_producers_only_queue_and_tk_work_is_deferred(self):
        overlay = voiceui.Overlay.__new__(voiceui.Overlay)
        overlay.events = queue.Queue()
        calls = []
        overlay.root = SimpleNamespace(after=lambda *args: calls.append(args))
        thread = threading.Thread(target=lambda: (overlay.set_state("listening"), overlay.push_level(0.1)))
        thread.start()
        thread.join()
        self.assertEqual(calls, [])
        self.assertEqual(overlay.events.get_nowait(), ("state", "listening", None))
        self.assertEqual(overlay.events.get_nowait(), ("level", 0.1, None))
        with self.assertRaises(ValueError):
            overlay.set_state("invalid")


class KeywordsAndManifest(unittest.TestCase):
    def test_keywords_file_is_utf8_and_carries_custom_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "keywords.txt"
            dest.write_text(voicewake.keywords_text(voicewake.WAKE_PIECES, threshold=0.4), encoding="utf-8")
            self.assertEqual(dest.read_bytes(), "▁JA R VI S :1.0 #0.4 @JARVIS\n".encode())
        self.assertEqual(voicewake.keywords_text(["▁JA", "R", "VI", "S"], boost=2, threshold=0.15),
                         "▁JA R VI S :2.0 #0.15 @JARVIS\n")

    def test_bad_manifests_are_short_failures_not_exceptions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for manifest in ("{", "[]", "{}", '{"files": []}', '{"files": [null]}',
                             '{"files": [{"file": "../outside", "bytes": 0, "sha256": "x"}]}'):
                (root / "manifest.json").write_text(manifest)
                ok, reason = voicewake.verify_model(root)
                self.assertFalse(ok)
                self.assertTrue(reason)
                self.assertNotIn(tmp, reason)

    def test_checks_every_entry_including_same_size_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entries = []
            for name in ("one", "two"):
                (root / name).write_bytes(b"ab")
                entries.append({"file": name, "bytes": 2, "sha256": hashlib.sha256(b"ab").hexdigest()})
            (root / "manifest.json").write_text(json.dumps({"files": entries}))
            self.assertTrue(voicewake.verify_model(root)[0])
            (root / "two").write_bytes(b"cd")
            self.assertFalse(voicewake.verify_model(root)[0])

    def test_default_model_does_not_depend_on_cwd(self):
        listener = voicewake.WakeListener(None, None, lambda: False)
        self.assertEqual(listener.model_dir, ROOT / "hud/vendor/kws-zipformer-gigaspeech-3.3M")


class FakeSpotter:
    def __init__(self, **kwargs):
        self.config = kwargs
        self.keywords = Path(kwargs["keywords_file"]).read_bytes() if kwargs else None
        self.streams, self.resets, self.decodes = [], 0, 0
        self.hit_at = {1}

    def create_stream(self):
        stream = SimpleNamespace(ready=False)
        def accept(rate, samples):
            stream.ready = True
        stream.accept_waveform = accept
        self.streams.append(stream)
        return stream

    def is_ready(self, stream):
        return stream.ready

    def decode_stream(self, stream):
        stream.ready = False
        self.decodes += 1

    def get_result(self, stream):
        return "JARVIS" if self.decodes in self.hit_at else ""

    def reset_stream(self, stream):
        stream.ready = False
        self.resets += 1


class ScriptQueue:
    """Deliver deterministic chunks/actions, then stop without a wall-clock wait."""
    def __init__(self, listener, items):
        self.listener, self.items = listener, deque(items)

    def get(self, timeout=None):
        while self.items:
            item = self.items.popleft()
            if callable(item):
                item()
            else:
                return item
        self.listener._done.set()
        raise queue.Empty


class Worker(unittest.TestCase):
    def setUp(self):
        import numpy as np
        self.np = np
        self.states, self.clips, self.levels = [], [], []
        self.busy = False
        self.listener = voicewake.WakeListener(self.clips.append, self.levels.append,
                                               lambda: self.busy, self.states.append)
        self.listener._np = np
        self.spotter = self.listener._spotter = FakeSpotter()

    def chunk(self, value=0, epoch=0, captured=100, frames=1600):
        pcm = self.np.full((frames, 1), value, dtype="<i2").tobytes()
        return epoch, captured, pcm

    def run_script(self, items):
        self.listener._audio_queue = ScriptQueue(self.listener, items)
        with patch.object(voicewake.time, "monotonic", return_value=100):
            self.listener._listen()

    def test_hit_records_levels_and_delivers_an_in_memory_wav(self):
        self.run_script([self.chunk(20000)] + [self.chunk(5000)] * 3 + [self.chunk()] * 8)
        self.assertEqual(self.states, ["listening"])
        self.assertEqual(len(self.levels), 11)
        self.assertAlmostEqual(self.levels[0], 5000 / 32768)
        self.assertEqual(len(self.clips), 1)
        with wave.open(io.BytesIO(self.clips[0])) as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()),
                             (1, 2, 16000, 17600))
            self.assertEqual(wav.readframes(4800), self.chunk(5000)[2] * 3)
        self.assertGreaterEqual(self.spotter.resets, 1)
        self.assertGreaterEqual(len(self.spotter.streams), 2)

    def test_wake_word_alone_times_out_without_delivering_audio(self):
        self.run_script([self.chunk(20000)] + [self.chunk()] * 40)
        self.assertEqual(self.states, ["listening", "hidden"])
        self.assertEqual(self.clips, [])

    def test_loud_wake_tail_in_grace_times_out_without_a_question(self):
        self.run_script([self.chunk(20000)] + [self.chunk(28000)] * 2 + [self.chunk()] * 38)
        self.assertEqual(self.states, ["listening", "hidden"])
        self.assertEqual(self.clips, [])
        self.assertEqual(len(self.levels), 40)
        self.assertAlmostEqual(self.levels[0], 28000 / 32768, places=6)

    def test_question_starting_immediately_after_grace_is_recorded(self):
        grace = [self.chunk(28000), self.chunk(), self.chunk(frames=800)]
        question = self.chunk(5000)
        self.run_script([self.chunk(20000)] + grace + [question] + [self.chunk()] * 8)
        self.assertEqual(self.states, ["listening"])
        self.assertEqual(len(self.clips), 1)
        with wave.open(io.BytesIO(self.clips[0])) as wav:
            self.assertEqual(wav.getnframes(), 4000 + 1600 + 12800)
            self.assertEqual(wav.readframes(wav.getnframes()),
                             b"".join(item[2] for item in grace + [question] + [self.chunk()] * 8))

    def test_wake_tail_ending_inside_a_chunk_at_grace_does_not_count(self):
        boundary = (0, 100, self.chunk(28000, frames=800)[2] + self.chunk(frames=800)[2])
        self.run_script([self.chunk(20000)] + [self.chunk(28000)] * 2 + [boundary]
                        + [self.chunk()] * 37)
        self.assertEqual(self.states, ["listening", "hidden"])
        self.assertEqual(self.clips, [])

    def test_question_starting_inside_a_chunk_at_grace_counts(self):
        boundary = (0, 100, self.chunk(frames=800)[2] + self.chunk(5000, frames=800)[2])
        self.run_script([self.chunk(20000)] + [self.chunk()] * 2 + [boundary]
                        + [self.chunk()] * 8)
        self.assertEqual(self.states, ["listening"])
        self.assertEqual(len(self.clips), 1)
        with wave.open(io.BytesIO(self.clips[0])) as wav:
            self.assertEqual(wav.getnframes(), 17600)

    def test_long_question_is_capped_at_fifteen_seconds(self):
        self.run_script([self.chunk(20000)] + [self.chunk(5000)] * 150)
        self.assertEqual(len(self.clips), 1)
        with wave.open(io.BytesIO(self.clips[0])) as wav:
            self.assertEqual(wav.getnframes(), 15 * 16000)

    def test_busy_transition_invalidates_queued_audio_and_partial_recording(self):
        def playback():
            self.busy = True
            self.listener._audio(self.np.zeros((1600, 1), dtype="int16"), 1600, None, False)
            self.busy = False
        self.run_script([self.chunk(20000), self.chunk(5000), playback,
                         self.chunk(20000), self.chunk(epoch=1)] + [self.chunk(epoch=1)] * 8)
        self.assertEqual(self.states, ["listening"])
        self.assertEqual(self.clips, [])
        self.assertEqual(len(self.levels), 1)
        self.assertEqual(self.spotter.decodes, 10)
        self.assertGreaterEqual(len(self.spotter.streams), 2)

    def test_stale_audio_cannot_wake(self):
        self.run_script([self.chunk(20000, captured=99)])
        self.assertEqual(self.states, [])
        self.assertEqual(self.spotter.decodes, 0)

    def test_callback_drops_busy_bad_and_overflowing_audio(self):
        samples = self.np.zeros((1600, 1), dtype="int16")
        self.busy = True
        self.listener._audio(samples, 1600, None, False)
        self.assertTrue(self.listener._audio_queue.empty())
        self.busy = False
        self.listener._audio(samples, 1600, None, True)
        self.assertTrue(self.listener._audio_queue.empty())
        for _ in range(9):
            self.listener._audio(samples, 1600, None, False)
        self.assertEqual(self.listener._audio_queue.qsize(), 8)
        self.assertEqual(self.listener._epoch, 3)
        self.assertNotEqual(self.listener._audio_queue.get_nowait()[0], self.listener._epoch)


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.spotters, self.inputs = [], []
        self.sherpa = SimpleNamespace(KeywordSpotter=self.new_spotter)
        self.sd = SimpleNamespace(query_hostapis=lambda: [{"name": "Windows WASAPI", "default_input_device": 7}],
                                  WasapiSettings=lambda **kwargs: kwargs, InputStream=self.new_input)
        self.listener = voicewake.WakeListener(lambda wav: None, lambda level: None, lambda: False,
                                               threshold=0.15)
        self.addCleanup(self.listener.stop)

    def new_spotter(self, **kwargs):
        spotter = FakeSpotter(**kwargs)
        self.spotters.append(spotter)
        return spotter

    def new_input(self, **kwargs):
        stream = SimpleNamespace(config=kwargs, closed=False, started=False)
        stream.start = lambda: setattr(stream, "started", True)
        stream.close = lambda: setattr(stream, "closed", True)
        self.inputs.append(stream)
        return stream

    def test_start_shared_wasapi_stop_joins_and_removes_keyword_file(self):
        with wake_imports(sherpa_onnx=self.sherpa, sounddevice=self.sd):
            self.assertTrue(self.listener.start(), self.listener.reason)
            thread = self.listener._thread
            keyword = Path(self.spotters[0].config["keywords_file"])
            self.assertEqual(keyword.read_bytes(), "▁JA R VI S :1.0 #0.15 @JARVIS\n".encode())
            self.assertTrue(self.listener.start())  # idempotent
            self.assertEqual(len(self.inputs), 1)
            config = self.inputs[0].config
            self.assertEqual((config["device"], config["samplerate"], config["channels"], config["blocksize"]),
                             (7, 16000, 1, 1600))
            self.assertEqual(config["extra_settings"], {"exclusive": False, "auto_convert": True})
            self.listener.stop()
            self.assertFalse(thread.is_alive())
            self.assertTrue(self.inputs[0].closed)
            self.assertFalse(keyword.exists())
            self.assertTrue(self.listener.start())  # restart
            self.listener.stop()
        # Real dependencies imported during start must survive fake teardown.
        self.assertIs(sys.modules["numpy"], self.listener._np)

    def test_mic_failure_cleans_temp_and_has_fixed_reason(self):
        def fail():
            raise RuntimeError("private device details")
        self.sd.query_hostapis = fail
        with wake_imports(sherpa_onnx=self.sherpa, sounddevice=self.sd):
            self.assertFalse(self.listener.start())
        self.assertEqual(self.listener.reason, "Wake word microphone unavailable")
        self.assertIsNone(self.listener._temp)
        self.assertIsNone(self.listener._stream)
        self.assertFalse(Path(self.spotters[0].config["keywords_file"]).exists())

    def test_missing_sherpa_is_an_actionable_failure(self):
        with wake_imports(sherpa_onnx=None):
            self.assertFalse(self.listener.start())
        self.assertEqual(self.listener.reason, "Wake word needs sherpa-onnx: run jarvis voice-link setup")


@unittest.skipUnless(importlib.util.find_spec("sherpa_onnx"), "optional sherpa_onnx package absent")
class RealSpotter(unittest.TestCase):
    def test_vendored_model_constructs_and_accepts_silence(self):
        import numpy as np
        import sherpa_onnx
        # Use the listener's real construction path, faking only the Windows mic.
        listener = voicewake.WakeListener(lambda wav: None, lambda rms: None, lambda: False)
        self.addCleanup(listener.stop)
        sd = SimpleNamespace(query_hostapis=lambda: [{"name": "WASAPI", "default_input_device": 0}],
                             WasapiSettings=lambda **kwargs: kwargs,
                             InputStream=lambda **kwargs: SimpleNamespace(start=lambda: None, close=lambda: None))
        with wake_imports(sounddevice=sd):
            self.assertTrue(listener.start(), listener.reason)
            spotter = listener._spotter
            self.assertIsInstance(spotter, sherpa_onnx.KeywordSpotter)
            stream = spotter.create_stream()
            stream.accept_waveform(16000, np.zeros(16000, dtype=np.float32))
            while spotter.is_ready(stream):
                spotter.decode_stream(stream)
                self.assertEqual(spotter.get_result(stream), "")
            listener.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
