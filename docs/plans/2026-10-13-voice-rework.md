# Plan: voice rework, dictation that keeps every word and replies read aloud (2026-10-13)

_Planner's proposal, written read-only against `claude/intelligent-mayer-inpj4u`.
Line numbers are from that checkout. **(unverified)** marks claims about a
library, the browser or the owner's machine that a package must measure
before relying on them._

## For the owner: what this gives you

- **Dictation stops losing words.**
  - A pause to think no longer ends the message and drops the rest. Talking
    again shortly after continues the same message.
  - There is no 30 s limit.
  - Say **"over"** to send at once, or **"cancel"** to throw the message away.
  - If transcription fails, the audio is kept and you get a Retry button.
- **Talking into a busy chat steers it**, as typing does.
- **TV, other people and noise are filtered** by a speech model and noise
  suppression. Once you record a voice sample in Settings → Voice, Jarvis
  acts only on your voice. The sample never leaves the PC. A **Guest voice**
  switch turns that check off so a friend can use Jarvis by voice.
- **Replies are read aloud again.** This covers every open chat pane, hidden
  ones included, for Claude, Codex and fast-path chats, including mid-task
  updates.
  - Code, diffs and tool calls are skipped.
  - Long replies are read as a short summary.
  - Each chat has its own voice, and one shared queue means two chats never
    talk at once.
- **Pause/resume, skip and stop** are buttons beside the orb, or you can say
  "Jarvis pause / resume / skip / stop". Talking over him pauses him: he
  carries on if you only gave a command, and skips ahead if you sent a
  message. **No spoken word ever answers an authorization card.**
- **Settings → Voice gains** a mic diagnostics view that says why anything
  was dropped, plus speaker enrollment, timings, send words and per-chat
  voices.

## 1. Diagnosis: each complaint and its root cause

**Words after a pause are lost.**
- An utterance closes after 1.5 s of quiet (`HANGOVER_MS`,
  `hud/src/lib/vad.ts:14`, applied at `:132`).
- It is sent only if *claimed*: push-to-talk, a wake hit, or the follow-up
  window (`hud/src/lib/capture.ts:113`). An unclaimed one is discarded with
  no status (`capture.ts:125`).
- A wake hit claims exactly one utterance (`capture.ts:116`), so the second
  half of a paused sentence goes nowhere.
- The v2 follow-up window opens only at the selected chat's `turn_finished`
  (`hud/src/App.tsx:1148`), not when speech ends, because nothing speaks.
- REVIEW drops the transcript in the box (`App.tsx:626`) and opens no window
  at all.

**Dictation into a working chat is ignored.**
- `suppressed()` includes the selected chat being busy (`App.tsx:549–557`).
- In AUTO, the first half of a sentence starts a turn, and that turn then
  suppresses the second half.
- Typing while busy already steers (`App.tsx:658`).
- Push-to-talk on a busy chat *interrupts* the turn (`App.tsx:2471`).

**Long dictation is cut.**
- `MAX_UTTER_MS = 30000` (`vad.ts:16`, `:128`) closes the utterance, and the
  rest re-opens unclaimed.
- Push-to-talk reads a 40 s ring (`vad.ts:11`). `Ring.slice` returns only
  what is left (`vad.ts:46–54`), so a longer hold loses its start.

**"Doesn't send reliably."**
- **A failure discards the WAV.** `STT FAILED` is shown (`App.tsx:604`) and
  there is no retry.
- **No client deadline.** `api.stt` is a bare `fetch` (`hud/src/api.ts:353`).
- **A long server worst case.** `llm.transcribe` defaults to a 60 s timeout
  × 3 attempts (`jarvis/llm.py:490–526`), for each of two models
  (`jarvis/voice.py:378`, `jarvis/config.py:792–805`), so TRANSCRIBING can
  last minutes.
- **No ordering.** Each utterance awaits STT independently (`App.tsx:579`),
  so two quick ones can arrive out of order.
- **Silent drops** also come from:
  - a picker, chip overlay or Settings being open (`App.tsx:554–555`);
  - the `longEnough` peak rule (`vad.ts:157`), which drops quiet speech in a
    noisy room.
- **Suspected (unverified):** frames lost to main-thread stalls, since the
  deprecated `ScriptProcessorNode` runs on the main thread (`App.tsx:2589`).

**TV, other voices and noise get in.**
- The detector is energy-only: RMS against floor × 3 (`vad.ts:94–99`).
- With a TV talking, 1.5 s of quiet never comes. The utterance runs to the
  30 s cap carrying TV audio, which is the effect v1 recorded
  (`docs/notes/voice-face.md`, "Voice round 2").
- Nothing knows *whose* voice it is.
- The wake word is Chrome's `webkitSpeechRecognition` (`App.tsx:2386–2443`).
  It streams room audio to Google whenever the mic is not OFF
  (`lib/dictation.ts:8–10`), and fires on any voice saying "Jarvis".

**Jarvis no longer talks.**
- `api.say()` (`api.ts:374`) is never called, and nothing sets the orb to
  `speaking`. It is only read (`App.tsx:550`, `state/store.tsx:724`).
- `POST /say` survives (`jarvis/v2/hud_api.py:1420–1425`, 2000-character
  cap). It calls `voice.tts()`: local Kokoro (`bm_george`, `config.py:219`)
  or Pocket for `pocket:` names, with an OpenRouter fallback if local fails
  (`voice.py:343–358`).
- The v2 design §12.2 (`docs/jarvis-v2-design.md:1389`) lists "TTS
  speculation" as carried over. **That is false.** v1's `_sentences`,
  `_stable_chunks` and `_Speculator` (`jarvis/face/server.py:140, 190, 209`)
  were never ported.
- Also, mic OFF posts `/mute` (`App.tsx:2738`). That is v1's *voice-out*
  flag (`jarvis/tools/voicectl.py`), which nothing in v2 reads, so the two
  mutes are coupled.

**Where agent prose comes from.** Every event arrives on the one `/events`
SSE stream (`api.ts:386`) and is routed to the panes showing its thread
(`routeEvent`, `lib/chats.ts:185`). Tool calls are always separate
`tool_started`/`tool_finished` events. Code and diffs are inside the prose's
markdown.

| Provider | Streamed prose | Message end | Mid-task prose |
|---|---|---|---|
| Fast path (`v2/providers/fastpath.py:807–816`) | `text_delta`, every step | final `text`: continuations joined, may end `[cut off at the token limit]` | Streams as deltas, then is re-sent as `thinking` (`jarvis/agent.py:815`) before `tool_started`. The HUD ignores `thinking`, and `op_start` clears the draft (`store.tsx:480`). Diagnostics (`[session not saved: …]`, `agent.py:608, 622, 925`) come as `thinking` **without** deltas. |
| Claude (`providers/claude.py:1561–1600`, partial messages on at `:805`) | `text_delta` from text deltas only (`:2111–2121`) | one `text` per `TextBlock` | Each interim block is its own `text` |
| Codex (`providers/codex.py:1027–1062`) | `item/agentMessage/delta` | `text` when an `agentMessage` completes | Each interim `agentMessage` |

## 2. Owner decisions (2026-10-13)

