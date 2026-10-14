Synthetic speech for the V11 local wake-word tests (`hud/src/lib/wake/*.test.ts`,
`tests/face/hud_v2_wake_check.py`).

Every clip is Kokoro text-to-speech output (16 kHz mono, 16-bit), copied from the V0 special fixture
set (`tests/voice_v0/make_special_fixtures.py`): no recording of a real person.
`tests/voice_v11/native_reference.py` is what sherpa-onnx's own spotter says about the same clips.

| file | voice | text |
|---|---|---|
| `kws_neg_00_am_adam.wav` | am_adam | "Travis drove to the service station." |
| `kws_neg_01_am_adam.wav` | am_adam | "The jar of marbles fell off the shelf." |
| `kws_neg_09_am_adam.wav` | am_adam | "Please cancel nothing, everything is fine." |
| `kws_pos_00_am_adam.wav` | am_adam | "Jarvis." |
| `kws_pos_00_bf_emma.wav` | bf_emma | "Jarvis." |
| `kws_pos_07_am_adam.wav` | am_adam | "Jarvis, pause." |
| `kws_pos_07_bf_emma.wav` | bf_emma | "Jarvis, pause." |
| `kws_pos_08_bf_emma.wav` | bf_emma | "Jarvis, resume." |
| `kws_pos_09_bf_emma.wav` | bf_emma | "Jarvis, skip." |
| `kws_pos_10_am_adam.wav` | am_adam | "Jarvis, stop." |
| `kws_pos_10_bf_emma.wav` | bf_emma | "Jarvis, stop." |
| `kws_pos_11_am_adam.wav` | am_adam | "Hey Jarvis, can you hear me?" |
