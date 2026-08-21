"""Checks for the Pocket TTS backend and custom voices. Free — no torch,
no model, no API.

A fake `pocket_tts` module is injected into sys.modules (jarvis/pocket.py
never imports the real one at module level, and `available()` checks
sys.modules first for exactly this reason). Its sample rate is deliberately
16000 — not Kokoro's 24000 — so anything that hardcodes a rate fails here.

The cases that matter are the degradations, written to fail against the
pre-pocket code: a `pocket:` name must route to the pocket backend, and
every failure — a raising model, a missing store entry, the library absent,
even a `JARVIS_TTS_VOICE=pocket:x` loop — must land on a Kokoro name and
leave him audible, with the cloud never once seeing a `pocket:` name.

Run:  .venv/bin/python tests/voice_pocket_check.py
"""

from __future__ import annotations

import http.client
import importlib.util
import io
import json
import queue
import sys
import tempfile
import types
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from jarvis import config

# Pin the avatar and every state directory before anything imports voice —
# the same rule as voice_local_check: a suite must not read (or write!) the
# owner's real avatars, allowlist, or voice store.
_TMP = tempfile.TemporaryDirectory()
config.AVATARS_DIR = Path(_TMP.name) / "avatars"
config.AVATAR_STATE_PATH = Path(_TMP.name) / "state" / "avatar.json"
config.VOICES_DIR = Path(_TMP.name) / "voices"
config.ALLOWLIST_PATH = Path(_TMP.name) / "allowlist.json"
config.AVATARS_DIR.mkdir(parents=True)
config.AVATAR_ENV = "jarvis"

from jarvis import pocket, voice  # noqa: E402

# Whether the *real* pocket-tts is importable decides which absence checks
# can run: once the owner installs the extra, "remove the fake" no longer
# makes the library unavailable, and pretending otherwise would test a lie.
REAL_INSTALLED = importlib.util.find_spec("pocket_tts") is not None

RATE = 16000
PORT = 8442


# ---- the fake library --------------------------------------------------------


