"""Regression checks over a real paired loopback link, with no microphone or model."""
import base64
import concurrent.futures
import threading
import unittest
from unittest.mock import patch

from jarvis.v2 import voice_link
from windows import voicewire as wire
from windows.voiceflow import SpokenAnswer
from windows.voice_agent import first_signal_frame
from tests.v2 import voice_link_va245_impl_check as integration
from jarvis.v2.runner import TaskRunner
from jarvis.v2.provider import Event, EventKind
from tests.v2.voice_link_va245_check import Case


class PacketVoice(Case):
    def setUp(self):
        super().setUp()
        self.client.ping()
        self.assertTrue(self.client.streaming)
        self.pool = concurrent.futures.ThreadPoolExecutor(2)
        self.addCleanup(self.pool.shutdown)

    def test_packet_arrives_before_release_and_exact_samples_reach_stt(self):
        capture = wire.Capture()
        arrived = threading.Event()
        original = wire.read_json

        def observed(*args):
            frame = original(*args)
            if "pcm" in frame:
                arrived.set()
            return frame

        pcm = bytes(range(256)) * 16
        with patch.object(voice_link.wire, "read_json", side_effect=observed):
            work = self.pool.submit(self.client.dictate_capture, capture)
            try:
                capture.append(pcm)
                self.assertTrue(arrived.wait(2))
                self.assertFalse(self.calls.hotkey)
                capture.append(b"12")
            finally:
                capture.finish()
            self.assertEqual(work.result(2)["text"], "hotkey words")
        self.assertEqual(self.calls.hotkey, [wire.wav_bytes(pcm + b"12")])

    def test_cancel_after_upload_keeps_link_and_does_not_transcribe(self):
        wire.send_json(self.client._stream, {"op": "dictate_stream", "id": 7})
        wire.send_json(self.client._stream, {"pcm": base64.b64encode(b"abcd").decode()})
        wire.send_json(self.client._stream, {"end": "cancel"})
        self.assertTrue(wire.read_json(self.client._stream)["cancelled"])
        self.client.ping()
        self.assertFalse(self.calls.hotkey)

    def test_malformed_upload_closes_without_stt(self):
        wire.send_json(self.client._stream, {"op": "dictate_stream", "id": 7})
        wire.send_json(self.client._stream, {"pcm": "YQ=="})  # half a sample
        with self.assertRaises(wire.LinkError):
            wire.read_json(self.client._stream)
        self.assertFalse(self.calls.hotkey)

    def test_first_sentence_before_answer_and_no_split_decimal(self):
        heard = threading.Event()
        def answer(_, delta):
            delta("Pi is 3.")
            delta("14. ")
            if not heard.wait(2):
                raise TimeoutError()
            delta("Next")
            return "Pi is 3.14. Next"

        self.link.asker = answer
        self.link.speaker = lambda s: s.encode()
        audio = []
        def playback(wav):
            audio.append(wav)
            heard.set()

        final = self.client.ask_spoken("question", lambda _: None, playback)
        self.assertEqual(final, "Pi is 3.14. Next")
        self.assertEqual(audio, [b"Pi is 3.14.", b"Next"])

    def test_barge_in_releases_link_before_provider_finishes(self):
        waiting, release = threading.Event(), threading.Event()
        def answer(_, delta):
            delta("First. ")
            release.wait(5)
            return "First. Late."

        self.link.asker = answer
        work = self.pool.submit(self.client.ask_spoken, "question", lambda _: None,
                                lambda _: waiting.set())
        try:
            self.assertTrue(waiting.wait(2))
            self.client.interrupt_speech()
            self.assertEqual(work.result(2), "")
            self.client.ping()
        finally:
            release.set()

    def test_already_interrupted_does_not_start_request(self):
        stop = threading.Event()
        stop.set()
        self.client.ask_spoken("question", lambda _: None, lambda _: None, stop)
        self.assertFalse(self.calls.asked)

    def test_broken_local_tts_preserves_full_text(self):
        def broken(_):
            raise RuntimeError()
        self.link.speaker = broken
        self.assertEqual(self.client.ask_spoken("question", lambda _: None, lambda _: None), "Hello")


class TaskAcknowledgment(integration.Base):
    def test_task_receipt_is_first_audio_and_task_is_durable(self):
        project = self.d.stores.projects.create("Voice checks", str(self.root))
        def propose(handle, message):
            ident = handle.thread_id
            yield Event(EventKind.TURN_STARTED, ident)
            yield Event(EventKind.TEXT_DELTA, ident, {"text": "Let me check. "})
            yield Event(EventKind.TOOL_STARTED, ident, {"name": "task_propose", "call_id": "t1", "args": {}})
            yield Event(EventKind.TOOL_FINISHED, ident, {"call_id": "t1", "name": "task_propose", "result": "proposed"})
            yield Event(EventKind.TEXT_DELTA, ident, {"text": "I will open it."})
            yield Event(EventKind.TURN_FINISHED, ident, {"stop": "end", "proposal": {"brief": "Watch the tests", "project": project.id}})

        self.fast.send = propose
        self.start()
        runner = TaskRunner(self.d, self.d.router)
        runner.serve()
        self.addCleanup(runner.stop)
        def speak(text):
            self.assertEqual(len(self.d.stores.tasks.list()), 1)
            return text.encode()
        self.link.speaker = speak
        audio = []
        answer = self.client().ask_spoken("Watch the tests", lambda _: None, audio.append,
                                         defer_speech=True)
        self.assertEqual(answer, "I opened a task for that.")
        self.assertEqual(audio, [answer.encode()])


class Playback(unittest.TestCase):
    def test_first_audible_frame_skips_leading_silence_and_counts_channels(self):
        self.assertIsNone(first_signal_frame(bytes(20), 2, 1))
        self.assertEqual(first_signal_frame(bytes(8) + bytes.fromhex("0000ff7f"), 2, 2), 2)
        self.assertEqual(first_signal_frame(bytes((128, 128, 129, 255)), 1, 1), 2)

    def test_interrupt_drops_queued_and_future_sentences(self):
        playing = threading.Event()
        class Player:
            def __init__(self):
                self.cancelled = threading.Event()
                self.heard = []
            def play(self, wav):
                self.heard.append(wav)
                playing.set()
                self.cancelled.wait(2)
            def stop(self):
                self.cancelled.set()
        player = Player()
        class Source:
            def ask_spoken(self, words, delta, offer, stop, **kwargs):
                offer(b"first")
                if not playing.wait(2):
                    raise TimeoutError()
                offer(b"queued")
                player.stop()
                offer(b"late")
                return ""
        SpokenAnswer(player).ask(Source(), "hello", lambda _: None)
        self.assertEqual(player.heard, [b"first"])


if __name__ == "__main__":
    unittest.main()
