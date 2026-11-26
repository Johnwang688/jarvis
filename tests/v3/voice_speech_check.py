"""Exercise the v3 speech core with in-memory PCM and local engine doubles."""

import contextlib
import io
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Condition, Event, Thread
from time import monotonic
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from jarvis import config, stt_local
from jarvis.v3.voice import engines
from jarvis.v3.voice.hearing import EnergyVad, FRAME_SAMPLES, Hearing, MAX_PENDING_FINALS, SileroVad
from jarvis.v3.voice.speaking import Speaking


def tone(length, amplitude=0.3):
    return (amplitude * np.sin(np.arange(length) * (2 * np.pi * 400 / 16_000))).astype(np.float32)


QUIET = np.zeros(FRAME_SAMPLES, dtype=np.float32)
SPEECH = tone(FRAME_SAMPLES)
TIMEOUT = 3


class Clock:
    def __init__(self):
        self.ms = 0

    def __call__(self):
        return self.ms / 1000


class Log:
    def __init__(self):
        self.rows = []
        self.changed = Condition()

    def add(self, row):
        with self.changed:
            self.rows.append(row)
            self.changed.notify_all()

    def wait_for(self, count):
        with self.changed:
            if not self.changed.wait_for(lambda: len(self.rows) >= count, TIMEOUT):
                raise AssertionError(f"Expected {count} rows, got {len(self.rows)}")


class FakeDetector:
    def is_speech(self, frame):
        return float(np.max(np.abs(frame)) > 0.05)


class FakeTranscriber:
    def __init__(self):
        self.calls = Log()
        self.holds = {}
        self.closed = Event()
        self.empty = False

    def transcribe(self, samples, *, names):
        ordinal = len(self.calls.rows) + 1
        self.calls.add((samples, names))
        if ordinal in self.holds and not self.holds[ordinal].wait(TIMEOUT):
            raise TimeoutError("test did not release fake decode")
        return "" if self.empty else f"decode {ordinal}"

    def close(self):
        self.closed.set()


