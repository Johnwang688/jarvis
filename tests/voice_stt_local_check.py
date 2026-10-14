"""Free checks for local speech-to-text and its cloud-fallback switch (V13).

A fake recognizer stands in for sherpa-onnx, and `llm.transcribe` is a recorder
(or raises), so nothing here needs the model, the network or a key. Temp HOME
and temp settings file throughout; the owner's `stt.json` is never read.

    HOME=$(mktemp -d) .venv/bin/python tests/voice_stt_local_check.py

The real model is exercised once by tests/voice_stt_local_real.py, gated behind
JARVIS_STT_LOCAL_REAL=1 so the normal sweep skips it.
"""
from __future__ import annotations

import io
import json
import os
import re
import stat
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis import config, llm, stt_local, stt_settings, voice  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def wav_bytes(seconds=1.0, rate=16000, channels=1, width=2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(b"\x01\x02" * int(seconds * rate) * channels if width == 2 else b"\x00" * int(seconds * rate) * channels * width)
    return buf.getvalue()


class FakeBackend:
    """Stands in for the sherpa recognizer; records concurrency and closing."""

    def __init__(self, text="local words", delay=0.0, fail=None):
        self.text, self.delay, self.fail = text, delay, fail
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.closed = False
        self._guard = threading.Lock()

    def decode(self, samples, rate):
        with self._guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.calls.append((len(samples), rate))
            if self.delay:
                time.sleep(self.delay)
            if self.fail:
                raise self.fail
            return self.text
        finally:
            with self._guard:
                self.active -= 1

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def engine_with(backend, **kw):
    built = []

    def factory(threads):
        built.append(threads)
        return backend

    kw.setdefault("schedule", False)
    eng = stt_local.Engine(factory, **kw)
    eng.built = built
    return eng


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Path(self.tmp.name) / "stt.json"
        for patch in (mock.patch.object(config, "STT_SETTINGS_PATH", self.settings),
                      mock.patch.object(config, "STT_BACKEND_DEFAULT", "cloud"),
                      mock.patch.object(config, "STT_CLOUD_FALLBACK_DEFAULT", True)):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(stt_local.set_engine, stt_local.set_engine(None))
        voice._warned.clear()

    def write(self, **values):
        self.settings.write_text(json.dumps(values))


class Cloud:
    """A recorder for llm.transcribe."""

    def __init__(self, text="cloud words", error=None):
        self.text, self.error, self.calls = text, error, []

    def __call__(self, audio, model, filename="audio.webm", mime="audio/webm", **kw):
        self.calls.append({"model": model, "mime": mime, **kw})
        if self.error:
            raise self.error
        return self.text

    def patch(self):
        return mock.patch.object(llm, "transcribe", self)


# ---------------------------------------------------------------------------------------------
class Settings(Base):
    def test_defaults_and_roundtrip(self):
        self.assertEqual(stt_settings.load(), {"backend": "cloud", "cloud_fallback": True, "error": ""})
        got = stt_settings.update(backend="local")
        self.assertEqual((got["backend"], got["cloud_fallback"]), ("local", True))
        got = stt_settings.update(cloud_fallback=False)
        self.assertEqual((got["backend"], got["cloud_fallback"], got["error"]), ("local", False, ""))
        self.assertEqual(stat.S_IMODE(self.settings.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in self.settings.parent.glob(".stt.json*")], [], "no temp left")

    def test_default_is_a_one_line_config_change(self):
        with mock.patch.object(config, "STT_BACKEND_DEFAULT", "local"):
            self.assertEqual(stt_settings.load()["backend"], "local")
        source = (ROOT / "jarvis" / "config.py").read_text()
        self.assertEqual(len(re.findall(r'^STT_BACKEND_DEFAULT = "cloud"$', source, re.M)), 1,
                         "V13 ships with the cloud default")

    def test_untrusted_file_fails_closed(self):
        for text in ("not json", "[]", '"x"', "{" * 100):
            self.settings.write_text(text)
            got = stt_settings.load()
            self.assertEqual((got["backend"], got["cloud_fallback"]), ("local", False), text)
            self.assertTrue(got["error"])
        self.settings.write_text("x" * (stt_settings.FILE_CAP + 10))
        self.assertFalse(stt_settings.load()["cloud_fallback"])
        self.write(backend="cloud", cloud_fallback="yes")
        got = stt_settings.load()
        self.assertEqual((got["backend"], got["cloud_fallback"]), ("cloud", False), "a non-boolean never means upload")
        self.write(backend="sideways")
        self.assertEqual(stt_settings.load()["backend"], "local")
        self.assertTrue(stt_settings.load()["cloud_fallback"], "an absent key is its default")

    def test_symlink_is_not_followed(self):
        target = Path(self.tmp.name) / "elsewhere.json"
        target.write_text('{"backend": "cloud", "cloud_fallback": true}')
        self.settings.symlink_to(target)
        got = stt_settings.load()
        self.assertEqual((got["backend"], got["cloud_fallback"]), ("local", False))

    def test_repair_through_one_switch_keeps_the_other_closed(self):
        self.settings.write_text("garbage")
        got = stt_settings.update(backend="auto")
        self.assertEqual((got["backend"], got["cloud_fallback"], got["error"]), ("auto", False, ""))

    def test_update_validates_before_writing(self):
        for bad in ({"backend": "wifi"}, {"cloud_fallback": 1}, {"cloud_fallback": "true"}, {"other": 1}):
            with self.assertRaises(ValueError):
                stt_settings.update(**bad)
        self.assertFalse(self.settings.exists())

    def test_unknown_keys_survive_a_write(self):
        self.write(backend="cloud", future="kept")
        stt_settings.update(cloud_fallback=False)
        self.assertEqual(json.loads(self.settings.read_text())["future"], "kept")

    def test_file_is_protected_state(self):
        from jarvis.tools import files
        protected = {p.name for p in files._protected_state_paths()}
        self.assertIn("stt.json", protected)
        self.assertIn(self.settings.resolve(), files._protected_state())


# ---------------------------------------------------------------------------------------------
class EngineChecks(Base):
    def test_lazy_load_once_with_pinned_threads(self):
        backend = FakeBackend()
        eng = engine_with(backend, threads=2)
        self.assertFalse(eng.loaded)
        self.assertEqual(eng.built, [])
        self.assertEqual(eng.transcribe(wav_bytes(), timeout=5), "local words")
        self.assertEqual(eng.transcribe(wav_bytes(), timeout=5), "local words")
        self.assertEqual((eng.built, eng.loads, eng.decodes), ([2], 1, 2))

    def test_default_threads_are_pinned_between_one_and_four(self):
        self.assertEqual(config.STT_LOCAL_THREADS, 2)
        env = {"JARVIS_STT_LOCAL_THREADS": "24"}
        import subprocess
        out = subprocess.run([sys.executable, "-c", "from jarvis import config; print(config.STT_LOCAL_THREADS)"],
                             env={**os.environ, **env, "PYTHONPATH": str(ROOT)}, capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "4", out.stderr)
        env = {"JARVIS_STT_LOCAL_THREADS": "banana"}
        out = subprocess.run([sys.executable, "-c", "from jarvis import config; print(config.STT_LOCAL_THREADS)"],
                             env={**os.environ, **env, "PYTHONPATH": str(ROOT)}, capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "2", out.stderr)

    def test_decodes_are_serialised(self):
        backend = FakeBackend(delay=0.15)
        eng = engine_with(backend)
        results, errors = [], []

        def go():
            try:
                results.append(eng.transcribe(wav_bytes(), timeout=10))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=go) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual((len(results), errors), (5, []))
        self.assertEqual(backend.max_active, 1, "two decodes never overlapped")

    def test_deadline_is_respected_and_abandoned_jobs_never_run(self):
        backend = FakeBackend(delay=1.0)
        eng = engine_with(backend)
        first = threading.Thread(target=lambda: eng.transcribe(wav_bytes(), timeout=5))
        first.start()
        while not backend.calls:
            time.sleep(0.01)
        started = time.monotonic()
        with self.assertRaises(stt_local.LocalSTTTimeout):
            eng.transcribe(wav_bytes(), timeout=0.2)      # queued behind the running decode
        self.assertLess(time.monotonic() - started, 0.6)
        first.join()
        time.sleep(0.1)
        self.assertEqual(len(backend.calls), 1, "the abandoned request was never decoded")
        self.assertEqual(eng.waiting, 0)

    def test_a_slow_decode_times_out_on_time(self):
        eng = engine_with(FakeBackend(delay=1.0))
        started = time.monotonic()
        with self.assertRaises(stt_local.LocalSTTTimeout):
            eng.transcribe(wav_bytes(), timeout=0.25)
        self.assertLess(time.monotonic() - started, 0.7)

    def test_idle_unload(self):
        clock = Clock()
        backend = FakeBackend()
        eng = engine_with(backend, idle_s=600, clock=clock)
        eng.transcribe(wav_bytes(), timeout=5)
        self.assertTrue(eng.loaded)
        clock.t += 599
        self.assertFalse(eng.reap(), "not idle long enough")
        self.assertTrue(eng.loaded)
        clock.t += 2
        self.assertTrue(eng.reap())
        self.assertFalse(eng.loaded)
        self.assertTrue(backend.closed, "the recognizer was closed so its memory can be freed")
        self.assertFalse(eng.reap(), "nothing left to unload")
        eng.transcribe(wav_bytes(), timeout=5)
        self.assertEqual((eng.loads, eng.built), (2, [config.STT_LOCAL_THREADS, config.STT_LOCAL_THREADS]))

    def test_use_postpones_the_unload(self):
        clock = Clock()
        eng = engine_with(FakeBackend(), idle_s=600, clock=clock)
        eng.transcribe(wav_bytes(), timeout=5)
        clock.t += 500
        eng.transcribe(wav_bytes(), timeout=5)
        clock.t += 500
        self.assertFalse(eng.reap())
        self.assertTrue(eng.loaded)

    def test_no_unload_during_a_decode(self):
        clock = Clock()
        backend = FakeBackend(delay=0.4)
        eng = engine_with(backend, idle_s=1, clock=clock)
        eng.transcribe(wav_bytes(), timeout=5)
        clock.t += 100
        runner = threading.Thread(target=lambda: eng.transcribe(wav_bytes(), timeout=5))
        runner.start()
        while backend.active == 0:
            time.sleep(0.01)
        self.assertFalse(eng.reap(), "a running decode is never unloaded under")
        runner.join()
        self.assertTrue(eng.loaded)

    def test_real_idle_timer_unloads(self):
        backend = FakeBackend()
        eng = stt_local.Engine(lambda t: backend, idle_s=0.2)
        eng.transcribe(wav_bytes(), timeout=5)
        deadline = time.monotonic() + 3
        while eng.loaded and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(eng.loaded)
        self.assertTrue(backend.closed)

    def test_load_failure_is_not_retried_in_a_storm(self):
        clock = Clock()
        calls = []

        def factory(threads):
            calls.append(1)
            raise RuntimeError("/home/secret/path is corrupt")

        eng = stt_local.Engine(factory, clock=clock, schedule=False)
        for _ in range(3):
            with self.assertRaises(stt_local.LocalSTTUnavailable) as caught:
                eng.transcribe(wav_bytes(), timeout=5)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("secret", str(caught.exception))
        clock.t += stt_local.LOAD_RETRY_S + 1
        with self.assertRaises(stt_local.LocalSTTUnavailable):
            eng.transcribe(wav_bytes(), timeout=5)
        self.assertEqual(len(calls), 2)

    def test_error_text_is_safe(self):
        eng = engine_with(FakeBackend(fail=RuntimeError("secret /home/x/token")))
        with self.assertRaises(stt_local.LocalSTTFailed) as caught:
            eng.transcribe(wav_bytes(), timeout=5)
        self.assertNotIn("secret", str(caught.exception))
        self.assertIn("RuntimeError", str(caught.exception))

    def test_audio_handling(self):
        backend = FakeBackend()
        eng = engine_with(backend)
        eng.transcribe(wav_bytes(1.0, rate=48000, channels=2), timeout=5)
        self.assertEqual(backend.calls[-1], (48000, 48000), "stereo is mixed to mono, rate passed through")
        for audio, mime in ((b"OggS" + b"\0" * 50, "audio/ogg"), (b"\x1aE\xdf\xa3" + b"\0" * 50, "audio/webm"),
                            (wav_bytes()[:30], "audio/wav"), (wav_bytes(width=1), "audio/wav")):
            with self.assertRaises(stt_local.LocalSTTUnavailable, msg=mime):
                eng.transcribe(audio, mime, timeout=5)
        with self.assertRaises(stt_local.LocalSTTUnavailable):
            eng.transcribe(wav_bytes(stt_local.MAX_CLIP_S + 5, rate=8000), timeout=5)
        self.assertEqual(eng.transcribe(wav_bytes(0.0), timeout=5), "", "an empty clip is empty text")
        self.assertEqual(eng.loads, 1)

    def test_status_never_loads(self):
        with mock.patch.object(stt_local, "installed", return_value=(False, "the sherpa-onnx package is not installed")):
            status = stt_local.status()
        self.assertFalse(status["installed"])
        self.assertFalse(status["loaded"])
        self.assertIn("sherpa", status["reason"])
        self.assertEqual(status["threads"], 2)

    def test_default_factory_without_the_dependency_is_unavailable_not_a_crash(self):
        eng = stt_local.Engine(schedule=False)
        with mock.patch.object(stt_local, "installed", return_value=(False, "the sherpa-onnx package is not installed")):
            with self.assertRaises(stt_local.LocalSTTUnavailable):
                eng.transcribe(wav_bytes(), timeout=5)

    def test_sherpa_is_not_imported_by_the_module_or_the_daemon_path(self):
        self.assertNotIn("sherpa_onnx", sys.modules, "an optional dependency must not be imported at import time")
        tree = (ROOT / "jarvis" / "stt_local.py").read_text()
        for line in tree.splitlines():
            if re.match(r"^(import|from)\s+sherpa", line):
                self.fail("module-level sherpa import")

    def test_pinned_download_matches_the_v0_manifest(self):
        pinned = json.loads((ROOT / "tests" / "voice_v0" / "models.json").read_text())["models"]["parakeet-tdt-0.6b-v3-int8"]
        self.assertEqual(stt_local.MODEL_SHA256, pinned["sha256"])
        self.assertEqual(stt_local.MODEL_URL, pinned["url"])

    def test_fetch_refuses_a_bad_digest(self):
        import urllib.request

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        dest = Path(self.tmp.name) / "model"
        with mock.patch.object(urllib.request, "urlopen", return_value=Resp(b"not the archive")):
            with self.assertRaises(SystemExit) as caught:
                stt_local.fetch(dest)
        self.assertIn("sha256 mismatch", str(caught.exception))
        self.assertFalse(dest.exists())
        self.assertEqual([p.name for p in Path(self.tmp.name).iterdir() if p.name.startswith(".stt-fetch")], [])