| # | Decision |
|---|---|
| V-1 | **End of utterance has two triggers: a smart pause and a send word.** <ul><li>An ML VAD with noise suppression replaces the energy VAD.</li><li>Pause tolerance is about 2.5 s, tunable.</li><li>Speech resuming within a short window continues the same message.</li><li>No hard cap; long dictation is transcribed in chunks.</li><li>A trailing "over" sends at once and is stripped. A trailing "cancel" discards the message in progress.</li></ul> |
| V-2 | **Noise covers other voices, TV and video, and non-speech noise.** <ul><li>ML VAD and noise suppression now.</li><li>Speaker enrollment and verification: local only, owner-only store, never leaves the PC. Enrolled in Settings → Voice when the owner is at the PC; off until then.</li><li>A mic diagnostics view.</li></ul> |
| V-3 | **Read aloud all agent prose** from Claude, Codex and fast-path chats as it streams, mid-task updates included. Skip code blocks, diffs and tool calls. |
| V-4 | **Long replies become a spoken summary** from a cheap, non-Chinese-hosted model on the Haiku 5.5 tier. On failure, read truncated prose. |
| V-5 | **Speakers are the worst case:** echo cancellation and self-voice gating. Speaker verification also rejects Jarvis's own voice. |
| V-6 | **Voice commands always start with "Jarvis":** pause, resume, skip, stop. Buttons do the same. **Never** an answer to an approval card. |
| V-7 | **Barge-in pauses, then resumes.** If it was a command or nothing was sent, playback resumes. If a message was sent, the current item is skipped. |
| V-8 | **Every open chat pane is read**, drawn or hidden, each in its own voice, through **one shared queue**. Threads in no pane are silent. |
| V-9 | **Distinct voice per chat**, auto-assigned from local Kokoro and Pocket voices, sticky per thread, changeable from the chat header. |
| V-10 | **Dictating into a busy chat steers** the running turn. |
| V-11 | **STT robustness:** timeouts, retries, errors surfaced, audio kept for retry. Local STT and local keyword spotting are evaluated with a recommendation. |
| V-12 | **The speaker store is not added to the protected-path detector** (owner, 2026-10-13, answering this plan's sign-off request). It is non-destructive configuration the owner can easily put back, so it does not need another agent-blocked path. It stays local, mode 0700, and is never uploaded. |
| V-13 | **Spoken summaries default to Haiku 5.5** (`anthropic/claude-haiku-5.5`) and stay under the 90k-token threshold, as compaction does now (`JARVIS_COMPACT_AT_TOKENS`). The owner reports Haiku at about 100–200 tokens/s, fast enough for speech. |
| V-14 | **A switch turns speaker verification off ("Guest voice")** so a friend can demo or use Jarvis by voice. It does not change ML VAD, noise suppression or any approval rule (§3.4.1). It **stays on until the owner turns it off**, surviving HUD reloads and daemon restarts. |

## 3. Design

### 3.1 Capture pipeline

```
getUserMedia({echoCancellation, noiseSuppression, autoGainControl: true})
 └ AudioWorklet: 48k→16k (windowed-sinc), 512-sample frames
     ├ ring (40 s, pre-roll only)    ├ level → orb
     └ Worker "listen": [RNNoise?] → Silero VAD → KWS (§3.6) → {p_speech, hits}
Capture (main): claim (PTT | wake | follow-up | continuation) → UtteranceRecorder
   → segments at short gaps → /stt per segment {utterance_id, seq}
   → assembler → send | steer | review box | discard — every drop has a DropReason
```

- **AudioWorklet replaces `ScriptProcessorNode`** (`App.tsx:2589`), as a
  bundled same-origin module. CSP already allows `script-src 'self'
  'wasm-unsafe-eval'` and `worker-src 'self'` (`hud_api.py:1073–1074`).
  Today's boxcar resample (`App.tsx:2590–2604`) aliases.
- **Constraints are requested explicitly** and read back with
  `getSettings()` for diagnostics. Chrome's defaults for `{audio: true}` are
  believed all-true **(unverified, recorded in V1)**.
- **The ring only supplies pre-roll.** A claim starts an
  `UtteranceRecorder`: pre-roll plus appended frames, with no cap and
  unlimited push-to-talk. A 15-minute safety ceiling ends dictation with a
  status line, never silently.
- **Every drop has a reason.** `Capture.onDrop(reason, ms)` takes a closed
  enum: `unclaimed`, `too_short`, `suppressed:<card|picker|settings|crash>`,
  `muted`, `self_voice`, `not_owner`, `cancelled`, `empty_transcript`,
  `stt_failed`.
- **Privacy rules unchanged:**
  - OFF claims and uploads nothing.
  - `micFloorSample` clamps uploads at the last unmute (`capture.ts:85–88,
    136`).
  - A crashed window sends nothing (`App.tsx:592`).
  - Unclaimed audio never leaves the window.

### 3.2 End of utterance and continuation (V-1)

- **Silero VAD v5 (ONNX, about 2 MB) in a Web Worker** through
  `onnxruntime-web` WASM, bundled and served by the daemon (no CDN, works
  offline).
  - Recommended over the daemon: always-open audio stays in the window as
    today, and frame decisions need no hop.
  - CPU cost **unverified (V0)**.
  - The energy `Vad` remains the fallback, and diagnostics name the
    detector in use.
- **`Vad` takes a speech probability instead of RMS:** it opens at p ≥ 0.5
  sustained for 60 ms and closes below 0.35. Constants stay in samples.
- **Three tunable timers:**
  - **Segment gap, 700 ms.** Closes a *segment* and starts its STT at once,
    which hides latency. The message stays open.
  - **Message pause, 2.5 s.** Ends the message.
  - **Continuation window, 6 s after a message ends.** A new onset is
    claimed without the wake word and **continues** it. In REVIEW the text
    is appended to the box. In AUTO, if the message was sent and its turn is
    running, it is sent as a **steer** (V-10); otherwise it is the next
    message.
- **The follow-up window means "speech ended" again.** It opens when the
  selected chat's spoken reply finishes, as v1's `doneSpeaking()` did. With
  read-aloud off, it opens at `turn_finished`, as now.
- **Smart end-of-turn model (later, optional).** Pipecat's Smart Turn is the
  candidate **(unverified: size, latency, licence)**. It is adopted only if
  V0 shows it beats the plain timer on the owner's own dictation.

### 3.3 Send and cancel words (V-1)

- **Phase 1 (transcript).** At message end the normalised text is checked:
  - A final `over` is stripped and the message **sends, even in REVIEW**.
  - A final `cancel` discards every segment since the last send. The line
    shows `CANCELLED · UNDO` for 10 s; the text is held in memory.
  - A message already sent is not recalled.
- **Phase 2 (KWS, after V11).** "over"/"cancel" followed by ≥ 500 ms of
  silence ends the message at once. If the transcript does not end with the
  word, the hit counts as a false alarm and the message takes the normal
  pause path.
- **Collision** ("the game is over"). Mitigations: trailing silence; a
  ≥ 200 ms pause before the word, once KWS timestamps exist; editable phrase
  lists; an on/off toggle. Open question 1.

### 3.4 Noise, speaker verification and self-voice (V-2, V-5)

- **Noise suppression.**
  - Chrome NS stays on.
  - V0 measures whether RNNoise (WASM) in the worker improves VAD precision
    with the TV on **(unverified)**.
  - The denoised stream feeds VAD, KWS and verification. STT gets the
    echo-cancelled stream, since heavy suppression can raise the word error
    rate (V0 measures both).
- **Verification runs on the daemon and sees only claimed audio.**
  - A speaker-embedding model (ECAPA-TDNN or CAM++ through `sherpa-onnx`'s
    speaker-embedding extractor) **(unverified: Python 3.14 wheel, CPU
    latency)**.
  - `/stt?verify=1` embeds the first ≤ 3 s and checks cosine similarity to
    the enrolled centroid.
  - On a fail it returns 422 `not the enrolled speaker` **before any cloud
    STT call**, so other voices never reach the cloud.
  - Clips under 1.2 s (commands) use a relaxed threshold; a command only
    controls playback.
- **Enrollment** (Settings → Voice → Your voice).
  - Three prompted readings of about 10 s, at the usual seat, with a live
    meter and quality checks (clipping, level, speech share).
  - The daemon scores a held-out reading against the enrollment and against
    every Kokoro and Pocket builtin (free impostors), then **proposes a
    threshold** and shows both distributions.
  - Verification switches on only after the owner accepts.
  - Until then the section says "Not enrolled: anyone's voice is accepted"
    and the dependent gates are skipped.
- **The store** is `~/.local/share/jarvis/speaker/`, mode 0700 like the
  Pocket voice store.
  - It holds the centroid, threshold, model id and reference clips (open
    question 12).
  - **It is not added to the protected/credential path detector (V-12).**
    The owner judged it non-destructive configuration that is easy to put
    back. The worst an agent edit can do is make verification reject the
    owner or accept another voice. Either is visible in diagnostics and
    fixed by re-enrolling or switching Guest voice on. Neither can authorize
    anything, because speech never answers an approval card (V-6). No route
    returns its audio or embeddings, and nothing uploads it.
  - Weights are prefetched. `HF_HUB_OFFLINE=1` stays the default, and no
    path uploads the store.
- **Self-voice gating** applies during playback and 300 ms after it:
  1. **Chrome AEC.** V0 measures whether Chrome on Windows cancels Web Audio
     output from the same page, or only WebRTC peer audio
     **(unverified)**.
  2. **Reference correlation.** The worker receives the playback PCM. An
     onset whose envelope the delayed reference explains is `self_voice`.
  3. **Verification**, once enrolled, rejects Jarvis's voices.
  4. **Wake and command hits** during playback must pass (2), and (3) when
     enrolled. A reply that says "Jarvis stop" never stops itself.

### 3.4.1 Guest voice: the verification off switch (V-14)

- **What it means.** "ML voice recognition" here is speaker verification,
  the part that accepts only the owner's voice. Guest voice skips it, so
  anyone in the room can dictate and give "Jarvis …" commands.
- **What stays on.** ML VAD, noise suppression and self-voice gating keep
  running, because they help every speaker. Steps (1) and (2) of
  self-voice gating still apply; step (3) is skipped.
- **Approvals.** Speech never answers an approval card (V-6), so Guest
  voice gives a friend no new authority, only the microphone.
- **Where it lives.** It is a switch in Settings → Voice → Your voice, plus
  a one-click toggle on the speech strip. It is held on the daemon, beside
  the store, so `/stt?verify=1` reads one source of truth, and it is
  published on the bus so every HUD window agrees.
- **What it shows.** While it is on, the orb line and the speech strip say
  **GUEST VOICE**, so the owner never forgets it is on. Diagnostics records
  `verify_skipped_guest` instead of a score.
- **Before enrollment.** It reads "Guest voice (no voice enrolled)" and is
  effectively always on, since there is nothing to verify against.
- **How long it lasts (owner, 2026-10-13).** Until the owner switches it
  off. It is persisted beside the store, so it survives HUD reloads and
  daemon restarts. There is no timer. The always-visible **GUEST VOICE**
  label is what keeps it from being forgotten.

### 3.5 STT reliability (V-11)

- **Audio is kept until its text is placed**, at most 20 clips or 10
  minutes, in memory only and gone on reload.
  - Failure shows `STT FAILED · RETRY`.
  - One automatic retry happens after 2 s.
  - Diagnostics lists the kept clips with Play and Retry.
- **Deadlines.** The client aborts at `max(15 s, 2 × clip)`. On the daemon,
  dictation gets a 20 s per-attempt timeout and one retry per model, instead
  of the 60 s × 3 default (`llm.py:496–497`).
- **Ordering.** `{utterance_id, seq}` on each segment; the assembler places
  text in spoken order.
- **Chunking.**
  - Segments are cut at gaps. Without a gap, a hard cut at 25 s with 0.5 s
    of overlap, deduplicated by the longest common word run.
  - At most two requests in flight. The 48 MiB body cap (`v2/daemon.py:1956`)
    is no constraint.
- **Local STT is recommended** (open question 6).
  - Parakeet-TDT-0.6B-v3, today's cloud model (`config.py:793`), has open
    weights. It runs as int8 ONNX through `sherpa-onnx` or `onnx-asr`
    **(unverified: real-time factor (RTF) on the owner's CPU, Python 3.14
    install)**.
  - Same model class, no outage path, and it meets "local voice
    privacy/offline".
  - Cloud stays as a switchable fallback ("Never send audio to the cloud").
  - It contends with Kokoro's single-instance lock (`voice.py:281`);
    measured in V0.

### 3.6 Wake word and commands (V-6, V-11)

- **Local keyword spotting replaces Chrome's cloud recognizer.**
  - Lead candidate: **sherpa-onnx open-vocabulary KWS**. A keyword list
    with no training covers "jarvis", the commands, "over", "cancel" and
    avatar wake phrases.
  - Alternative: **openWakeWord**. It ships "hey jarvis" but needs training
    for bare "jarvis" and each command **(unverified: accuracy on the
    owner)**.
  - Avatars' wake patterns are regexes (`compileWake`, `App.tsx:2156`). KWS
    uses the literal `wake` list, and a regex-only avatar keeps Chrome
    until it has one.
- **Placement.** Preferred: the listen worker (WASM), so room audio stays
  in the window. If V0 finds that too heavy, the daemon over a local
  WebSocket, following `jarvis/v2/ws.py`. Either way, Google stops hearing
  the room.
- **Phase 1 commands (V9).** A claimed utterance whose *whole* normalised
  transcript is "jarvis pause|resume|skip|stop" (fillers allowed) is a
  command and is never sent. "Jarvis, stop the build" is a message.
- **Phase 2 (V11).** A KWS hit plus ≥ 600 ms of silence acts in under a
  second.

### 3.7 From events to sentences (V-3)

This is new HUD code (`lib/speech/*.ts`); synthesis stays `/say`.

1. **Source.** `text_delta` for a thread shown in any chat pane feeds its
   `ProseStream`. Fast-path diagnostics have no deltas, so they are never
   spoken.
2. **Boundaries.** A message closes on `text`, on `tool_started` (which
   flushes any partial sentence), on `turn_finished`, or on a stop. A `text`
   that differs from the stream (the fast path's joined continuations)
   contributes only its unspoken remainder, and a trailing `[cut off …]` is
   dropped.
3. **Streaming markdown filter.**
   - It is fence-aware across delta boundaries.
   - It drops fenced blocks, diff hunks and indented code that follows a
     blank line and is not a list. Long tables become "a table".
   - Paths become their basename, URLs "a link", and hashes and ids "a
     commit" or "an id".
   - Then the `voice.speakable()` rules, ported to TS against
     `tests/voice_speakable_check.py`.
4. **Segmentation.**
   - A TS port of `_sentences`/`_stable_chunks`, keeping v1's short-opener
     and stable-chunk rules. A chunk is queued once stable.
   - **Speech starts while the model writes.** This deliberately reverses
     v1's audio-after-the-turn rule, because the owner asked for mid-task
     prose (V-3).
5. **Synthesis.**
   - `POST /say {text, voice, local_only: true}`, prefetching 2 ahead.
     Kokoro serializes on one instance, so more would only queue.
   - `local_only` skips the cloud fallback. On a local failure the chunk is
     skipped and marked (open question 11).
   - `/say` also **validates `voice` against `catalog()`**. Today an unknown
     explicit name fails in Kokoro and reaches the cloud carrying that name.

### 3.8 Spoken summaries (V-4)

- **Rule (open question 2).**
  - Prose ≤ **600 characters** (about 35–40 s at the 1.2× default,
    `config.py:224`) is read verbatim as it streams.
  - Longer prose: verbatim stops after the sentence crossing 400 characters.
    When the message closes, `/speech/summary` returns 1–3 sentences
    (≤ 300 characters) covering **the rest**, without repeating the opening
    or reading code.
- **Model (V-13).**
  - A new tier, `speech_summary` (`JARVIS_SPEECH_SUMMARY_MODEL`), defaulting
    to `anthropic/claude-haiku-5.5`, the owner's choice. The existing `cheap`
    tier is `gpt-oss-20b` (`config.py:937`), so it is not used.
  - **Its input stays under the compaction threshold**
    (`JARVIS_COMPACT_AT_TOKENS`, default 90k), as conversations do now. That
    keeps every summary request below Haiku 5.5's 100k price step (CLAUDE.md,
    "Model default update"). A reply too long to fit is condensed in
    sections first, and the sections' summaries are then summarized. One
    request is the normal case; a 90k-token spoken reply is unusual.
  - Screened with `models.cn_hosted` (`jarvis/models.py:641`), as
    `titles.openrouter_model()` does (`v2/titles.py:184–191`).
  - Input passes `scrub()` and `strip_invisible`. One-shot, `cache=False`,
    with a **6 s deadline**.
  - On failure, the next 400 characters are read, then "…and more on
    screen."
- **Estimate (unverified, measured in V8).** At $0.10/M in and $0.50/M out
  (CLAUDE.md), about 2.5k tokens in and 120 out ≈ $0.0003 per summary, so
  100 a day ≈ $0.03. At the owner-reported 100–200 tokens/s, 120 output
  tokens take about 0.6–1.2 s plus time to first token, hidden behind the
  verbatim opening.

### 3.9 Per-chat voices and the shared queue (V-8, V-9)

- **Speakers.** A thread speaks if any chat pane in `ws.panes` (drawn or
  hidden, `lib/workspace.ts:80`) shows it.
  - Task views and threads in no pane are silent.
  - The queue follows the thread across panes. A thread leaving every pane
    has its items dropped, and its playback stops at once.
- **Voices.**
  - The first thread takes the avatar voice (`voice.voice_for`). Later ones
    take the next unused voice from an ordered pool: English Kokoro `a*`/`b*`,
    then Pocket builtins when installed.
  - Sticky per thread, stored by the daemon in
    `~/.config/jarvis/speech.json` and pruned with deleted threads. It is not
    a `Thread` field: the live session saves `Thread` too
    (`hud_api.py:1797–1800`).
  - Changed from a **voice chip in the pane's view strip**, beside the
    selected-chat mic mark (`components/Workspace.tsx:347`). The chip
    pulses while that pane speaks.
- **Queue items** are `{thread, message_key, seq, voice, kind:
  verbatim|summary|fallback, text, state}`.
  - Item states: `pending → synthesizing → ready → playing → done`;
    `paused` (owner) and `held` (barge-in) off `playing`; `skipped` and
    `dropped` are terminal.
  - Player states: `idle`, `playing`, `paused`, `held`, `stopped`.
- **Ordering rules:**
  1. One item at a time, never overlapping.
  2. A message holds the floor: once it starts, its later chunks go before
     other threads' items, so messages never interleave.
  3. Between threads, messages go in order of their first stable chunk.
  4. **Floor timeout:** a holder with nothing ready for 4 s (a slow model or
     a tool running) yields, and its rest re-queues.
  5. **Stale drop:** a send to a thread by any means drops its unplayed
     items and skips its playing one (V-7). Stop or an interrupted
     `turn_finished` drops that turn's items.
  6. **Backlog cap:** more than about 60 s unplayed in one thread collapses
     its oldest *interim* items ("…skipping 3 updates"). Final replies are
     never collapsed.
- **One speaking window.** A Web Lock (`navigator.locks`, `"jarvis-speech"`)
  picks it. Focusing a window steals the lock, and the others build no
  queue.
- **Playback.**
  - Web Audio (`decodeAudioData`, `AudioBufferSourceNode`) gives exact
    pause offsets and an output level for the orb. `<audio>` with `blob:` is
    out, because CSP `media-src 'self'` (`hud_api.py:1079`) excludes `blob:`.
  - Resume restarts the paused sentence (open question 13).

### 3.10 Controls, commands, barge-in (V-6, V-7)

- **Buttons** (open question 9).
  - A **speech strip under the orb** in the sidebar foot: ⏸/▶, ⏭ skip,
    ⏹ stop and clear, and "Speaking: <chat> · N queued". It folds into the
    rail with the orb.
  - Each pane's voice chip adds "Skip this chat" and "Mute this chat".
- **Commands.**
  - pause; resume (from `paused` or `held`); skip (the rest of the current
    message); stop (clear the queue).
  - **None stops an agent turn** (open question 8).
  - The speech controller has no handle on approvals, `decide` or
    `/approve`.
  - A test pins that "Jarvis approve/authorize/yes/allow" with a card up
    reaches no approval route. Mutation: wiring commands to `decide` must
    fail it.
- **Under a card** (open question 10):
  - Dictation stays suppressed (`App.tsx:553`): nothing is uploaded or sent.
  - Playback commands work **through local KWS only**, so before V11 there
    are none under a card.
- **Barge-in state machine** (Settings: "Pause when: any voice / my verified
  voice / only 'Jarvis' or Ctrl+D", open question 7):

  ```
  playing --onset (VAD ∧ ¬self_voice)--> held
  held --command--> run it (pause stays paused; resume/skip/stop act)
  held --dropped / landed in REVIEW box / cancelled / not_owner / 10 s--> playing (restart sentence)
  held --message sent or steered--> skip current item, continue queue
  ```

### 3.11 Steering while busy (V-10)

- Remove `chats[selectedChat].busy` from `suppressed()` (`App.tsx:552`).
  The transcript goes through `send()`, which already steers or queues
  (`App.tsx:658`, `steer_queued`).
- **Push-to-talk stops interrupting** (`App.tsx:2471`). Stop remains the
  interrupt (open question 4).
- The other suppressors stay: card, picker, chip overlay, Settings, crash,
  no chat. `orb === "speaking"` gives way to self-voice gating (§3.4).
- **Where speech goes** stays W-6, the selected chat. Using the follow-up
  window that a *drawn* pane's reply opened selects that pane first, and
  the mic mark moves (open question 5). Hidden panes open no follow-up
  window.

### 3.12 Orb, status line, diagnostics

- **The orb's `speaking` state** (`lib/orbpalette.ts:50`) is driven by
  output level; `held` uses the listening hue.
- **New `orbLine` states** (`lib/dictation.ts:72`):
  - `SPEAKING · <chat>`
  - `PAUSED · SAY "JARVIS RESUME"`
  - `LISTENING → <chat>`
  - `TRANSCRIBING 2/3`
  - `STT FAILED · RETRY`
  - `CANCELLED · UNDO`
- **Settings → Voice → Diagnostics**, memory only:
  - live: level and noise floor; VAD probability with thresholds; NS and AEC
    state from `getSettings()`; playback reference; KWS hits; speaker score;
  - the last 50 utterances: time, length, claim source, detector, outcome or
    `DropReason`, STT model, latency, with Play and Retry for clips still
    held.

### 3.13 Settings → Voice and daemon routes

**Settings → Voice** extends the embedded `VoicePicker` (`App.tsx:3745`):
- **Input:** microphone; pause (2.5 s), continuation (6 s) and segment gap;
  send and cancel words (lists, on/off); wake engine (local or Chrome); STT
  (local / cloud / local with cloud fallback); noise suppression (Chrome or
  RNNoise); barge-in sensitivity.
- **Output:** read aloud on/off, separate from mic OFF (open question 11);
  summary threshold; speed; voice pool and per-chat overrides; announce chat
  on switch (off by default); Browser ducking (§3.14).
- **Your voice:** enrollment, threshold, Guest voice (§3.4.1), delete.
- **Diagnostics.**

These settings are window-local, kept in localStorage behind try/catch.
Per-thread voices, enrollment and the STT engine live on the daemon.

**Routes.** All are HUD-origin only. Audio bodies are allowed on an
**explicit set** of routes, extending the `/stt` exemption at
`hud_api.py:1314`, never a prefix match.

| Route | Change |
|---|---|
| `POST /stt` | Gains `?verify=1`, an engine choice, the dictation deadline, and echoes `utterance_id`/`seq`. A `not_owner` 422 never calls the cloud. |
| `POST /say` | Gains `local_only`; validates `voice` against `catalog()`; returns `X-Speech-Ms`. |
| `POST /speech/summary` | New. `{thread_id, text, spoken_prefix}` → `{summary}`, per §3.8. |
| `GET`/`POST /speech/voices` | New. The per-thread map; publishes `thread_voice` on the bus. |
| `GET`/`POST`/`DELETE /speaker` | New. Status, add a reading, accept a threshold, set Guest voice, delete. Publishes `speaker_guest` on the bus. Never returns audio or embeddings. |
| `GET /voices` | Gains `voice_out`. Mic OFF stops posting `/mute`. |

### 3.14 Browser sound (`docs/plans/2026-10-13-browser-sound.md`)

Speech and Browser audio must not fight. Two recommendations, left as open
question 14 until that plan settles:
- **Duck** Browser audio that the HUD plays itself (a Remote stream) by
  about −15 dB while an item plays.
- **Hold speech** while a Direct frame, which is cross-origin and cannot be
  ducked, plays sound, using that plan's audible flag.

Browser audio is also an echo source on speakers that the §3.4 reference
cannot cancel. Speaker verification covers it.

## 4. Packages (dependency order)

Each package is one small PR from its own worktree, with an independent
review. Every boundary change carries **mutation evidence**. Report each
package as committed, merged, built, browser-tested or live-checked.

| Pkg | What | Needs | Size | Risk |
|---|---|---|---|---|
| V0 | Spikes and measurements, no product change: AEC on speakers; WASM Silero, RNNoise and KWS cost; local Parakeet RTF; speaker-embedding equal error rate (EER); Smart Turn. Probes beside `tests/voice_probe.py`; results to `docs/notes/voice-face.md` | — | M | none |
| V1 | `DropReason`s, diagnostics view on today's pipeline, explicit constraints read back | — | S | low |
| V2 | STT reliability: kept clips, Retry, deadlines, `seq` ordering | — | S–M | low |
| V3 | Steer while busy; push-to-talk stops interrupting; continuation window | V2 | S | medium |
| V4 | AudioWorklet, resampler, `UtteranceRecorder` (no cap), chunked STT | V2 | M | medium |
| V5 | Silero VAD worker, energy fallback, the three timers | V4, V0 | M | med–high |
| V6 | Speech core: `/say` changes, markdown filter, splitter port, Web Audio player, queue (one thread), speech strip, orb, Web Lock, voice-out decoupled from mic OFF; corrects design §12.2 | — | M–L | medium |
| V7 | All open panes, per-thread voices and chip, floor, stale and backlog rules | V6 | M | medium |
| V8 | `/speech/summary`, tier, threshold, fallback; cost and latency measured | V6 | S–M | low |
| V9 | Barge-in, self-voice reference gating, transcript commands, approval-isolation tests | V5, V6 | M | high |
| V10 | Send and cancel words (transcript phase), Undo | V5, V2 | S | low–med |
| V11 | Local KWS for wake, commands and send words; Chrome recognizer retired for phrase avatars | V0, V9 | M–L | med–high |
| V12 | Enrollment, verification, store, Guest voice switch, `/stt?verify` | V0, V4 | L | high |
| V13 | Local STT with a cloud-fallback switch | V0, V2 | M | medium |
| V14 | RNNoise in the worker, if V0 shows a gain | V0, V5 | S–M | low |
| V15 | Browser ducking and hold | V7, Browser sound plan | S | low |

**Order.** V1, V2 and V6 can start at once. V3 fixes the most-felt
complaint, so it lands early. V2–V5 rewrite the same capture code, so they
merge in order.

## 5. Tests

**Free tests.** vitest and pytest; temporary HOME/config; fake providers;
ephemeral ports; run under `systemd-run --user --scope -q -p MemoryMax=3G
-p MemorySwapMax=0`.
- **Capture**, with scripted per-frame probabilities and levels via the
  `feedMs` pattern (`capture.ts:198`):
  - a 2.4 s pause continues and a 2.6 s pause ends;
  - continuation claimed at 5 s but not at 7 s;
  - no cap at 30 s, nor on a 45 s push-to-talk;
  - every drop emits its `DropReason`;
  - mute and the unmute clamp still hold;
  - self-voice frames never open an utterance.
- **Assembler:**
  - out-of-order transcripts are placed in `seq` order;
  - overlap is deduplicated;
  - `over` is stripped and sent in REVIEW;
  - `cancel` discards everything since the last send;
  - "the game is over" without trailing silence does not send.
- **Commands:** whole-utterance match only. With a card pending, **nothing
  reaches `decide` or `/approve`** (mutation required).
- **Speech:**
  - the fence filter across split deltas, plus diffs, inline code and
    tables;
  - the splitter checked against v1's vectors
    (`tests/face/speculation_check.py`);
  - `[…]` diagnostics never spoken.
- **Queue**, with a fake clock and a fake TTS (silence ∝ text):
  - no overlap;
  - floor holding and floor timeout;
  - stale drop on send;
  - backlog collapse;
  - pane close stops playback;
  - hidden panes speak, threads in no pane do not;
  - barge-in transitions.
- **Daemon:**
  - `/say` refuses unknown voices, and `local_only` never calls
    `llm.speech` (patched to raise);
  - `/speech/summary` uses its tier with the CN fallback, `scrub` and
    `cache=False`, and keeps its deadline;
  - the `/speech/voices` round trip;
  - `/stt?verify=1` with a fake embedder never calls `transcribe` on a
    rejection;
  - the speaker store is 0700;
  - **Guest voice on skips verification; off with an enrolled store rejects
    an impostor before any cloud call** (mutation: a guest flag that is
    ignored, or one that also skips self-voice gating, must fail);
  - Guest voice never changes approval handling ("Jarvis approve" with a
    card up and Guest on: zero approval requests);
  - other routes still refuse non-JSON bodies.

**Playwright against the mock daemon** (`tests/face/hud_v2_mock.py`). Chromium
runs with `--use-fake-ui-for-media-stream
--use-file-for-fake-audio-capture=<wav>`.
- Committed fixtures are synthetic Kokoro speech, **never the owner's
  voice**. They drive the real worklet and VAD.
- Mock `/say` returns tones; a `__hud.speech` hook exposes the queue and the
  active source count.
- Three panes, one hidden, stream deltas: assert order, voices and no
  overlap.
- Buttons and transcript commands.
- A card pending and "Jarvis approve": zero approval requests.
- Dictation into a busy chat steers.
- The diagnostics rows.
- Never live 8402/8403/8405.

**Live checks on the owner's PC.** The HUD runs in Windows Chrome, so audio
is the **Windows** stack (`docs/notes/voice-face.md`). WSLg's
`RDPSource`/`RDPSink` (CLAUDE.md Environment) matter only for a
Chrome-for-Testing fallback or Playwright in WSL.
1. Speakers: three chats reading, barge-in, commands.
2. Headphones.
3. TV or YouTube running: false holds and dictation accuracy.
4. Three minutes of dictation with pauses, then "over".
5. "Cancel" and Undo.
6. Enrollment, then the TV or another person saying "Jarvis"; then Guest
   voice on, and the other person dictates successfully.
7. A long Claude reply → summary.
8. STT down → Retry.
9. A card up while speaking.

## 6. Measurements

- **Echo:** ERLE on speakers with same-page Web Audio; false holds per hour
  of playback.
- **CPU:**
  - in Chrome: WASM Silero, RNNoise and KWS, as % of a core;
  - on the daemon: local STT RTF, verification latency for 3 s, and Kokoro
    time to first audio while STT runs.
- **Accuracy:**
  - VAD false accepts with the TV on;
  - KWS misses at 1 m and 3 m, and false accepts per hour with TV and
    Jarvis speech;
  - speaker EER: owner vs Kokoro/Pocket and TV;
  - word error rate (WER) cloud vs local on 20 owner dictations, kept
    locally and then deleted;
  - STT with NS on vs off.
- **Latency and cost:** speech end → text; "over" → send; summary round
  trip and cost; first audio after a chunk is stable.
- **A day's `DropReason` histogram**, before V3/V5 and after.

## 7. Open questions for the owner

1. **"Over" collisions.** *Recommend:* keep "over" but require ≥ 500 ms of
   silence after it (and a short pause before it, once KWS exists). Make the
   list editable. It also sends in REVIEW.
2. ~~Summary threshold.~~ **Answered (2026-10-13): summary only.** A
   reply up to 600 characters is read verbatim. A longer one is replaced by
   a short spoken summary (V8, Haiku 5.5 tier), with no verbatim lead-in. The
   owner approved one paid V8 cost/latency probe, capped at $0.10.
3. ~~Continuation in AUTO.~~ **Answered (2026-10-13):** as recommended. Send
   at the 2.5 s pause; continuations steer the running turn. "Over" still
   sends at once.
4. ~~Push-to-talk on a busy chat.~~ **Answered (2026-10-13):** as
   recommended. It steers, as typing does, and Stop interrupts.
5. ~~Follow-up target.~~ **Answered (2026-10-13):** as recommended. A reply
   right after a drawn pane spoke selects that pane and sends there;
   otherwise the selected chat (W-6).
6. **Local STT.** *Recommend:* make local Parakeet the default if V0 shows
   RTF < 0.3 and WER within 1 point of cloud, with cloud as a fallback you
   can switch off.
7. **Barge-in before enrollment.** *Recommend:* any non-self voice pauses
   playback. If more than 3 false pauses per 10 minutes with the TV on,
   only "Jarvis" or Ctrl+D pause it until you enroll.
8. **"Jarvis stop."** *Recommend:* it stops speech and clears the queue,
   never an agent turn.
9. **Buttons.** *Recommend:* a speech strip under the orb, plus a per-pane
   voice chip with skip and mute. Not the status column, which folds.
10. **Commands under a card.** *Recommend:* playback commands only, through
    local KWS, with nothing uploaded and no approval effect.
11. **Mute coupling and cloud TTS.** *Recommend:* mic OFF no longer mutes
    Jarvis. Read-aloud never uses cloud TTS; a local failure skips the
    chunk with a mark.
12. **Keep enrollment clips?** *Recommend:* yes, in the 0700 store, so a
    model upgrade can re-derive the voiceprint without re-recording. Delete
    removes all of it. (The store is not agent-protected; V-12.)
13. **Resume point.** *Recommend:* restart the interrupted sentence.
14. **Browser sound.** *Recommend:* duck HUD-played Browser audio by about
    −15 dB, and offer "hold speech while a tab plays sound" (off) for Direct
    frames. Final answer in the Browser sound plan.
15. **Which window speaks, and task views.** *Recommend:* the most recently
    focused window. Task views stay silent.
16. **KWS engine and placement.** *Recommend:* sherpa-onnx open-vocabulary
    KWS in the window if V0 shows ≤ 15% of a core and acceptable misses;
    otherwise on the daemon. Retire Chrome's cloud recognizer either way.
17. ~~How long Guest voice stays on.~~ **Answered (2026-10-13):** until you
    switch it off. It survives reloads and restarts, and is always labelled
    (V-14, §3.4.1).

## 8. Out of scope

- Discord voice notes.
- Spoken approval cues: a tone could come later, but never a spoken yes.
- Pocket clone tooling.
- Fast-path interim prose vanishing from the *screen* when `op_start`
  clears the draft (`store.tsx:480`). Read-aloud will speak it; showing it
  is a separate HUD fix.