class _FakeAudio:
    """What generate_audio returns: enough tensor to survive
    .detach().cpu().numpy()."""

    def __init__(self, arr):
        self._arr = arr

    def detach(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return self._arr


class _FakeModel:
    sample_rate = RATE

    def __init__(self):
        self.prompts: list[str] = []
        self.spoken: list[str] = []
        self.boom = False

    def get_state_for_audio_prompt(self, prompt):
        self.prompts.append(str(prompt))
        return {"prompt": str(prompt)}

    def generate_audio(self, state, text):
        if self.boom:
            raise RuntimeError("synthetic pocket failure")
        self.spoken.append(text)
        pad = np.zeros(int(RATE * 0.2), dtype="float32")
        tone = (np.sin(np.linspace(0, 300, int(RATE * 0.8))) * 0.4).astype("float32")
        return _FakeAudio(np.concatenate([pad, tone, pad]))


def install_fake() -> _FakeModel:
    """Build the one fake module + model the whole suite shares. The checks
    that count loads/prompts run before any uninstall churn (main() order)."""
    mod = types.ModuleType("pocket_tts")
    model = _FakeModel()

    class TTSModel:
        loads = 0

        @classmethod
        def load_model(cls):
            cls.loads += 1
            return model

    def export_model_state(state, path):
        Path(path).write_bytes(b"FAKE-STATE:" + json.dumps(state).encode())

    mod.TTSModel = TTSModel
    mod.export_model_state = export_model_state
    _FAKE["module"], _FAKE["model"] = mod, model
    sys.modules["pocket_tts"] = mod
    _reset()
    return model


_FAKE: dict = {}


def uninstall_fake() -> None:
    sys.modules.pop("pocket_tts", None)
    _reset()


def reinstall_fake() -> None:
    """Put the same module object back — a fresh one would orphan the model
    the earlier checks hold counters on."""
    sys.modules["pocket_tts"] = _FAKE["module"]
    _reset()


def _reset() -> None:
    pocket._available = None
    pocket._model_instance = None
    pocket._states.clear()


def make_avatar(slug: str, meta: dict) -> None:
    path = config.AVATARS_DIR / slug
    path.mkdir(parents=True, exist_ok=True)
    (path / "avatar.json").write_text(json.dumps(meta), encoding="utf-8")


def make_ref_wav(path: Path, seconds: float = 1.0, rate: int = 24000) -> None:
    tone = (np.sin(np.linspace(0, 800, int(rate * seconds))) * 0.3).astype("float32")
    path.write_bytes(voice._encode_wav(tone, rate))


# ---- checks ------------------------------------------------------------------


def store_checks(model: _FakeModel) -> None:
    """clone/mix/rm against the temp store, model faked throughout."""
    ref = Path(_TMP.name) / "sample.wav"
    make_ref_wav(ref)

    target = pocket.clone("dad", [ref])
    assert (target / "voice.json").is_file() and (target / "ref.wav").is_file()
    assert (target / "state.safetensors").read_bytes().startswith(b"FAKE-STATE:")
    meta = json.loads((target / "voice.json").read_text())
    assert meta["kind"] == "clone" and meta["created"], meta
    assert pocket.known("dad") and not pocket.known("ghost")
    assert [v["name"] for v in pocket.custom_voices()] == ["dad"]
    print("ok  store: clone writes state + ref + metadata, and the catalog sees it")

    for bad in ("dad", "alba"):
        try:
            pocket.clone(bad, [ref])
            raise AssertionError(f"clone({bad!r}) should have refused")
        except ValueError:
            pass
    for evil in ("../evil", "no/slash", "Upper", ""):
        try:
            pocket.clone(evil, [ref])
            raise AssertionError(f"clone({evil!r}) should have refused")
        except ValueError:
            pass
    assert not any(p.name.startswith(".") for p in config.VOICES_DIR.iterdir()), (
        "a refused clone left a half-voice behind"
    )
    print("ok  store: collisions, builtins and hostile names are refused cleanly")

    # A multi-file clone concatenates — and stops at the 30s prompt window.
    a, b = Path(_TMP.name) / "a.wav", Path(_TMP.name) / "b.wav"
    make_ref_wav(a, seconds=1.0, rate=24000)
    make_ref_wav(b, seconds=1.0, rate=16000)  # resampled to a's rate
    target = pocket.clone("duo", [a, b])
    with wave.open(str(target / "ref.wav")) as wav_in:
        seconds = wav_in.getnframes() / wav_in.getframerate()
        assert wav_in.getframerate() == 24000, "concat must use the first file's rate"
    assert 1.9 < seconds < 2.1, seconds
    assert model.prompts[-1].endswith("ref.wav"), model.prompts[-1]
    print("ok  store: a multi-file clone concatenates and resamples the references")

    # A quiet take must not become a quiet voice: the model carries the
    # prompt's loudness into everything it synthesizes, so the stored
    # reference is normalized (the 2026-08-21 laptop-mic case — written to
    # fail against the copy-the-file-untouched code).
    quiet = Path(_TMP.name) / "quiet.wav"
    tone = (np.sin(np.linspace(0, 800, 24000)) * 0.008).astype("float32")
    quiet.write_bytes(voice._encode_wav(tone, 24000))
    target = pocket.clone("whisper", [quiet])
    with wave.open(str(target / "ref.wav")) as wav_in:
        x = np.frombuffer(
            wav_in.readframes(wav_in.getnframes()), dtype="<i2"
        ).astype("float32") / 32767.0
    rms = float(np.sqrt((x**2).mean()))
    assert 0.06 < rms < 0.1, rms
    assert float(np.abs(x).max()) <= 0.96
    pocket.remove("whisper")

    # ...and a file the stdlib cannot decode is handed through untouched.
    fake_mp3 = Path(_TMP.name) / "take.mp3"
    fake_mp3.write_bytes(b"\xff\xfbNOT-REALLY-MP3")
    target = pocket.clone("tape", [fake_mp3])
    assert (target / "ref.mp3").read_bytes() == fake_mp3.read_bytes()
    pocket.remove("tape")
    print("ok  store: a quiet reference is normalized to speech level; "
          "an undecodable file passes through untouched")

    # mix() orchestration, with the tensor I/O stubbed out — the real
    # safetensors round-trip needs torch and belongs to the live smoke.
    real_flat, real_save = pocket._flat_state, pocket._save_state
    pocket._flat_state = lambda name: {"k": np.ones(4, dtype="float32")}
    pocket._save_state = lambda tensors, path: Path(path).write_bytes(b"MIXED")
    try:
        target = pocket.mix("blend", {"dad": 0.5, "alba": 0.5})
        meta = json.loads((target / "voice.json").read_text())
        assert meta["kind"] == "hybrid" and meta["weights"] == {"dad": 0.5, "alba": 0.5}
        assert (target / "state.safetensors").read_bytes() == b"MIXED"
        try:
            pocket.mix("solo", {"dad": 1.0})
            raise AssertionError("a one-voice mix should have refused")
        except ValueError:
            pass
    finally:
        pocket._flat_state, pocket._save_state = real_flat, real_save
    print("ok  store: mix writes hybrid metadata and refuses a one-voice blend")

    pocket._states["duo"] = "stale-cached-state"
    target = pocket.rename("duo", "pair")
    meta = json.loads((target / "voice.json").read_text())
    assert meta["name"] == "pair", meta
    assert pocket.known("pair") and not pocket.known("duo")
    assert "duo" not in pocket._states, "a rename must drop the cached state"
    for old, new, exc in (
        ("alba", "x", ValueError),      # builtins are not renameable
        ("dad", "alba", ValueError),    # nor shadowable
        ("dad", "pair", ValueError),    # the target exists
        ("nope", "x", LookupError),     # the source does not
    ):
        try:
            pocket.rename(old, new)
            raise AssertionError(f"rename({old!r}, {new!r}) should have raised")
        except exc:
            pass
    print("ok  store: rename moves the voice, rewrites its metadata, "
          "drops the stale cache, refuses the bad shapes")

    pocket.remove("blend")
    assert not pocket.known("blend")
    for name, exc in (("alba", ValueError), ("nope", LookupError)):
        try:
            pocket.remove(name)
            raise AssertionError(f"remove({name!r}) should have raised")
        except exc:
            pass
    print("ok  store: rm deletes a custom voice, refuses builtins and ghosts")


def catalog_checks() -> None:
    entries = {e["name"]: e for e in voice.catalog()}
    for name in sorted(voice.available_voices()):
        assert entries[name] == {"name": name, "backend": "kokoro", "kind": "builtin"}
    assert entries["pocket:alba"] == {
        "name": "pocket:alba", "backend": "pocket", "kind": "builtin",
    }
    assert entries["pocket:dad"]["kind"] == "clone", entries["pocket:dad"]
    print(f"ok  catalog: {len(entries)} voices across both backends, kinds labeled")

    if REAL_INSTALLED:
        print("ok  catalog: absence case skipped (real pocket-tts is installed)")
    else:
        uninstall_fake()
        try:
            assert all(e["backend"] == "kokoro" for e in voice.catalog())
            assert not voice._known("pocket:alba")
        finally:
            reinstall_fake()
        print("ok  catalog: without pocket-tts, only Kokoro voices are listed")


def routing_checks(model: _FakeModel) -> None:
    said: list[dict] = []

    def fake_local(text, voice_name, speed):
        said.append({"voice": voice_name, "speed": speed})
        return b"RIFF-FAKE"

    real_local = voice._local_tts
    voice._local_tts = fake_local
    try:
        audio = voice.tts("Hello there.", voice="pocket:alba")
        assert audio.startswith(b"RIFF"), "pocket path must emit WAV"
        with wave.open(io.BytesIO(audio)) as wav_in:
            assert wav_in.getframerate() == RATE, (
                "the backend's own sample rate must be honored, not Kokoro's"
            )
            seconds = wav_in.getnframes() / wav_in.getframerate()
        # _repad trimmed the fake's 200ms pads and re-padded deliberately.
        assert 0.7 < seconds < 1.05, seconds
        assert not said, "a pocket: name must never reach the Kokoro path"

        voice.tts("Hello again.", voice="pocket:alba")
        assert model.prompts.count("alba") == 1, "the prompt state must be cached"
        assert sys.modules["pocket_tts"].TTSModel.loads == 1, "one model instance"
    finally:
        voice._local_tts = real_local
    print("ok  routing: pocket:alba synthesizes locally at the backend's rate, "
          "state and model cached")


def degradation_checks(model: _FakeModel) -> None:
    """Every pocket failure lands on a Kokoro name. Cloud never sees pocket:."""
    said: list[dict] = []

    def fake_local(text, voice_name, speed):
        said.append({"voice": voice_name, "speed": speed})
        return b"RIFF-FAKE"

    real_local = voice._local_tts
    voice._local_tts = fake_local
    try:
        # A raising model.
        model.boom = True
        before = set(voice._warned)
        out = voice.tts("hi", voice="pocket:alba")
        assert out == b"RIFF-FAKE" and said[-1]["voice"] == config.TTS_VOICE
        assert len(voice._warned - before) == 1, voice._warned - before
        voice.tts("hi again", voice="pocket:alba")
        assert len(voice._warned - before) == 1, "the warning must not repeat"
        print("ok  degrade: a raising pocket backend falls to the configured "
              "Kokoro voice, warned once")

        # A voice that does not exist.
        model.boom = False
        out = voice.tts("hi", voice="pocket:ghost")
        assert out == b"RIFF-FAKE" and said[-1]["voice"] == config.TTS_VOICE
        print("ok  degrade: a missing pocket voice stays audible")

        # The loop guard: a pocket default must not bounce back into pocket.
        model.boom = True
        real_default = config.TTS_VOICE
        config.TTS_VOICE = "pocket:ghost"
        try:
            voice.tts("hi", voice="pocket:alba")
            assert said[-1]["voice"] == "bm_george", said[-1]
        finally:
            config.TTS_VOICE = real_default
        model.boom = False
        print("ok  degrade: JARVIS_TTS_VOICE=pocket:x cannot loop the fallback")
    finally:
        voice._local_tts = real_local

    # Both local backends broken: the cloud gets a Kokoro name, never pocket:.
    calls: list[dict] = []

    def fake_speech(**kwargs):
        calls.append(kwargs)
        return b"\xff\xfbFAKE-MP3"

    def broken_local(text, voice_name, speed):
        raise RuntimeError("kokoro down too")

    real_local, real_speech = voice._local_tts, voice.llm.speech
    real_warned_flag = voice._local_warned
    voice._local_tts, voice.llm.speech = broken_local, fake_speech
    voice._local_warned = False
    model.boom = True
    try:
        out = voice.tts("hi", voice="pocket:alba")
        assert out == b"\xff\xfbFAKE-MP3"
        assert calls and calls[0]["voice"] == config.TTS_VOICE, calls
        assert not any(str(c["voice"]).startswith("pocket:") for c in calls)
    finally:
        voice._local_tts, voice.llm.speech = real_local, real_speech
        voice._local_warned = real_warned_flag
        model.boom = False
    print("ok  degrade: with every local path down, the cloud is asked for a "
          "Kokoro voice — never a pocket: name")

    if REAL_INSTALLED:
        print("ok  degrade: absent-library case skipped (real pocket-tts installed)")
    else:
        uninstall_fake()
        said.clear()
        voice._local_tts = fake_local
        try:
            out = voice.tts("hi", voice="pocket:alba")
            assert out == b"RIFF-FAKE" and said[-1]["voice"] == config.TTS_VOICE
        finally:
            voice._local_tts = real_local
            reinstall_fake()
        print("ok  degrade: pocket-tts not installed at all still leaves him audible")


def speed_checks() -> None:
    # routing_checks already synthesized at the configured default (1.2x), so
    # that warning is spent; probe with a pace this process has not asked for.
    before = set(voice._warned)
    voice.tts("hi", voice="pocket:alba", speed=0.8)
    voice.tts("hi again", voice="pocket:alba", speed=0.8)
    new = [w for w in voice._warned - before if "speed" in w]
    assert len(new) == 1, new
    before = set(voice._warned)
    voice.tts("hi", voice="pocket:alba", speed=1.0)
    assert not [w for w in voice._warned - before if "speed" in w]
    print("ok  speed: the no-speed-control note is said once, and 1.0x says nothing")


def set_voice_checks() -> None:
    assert voice.set_voice("pocket:alba") == "pocket:alba"
    assert voice.selected_voice() == "pocket:alba"
    assert voice.set_voice("pocket:dad") == "pocket:dad"
    assert voice.set_voice(config.TTS_VOICE) == config.TTS_VOICE
    for bad in ("pocket:nope", "gibberish"):
        try:
            voice.set_voice(bad)
            raise AssertionError(f"set_voice({bad!r}) should have raised")
        except LookupError:
            pass
    resolved = voice.set_voice("")
    assert voice._voice_override is None and resolved == voice.voice_for()
    print("ok  set_voice: accepts both backends and the store, rejects ghosts, "
          "and \"\" clears back to the avatar")


def avatar_checks() -> None:
    make_avatar("podcast", {"name": "Podcast", "voice": "pocket:dad"})
    make_avatar("phantom", {"name": "Phantom", "voice": "pocket:ghost"})

    said: list[dict] = []

    def fake_local(text, voice_name, speed):
        said.append({"voice": voice_name})
        return b"RIFF-FAKE"

    real_local = voice._local_tts
    voice._local_tts = fake_local
    try:
        config.AVATAR_ENV = "podcast"
        audio = voice.tts("hello")
        assert audio.startswith(b"RIFF") and not said, (
            "the avatar's pocket voice must route to the pocket backend"
        )
        with wave.open(io.BytesIO(audio)) as wav_in:
            assert wav_in.getframerate() == RATE

        config.AVATAR_ENV = "phantom"
        before = set(voice._warned)
        voice.tts("hello")
        assert said[-1]["voice"] == config.TTS_VOICE, said[-1]
        assert len(voice._warned - before) == 1
    finally:
        voice._local_tts = real_local
        config.AVATAR_ENV = "jarvis"
    print("ok  avatar: a pocket voice in avatar.json speaks, a ghost degrades audibly")


def blend_checks() -> None:
    """The mixing math, on numpy — production runs the same operators on torch."""
    a = {"k": np.array([1.0, 3.0], dtype="float32")}
    b = {"k": np.array([3.0, 5.0], dtype="float32")}
    out = pocket._blend([a, b], [1.0, 1.0])
    assert np.allclose(out["k"], [2.0, 4.0]), out["k"]
    out = pocket._blend([a, b], [3.0, 1.0])  # un-normalized -> 0.75 / 0.25
    assert np.allclose(out["k"], [1.5, 3.5]), out["k"]
    print("ok  blend: weighted average, weights normalized")

    # The prompt-length axis: slice to the shortest, keeping the head.
    long = {"kv": np.arange(4 * 10 * 8, dtype="float32").reshape(4, 10, 8)}
    short = {"kv": np.zeros((4, 7, 8), dtype="float32")}
    out = pocket._blend([long, short], [1.0, 1.0])
    assert out["kv"].shape == (4, 7, 8), out["kv"].shape
    assert np.allclose(out["kv"], long["kv"][:, :7, :] * 0.5), (
        "the head must survive the trim — trimming the tail keeps positions aligned"
    )
    print("ok  blend: mismatched prompt lengths slice to the shortest, head kept")

    # Integer bookkeeping (cache offsets) takes the elementwise minimum — a
    # blended position only counts as valid if it is valid in every
    # component, and "first wins" would make the mix order-dependent.
    ints = {"pos": np.array([5, 2, 9])}
    out = pocket._blend([ints, {"pos": np.array([3, 4, 9])}], [1.0, 1.0])
    assert out["pos"].dtype == ints["pos"].dtype and list(out["pos"]) == [3, 2, 9]
    print("ok  blend: integer offsets take the conservative elementwise minimum")

    failures = [
        ([{"k": np.zeros(2)}, {"x": np.zeros(2)}], [1, 1]),          # keys differ
        ([{"k": np.zeros((2, 3))}, {"k": np.zeros((3, 4))}], [1, 1]),  # two axes
        ([{"k": np.zeros(2)}, {"k": np.zeros((2, 2))}], [1, 1]),     # ranks differ
        ([{"k": np.zeros(2)}, {"k": np.zeros(2)}], [0.0, 0.0]),      # no weight
        ([{"k": np.zeros(2)}], [1.0]),                                # one state
    ]
    for states, weights in failures:
        try:
            pocket._blend(states, weights)
            raise AssertionError(f"blend should have refused {weights} over "
                                 f"{[list(s) for s in states]}")
        except ValueError:
            pass
    print("ok  blend: strangeness fails loudly — an experiment must not whisper")


def hardening_checks() -> None:
    """The privacy pins: the store is owner-only, and a Jarvis process
    defaults to HF offline mode (fresh subprocesses, since this one may have
    inherited explicit values)."""
    import os
    import stat
    import subprocess

    repo = str(Path(__file__).resolve().parents[1])
    probe = "import os, jarvis.config; print(os.environ.get('HF_HUB_OFFLINE'))"

    def run(**extra):
        env = {k: v for k, v in os.environ.items()
               if k not in ("HF_HUB_OFFLINE", "JARVIS_HF_OFFLINE")}
        env.update(extra)
        return subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True, env=env, cwd=repo,
        ).stdout.strip()

    assert run() == "1", "importing config must default to HF offline"
    assert run(JARVIS_HF_OFFLINE="0") == "None", "the knob must open the door"
    assert run(HF_HUB_OFFLINE="0") == "0", "an explicit env value must win"
    print("ok  hardening: config pins HF_HUB_OFFLINE=1 by default; "
          "JARVIS_HF_OFFLINE=0 and explicit env both override")

    for path in (config.VOICES_DIR, config.VOICES_DIR / "dad"):
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o700, (path, oct(mode))
    print("ok  hardening: the voice store and each voice are owner-only (0700)")