# ---------------------------------------------------------------------------------------------
class Policy(Base):
    """`voice.stt_result`: which backend, when the cloud may be used, and the budget."""

    def use(self, backend=None, **kw):
        eng = engine_with(backend or FakeBackend(), **kw)
        stt_local.set_engine(eng)
        return eng

    def test_default_is_cloud_and_never_touches_local(self):
        eng = self.use()
        cloud = Cloud()
        with cloud.patch():
            for prefer in (False, True):
                got = voice.stt_result(b"RIFFx", "audio/wav", prefer_local=prefer)
                self.assertEqual((got.text, got.backend, got.fallback), ("cloud words", "cloud", None))
        self.assertEqual((eng.loads, eng.decodes), (0, 0))
        self.assertEqual(len(cloud.calls), 2)

    def test_local_serves_and_the_cloud_is_never_called(self):
        for fallback in (True, False):
            self.write(backend="local", cloud_fallback=fallback)
            eng = self.use()
            boom = Cloud(error=AssertionError("audio left the machine"))
            with boom.patch():
                got = voice.stt_result(wav_bytes(), "audio/wav", prefer_local=True, budget=15.0)
            self.assertEqual((got.text, got.backend, got.fallback), ("local words", "local", None), fallback)
            self.assertIn("local", got.model)
            self.assertEqual((boom.calls, eng.decodes), ([], 1))

    def test_local_only_never_calls_llm_transcribe_even_when_local_breaks(self):
        self.write(backend="local", cloud_fallback=False)
        for failure in (FakeBackend(fail=RuntimeError("x")), FakeBackend(delay=2.0)):
            self.use(failure)
            with mock.patch.object(llm, "transcribe", side_effect=AssertionError("audio left the machine")) as spy:
                started = time.monotonic()
                with self.assertRaises(voice.STTUnavailable) as caught:
                    voice.stt(wav_bytes(), "audio/wav", prefer_local=True, budget=2.0)
            self.assertFalse(spy.called)
            self.assertIn("nothing was uploaded", str(caught.exception))
            self.assertNotIsInstance(caught.exception, voice.STTBlocked)
            self.assertLess(time.monotonic() - started, 3.0, "local gets the whole budget, not more")
        stt_local.set_engine(None)
        with mock.patch.object(llm, "transcribe", side_effect=AssertionError("audio left the machine")) as spy, \
                mock.patch.object(stt_local, "installed", return_value=(False, "the sherpa-onnx package is not installed")):
            with self.assertRaises(voice.STTUnavailable) as caught:
                voice.stt(wav_bytes(), "audio/wav", prefer_local=True)
        self.assertFalse(spy.called)
        self.assertIn("not set up", str(caught.exception))

    def test_fallback_on_local_failure_goes_to_the_cloud_with_the_reason(self):
        self.write(backend="local", cloud_fallback=True)
        self.use(FakeBackend(fail=RuntimeError("boom")))
        cloud = Cloud()
        with cloud.patch():
            got = voice.stt_result(wav_bytes(), "audio/wav", prefer_local=True, budget=15.0)
        self.assertEqual((got.text, got.backend), ("cloud words", "cloud"))
        self.assertIn("local decode failed", got.fallback)
        self.assertNotIn("boom", got.fallback)
        self.assertEqual(len(cloud.calls), 1)

    def test_local_timeout_leaves_the_cloud_time_inside_the_budget(self):
        self.write(backend="local", cloud_fallback=True)
        self.use(FakeBackend(delay=3.0))
        cloud = Cloud()
        started = time.monotonic()
        with mock.patch.object(config, "STT_LOCAL_BUDGET_SHARE", 0.1), cloud.patch():
            got = voice.stt_result(wav_bytes(), "audio/wav", prefer_local=True, budget=12.0,
                                   timeout=20.0, max_retries=2)
        took = time.monotonic() - started
        self.assertEqual(got.backend, "cloud")
        self.assertIn("ran out of time", got.fallback)
        self.assertLess(took, 2.0, "local was cut off at its share, not left to eat the deadline")
        # The cloud chain was handed only what was left of the 12 s budget (11 s after the margin,
        # about 1.1 s spent): its two models share about 9.9 s.
        call = cloud.calls[0]
        self.assertLess(call["timeout"], 5.0)
        self.assertGreater(call["timeout"], 3.0)

    def test_local_share_applies_only_with_a_fallback_and_a_budget(self):
        seen = []

        class Spy(stt_local.Engine):
            def transcribe(self, audio, mime="audio/wav", *, timeout):
                seen.append(timeout)
                return "x"

        stt_local.set_engine(Spy(lambda t: FakeBackend(), schedule=False))
        self.write(backend="local", cloud_fallback=True)
        voice.stt_result(b"RIFFx", "audio/wav", prefer_local=True, budget=15.0)
        voice.stt_result(b"RIFFx", "audio/wav", prefer_local=True)
        self.write(backend="local", cloud_fallback=False)
        voice.stt_result(b"RIFFx", "audio/wav", prefer_local=True, budget=15.0)
        share, no_budget, whole = seen
        self.assertAlmostEqual(share, (15.0 - voice.STT_BUDGET_MARGIN_S) * config.STT_LOCAL_BUDGET_SHARE, delta=0.2)
        self.assertEqual(no_budget, config.STT_LOCAL_TIMEOUT_S)
        self.assertAlmostEqual(whole, 15.0 - voice.STT_BUDGET_MARGIN_S, delta=0.2)

    def test_auto(self):
        self.write(backend="auto", cloud_fallback=True)
        cloud = Cloud()
        with mock.patch.object(stt_local, "installed", return_value=(False, "no")), cloud.patch():
            got = voice.stt_result(wav_bytes(), "audio/wav", prefer_local=True)
        self.assertEqual((got.backend, got.fallback), ("cloud", None), "not installed: the cloud, quietly")
        self.use()
        boom = Cloud(error=AssertionError("audio left the machine"))
        with boom.patch():
            self.assertEqual(voice.stt_result(wav_bytes(), "audio/wav", prefer_local=True).backend, "local")
        self.write(backend="auto", cloud_fallback=False)
        stt_local.set_engine(None)
        with mock.patch.object(stt_local, "installed", return_value=(False, "no")), boom.patch():
            with self.assertRaises(voice.STTBlocked):
                voice.stt_result(wav_bytes(), "audio/wav", prefer_local=True)
        self.assertEqual(boom.calls, [])

    def test_cloud_switched_off_blocks_every_caller_and_uploads_nothing(self):
        self.write(backend="cloud", cloud_fallback=False)
        eng = self.use()
        boom = Cloud(error=AssertionError("audio left the machine"))
        with boom.patch():
            for prefer in (False, True):
                with self.assertRaises(voice.STTBlocked):
                    voice.stt(wav_bytes(), "audio/wav", prefer_local=prefer)
            self.write(backend="local", cloud_fallback=False)
            with self.assertRaises(voice.STTBlocked):
                voice.stt(wav_bytes(), "audio/wav")        # a caller that did not opt in (Discord, face)
        self.assertEqual((boom.calls, eng.decodes), ([], 0))

    def test_callers_that_do_not_opt_in_are_unchanged(self):
        """Discord voice notes and the face server call `voice.stt(audio, mime=)`: cloud, whatever the backend."""
        self.write(backend="local", cloud_fallback=True)
        eng = self.use()
        cloud = Cloud()
        with cloud.patch():
            self.assertEqual(voice.stt(b"OggSdata", mime="audio/ogg"), "cloud words")
            self.assertEqual(voice.stt(b"RIFFwav", mime="audio/wav"), "cloud words")
        self.assertEqual(eng.loads, 0)
        self.assertEqual([c["mime"] for c in cloud.calls], ["audio/ogg", "audio/wav"])
        for rel in ("jarvis/discord_gateway.py", "jarvis/v2/discord/gateway.py", "jarvis/face/server.py"):
            text = (ROOT / rel).read_text()
            self.assertIn("voice.stt(", text)
            self.assertNotIn("prefer_local", text, f"{rel} must not opt in without a decision")
        self.assertIn("prefer_local=True", (ROOT / "jarvis" / "v2" / "hud_api.py").read_text())

    def test_stt_wrapper_is_the_old_signature(self):
        import inspect
        self.assertEqual(list(inspect.signature(voice.stt).parameters),
                         ["audio", "mime", "timeout", "max_retries", "budget", "prefer_local"])
        with Cloud().patch():
            self.assertIsInstance(voice.stt(b"x"), str)

    def test_cloud_budget_unchanged_when_local_was_not_tried(self):
        cloud = Cloud()
        with cloud.patch():
            voice.stt_result(b"RIFFx", "audio/wav", timeout=20.0, max_retries=2, budget=15.0, prefer_local=True)
        # The V2 fit, untouched: the 14 s that follow the margin, shared over the two models.
        expected = voice._fit_budget(15.0 - voice.STT_BUDGET_MARGIN_S, 2, 2, 20.0)
        self.assertAlmostEqual(cloud.calls[0]["timeout"], expected[0], delta=0.05)
        self.assertEqual(cloud.calls[0]["max_retries"], expected[1])

    def test_stt_result_needs_audio(self):
        with self.assertRaises(ValueError):
            voice.stt_result(b"", prefer_local=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
