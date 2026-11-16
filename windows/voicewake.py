"""VA-4 wake listener: sherpa-onnx KWS "Jarvis" + endpoint (W2 implements).

Interface fixed by docs/plans/2026-11-16-voice-anywhere-va245.md. This stub
exists so W1 can import it; every call raises until W2 lands.
"""
from __future__ import annotations


def keywords_text(pieces):
    raise NotImplementedError("VA-2/4/5 W2: voicewake.keywords_text")


def verify_model(directory):
    raise NotImplementedError("VA-2/4/5 W2: voicewake.verify_model")


class Endpointer:
    def __init__(self, rate=16000, silence_ms=800, max_ms=15000):
        raise NotImplementedError("VA-2/4/5 W2: voicewake.Endpointer")

    def feed(self, rms, n):
        raise NotImplementedError


class WakeListener:
    def __init__(self, on_utterance, on_level, busy, on_state=None):
        raise NotImplementedError("VA-2/4/5 W2: voicewake.WakeListener")

    def start(self):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError
