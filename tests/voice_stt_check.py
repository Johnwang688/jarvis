"""Free checks for `voice.stt`'s fallback chain (2026-10-08).

`nvidia/parakeet-tdt-0.6b-v3` has exactly one OpenRouter endpoint (Together).
When it is down every request answers `HTTP 404: Provider returned 404`, and
dictation in the v2 HUD, the v1 face and Discord voice notes all stopped with
a traceback. `llm.transcribe` is replaced by a recorder throughout: no network,
no key.

    .venv/bin/python tests/voice_stt_check.py
"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis import config, llm, voice  # noqa: E402

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

    text, fake, exc = run({"a/primary": llm.LLMError("HTTP 401: bad key"),
                           "b/fallback": "unreached"})
    ok(isinstance(exc, voice.STTUnavailable) and "key" in str(exc),
       "a refused key stops at once with a sentence about the key")
    ok([c[0] for c in fake.calls] == ["a/primary"],
       "and spends no second request on a fallback that would be refused too")

    text, fake, exc = run({"a/primary": llm.LLMError("HTTP 402: no credit"), "b/fallback": "x"})
    ok([c[0] for c in fake.calls] == ["a/primary"], "nor on a 402")

    text, fake, exc = run({"a/primary": "once"}, fallback=("a/primary",))
    ok([c[0] for c in fake.calls] == ["a/primary"], "a fallback equal to the primary is not tried twice")

    text, fake, exc = run({"a/primary": provider_404}, fallback=())
    ok(isinstance(exc, voice.STTUnavailable), "an empty fallback list still fails cleanly")

    ok(config.STT_FALLBACK_MODELS and config.STT_MODEL not in config.STT_FALLBACK_MODELS,
       "the shipped default has a fallback that is a different model")

    try:
        voice.stt(b"", mime="audio/wav")
        ok(False, "empty audio is refused")
    except ValueError:
        ok(True, "empty audio is refused")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} of {CHECKS} voice stt checks FAILED")
        return 1
    print(f"all voice stt checks passed ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
