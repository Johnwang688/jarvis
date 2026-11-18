"""Offline end-to-end Voice Anywhere probe. Only fabricated Windows SAPI input.

From WSL, with the voice-probe venv, Jarvis Windows bridge and models installed:
  JARVIS_WINDOWS_PYTHON=/mnt/c/Users/<user>/.jarvis-bridge/venv/Scripts/python.exe \
    ~/.cache/jarvis-voice-v0/venv/bin/python tests/voice_anywhere_synthetic_real.py --run

The real Windows KWS, endpoint and Start-menu index and the real Linux Parakeet
engine run. The only substituted endpoints are microphone frames (generated
speech) and the final OS launch (captured argv). Nothing is played, typed or
opened and no production listener, credential or speaker enrollment is used.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))
# phrase, SAPI rate, loudness, voice, expected Start-menu app
CASES = (
    ("Jarvis, open Chrome.", 0, 100, "David", "Google Chrome"),
    ("Jarvis open Chrome.", 2, 85, "David", "Google Chrome"),
    ("Jarvis, open Chrome.", 0, 100, "Zira", "Google Chrome"),
    ("Jarvis open Chrome.", 1, 75, "Zira", "Google Chrome"),
    ("Jarvis, please open Notepad.", -1, 95, "David", "Notepad"),
    ("Jarvis, open Notepad.", -1, 95, "Zira", "Notepad"),
    ("Open Chrome.", 0, 100, "David", None),
    ("Open Chrome.", 1, 85, "Zira", None),
    ("Jarvis.", 0, 100, "David", None),
    ("Jarvis.", 1, 85, "Zira", None),
)

SYNTH = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo -ArgumentList 16000, Sixteen, Mono
try {
    foreach ($case in (ConvertFrom-Json $env:JARVIS_TEST_CASES)) {
        $bytes = New-Object System.IO.MemoryStream
        try {
            $synth.SelectVoice("Microsoft " + $case.voice + " Desktop")
            $synth.Rate = $case.rate
            $synth.Volume = $case.volume
            $synth.SetOutputToAudioStream($bytes, $fmt)
            $synth.Speak([string]$case.text)
            [Convert]::ToBase64String($bytes.ToArray())
            $synth.SetOutputToNull()
        } finally { $bytes.Dispose() }
    }
} finally { $synth.Dispose() }
"""


def capture_pcm(pcm):
    from windows import voicewake
    from unittest.mock import Mock
    import numpy as np

    clips = []
    states = []
    listener = voicewake.WakeListener(clips.append, lambda value: None, lambda: False, states.append)
    callback_stream = Mock(active=True)
    api = [{"name": "WASAPI", "default_input_device": 0}]
    # Only replace device discovery and opening. Keep real model verification,
    # recognizer, capture callback, queue, native timestamp handling and VAD.
    with patch("sounddevice.query_hostapis", return_value=api), patch("sounddevice.InputStream", return_value=callback_stream):
        if not listener.start():
            raise RuntimeError(listener.reason)
    try:
        block = voicewake.CHUNK * 2
        pcm = bytes(8 * block) + pcm + bytes(48 * block)
        pcm += bytes((-len(pcm)) % block)
        deadline = time.monotonic() + 25
        for pos in range(0, len(pcm), block):
            while listener._audio_queue.qsize() > 2:
                if time.monotonic() > deadline or not listener._thread.is_alive():
                    raise RuntimeError("Wake queue failed to drain")
                time.sleep(.001)
            samples = np.frombuffer(pcm[pos:pos + block], dtype="<i2")
            listener._audio(samples, len(samples), None, None)
        while listener._audio_queue.qsize():
            if time.monotonic() > deadline or not listener._thread.is_alive():
                raise RuntimeError("Wake worker failed")
            time.sleep(.001)
    finally:
        listener.stop()
    return {"clips": [base64.b64encode(x).decode("ascii") for x in clips], "woke": "listening" in states}


