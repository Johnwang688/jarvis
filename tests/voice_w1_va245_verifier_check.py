"""PR #395 counterexamples only; no Windows, W2 implementation, or new wire needed.

Run from the repository root:
  systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 \
    env PYTHONPATH=. /home/johnw/projects/Jarvis/.venv/bin/python \
    tests/voice_w1_va245_verifier_check.py

Every test below intentionally fails on 1e32e3aa. Expectations cover the
requested Unicode/command boundaries, destructive scratch, short app targets,
hotkey route-error fallback, and clips spanning an offline interval.
"""
from __future__ import annotations

from array import array
from pathlib import Path
import queue
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

try:  # Orchestrator: import once, outside patch.dict(sys.modules), which would unload it.
    import numpy  # noqa: F401
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows import voice_agent, voiceactions, voiceflow, voicetype
from windows.voicewire import LinkError


class Input:
    def __init__(self):
        self.hwnd = 42
        self.calls = []

    def foreground(self):
        return self.hwnd

    def text(self, text):
        self.calls.append(("text", text))

    def key(self, key):
        self.calls.append(("key", key))


def bare_agent():
    a = voice_agent.Agent.__new__(voice_agent.Agent)
    a.done, a.online = threading.Event(), threading.Event()
    a.recording, a.flow_running = threading.Event(), threading.Event()
    a.generation, a.generation_lock = 1, threading.Lock()
    a.capture_lock = threading.Lock()
    a.capture, a.chunks, a.frames, a.audio_bad = None, [], 0, False
    a.clips = queue.Queue(maxsize=4)
    a.overlay, a.player = Mock(), voice_agent.Player()
    a.hud_in_front = Mock(return_value=False)
    a.user32 = SimpleNamespace(GetAsyncKeyState=lambda key: 0)
    a.host, a.port, a.key_path = "unused", 0, "unused"
    return a


class Counterexamples(unittest.TestCase):
    def test_unicode_line_boundaries_are_spaces(self):
        # str.splitlines recognizes these as line boundaries. Passing Zl/Zp
        # unchanged to KEYEVENTF_UNICODE can insert actual line/paragraph breaks;
        # dropping NEL instead also joins words across a line boundary.
        for separator in ("\u2028", "\u2029", "\x85"):
            with self.subTest(separator=ascii(separator)):
                self.assertEqual(voicetype.sanitize("alpha" + separator + "beta"),
                                 "alpha beta")

    def test_combining_marks_are_not_command_punctuation(self):
        # Mn/Me characters alter the last word; they are not punctuation.
        # Regex word extraction silently throws them away and emits a command.
        for text in ("hello send it\u0301", "new line\u0301", "scratch that\u0338"):
            with self.subTest(text=ascii(text)):
                self.assertEqual(voicetype.plan(text), [("text", text)])

    def test_scratch_after_enter_does_not_delete_another_line_or_field(self):
        for utterance in ("abc send it", "abc"):
            with self.subTest(utterance=utterance):
                backend = Input()
                typer = voicetype.Typer(backend, clock=lambda: 100.0)
                typer.run(voicetype.plan(utterance))
                if utterance == "abc":
                    typer.run(voicetype.plan("new line"))
                before = list(backend.calls)
                typer.run(voicetype.plan("scratch that"))
                self.assertEqual(backend.calls, before,
                                 "scratch backspaced after Enter changed the insertion point")

    def test_one_letter_target_does_not_launch_unrelated_app(self):
        # Explicitly probe the requested short-target false positive through
        # real Actions glue, rather than merely asserting on the pure matcher.
        launch = Mock()
        runner = Mock(return_value=SimpleNamespace(
            returncode=0,
            stdout='[{"Name":"Random Unrelated Maintenance Utility","AppID":"maintenance-id"}]'))
        actions = voiceactions.Actions(desktop=Mock(), clock=lambda: 100.0,
                                       runner=runner, launcher=launch, opener=Mock())
        actions.open_app("r")
        self.assertEqual(launch.call_count, 0,
                         "target 'r' launched Random Unrelated Maintenance Utility")

    def test_hotkey_route_error_still_types_original_words(self):
        client, typer, actions, ui, player = (Mock() for _ in range(5))
        client.dictate.return_value = {"text": "open the report and check it"}
        client.route.side_effect = LinkError("Route unavailable")
        flow = voiceflow.Flow(client, typer, actions, ui, player)
        flow.on_clip(b"fake wav", "hotkey")
        self.assertEqual(typer.run.call_args_list,
                         [unittest.mock.call([("text", "open the report and check it")])],
                         "a failed route dropped the hotkey dictation")

    def test_recording_that_overlaps_offline_is_discarded_after_reconnect(self):
        # Exercise the real capture lifecycle, without opening a microphone.
        # Both a recording begun offline and a disconnect during capture must
        # be discarded, even when the connection is back at key release.
        for starts_online in (False, True):
            with self.subTest(starts_online=starts_online):
                a = bare_agent()
                if starts_online:
                    a.online.set()
                stream = Mock()
                sd = SimpleNamespace(
                    query_hostapis=lambda: [{"name": "WASAPI", "default_input_device": 0}],
                    WasapiSettings=Mock(), InputStream=Mock(return_value=stream))
                with patch.dict(sys.modules, {"sounddevice": sd}):
                    a.start_capture()
                    if starts_online:
                        # This is the same online-state transition network()
                        # performs when a ping or an in-flight flow fails.
                        a.online.clear()
                        a.discard_clips("Jarvis isn't running")
                    a.audio(array("h", [100] * 160), 160, None, False)
                    a.online.set()  # network has reconnected before release
                    a.finish_capture()
                self.assertEqual(a.clips.qsize(), 0,
                                 "audio captured offline was queued after reconnect")


if __name__ == "__main__":
    unittest.main(verbosity=2)