class HearingCheck(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.transcriber = FakeTranscriber()
        self.events = Log()
        self.hearing = Hearing(self.transcriber, names=["Codex"], clock=self.clock,
                               detector=FakeDetector(), on_event=self.on_event)
        self.addCleanup(self.hearing.close)

    def on_event(self, kind, **fields):
        self.events.add((kind, fields))

    def feed(self, count, frame=SPEECH):
        for _ in range(count):
            self.clock.ms += 20
            self.hearing.feed(frame)

    def hold(self, ordinal):
        event = self.transcriber.holds[ordinal] = Event()
        self.addCleanup(event.set)
        return event

    def idle_decode(self):
        with self.hearing._changed:
            self.assertTrue(self.hearing._changed.wait_for(lambda: self.hearing._job is None, TIMEOUT))

    def test_preroll_start_endpoint_and_head_start_metrics(self):
        quiet_marker = tone(FRAME_SAMPLES, 0.01)
        self.feed(15, QUIET)
        self.feed(1, quiet_marker)
        self.feed(7)
        self.assertFalse(self.hearing.speech_active())
        self.assertEqual(self.events.rows, [])
        self.feed(1)
        self.assertTrue(self.hearing.speech_active())
        self.events.wait_for(1)
        self.assertEqual(self.events.rows, [("speech_start", {})])
        release = self.hold(1)
        self.feed(9, QUIET)
        self.assertEqual(len(self.transcriber.calls.rows), 0)
        self.feed(1, QUIET)
        self.transcriber.calls.wait_for(1)
        audio, names = self.transcriber.calls.rows[0]
        np.testing.assert_array_equal(audio, np.concatenate([QUIET] * 14 + [quiet_marker]
                                                            + [SPEECH] * 8 + [QUIET] * 10))
        self.assertEqual(names, ("Codex",))
        self.clock.ms += 40
        release.set()
        self.idle_decode()
        self.feed(14, QUIET)
        self.assertTrue(self.hearing.speech_active())
        self.assertEqual(len(self.events.rows), 1)
        self.feed(1, QUIET)
        self.assertFalse(self.hearing.speech_active())
        self.events.wait_for(3)
        kind, result = self.events.rows[1]
        self.assertEqual(kind, "final")
        self.assertEqual(result["text"], "decode 1")
        self.assertEqual(result["audio_ms"], 660)
        self.assertAlmostEqual(result["decode_ms"], 40)
        self.assertEqual(result["waited_ms"], 0)
        self.assertEqual(self.events.rows[2], ("speech_end", {}))
        self.feed(25, QUIET)
        self.assertEqual(len(self.transcriber.calls.rows), 1)
        self.assertEqual(len(self.events.rows), 3)

    def test_short_pause_discards_inflight_head_and_keeps_internal_gap(self):
        release = self.hold(1)
        self.feed(8)
        self.feed(10, QUIET)
        self.transcriber.calls.wait_for(1)
        self.feed(2)
        self.assertTrue(self.hearing.speech_active())
        release.set()
        self.idle_decode()
        self.feed(25, QUIET)
        self.events.wait_for(3)
        self.assertEqual([kind for kind, _ in self.events.rows], ["speech_start", "final", "speech_end"])
        self.assertEqual(self.events.rows[1][1]["text"], "decode 2")
        self.assertEqual(len(self.transcriber.calls.rows), 2)
        samples, _ = self.transcriber.calls.rows[1]
        np.testing.assert_array_equal(samples, np.concatenate([SPEECH] * 8 + [QUIET] * 10
                                                              + [SPEECH] * 2 + [QUIET] * 10))

    def test_resumed_speech_discards_already_prepared_head(self):
        self.feed(8)
        self.feed(10, QUIET)
        self.idle_decode()
        self.feed(2)
        self.feed(25, QUIET)
        self.events.wait_for(3)
        self.assertEqual(self.events.rows[1][1]["text"], "decode 2")
        self.assertEqual(len(self.transcriber.calls.rows), 2)

    def test_partials_skip_busy_rounds_and_feed_stays_responsive(self):
        release = self.hold(1)
        self.feed(24)
        self.assertEqual(len(self.transcriber.calls.rows), 0)
        self.feed(1)
        self.transcriber.calls.wait_for(1)
        fed = Event()

        def feed_while_decoding():
            self.feed(49)
            fed.set()

        feeder = Thread(target=feed_while_decoding, daemon=True)
        feeder.start()
        self.assertTrue(fed.wait(0.5), "feed blocked on transcription")
        self.assertEqual(len(self.transcriber.calls.rows), 1)
        release.set()
        self.idle_decode()
        self.assertEqual(len(self.transcriber.calls.rows), 1)
        self.feed(1)
        self.events.wait_for(3)
        self.assertEqual(self.events.rows[1:], [("partial", {"text": "decode 1"}),
                                               ("partial", {"text": "decode 2"})])
        self.assertEqual([len(audio) // FRAME_SAMPLES for audio, _ in self.transcriber.calls.rows], [25, 75])

    def test_cap_counts_preroll_and_keeps_available_tail(self):
        self.transcriber.empty = True
        self.feed(15, QUIET)
        self.feed(1484)
        self.assertTrue(self.hearing.speech_active())
        quiet_marker = tone(FRAME_SAMPLES, 0.01)
        self.feed(1, quiet_marker)
        self.assertFalse(self.hearing.speech_active())
        self.events.wait_for(3)
        self.assertEqual(self.events.rows[1][1]["audio_ms"], 30_000)
        audio, _ = self.transcriber.calls.rows[-1]
        self.assertEqual(len(audio), 1500 * FRAME_SAMPLES)
        np.testing.assert_array_equal(audio[-FRAME_SAMPLES:], quiet_marker)
        self.feed(8)
        self.feed(25, QUIET)
        self.events.wait_for(6)
        self.assertEqual([kind for kind, _ in self.events.rows], ["speech_start", "final", "speech_end"] * 2)
        self.assertEqual([fields["text"] for kind, fields in self.events.rows if kind == "final"], ["", ""])

    def test_final_uses_head_already_running_at_endpoint(self):
        release = self.hold(1)
        self.feed(8)
        self.feed(10, QUIET)
        self.transcriber.calls.wait_for(1)
        self.feed(15, QUIET)
        self.assertFalse(self.hearing.speech_active())
        self.clock.ms += 80
        release.set()
        self.events.wait_for(3)
        self.assertEqual(self.events.rows[1][1]["text"], "decode 1")
        self.assertAlmostEqual(self.events.rows[1][1]["waited_ms"], 80)
        self.assertEqual(len(self.transcriber.calls.rows), 1)

    def test_queue_overflow_completes_oldest_and_discards_its_late_decode(self):
        partial, final = self.hold(1), self.hold(2)
        self.feed(25)
        self.transcriber.calls.wait_for(1)
        self.feed(25, QUIET)
        partial.set()
        self.transcriber.calls.wait_for(2)
        for _ in range(4):
            self.feed(8)
            self.feed(25, QUIET)

        self.events.wait_for(9)
        with self.hearing._changed:
            self.assertEqual(MAX_PENDING_FINALS, 3)
            self.assertEqual(len(self.hearing._ended), MAX_PENDING_FINALS)
        final.set()
        self.events.wait_for(15)
        self.idle_decode()
        finals = [fields for kind, fields in self.events.rows if kind == "final"]
        self.assertEqual([fields["text"] for fields in finals], ["", "", "decode 3", "decode 4", "decode 5"])
        self.assertEqual([fields.get("dropped") for fields in finals], [True, True, None, None, None])
        self.assertEqual([kind for kind, _ in self.events.rows].count("speech_end"), 5)

    def test_close_during_decode_suppresses_events_and_releases_engine(self):
        release = self.hold(1)
        self.feed(8)
        self.events.wait_for(1)
        self.feed(25, QUIET)
        self.transcriber.calls.wait_for(1)
        closed = Event()

        def close():
            self.hearing.close()
            closed.set()

        Thread(target=close, daemon=True).start()
        self.assertTrue(closed.wait(0.5), "close waited for native decode")
        release.set()
        self.assertTrue(self.transcriber.closed.wait(TIMEOUT))
        self.feed(8)
        self.assertEqual(self.events.rows, [("speech_start", {})])

    def test_silero_buffers_real_windows_and_preserves_recurrent_context(self):
        session = Mock()
        session.run.side_effect = [(np.array([[0.8]]), np.full((2, 1, 128), i, dtype=np.float32))
                                   for i in (1, 2, 3)]
        runtime = SimpleNamespace(SessionOptions=SimpleNamespace, InferenceSession=Mock(return_value=session))
        with patch.dict(sys.modules, {"onnxruntime": runtime}):
            vad = SileroVad()
            runtime.InferenceSession.assert_not_called()
            self.assertEqual(vad.status(), "energy fallback (Silero not started)")
            vad.warm()
            vad._loader.join(TIMEOUT)
            self.assertEqual(vad.status(), "Silero VAD")
        frames = np.arange(1600, dtype=np.float32).reshape(-1, 320)
        probabilities = [vad.is_speech(frame) for frame in frames]
        self.assertEqual(probabilities, [0.0, 0.8, 0.8, 0.8, 0.8])
        first, second, third = [call.args[1] for call in session.run.call_args_list]
        np.testing.assert_array_equal(first["input"], np.r_[np.zeros(64), np.arange(512)][None, :])
        np.testing.assert_array_equal(second["input"], np.arange(448, 1024)[None, :])
        np.testing.assert_array_equal(third["input"], np.arange(960, 1536)[None, :])
        np.testing.assert_array_equal(second["state"], np.ones((2, 1, 128)))
        self.assertEqual(first["sr"].item(), 16_000)
        self.assertEqual(runtime.InferenceSession.call_args.kwargs["providers"], ["CPUExecutionProvider"])

    def test_first_frame_starts_silero_and_uses_energy_while_loading(self):
        entered, called, release = Event(), Event(), Event()

        def load(*args, **kwargs):
            entered.set()
            if not release.wait(TIMEOUT):
                raise TimeoutError("test did not release VAD load")
            raise ImportError("fake unavailable runtime")

        runtime = SimpleNamespace(SessionOptions=SimpleNamespace, InferenceSession=Mock(side_effect=load))
        with patch.dict(sys.modules, {"onnxruntime": runtime}):
            hearing = Hearing(self.transcriber, on_event=self.on_event)
            self.addCleanup(hearing.close)
            self.assertEqual(hearing.detector_status(), "energy fallback (Silero not started)")

            def feed():
                for _ in range(8):
                    hearing.feed(SPEECH)
                called.set()

            Thread(target=feed, daemon=True).start()
            try:
                self.assertTrue(entered.wait(TIMEOUT))
                self.assertTrue(called.wait(0.5), "feed waited for the VAD loader")
                self.assertTrue(hearing.speech_active())
                self.assertEqual(hearing.detector_status(), "energy fallback (Silero loading)")
            finally:
                release.set()
            hearing._detector._loader.join(TIMEOUT)
            self.assertEqual(hearing.detector_status(), "energy fallback (Silero unavailable)")
            runtime.InferenceSession.assert_called_once()

    def test_adaptive_energy_fallback_tracks_low_noise(self):
        vad = EnergyVad()
        noise = tone(FRAME_SAMPLES, 0.01)
        for _ in range(80):
            self.assertLess(vad.is_speech(noise), 0.5)
        self.assertLess(vad.is_speech(tone(FRAME_SAMPLES, 0.025)), 0.5)
        self.assertGreaterEqual(vad.is_speech(SPEECH), 0.5)


class EnginesCheck(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for target in ("socket.socket.connect", "socket.create_connection", "socket.getaddrinfo"):
            blocked = self.enterContext(patch(target, side_effect=AssertionError("network call")))
            self.addCleanup(blocked.assert_not_called)

    def fake_whisper(self):
        module = ModuleType("faster_whisper")
        download_model = Mock(side_effect=AssertionError("model download attempted"))
        download_tokenizer = Mock(side_effect=AssertionError("tokenizer download attempted"))
        self.addCleanup(download_model.assert_not_called)
        self.addCleanup(download_tokenizer.assert_not_called)
        backend = Mock()

        def whisper(path, *, local_files_only, **options):
            if not Path(path).is_dir() or not local_files_only:
                download_model()
            if not (Path(path) / "tokenizer.json").is_file():
                download_tokenizer()
            self.assertNotIn("files", options)
            self.assertEqual((Path(path) / "tokenizer.json").read_bytes(), b"fake-file")
            self.assertEqual((Path(path) / "model.bin").resolve(), self.root / "model.bin")
            return backend

        module.WhisperModel = Mock(side_effect=whisper)
        self.enterContext(patch.dict(sys.modules, {"faster_whisper": module}))
        self.enterContext(patch.object(engines, "_preload_cuda"))
        return module.WhisperModel, backend

    def model_files(self):
        for name in ("model.bin", "config.json", "vocabulary.json", "tokenizer.json", "preprocessor_config.json"):
            (self.root / name).write_bytes(b"fake-file")

    def test_whisper_filters_hallucinations_and_bounds_prompt(self):
        load, backend = self.fake_whisper()
        self.model_files()
        engine = engines.WhisperGpu(self.root)
        self.addCleanup(engine.close)
        load.assert_not_called()
        for stock in (" Thank you. ", "Thanks for watching!", "you"):
            backend.transcribe.return_value = ([SimpleNamespace(text=stock)], None)
            with self.subTest(stock=stock):
                self.assertEqual(engine.transcribe(tone(8_000), names=[]), "")
                self.assertEqual(engine.transcribe(tone(40_000, 0.01), names=[]), "")
                self.assertEqual(engine.transcribe(tone(40_000), names=[]), stock.strip())
        backend.transcribe.reset_mock()
        self.assertEqual(engine.transcribe(tone(2_000, 0.001), names=[]), "")
        backend.transcribe.assert_not_called()
        backend.transcribe.return_value = ([SimpleNamespace(text="Codex opened the file.")], None)
        pcm = (tone(40_000) * 32768).astype(np.int16)
        self.assertEqual(engine.transcribe(pcm, names=["Codex", "Example", "X" * 5000]), "Codex opened the file.")
        args, options = backend.transcribe.call_args
        self.assertEqual(args[0].dtype, np.float32)
        options = options.copy()
        prompt = options.pop("initial_prompt")
        self.assertTrue(prompt.startswith("Names and apps: Codex, Example, "))
        self.assertEqual(len(prompt), engines.MAX_PROMPT_CHARS)
        self.assertEqual(options, dict(language="en", beam_size=1, temperature=0, vad_filter=False,
                                       without_timestamps=True, condition_on_previous_text=False))
        self.assertTrue(engine.available())
        load.assert_called_once()
        self.assertEqual(load.call_args.kwargs["device"], "cuda")

    def test_missing_whisper_directory_or_tokenizer_never_enters_loader(self):
        load, _ = self.fake_whisper()
        absent = engines.WhisperGpu(self.root / "missing")
        self.assertFalse(absent.available())
        self.assertIn("missing", absent.reason)
        self.model_files()
        (self.root / "tokenizer.json").unlink()
        self.assertFalse(engines.WhisperGpu(self.root).available())
        (self.root / "tokenizer.json").touch()
        self.assertFalse(engines.WhisperGpu(self.root).available())
        load.assert_not_called()

    def test_whisper_tokenizer_stays_local_during_model_directory_replacement(self):
        load, _ = self.fake_whisper()
        self.model_files()
        check_local_files = load.side_effect

        def replace_tokenizer(path, **options):
            (self.root / "tokenizer.json").unlink()
            return check_local_files(path, **options)

        load.side_effect = replace_tokenizer
        engine = engines.WhisperGpu(self.root)
        self.addCleanup(engine.close)
        self.assertTrue(engine.available())
        self.assertFalse(Path(load.call_args.args[0]).exists())

    def test_transcriber_selection_matrix_and_explicit_choice(self):
        for gpu in (False, True):
            for cpu in (False, True):
                whisper = Mock(reason="Whisper unavailable", **{"available.return_value": gpu})
                parakeet = Mock(reason="Parakeet unavailable", **{"available.return_value": cpu})
                with self.subTest(gpu=gpu, cpu=cpu), patch.multiple(
                    engines, WhisperGpu=Mock(return_value=whisper), ParakeetCpu=Mock(return_value=parakeet)
                ):
                    choice = engines.pick_transcriber("auto")
                    self.assertEqual(choice.name, "whisper-gpu" if gpu else "parakeet-cpu" if cpu else None)
                    self.assertIs(choice.engine, whisper if gpu else parakeet if cpu else None)
                    self.assertIn("whisper-gpu is available" if gpu else "Whisper unavailable", choice.reason)
                    if gpu:
                        parakeet.available.assert_not_called()
                    explicit = engines.pick_transcriber("parakeet-cpu")
                    self.assertEqual(explicit.reason, "parakeet-cpu was requested" if cpu else "Parakeet unavailable")
        self.assertIsNone(engines.pick_transcriber("invalid").engine)

    def fake_kokoro(self):
        self.model_files()
        self.enterContext(patch.multiple(config, KOKORO_MODEL=self.root / "model.bin",
                                         KOKORO_VOICES=self.root / "config.json"))
        synth = Mock()
        module = SimpleNamespace(Kokoro=SimpleNamespace(from_session=Mock(return_value=synth)))
        ort = SimpleNamespace(SessionOptions=Mock, InferenceSession=Mock(), get_available_providers=Mock(
            return_value=["CPUExecutionProvider", "CUDAExecutionProvider"]))
        self.enterContext(patch.dict(sys.modules, {"onnxruntime": ort, "kokoro_onnx": module}))
        self.enterContext(patch.object(engines, "_preload_cuda"))
        return ort, synth

    def test_voice_selection_checks_actual_provider_in_all_combinations(self):
        ort, synth = self.fake_kokoro()
        for gpu in (False, True):
            for cpu in (False, True):
                def session(model, *, sess_options, providers):
                    active = providers[0]
                    if active == "CUDAExecutionProvider" and not gpu:
                        active = "CPUExecutionProvider"  # provider advertised but loading silently fell back
                    elif active == "CPUExecutionProvider" and not cpu:
                        raise RuntimeError("unavailable")
                    return Mock(**{"get_providers.return_value": [active]})

                ort.InferenceSession.side_effect = session
                with self.subTest(gpu=gpu, cpu=cpu):
                    choice = engines.pick_voice("auto")
                    self.assertEqual(choice.name, "kokoro-gpu" if gpu else "kokoro-cpu" if cpu else None)
                    self.assertIn("kokoro-gpu is available" if gpu else "CUDA session could not be initialized",
                                  choice.reason)
                    if choice.engine is not None:
                        self.assertIs(choice.engine._backend, synth)
                        choice.engine.close()
                    explicit = engines.pick_voice("kokoro-gpu")
                    self.assertEqual(explicit.name, "kokoro-gpu" if gpu else None)
                    if explicit.engine is not None:
                        explicit.engine.close()
                    explicit = engines.pick_voice("kokoro-cpu")
                    self.assertEqual(explicit.name, "kokoro-cpu" if cpu else None)
                    if explicit.engine is not None:
                        explicit.engine.close()
        self.assertIsNone(engines.pick_voice("invalid").engine)

    def test_kokoro_lazy_threads_speed_markdown_and_edge_trimming(self):
        ort, synth = self.fake_kokoro()
        ort.InferenceSession.return_value = Mock(**{"get_providers.return_value": ["CPUExecutionProvider"]})
        engine = engines.KokoroVoice(device="cpu")
        self.addCleanup(engine.close)
        ort.InferenceSession.assert_not_called()
        rate = 24_000
        synth.create.return_value = (np.r_[np.zeros(5000), np.full(600, 0.7), np.zeros(6000)], rate)
        pcm, actual_rate = engine.synthesize("**Read this.**")
        self.assertEqual(actual_rate, rate)
        self.assertEqual(pcm.dtype, np.int16)
        self.assertEqual(len(pcm), 1440 + 600 + 1440)
        audible = np.flatnonzero(pcm)
        self.assertEqual((audible[0], len(pcm) - audible[-1] - 1), (1440, 1440))
        synth.create.assert_called_once_with("Read this.", voice="bm_george", speed=1.35, lang="en-gb", trim=False)
        options = ort.InferenceSession.call_args.kwargs["sess_options"]
        self.assertEqual((options.intra_op_num_threads, options.inter_op_num_threads), (4, 1))
        for supplied, expected in ((0.2, 0.8), (4, 2.0)):
            limited = engines.KokoroVoice(device="cpu", speed=supplied)
            limited.synthesize("A sentence.")
            self.assertEqual(synth.create.call_args.kwargs["speed"], expected)
            limited.close()

    def test_parakeet_wraps_shared_loader_and_normalizes_pcm(self):
        backend = Mock(**{"decode.return_value": " decoded "})
        with patch.object(stt_local, "_default_factory", return_value=backend) as load:
            engine = engines.ParakeetCpu()
            load.assert_not_called()
            self.assertTrue(engine.available())
            self.assertEqual(engine.transcribe(np.array([-32768, 0], dtype=np.int16), names=["ignored"]), "decoded")
            load.assert_called_once_with(config.STT_LOCAL_THREADS)
            samples, rate = backend.decode.call_args.args
            np.testing.assert_array_equal(samples, np.array([-1, 0], dtype=np.float32))
            self.assertEqual(rate, 16_000)
            engine.close()
            backend.close.assert_called_once()
            self.assertFalse(engine.available())

    def test_no_models_available_quietly_with_temporary_home(self):
        capture = io.StringIO()
        with patch.dict(os.environ, {"HOME": str(self.root)}), patch.multiple(
            config, STT_LOCAL_MODEL_DIR=self.root / "stt", KOKORO_MODEL=self.root / "tts.onnx",
            KOKORO_VOICES=self.root / "voices.bin"
        ), contextlib.redirect_stdout(capture), contextlib.redirect_stderr(capture):
            self.assertFalse(engines.ParakeetCpu().available())
            self.assertFalse(engines.KokoroVoice().available())
        self.assertEqual(capture.getvalue(), "")

    def test_gpu_wheel_dependencies_and_silero_packaging(self):
        try:
            import tomllib
        except ImportError:
            self.skipTest("tomllib needs Python 3.11")
        data = tomllib.loads((config.REPO_ROOT / "pyproject.toml").read_text())
        self.assertEqual(data["project"]["optional-dependencies"]["voice-gpu"], [
            "faster-whisper==1.2.1", "ctranslate2==4.9.1",
            "nvidia-cublas-cu12==12.9.3.13", "nvidia-cudnn-cu12==9.28.1.4",
            "nvidia-cuda-runtime-cu12==12.9.79", "nvidia-cufft-cu12==11.4.1.4",
            "nvidia-curand-cu12>=10.3.10,<10.4", "nvidia-cuda-nvrtc-cu12==12.9.86",
            "nvidia-nvjitlink-cu12==12.9.86",
        ])
        includes = data["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
        self.assertIn("jarvis/v3/voice/" + SileroVad._MODEL, includes.values())
        self.assertIn("jarvis/v3/voice/silero-LICENSE.txt", includes.values())

    def test_cuda_preloads_absolute_wheel_paths_in_dependency_order(self):
        def open_library(path, *, mode):
            if path == "libcuda.so.1":
                raise OSError("driver requires WSL path")
            self.assertEqual(mode, engines.ctypes.RTLD_GLOBAL)
            return path

        package = SimpleNamespace(locate_file=lambda path: self.root / path)
        engines._preload_cuda.cache_clear()
        self.addCleanup(engines._preload_cuda.cache_clear)
        search_path = os.environ.get("LD_LIBRARY_PATH")
        with patch.object(engines, "distribution", return_value=package), patch.object(
            engines.ctypes, "CDLL", side_effect=open_library
        ) as dlopen:
            handles = engines._preload_cuda()
            expected_calls = dlopen.call_count
            self.assertIs(engines._preload_cuda(), handles)
            self.assertEqual(dlopen.call_count, expected_calls)
        self.assertEqual(handles[0], "/usr/lib/wsl/lib/libcuda.so.1")
        self.assertTrue(all(Path(path).is_relative_to(self.root) for path in handles[1:]))
        names = [Path(path).name for path in handles]
        self.assertLess(names.index("libcublasLt.so.12"), names.index("libcublas.so.12"))
        self.assertLess(names.index("libcudnn_graph.so.9"), names.index("libcudnn_ops.so.9"))
        self.assertEqual(os.environ.get("LD_LIBRARY_PATH"), search_path)


class SpeakingCheck(unittest.TestCase):
    def setUp(self):
        self.engine_calls = Log()
        self.audio = Log()
        self.done = Log()
        self.errors = Log()
        self.clock = Clock()
        self.synthesis_holds = {}
        self.audio_holds = {}
        self.pcm = np.arange(9000, dtype=np.int16)
        self.closed = Event()
        voice = SimpleNamespace(synthesize=self.synthesize, close=self.closed.set)
        self.speaking = Speaking(voice, on_audio=self.on_audio, on_done=self.done.add,
                                 on_error=self.errors.add, clock=self.clock)
        self.addCleanup(self.speaking.close)

    def synthesize(self, text):
        self.engine_calls.add(text)
        if text in self.synthesis_holds and not self.synthesis_holds[text].wait(TIMEOUT):
            raise TimeoutError("test did not release synthesis")
        if text == "fail":
            raise RuntimeError("fake synthesis failure")
        return self.pcm, 16_000

    def on_audio(self, id, samples, rate, first, last):
        self.audio.add((id, samples, rate, first, last))
        if id in self.audio_holds and not self.audio_holds[id].wait(TIMEOUT):
            raise TimeoutError("test did not release sink")

    def hold(self, holds, key):
        event = holds[key] = Event()
        self.addCleanup(event.set)
        return event

    def test_order_chunks_two_prepared_ahead_and_timing(self):
        first_play = self.hold(self.audio_holds, 1)
        first_synth = self.hold(self.synthesis_holds, "1")
        second = self.hold(self.synthesis_holds, "2")
        for id in range(1, 6):
            self.speaking.say(id, str(id))
        self.engine_calls.wait_for(1)
        self.clock.ms = 65
        first_synth.set()
        self.audio.wait_for(1)
        self.engine_calls.wait_for(2)
        self.assertEqual(self.engine_calls.rows, ["1", "2"])
        second.set()
        with self.speaking._changed:
            self.assertTrue(self.speaking._changed.wait_for(
                lambda: self.speaking._queue[2].ready, TIMEOUT))
            self.assertIsNone(self.speaking._next_unprepared())
        self.assertEqual(self.engine_calls.rows, ["1", "2", "3"])
        self.assertEqual(self.done.rows, [])
        first_play.set()
        self.done.wait_for(5)
        self.assertEqual(self.done.rows, [1, 2, 3, 4, 5])
        self.assertEqual([row[0] for row in self.audio.rows], [1, 1, 1, 2, 2, 2, 3, 3, 3, 4, 4, 4, 5, 5, 5])
        for offset in range(0, 15, 3):
            chunks = self.audio.rows[offset:offset + 3]
            self.assertEqual([row[3:] for row in chunks], [(True, False), (False, False), (False, True)])
            self.assertTrue(all(len(pcm) / rate <= 0.2 for _, pcm, rate, _, _ in chunks))
            np.testing.assert_array_equal(np.concatenate([row[1] for row in chunks]), self.pcm)
        self.assertAlmostEqual(self.speaking.timings()[1], 65)

    def test_stop_mid_sentence_drops_queued_and_inflight_work(self):
        playing = self.hold(self.audio_holds, 1)
        synthesizing = self.hold(self.synthesis_holds, "second")
        self.speaking.say(1, "first")
        self.speaking.say(2, "second")
        self.speaking.say(3, "queued")
        self.audio.wait_for(1)
        self.engine_calls.wait_for(2)
        stopped = Event()
        dropped = []

        def stop():
            dropped.extend(self.speaking.stop())
            stopped.set()

        Thread(target=stop, daemon=True).start()
        try:
            with self.speaking._changed:
                self.assertTrue(self.speaking._changed.wait_for(lambda: not self.speaking._queue, TIMEOUT))
            self.assertFalse(stopped.wait(0.05))
        finally:
            playing.set()
        self.assertTrue(stopped.wait(TIMEOUT))
        self.assertEqual(dropped, [1, 2, 3])
        self.assertEqual(self.speaking.stop(), [])
        self.speaking.say(2, "new sentence")  # a reused ID must not revive the old synthesis
        synthesizing.set()
        self.done.wait_for(1)
        self.assertEqual(self.done.rows, [2])
        self.assertEqual(self.engine_calls.rows, ["first", "second", "new sentence"])
        self.assertEqual([row[0] for row in self.audio.rows], [1, 2, 2, 2])

    def test_failed_sentence_is_reported_and_skipped(self):
        self.speaking.say("bad", "fail")
        self.speaking.say("good", "next")
        self.done.wait_for(1)
        self.assertEqual(self.errors.rows, ["bad"])
        self.assertEqual(self.speaking.failures(), ("bad",))
        self.assertEqual(self.done.rows, ["good"])
        self.assertNotIn("bad", self.speaking.timings())
        self.speaking.close()
        self.assertTrue(self.closed.wait(TIMEOUT))


@unittest.skipUnless(os.environ.get("JARVIS_VOICE_REAL") == "1", "opt-in local model check")
class RealSpeechCheck(unittest.TestCase):
    def test_local_engines_with_one_synthetic_sentence(self):
        sentence = "The lighthouse shines across the quiet harbor."
        voice = engines.pick_voice()
        if voice.engine is None:
            self.skipTest(voice.reason)
        try:
            started = monotonic()
            pcm, rate = voice.engine.synthesize(sentence)
            print(f"{voice.name} synthesis {(monotonic() - started) * 1000:.1f} ms")
            self.assertGreater(len(pcm), 0)
        finally:
            voice.engine.close()
        positions = np.arange(len(pcm) * 16_000 // rate) * (rate / 16_000)
        audio = (np.interp(positions, np.arange(len(pcm)), pcm) / 32768).astype(np.float32)
        for engine in (engines.WhisperGpu(), engines.ParakeetCpu()):
            with self.subTest(engine=type(engine).__name__):
                try:
                    if not engine.available():
                        self.skipTest(engine.reason)
                    started = monotonic()
                    text = engine.transcribe(audio, names=())
                    print(f"{type(engine).__name__} decode {(monotonic() - started) * 1000:.1f} ms")
                    self.assertIn("lighthouse", text.casefold())
                finally:
                    engine.close()


if __name__ == "__main__":
    unittest.main()