def route_checks() -> None:
    """GET /voices and POST /voice against a real threaded server."""
    from jarvis.face import server

    srv = server.create_server(port=PORT)
    sse: queue.Queue = queue.Queue(maxsize=50)
    with server._subs_lock:
        server._subscribers.append(sse)

    def post_voice(name, origin=f"http://localhost:{PORT}"):
        conn = http.client.HTTPConnection("localhost", PORT, timeout=5)
        conn.request(
            "POST", "/voice", json.dumps({"voice": name}),
            {"Content-Type": "application/json", "Origin": origin},
        )
        response = conn.getresponse()
        data = json.loads(response.read() or b"{}")
        conn.close()
        return response.status, data

    try:
        conn = http.client.HTTPConnection("localhost", PORT, timeout=5)
        conn.request("GET", "/voices")
        listing = json.loads(conn.getresponse().read())
        conn.close()
        names = {e["name"]: e for e in listing["voices"]}
        assert listing["override"] is None and listing["current"] == voice.voice_for()
        assert names["pocket:alba"]["backend"] == "pocket"
        assert names["pocket:dad"]["kind"] == "clone"
        assert config.TTS_VOICE in names

        status, data = post_voice("pocket:dad")
        assert status == 200 and data["voice"] == "pocket:dad", (status, data)
        msg = json.loads(sse.get(timeout=2))
        assert msg["kind"] == "voice" and msg["data"]["voice"] == "pocket:dad", msg

        conn = http.client.HTTPConnection("localhost", PORT, timeout=5)
        conn.request("GET", "/voices")
        listing = json.loads(conn.getresponse().read())
        conn.close()
        assert listing["override"] == "pocket:dad" and listing["current"] == "pocket:dad"

        status, data = post_voice("")
        assert status == 200 and data["voice"] == voice.voice_for(), (status, data)
        assert voice._voice_override is None
        sse.get(timeout=2)

        assert post_voice("pocket:nope")[0] == 404
        assert post_voice("pocket:dad", origin="http://evil.example")[0] == 403
        assert voice._voice_override is None, "a refused switch must change nothing"
    finally:
        with server._subs_lock:
            if sse in server._subscribers:
                server._subscribers.remove(sse)
        srv.shutdown()
        srv.server_close()
    print("ok  routes: /voices lists both backends with override state; /voice "
          "switches, clears on \"\", 404s ghosts, 403s cross-origin")


def main() -> int:
    model = install_fake()
    # The counter-sensitive checks run before anything uninstalls the fake —
    # a reinstall resets pocket's caches, so loads/prompts counts only mean
    # something on the uninterrupted prefix.
    store_checks(model)
    routing_checks(model)
    speed_checks()
    set_voice_checks()
    avatar_checks()
    blend_checks()
    catalog_checks()
    degradation_checks(model)
    hardening_checks()
    route_checks()
    print("\nall pocket-voice checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
