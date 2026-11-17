"""W2 verifier counterexamples only; each test fails on PR #393's head.

No device, native inference, network, or NumPy dependency: samples and devices
are Python fakes. Product code and the existing suites are left unchanged.
Run under the memory-limited systemd scope specified in the verifier card.
"""
from __future__ import annotations

import builtins
from collections import deque
import math
from pathlib import Path
import queue
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "windows"))
import voicewake


class Samples(list):
    """The tiny array surface used by _listen, implemented without NumPy."""

    def astype(self, dtype):
        return self

    def __truediv__(self, divisor):
        return Samples(value / divisor for value in self)

    def __mul__(self, other):
        return Samples(a * b for a, b in zip(self, other))


FAKE_NP = SimpleNamespace(
    frombuffer=lambda pcm, dtype: Samples(struct.unpack("<" + "h" * (len(pcm) // 2), pcm)),
    float32=float,
    mean=lambda values: sum(values) / len(values) if values else float("nan"),
    sqrt=math.sqrt,
)


def wake_imports(**modules):
    """Inject at voicewake's import call site, as the implementation suite does."""
    original = builtins.__import__

    def at_call_site(name, globals=None, locals=None, fromlist=(), level=0):
        if globals is vars(voicewake) and name in modules and level == 0:
            return modules[name]
        return original(name, globals, locals, fromlist, level)

    return patch("builtins.__import__", side_effect=at_call_site)


class FakeSpotter:
    def __init__(self, **kwargs):
        self.decodes = 0

    def create_stream(self):
        stream = SimpleNamespace(ready=False)
        stream.accept_waveform = lambda rate, samples: setattr(stream, "ready", True)
        return stream

    def is_ready(self, stream):
        return stream.ready

    def decode_stream(self, stream):
        stream.ready = False
        self.decodes += 1

    def get_result(self, stream):
        return "JARVIS" if self.decodes == 1 else ""

    def reset_stream(self, stream):
        stream.ready = False


class DeferredThread:
    """start() sets up the real listener; the test drives its worker explicitly."""
    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        pass

    def is_alive(self):
        return False

    def join(self):
        pass


class ScriptQueue:
    def __init__(self, listener, values):
        self.listener, self.values = listener, deque(values)

    def get(self, timeout=None):
        while self.values:
            value = self.values.popleft()
            if callable(value):
                value()
            else:
                return value
        self.listener._done.set()
        raise queue.Empty

    def get_nowait(self):
        raise queue.Empty


def chunk(value=0, epoch=0):
    return epoch, 100.0, struct.pack("<h", value) * voicewake.CHUNK


class Counterexamples(unittest.TestCase):
    def test_stationary_loud_room_without_speech_times_out(self):
        endpoint = voicewake.Endpointer()
        # Steady fan/room noise, no question: no-speech timeout is four seconds.
        result = None
        for _ in range(40):
            result = endpoint.feed(0.02, 1600)
        self.assertEqual(result, "timeout",
                         "steady 0.02 RMS room noise is permanently classified as speech")

    def test_wake_tail_in_next_chunk_is_not_question_speech(self):
        clips, states = [], []
        listener = voicewake.WakeListener(clips.append, lambda rms: None,
                                         lambda: False, states.append)
        self.addCleanup(listener.stop)
        mic = SimpleNamespace(start=lambda: None, close=lambda: None)
        sd = SimpleNamespace(
            query_hostapis=lambda: [{"name": "WASAPI", "default_input_device": 0}],
            WasapiSettings=lambda **kwargs: kwargs, InputStream=lambda **kwargs: mic)
        with wake_imports(sherpa_onnx=SimpleNamespace(KeywordSpotter=FakeSpotter),
                          sounddevice=sd, numpy=FAKE_NP), \
                patch.object(voicewake.threading, "Thread", DeferredThread):
            self.assertTrue(listener.start(), listener.reason)
        # Hit near the end of "Jarvis", then 100 ms of its final /s/.
        # No question follows. Dropping only the hit chunk does not drop this tail.
        listener._audio_queue = ScriptQueue(
            listener, [chunk(12000), chunk(2000)] + [chunk()] * 40)
        with patch.object(voicewake.time, "monotonic", return_value=100.0):
            listener._run()
        self.assertEqual(listener.reason, "", "worker must finish without an exception")
        self.assertEqual(len(clips), 0, "wake tail alone emitted an utterance")
        self.assertEqual(states, ["listening", "hidden"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
