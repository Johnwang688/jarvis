"""VA-4/VA-2 overlay: bottom-middle pop-up with a live waveform (W2 implements).

Interface fixed by docs/plans/2026-11-16-voice-anywhere-va245.md. This stub
exists so W1 can import it; every call raises until W2 lands.
"""
from __future__ import annotations

STATES = ("hidden", "listening", "transcribing", "thinking", "answering", "done", "error")


def bars(levels, n=24):
    raise NotImplementedError("VA-2/4/5 W2: voiceui.bars")


def place(screen_w, screen_h, w, h):
    raise NotImplementedError("VA-2/4/5 W2: voiceui.place")


class Overlay:
    def __init__(self, root):
        raise NotImplementedError("VA-2/4/5 W2: voiceui.Overlay")

    def set_state(self, state, text=None):
        raise NotImplementedError

    def push_level(self, rms):
        raise NotImplementedError
