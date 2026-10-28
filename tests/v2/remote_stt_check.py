"""Push to talk from the phone (phone surface design §5 `POST stt`, §6;
work package P-7): `jarvis/v2/remote_stt.py`, the listener's per-path body
limit for it (`remote_listener._limits`) and the desk's shared dictation
(`hud_api.dictate`).

Free and hermetic, as tests/v2/remote_answer_check.py (whose `Phone` base it
reuses): a real daemon on ephemeral loopback ports (never 8402, 8403, 8405 or
8408), temporary HOME, data, speech-to-text settings and speaker store, a
fake local speech engine (never a real model) and a spy in place of the
cloud call (never the network).

What it pins (the package's acceptance items):

  - 401 without a session, 403 cross-origin, 400 bad Host; 404 on the API
    and HUD listeners;
  - only `audio/wav` (415 otherwise), only a 16 kHz mono 16-bit PCM WAV of
    1 sample to 60 s (400 otherwise), at most `BODY_CAP` (413); a clip larger
    than the listener's usual 64 KiB is read only for a signed-in request;
  - the session's rate (12 a minute) and one clip at a time (429);
  - the desk's own path: speaker verification first, then the owner's
    switches; local-only never calls the cloud, and a missing local engine
    with the fallback off is a refusal that uploads nothing;
  - the reply is `{text}` only; the words are never sent, delivered or
    taken as an answer (an "allow" spoken leaves the approval pending);
  - nothing of the clip is written to disk, on success or on error;
  - nothing of the clip or its words is logged.

Run: PYTHONPATH=. .venv/bin/python tests/v2/remote_stt_check.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import logging
import math
import os
from pathlib import Path
import random
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rnc = _load("remote_answer_check")
rac = rnc.rac

from jarvis import config, llm, speaker, stt_local, voice  # noqa: E402
from jarvis.v2 import hud_api, remote_listener, remote_stt  # noqa: E402

HOST, ORIGIN = rac.HOST, rac.ORIGIN
WORDS = "remember the milk 7f3a"


def wav(seconds=1.0, *, rate=16000, channels=1, width=2, seed=1) -> bytes:
    """A tone with seeded noise (so its bytes are unique to the test)."""
    rng = random.Random(seed)
    frames = int(seconds * rate)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        if width == 2:
            samples = bytearray()
            for i in range(frames):
                v = int(8000 * math.sin(2 * math.pi * 220 * i / rate)) + rng.randint(-500, 500)
                samples += int(max(-32768, min(32767, v))).to_bytes(2, "little", signed=True) * channels
            w.writeframes(bytes(samples))
        else:
            w.writeframes(bytes(rng.randint(0, 255) for _ in range(frames * channels * width)))
    return out.getvalue()


class LocalBackend:
    """The fake local recognizer `stt_local.Engine` drives."""

    def __init__(self, text=WORDS, error=None, gate=None):
        self.text, self.error, self.gate = text, error, gate
        self.calls = []

    def decode(self, samples, rate):
        self.calls.append((len(samples), rate))
        if self.gate is not None:
            self.gate.wait(5)
        if self.error:
            raise self.error
        return self.text

    def close(self):
        pass


class Cloud:
    """A spy in place of `llm.transcribe`: what would leave the machine."""

    def __init__(self, text="cloud words"):
        self.text, self.calls = text, []

    def __call__(self, audio, model, filename="audio.webm", mime="audio/webm", **kw):
        self.calls.append({"model": model, "mime": mime, "bytes": len(audio)})
        return self.text


class Base(rnc.Phone):
    unlimited = False

    def setUp(self):
        super().setUp()
        self.settings = self.root / "stt.json"
        for name, value in dict(STT_SETTINGS_PATH=self.settings, SPEAKER_DIR=self.root / "speaker",
                                STT_BACKEND_DEFAULT="local", STT_CLOUD_FALLBACK_DEFAULT=False).items():
            guard = patch.object(config, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        self.backend = LocalBackend()
        self.addCleanup(stt_local.set_engine, stt_local.set_engine(
            stt_local.Engine(lambda threads: self.backend, schedule=False)))
        self.cloud = Cloud()
        guard = patch.object(llm, "transcribe", self.cloud)
        guard.start()
        self.addCleanup(guard.stop)
        clock = rac.Clock(1000.0)
        guard = patch.object(remote_stt, "clock", clock)
        guard.start()
        self.addCleanup(guard.stop)
        self.rate_clock = clock
        voice._warned.clear()

    def setting(self, **values):
        self.settings.write_text(json.dumps(values))

    def clip(self, data: bytes | None = None, *, cookie=None, mime="audio/wav", host=HOST, origin=ORIGIN,
             path=remote_stt.PATH, method="POST", chunked=False):
        data = wav() if data is None else data
        if self.unlimited:          # these tests are not about the rate
            self.rate_clock.now += remote_stt.RATE_S
        headers = [("Host", host)]
        if origin is not None:
            headers.append(("Origin", origin))
        if cookie is None:
            cookie = self.cookie
        if cookie:
            headers.append(("Cookie", cookie))
        if mime is not None:
            headers.append(("Content-Type", mime))
        if chunked:
            head = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers)
            piece = 60000
            body = b"".join(b"%x\r\n%s\r\n" % (len(data[i:i + piece]), data[i:i + piece])
                            for i in range(0, len(data), piece)) + b"0\r\n\r\n"
            answer = rac.send_bytes(self.port, (head + "Transfer-Encoding: chunked\r\n\r\n").encode() + body)
            status = int(answer.split(b" ", 2)[1])
            payload = answer.split(b"\r\n\r\n", 1)[1]
            return status, (json.loads(payload) if payload else None)
        status, _, payload = rac.raw(self.port, method, path, headers, data, timeout=30)
        return status, (json.loads(payload) if payload else None)


class Access(Base):
    unlimited = True

    def test_a_session_on_the_remote_listener_and_nothing_else(self):
        data = wav()
        self.assertEqual(self.clip(data, cookie=""), (401, {"error": "unauthorized"}))
        self.assertEqual(self.clip(data, cookie=self.cookie + "x")[0], 401)
        self.assertEqual(self.clip(data, origin="https://evil.ts.net")[0], 403)
        self.assertEqual(self.clip(data, origin=None)[0], 403)
        self.assertEqual(self.clip(data, host="evil.ts.net")[0], 400)
        status, body = self.clip(data, method="GET", mime=None)
        self.assertEqual(status, 405, body)
        self.assertEqual(self.clip(data, path=remote_stt.PATH + "?x=1")[0], 400)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.cloud.calls, [])
        # The API and HUD listeners: their own Host and Origin, a session cookie, and still 404.
        for port in (self.d.port, self.d.face_port):
            host = f"127.0.0.1:{port}"
            for mime, want in (("audio/wav", (400, 404)), ("application/json", (404,))):
                status, _, got = rac.raw(port, "POST", remote_stt.PATH,
                                         [("Host", host), ("Origin", f"http://{host}"), ("Cookie", self.cookie),
                                          ("Content-Type", mime)], data)
                self.assertIn(status, want, (port, mime, got))
        self.assertEqual(self.backend.calls, [])
        status, body = self.clip(data)
        self.assertEqual((status, body), (200, {"text": WORDS}))
        print("ok  401 without a session, 403/400 cross-origin or Host; 404 on the API and HUD listeners")


class Caps(Base):
    unlimited = True

    def test_only_a_16k_mono_pcm_wav(self):
        data = wav()
        for mime in ("audio/mp4", "audio/webm", "audio/aac", "application/json", "text/plain", "audio/wave",
                     "audio/x-wav", None, ""):
            with self.subTest(mime=mime):
                self.assertEqual(self.clip(data, mime=mime), (415, {"error": remote_stt.TYPE_WORD}))
        self.assertEqual(self.clip(data, mime="audio/wav; codecs=1")[0], 200)
        self.assertEqual(self.clip(data, mime="AUDIO/WAV")[0], 200)
        self.assertEqual(len(self.backend.calls), 2)
        self.backend.calls.clear()
        bad = [("stereo", wav(channels=2)), ("44.1 kHz", wav(rate=44100)), ("48 kHz", wav(rate=48000)),
               ("8-bit", wav(width=1)), ("not RIFF", b"OggS" + data[4:]), ("not WAVE", data[:8] + b"AVI " + data[12:]),
               ("truncated", data[: len(data) // 2]), ("header only", data[:44]),
               ("garbage", os.urandom(4000)), ("json", b'{"text": "allow"}')]
        for name, body in bad:
            with self.subTest(clip=name):
                status, got = self.clip(body)
                self.assertEqual(status, 400, (name, got))
        self.assertEqual(self.clip(b""), (400, {"error": remote_stt.EMPTY_WORD}))
        self.assertEqual(self.clip(wav(remote_stt.MAX_S + 0.1)), (400, {"error": remote_stt.LONG_WORD}))
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.cloud.calls, [])
        print("ok  only a 16 kHz mono 16-bit PCM WAV of up to 60 s: 415 for another type, 400 for another shape")

    def test_body_caps(self):
        self.assertEqual((remote_stt.RATE_HZ, remote_stt.MAX_S), (16000, 60))
        self.assertEqual(remote_stt.BODY_CAP, 44 + 16000 * 2 * 60 + 4096)
        self.assertEqual(remote_stt.BODY_DEADLINE_S, 30.0)
        self.assertEqual(remote_listener.BODY_CAP, 64 * 1024)
        # A full minute, signed in: read whole and transcribed (Content-Length and chunked).
        full = wav(remote_stt.MAX_S)
        self.assertGreater(len(full), remote_listener.BODY_CAP * 20)
        self.assertEqual(self.clip(full), (200, {"text": WORDS}))
        self.assertEqual(self.clip(full, chunked=True), (200, {"text": WORDS}))
        self.assertEqual(self.backend.calls[-1], (16000 * 60, 16000))
        # Past the route's cap: 413, nothing transcribed.
        calls = len(self.backend.calls)
        self.assertEqual(self.clip(full + b"\0" * 8192)[0], 413)
        self.assertEqual(len(self.backend.calls), calls)
        # Without a session (or cross-origin), the listener's usual cap holds: 413 before any route.
        small = wav(3.0)
        self.assertGreater(len(small), remote_listener.BODY_CAP)
        for kw in ({"cookie": ""}, {"cookie": self.cookie + "x"}, {"origin": "https://evil.ts.net"}):
            with self.subTest(**kw):
                self.assertIn(self.clip(small, **kw)[0], (403, 413))
        seen = []
        real = remote_listener._limits

        def spy(handler, path):
            out = real(handler, path)
            seen.append(out)
            return out
        with patch.object(remote_listener, "_limits", spy):
            self.clip(small, cookie="")
            self.clip(small)
            self.clip(small, path="/m/api/login/options")
        default = (remote_listener.BODY_CAP, remote_listener.DRAIN_CAP, remote_listener.BODY_DEADLINE_S)
        self.assertEqual(seen, [default, (remote_stt.BODY_CAP, remote_stt.BODY_CAP + remote_listener.DRAIN_CAP,
                                          remote_stt.BODY_DEADLINE_S), default])
        self.assertEqual(len(self.backend.calls), calls + 1)
        print("ok  caps: a minute's clip read whole only past the owner test; 413 past it; 64 KiB for everyone else")

    def test_rate_and_one_at_a_time(self):
        self.unlimited = False
        self.assertEqual((remote_stt.RATE, remote_stt.RATE_S), (12, 60.0))
        data = wav(0.5)
        for _ in range(remote_stt.RATE):
            self.assertEqual(self.clip(data)[0], 200)
        self.assertEqual(self.clip(data), (429, {"error": remote_stt.RATE_WORD}))
        self.rate_clock.now += remote_stt.RATE_S
        self.assertEqual(self.clip(data)[0], 200)
        # A refused clip still counts (the rate is on uploads, not successes).
        self.rate_clock.now += remote_stt.RATE_S
        for _ in range(remote_stt.RATE):
            self.assertEqual(self.clip(b"nope")[0], 400)
        self.assertEqual(self.clip(data)[0], 429)
        self.rate_clock.now += remote_stt.RATE_S
        # One at a time per session: a second clip while the first is decoding is refused.
        gate = threading.Event()
        self.backend.gate = gate
        out = {}
        first = threading.Thread(target=lambda: out.setdefault("first", self.clip(data)), daemon=True)
        first.start()
        rnc.eventually(lambda: len(self.backend.calls) >= 1 + remote_stt.RATE + 1, what="the first decode")
        self.assertEqual(self.clip(data), (429, {"error": remote_stt.BUSY_WORD}))
        gate.set()
        first.join(10)
        self.assertEqual(out["first"], (200, {"text": WORDS}))
        self.backend.gate = None
        self.assertEqual(self.clip(data)[0], 200)
        print("ok  12 clips a minute per session, one at a time (429)")


class DeskPath(Base):
    def test_local_only_never_calls_the_cloud(self):
        self.setting(backend="local", cloud_fallback=False)
        self.assertEqual(self.clip(), (200, {"text": WORDS}))
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(self.cloud.calls, [])
        # The local engine fails: refused, the desk's words, nothing uploaded.
        self.backend.error = RuntimeError("model detail")
        status, body = self.clip()
        self.assertEqual(status, 502, body)
        self.assertIn("cloud fallback is off, so nothing was uploaded", body["error"])
        self.assertNotIn("model detail", body["error"])
        self.assertEqual(self.cloud.calls, [])
        # No local engine at all (not installed): the same, never the cloud.
        stt_local.set_engine(None)
        with patch.object(stt_local, "installed", return_value=(False, "the sherpa-onnx package is not installed")):
            status, body = self.clip()
            self.assertIn(status, (502, 409), body)
            self.assertIn("nothing was uploaded", body["error"])
            self.setting(backend="auto", cloud_fallback=False)
            self.assertEqual(self.clip(), (409, {"error": "local speech-to-text is not installed and cloud fallback is off, "
                                                         "so nothing was uploaded (install it, or switch cloud fallback on)"}))
        self.setting(backend="cloud", cloud_fallback=False)
        status, body = self.clip()
        self.assertEqual(status, 409, body)
        self.assertEqual(self.cloud.calls, [])
        # An untrustworthy settings file reads as local, no upload (stt_settings fails closed).
        self.settings.write_text("{not json")
        self.backend.error = RuntimeError("x")
        stt_local.set_engine(stt_local.Engine(lambda threads: self.backend, schedule=False))
        self.assertEqual(self.clip()[0], 502)
        self.assertEqual(self.cloud.calls, [])
        print("ok  local only: the phone's clip never reaches the cloud; the refusal says nothing was uploaded")

    def test_cloud_when_the_desk_allows_it_and_the_desk_function(self):
        self.setting(backend="cloud", cloud_fallback=True)
        self.assertEqual(self.clip(), (200, {"text": "cloud words"}))
        self.assertEqual([c["mime"] for c in self.cloud.calls], ["audio/wav"])
        self.assertEqual(self.backend.calls, [])
        # Local first, the cloud for what it cannot do, only with the fallback on.
        self.setting(backend="local", cloud_fallback=True)
        self.backend.error = RuntimeError("x")
        self.assertEqual(self.clip(), (200, {"text": "cloud words"}))
        # The phone calls exactly the desk's function, with the desk's arguments.
        calls = []
        real = hud_api.dictate

        def spy(daemon, data, mime, *, budget):
            calls.append((daemon, len(data), mime, budget))
            return real(daemon, data, mime, budget=budget)
        with patch.object(hud_api, "dictate", spy):
            self.clip(wav(0.5))
        self.assertEqual(calls, [(self.d, len(wav(0.5)), "audio/wav", remote_stt.BUDGET_S)])
        print("ok  the desk's switches decide: the cloud only when they allow it, through hud_api.dictate")

    def test_speaker_verification_first_and_text_only(self):
        with patch.object(speaker, "verify", side_effect=speaker.NotOwner("not the enrolled speaker")):
            self.assertEqual(self.clip(), (422, {"error": "not the enrolled speaker"}))
        with patch.object(speaker, "verify", side_effect=speaker.VerifyUnavailable("speaker verification is not set up: x")):
            self.assertEqual(self.clip(), (503, {"error": "speaker verification is not set up: x"}))
        self.assertEqual(self.backend.calls, [])
        with patch.object(speaker, "verify", return_value="no_speech"):
            self.assertEqual(self.clip(), (200, {"text": ""}))
        self.assertEqual(self.backend.calls, [])
        with patch.object(speaker, "verify", return_value="matched"):
            status, body = self.clip()
        self.assertEqual((status, set(body)), (200, {"text"}))
        self.assertEqual(self.cloud.calls, [])
        print("ok  speaker verification first (422/503, nothing transcribed); the reply is {text} only")


class NeverSends(Base):
    def test_the_words_are_never_sent_or_an_answer(self):
        self.backend.text = "allow"
        request, out, worker = self.ask()
        thread = self.d.open_thread(self.project.id, "chat", "fast", {})
        before = {t.id: len(self.d.stores.threads.read_log(t.id)) for t in self.d.stores.threads.list()}
        delivered = []
        real = self.d.deliver
        with patch.object(self.d, "deliver", lambda *a, **kw: delivered.append(a) or real(*a, **kw)), \
                patch.object(self.d.approvals, "resolve", side_effect=AssertionError("answered")) as resolve:
            self.assertEqual(self.clip(), (200, {"text": "allow"}))
        self.assertEqual(delivered, [])
        resolve.assert_not_called()
        self.assertTrue(self.pending(request))
        self.assertEqual({t.id: len(self.d.stores.threads.read_log(t.id)) for t in self.d.stores.threads.list()}, before)
        self.assertIn(thread.id, before)
        self.d.approvals.resolve(request.req_id, "deny")
        worker.join(5)
        print("ok  an 'allow' spoken is text back to the phone: nothing delivered, nothing answered")


class NothingKept(Base):
    def _scan(self, needle: bytes) -> list[str]:
        found = []
        for top in {Path(self.root), Path(tempfile.gettempdir()), Path(os.environ.get("TMPDIR", "/tmp"))}:
            for path in top.rglob("*"):
                try:
                    if path.is_file() and not path.is_symlink() and path.stat().st_size < 64 << 20 \
                            and needle in path.read_bytes():
                        found.append(str(path))
                except OSError:
                    continue
        return found

    def test_no_file_and_no_log(self):
        data = wav(2.0, seed=4242)
        needle = data[1000:1064]
        records = []

        class Keep(logging.Handler):
            def emit(self, record):
                records.append(self.format(record))
        keep = Keep(logging.DEBUG)
        root = logging.getLogger()
        old = root.level
        root.addHandler(keep)
        root.setLevel(logging.DEBUG)
        logging.disable(logging.NOTSET)
        try:
            self.assertEqual(self.clip(data), (200, {"text": WORDS}))
            self.backend.error = RuntimeError("decode failed " + WORDS)
            self.assertEqual(self.clip(data)[0], 502)
            self.backend.error = None
            with patch.object(speaker, "verify", side_effect=speaker.NotOwner("not the enrolled speaker")):
                self.assertEqual(self.clip(data)[0], 422)
            self.assertEqual(self.clip(data[:300])[0], 400)
        finally:
            root.removeHandler(keep)
            root.setLevel(old)
        self.assertEqual(self._scan(needle), [], "the clip was written to disk")
        text = "\n".join(records)
        self.assertNotIn(WORDS, text)
        self.assertNotIn(str(len(data)), text)
        self.assertNotIn("RIFF", text)
        self.assertNotIn(needle.hex()[:32], text)
        stt_lines = [r for r in records if "dictation" in r]
        self.assertEqual(len(stt_lines), 4, records)
        print("ok  nothing of the clip on disk or in a log line (success, decode error, refusal, malformed)")


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main(verbosity=1)
