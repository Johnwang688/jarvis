"""Synthetic fixtures for the KWS and Smart Turn probes (Kokoro only).

    PYTHONPATH=<worktree> python tests/voice_v0/make_special_fixtures.py

Writes under ~/.cache/jarvis-voice-v0/fixtures/special/ with special.json:
  kws        isolated keywords, keywords in carrier phrases, trailing send/cancel
             words in dictation, and hard negatives (similar-sounding words)
  turn       complete sentences vs. mid-sentence fragments (for Smart Turn)
All text is known, all audio is synthetic; no real person's voice.
"""

from __future__ import annotations

import json
import sys

import numpy as np

import common

KWS_VOICES = ["af_heart", "af_bella", "am_adam", "am_michael", "bf_emma", "bm_george", "bm_lewis", "af_sarah"]

KEYWORD_WORDS = ["jarvis", "over", "cancel", "pause", "resume", "skip", "stop"]

# (text, expected keywords in order). Expectations are the words actually spoken.
KWS_TEXTS = (
    [(w.capitalize() + ".", [w]) for w in KEYWORD_WORDS]
    + [("Jarvis, pause.", ["jarvis", "pause"]), ("Jarvis, resume.", ["jarvis", "resume"]),
       ("Jarvis, skip.", ["jarvis", "skip"]), ("Jarvis, stop.", ["jarvis", "stop"]),
       ("Hey Jarvis, can you hear me?", ["jarvis"]),
       ("Add the note to the plan and send it, over.", ["over"]),
       ("That is the wrong file, cancel.", ["cancel"]),
       ("Please rename the branch before tonight, over.", ["over"])]
)
# Similar-sounding or embedding words that must NOT trigger anything unless
# the keyword genuinely appears (the list gives the true keywords).
KWS_HARD_NEG = [
    ("Travis drove to the service station.", []),
    ("The jar of marbles fell off the shelf.", []),
    ("Her cancer research was published last year.", []),
    ("The game is over there by the window.", ["over"]),
    ("He hovered over the table for a moment.", ["over"]),
    ("A pass was skipped in the third run.", []),
    ("They resumed the conversation after lunch.", []),
    ("The pauses between words were short.", []),
    ("Stop signs line the road to the school.", ["stop"]),
    ("Please cancel nothing, everything is fine.", ["cancel"]),
    ("Overcast skies kept the ground cold all morning.", []),
    ("The cancelled flight left everyone stuck at the airport.", []),
]

TURN_VOICES = ["af_heart", "am_adam", "bf_emma", "bm_george", "af_sarah", "am_michael"]
TURN_COMPLETE = [
    "Please open the settings panel and turn the microphone sensitivity down.",
    "I think we should rename the branch before opening the pull request.",
    "The build finished without errors.",
    "Remind me to call the dentist tomorrow morning.",
    "Could you summarize the last three messages for me?",
    "Add a short note to the plan.",
    "What changed in the editor since yesterday?",
    "That reply was far too long.",
    "If the transcription fails, keep the audio and show a retry button.",
    "Read me the error message from the terminal.",
    "Stop reading the diff and just tell me whether the tests passed.",
    "Write a commit message that explains the fix.",
]
# Cut mid-sentence: (a) at a function word, no punctuation; (b) after a comma.
TURN_INCOMPLETE = [
    "Please open the settings panel and turn the",
    "I think we should rename the branch before",
    "The build finished without errors, but the",
    "Remind me to call the dentist tomorrow morning and then",
    "Could you summarize the last three messages in",
    "Add a short note to the plan explaining why we",
    "What changed in the editor since",
    "That reply was far too long, so keep the next one",
    "If the transcription fails, then",
    "Read me the error message from the terminal, but",
    "Stop reading the diff and just tell me whether the",
    "Write a commit message that explains the fix in",
]


def main() -> int:
    import onnxruntime as ort
    from kokoro_onnx import Kokoro

    from jarvis import config

    out = common.FIXTURES / "special"
    out.mkdir(parents=True, exist_ok=True)
    so = ort.SessionOptions()
    so.intra_op_num_threads = 4
    so.inter_op_num_threads = 1
    k = Kokoro.from_session(ort.InferenceSession(str(config.KOKORO_MODEL), sess_options=so,
                                                 providers=["CPUExecutionProvider"]), str(config.KOKORO_VOICES))

    def synth(text: str, voice: str, speed: float = 1.0) -> np.ndarray:
        lang = "en-gb" if voice[0] == "b" else "en-us"
        samples, sr = k.create(text, voice=voice, speed=speed, lang=lang)
        return common.resample_to_16k(np.asarray(samples, dtype=np.float32), sr)

    def trim(x: np.ndarray, thr=0.004) -> np.ndarray:
        idx = np.where(np.abs(x) > thr)[0]
        return x[idx[0]: idx[-1] + 1] if len(idx) else x

    items = []
    n = 0
    for group, texts in (("kws_pos", KWS_TEXTS), ("kws_neg", KWS_HARD_NEG)):
        for ti, (text, expect) in enumerate(texts):
            for vi, voice in enumerate(KWS_VOICES):
                x = trim(synth(text, voice, speed=(1.0, 1.15)[(ti + vi) % 2]))
                name = f"{group}_{ti:02d}_{voice}.wav"
                common.write_wav(out / name, x)
                items.append({"file": name, "group": group, "text": text, "expect": expect, "voice": voice,
                              "seconds": len(x) / common.SR})
                n += 1
    for group, texts in (("turn_complete", TURN_COMPLETE), ("turn_incomplete", TURN_INCOMPLETE)):
        for ti, text in enumerate(texts):
            for voice in TURN_VOICES:
                x = trim(synth(text, voice))
                name = f"{group}_{ti:02d}_{voice}.wav"
                common.write_wav(out / name, x)
                items.append({"file": name, "group": group, "text": text, "voice": voice, "seconds": len(x) / common.SR})
                n += 1
    (out / "special.json").write_text(json.dumps(items, indent=1))
    print(f"{n} special clips -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