def windows_worker():
    from windows.voiceactions import Actions, match_app
    cases = [{"text": phrase, "rate": rate, "volume": volume, "voice": voice}
             for phrase, rate, volume, voice, _ in CASES]
    encoded = base64.b64encode(SYNTH.encode("utf-16le")).decode("ascii")
    generated = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        check=True, capture_output=True, text=True, timeout=45,
        env=dict(os.environ, JARVIS_TEST_CASES=json.dumps(cases)),
        creationflags=subprocess.CREATE_NO_WINDOW,
    ).stdout.splitlines()
    if len(generated) != len(CASES):
        raise RuntimeError("Missing generated speech")
    index_start = time.perf_counter()
    index = Actions(desktop=object())
    index_ms = round((time.perf_counter() - index_start) * 1000)
    apps = [match_app(name, index.apps) for name in ("Google Chrome", "Notepad")]
    if not all(apps):
        raise RuntimeError("Test requires Google Chrome and Notepad in Windows Start menu")
    print(json.dumps({"captures": [capture_pcm(base64.b64decode(x, validate=True)) for x in generated],
                      "apps": apps, "index_ms": index_ms}))


def check(windows_python):
    import stt_compare_probe as probe
    from jarvis.v2 import voice_intent
    from windows.voiceactions import Actions
    from windows.voiceflow import Flow

    model = probe.find_model_dir(None)
    if model is None:
        raise RuntimeError("Missing local Parakeet model")
    script = subprocess.check_output(["wslpath", "-w", str(HERE)], text=True).strip()
    raw = subprocess.run([windows_python, "-B", script, "--windows-worker"], capture_output=True,
                         text=True, timeout=90)
    if raw.returncode:
        raise RuntimeError(raw.stderr[-2000:])
    result = json.loads(raw.stdout)
    print(f"Real Windows Start-menu index: {result['index_ms']} ms")
    stt = probe.load_stt_local(model).Engine(threads=2, schedule=False)
    launches = []
    index = [{"Name": app["name"], "AppID": app["id"]} for app in result["apps"]]
    indexed = SimpleNamespace(returncode=0, stdout=json.dumps(index))
    actions = Actions(desktop=object(), runner=lambda *args, **kwargs: indexed,
                      launcher=lambda argv, **kwargs: launches.append(argv))
    errors = 0
    try:
        for (phrase, rate, _, voice, expected), sample in zip(CASES, result["captures"], strict=True):
            states = []
            texts = []
            elapsed = []
            def dictate(wav, source):
                start = time.perf_counter()
                text = stt.transcribe(wav, "audio/wav", timeout=45)
                elapsed.append(round((time.perf_counter() - start)*1000))
                texts.append(text)
                return {"text": text}

            launches.clear()
            flow = Flow(SimpleNamespace(dictate=dictate, route=voice_intent.route), SimpleNamespace(), actions,
                        SimpleNamespace(set_state=lambda *v: states.append(v)), None)
            for clip in sample["clips"]:
                flow.on_clip(base64.b64decode(clip), "wake")
            expected_id = next((app["id"] for app in actions.apps if app["name"] == expected), None)
            argv = [["explorer.exe", "shell:AppsFolder\\" + expected_id]] if expected_id else []
            ok = launches == argv and (expected is None or (len(texts) == 1 and sample["woke"]))
            errors += int(not ok)
            print(f"{'PASS' if ok else 'FAIL'} {voice} {phrase!r} rate={rate}: wake={sample['woke']} "
                  f"transcription={texts} decode_ms={elapsed} dispatched={len(launches)}")
    finally:
        stt.unload()
    return int(bool(errors))


if __name__ == "__main__":
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--run", action="store_true")
    args.add_argument("--windows-worker", action="store_true", help=argparse.SUPPRESS)
    options = args.parse_args()
    if options.windows_worker:
        windows_worker()
    elif options.run:
        raise SystemExit(check(os.environ["JARVIS_WINDOWS_PYTHON"]))
    else:
        print("skipped: pass --run to test the real Windows and local voice models")

