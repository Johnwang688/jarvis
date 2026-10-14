"""Free checks for `voice.stt`'s fallback chain (2026-10-08).

`nvidia/parakeet-tdt-0.6b-v3` has exactly one OpenRouter endpoint (Together).
When it is down every request answers `HTTP 404: Provider returned 404`, and
dictation in the v2 HUD, the v1 face and Discord voice notes all stopped with
a traceback. `llm.transcribe` is replaced by a recorder throughout: no network,
no key.

    .venv/bin/python tests/voice_stt_check.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis import config, llm, voice  # noqa: E402

# The owner's STT switches (V13) decide whether the cloud chain may run at all; this
# suite is about the chain, so it reads no real settings file.
config.STT_SETTINGS_PATH = Path(tempfile.mkdtemp()) / "no-such-stt.json"

FAILURES: list[str] = []
CHECKS = 0


def ok(condition, message):
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(message)
        print(f"  FAIL: {message}")


class Transcriber:
    """Answers per model from a script: a string is a transcript, an
    exception is raised."""

    def __init__(self, script):
        self.script = script
        self.calls = []

    def __call__(self, audio, model, filename="audio.webm", mime="audio/webm", **_kw):
        self.calls.append((model, filename, mime))
        answer = self.script[model]
        if isinstance(answer, Exception):
            raise answer
        return answer


def run(script, primary="a/primary", fallback=("b/fallback",)):
    saved = (llm.transcribe, config.STT_MODEL, config.STT_FALLBACK_MODELS,
             set(voice._warned))
    fake = Transcriber(script)
    llm.transcribe = fake
    config.STT_MODEL = primary
    config.STT_FALLBACK_MODELS = list(fallback)
    voice._warned.clear()
    try:
        try:
            return voice.stt(b"RIFFaudio", mime="audio/wav"), fake, None
        except Exception as exc:  # noqa: BLE001
            return None, fake, exc
    finally:
        llm.transcribe, config.STT_MODEL, config.STT_FALLBACK_MODELS = saved[:3]
        voice._warned.clear()
        voice._warned.update(saved[3])


def main():
    provider_404 = llm.LLMError('HTTP 404: {"error":{"message":"Provider returned 404","code":404}}')

    text, fake, exc = run({"a/primary": "hello there"})
    ok(text == "hello there" and exc is None, "a healthy primary answers")
    ok([c[0] for c in fake.calls] == ["a/primary"], "and the fallback is never called")
    ok(fake.calls[0][1:] == ("audio.wav", "audio/wav"), "the filename follows the mime")

    text, fake, exc = run({"a/primary": provider_404, "b/fallback": "still heard"})
    ok(text == "still heard" and exc is None,
       "a provider 404 on the primary falls through to the fallback (the live outage)")
    ok([c[0] for c in fake.calls] == ["a/primary", "b/fallback"], "in configured order")

    text, fake, exc = run({"a/primary": llm.LLMError("a/primary transcription failed after 3 attempts: x"),
                           "b/fallback": "heard"})
    ok(text == "heard", "exhausted retries on the primary fall through too")

    text, fake, exc = run({"a/primary": provider_404,
                           "b/fallback": llm.LLMError("HTTP 503: secret-looking body")})
    ok(isinstance(exc, voice.STTUnavailable), "every model failing raises STTUnavailable")
    ok(isinstance(exc, llm.LLMError), "which is still an LLMError for older callers")
    message = str(exc)
    ok("a/primary (HTTP 404)" in message and "b/fallback (HTTP 503)" in message,
       f"the message names each model and its status: {message!r}")
    ok("secret-looking body" not in message and "Provider returned" not in message,
       "and never quotes a response body")

    text, fake, exc = run({"a/primary": llm.LLMError("HTTP 401: body-quoted-401 sk-or-v1-fake"),
                           "b/fallback": "unreached"})
    ok(isinstance(exc, voice.STTUnavailable) and "key" in str(exc),
       "a refused key stops at once with a sentence about the key")
    ok("body-quoted-401" not in str(exc) and "sk-or-v1" not in str(exc),
       f"and the 401's response body is not quoted: {str(exc)!r}")
    ok([c[0] for c in fake.calls] == ["a/primary"],
       "and spends no second request on a fallback that would be refused too")

    text, fake, exc = run({"a/primary": llm.LLMError("HTTP 402: body-quoted-402"), "b/fallback": "x"})
    ok([c[0] for c in fake.calls] == ["a/primary"], "nor on a 402")
    ok(isinstance(exc, voice.STTUnavailable) and "body-quoted-402" not in str(exc),
       f"and the 402's response body is not quoted either: {str(exc)!r}")

    text, fake, exc = run({"a/primary": "once"}, fallback=("a/primary",))
    ok([c[0] for c in fake.calls] == ["a/primary"], "a fallback equal to the primary is not tried twice")

    text, fake, exc = run({"a/primary": provider_404}, fallback=())
    ok(isinstance(exc, voice.STTUnavailable), "an empty fallback list still fails cleanly")

    # The *shipped* default, read in a fresh interpreter with both variables
    # removed: the owner's environment (an empty JARVIS_STT_FALLBACK is a
    # legitimate way to disable it) must not decide what this asserts.
    env = {k: v for k, v in os.environ.items()
           if k not in ("JARVIS_STT_FALLBACK", "JARVIS_STT_MODEL")}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    shipped = subprocess.run(
        [sys.executable, "-c",
         "import json; from jarvis import config; "
         "print(json.dumps([config.STT_MODEL, config.STT_FALLBACK_MODELS]))"],
        env=env, capture_output=True, text=True, timeout=60)
    try:
        primary, fallbacks = json.loads(shipped.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        primary, fallbacks = None, []
    ok(bool(fallbacks) and primary not in fallbacks,
       f"the shipped default has a fallback that is a different model: {primary!r} {fallbacks!r}")

    try:
        voice.stt(b"", mime="audio/wav")
        ok(False, "empty audio is refused")
    except ValueError:
        ok(True, "empty audio is refused")

    # V2: dictation passes tighter limits through to every model it tries; the
    # default callers (Discord voice notes) pass none and keep llm's defaults.
    seen: list[dict] = []
    saved_t = llm.transcribe

    def limited(audio, model, filename="audio.webm", mime="audio/webm", **kw):
        seen.append(kw)
        return "ok"

    llm.transcribe = limited
    try:
        voice.stt(b"RIFFaudio", mime="audio/wav", timeout=20.0, max_retries=2)
        voice.stt(b"RIFFaudio", mime="audio/wav")
    finally:
        llm.transcribe = saved_t
    ok(seen == [{"timeout": 20.0, "max_retries": 2}, {}],
       f"timeout and attempts reach llm.transcribe only when given: {seen!r}")
    ok(config.STT_DICTATION_TIMEOUT_S <= 20 and config.STT_DICTATION_ATTEMPTS == 2,
       "the shipped dictation bounds are 20 s and two attempts")

    # llm.transcribe itself: bounded attempts, and no sleep after the last one
    # (it used to wait 2**n s before raising, so a failure came late).
    import httpx
    sleeps: list[float] = []
    posts: list[float] = []

    class DeadClient:
        def post(self, *a, timeout=None, **kw):
            posts.append(timeout)
            raise httpx.ConnectTimeout("simulated")

    real = (llm._client, llm.time.sleep, config.api_key)
    llm._client, llm.time.sleep, config.api_key = DeadClient(), sleeps.append, lambda: "sk-dummy-not-a-key"
    try:
        try:
            llm.transcribe(b"x", "m/x", timeout=20.0, max_retries=2)
            ok(False, "a dead provider raises")
        except llm.LLMError as exc:
            ok("after 2 attempts" in str(exc), f"two attempts, named: {exc}")
    finally:
        llm._client, llm.time.sleep, config.api_key = real
    ok(posts == [20.0, 20.0], f"each attempt used the 20 s timeout: {posts!r}")
    ok(sleeps == [1], f"one backoff between two attempts and none after the last: {sleeps!r}")

    # MED-2: a caller that states its deadline (the HUD: budget_ms) gets the
    # model chain fitted inside it. A virtual clock models llm.transcribe: a
    # hung model consumes `timeout` per attempt plus the 1 s, 2 s... backoff
    # between attempts; a healthy one answers at once.
    class Clock:
        now = 0.0

    def hung_primary(audio, model, filename="audio.webm", mime="audio/webm", timeout=60.0, max_retries=3, **_kw):
        if model == "a/primary":
            for attempt in range(max_retries):
                Clock.now += timeout
                if attempt + 1 < max_retries:
                    Clock.now += 2 ** attempt
            raise llm.LLMError(f"{model} transcription failed after {max_retries} attempts: timeout")
        return "fallback heard"

    for client_ms in (15_000, 20_000, 40_000, 120_000):
        saved_b = (llm.transcribe, config.STT_MODEL, config.STT_FALLBACK_MODELS, voice._clock)
        llm.transcribe, config.STT_MODEL, config.STT_FALLBACK_MODELS = hung_primary, "a/primary", ["b/fallback"]
        voice._clock = lambda: Clock.now
        Clock.now = 0.0
        voice._warned.clear()
        try:
            text = voice.stt(b"RIFFaudio", mime="audio/wav", timeout=20.0, max_retries=2, budget=client_ms / 1000)
        except Exception as exc:  # noqa: BLE001
            text = f"raised {exc!r}"
        finally:
            llm.transcribe, config.STT_MODEL, config.STT_FALLBACK_MODELS, voice._clock = saved_b
        ok(text == "fallback heard", f"a hung primary still fails over inside a {client_ms} ms deadline: {text!r}")
        ok(Clock.now <= client_ms / 1000 - 0.9, f"and the whole chain ({Clock.now:.1f} s) ends inside the client's {client_ms / 1000:.0f} s")
    ok(voice._fit_budget(7.0, 1, 2, 20.0) == (7.0, 1), "a short slot is one attempt")
    ok(voice._fit_budget(40.0, 1, 2, 20.0) == (19.5, 2), "a long slot is two attempts, capped at the 20 s ceiling")
    ok(voice._fit_budget(0.5, 2, 2, 20.0) is None, "no time left is reported, not guessed")

    # Without a budget (Discord, the face) the fixed limits pass through untouched.
    seen2: list[dict] = []
    llm.transcribe = lambda audio, model, **kw: seen2.append(kw) or "ok"
    try:
        voice.stt(b"RIFFaudio", mime="audio/wav", timeout=20.0, max_retries=2)
    finally:
        llm.transcribe = saved_t
    ok([{k: v for k, v in kw.items() if k in ("timeout", "max_retries")} for kw in seen2] == [{"timeout": 20.0, "max_retries": 2}], f"no budget_ms keeps the fixed dictation bounds: {seen2!r}")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} of {CHECKS} voice stt checks FAILED")
        return 1
    print(f"all voice stt checks passed ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
