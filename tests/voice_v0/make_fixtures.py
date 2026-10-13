"""Generate synthetic speech fixtures with the repo's local Kokoro (and Pocket).

    PYTHONPATH=<worktree> python tests/voice_v0/make_fixtures.py [--pocket]

Writes 16 kHz mono WAVs plus manifest.json (text, voice, speed) under
~/.cache/jarvis-voice-v0/fixtures/. Synthetic only; never a real person's
voice. Kokoro runs through kokoro_onnx with the same model files the daemon
uses (config.KOKORO_MODEL / KOKORO_VOICES). `--pocket` additionally renders the
Pocket builtin voices through `jarvis.pocket.synthesize`, which needs the repo
venv (torch); run that half with the repo's interpreter.
"""

from __future__ import annotations

import json
import sys
import wave
import io

import numpy as np

import common

# Dictation-style sentences. No digits, so STT number formatting cannot add
# normalisation noise to the WER.
SENTENCES = [
    "Please open the settings panel and turn the microphone sensitivity down a little.",
    "I think we should rename the branch before opening the pull request tonight.",
    "The build finished without errors, but the second test suite is still running.",
    "Remind me to call the dentist tomorrow morning before the first class starts.",
    "Could you summarize the last three messages in this conversation for me.",
    "The robot drove straight across the field and stopped just short of the wall.",
    "Add a short note to the plan explaining why we chose the smaller model.",
    "Tell me what changed in the editor since yesterday afternoon.",
    "We need to measure the processor cost before we decide where this runs.",
    "That reply was far too long, so keep the next one to a single paragraph.",
    "Schedule the review for Thursday and invite everyone who touched the voice code.",
    "If the transcription fails, keep the audio and show a retry button.",
    "The weather looks clear this evening, so the practice can stay outside.",
    "Read me the error message from the terminal, but skip the stack trace.",
    "She walked quickly through the empty hallway and closed the heavy wooden door.",
    "Open the browser and search for the documentation about audio worklets.",
    "Every open chat should have its own voice, and they should never talk at once.",
    "Stop reading the diff and just tell me whether the tests passed.",
    "The garden was quiet except for the sound of water running somewhere nearby.",
    "Set a timer for ten minutes and let me know when the pasta is ready.",
    "Please compare the two approaches and recommend the one that costs less.",
    "He forgot his notebook on the train and had to go back for it.",
    "Write a commit message that explains the fix in plain language.",
    "The orchestra tuned their instruments while the audience slowly found their seats.",
]

# English Kokoro families (a* American, b* British), first letter = language.
KOKORO_EN = ["af_heart", "af_bella", "af_nicole", "af_sarah", "af_sky", "am_adam", "am_michael",
             "am_eric", "am_liam", "bf_emma", "bf_isabella", "bm_george", "bm_lewis", "bm_daniel"]


def kokoro_clips() -> list[dict]:
    from kokoro_onnx import Kokoro

    from jarvis import config

    import onnxruntime as ort

    # The daemon's default session grabs every core and oversubscribes a shared
    # machine; four intra-op threads is plenty for fixture generation.
    so = ort.SessionOptions()
    so.intra_op_num_threads = 4
    so.inter_op_num_threads = 1
    sess = ort.InferenceSession(str(config.KOKORO_MODEL), sess_options=so, providers=["CPUExecutionProvider"])
    k = Kokoro.from_session(sess, str(config.KOKORO_VOICES))
    have = set(np.load(str(config.KOKORO_VOICES)).files)
    clips = []
    for vi, voice in enumerate(v for v in KOKORO_EN if v in have):
        lang = "en-gb" if voice[0] == "b" else "en-us"
        for si, text in enumerate(SENTENCES):
            speed = (0.9, 1.0, 1.1, 1.2)[(vi + si) % 4]
            name = f"kokoro_{voice}_{si:02d}.wav"
            if (common.FIXTURES / name).exists():  # resumable; Kokoro is deterministic
                x = common.read_wav(common.FIXTURES / name)
            else:
                samples, sr = k.create(text, voice=voice, speed=speed, lang=lang)
                x = common.resample_to_16k(np.asarray(samples, dtype=np.float32), sr)
                common.write_wav(common.FIXTURES / name, x)
            clips.append({"file": name, "text": text, "voice": voice, "engine": "kokoro", "speed": speed,
                          "seconds": len(x) / common.SR, "sentence": si})
        print(voice, "done", file=sys.stderr)
    return clips


def pocket_clips() -> list[dict]:
    from jarvis import pocket

    clips = []
    for vi, name in enumerate(pocket.BUILTINS):
        for si, text in enumerate(SENTENCES[:8]):
            data = pocket.synthesize(text, name)
            with wave.open(io.BytesIO(data)) as w:
                sr = w.getframerate()
                pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
            x = common.resample_to_16k(pcm, sr)
            fn = f"pocket_{name}_{si:02d}.wav"
            common.write_wav(common.FIXTURES / fn, x)
            clips.append({"file": fn, "text": text, "voice": f"pocket:{name}", "engine": "pocket",
                          "speed": 1.0, "seconds": len(x) / common.SR, "sentence": si})
        print("pocket", name, "done", file=sys.stderr)
    return clips


def main(argv: list[str]) -> int:
    common.FIXTURES.mkdir(parents=True, exist_ok=True)
    mpath = common.FIXTURES / "manifest.json"
    existing = json.loads(mpath.read_text()) if mpath.exists() else []
    if "--pocket" in argv:
        keep = [c for c in existing if c["engine"] != "pocket"]
        clips = keep + pocket_clips()
    else:
        keep = [c for c in existing if c["engine"] != "kokoro"]
        clips = keep + kokoro_clips()
    mpath.write_text(json.dumps(clips, indent=1))
    print(f"{len(clips)} clips, {sum(c['seconds'] for c in clips) / 60:.1f} min -> {common.FIXTURES}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
