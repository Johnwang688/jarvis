# Voice anywhere: VA-2, VA-4, VA-5 together (amendment, 2026-11-16)

Amends [the voice-anywhere plan](2026-11-15-voice-anywhere-plan.md). VA-1 (#386) and VA-0
(#387, #389) are merged. The owner wants hotkey typing (VA-2), the wake-word pop-up (VA-4) and
voice actions (VA-5) usable the same day, so the three are built together as three parallel
packages over the fixed interfaces below.

## Owner decisions (2026-11-16)

| # | Decision |
|---|---|
| VA-D3 | **Hotkey dictation types into the HUD window too.** The owner approves every card today and is loosening safeguards gradually toward autonomy. This loosens CLAUDE.md "speech never authorizes an approval" for the hotkey path only (a physical key press precedes it). The wake-word path still never types. |
| VA-D4 | Wake-word questions and answers live in **one dedicated "Voice" chat**, made on first use. A small pop-up at the **bottom middle** of the screen shows a **live waveform** and the state (listening, transcribing, thinking, answering), for the hotkey and the wake word alike. |
| VA-D5 | **A decision model picks the intent** (type it, an action, or a question), not a fixed prefix. Today that is Haiku 5.5 through the daemon (`route`); VA-3's local model may replace it later. To keep plain dictation instant, hotkey speech goes to the model only when a cheap local check says it *might* be a command (it starts with "Jarvis" or an action verb). Wake-word speech always goes to the model. |
| VA-D6 | Wake-word speech goes through the **desk's speaker check exactly as the HUD's wake word does** (`verify=True`: verified when enrolled, the Guest-voice setting respected). The hotkey stays `verify=False`. |

Unchanged from VA-D1/VA-D2: the hotkey types (now anywhere); the wake word never types.

## Packages and file ownership

| Pkg | Owner files | Depends on |
|---|---|---|
| **D: daemon + wire** | `jarvis/v2/voice_link.py`, `windows/voicewire.py`, `jarvis/v2/daemon.py` (the `"voice"` via only), the Voice chat helper, `tests/v2/voice_link_va245_*` | — |
| **W1: typing, actions, flow** | `windows/voicetype.py`, `windows/voiceactions.py`, `windows/voiceflow.py`, `windows/voice_agent.py` | the wire API and the W2 interfaces below (fakes in tests) |
| **W2: overlay + wake** | `windows/voiceui.py`, `windows/voicewake.py`, `jarvis/voice_link_setup.py` (add `sherpa-onnx` to the pip list only) | — |

Merge order D, W2, W1. All three branches start from the same base commit (this file, the
interface stubs, the SELF_PROTECTED entries and the acceptance tests).

## Wire protocol additions (`jarvis-voice-link/1`, after the handshake)

| Request | Reply |
|---|---|
| `{"op":"dictate","id","wav_b64","source":"hotkey"\|"wake"}` (`source` defaults to `"hotkey"`) | as VA-1. `"wake"` transcribes with the desk's speaker check; a refusal is `ok:false` with a fixed sentence |
| `{"op":"route","id","text","source"}` | `{"id","ok":true,"intent":{"kind","text","target"}}`; `kind` ∈ `dictate, ask, open_app, switch_app, search, read_selection`. Never fails: on any model error or invalid output it answers the fallback (`dictate` for hotkey, `ask` for wake) with the original text |
| `{"op":"ask","id","text"}` | zero or more `{"id","event":"delta","text"}` lines, then `{"id","ok":true,"done":true,"text":<full answer>}`, or `ok:false` with a fixed sentence. The question goes to the Voice chat as `UserMessage(origin="owner", via="voice", desk=False, spoken=True)`; `"voice"` joins `VIAS` and `NEVER_STEER` |
| `{"op":"say","id","text"}` | `{"id","ok":true,"wav_b64"}`: the daemon's own TTS, text capped |

`voicewire.Client` gains `dictate(wav, source="hotkey")`, `route(text, source) -> dict`,
`ask(text, on_delta=None) -> str` and `say(text) -> bytes`; errors raise `LinkError` as before.

Model output is untrusted: `voice_link.parse_intent(raw, source, text)` validates kind, caps
`text` (2,000) and `target` (80), strips control characters, and falls back on anything else.
No intent can run a command line: `open_app` only launches an entry of the Start menu index,
`switch_app` only focuses an existing window, and `search` only opens a fixed search URL with
the query encoded.

## Windows interfaces (fixed; stubs in this base commit)

`voiceui.Overlay(root)`, thread-safe via a queue, drawn on the Tk thread:
`set_state(state, text=None)` with `state` ∈ `hidden, listening, transcribing, thinking,
answering, done, error`; `push_level(rms: float)`. Pure helpers: `bars(levels, n) -> list[float]`
(0..1, newest last), `place(screen_w, screen_h, w, h) -> (x, y)` (bottom middle, 80 px margin).
It never takes focus (`WS_EX_NOACTIVATE`).

`voicewake.WakeListener(on_utterance, on_level, busy, on_state=None)`: `start() -> bool`
(False with `reason` when sherpa-onnx, the model or the mic is missing), `stop()`. Calls
`on_utterance(wav_bytes)` after "Jarvis" plus speech up to the endpoint. Ignores audio while
`busy()` is true (hotkey recording, or Jarvis speaking). Pure helpers: `Endpointer`,
`keywords_text(pieces)`, `verify_model(directory)`.

`voiceflow.Flow(client, typer, actions, ui, player)`: `on_clip(wav, source)` runs the whole path:
dictate, the local pre-check, route when needed, then type, act or ask (answer streamed to the
overlay and spoken through `player`). The wake word's `dictate` intent is treated as `ask`.

## Not in these packages
Cleanup pass (VA-3), streaming STT partials and latency fixes (VA-6), a second wake phrase.
