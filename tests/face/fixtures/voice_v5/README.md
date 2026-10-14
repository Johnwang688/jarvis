Synthetic speech for the V5 VAD check (`tests/face/hud_v2_vad_check.py`).

Both clips are Kokoro text-to-speech output (16 kHz mono, 16-bit), copied from the
V0 fixture set (`tests/voice_v0/make_fixtures.py`): no recording of a real person.

| file | voice | text |
|---|---|---|
| `kokoro_a.wav` | af_heart, speed 0.9 | "Please open the settings panel and turn the microphone sensitivity down a little." |
| `kokoro_b.wav` | am_adam | "The build finished without errors, but the second test suite is still running." |

The check builds its scenario files (silence, clip, silence, clip ...) from these at run
time; noise is generated, not stored.
