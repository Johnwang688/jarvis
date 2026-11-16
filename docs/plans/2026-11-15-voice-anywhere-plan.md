# Voice anywhere: Infina-style voice outside the HUD (plan, 2026-11-15)

Status: **proposal**. Nothing is built. Decisions below are the owner's answers of
2026-11-15; everything else is a default the owner can change.

## Why

The owner saw Infina (infina.so, Mac/Windows): hands-free voice that types into
whatever app has focus, with spoken commands ("send it", "open Slack", search with a
pop-up answer). Jarvis voice (V0–V15, [rework plan](2026-10-13-voice-rework.md),
[follow-ups](2026-10-15-voice-followups.md)) is strong *inside* the HUD's Chrome tab —
local wake word, Silero VAD, local Parakeet STT, streaming Kokoro TTS, barge-in,
send/cancel words — and absent everywhere else.

Owner's pain points (2026-11-15): **only works in the HUD**, **too slow**, **can't do
things by voice**. Mishearing was not picked (note: no package has yet been accepted
with the owner's real voice or mic).

## Owner decisions (2026-11-15)

| # | Question | Answer |
|---|---|---|
| VA-D1 | How voice starts outside the HUD | **Both**: a global hotkey dictates into apps; the wake word opens a Jarvis question pop-up and **never types**. |
| VA-D2 | Where dictation may type | **Any app**, terminals included. |

Consequence of D1 + D2: keystrokes into other apps only ever follow a **physical
hotkey press**, so a TV or another voice cannot type into a terminal. The wake word
path has no typing capability at all.

## Boundaries that still hold

- **The agent gains nothing.** Dictation is the owner's transcribed words sent as
  keystrokes to the focused window. No model tool gets "type into any app"; the
  desktop tools keep their registered-app allowlist and their exclusions
  ([desktop notes](../notes/desktop.md), CLAUDE.md *Integrations stay scoped*).
  Voice actions that *the model* performs (VA-5) go through existing tools only.
- **Speech never authorizes an approval** (CLAUDE.md, *Delegation inherits limits*).
  D2 does not override this. When the foreground window is the HUD (or any window
  showing an approval card), dictation must not send raw keystrokes: text goes to the
  HUD's own composer, and "send it" never becomes an Enter on a focused button.
- Dictation is not speaker-verified (owner, Phone section) — acceptable because the
  hotkey gates it.
- Local-first audio: no audio to the cloud unless the existing STT cloud-fallback
  switch is on; nothing written to disk.
- Windows-side code is started by the owner, like the bridge.

## Architecture

```
Windows (owner-started)                      WSL daemon
┌──────────────────────────────┐   dial-in   ┌───────────────────────────┐
│ voice agent (extends          │ ──────────▶ │ stt_local (Parakeet)       │
│ windows/bridge.py or sibling) │   8404-     │ command grammar            │
│  • global hotkey (hold/tap)   │   style     │ fast-path chat (v1 loop)   │
│  • WASAPI mic capture         │ ◀────────── │ TTS (Kokoro) for pop-up    │
│  • wake word (sherpa-onnx KWS)│  text/audio │ timings log                │
│  • overlay pop-up (topmost)   │             └───────────────────────────┘
│  • SendInput into focus       │
└──────────────────────────────┘
```

Why the Windows side: a global hotkey, the focused window and `SendInput` exist only
there, and capturing on Windows avoids WSLg's mic latency. The bridge already dials
into WSL, so it needs no firewall rule and no address discovery.

## Packages

| Pkg | What | Notes |
|---|---|---|
| **VA-0** | **Latency receipt.** Timestamp each stage in the HUD path: end of speech → STT text → first model token → first audio out. Show it in Settings → Voice → Diagnostics. No behaviour change. | Do first: "too slow" needs a number per stage before anything is tuned. Real numbers need the owner at the mic. |
| **VA-1** | Windows voice agent: hotkey push-to-talk, WASAPI capture, stream to daemon STT, overlay shows live transcript. | Default hotkey: hold Right Ctrl (tap = toggle). Owner-started, like the bridge. |
| **VA-2** | Dictation into the focused app via `SendInput` (Unicode), with local command words: "send it" (Enter), "new line", "scratch that" (undo the last insert). HUD-window guard per *Boundaries*. | Deterministic grammar, no model call. |
| **VA-3** | Cleanup pass: punctuation, fillers ("um"), capitalisation. Local rules first; an optional Haiku pass behind a setting. | Haiku adds a round trip; off by default until VA-0 says there is room. |
| **VA-4** | Wake word outside the HUD → question pop-up. Answer streams into the overlay and is spoken; uses the fast path. Only one wake listener fires (the HUD tab's or the agent's, never both). | KWS via sherpa-onnx (V0: Python wheel exists). Pop-up has no typing and no approval controls. |
| **VA-5** | Voice actions: "open/switch to <app>", "search <x>" (answer in pop-up), "read this" (selection). Local grammar first; anything else goes to the model with its existing tools. | Opening any app is the owner's own command, executed by the agent process, not by a model tool. |
| **VA-6** | Latency fixes chosen from VA-0 (endpoint hangover, streaming STT partials, earlier TTS start, model choice for the fast path). | Scoped after VA-0. |

Order: VA-0 and VA-1 in parallel → VA-2 → VA-4 → VA-5 → VA-3/VA-6 as measured.

## Open questions (defaults in use until answered)

1. Hotkey: hold Right Ctrl? (default yes)
2. VA-3 Haiku cleanup on by default? (default off)
3. When the HUD tab is open and listening, which wake listener owns "Jarvis"? (default: the HUD tab while it is focused, the agent otherwise)
