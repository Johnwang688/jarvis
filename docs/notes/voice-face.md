# Voice Face — preserved notes

Verbatim from `CLAUDE.md` at `5ef4e545432ff632fa47343743fd4b075828aa46`.
Archived instruction/history snapshot, not a new deployment receipt.
Both current obligations and dated/superseded findings appear here.
Moving a rule does not revoke it. Start with [the working guide](../../CLAUDE.md),
read this area's complete rules before changing a boundary, and resolve older
conflicts using the latest explicit owner decision and current implementation.
Paths in preserved prose are repository-relative. Source labels refer to the
original file. Keep payloads verbatim; later notes belong outside the markers.

## Original CLAUDE.md lines 7531–7902

<!-- claude-source:007531-007902:begin -->
**Face & voice underway (2026-07-30), phases 0–1 done.** Decisions made with
the owner: the face is a **web HUD run in Chromium app mode** (chromeless
window via the Playwright Chromium; profile in `~/.cache/jarvis-face` and
**port fixed at 8402** — both preserve the origin-scoped mic grant, do not
change either); activation is **push-to-talk** v1, wake word later. All audio
I/O happens in the browser (getUserMedia + `<audio>`), so Python needs no
PortAudio. Phase 0 verified the mic→record→playback round trip in the
app-mode window (clear audio, peak 59%).

Phase 1 shipped Jarvis's voice: `voice.tts(text) -> mp3` (`jarvis/voice.py`,
swappable contract) → `llm.speech()` (HTTP stays in llm.py) → OpenRouter
`/api/v1/audio/speech`, served to the window via `POST /say`
(`jarvis/face/server.py`). **TTS model: `hexgrad/kokoro-82m`, voice
`bm_george`** — $0.62/M chars, ~25× cheaper than every other hosted voice and
already a composed British male. Gotchas: the OpenRouter TTS docs' example
model id does not exist — the **collections pages are the source of truth**
for audio model ids; every TTS provider requires an explicit `voice`, and
voice names are provider-specific (change model and voice together). The
`speed` param works on Kokoro up to 1.3 but **silently truncates the audio
above that** (1.35 dropped a quarter of the speech) — `voice.tts()` clamps
to 1.3; default pace is `JARVIS_TTS_SPEED` (1.2).
`microsoft/mai-voice-2-flash` ($15/M, shipped 2026-07-23) is the premium
upgrade candidate but does not publish voice ids on its model page.

Phase 2 shipped the conversation loop: hold-to-talk in `talk.html` →
`POST /converse` (raw webm body) → `voice.stt()` → one persistent
`Agent.run_turn()` (voice-mode system prompt: short spoken replies; a real
conversation with memory for the life of the server) → `voice.tts()` → JSON
back with transcript, reply, audio_b64, per-stage timings, and cost.
**STT: `nvidia/parakeet-tdt-0.6b-v3`** — probed on real webm/opus
(`tests/voice_probe.py`): parakeet 100% @0.52s, grok-stt 100% @0.80s,
voxtral 100% @0.99s; `microsoft/mai-transcribe-1.5` rejects webm. Owner is
fine with voice audio going cloud; local whisper stays a `stt()` drop-in.
**`dispatch()` runs dangerous tools *unguarded* when `approve is None`**, so
the face server must always construct its Agent with a real approver — never
None, never `lambda: True`. (Phase 5 replaced the hard denier with the HUD
gate; the invariant is the same.) A full voice turn costs ~$0.0002.
Launch: `jarvis face` (the CLI subcommand runs inside the venv, so it works
from any shell; bare `python -m jarvis.face` fails on the system Python —
no httpx). End-to-end self-test pattern: TTS a
question → Chromium re-records it as webm (`RECORD_JS`) → POST /converse.

Phase 3 shipped the HUD (2026-07-31): `jarvis.html`, styled to the owner's
reference (classic "Stark Industries" skin — cyan-on-black, canvas arc-
reactor orb with counter-rotating tick rings and triangle emblem, angular
panels). The orb is the PTT button and is audio-reactive (mic level while
listening, output level while speaking) with a state palette: idle/listening
cyan, thinking fast-spin, tool amber, error red. Panels: COMMS LOG
(transcript), SYSTEMS (clock, model readouts from `GET /config`, session
cost), OPERATIONS (live tool ticker fed by `GET /events` SSE — the agent's
`on_event` broadcasts `tool_start` mid-turn). Test hooks: `window.__hud`
(setState/setLevel/addMsg/addOp) for headless screenshot checks.

**The window is now the owner's real Windows Chrome/Edge in app mode**
(`find_browser()` — WSL launches the Windows exe, Windows reaches the WSL
server via automatic localhost forwarding; `JARVIS_FACE_BROWSER` overrides).
No Chrome-for-Testing banner, native frame, and audio runs on the Windows
stack (the WSLg pulse path only matters for the CfT fallback now). The mic
grant lives in the user's real browser profile. Quirk: an already-running
Windows browser delegates the app window and the launcher's child exits
immediately — `main()` detects that and keeps serving (Ctrl-C to stop).

Phase 4 shipped (2026-07-31): **streamed TTS** — `/converse` now returns
NDJSON written progressively (meta line → one audio line per sentence via
`_sentences()` → done line); the HUD schedules chunks back-to-back on the
audio clock. First audio lands ~1s after the agent finishes instead of after
the whole reply synthesizes (measured: 2.8s to first speech on a 1-step
turn). **Barge-in** — pressing PTT while he speaks stops all scheduled
sources and aborts the fetch (server sees a broken pipe mid-stream — normal,
swallowed). Client aborts cannot cancel a running `run_turn`, so barge-in is
only honored in the speaking state, never mid-think. **Wake word** — Chrome's
built-in SpeechRecognition (webkitSpeechRecognition, zero dependencies,
audio goes to Google's speech service — owner OK with that) runs
continuously when armed via the WAKE WORD row in the SYSTEMS panel
(persisted in localStorage); hearing a wake phrase chimes and starts a
recording that self-stops on ~1.4s of silence. Saying one while he speaks
barges in. `talk.html` was retired — it spoke the old single-JSON
`/converse` shape.

**Wake phrases.** The default answers to **"jarvis"** and nothing else; every
other phrase belongs to an avatar and arrives over `/config` as regex source
(**"big yahu" is the `bibi` avatar's**, moved off the default 2026-08-06 —
see *Avatars* below). The built-in lives in `WAKE_PATTERNS` in `jarvis.html`
(duplicated from `avatars.DEFAULT`, and the fallback if `/config` never
answers); `matchesWake()` is what `recog.onresult` calls. Two rules for any
new phrase, wherever it is written. It is matched against a *live interim*
transcript of a phrase the recognizer has never heard, so it has to tolerate
the spellings Chrome guesses ("big ya hoo", "big yoohoo", "big yahu", "big
yawho" all resolve to the same intent) — a literal string match would miss
most real utterances. And **every pattern is `\b`-anchored at both ends**,
because a wake hit cancels the turn in flight and starts recording: an
unanchored pattern fires on a substring of ordinary conversation and takes
the owner's words mid-sentence. `tests/face/hud_wake_check.py` is the free
suite (the built-in firing, 11 utterances that must stay silent — "yahoo
finance", "big yacht", "jarvisson" — the bibi phrasings being silence on the
default and firing once that avatar is applied, and the armed hint naming
whatever is live, since an undiscoverable wake word is one nobody says). Run
it after touching the patterns.

**A wake phrase belongs to exactly one avatar.** Leaving "big yahoo" on the
default *and* giving it to `bibi` would mean the owner cannot tell which one
they just summoned — the window and the prompt would disagree about who
answered. Both `avatars_check` and `hud_wake_check` assert the default no
longer fires on it.

`bibi` wakes on **"big yahu"**, **"netanyahu"** (whole — "benjamin
netanyahu" — or on its own) and **"bibi"**. Two things decided while adding
the name, both instances of the anchoring rule rather than new ones:
**bare "benjamin" is deliberately not a phrase**, because it is a common
name and a wake hit takes the owner's words mid-sentence, and the name's
regex tolerates what the recognizer actually returns ("netan yahoo",
"nathan yahoo", "netanyaho", "netanya who") while staying clear of
"nathan is on the call" and "netanya beach". The near-misses are in
`hud_wake_check`'s QUIET list, which is the half of that suite that matters.

**Avatars shipped (2026-08-06).** Who he presents as is now data, not
hard-coded: an avatar is `avatars/<slug>/avatar.svg` + `avatar.json`
(`{name, wake, voice, rings, accent, banner, label, description}`), and switching
one changes four things at once — the **name** (an identity rename applied to
`config.SYSTEM_PROMPT` in `Agent._refresh_system`, so it reaches every
surface at once and survives compaction), the **wake phrases**, the **face**
drawn where the triangle emblem was, and the **voice** he answers in. `avatars/` is gitignored, so
`jarvis avatar new <slug> --template fox|owl|bust|reactor|bibi` scaffolds one
from art checked into `avatar_templates.py` (`bibi` is the one template that
is a specific face rather than an archetype — it is checked in *because*
`avatars/` is not, so a reclone still has somewhere for "big yahu" to live;
a template carries art only, so the phrase itself goes in the scaffolded
`avatar.json`). Controls: `jarvis avatar` to
list, `jarvis avatar <slug>` to switch persistently, `jarvis face -a <slug>`
to run one window as an avatar without touching the saved choice, the AVATAR
row in the HUD's SYSTEMS panel, and the `set_avatar` / `avatar_list` tools
("Jarvis, become the fox"). All of them land on `avatars.set_active`, which
broadcasts SSE `avatar` so every open window relabels live.

**A window boots as the default (2026-08-12, owner's call).** `jarvis face`
with no `-a` and no `JARVIS_AVATAR` is now exactly `jarvis face -a jarvis`:
`cmd_face` calls `avatars.pin_for_window()`, which pins the built-in for the
process instead of reading the saved pointer. The reason is that the pointer
is writable *by Jarvis himself* — `set_avatar` persists, so one "become the
fox" renamed the desk window for every launch afterwards, and the owner would
have to know which command undid it. An avatar now lasts as long as the window
that chose it. Three things deliberately unchanged: it is a **pin**, so
`set_active` clears it and switching in the HUD works and persists as before
(it just no longer decides how the *next* window boots); an explicit
`JARVIS_AVATAR` in the environment is still honoured, because it is a
deliberate pin and not a leftover; and every other surface — `jarvis chat`,
the daemon, Discord — still reads the saved slug, so this is a statement about
the desk window, not about who he is. An unknown `-a` slug refuses to start
rather than falling back to the default: booting as somebody the owner did not
ask for is worse than not booting. Covered in `tests/avatars_check.py`.

**An avatar can change the orb's outer ring (2026-08-06).** `"rings"` in
`avatar.json` picks a set from `RING_SETS` in `jarvis.html`; `"triangles"`
(bibi's) replaces the outermost 144-tick ring with **two interlocking
equilateral triangles turning as one figure** — the ring drawer gained a
`poly`/`copies` shape alongside `ticks` and `arcs`, so a new figure is a data
entry, not new drawing code. Only the outer ring is swappable; the four inner
rings are shared (`INNER_RINGS`) so a variant cannot quietly restyle the
whole orb. **An unknown name falls back to the default set** — the orb *is*
the push-to-talk button, so an avatar that could blank it would take the
surface's main control with it, and `hud_avatar_check` pins that.

**An avatar has a voice too (2026-08-06).** `avatar.json` takes `voice` (a
name from `voice.catalog()` — a Kokoro bundle voice, of which
`voice.available_voices()` lists the 54 already local, or since 2026-08-20 a
`pocket:` name — see *Pocket TTS* below) and an optional `speed`. Resolution lives in **`voice.tts()`**,
not the call sites — the same rule as `speakable()`, because three surfaces
synthesize speech (face, `/say`, Discord voice notes) and a fourth will. It
reads the active avatar per call, so a switch moves the voice on the next
sentence with no restart. Current: Jarvis `bm_george`, bibi `am_michael`,
hoot `bf_emma`, vex `am_puck`; `jarvis avatar` lists what each will *actually*
speak in, which is not always what its file asks for. Four notes:

- **The degradation rule is "leave him audible."** A voice that is not
  installed falls back to `config.TTS_VOICE` with a **once-per-process**
  warning (the face synthesizes a chunk at a time, so per-sentence would
  scroll), an unparseable `speed` falls back without losing the voice, and a
  vanished slug still speaks. Same shape as the wake-regex degradations.
- **The clamp is the last word, not the avatar.** `speed` still passes
  through the 0.5–1.3 clamp, because above ~1.3 the cloud provider silently
  truncates its own audio — an avatar must not be able to cut itself off.
- **Validation is what makes the cloud fallback safe.** The name is checked
  against the local bundle for the same model both paths run, so a
  local→cloud fallback cannot send a voice name the provider will reject.
  With no bundle installed, `available_voices()` is empty and every avatar
  speaks in the configured default — never an error.
- **The language heuristic was quietly wrong** and is fixed in the same
  change: `_local_tts` derived language from the voice prefix as `"en-gb" if
  voice.startswith("b") else "en-us"`, which was right for the two English
  families and made the other thirty voices (`jf_`, `zm_`, `ff_`, …) read as
  American English. `voice._LANGS` maps all nine prefixes now.

Four things worth keeping from building it:

- **The default has to be byte-identical to no-avatar.** `avatars.DEFAULT` is
  a built-in with the two existing wake regexes, and `jarvis.html` keeps its
  own hard-coded copy as the fallback for a `/config` that never answers —
  losing the wake word to a config hiccup is worse than a stale name. A test
  asserts the two copies have not drifted, because there is now a Python and
  a JS spelling of the same rule.
- **Every degradation path has to leave him summonable.** A missing slug, an
  avatar with no stated phrase, a regex that will not compile, an SVG that
  will not parse: each falls back rather than raising, because the failure
  mode of getting this wrong is a HUD that cannot be spoken to.
- **The rename is case-sensitive**, because the prompt also names shell
  commands (`jarvis chat -c`) and renaming those sends the owner to a command
  that does not exist. Only capital-J `Jarvis` moves.
- **An explicit switch beats the `-a` pin.** Without that, the picker would
  save the new avatar and redraw the window while the server went on serving
  the pinned one — the window and the server disagreeing about who he is,
  which is the same class of bug as the invisible step budget.

Also fixed on the way: the right-hand HUD column is a **flex stack** now
(`#rail`), not two absolutely-positioned panels. The AVATAR row pushed
SYSTEMS past OPERATIONS' hard-coded `top: 388px` and OPERATIONS silently
covered the SESSION row — a click-through bug that only `hud_session_check`
caught. And `tests/longhorizon_check.py`'s concurrency check was flaky
(~40%) for an unrelated reason: `_run` monkeypatches the module-global
`llm.chat`, so two threads each installing their own closure raced and the
loser's agent ran the winner's script. One shared script dispatching on the
calling agent's own user message fixes it.

**Phase 5 shipped the approval gate (2026-07-31)** — the face is no longer
read-only. `jarvis/face/approvals.py` turns a dangerous tool call into a card
in the HUD: `dispatch()` calls the approver synchronously, so `request()`
**blocks the agent thread** while the pending decision goes out over SSE, the
window POSTs `/approve`, and the blocked thread wakes with the answer. This
only works because the face is a `ThreadingHTTPServer` — the deciding request
must run on a different thread than the one it unblocks. Every failure mode
denies: no window connected (asked and answered before anything is
broadcast), 120s timeout, window closed mid-question, shutdown. Request ids
are one-shot 72-bit tokens, so an answer cannot be replayed or applied to a
later card than the one the owner read. `/approve` is same-origin only,
compared against the request's own `Host` (not a hard-coded 8402, so a server
on another port still approves). In the HUD, deny is the cheap action
(Escape, or the button) and authorize takes a deliberate click — nothing is
keyboard-defaulted, and PTT/wake are inert while a card is up. Card contents
are built with `textContent`; the args are model-written strings and the HUD
is trusted UI, so never `innerHTML` there. When a card appears, the HUD also plays a short two-tone Web Audio chime so an owner who is not watching the window notices the prompt; the chime is best-effort because browsers may block audio before user interaction.

**The gate is only real if Jarvis cannot reach it.** He has browser tools and
the allowlist is `localhost` — so he could have opened his own HUD and
clicked his own AUTHORIZE button. `config.is_face_origin()` now makes
`BrowserPolicy.allows()` and `fetch_page()` refuse the face's origin ahead of
every other check, *including* `allowed_hosts=['*']`. It matches all of
loopback, `*.localhost` included, because Chrome resolves `foo.localhost` to
127.0.0.1. Remaining honest gap: any local process running as the owner can
read the SSE stream and POST an approval — but that process could just run
the command itself, so the boundary is unchanged.

**Turn state display rebuilt (2026-07-31).** The window used to show three
things — PROCESSING, RUNNING · TOOL, RESPONDING — and got both ends wrong: it
sat on the last tool's label through the model call *and* through TTS, so the
slowest, most opaque seconds of a turn read as "still running gmail_search".
Two channels now carry it:

- **Phases on the `/converse` stream**, emitted as each stage actually
  starts: `transcribing -> heard -> thinking -> meta -> composing -> audio*`.
  Headers go out before STT so the window can show SENDING immediately; STT
  failures are now an in-stream `error` line rather than a 500. `heard`
  arrives on its own line, so your words hit the log mid-turn instead of
  after the reply.
- **Tool lifecycle on SSE**: `tool_done` was the missing half. The window
  tracks a running count and falls back to THINKING when it hits zero, and
  the OPERATIONS feed marks each op running/done with its duration.

Why phases go on the turn stream and tools stay on SSE: phases must stay
ordered with `meta`/`audio`, and SSE is a *separate connection* with no
ordering guarantee against it. So the window only lets a tool event set state
while it is in the thinking phase — a late tool event is still logged but
cannot rewind a later phase. There is a free test for exactly that race.

Also: an elapsed clock ticks in the status through every waiting state and
stops when he speaks (a turn that is alive vs one that is wedged is now
visible), `agent.py` emits **`interim_text`** for what the model says on its
way to a tool call — distinct from the final `text` event, and shown as a
note in OPERATIONS — and `init()` may no longer clobber the status: async
getUserMedia can resolve after a turn has started, so boot only writes the
status if nothing else has.

**A turn is interruptible mid-thought (2026-07-31).** Showing the transcript
at STT is only half of it — the owner's reason for wanting it was catching a
misheard line, which needs a way to take the turn back. Barge-in used to work
only while Jarvis was *speaking* (`S.busy` blocked `startRec` for the whole
agent run), which is precisely the wrong window. Now push-to-talk and the wake
word both cancel a turn in flight: the window aborts the stream, POSTs
`/cancel` (same-origin, like `/approve`), and starts recording immediately.

`Agent` takes a `should_stop` callable and **checks it between steps and
nowhere else.** That is invariant 3, not fussiness: bailing out inside the
tool loop would leave an assistant message whose `tool_call` ids never get
their results, and the next request 400s. Between steps the transcript is
always whole, so the cancelled conversation is immediately reusable — the
correction is just the next user message. The face clears its cancel Event
under `_agent_lock` right before `run_turn`, so a cancel aimed at the previous
turn cannot kill the replacement. A cancelled turn returns no reply and
synthesizes no speech.

**Since 2026-08-10 that is no longer worst-case latency.** Completions stream
(`config.STREAM`, on by default), and `should_stop` is checked per SSE line, so
a cancel lands *during* generation instead of after it. This is safe precisely
where it happens: nothing has been dispatched at that point, so no `tool_call`
is outstanding and the assistant turn is simply never appended — the transcript
stays whole, which is all invariant 3 asks. `llm.Cancelled` is the signal.

Two properties keep streaming honest. A failure **before** the first token is
retried, and a failure **after** one is not — once bytes have reached a surface,
replaying the call would duplicate what the owner already saw. And if streaming
never starts at all (a provider that cannot), `chat()` falls back to the
non-streaming path once: degrade to a slower answer, never to no answer.

`on_delta` also carries the text out as it arrives, surfaced as an `on_event`
**`delta`**. Nothing renders it yet — the HUD still shows the reply when the
turn completes — so progressive display in `jarvis.html` is the obvious next
step, and it needs no server work.

**Step budget, and admitting when it runs out (2026-08-02).** `Agent`'s
default `max_steps` is **30** (was 12). Only the conversation surfaces take
the default — the HUD/voice agent and `jarvis chat`/`ask` both construct
`Agent` without the argument; designer (24), workflows (20) and every bench
set their own. 12 was too tight for real work: a self-improve turn spends most
of it on checkpoint + edit + test runs before any misstep, and the observed
failure was a completed, committed UI change reported to the owner as a bare
`[stopped after 12 steps without finishing]`.

The bug underneath it is the one worth remembering: **exhausting the budget
used to be invisible to the model.** Every other exit path appends its
assistant message inside the loop, but the `stopped_early` tail set only
`turn.text` — so the transcript ended on a pile of tool results with no sign
of the cut, and the *next* turn's model had nothing to explain itself with.
Asked why he had stopped, Jarvis confidently answered "there isn't a
twelve-step limit" while the window showed exactly that message: not a lie, a
confabulation from a transcript that never mentioned the truncation. The
notice is now appended to `self.messages` before returning (safe — the loop
only exits at a step boundary, so invariant 3 holds). Generalizes: **any state
the surface shows the owner but never writes into the transcript is state the
model will invent a story about.** Covered by `tests/face/cancel_check.py`,
which owns both no-reply exit paths now.

**Hitting the wall is survivable now (2026-08-12), and the number is the least
of it.** The owner hit `[stopped after 30 steps without finishing]` again, on
`jarvis chat` — the face had already been raised to 60 in 2026-08-06 for the
same reason, which is the tell that raising one number was never the fix.
Three changes, in increasing order of what they buy:

- **One budget, `config.MAX_STEPS` (env `JARVIS_MAX_STEPS`), default 60.**
  Every conversation surface — chat/ask, the HUD, Discord, goal slices — takes
  the `Agent` default, and `FACE_MAX_STEPS` now defaults to it rather than
  carrying its own 60. The cap is a runaway guard, not a work limit: a step is
  one cheap call, and the real ceilings are the goal runner's dollars and an
  owner who can cancel.
- **The model can see the budget before it hits it** (`Agent._budget_note`).
  Only the last fifth (never fewer than the last three steps) is announced,
  and it rides the working-context block — rewritten every step, so it never
  accumulates in the transcript and costs nothing on the other 80% of a run.
  This is the confabulation lesson applied one step earlier: a run that knows
  it has three steps left stops opening new threads and writes down where it
  got to; a run that does not know simply stops mid-stride.
- **The wall produces a handoff, not a bin** (`Agent._handoff`). Exhaustion
  spends one more call with **`tools=None`** — the point, because a plain
  "keep going" nudge invites the model to spend it asking for a 61st step —
  for DONE / OPEN / NEXT, and that text follows the notice into `turn.text`
  and the transcript. The next message resumes from it. Every failure path
  degrades to the bare notice: a handoff is a bonus and must never be why a
  turn raises, so a provider error, a cancel at the wall, or an empty reply
  all fall back. The `text` event now fires on this path too — before it,
  `jarvis chat` printed **nothing at all** for a turn that ran out, because
  the CLI renders on that event only.

Covered in `tests/face/cancel_check.py` (handoff content and tool-free call,
the degraded path, the announcement window, and the shared default).

<!-- claude-source:007531-007902:end -->

## Original CLAUDE.md lines 8066–8385

<!-- claude-source:008066-008385:begin -->
**Markdown: rendered in the HUD, stripped for speech (2026-08-03).** The
model writes markdown and both surfaces were taking it literally — the COMMS
log printed `**Done.**` as source, and TTS read the asterisks aloud.

- **The HUD renders it** (`renderMarkdown` in `jarvis.html`): headings,
  lists (one level of nesting), fenced code, blockquotes, tables, rules,
  inline code/emphasis/strike/links. Hand-rolled into **DOM nodes, never
  `innerHTML`** — same rule as the authorization card, and it matters more
  here: a reply that could inject markup into the window that gates approvals
  could draw its own AUTHORIZE button. Link hrefs are scheme-checked
  (http/https/mailto only, `target=_blank` so the HUD itself never
  navigates); anything else renders as plain text. Only *his* messages are
  rendered — the owner's own line goes up verbatim, because typing `*foo*`
  means `*foo*`.
- **TTS strips it** (`voice.speakable()`), and the strip lives inside
  `voice.tts()` so no speech path — face, Discord voice notes, `/say` — can
  forget it. Code fences are dropped whole rather than read aloud; a reply
  that was *only* a fence yields `""`, which the face treats as text-only.
  The face also strips before `_sentences()`, because the first-chunk clamp
  measures length and counting asterisks cuts speech in the wrong place —
  and `_sentences()` now splits on newlines too, so a de-bulleted list gets
  a pause per item instead of synthesizing as one long chunk.

Not done: `whiteboard.html`'s reply panel still shows raw text. Sharing the
renderer means lifting it out of `jarvis.html` into a static JS file both
pages load.

**Voice round 2 (2026-08-18) — the mic stops being a button.** Three owner
complaints, one architecture: *"I have to wake jarvis each time by saying his
name"*, *"sometimes the recording gets cut"*, and *"somebody is talking in the
background and it just doesn't send"*. All three were the same design fault —
the microphone opened when a turn started and closed when it ended, so every
question about *when speech begins and ends* had to be answered before the
audio existed.

**The mic is now open from boot and never closes.** Audio flows continuously
into a 40-second ring buffer at 16kHz (parakeet's own rate, so resampling in
the browser costs nothing and shrinks the upload); utterances are carved out
of the ring *afterwards* by a detector, and the payload is a WAV the HUD
builds itself rather than a webm the MediaRecorder owns. Everything is
measured in **samples, not wall-clock milliseconds** — the audio thread is the
only clock that cannot drift against the buffer actually being sent.

What each complaint turned out to be:

- **The first word was missing on every wake-word turn.** Chrome's recognizer
  reports a phrase several hundred ms after it was said, and `onWake` called
  `startRec()` at *that* moment — so recording began somewhere inside "what's
  the weather". Nothing starts recording now; a wake hit only decides where to
  cut, and back-dates the start past the lag. Measured in
  `hud_capture_check.py`: 1.4s spoken uploads as 2.9s, and the difference is
  exactly what used to be lost. Push-to-talk gets the same pre-roll, so
  talking the instant you press no longer clips the first syllable.
- **A fixed threshold cannot be right twice.** The old detector compared a raw
  peak against a hard-coded `0.045` with a 1400ms hangover. On a quiet mic
  ordinary speech never cleared it, so a breath mid-sentence ended the turn;
  in a noisy room the level never fell *below* it, so the end of the utterance
  was never detected and the recording ran to its 15s cap. The floor is
  tracked from the room now — falling fast (0.2s) so a room going quiet is
  followed within a breath, rising slowly (8s) so background chatter has to be
  sustained to count, and **rising slower still while an utterance is open
  (25s), so the owner's own voice cannot walk the threshold up underneath
  itself and cut the sentence off.** Verified: 20s of background talk lifts the
  threshold 0.0200 → 0.0875 and leaves nothing wedged open, while speech at
  0.02 — which the old 0.045 could never see — opens an utterance in a quiet
  room.

  The time constants are **in seconds, not per frame**. They were per-frame
  first, which silently means a different amount of time on a different buffer
  size — and made the first test that fed one large frame measure something
  the microphone never does.
- **His name is not needed every time.** A follow-up window opens the moment he
  stops speaking (`doneSpeaking()`), so the next utterance is simply the next
  turn. This is free rather than new machinery: the mic was never closed, so
  there is nothing to reopen. The window is stated in the hint ("JUST SPEAK ·
  STILL LISTENING") — an affordance nobody knows about is one nobody uses —
  and expires on its own timer, because nothing else redraws while the HUD is
  idle and a lapsed promise is worse than none.

Four rules the design turns on, each of which was a bug first:

- **An utterance is only sent if it was addressed to him**: the orb was held, a
  wake phrase was heard around it, or it landed in the follow-up window.
  Everything else is captured and discarded. Continuous capture without that
  gate is a hot mic.
- **A wake phrase claims exactly one utterance.** Leaving the hit live for the
  rest of its grace window let the *next* thing said — an aside to someone
  else, a sentence finished after he had already started — arrive as a second
  turn nobody addressed to him. Found by a test that failed for what looked
  like a harness reason and was not.
- **He does not answer himself.** The detector is suppressed while he is
  speaking, thinking, or holding an authorization card. It still *tracks the
  room* through all of it, so the threshold is current the moment the
  follow-up window opens. Barge-in stays deliberate — orb, space, or his name
  — because an open mic that interrupts on any sound interrupts on the wrong
  ones.
- **The minimum length is measured on the speech, not the segment.** The
  segment is padded at both ends, so measuring *it* let a 90ms cough clear a
  350ms floor on padding alone. `VAD.voiced` counts only frames above the
  threshold. Relatedly, the hangover is trimmed off the end before upload —
  it is the silence that *proved* the utterance ended, and shipping it is a
  second of nothing for the transcriber.

**And the reply starts sooner (same day).** Two independent fixes, measured
against local Kokoro:

- **`_sentences()` was defeating its own purpose.** The rule that keeps a stray
  fragment from becoming its own chunk ("A tiny one.") also fired when the
  *previous* chunk was short — which is exactly the opener. So a perfect
  20-character first chunk was swallowed into a 98-character one, and the
  first-chunk clamp then hacked *that* apart at the last space, between "is"
  and "complete." **1633ms to first audio instead of 607ms, plus an audible
  break mid-phrase, on the one metric the function exists to optimize.** The
  two merge reasons are not symmetric and are now held apart in `_merges()`:
  a tiny *incoming* part is always glued back; a tiny *preceding* chunk
  absorbs what follows unless it is the opener. The clamp's no-clause fallback
  also backs off past a **binding word** (`_BINDING` — articles, auxiliaries,
  prepositions, determiners, degree adverbs), because a cut is permanent and
  audible while backing off a word costs nothing.
- **TTS now runs while the model is still writing** (`_Speculator`). The text
  has been streaming all along — it is what draws the HUD's live draft — so
  chunks are built as they appear and are waiting when `run_turn` returns.
  **Turn-end to first audio: 879ms → 188ms, 3 of 4 chunks pre-rendered.**

  The owner's call, and the safe one: **audio still goes out only after the
  turn completes**, in order, exactly as before. A sentence spoken early is a
  sentence a later tool call can contradict. So this is a *cache keyed on the
  chunk text* — a guess that does not match the finished reply is thrown away
  and resynthesized, and being wrong costs CPU rather than correctness.

  But wrong is not free, and that shapes the whole thing: **local Kokoro
  serializes on one model instance** (`_kokoro_lock` — which is also why the
  "two in flight" pool only ever measured 1.24x, not 2x), so a wasted
  synthesis holds the lock the chunk actually being waited on needs. Hence
  `_stable_chunks()`, which declares a chunk settled only once the one after
  it exists *and* is too long to be merged back into it; hence
  `SPECULATION_LIMIT`; and hence the reset on **`interim_text`**, which is the
  loop's existing signal that a step ended in a tool call and everything
  streamed so far was thinking out loud rather than the answer.

  `feed()` guards its **whole body**, not just the synthesis: it runs inside
  `llm.chat`'s streaming loop by way of `on_delta`, so anything raised there
  comes out of the middle of the model call. The first version guarded only
  "the part I thought could fail", which is not the same promise — the test
  caught it.

- **Chunk boundaries are deliberate now** (`voice._repad`). Each Kokoro chunk
  carried ~30ms of lead-in and ~85-105ms of tail, and back-to-back scheduling
  turns that padding *into* the pause between sentences — the same pause
  whether the boundary fell between two sentences or in the middle of one.
  Since the clamp splits a long opener mid-phrase routinely, that read as a
  stumble. The pad is trimmed and put back sized by what the chunk ends on:
  130ms after a full stop, 70ms after a clause, 15ms after a bare word cut. A
  margin is kept at both ends so a plosive's attack and a final decay survive,
  and a chunk that never rises above the floor is returned untouched — a quiet
  chunk is recoverable, an empty one is a dropped sentence.

Still cloud, still one shot: **STT is unchanged.** `voice.stt()` sends the
whole utterance to parakeet when it closes. Streaming/incremental
transcription would need a websocket endpoint OpenRouter's transcription API
does not offer, and is the next thing to look at if the remaining latency
matters.

**The owner can mute themselves (2026-08-20).** An always-open mic needs an
off switch the owner controls: the **MIC row** in SYSTEMS (beside WAKE WORD;
VOICE OUT remains his output mute). It is HUD-local by design — capture,
segmentation and wake all live in `jarvis.html`, so there is no server state
and **no tool**: an agent that could deafen its own input channel is a lever
Jarvis must not hold, and with the mic off, "jarvis, unmute" could never be
heard anyway — unmute is a click. Strict semantics, each one a deliberate
choice: nothing is ever claimed or uploaded while muted; the **wake
recognizer is stopped outright** (it streams audio to Google's speech
service — a muted mic must stop that too, so the recognizer is re-armed from
`wakeMode` on unmute and `setWake` while muted only records intent); the orb
**still interrupts** him but records nothing (mute must not take away the way
to shut him up); the ring keeps filling *locally* so the room threshold is
current at unmute; the orb's level meter reads zero (a level meter on a muted
mic promises listening); the hint says MIC MUTED · TYPE · OR UNMUTE IN
SYSTEMS, since every other hint is an invitation to speak; the state persists
in localStorage and **fails toward muted** across a reboot (a window that
restarts into a hot mic is the wrong surprise — the same reasoning as the
wake toggle, in the opposite direction); and `sendUtterance` clamps every
upload at the last unmute mark, because the wake path back-dates an
utterance's start by WAKE_GRACE + PREROLL and across an unmute that would
ship audio recorded while the owner believed the mic was off. Typed input is
untouched — muting the mic and typing is the point. Covered by the mic-mute
section of `tests/face/hud_capture_check.py`.

**Pocket TTS shipped (2026-08-20) — a second local voice backend, with
cloned and hybrid voices** (`jarvis/pocket.py`; pinned synthetically by
`tests/voice_pocket_check.py`, live-smoked same day — findings at the end
of this section). Kyutai's pocket-tts is a 100M-param CPU TTS
with zero-shot cloning: ≤30s of someone speaking becomes a voice, and the
computed prompt state (the model's KV-cache) exports to a `.safetensors`
that reloads fast. Install: `uv pip install -e .[pocketvoice]` — it pulls
CPU PyTorch, the first heavyweight ML stack in the venv; use a CPU torch
index so a CUDA wheel doesn't ride along. Weights download from HF on first
use. The rules, in the order they matter:

- **The namespace is the router.** Pocket voices are `pocket:<name>`
  (`pocket:alba` builtin, `pocket:dad` clone/hybrid); bare names stay
  Kokoro's, so avatars and config needed no migration. The prefix is applied
  and stripped only in `voice.py` — `pocket.py` deals in bare names — and
  `tts()` routes per resolved voice, so the face pre-warm, `/say`, Discord
  and the speculator all needed zero changes. Pocket serializes on its own
  `_lock` beside `_kokoro_lock` (never nested; the fallback runs outside it).
- **The cloud never sees a `pocket:` name.** Any pocket failure — raising
  model, missing store entry, library absent — warns once and degrades to
  `config.TTS_VOICE` on the existing Kokoro→cloud chain; if the configured
  default is itself `pocket:`, it degrades to `bm_george` so the fallback
  cannot loop. This preserves both standing invariants: every path leaves
  him audible, and the cloud only receives names it can synthesize. Speed is
  ignored on pocket (no such control) with a once-per-process note.
- **`voice.catalog()` is the one list** — Kokoro bundle + pocket builtins +
  the custom store, each `{name, backend, kind}`; `set_voice`/`voice_for`
  validate against it. `available_voices()` stays Kokoro-only on purpose
  (tests and the language map key off the bundle). `pocket.available()`
  answers without importing torch (`sys.modules` first — which is also what
  lets the test fake work — then `find_spec`), so `import jarvis.voice`
  never drags the ML stack in.
- **Creating a voice is the owner's act, never the agent's.** `jarvis voice
  clone <name> <audio…>` and `jarvis voice mix <name> a=0.6 b=0.4` are CLI
  subcommands (the `jarvis auth` pattern) with a consent confirmation —
  the pocket-tts license prohibits cloning without the speaker's lawful
  consent, so there is no clone tool and no HUD upload. `jarvis voice
  list|say|rename|rm` complete the set; `say` writes a WAV and plays it via
  paplay/aplay when it can. The store is `config.VOICES_DIR`
  (`JARVIS_VOICES`, default `~/.local/share/jarvis/voices/<name>/` —
  voice.json + state.safetensors + the reference audio), **outside the repo
  like sessions and never committed**: clones are audio of real people.
  `.gitignore` carries a `voices/` backstop anyway. Directories are built
  aside and renamed into place so a crashed clone never leaves a half-voice.
- **Hybrids are experimental, and fail loudly.** pocket-tts has no native
  mixing, so `mix` blends the *exported* states per tensor (weights
  normalized; shapes differing in exactly one axis — the prompt-length one —
  slice to the shortest keeping the head, so positions stay aligned;
  anything stranger is a ValueError). `_blend` is written with plain
  operators so the free suite runs it on numpy while production runs torch.
  Judged by ear via `jarvis voice say`; the documented fallback for a bad
  blend is a multi-file clone, which concatenates reference audio instead.
- **The HUD picker now spans backends.** `/voices` returns grouped entries
  plus `override`; the picker's first row is AVATAR DEFAULT (`POST /voice`
  with `""` clears `_voice_override` — the picker used to outrank every
  avatar forever with no way back), and rows carry KOKORO / POCKET / CLONE /
  HYBRID badges. Still `textContent`-built, still same-origin.

Live smoke findings (2026-08-20, pocket-tts 2.1.0, torch 2.13.0+cpu via
`UV_TORCH_BACKEND=cpu` — the resolver did land a clean CPU wheel on 3.14):

- **The probe answered every unknown the right way.** sample_rate is 24000
  (never assumed); output floats sit in [-1, 1] so `_repad`'s 0.01 floor
  applies unchanged; generation ran 780–1871ms per chunk on this CPU —
  under the face's 2500ms SLOW threshold for ordinary sentences; and
  `generate_audio` does **not** mutate the state it is passed (exported
  before/after, byte-identical), so `_state` needs no defensive clone.
- **The exported state is small and fixed-shape**: 12 tensors — per-layer
  `self_attn/cache` float32 `(2, 1, 126, 16, 64)` plus an int64 `offset` —
  and the cache capacity is fixed at 126, so two voices' states are
  *shape-identical* and `_blend`'s one-axis slicing is a safety net rather
  than the common path. The int64 offsets do differ per prompt, which is
  why `_blend` takes the elementwise **minimum** for integer tensors: a
  blended position only counts as valid if it is valid in every component,
  and "first wins" would have made the mix order-dependent.
- **Mixing works on real tensors**: `jarvis voice mix smoke-blend alba=0.5
  marius=0.5` wrote a hybrid whose state loads through the normal store
  path and synthesizes real speech (4.0s for the standard test line)
  through the full `voice.tts()` routing and the HUD-facing surfaces.
  Whether it *sounds* like a plausible blend is the owner's ear-call —
  A/B WAVs were handed over; `smoke-blend` is left in the store to audition
  from the picker (`jarvis voice rm smoke-blend` when done).
- **The `BUILTINS` list is exactly the library's catalog** — the failure
  message below printed all 26, matching name for name.
- **A quiet reference clones into a quiet voice** (found on the owner's
  first real clone, 2026-08-21): the model carries the prompt's loudness
  into everything it synthesizes — a peak-0.09 laptop-mic take produced
  output ~7x quieter than the builtins. `pocket.clone` therefore
  normalizes decodable reference audio to a healthy speech level
  (`_REF_RMS` with a `_REF_PEAK` clipping ceiling) before prompting;
  measured fix: output rms 0.0125 → 0.0746, in line with builtins. Files
  the stdlib cannot decode (mp3 and friends) still pass through at their
  recorded level.
- **Voice cloning is gated upstream — resolved 2026-08-20.** The
  cloning-capable weights require accepting the terms at
  huggingface.co/kyutai/pocket-tts and a local `hf auth login` (the venv's
  own `.venv/bin/hf`, no apt install); without them, builtins and *mixes of
  builtins* work fine, a clone attempt fails with the library's own clear
  message, and the build-aside store logic leaves no half-voice behind
  (verified — that failure path ran for real). The owner accepted the terms
  and logged in same day; cloning was then validated live: clone → speak,
  2.6s of valid WAV — **with `HF_HUB_OFFLINE=1` for the whole run**, which
  is the proof the privacy pins below rest on.

**The privacy pins (2026-08-20, owner's ask: reference audio and cloned
states must never share a connection with the internet).** They never could
— there is no upload path anywhere in pocket-tts or Jarvis, and the only
network use is *downloading* weights — but two pins remove even the
metadata residue and harden the at-rest story:

- **Jarvis processes default to HF offline** (`config.py`, right after
  `_load_dotenv`): `HF_HUB_OFFLINE=1` unless `JARVIS_HF_OFFLINE=0`, set
  before anything can import `huggingface_hub` (config is every
  entrypoint's first import; both TTS backends load lazily). This kills the
  hub client's version-check requests and download telemetry, so synthesis
  and cloning provably touch no network at all. It holds because
  **everything is prefetched**: both model variants (the gated cloning one
  and the fallback) and all 26 builtin voice states are in
  ~/.cache/huggingface. The cost, documented in the comment: anything
  needing a *fresh* Hub fetch — a pocket-tts upgrade, a new machine, a
  swecompare dataset refresh — needs `JARVIS_HF_OFFLINE=0` for that one
  run. An explicit `HF_HUB_OFFLINE` in the environment always wins.
- **The voice store is owner-only** (`pocket._store_root`): 0700 on the
  root and every voice directory (mkdtemp is 0700 by construction and
  `os.replace` keeps the mode) — a cloned voice is reference audio of a
  real person plus the state to speak as them, which is
  credential-adjacent, so it gets the token-file treatment.

  Both pins are in `tests/voice_pocket_check.py` (`hardening_checks`:
  fresh-subprocess env matrix for the offline default and its two
  overrides, and the 0700 modes).

<!-- claude-source:008066-008385:end -->

## Original CLAUDE.md lines 9446–9491

<!-- claude-source:009446-009491:begin -->
**The wake chime fired once per recognizer update, not once per phrase**
(fixed 2026-08-22). Two owner reports, one cause: saying just "jarvis" chimed
**twice**, and carrying on talking after he replied chimed **again and again**.

`recog.onresult` is level-triggered by nature — Chrome delivers the phrase
being spoken as a *growing interim transcript*, one event per revision, then
delivers the same words once more as the final result. So "does this
transcript contain his name?" answers yes on every one of them: twice for a
bare "jarvis" (interim, then final), and once more for every word spoken after
it while that segment stayed open, because the transcript being revised still
began with his name. The chime was only the audible half — each of those
firings also ran `cancelTurn()` and re-armed the claim.

The 2026-08-18 rewrite had already fixed the *claim* side of exactly this
("a wake phrase claims exactly one utterance"), by spending `wakeHitAt` inside
`onWake`. What it missed is that `chime()` and `cancelTurn()` sit **above**
that guard, so the half of `onWake` that is audible was never covered by it.
The lesson generalises past this handler: **guarding what an event *does* is
not guarding how often it fires**, and a de-duplication rule belongs at the
edge that produces the events, not partway down the function that consumes
them.

Two gates now, for two different failure modes, and neither can wedge the wake
word off — which is the property that matters, because a silent wake word is a
worse bug than a loud one:

- **One decision per result segment.** A segment index that has already been
  answered cannot be answered again, however many times it is re-reported —
  and the decision is recorded even when the second gate suppresses it, so a
  suppressed hit cannot fire late when that segment is revised.
- **A 1500ms floor** (`WAKE_REFIRE_MS`), the backstop for what an index cannot
  see: Chrome may revise an earlier segment and re-deliver an old "jarvis"
  under a lower index.
- **An index below the last one fired means a new session, not an old phrase.**
  `onend` restarts recognition every few seconds and indices restart with it;
  without that rule the wake word would go silent until as many segments had
  accumulated again. Reset in `onstart` as well, and the recovery is
  self-healing either way.

Matching still concatenates from `resultIndex` on purpose — a two-word phrase
can straddle a segment boundary, and matching the pieces separately would miss
it. `tests/face/hud_wake_check.py` gained a `recognizer_checks` section that
drives the real handler through a scripted `SpeechRecognition` (the event
sequences Chrome actually produces, chimes counted rather than played); it is
verified to fail against the old handler with "the final result chimed again".

<!-- claude-source:009446-009491:end -->


## 2026-10-13: V1 microphone diagnostics

V1 adds a window-local ring of the latest 50 utterance dispositions under
Settings → Voice. It contains only a local numeric sequence, monotonic time,
bounded duration, a closed claim-source/outcome enum, energy-detector identity
and STT latency. No audio, transcript, title, device ID or exception text is
stored in the ring, logged, sent to the daemon or persisted. Reload clears it.
`handed_to_chat` means the transcript reached the chat send function; it does
not claim the daemon accepted a send. REVIEW placement and STT failure are
separate outcomes, and each pending row settles at most once.

Capture's existing claim, muted-upload refusal, unmute pre-roll clamp,
suppression, 30-second cap and PTT behavior remain. An observational energy
detector groups speech under existing suppressors into one drop row per
segment; it cannot claim audio or upload. Observer failures cannot prevent
the real capture callback. The live level is zero and explicitly labelled
MIC MUTED in OFF; the local room floor continues tracking as before.

The microphone explicitly requests echo cancellation, noise suppression and
automatic gain. Unsupported constraints fall back to the existing `audio:
true` request. Permission denial never triggers a second request. Actual
`getSettings()` readback is normalized to booleans or "not reported"; requested
constraints are not presented as proof of effective processing.

Free checks: full HUD vitest, production build,
`hud_v2_voice_diagnostics_check.py` (isolated fake microphone/STT, real fetch,
the daemon CSP and mock ephemeral listeners), and capture mutations removing
the unmute clamp, allowing muted PTT, or omitting drop observations. Each
mutant fails a relevant test; source is restored byte for byte afterward.
The browser checks include no upload under Settings or OFF, failed STT with
no exception in diagnostics, failed chat send with no false "sent" outcome,
reload clearing, normalized settings and fixed permission-failure wording.

V0 acoustic/ML measurements remain pending: Windows speaker AEC/ERLE, TV
false holds, WASM Silero/RNNoise/KWS CPU, local Parakeet RTF/WER and speaker
EER. Synthetic diagnostics checks establish none of those measurements.
This package does not change wake recognition, add speech playback, alter
approvals, retain retry clips, add continuation, or claim a live deployment.

## 2026-10-13: V2 STT reliability

Plan §3.5 / decision V-11. A claimed utterance now goes through
`hud/src/lib/stt.ts` (`SttAssembler`) instead of one bare `fetch`.

- **Deadlines.** The client aborts each attempt at `max(15 s, 2 x clip)`, and
  the deadline fires even when the transport ignores its abort signal.
  **Revised from plan §3.5 ("a 20 s per-attempt timeout and one retry per
  model", unchanged for callers that state no deadline):** 20 s x 2 attempts x
  2 models is about 82 s, so a hung primary could never fail over inside a
  15 s client deadline. The client therefore sends its attempt's whole
  deadline as `POST /stt?budget_ms=` (digits only, clamped to 1 s..600 s) and
  the daemon fits the model chain inside it (`voice.stt(budget=)` ->
  `_fit_budget`): 1 s is held back for the reply, the rest is shared evenly
  over the models still to try (so a model that fails fast frees time for the
  next), and a model gets two attempts only if each still has 6 s, else one;
  per-attempt timeouts never exceed `config.STT_DICTATION_TIMEOUT_S` (20 s).
  A 15 s deadline is one 7 s attempt on the primary, then the fallback with
  what is left. With no `budget_ms` (Discord voice notes, the face server) the
  fixed limits apply (20 s x `STT_DICTATION_ATTEMPTS` for dictation, llm's
  defaults for the rest). `llm.transcribe` no longer sleeps after its last
  attempt. Known limits: httpx timeouts are per phase (connect, read), not
  wall-clock, so the fit is a bound on a hung read, not a hard guarantee; and a
  client abort does not cancel the daemon's request, so each client retry
  starts a fresh, budgeted chain that may overlap the old one.
- **Retries.** One automatic retry after 2 s, only for failures a repeat could
  fix (timeout, network, 408/429, 5xx). Any other 4xx is not repeated. The
  Retry button is explicit and is one attempt per press. A retry (automatic
  or explicit) is refused while the mic is OFF, the window has crashed, or an
  authorization card is up: the same fail-closed rule as the first upload.
- **Kept clips.** The WAV of a clip is held, in memory only, until its text is
  placed or the owner presses Discard: at most 20 clips, 10 minutes of audio
  and 32 MiB. The oldest failed clip is evicted first (the diagnostics row then
  reads "audio not kept"); a new clip that cannot fit because 20 are still in
  flight is refused with a visible `STT BACKLOG` error, never by evicting audio
  in flight. The incoming clip counts against all three caps at submit; a
  clip so large that it is evicted the moment it fails is reported as such
  (`STT FAILED`, "too large to keep"), never as "audio is kept". Reload clears
  everything. Held clips survive mic OFF until Discard or reload, and Retry is
  blocked while OFF (see above); Discard uploads nothing, so it works while a
  card is up. Nothing is persisted or uploaded except the `/stt` request itself. The V1 ring still holds no audio; the clips live
  in the assembler, and Play decodes them through Web Audio (the CSP's
  `media-src 'self'` excludes `blob:` for `<audio>`).
- **Visible failure.** `STT FAILED · RETRY` under the orb, the window error
  line, and a Retry/Discard block in the Status pane. Settings -> Voice rows
  with a kept clip offer Play and Retry. If the right pane is folded, the
  Settings row is the way back to Retry.
- **Order.** Each clip gets a `seq` at capture and is sent as
  `/stt?utterance_id=<id>&seq=<seq>`; the daemon echoes both (non-negative
  integers, anything else is a 400 before any provider call). Text is placed
  in `seq` order: a reply that lands early waits for earlier clips still in
  flight. A clip that has *failed* does not hold the line; later text is
  placed and the failed clip's text goes in when its retry lands (marked
  `late`). Texts that become ready together are one batch: REVIEW joins them
  with a space in one box update; AUTO sends them one by one.
- **Where it lands.** The capture records the mode and the selected chat
  (`draftKey`) in the clip's `meta`. AUTO sends a transcript only when nothing
  has moved: it was AUTO when said and still is, the same chat is still
  selected, that chat is idle (a busy chat would be steered), no authorization
  card is waiting, and the text is neither `late` (an earlier clip failed) nor
  the product of the owner's Retry (`placementFor`). Otherwise the words go
  into the selected chat's box, unsent. If any item in a batch must wait in the
  box, the whole batch does, to keep the spoken order; AUTO items in one batch
  are sent as one message, joined in order. While a chat runs a turn, dictation
  progress never overwrites its orb or status line (errors still show).

Free checks: `hud/src/lib/stt.test.ts` (fake clock: deadline expiry keeps the
clip and shows the failure, bounded retry, Retry re-sends the same Blob,
out-of-order replies, retention bounds), `tests/voice_stt_check.py`,
`tests/v2/hud_backend_check.py` (`/stt` ids and limits),
`tests/face/hud_v2_stt_check.py` (real HUD, scripted mock `/stt`). Mutations
(ordering that does not wait for an in-flight clip, a no-op Retry, an
unbounded automatic retry) each fail the vitest suite.

Not done and not claimed: physical-microphone acceptance, a live provider
outage test, chunked/overlapping STT (V4), continuation and steering (V3, its own section
below).

## V6: streaming speech core (2026-10-13)

Design §12.2 listed "TTS speculation" as carried over; it was not (v1's
`_sentences`/`_stable_chunks`/`_Speculator` stayed in `jarvis/face/server.py`
and nothing in the v2 HUD called `/say`). V6 ports the splitter to
`hud/src/lib/speech/split.ts` and holds it to v1's answers with recorded
vectors (`tests/face/speech_vectors_check.py --write` regenerates
`vectors.json`; the check fails when the Python and the file drift). Unlike
v1, speech starts while the model writes (decision V-3).

Pieces (`hud/src/lib/speech/`): `prose.ts` (streaming markdown filter: fences,
unfenced diffs, indented code, tables, `[...]` diagnostics, inline code, paths,
URLs, hashes; independent of where deltas fall), `stream.ts` (filter + splitter
per message, `text` remainder rule, 1200-character stopgap until V8's summary),
`queue.ts` (one item at a time, prefetch 2, stale/pane drops, `PlayOrder` seam
for V7), `player.ts` (Web Audio; `<audio>` is out, CSP `media-src 'self'`
excludes `blob:`), `speaker.ts` (Web Lock `jarvis-speech`, focus steals),
`controller.ts`, `policy.ts` (the owner's answers to Q9/Q11/Q15 as constants).

`/say` refuses a voice not in `catalog()` (400) and `local_only` never reaches
`llm.speech` (503 on a local failure). The mic's OFF no longer posts `/mute`.
While a chunk plays the capture detector stays suppressed, as v1 did (self-voice
gating is V9). Not done: multi-pane shared queue and per-thread voices (V7),
summaries (V8), barge-in and spoken commands (V9), the follow-up window opening
when speech ends (V3/V5), and any physical speaker or Windows-audio acceptance.

The v1 mute flag (`voicectl`, `POST /mute`) and the `set_voice_mute` tool no longer
affect read-aloud: the HUD's read-aloud switch (Settings → Voice) and the speech
strip are its only controls, and the mic's OFF touches neither.

## V7: one queue over every open chat pane (2026-10-13)

Decisions V-8 and V-9 (plan sections 3.9, 3.10): every thread shown in any chat pane
is read aloud, each in its own voice, through the V6 queue.

- **Who is read** (`policy.speakingThreads(ws, chats, pending)`): a thread shown by a pane
  that is in the workspace (`order` or `kept`, so a pane the layout folded away still reads)
  and whose view is `chat`. A task view, a pane showing a file, a thread in no pane, and a
  chat the owner muted are silent; a thread leaving the set drops its items and stops its
  sound at once. A thread shown in two panes is one entry.
- **Floor rules** (`floor.ts`, the `PlayOrder` V6 left): a message that has started holds
  the floor and its later chunks go before other messages; messages go in the order of their
  first chunk; a holder with nothing ready for 4 s yields (`FLOOR_TIMEOUT_MS`) and the rest
  of its message re-queues behind what is queued by then; an ended message with nothing left
  releases the floor at once (the controller calls `queue.endMessage` when a message closes,
  is skipped, dropped or its thread leaves). While a holder keeps the floor the queue plays
  nothing else, even a chunk already made.
- **Stale drop** is V6's (a `user_message` drops that thread's items and stops its sound).
- **Backlog** (rule 6): over 60 s of estimated speech (16 characters a second) unplayed in
  one thread, its oldest *interim* messages are dropped (mark `collapsed`) and one spoken
  "Skipping N updates." (`kind: "note"`) takes their place in line. Never collapsed: the
  last message of a finished turn (`queue.turnEnded` marks it final at `turn_finished`), the
  message being played or holding the floor, a thread's newest message *by arrival* (it may
  still be arriving; a message that yielded is behind it in line but older), and markers. A marker already being made is not edited, so a later collapse
  gets a marker of its own.
- **Voices**: `jarvis/v2/speech_voices.py` and `GET`/`POST /speech/voices` (owner-only, HUD
  listener), stored in `~/.config/jarvis/speech.json` (`config.SPEECH_VOICES_PATH`), pruned
  with deleted threads, published as `thread_voice`. The first thread takes the voice the
  owner hears now (`voice.selected_voice()`, so the HUD picker's choice, else the avatar's);
  later ones the next unused of English Kokoro `a*` then `b*`, then English Pocket builtins;
  all taken reuses the least used. Sticky; a stored voice no longer installed is reassigned.
  Nothing is stored (or published) when no voice can be found, and the window backs off for
  5 s after such an answer. A voice is assigned only when a chat is first *read*, never
  because it is merely open, and again asked for at synthesis time, so a voice chosen from
  the chip applies to chunks not yet made, and a chunk waits for a voice still being assigned.
  The store is read defensively (regular file only, size-bounded, any parse error is empty),
  `thread_voice` is published while the store lock is held, and deleting a thread removes
  its entry (`projects.delete_thread`; every write also prunes). Mute is stored beside the voice so every window agrees.
- **Voice chip** (`components/VoiceChip.tsx`, in the pane's header strip): the voice name,
  a pulse while that chat is read, and opened inline: voice select, Skip (this chat's
  current message), Mute. Disabled under an authorization card. The speech strip now names
  the chat being read, not the selected one.

**Known limit until V9:** the detector is held off while anything is read
(`micSuppressedBySpeech`, V6), and now that includes any open pane, not only the selected
chat. A hands-free utterance during, or within the 300 ms hangover of, another pane's speech is
dropped as "speaking"; the talk key still works (it pauses the speech). A multichat section
that streamed pane 7's words while speaking into pane 6 started failing for exactly this
(the suites ran with the page holding the speech lock); it now mutes pane 7's chat, and
`hud_v2_speech_check.py` pins the suppression and the working talk key. Self-voice gating
(V9) replaces the blanket hold.

Not done here: summaries (V8), barge-in and spoken commands (V9), Browser sound ducking
(V15), any physical speaker or Windows-audio acceptance.

## 2026-10-13: V0 measurements (synthetic and offline, no product change)

Package V0 of `docs/plans/2026-10-13-voice-rework.md` section 6. Probes live in
`tests/voice_v0/`; nothing under `jarvis/` or `hud/` changed, no dependency was added to
`pyproject.toml` or `hud/package.json`, and no paid API was called. No probe imports `jarvis`
(its `config` import reads the credential `.env`); Kokoro's model paths come from the same
`JARVIS_KOKORO_*` variables and defaults. **Everything below is
synthetic**: Kokoro and Pocket text-to-speech, generated noise, and two TTS voices standing in
for a television. No recording of a real person was made or used. Numbers say what the models
and the browser cost on this machine and how they separate synthetic voices; they do **not**
establish acoustic quality for the owner, which stays a manual gate (end of this section).

### Setup, and how to re-run

- Machine: Intel Core Ultra 9 275HX (24 threads), 15 GB, WSL2 Ubuntu on Windows 11, shared with
  other agents. Every probe ran inside `systemd-run --user --scope -p MemoryMax=3G
  -p MemorySwapMax=0 -p CPUQuota=800%` (4G for the Parakeet steps, whose peak is 1.9 GB). The
  quota means "24 threads" below is 24 threads on an 8-core cap, a deliberately bad
  configuration. `loadavg` at the time is recorded per run; other agents' suites made it 2 to
  38, so timings carry that noise (repeat runs are quoted where they differ).
- Python 3.14.4 probe venv at `~/.cache/jarvis-voice-v0/venv` (`sherpa-onnx` **1.13.8 has a
  CPython 3.14 wheel**, which the plan listed as unverified; `onnxruntime` 1.31.0). Browser:
  Playwright 1.61.0 driving the headless Chromium 149.0.7827.55 already in
  `~/.cache/ms-playwright` (the Playwright version must match the installed Chromium revision;
  1.63 wants a build that is not installed). Node v22.22.2 for the KWS WASM cross-check.
- Fixtures: `tests/voice_v0/make_fixtures.py` renders 24 dictation-style sentences (no digits)
  in 14 English Kokoro voices at speeds 0.9 to 1.2 (336 clips, 22 min) and 8 sentences in the 26
  Pocket builtins (208 clips), with the repo's own model files, plus
  `make_special_fixtures.py` (keywords, hard negatives, complete versus cut-off sentences).
  `--pocket` now calls the `pocket_tts` library directly instead of `jarvis.pocket.synthesize`
  (to avoid importing `jarvis.config`), which skips jarvis's `_repad`/`_encode_wav` boundary
  padding, so Pocket fixtures regenerated today differ slightly from the clips behind the
  recorded numbers; that new Pocket path has **not** been run end to end (only the library calls
  were checked on one short phrase).
  They are generated, deterministic, and kept under `~/.cache/jarvis-voice-v0/fixtures`, not
  committed.
- Pinned downloads, each into its own directory under `~/.cache/jarvis-voice-models/` and
  checked against a SHA256 in `tests/voice_v0/models.json` before use (npm tarballs also against
  the registry's own digest; tar extraction is capped at 3 GB and 5,000 members). A later run
  re-hashes single-file models; for archives only the extracted tree remains, so it trusts the
  `.verified` marker that records the archive's digest. **Downloaded code does execute**: the
  pinned `onnxruntime-web` and RNNoise WASM run inside headless Chromium, served only from the
  loopback server, with every other host made unresolvable (`--host-resolver-rules`); the pinned
  `sherpa-onnx` npm package runs under Node with an empty environment (`env -i`) and, where an
  unprivileged network namespace exists, no network (`unshare -rn`). Node's `--permission` model
  was tried for this and cannot be used: Emscripten's runtime calls `process.binding`, which it denies. Sources: GitHub releases of `k2-fsa/sherpa-onnx` and
  `snakers4/silero-vad`, the `pipecat-ai/smart-turn-v3` Hugging Face repo at commit f766f81,
  and npm. `JARVIS_VOICE_V0=1 python tests/voice_v0/fetch_models.py` fetches them (about 950 MB); `--record`
  prints digests for new entries.

| item | version / file | sha256 (first 16) | size |
|---|---|---|---|
| Parakeet-TDT-0.6B-v3, int8 | sherpa-onnx `asr-models` release | 5793d0fd397c5778 | 487 MB archive, 641 MB unpacked |
| Silero VAD | snakers4/silero-vad v5.1.2 and v6.2.3 | 2623a2953f6ff3d2 / 1a153a22f4509e29 | 2.3 MB |
| onnxruntime-web | 1.30.0 (npm) | d2228df7e4616bc3 | WASM 14.2 MB |
| RNNoise WASM | @jitsi/rnnoise-wasm 0.2.1 (npm), the 0.2 model | c80b015b69701699 | 1.9 MB inlined |
| sherpa-onnx KWS | zipformer gigaspeech 3.3M, 2024-01-01 | f170013b4716e41b | fp32 encoder 12.2 MB, int8 4.8 MB |
| sherpa-onnx WASM (Node build) | sherpa-onnx 1.13.8 (npm) | 15dc53065cc6bbc7 | WASM 15.1 MB |
| speaker: wespeaker ResNet34, wespeaker CAM++, 3D-Speaker CAM++, NeMo TitaNet-small | sherpa-onnx `speaker-recongition-models` release | 5ef208a9da14.., c46fad10b5f8.., 357a834f702b.., ad4a1802485d.. | 26 / 29 / 30 / 40 MB |
| Smart Turn | `smart-turn-v3.2-cpu.onnx` | 2bb026316b14a660 | 8.3 MB |

Probe venv: `tests/voice_v0/requirements.txt` pins the versions used. Re-run: `JARVIS_VOICE_V0=1 tests/voice_v0/run_all.sh [step ...]` (steps `fetch fixtures special
browser vadquality stt ns contention speaker kws turn`), then `python
tests/voice_v0/render_results.py` for the tables below. Every script in `tests/voice_v0/`
(downloads, fixture generators, probes, the node check, the echo-page server) prints "skipped"
and exits 0 unless `JARVIS_VOICE_V0=1`, with no bypass for the STT worker subprocesses (they
inherit the variable), so none runs in the normal test sweep.
`tests/voice_v0_helpers_check.py` is the only sweep-visible file: it checks the WER, EER and SNR
helpers offline.

### 1. CPU in Chromium: Silero VAD, RNNoise, KWS (plan 3.2, 3.4, 3.6; Q16)

Method: each module runs single-threaded in its own Web Worker (the HUD is not cross-origin
isolated, so no WASM threads) served from an ephemeral local port with the pinned `onnxruntime-web`.
The main thread posts one frame per real-time interval, as an AudioWorklet port would: 512
samples at 16 kHz (32 ms) for Silero, 480 samples at 48 kHz (10 ms) for RNNoise, which only runs at
48 kHz. CPU is the browser's own process CPU time (CDP `SystemInfo.getProcessInfo`) over six 5 s
segments, reported as the median of segments 2 to 6; segment 1 carries one-off JIT tier-up and is
shown separately. A do-nothing worker at the same cadence is subtracted for the "above baseline"
column. Silero's output from this build matches native onnxruntime: `check_silero_parity.py` re-scores
the 36-clip "clean" condition of section 2 natively and gets the identical precision 0.999,
recall 0.890 and 4 false-accept frames.

| module | steady % of one core (median) | above null-worker baseline | first 5 s | worker ms/frame p50 / p95 (budget) | renderer RSS delta MB |
|---|---|---|---|---|---|
| Silero VAD v5.1.2 (ort-web 1.30.0) | 3.4 | 2.4 | 14.1 | 0.6 / 0.8 (32) | 89 |
| Silero VAD v6.2.3 (ort-web 1.30.0) | 3.4 | 2.4 | 15.5 | 0.7 / 1.1 (32) | 40 |
| RNNoise 0.2 (Jitsi WASM), 48 kHz | 5.6 | 3.4 | 7.6 | 0.3 / 0.6 (10) | 21 |
| RNNoise + Silero together | 9.8 | 6.6 | 22.6 | - | - |

Null worker (message passing only): 1.0% at 32 ms frames, 2.2% at 10 ms frames.

| sherpa-onnx KWS WASM under Node/V8, 1 thread | CPU s per audio s (= fraction of a core) | wall s per audio s | RSS MB |
|---|---|---|---|
| int8 | 3.3% | 2.7% | 272 |
| fp32 | 3.7% | 3.0% | 281 |

Repeat run earlier the same day (load 2 to 5): Silero 3.0% and 2.8%, RNNoise 5.0%, together 8.6%.
Typical worker frame times are far inside budget (p95 0.8 ms of 32 ms for Silero, 0.6 ms of 10 ms
for RNNoise) and the main thread never posted a frame late (`late_frames` 0). A single frame can
exceed the budget: the slowest Silero frame was 28 to 36 ms in the paced runs and 38 to 39 ms in
bursts, probably the first call of each worker (JIT warm-up; the slowest frame's index was not
logged, so this is an inference). RNNoise's slowest burst frame was 11.2 ms against its 10 ms budget,
likewise once. A burst runs at 94x (v5.1.2) and 114x
(v6.2.3) real time for Silero and 33x for RNNoise.

**KWS in the browser was not measurable directly.** sherpa-onnx 1.13.8 ships no browser KWS WASM
(its release assets carry TTS, VAD and speech-enhancement builds, and the k2-fsa Hugging Face
Spaces I tried for KWS and ASR were not available), and the npm `sherpa-onnx` package is the Node build: it calls `require("path")`
unconditionally and uses NODERAWFS, so it does not initialise in a worker. The KWS row therefore
runs that same WASM binary under Node, which has the same V8 WASM tiers as Chrome, one thread, 60 s
of audio in 512-sample pushes; add the roughly 1% null-worker message cost. Native onnxruntime
(Python, 1 thread) costs less: **1.6% (int8) and 1.8% (fp32) of a core**, so WASM is about 2x native.
Two native threads cost 4.7 to 5.9% for no gain, since the model is tiny; use one.

**Sum for the window**, steady state: measured RNNoise + Silero together 9.8% (8.6% in the repeat
run) + KWS 3.3% under Node + about 1% worker message passing for KWS = **about 12 to 14% of one core**
with all three, against the 15% bar; about 7 to 8% without RNNoise. The first 5 s of the combined
RNNoise + Silero run cost 22.6% (JIT tier-up, roughly 0.6 CPU-seconds in all), before KWS. Renderer memory grew by about 40 to 90 MB for Silero and 20 MB for RNNoise (noisy
measurement); the KWS process peaked at 250 to 330 MB under Node.

Caveats: headless Chromium on Linux, not the owner's Windows Chrome (same V8, different OS
scheduling and CPU); synthetic bench audio; the machine was loaded; the resampling that a
16 kHz pipeline would need around RNNoise (16 to 48 to 16 kHz) is not included. Place RNNoise
before the 48-to-16 kHz downsample if it is used.

**Q16, cost half: near the bar, not comfortably under it.** All three modules come to about 12 to
14% of a core on this machine, against 15%; without RNNoise it is about 7 to 8%. The KWS figure is a
Node/V8 proxy (not measured in a browser), the machine was loaded, and Windows Chrome on the owner's
CPU is unmeasured, so treat the margin as unconfirmed. It does not decide the other half,
"acceptable misses": see section 4.

### 2. Does RNNoise help the VAD or the STT? (plan 3.4, V14)

Method: 36 clips joined with 0.8 s gaps. Ground truth is the clean signal's frame energy
(32 ms frames above -50 dBFS, dilated by 3 frames). Degradations: 1/f noise and white noise at 10
dB (and 0 dB) SNR, and two other Kokoro voices talking at 10 and 5 dB below the target as a "TV".
Silero is run on the degraded signal, on RNNoise's output, and RNNoise's own speech
probability is shown for reference. All at threshold 0.5, 5,598 frames.

| condition | Silero precision / recall / false-accept frames | RNNoise then Silero | RNNoise's own VAD output |
|---|---|---|---|
| clean | 0.999 / 0.890 / 4 | 0.998 / 0.899 / 8 | 1.000 / 0.805 / 0 |
| pink_10dB | 0.995 / 0.893 / 19 | 1.000 / 0.876 / 0 | 1.000 / 0.751 / 0 |
| pink_0dB | 0.996 / 0.811 / 14 | 1.000 / 0.849 / 1 | 1.000 / 0.727 / 0 |
| white_10dB | 0.997 / 0.875 / 13 | 1.000 / 0.875 / 0 | 1.000 / 0.756 / 0 |
| tv_10dB | 0.961 / 0.959 / 181 | 0.959 / 0.958 / 188 | 0.969 / 0.923 / 137 |
| tv_5dB | 0.960 / 0.961 / 184 | 0.955 / 0.963 / 211 | 0.966 / 0.940 / 152 |

pink_noise_only: Silero accepts 0.0% of frames, after RNNoise 0.0%.

white_noise_only: Silero accepts 0.0% of frames, after RNNoise 0.0%.

tv_only_two_voices: Silero accepts 97.8% of frames, after RNNoise 97.9%.

Reading: on generated noise Silero already has precision above 0.99, so the gain is small;
RNNoise removes nearly all its false-accept frames (19 to 0, 14 to 1, 13 to 0) and moves recall
by -2 to +4 points (+4 at 0 dB); on clean speech it changes little (4 to 8 false-accept frames). **On the TV stand-in nothing helps**: it is speech,
so Silero accepts 97.8% of the frames of a signal that is only two TTS voices, before and after
RNNoise. The VAD cannot tell a TV from the owner; that is speaker verification's job (section 3).

STT on the same degraded clips (Parakeet, 4 threads, the as-captured signal versus the same
signal through RNNoise, 36 clips and 474 words, so one word is 0.2 points):

| degradation | WER as captured | WER after RNNoise |
|---|---|---|
| clean | 1.1% | 0.4% (474 words) |
| pink_10dB | 1.3% | 1.3% (474 words) |
| pink_0dB | 5.3% | 10.8% (474 words) |
| white_10dB | 1.1% | 2.1% (474 words) |
| tv_10dB | 15.4% | 6.8% (474 words) |
| tv_5dB | 20.7% | 20.5% (474 words) |

RNNoise is **not a consistent win for STT**: it roughly doubles WER at 0 dB pink noise (5.3 to
10.8%) and at 10 dB white noise (1.1 to 2.1%), halves it with the 10 dB TV (15.4 to 6.8%), and
is unchanged at 5 dB TV. This supports plan 3.4's rule that STT takes the echo-cancelled stream,
not the denoised one. **For V14:** RNNoise buys fewer false VAD accepts on steady noise and
nothing against speech interference; its 3.4% extra core and a regression on part of the STT matrix
argue for treating it as optional, behind a setting, and not before V5 shows the Silero worker
false-accepts on the owner's real room noise.

### 3. Local STT (plan 3.5; Q6)

Method: Parakeet-TDT-0.6B-v3 int8 through `sherpa_onnx.OfflineRecognizer.from_transducer(...,
model_type="nemo_transducer")`, greedy decoding, one subprocess per thread count so peak RSS is
clean. 96 clips (4 to 5 s) spread over voices and sentences for the short RTF; 30 s and 60 s inputs
are the same clips joined with 350 ms gaps. RTF is decode wall time over audio seconds. sherpa's own
default is 1 thread. Warm-up decode excluded. No cloud STT was called.

|---|---|---|---|---|---|---|---|
| 1 | 1.9 | 1084 / 1832 | 0.074 / 0.092 | 0.072 | 0.090 | 0.07 | 3.7 / 3.2 |
| 2 | 1.9 | 1100 / 1847 | 0.043 / 0.049 | 0.045 | 0.055 | 0.17 | 3.6 / 4.0 |
| 4 | 2.4 | 1102 / 1853 | 0.042 / 0.053 | 0.033 | 0.040 | 0.33 | 6.9 / 7.8 |
| 8 | 1.9 | 1125 / 1870 | 0.257 / 0.436 | 0.084 | 0.048 | 2.06 | 9.7 / 8.5 |
| 24 | 4.0 | 1093 / 1843 | 0.751 / 1.390 | 0.280 | 0.268 | 5.99 | 38.2 / 12.0 |

(The 24-thread row ran on an 8-core quota: oversubscription costs 10x. The 8-thread row ran
while other suites loaded the machine. Both illustrate why the daemon should **pin** the thread
count, not leave it at ONNX Runtime's all-cores default.)

- **RTF 0.07 on one thread, 0.04 on two to four.** A 10 s dictation decodes in 0.4 to 0.7 s; a
  60 s one in 2.4 to 5.4 s. One thread uses 0.07 CPU-seconds per audio second; four threads burn
  0.33 for a 40% faster result.
- **Memory is the real cost:** about **1.1 GB resident** after load (loading takes about 2 s)
  and **1.8 to 1.9 GB peak** during load. That has to fit the daemon's budget next to Kokoro and
  Pocket.

WER, clean synthetic: 1.4% (13 sub, 4 del, 0 ins over 1233 words, 96 clips, 384 s of audio).

The 17 errors are mostly clipped word ends in Kokoro's fast speech ("finis" for finished,
"audien", "Plepen" for "Please open"), plus a dropped plural, a spelling variant ("summarise") and a
number format ("ten" to "10") that a normaliser would absorb. It is clean synthetic speech, so it bounds the model from
below and says nothing about an accented or mumbling owner. Degraded copies of 40 clips:

| degradation (synthetic) | WER |
|---|---|
| clean | 1.9% (519 words) |
| pink_10dB | 2.3% (519 words) |
| pink_0dB | 6.9% (519 words) |
| white_10dB | 2.7% (519 words) |
| tv_10dB | 6.4% (519 words) |
| tv_5dB | 18.5% (519 words) |

TV at 5 dB is the only condition that breaks it badly (18.5%), which is also where speaker
verification must remove the second voice before STT. The TV stand-in is erratic between subsets
(6.4% here, 15.4% in the RNNoise comparison above), so read its rows as "somewhere between 5% and
20%", not as a figure.

**Kokoro while Parakeet decodes** (same process, Parakeet 4 threads decoding a clip of about 25 s in a loop):

| Kokoro (one 15-word sentence) | alone p50 / p95 s | during Parakeet p50 / p95 s | slowdown |
|---|---|---|---|
| daemon_default_threads | 4.03 / 5.54 | 8.10 / 13.45 | 2.01x |
| pinned_4_threads | 0.80 / 0.90 | 1.63 / 1.99 | 2.03x |
(load1 9.3 -> 7.9)

Kokoro's per-sentence latency doubles while Parakeet runs, in both configurations. Note the daemon
configuration (ONNX Runtime default threads) is itself slow on an 8-core quota (4 s a sentence
against 0.8 s pinned to 4 threads), so that row measures this probe's cgroup, not the daemon;
pin Kokoro's threads too. Decodes are short (RTF 0.07), so the overlap is a burst, not a
steady state, but a reply being synthesized while the owner dictates will stutter.

**Supports Q6 on RTF:** 0.07 against a 0.3 threshold, with 4x margin even under load, if the
thread count is pinned to 1 to 4. **The other half of the Q6 condition, "WER within 1 point of
cloud", is not measured**; it needs paid cloud calls on real recordings and is a stated
owner-authorised follow-up. The synthetic WER (1.4%) and the same-class model make the claim
plausible, not shown.

### 4. Keyword spotting accuracy (plan 3.3, 3.6; V10, V11)

Method: sherpa-onnx native KeywordSpotter, gigaspeech 3.3M, keywords JARVIS, OVER, CANCEL, PAUSE,
RESUME, SKIP, STOP encoded with the model's BPE, 100 ms chunks plus 0.8 s of trailing silence.
Recall pools the positive phrases with the few negative-group phrases that legitimately contain
a keyword; n per keyword (positive + incidental) is JARVIS 48+0, OVER 24+16, CANCEL 16+8, PAUSE
16+0, RESUME 16+0, SKIP 16+0, STOP 16+8 (`render_results.py` prints it), so the single-word rates are
over 16 to 48 trials from 8 voices. Positives are 8 voices x 15 phrases (isolated word, "Jarvis, pause.", carried in a sentence,
trailing "...send it, over."): 152 expected keyword occurrences, plus 32 in negative phrases that
legitimately contain one ("the game is over there"), 184 in all. Negatives are 12 similar-sounding
or embedding phrases and 11.5 minutes of ordinary dictation (every other clip of the Kokoro
set). Detection delay after the last word is a median 0.35 s (p95 0.64 s) in audio time, before
compute.

| model file | threads | CPU % of a core, real time | RSS MB |
|---|---|---|---|
| fp32 | 1 | 1.79 | 168 |
| fp32 | 2 | 5.89 | 169 |
| int8 | 1 | 1.56 | 167 |
| int8 | 2 | 4.66 | 166 |

| config (boost, threshold) | recall | JARVIS | OVER | CANCEL | PAUSE | RESUME | SKIP | STOP | stem-variant FA | unrelated FA | dictation FA/h |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.0 / 0.15 | 76% | 92% | 75% | 100% | 38% | 62% | 50% | 71% | 33 | 1 | 0.0 (0 in 11.5 min) |
| 1.0 / 0.25 | 73% | 90% | 68% | 100% | 38% | 69% | 50% | 67% | 32 | 1 | 0.0 (0 in 11.5 min) |
| 1.0 / 0.4 | 64% | 79% | 62% | 92% | 19% | 56% | 31% | 62% | 29 | 1 | 0.0 (0 in 11.5 min) |
| 2.0 / 0.15 | 67% | 94% | 32% | 96% | 38% | 69% | 50% | 71% | 34 | 1 | 5.2 (1 in 11.5 min) |
| 2.0 / 0.25 | 58% | 85% | 28% | 92% | 38% | 44% | 31% | 62% | 31 | 1 | 0.0 (0 in 11.5 min) |
| 2.0 / 0.4 | 40% | 52% | 25% | 71% | 25% | 25% | 19% | 42% | 21 | 1 | 0.0 (0 in 11.5 min) |
| 1.0 / 0.25 int8 | 73% | 90% | 72% | 100% | 38% | 62% | 50% | 62% | 30 | 1 | 0.0 (0 in 11.5 min) |
| JARVIS/CANCEL 1.0 #0.25; OVER/PAUSE/RESUME/SKIP/STOP 3.0 #0.1 / - | 59% | 88% | 15% | 100% | 38% | 44% | 56% | 58% | 33 | 3 | 10.5 (2 in 11.5 min) |

With 10 dB pink noise recall is 66%, with 10 dB competing speech 62% (default config).

"Stem-variant FA" counts hits on "skipped", "resumed", "cancer" and "pauses" (the model hears the
keyword inside the longer word); "unrelated FA" is anything else (1 in 12 clips).

- **Cheap and quiet:** 0 false accepts in 11.5 minutes of dictation at the default operating
  point (the 95% upper bound on that is about 16 per hour, so this is "no evidence of trouble",
  not "none"); one at the loosest setting. Nothing fired in the 60 s browser bench except a true "stop".
- **But the short command words are weak:** recall is JARVIS 90%, CANCEL 100%, OVER 68 to 75%, STOP
  67%, **RESUME 69%, SKIP 50%, PAUSE 38%** for isolated synthetic speech. Raising the boost for
  the short words (the "tuned" row) cost recall on OVER and false accepts on dictation without
  fixing PAUSE; it is not a free fix. Noise makes it worse: 66% with 10 dB pink noise and 62%
  with a 10 dB competing voice.
- **Implication for Q16 / V11:** CPU is fine, but "acceptable misses" is **not** established for
  the commands. JARVIS and CANCEL look usable; the rest need either a longer phrase ("Jarvis
  pause" as one keyword, which also handles V-6's "starts with Jarvis" rule), per-keyword tuning on
  the owner's own voice, or the transcript path (phase 1, V9/V10) as the primary route with KWS
  as an accelerator. Do not retire the Chrome recognizer on this evidence.
- Open vocabulary works as advertised: the keyword list is a text file, no training.

### 5. Speaker verification (plan 3.4, V12)

Method: enrol a centroid from 3 clips of one voice, score held-out clips of that voice and of
all other voices by cosine similarity, pool, and report equal error rate (EER). 40 synthetic speakers
(14 Kokoro, 26 Pocket), 424 genuine and 16,536 impostor trials per condition. Clips are
trimmed of Kokoro's digital-zero padding and given a -60 dBFS noise floor first (some models score
exact-zero frames very differently). Conditions: clean full clips; the first 3 s;
3 s with 15 dB pink noise and a 24 kbit/s Opus round trip (the codec the HUD uploads); and
3 s with a second voice mixed in at 10 dB. **This is not owner-vs-TV EER and not a false-accept
rate to expect.** Two clips of one synthetic voice differ only in text and speed, so
genuine trials are easier than real life; the impostors are TTS voices, which are neither a TV
nor your family. Use it to compare models and as a latency receipt.

| model | EER clean full clip | EER 3 s | EER 3 s, noise + Opus | EER 3 s + second voice at 10 dB | FAR at 1% FRR (overlap) | EER threshold (3 s) |
|---|---|---|---|---|---|---|
| wespeaker_en_voxceleb_resnet34 | 1.12% | 1.23% | 3.79% | 13.68% | 72.7% | 0.813 |
| wespeaker_en_voxceleb_CAM++ | 31.37% | 14.17% | 12.74% | 24.31% | 80.7% | 0.920 |
| 3dspeaker_campplus_en_voxceleb | 24.76% | 9.91% | 10.67% | 15.80% | 84.8% | 0.858 |
| nemo_titanet_small | 0.02% | 0.00% | 0.44% | 1.93% | 2.8% | 0.706 |

Trials per condition: 424 genuine, 16536 impostor.

| model | 3 s embedding p50 / p95 ms, 1 thread | 4 threads | RSS growth MB | 
|---|---|---|---|
| wespeaker_en_voxceleb_resnet34 | 101.9 / 152.2 | 33.8 / 41.4 | 30 |
| wespeaker_en_voxceleb_CAM++ | 38.7 / 44.6 | 21.8 / 23.8 | 6 |
| 3dspeaker_campplus_en_voxceleb | 36.2 / 43.2 | 25.0 / 27.6 | 0 |
| nemo_titanet_small | 33.6 / 39.8 | 19.1 / 22.5 | 83 |

- **NeMo TitaNet-small is the clear leader**: 0.0% EER at 3 s, 0.4% with noise and Opus, 1.9% with
  a second voice at 10 dB, and 2.8% false accepts at 1% false rejects in that overlap case. WeSpeaker
  ResNet34 is clean-good (1.2%) but 13.7% with overlap. **Both CAM++ models were poor here (10 to
  31%) and behaved oddly: worse on the whole clip than on its first 3 s**; this was not
  root-caused (it persisted after trimming and a noise floor), so read it as "failed on this input
  in this runtime", not as a verdict on CAM++.
- **Latency for 3 s of audio, 1 thread: 34 ms p50 / 40 ms p95 for TitaNet-small** (19 / 23 ms on 4
  threads); about 3 s of dictation can be verified with 1% of the audio's duration. WeSpeaker
  ResNet34 is 3x slower. Model file 40 MB; resident growth 66 to 83 MB.
- The default Jarvis voice (`bm_george`), scored as an impostor against every enrolled centroid,
  peaked at 0.69 against the clean-audio EER threshold of 0.71: rejected, but narrowly. Under noise and
  Opus the threshold falls to 0.61 and the same voice peaks at 0.68, and with overlap 0.77 against
  0.51, so a threshold fitted to degraded audio **would** accept some of Jarvis's own voice for
  the closest synthetic enrollee. Verification alone must not be the self-voice gate; keep the
  plan's reference correlation (3.4 step 2) and the enrollment-time check against every builtin voice.
- The EER thresholds differ by model and condition (0.51 to 0.71 for TitaNet), so V12 must
  derive the threshold from the owner's enrollment, as the plan says, not hard-code one.
- TitaNet-small's licence was not checked here (NVIDIA NeMo); check before V12 ships it.

### 6. Smart Turn (plan 3.2)

Method: `smart-turn-v3.2-cpu.onnx` with pipecat's protocol (last 8 s, Whisper log-mel features
via `WhisperFeatureExtractor(chunk_length=8)`, sigmoid output). Test set: 12 complete Kokoro sentences
versus 12 fragments cut mid-sentence (at "the", "before", "but", ...), 6 voices each, 144 clips,
followed by 0.2 to 1.0 s of silence, with and without a -55 dBFS noise floor.

| trailing audio | p(complete) median: complete / incomplete | complete recall @0.5 | incomplete correct @0.5 | AUC | best-threshold accuracy |
|---|---|---|---|---|---|
| 0.2s_digital_silence | 0.974 / 0.940 | 100% | 19% | 0.73 | 72% @ 0.92 |
| 0.5s_digital_silence | 0.973 / 0.927 | 100% | 18% | 0.72 | 69% @ 0.94 |
| 1.0s_digital_silence | 0.974 / 0.933 | 100% | 18% | 0.71 | 70% @ 0.88 |
| 0.5s_noise_floor_-55dBFS | 0.974 / 0.936 | 100% | 21% | 0.74 | 67% @ 0.79 |
| 1.0s_noise_floor_-55dBFS | 0.974 / 0.889 | 100% | 31% | 0.75 | 72% @ 0.73 |

| threads | features ms p50 / p95 | model ms p50 / p95 | total ms p50 / p95 | CPU ms p50 |
|---|---|---|---|---|
| 1 | 5.3 / 7.6 | 32.0 / 42.2 | 37.4 / 50.4 | 37.4 |
| 4 | 5.3 / 6.0 | 11.7 / 13.3 | 17.1 / 19.0 | 65.7 |

- **Fast enough:** about 37 ms per decision on one thread (5 ms features from the numpy
  extractor, 32 ms model), 17 ms on four; 8 MB model, process RSS about 220 MB with the
  `transformers` import (a daemon-side implementation would avoid that). It would run on the
  daemon, since the log-mel front end does not exist in the browser.
- **Not discriminating here:** it calls 100% of the complete sentences complete but also 69 to
  82% of the cut-off fragments (AUC 0.71 to 0.75). Trailing silence did not matter (the feature
  extractor pads with zeros either way). **Caveat:** a TTS fragment is not a person trailing off;
  Kokoro closes a clipped phrase with sentence-final prosody, which is what the model listens to,
  so this probably understates the model. It is still no evidence for it.
- **Recommendation:** leave Smart Turn out of V5. The plan's own adoption test (beat the plain
  2.5 s timer on the owner's dictation) cannot be run synthetically.

### Findings against the plan, in one place

| Plan item | Result | Effect |
|---|---|---|
| Q6: local STT default if RTF < 0.3 and WER within 1 point of cloud | RTF 0.07 (1 thread) / 0.04 (2 to 4); 1.1 GB resident, 1.9 GB peak; synthetic WER 1.4%; cloud WER **not measured** | RTF half met with margin; WER half open (paid, owner-authorised) |
| Q16: KWS in the window if at most 15% of a core and acceptable misses | KWS about 3.3% (Node proxy); all three about 12 to 14% (about 7 to 8% without RNNoise) | cost **near** the bar, unconfirmed on Windows Chrome; misses **not** acceptable for PAUSE, SKIP, RESUME on synthetic speech; needs the owner-voice run |
| 3.2 / V5: Silero in a worker | 2.4% above baseline (3.4% total), 0.6 ms/frame, no late frames | go; plan the one-off 0.6 CPU-s start-up |
| 3.4 / V14: RNNoise | extra 3.4% core; fewer false accepts on noise, no help with speech; STT mixed | optional, off by default |
| 3.4 / V12: speaker verification | TitaNet-small: 34 ms per 3 s, EER 0.0 to 1.9% synthetic, CAM++ poor | candidate is TitaNet-small via sherpa-onnx (Python 3.14 wheel exists); owner-vs-TV EER open |
| 3.2: Smart Turn | 37 ms, but does not separate synthetic fragments | not adopted |
| 3.4 self-voice gating step 1 (Chrome AEC) | **not measured** | manual gate |

### Findings that are about the tooling

- The sherpa-onnx WASM does not run in the browser as shipped (above); for KWS-in-window the
  work in V11 is building or finding a browser KWS WASM, or running KWS on the daemon over a
  local WebSocket, which Q16 already names as the fallback.
- ONNX Runtime's default thread count is harmful on a shared or capped machine: 8 and 24 threads
  were 4 to 18x slower than 1 to 4 for Parakeet. Pin threads for every local model.
- Kokoro and Parakeet slow each other roughly 2x when they overlap; a single process lock for
  heavy local audio models may be worth considering in V13.

### Manual gate: what V0 could not measure (owner at the PC)

None of these is faked above; each needs the owner's room, speakers or voice. The
probes are ready to run; none writes audio to disk or calls the network while measuring, and
none is in the normal test sweep (`JARVIS_VOICE_V0=1` required). The scripts need the probe venv
and the pinned models, and download any that are missing on first use, so run
`JARVIS_VOICE_V0=1 python tests/voice_v0/fetch_models.py` first (about 950 MB). The `wer` and
`speaker` runs load local models (Parakeet peaks at about 1.9 GB resident), so cap them, for
example `systemd-run --user --scope -q -p MemoryMax=4G -p MemorySwapMax=0 -- python ...`.
The echo page needs `JARVIS_VOICE_V0=1` for its server too, and fetches only `/stimulus.wav`
from it; the numbers it prints hold no browser string or device name, and it labels an ERLE as a
lower bound when the echo with cancellation on is at or below the noise floor.

| Measurement | Why not synthetic | How to run it | Fills |
|---|---|---|---|
| **Speaker echo return loss (ERLE)**, Chrome AEC against same-page Web Audio output | needs your speakers, mic, room and Windows' audio stack | `JARVIS_VOICE_V0=1 python tests/voice_v0/manual/aec_manual_server.py`, open the printed `http://127.0.0.1:PORT/aec_probe.html` in the **same Windows Chrome profile as the HUD**, click Start, stay silent for about 2 minutes. Four trials (AEC off/on x Web Audio/`<audio>`); copy the JSON | plan 3.4 self-voice step 1; whether the reference-correlation gate (step 2) is mandatory |
| **False holds per hour of playback** | needs V9's barge-in | after V9: play 30 min of replies at your usual volume with the TV on and you silent; count holds in the diagnostics view | Q7 (3 false pauses / 10 min rule) |
| **KWS misses at 1 m and 3 m, false accepts with TV** | distance, reverberation, your voice | `JARVIS_VOICE_V0=1 python tests/voice_v0/manual/owner_manual.py kws --seconds 60 --label "1 m"` (say each keyword about 10 times), again `--label "3 m"`, then silent with the TV on for 5+ min `--seconds 300 --label "TV"`; hits per hour = hits / minutes x 60 | Q16, V11 thresholds |
| **VAD false accepts with the real TV** | the synthetic "TV" is two TTS voices | `owner_manual.py vad --minutes 5` (silent, TV on) | V5 (p>=0.5 share, speech onsets per minute) |
| **Owner WER (local Parakeet)** | your voice; cloud comparison needs paid calls | `owner_manual.py wer --n 20` reads 20 prompts, transcribes locally, reports WER; audio never saved. For cloud, read the same 20 sentences into the HUD yourself and compare, or authorise a paid follow-up | Q6 "within 1 point of cloud" |
| **Owner-vs-TV speaker EER** | your voice and a real TV | `owner_manual.py speaker --tv-minutes 3` (3 enrolment readings, 20 own clips, then TV windows) | V12 threshold and Guest voice default |
| **A day's DropReason histogram** | needs a real day of use, and V1's ring keeps only the last 50 utterances, in memory, cleared on reload | Not obtainable from V1 alone. Add a counter in V3/V5, or tally by hand from Settings -> Voice -> Diagnostics at intervals | plan 6 |

`owner_manual.py` reads the mic through PulseAudio (`parec`, WSLg `RDPSource`), so it
measures the WSL path rather than Windows Chrome's capture. Use `aec_probe.html` for
anything about the browser's own processing. `--source stdin` feeds raw 16 kHz s16le PCM
instead (that is how the scripts were smoke-tested with synthetic audio).

## 2026-10-13: V3 steering, push-to-talk, continuation

Plan decisions V-1, V-10 and the owner's answers to Q3-Q5. Stacked on V2.

- **A running turn no longer silences the mic.** `busy` is gone from the
  capture's suppressors and from `SuppressionReason` (no `suppressed:busy`
  row exists any more). Still suppressed: Jarvis speaking, no chat, a card, a
  picker or chip overlay, Settings, a crashed window.
- **Dictation into a busy chat steers.** `placementFor` (V2) boxed every
  busy-chat transcript; it now sends one when the chat's running turn is its
  own thread's (`landing.steers` = `runningHere(chat)`, the test that also
  gives the box its Stop and Steer), through the same `send()` typing uses (`steer_queued`, `queued`
  and the "not sent" give-back after Stop all apply). Kept from V2, each
  still a box placement: a different chat than at capture, a waiting card, a
  late or retried text, mode REVIEW at capture or now, OFF. New and
  conservative: a busy chat whose turn is *not* its own thread's (pane moved
  to another thread mid-turn, or a first send still opening its thread) boxes
  the words, because `send()` would start a new turn there unseen.
  A busy pane keeps its own orb and status while listening: the claim
  borrows them and `endListening()` restores them (`turnOrb`, now exported)
  when the capture ends; "didn't catch that" notes are not written over a
  turn's status.
- **Push-to-talk no longer interrupts** (`press()` in App.tsx; was the
  "interrupt half" until 2026-10-13). Stop (InputBar) is the interrupt.
  Consequence, closed: a first send in a compose row, between its thread
  opening and the send's acknowledgement, had the orb as its only stop. The
  box now offers Stop there too (`InputBar` `stoppable` = `runningHere ||
  (busy && compose.sending)`) without turning Send into Steer; the stop
  waits for the acknowledgement as before (`sendStops`). The
  Ctrl+D gesture, its repeat/blur/terminal/Monaco rules and the card refusal
  are unchanged. Muted, pressing records nothing and does nothing else.
- **Continuation window** (`CONTINUE_MS` = 6 s, on the ring's sample clock,
  so no timers). A claimed message that goes out opens it; speech whose onset
  falls inside is claimed as `continuation` with no wake word. **Stopgap cap
  until speaker verification (V12), the lead's decision after review:** only a
  `wake` or `follow_up` claim starts a chain; a chain allows at most
  `CONTINUE_MAX_CLAIMS` = 2 continuation claims and ends `CONTINUE_CHAIN_MS`
  = 20 s after the message that started it *ended* (a long dictation still
  gets its window), so TV or room speech with short gaps
  cannot keep steering a running turn without a wake word. A too-short
  *continuation* (a cough, a click) just drops and does not reopen the
  follow-up window - that would let the next noise start a fresh chain; a
  too-short wake or follow-up claim still gives the window back. (Side effect: until
  V4 removes the 30 s cap, only two continuations follow a cut message.)
  Not opened by push-to-talk or an unclaimed message (releasing the key is the
  full stop; an open question). The window is armed when the message ends, but
  `Capture` hands the caller a token and App takes it back
  (`cancelContinuation`) if the message did not go out: a refused or failed
  upload, an empty transcript, a backlog refusal, a crashed window. It also
  closes on: the mic going OFF or an unmute, anything that suppresses the
  detector (card, picker, Settings, his own voice, no chat, crash), `abandon()`,
  and the owner selecting another chat or another conversation in the pane
  (`draftKey` change; a compose row becoming its own just-opened thread is not
  one). In AUTO the continuation is just the next transcript: it steers if the
  first message's turn is running, else it is the next message; in REVIEW it
  is appended to the box by the existing `pendingTranscript` append. Segment
  gap, 2.5 s message pause and the ML VAD are V5's; the pause stays
  `HANGOVER_MS`. Consequence worth knowing: a wake word no longer claims
  "exactly one utterance" in the sense that speech straight after it is the
  continuation (`capture.test.ts` says so); an aside after the window has
  lapsed is not.
- **Follow-up target (Q5).** `Capture.openFollowUp(tag)` remembers what the
  window was opened for and hands it to `onListening(source, tag)`. App's
  `openFollowUpFor(pane)` opens it only for the selected chat or a *drawn*
  chat pane (hidden panes open none). On a `follow_up` claim for another drawn
  pane, App focuses that pane and selects it (`layout.focus` + `select`) - unless
  the owner has chosen a different chat since the window opened
  (`followBase`) - and files the clip under it
  (`voiceTarget`: the selection itself follows only after the next render).
  If the pane is gone or hidden by the claim, the words
  wait in the selected chat's box, unsent (`boxOnly`). Today only the selected
  chat's `turn_finished` (or its spoken reply's end) calls it; a drawn pane's
  *spoken* reply will (V7). `window.__hud.followUpFor(pane)` is the test hook.

Free checks: `capture.test.ts` (continuation at 5 s and not 7 s, window edge,
chaining, PTT, mute, suppression, selection, abandon; claim tag), `stt.test.ts`
(`placementFor` rows), `tests/face/hud_v2_steer_check.py` (46 checks:
busy+PTT steers, Stop, REVIEW, busy-but-other-thread, V2 conditions, card
before and in flight with "Jarvis approve", continuation in AUTO and REVIEW,
Ctrl+D on a busy chat, follow-up target). V2/older checks changed on purpose:
`hud_v2_stt_check.py` (a busy chat steers), `hud_v2_ptt_check.py` (no
interrupt), `hud_v2_multichat_check.py` (mic not suppressed by a turn; PTT does
not interrupt; and `rv_stt_lands_selected`, which still described the pre-V2
W-6 behaviour of sending to a chat selected during STT).
Not claimed: a physical-microphone run, the 2.5 s pause, or a live deployment.

## 2026-10-13: V4 AudioWorklet, uncapped recorder, chunked STT

Plan decisions V-1 and V-11, §3.1 and §3.5. Stacked on V2/V3; builds the seam
V5 (Silero VAD worker, the three timers) and V12 (speaker verification) stand on.

- **Microphone.** `lib/micInput.ts` opens an `AudioWorklet` (`lib/micWorklet.ts`,
  bundled by Vite as its own same-origin module via `?worker&url`, so the CSP's
  `script-src 'self'` covers it; no CDN). All the logic is `MicFramer`
  (`lib/resample.ts`): render quanta at the device rate -> **windowed-sinc**
  resample (Kaiser beta 8.6, 85 % of the output Nyquist, 3 dB down at 6.5 kHz, 9 dB at 7 kHz,
  80+ dB beyond 8.5 kHz,
  zero group delay, position counted rather than accumulated; since the review: 85 % of the
  output Nyquist, 14 lobes) -> mono ->
  **512-sample frames at 16 kHz** posted to the main thread as transferred
  `Float32Array`s. They queue there if the main thread stalls instead of being
  dropped, which `ScriptProcessorNode` could not promise. The old boxcar
  average let a 12 kHz tone through 30 dB down; the new one is 80+ dB down at 8.5 kHz,
  the tone that would alias to 7.5 kHz (the first cut, at 90 % and 10 lobes, measured only
  -29 dB there, which the review caught; `resample.test.ts` pins 8.5 and 9 kHz at -60 dB).
- **Fallback, visibly.** No `audioWorklet` (insecure origin, old browser), no
  `AudioWorkletNode`, a module that fails to load, or a node that cannot be built:
  the *same* resampler runs behind a `ScriptProcessorNode`. `VoiceDiagnostics.path`
  / `pathNote` name the path (Settings -> Voice, `voice-capture-path`) and the
  window error line says "The microphone is using the older capture path: <reason>".
  The reason never contains a URL or device name. Neither path available = the
  existing "microphone unavailable".
- **Seam for V5/V12.** `MicInput.addFrameListener(fn)` (also `__hud.mic.listen`)
  gets every frame after the capture; V5's VAD worker posts from there and the
  worklet needs no change. Per-utterance audio is `Capture.hooks.onChunk(chunk)`:
  `chunk.samples` is the chunk's 16 kHz PCM (do not mutate or keep it),
  `chunk.index`, `chunk.startMs`, `chunk.overlapMs`, `chunk.source`. V12 embeds the
  first <= 3 s of chunk 0 there and can refuse the whole message before any upload
  by not forwarding it. `vad.loud` is the per-frame speech flag the recorder reads;
  V5 supplies it from its probability.
- **`UtteranceRecorder` (`lib/recorder.ts`), no cap.** The 40 s ring is now only
  the pre-roll. A claim (follow-up, wake, continuation, talk key) starts a
  recorder with the ring's audio from before the speech, then every frame until
  the message ends. `vad.uncapped` is set while a claim is live, so `MAX_UTTER_MS`
  (30 s) now only closes an utterance **nobody claimed** (room speech that never
  pauses). The talk key has no limit and no longer loses its start past 40 s.
  Ceiling: **15 minutes** (`UTTERANCE_LIMIT_MS`), after which the message goes as it
  stands and the error line says so (`Capture.hooks.onLimit`), never silently.
- **Chunks.** Memory is bounded by the chunk, not the message: the recorder holds
  at most one chunk (`CHUNK_HARD_MS` 25 s plus a frame). A chunk is cut at a pause
  where there is one: once it is `CHUNK_SOFT_MS` (8 s) long, the first quiet run of
  `CHUNK_GAP_MS` (700 ms); past `CHUNK_LATE_MS` (18 s), a quiet run of 300 ms. Such a
  cut loses nothing, so the next chunk has overlap 0. Failing that, a hard cut at
  25 s in speech, and the next chunk repeats the last `CHUNK_OVERLAP_MS` (500 ms).
  (Plan §3.5's single "700 ms segment gap" is V5's timer; here the gap is read
  off the energy detector's per-frame flag, and the constants are exported for V5.)
  A final chunk with no speech in it is not sent unless it is the first (a held
  talk key is the owner's say-so). `end(trim)` drops the silence that proved the
  message ended, never into the overlap.
- **Capture contract changed.** `CaptureHooks.onUtterance` is gone. `onListening(source,
  tag, utterance)` names the message; `onChunk(chunk)` for each chunk; and
  `onUtteranceEnd({utterance, source, ms, chunks, continuation, truncated})`
  **exactly once** for every `onListening`, after the last chunk (also when nothing
  was uploaded: chunks 0). The continuation window / chain cap (V3) arm at the
  message's end as before, and the token rides on `onUtteranceEnd`.
- **A recording stops uploading the moment the detector may not listen.**
  The mic going OFF, a card, a picker, Settings or a crash end the recording at
  once: what already left stays (it was captured while allowed), what is held is
  discarded and reported as that `DropReason` (`muted`, `suppressed:*`) with
  `truncated: true`; no continuation is armed. A **held talk key** carries on
  through `suppressed:speaking` and `suppressed:no_chat` (barge-in, V7: it pauses
  speech and records over the tail), but is stopped by the five above. This is
  stricter than before for a card appearing while the key is held (the old
  pipeline let the held key finish and upload). While the key is held the detector
  neither claims a second utterance beside it nor ends it at a pause, and a wake
  hit during it is ignored. A talk key pressed over a hands-free recording carries
  that recording on as `ptt`. A tap < 350 ms held is `too_short` as before.
  The unmute clamp (`micFloorSample`) now limits where the pre-roll *starts*;
  `captureChunks.test.ts` proves no audio from before it is in any chunk.
- **STT groups (`lib/stt.ts`).** `SttAssembler` keeps every V2 rule per clip
  (deadline `max(15 s, 2 x clip)`, one automatic retry, kept audio, Retry,
  `seq`, caps) and adds a *group*: `beginGroup`, `submit(..., {group, overlapMs})`,
  `closeGroup`, `whenGroupSettled`. A group is one diagnostics row (its length is the
  audio, overlap once), one `meta`, one placement. Its text is placed once, when the
  message has ended and no chunk is in flight: texts in order, the words an
  overlap repeats said once (`lib/joinChunks.ts`: longest run of words ending one
  and beginning the next, <= 8 words, exact match, with a fragment of >= 4 letters
  allowed only at the two edges where a cut can fall inside a word; applied only
  where the recorder overlapped, so "very | very" across a cut at a pause is kept).
  **At most two uploads in flight** (`STT_MAX_IN_FLIGHT`, passed by App; the class
  itself is unbounded unless told), the rest in `seq` order; a chunk that waited its
  turn is re-judged by `canUpload` when it starts (a card or OFF that came up
  meanwhile makes it `failed`, "not sent: dictation is paused", kept, never
  uploaded). The daemon is told `utterance_id` = the first chunk's id (= `seq` for an
  unchunked clip, so V2's numbering is unchanged) and `seq` = the chunk's own.
  - **A chunk's audio is released once its words are in**; a failed chunk keeps its
    own for Retry. Otherwise a long message would hold every chunk until its end and
    its length would become a memory bound (found by test: ten minutes tripped the
    20-clip cap at the 21st chunk).
  - **A failed chunk** does not hold the line: the others are placed together, in
    order, marked `late` (so AUTO boxes them, never sends half a message). The failed
    chunk shows `Couldn't transcribe 12.3 s (part 2 of 3 of a longer message)` in the
    Status pane with Retry/Discard, and Play/Retry on the Settings row. Its retry
    lands alone, `late` and `retried`, with the words its neighbours already said
    taken off its head and tail. Discarding one chunk does not cancel the others.
  - A chunk of silence in the middle is a normal blank answer; a message of only
    silence is `empty_transcript` once. A chunk refused by the backlog caps marks
    the message `late` and shows `STT BACKLOG`; the audio of that chunk is gone.
  - `STT FAILED`, "TRANSCRIBING n" and the rest are as V2; "TRANSCRIBING" is not
    written over LISTENING while the message is still being spoken.
- **App.** `onUtterance` is replaced by `onChunk` (re-judges crash, OFF and a waiting
  card before every chunk, as it judged the whole utterance; begins the group with
  the first chunk) and `onUtteranceEnd` (closes the group, ends listening, resolves
  the continuation token and a talk-key barge-in only when the *whole message's*
  fate is known). What a message is for (chat, mode, a follow-up pane that is
  gone) is fixed when it is claimed, not when it ends: a chat switch halfway
  through a long dictation boxes it with "SPOKEN IN ANOTHER CHAT".

Free checks: vitest 2351 tests / 145 files after the delta-review fixes (2347 after the first review fixes, 2315 before them, 2216 / 138 before V8 was merged; first-cut new: `resample.test.ts` 9,
`joinChunks.test.ts` 11, `recorder.test.ts` 9, `captureChunks.test.ts` 30,
`micInput.test.ts` 5, `stt.test.ts` +17 for groups; `capture.test.ts`,
`captureDiagnostics.test.ts` and `speech/suppress.test.ts` adapted to the new
hooks), `tsc` + `vite build`, and `tests/face/hud_v2_chunks_check.py` (the real
worklet in real time, the fallbacks, 45 s PTT, 36 s hands-free, failed chunk,
two in flight, OFF, card and card mid-message). Mutations that fail on purpose:
restoring the 30 s cap on claimed utterances (3 tests), dropping the overlap
dedupe (4), letting a card not stop a held key (2), not re-judging a queued chunk
(2). Source restored byte for byte afterwards.

Not done and not claimed: Silero/ML VAD, the 2.5 s pause and 700 ms/6 s timers
(V5); speaker verification (V12); any physical-microphone, Windows-Chrome or
live-daemon run. The 45 s and 36 s cases run as scripted frames through the real
detector, recorder and assembler (and the HUD with a mock daemon); only a ~14 s
dictation ran in real time through the worklet, so a 45 s *real-time* worklet run,
the worklet's CPU in Chrome (V0 measured the VAD, not this) and whether Chrome on
Windows delivers the worklet frames without gaps under load are unmeasured. A
transcript appears when the message ends, not chunk by chunk. Joining by words is
a heuristic: a hard cut in the middle of a word the model hears differently in
the two chunks can leave a fragment (shorter than four letters) or, rarely, drop a
word repeated on purpose exactly at a cut.

**Review fixes (2026-10-14, the lead's Opus review of the first cut; the owner confirmed that
a card or OFF cuts the recording):**
- **H1, a cut-short message is late.** `closeGroup(gid, {late})`: App passes
  `end.truncated || u.refused`, so a message cut by OFF, a card, a picker, Settings, a
  crash or his own voice is placed `late` even though every chunk that left answered, and
  AUTO boxes it (it used to be sent as if whole). `late` = a chunk failed **or** the message
  was cut short.
- **M1.** `onListening` is announced **before** the pre-roll is fed to the recorder: a long
  pre-roll (27 s of room speech, then a wake word) cuts chunks while it is fed, and they
  used to reach the consumer before it knew the message and were dropped silently.
- **M2.** A tap on the talk key over a hands-free message is not "too short": the key
  *adopts* the recording and release just finishes it. `claimSource` is reset after a key
  ends, so the detector's later `unclaimed` row is no longer labelled `ptt`.
- **M3, a stopgap until V12.** A message claimed by the follow-up window or as a
  continuation (no wake word, no talk key) ends at **60 s** (`CLAIM_CAP_MS`), boxed (late), with the error line saying so (`onLimit(ms, claimCap)`). A wake claim and the
  talk key keep the 15-minute ceiling; a key pressed over a hands-free message lifts the 60 s.
- **M4.** `CaptureHooks.hardStop()` names a card, picker, Settings or crash on its own, so
  `suppressed:speaking` / `no_chat` (reported first) cannot mask one: a held key over his voice
  or a closed chat now stops when a card or Settings arrives, and the drop names the card.
  App's `captureSuppression` lists the hard reasons first as well. `canUpload(explicit)` and
  `onChunk` also refuse under a picker or Settings; the owner's own **Retry** (`explicit`) is
  still allowed from Settings, which is where the Retry row lives.
- **L1.** When the previous chunk heard only the start of a word ("transcrip") and the next
  hears it whole, the fragment is dropped and the whole word kept (`overlapMerge`);
  `MAX_OVERLAP_WORDS` is 4.
- **L3.** A late message placed around a failed chunk carries `[…]` (`GAP_MARK`) where the
  missing part belongs (first, middle or last); Retry's text still lands at the end.
- **L4.** `onprocessorerror` hands the microphone to the ScriptProcessor and says so
  (`onPathChange`); `addModule` times out after 8 s into the same fallback;
  `Resampler.processShared` allocates nothing per call on the audio thread.
- **L5.** `Capture.muteNow()` (App `setMode` OFF) ends a recording at once, not at the next
  frame; `onUtteranceEnd` closes the group even if a status write throws; a pane left
  saying LISTENING after a cut-short message now says TRANSCRIBING or nothing; the limit
  messages say "Say Jarvis or hold the talk key" (speaking on would not be claimed).
- **Soft cuts start the next chunk with 200 ms of digital silence** (`CHUNK_SOFT_OVERLAP_MS`),
  so no first phoneme sits on a chunk edge; `chunk.repeats` is false for them, so the join does
  not look for echoed words ("very | very" stays). (First tried as 200 ms of the quiet audio
  before the cut; the delta review noted that "quiet" is only below the detector's threshold,
  so a faint trailing word could be heard twice. Silence cannot echo, which is the safer pick.)
- **The 60 s cap splits nothing (delta review).** A claim cut at the cap is announced
  `truncated`, so it is placed late and boxed, and arms **no** continuation: a 91 s follow-up used
  to be a 60 s piece sent and a 33 s continuation sent. A wake hit over a message the cap would
  cut lifts the cap and makes it a wake claim, as the talk key does. The 15-minute ceiling still
  sends as it stands.
- **Gap marks once.** `[…]` is added only on a message's first placement; a retried chunk placed
  alone adds none (chunks 2 and 4 failed, retry 2 gave "t2 […]" before).

## 2026-10-13: V13 local speech-to-text with a cloud-fallback switch

Plan §3.5 and §4 V13; Q6 answered (build local Parakeet with a switchable cloud
fallback; one paid comparison on synthetic speech, at most $0.25; local becomes
the default only if its WER is within 1 point of cloud). **Status: built and
tested on fakes plus one real-model smoke run; the default stays `cloud`, the
paid comparison has NOT been run, and nothing is deployed.**

- **Engine** (`jarvis/stt_local.py`). Parakeet-TDT-0.6B-v3 int8 through
  `sherpa-onnx` (`OfflineRecognizer.from_transducer`, `nemo_transducer`, greedy),
  inside the daemon. `sherpa_onnx` is imported only at first load, so the daemon
  runs without it. Threads are pinned (`config.STT_LOCAL_THREADS`, default 2,
  clamped 1..4; V0: more than 4 was 4-18x slower). Lazy load; a daemon
  `threading.Timer` drops the model after `STT_LOCAL_IDLE_MIN` (default 10)
  idle minutes (`Engine.reap`, never under a running decode). Decodes are
  serialised on one lock; each request waits no longer than its own deadline,
  and a caller that gave up marks its job cancelled so a job that never started
  never runs (no stacking). The decode itself cannot be interrupted: an
  abandoned one finishes in the background, a clip being capped at 90 s. A
  failed load is not retried for 60 s. WAV (16-bit PCM, any rate/channels) only:
  no media decoder runs on uploaded bytes, so Discord's Ogg would need a
  decision before it opts in. Every `LocalSTTError` message is a fixed sentence
  (no path, no library text).
- **Policy** (`voice.stt_result`, `voice.stt` is its `.text`). Settings
  (`jarvis/stt_settings.py`, `~/.config/jarvis/stt.json`, env
  `JARVIS_STT_SETTINGS`): `backend` = `cloud`/`local`/`auto` and `cloud_fallback`
  (bool, default on). Callers that do not pass `prefer_local` (Discord voice
  notes, the face server) are unchanged: the cloud chain, whatever `backend`
  says. The HUD's `/stt` opts in (`prefer_local=True`). With `local`/`auto`, local
  decodes first; on failure or timeout the cloud answers with what is left of
  the caller's budget, only if `cloud_fallback` is on. With a budget, local gets
  at most `STT_LOCAL_BUDGET_SHARE` (0.45) of it while a fallback is allowed, and
  all of it when not. `cloud_fallback` off forbids EVERY upload for every
  caller: the cloud chain is never entered; a local failure is a visible 502
  ("...cloud fallback is off, so nothing was uploaded"), and a path that would
  need the cloud (backend `cloud`, or a caller that did not opt in) is
  `STTBlocked`, a 409 the HUD does not retry. `auto` with no local engine goes
  straight to the cloud without a complaint. The chosen backend and the reason
  local did not serve are in the `/stt` reply (`backend`, `model`, `fallback`).
- **Fail closed.** A missing `stt.json` is the defaults. One that exists but is
  not JSON, not an object, a symlink, over 4 KB, or has a key of the wrong type
  reads as `local` with no upload, and `/stt/settings` says so. A repair through
  one switch keeps the other at its fail-closed value. `stt.json` is **protected
  state** (`tools/files._protected_state_paths`): no agent write, grant or shell
  command may change it, since flipping it could send the owner's voice to the
  cloud and an upload cannot be undone. **Owner decision, 2026-10-14: keep it
  protected** (plan §2, V-12a; the protected-state set, not the SELF_PROTECTED
  source list). The shell detector and v2's layer 1 are pinned for it
  (`tests/voice_stt_local_check.py`, `tests/v2/permissions_check.py`), and v2's
  refusal says what the file is.
- **Route.** `GET/POST /stt/settings`, owner-only on the HUD listener
  (`{backend?, cloud_fallback?}`, validated before anything is written; a read
  never loads the model). Settings -> Voice has a Speech-to-text section: the
  three backends, the Cloud fallback switch, one sentence saying what happens to
  the next clip, and the local engine's state (installed, loaded, threads, idle
  unload, or how to install). The window keeps nothing: it shows the daemon's
  answer after every change. Diagnostics rows say `· local`, `· cloud` or
  `· cloud (local failed)` per clip (known values only, never daemon text).
- **Real-model smoke** (`tests/voice_stt_local_real.py`, gated by
  `JARVIS_STT_LOCAL_REAL=1`, V0 venv, 4G scope), one synthetic Kokoro clip:
  WER 0, cold 2.7 s (load included), warm RTF 0.055, a 48 kHz stereo copy still
  decodes, RSS 41 -> 1117 MB loaded -> **78 MB after unload** (`gc` plus
  `malloc_trim`), reload on demand. Synthetic speech, one clip: it shows the
  wiring works and that the 1.1 GB is returned, not accuracy.
- **Deploy** (once, by the lead): install `sherpa-onnx` into the daemon venv
  (the V0 probe venv has 1.13.8, which has a CPython 3.14 wheel), then
  `python -m jarvis.stt_local fetch` (about 490 MB, SHA256-pinned, same digest as
  `tests/voice_v0/models.json`, extracted with the `data` filter under member
  and size caps into `~/.cache/jarvis-stt/parakeet-tdt-0.6b-v3-int8`; or point
  `JARVIS_STT_LOCAL_MODEL` at an existing directory). Restart the daemon, then
  Settings -> Voice shows "installed". Nothing changes until the owner picks
  Local or Auto. The first load after each daemon start hashes about 640 MB of
  model files (`verify_model`, once per file identity) on top of the 2-4 s load,
  so the first clip after a start may miss its local share of the budget and
  fall back to the cloud (or fail, with the fallback off).
- **The default flip** is one line: `config.STT_BACKEND_DEFAULT = "cloud"`. Run
  `tests/stt_compare_probe.py` first (header has the command; `--yes-spend`
  required, `--dry-run` free, hard cap $0.24, estimate at 4x the listed price,
  no key printed, no `.env` read); its output ends "local within 1 point of
  cloud: YES/NO". Synthetic speech bounds the models from below and says nothing
  about the owner's voice.

Free checks: `tests/voice_stt_local_check.py` (settings, engine lazy load,
threads, serialisation, deadline, abandoned jobs, idle unload, load-failure
backoff, safe errors, audio handling, pin parity with V0's manifest, policy
matrix incl. local-only never calling `llm.transcribe`, budget share, Discord/face
unchanged), `tests/stt_compare_probe_check.py` (the probe's spend guards),
`tests/v2/hud_backend_check.py` (`/stt` reply keys, 409, `/stt/settings`),
`tests/v2/frame_check.py`, `hud/src/lib/sttSettings.test.ts`,
`stt.test.ts`/`voiceDiagnostics.test.ts` (served-by), and
`tests/face/hud_v2_stt_local_check.py` (real HUD, mock daemon, 17 checks).
Mutations that each fail the Python suite: serialisation lock removed, idle
reap that never unloads, a fallback-off path that still uploads, no budget share,
a corrupt settings file that fails open, non-opt-in callers using local, the
unpinned thread clamp, a cancelled job that still decodes after its load.
Not done and not claimed: the paid comparison, any deployment, a run in the
live daemon, a physical-microphone run, WAV-only means no Ogg/webm locally.

### V13 review fixes (2026-10-14)

- **The privacy promise is scoped to HUD dictation.** Chrome's wake recognizer
  (`webkitSpeechRecognition`, App.tsx) streams the microphone to Google whatever
  `stt.json` says, so with Cloud fallback OFF (or its state not yet known) it
  never starts: wake-by-voice is off until V11's local wake detection, and the
  talk key still works. Settings -> Voice says so ("Dictation audio never leaves
  this computer. Wake word by voice is off until local wake detection lands; use
  the talk key."), and with it ON it says the wake word uses Chrome's speech
  recognition (Google). With local chosen and the fallback on, the summary says
  Discord voice notes and the face server still use the cloud. The window's copy
  of the settings (`hud/src/state/sttSettings.ts`) is shared by the panel and the
  recognizer's gate. **Every window hears a change at once:** the daemon
  publishes an `stt_settings` event on `/events` after each successful POST and
  whenever a read finds the resolved value changed by itself (a hand edit, a
  corrupt file; `stt_settings.watch`), and each window applies it, because a
  recognizer in a second window would otherwise keep streaming to Google. The
  backstops are a re-read when the window becomes visible (`visibilitychange`,
  not focus), on every event-stream (re)connect, every 60 s, and every 15 s
  while the value is unknown. `hud_v2_stt_local_check.py` asserts the recognizer
  never starts after a reload with the fallback off, stops when it is switched
  off, and starts when it is switched on again; `hud_v2_stt_wake_windows_check.py`
  (two pages, one mock that fans events out) asserts the other window stops with
  no focus change, and carries a control run with the event dropped in which the
  other window keeps streaming.
- `wav_samples` maps any exception from `wave`/numpy on hostile bytes to
  `LocalSTTUnavailable` (a fuzzed header was a 500); a queued job now waits for
  the engine in 50 ms slices and leaves the queue as soon as its caller has gone.
- `fetch` caps the download (700 MB) while streaming, refuses to replace a
  directory without the `.verified` marker, and records per-file hashes there;
  `verify_model` re-checks them when the model loads (once per file identity:
  path, inode, size, mtime, ctime). This guards against corruption, not
  tampering: the marker lives in the same writable directory. A marker that is
  there but unreadable, not JSON or without a `files` table refuses ("run the
  fetch again") rather than being trusted. A model directory the owner supplies
  has no marker and is trusted as it is: only the download is verified. The
  check runs under the engine's lock and the directory is writable by anything
  running as the owner, so it cannot be made to hang or exhaust memory: the
  marker and every model file are opened `O_NOFOLLOW | O_NONBLOCK` and must be
  regular files (a symlink, FIFO or device refuses at once), the marker is
  read to at most 64 KB, and each model file to at most its size, which must
  equal the size `fetch` recorded in `.verified` (`sizes`).
- The compare probe charges its estimate for a failed request too. An invalid
  `backend` reads "using local" (the fallback keeps its value); `auto` with
  nothing installed and the fallback off says the engine is not installed, not
  that cloud was switched off.

## V15: lowering the Remote Browser's sound while Jarvis speaks (2026-10-13)

Q14 is answered (plan section 7): duck Remote tabs through WSLg. The daemon lowers the Remote
Browser's own PulseAudio sink-inputs by about 15 dB while the HUD holds a short lease, and
puts them back exactly. It never unmutes, never touches another app's stream, and does
nothing for Direct frames (cross-origin iframes, not observable or controllable from here).
Code: `jarvis/v2/speech_duck.py`, `BrowserSound.duck_owners()` (`browser/sound.py`),
`hud/src/lib/speech/duck.ts`. Tests: `tests/v2/speech_duck_check.py` (58),
`hud_backend_check` (route), `frame_check` (walk), `hud/src/lib/speech/duck.test.ts`,
`tests/face/hud_v2_duck_check.py` (23). Committed and pushed on `claude/voice-v15`; not merged,
built-and-installed or live-validated. **Physical audibility of the duck is UNMEASURED.**

### Probe (2026-10-13, local only, no network)

WSLg's server is PulseAudio 17 at `/mnt/wslg/PulseServer`, one sink `RDPSink`,
`module-stream-restore` loaded. A throwaway `/opt/google/chrome/chrome --headless` with a
temporary profile and `PULSE_SERVER` set (the way B-SOUND launches an audible instance)
played a 1% sine from a `file://` page. Every process and stream I started was stopped; I
changed the volume only of sink-inputs whose PID was in my own Chrome tree (and unmuted
one of my own after muting it to test).

- **Attribution works, by PID ancestry.** One sink-input appeared, `application.process.id`
  = the Chrome *audio service* utility process (`--type=utility --utility-sub-type=
  audio.mojom.AudioService`), a **direct child of the browser root** whose command line carries
  `--user-data-dir`. `application.process.binary` was `chrome`, `application.name` "Google
  Chrome". The existing `audio.OwnedChrome.chain(pid)` (executable, uid, start time, ancestry up
  to the pinned root) therefore attributes it, and the root is found the way the mute finds
  it (`chrome_pids(folder)`, exactly one pinned root).
- **Name matching would be wrong, and the probe showed why.** During the probe a second,
  unrelated sink-input from another agent's Playwright `chrome-headless-shell` (application
  name "Chromium") was playing. It has its own PID and ancestry, so it is never ours; a
  name-based rule would be one rename away from touching it. It was only read.
- **Volume read and exact restore work.** `pactl --format=json list sink-inputs` gives
  per-channel raw values (`volume.front-left.value`, 65536 = 100%). `pactl
  set-sink-input-volume <id> <v1> <v2>` set an asymmetric (40000, 30000) pair, a duck pair and
  the original pair again, each read back bit-exactly. Setting a volume on a muted stream left
  `mute` untouched (the code never sends a mute command at all).
- **Scale.** PulseAudio's raw volume is cubic in amplitude: pactl's `-15dB` is raw 36854
  (56%), `18%` is -44.7 dB. The duck is `raw * 10**(-15/60)` per channel, which is a true
  -15 dB and keeps the balance.
- **`module-stream-restore` remembers pactl volumes (a finding that shaped the design).** After
  I set a stream to 18% and killed its Chrome, the *next* Chrome stream (a new process, new
  profile) was created at 18%. PulseAudio keys that memory by application name ("Google
  Chrome"), and every `set-sink-input-volume` is saved. So a duck that is not restored, whether
  the daemon is killed or the Chrome restarts mid-duck, would make the next Remote stream start
  quiet with nobody having asked. Restoring writes the prior value, which is then what is
  remembered (checked: a 100% restore made the next stream start at 100%).
- **pactl latency is not constant.** Normally 0.00 s; for a few minutes, with several agents'
  browsers running and load average 6, individual `list` and `set` calls took 6 to 15 s. Every
  call is therefore bounded (2 s) and failure is non-fatal and retried; under such load the
  duck is simply late, never stuck.

**Live smoke of the finished code (same day, real pactl and real `/proc`, a throwaway
headless Chrome with a temporary profile and a temporary journal):** the stream was attributed
by `OwnedChrome.chain`, went 65536 -> 36854 per channel under a lease, the journal held
prior/applied, `release()` restored 65536 exactly and deleted the journal, and a lease left to
expire restored the same way. The one other app's stream present earlier was not touched.

### What it does

- **Route** `GET`/`POST /speech/duck` (owner-only, HUD listener, like `/speech/voices`);
  body is exactly `{on: bool}` (unknown or missing key, or a non-boolean, is 400; a daemon that
  is stopping answers 409). `on:true` grants or renews a **3 s lease**; `on:false` ends it.
  The answer is `{on, ducked, lease_s}`: counts only, never PIDs or stream names.
- **The HUD** (`DuckLease`, wired in `createSpeech`): the speech controller's `active` (playing,
  buffering, or inside the 300 ms hangover) takes the lease at once and renews it every second;
  pause, stop, end, the Settings switch going off, or `pagehide` end it. Requests never overlap and
  an `off` cannot overtake an `on`; a failed request is dropped (the next renewal, or expiry,
  covers it). Settings, Voice: **"Lower Browser sound while Jarvis speaks"**, in
  `localStorage` (`jarvis.duckBrowser`), **on by default**; one choice for every window (a
  `storage` listener applies another window's change). Only the window that holds the speech
  lock ever has an active queue, so only it holds a lease. Each window sends its own random lease
  token (`window`, 32 hex, optional in the route) and the daemon keeps one deadline per token
  (at most 16), so one window's `off` or expiry cannot end another's lease; the duck lasts while
  any lease lives.
- **Whose streams.** A sink-input is lowered only if its PID descends from a Remote Chrome
  named by `BrowserSound.duck_owners()`: an instance that launched audible
  (`instance.audio.applied`), is not closing or Private, and for which the owner's request and
  the master permission stand **now** (`preference(profile)`). Anything else (master OFF,
  structurally muted launch, nothing requested, Browser not running) lists no owner and
  causes **no pactl call at all**. It never starts the Browser. A stream with no process is
  never ours; a stream whose chain does not reach the pinned root is never ours.
- **Never the mute.** The runner (`speech_duck.pactl`) allows only the JSON listing and
  `set-sink-input-volume <index> <1..8 raw values>`; mute, default devices and everything else
  are refused before a process starts (tested). A muted stream is left entirely alone, and a
  mute set during the duck survives the restore.
- **Exact restore, owner wins.** Each held stream records its prior per-channel volume and the
  value that was written. Restore (lease end, `off`, expiry, shutdown) writes the prior value back
  **only if the stream still has the value we wrote**. If it does not (the owner moved the slider
  in the Windows mixer, or anything else changed it) the stream is not touched again, now or at
  restore, and is not re-ducked for the rest of that lease; the next lease ducks it afresh from
  where the owner left it.
- **A stream that appears mid-duck** is found on the next scan (every 0.5 s while a lease
  lives) and ducked from its own volume, then restored to it. It can be audible at full volume
  for up to one scan interval plus the pactl latency; that is the one unavoidable gap (no
  subscription, to keep the B-SOUND "no leak-free claim" posture). A stream born carrying exactly
  a duck level (the stream-restore case above) takes the prior of the duck that made that level,
  not the level itself.
- **Index reuse.** Before a write the listing is repeated and the stream must still have the same
  PID and volume, and the chain is re-checked. The residual race (the server frees and reuses an
  index between that check and the write) is the one B-SOUND documents for the mute; it cannot be
  closed from pactl.

### If the daemon dies mid-duck

- **A closed or crashed HUD, a lost connection, or a daemon whose HUD stops renewing:** the lease
  expires 3 s after the last renewal and the worker restores (nothing else is needed).
- **Orderly daemon shutdown:** `Daemon.stop()` calls `speech_ducker.stop()` first (before Chrome
  closes) and restores, bounded to about 6 s.
- **`kill -9` of the daemon (or a pactl that never answers):** nothing can run. Each duck is
  written to `~/.config/jarvis/speech_duck.json` (`config.SPEECH_DUCK_PATH`, 0600 via a random
  `mkstemp` file and `os.replace`, at most 64 KB; active ducks are always kept and only the oldest
  leftovers are cut at 16; deleted when everything is restored) *before* the volume is written. The next daemon start (`Ducker.recover()`, only for the owner's data folder) reads it;
  while it holds an entry the idle worker polls every 3 s, and only when a Remote Chrome is
  audible: a Remote stream that appears carrying exactly the recorded ducked value is set back to
  the recorded prior value (also under a new lease). Whether a Chrome that outlives a
  `kill -9`'d daemon keeps its stream ducked was not tested (its pipe to the daemon closes, so it
  most likely exits); the journal covers both. **Remaining gap:** until a new Remote stream appears (the owner turns Browser
  sound on again) the stored volume sits in PulseAudio's memory, and an entry older than 30 days is
  forgotten; a corrupt or missing journal repairs nothing. The owner can always fix a stuck
  volume in the Windows volume mixer, and nothing here changes the mute.
- Chrome restarting mid-duck (B-SOUND's idle replacement, sound OFF then ON) is the same case: the
  vanished stream becomes a journal entry and the new stream is repaired when it appears.

### Review fixes (2026-10-14, PR #156 review at `7f908b11`)

- **A forged journal can no longer raise a volume.** On load an entry must have `prior` at most
  100% per channel, `applied` within 2 of the duck of `prior`, equal channel counts, and a finite
  time that is not in the future (beyond 300 s of skew); otherwise it is dropped. Every volume
  written is at most 100% per channel (the runner refuses more; a restore or repair writes
  `min(prior, 100%)`), and a duck writes `min(duck of prior, the stream's current volume)`, so it
  can only lower. The regression is the reviewer's repro (`prior` 262144), plus a mutant for each
  guard. A stream the owner boosted past 100% is left alone (giving it back would need a write
  above the norm).
- **The journal is written through `tempfile.mkstemp`** (random name, `O_EXCL`, 0600) then
  `os.replace`; a FIFO or hardlink at the old predictable name cannot block the worker (tested).
- **Restore is bounded**: one overall deadline for `stop()` (6 s, worker join included) and each
  call gets only the time left; the first pactl failure stops that round of writes, and the rest
  stay held and journaled. A leftover in the journal covers what shutdown could not restore.
- pactl runs through the same capped streaming runner as the mute (`audio.run_bounded`; the
  mute's own allowlist and tests are unchanged).
- Only the owner's daemon reads or writes the journal (`Daemon._duck_journal`, the same
  `_owns_data` rule as the carrier sweep).
- `duck_owners()` excludes any instance that is not `audio.synchronized(True)`: a mute attempt
  pending or ever made on it (tainted) is not ours to lower.
- **One continuous duck is capped at 10 minutes** (`MAX_LEASE_S`): the volumes are restored and
  stay so for 5 s even while renewals continue, then a request starts a new period (a stuck
  window renewing for ever is audible as a short gap every 10 minutes, not a permanent duck; a
  genuine 10-minute read-aloud has the same gap). The idle leftover poll backs off 3 s -> 60 s
  while Chrome plays with nothing to repair.

- **One odd foreign stream cannot block a restore.** `parse()` fails the whole listing only on
  structural problems (not a list, more than 256 rows, a row that is not an object, an index that
  is not an integer or is repeated, a `mute` that is not a boolean). A row with a non-numeric
  `application.process.id`, a volume above the read bound or a bad channel map is just dropped,
  never written; a held stream that turns malformed is treated as gone and becomes a journal
  leftover. **Residual:** a listing larger than `OUTPUT_CAP` (256 KB) or with more than 256
  streams, or a repeated index, still fails the listing, so nothing is written and the duck
  cannot be undone until pactl answers sanely again (the journal covers a restart).
- `audio._run_bounded` is private and applies no allowlist; only `audio._pactl` and
  `speech_duck.pactl` call it, each after its own allowlist. `stop()` is rechecked immediately
  before a duck is written.

### Not done, and not claimed

No physical audibility, echo or latency measurement; the Windows-side mixer behaviour with the
owner's volume sliders is inferred from the listing, not heard. The fallback ("hold speech while a
tab plays") was **not** needed: attribution is possible. Direct frames are unchanged and there is
no ducking hook for them. Browser audio as a mic echo source (voice plan 3.14) is still
UNMEASURED and speaker verification covers it.

## V8: spoken summaries (2026-10-13)

Owner's answers to open question 2: **summary only**, and (confirmed) **hold
until close, then speak**. A message is held until it closes (a `text`, a tool
call, the turn's end). Up to 600 spoken characters (what the speech filter lets
through; code never counts) it is then read as written; a longer one is
replaced by a short summary, with no verbatim lead-in. This **supersedes plan
§3.8's** "read the opening to ~400 characters, then summarise the rest" (the
plan now carries the amendment) and reverses V6's "speech starts while the
model writes" for the per-message decision. Mid-task messages are short and
close at the next tool call, so the cost is the few seconds the message takes
to finish. (`hud/src/lib/speech/controller.ts`, `summary.ts`; the 1200-character
V6 stopgap is gone.)

- **Route.** `POST /speech/summary {thread_id, text}` -> `{summary}`
  (`jarvis/v2/speech_summary.py`, `hud_api.speech_summary`). Unlike `/say` and
  `/stt` (which have no owner check; tracked as a follow-up, unchanged here),
  it is **owner-only** (`projects.owner_only`, before the body is read): it is
  a paid model call. Then: body capped at 1 MiB (413), `text` over 300,000
  characters refused (400) before any cleaning, at most 4 in flight (429, a
  timed-out call keeps its slot until it returns), unknown keys 400. Failures
  are 400/429/502/503/504 sentences carrying no provider text. `spoken_prefix`
  from the plan's contract is not accepted: under summary-only there is no
  spoken opening.
- **Model.** Tier `speech_summary` (`JARVIS_SPEECH_SUMMARY_MODEL`, default
  `anthropic/claude-haiku-5.5`), read straight from `config.TIERS`, **not**
  through `models.tier()`: the default equals the orchestrator default, so
  `tier()` would follow the owner's chat-model selection and an Opus pick would
  make every summary an Opus call (owner confirmed this must not happen). A
  Chinese-hosted override is skipped for the default (`models.cn_hosted`); with
  nothing acceptable the route is 503. A failing model tries the next within the
  one deadline. `JARVIS_CHAT_PROVIDER` is not applied to this call (`provider=""`).
- **Request.** `scrub` + `strip_invisible` on the text, then `untrusted.fence`
  (fresh nonce per call, marker phrases neutralized) so a reply cannot close its
  own fence; `cache=False`; `stream=False`, `max_retries=1`, `max_tokens=1024` (a ceiling; was 160, see the receipt), two tries per model.
  Output is cleaned with `voice.speakable`, bounded to 300 characters at a
  sentence. A request never exceeds `min(JARVIS_COMPACT_AT_TOKENS, 90k)`
  (estimated at 3 characters a token, less 1000): longer text is summarised in
  at most 4 parallel sections, then the sections' summaries are summarised.
  Deadline: 6 s total on the daemon; the HUD waits 8 s. A timed-out call's cost
  is logged as unknown.
- **HUD failure path.** A failed, late (8 s) or unusable (empty, over 400
  characters) summary reads whole sentences up to 400 characters and then
  "...and more on screen." (kind `fallback`).
- **Ordering and silencing.** Summaries sit in the thread's order: a later
  message of the same thread waits behind a pending summary; other threads do
  not. A per-thread **epoch** is captured when a message closes and checked at
  `deliver()`: a send to the thread, Stop, an interrupted turn, Skip with nothing
  sounding (it takes the oldest awaited summary and its thread only), the pane leaving, read-aloud off and the lock lost all bump it, so
  the pending summary *and every message queued behind it* are dropped even if
  the summariser answers despite its abort (review HIGH-1; vitest pins each path
  and the mutations of the epoch check and its bumps fail them).
- **While a summary is awaited** (`SpeechView.summarizing`) speech counts as
  `active` (the detector stays held off), `onIdle` (the follow-up window) is
  withheld and then paid once nothing is coming, and a barge-in holds the
  delivery until `endBargeIn` (a send in between drops it).
- **With V7's shared queue.** `deliver()` is still the only place anything is
  enqueued: it takes the thread's known voice (`voiceFor`, asked again at
  synthesis), queues the whole message and calls `queue.endMessage` at once, so
  the floor is free the moment it has been said. A message out for its summary
  has no items, so it holds no floor and the backlog collapse cannot see it; it
  is queued when the answer comes. `turnEnded` (marks the reply a collapse never
  takes) runs in the thread's lane after the reply is queued, not at
  `turn_finished`, or it would mark the wrong message. Mute and a pane leaving
  go through `prune` -> `leave` -> `cancel`, which also finds a thread that
  only has a summary pending (after a reconnect forgot its open message).
  `skipThread` with nothing of that chat queued drops its awaited summary, else
  the message it is writing. `shared.test.ts`'s old skipThread case assumed
  speech while a message streams and was rewritten for hold-until-close.
- **Not done / not claimed.** Live Haiku quality of the summaries and the
  numbers below are unmeasured until the probe runs. Nothing was run against a
  speaker.

### Paid probe receipt (owner-approved, cap $0.10)

Run by the lead on **2026-10-14** from the main checkout
(`tests/speech_summary_probe.py --yes-spend`), model
`anthropic/claude-haiku-5.5`, 24 calls on synthetic replies of 723 to 8053
characters, no real chat content. Numbers as relayed by the lead (the console
output itself was not kept):

| | |
|---|---|
| Calls | 24, of which **7 failed with 502** (29%) |
| Total spend | $0.002314 (cap $0.10) |
| Mean cost per call | $0.000136 |
| Prompt tokens, mean | 902 |
| Completion tokens, mean | 92 |
| Latency | p50 1.46 s, p95 2.1 s |
| Failed inputs (characters) | 786, 832, 1742, 1778, 3635, 3644, 8053 |

Cost and latency are well inside the plan's estimate ($0.0003 a summary, a
second or so). The failure rate is not acceptable: the owner would hear the
truncated fallback about three times in ten.

**Cause.** Not proven by the first run (the second run below confirmed it). The probe kept only "502" for the failed calls, and the
route answers a fixed sentence, so the internal reason was lost. What the code
shows: the inputs are not the cause (the seven are scattered across all four
sizes and the first call of the run was one of them; `prepare()` leaves every
probe text unchanged, so `scrub` and the fence are not involved), and there are
only three ways `summarize` returns a 502: the call raises (a provider or
network error, one attempt, no retry), the reply comes back with no text, or the
text is empty after `clean()`. The bounds in `clean()` cannot empty a reply that
has words, whatever its sentence ends (tested). The likeliest cause is the first
two together: `max_tokens=160` was the *whole* budget, so a model that reasons
before it answers could spend it on reasoning and return nothing (finish reason
`length`), and a transient 429/5xx/529 was never tried again. A mean of 92
completion tokens for replies of at most 300 characters (about 70 tokens) is
consistent with some thinking being counted.

**Fix (V8 fix, `claude/voice-v8-fix`).** `OUT_MAX_TOKENS` 160 -> 1024 (a ceiling;
length is still the prompt's and `clean()`'s job); each model gets two tries
inside the one 6 s deadline, the second for a transient HTTP error (anything but
400/401/402/403/404/413/422) or an empty reply, with a retry that must leave
0.3 s; content parts are read as text. Every failure now carries a **reason
code and numbers only** (`SummaryError.reason`/`.detail`: `http_<status>`,
`llm_error`, `empty_content`, `empty_after_clean`, `exception:<Class>`, `deadline`;
finish reason, completion and reasoning tokens, whether a reasoning field was
present, raw length), logged and printed by the probe, never sent to the HUD.
Tests in `tests/v2/speech_summary_check.py` (`FailedSummaries`) reproduce each
path with a fake reply, and two mutations (the old 160-token budget, a single
attempt) fail them.

**Still to do (done 2026-10-14, see the second run below).** A second probe
(owner's OK; same cap) to confirm the failure rate and to read the reason codes
from a real run. If `empty_content` shows `finish=length` with
`reasoning_tokens` close to the budget, the cause was the budget; if `http_*`,
the retry; if neither, the next step is turning reasoning off for this call.

### Second probe receipt (owner-approved, same $0.10 cap)

Run on **2026-10-14**, from main `3165b31`, after PR #164 was installed.

| | |
|---|---|
| Model | `anthropic/claude-haiku-5.5` |
| Calls | 24, **0 failed**, **0 retried** |
| Total spend | $0.004219 (cap $0.10) |
| Mean cost per call | $0.000176 |
| Prompt tokens, mean | 956 |
| Completion tokens, mean | 160 (one reply used 392) |
| Latency | p50 1.57 s, p95 2.73 s, max 3.55 s |
| Estimated costs | none |

**Conclusion.** The old `max_tokens=160` budget was the cause. Replies needed up
to 392 output tokens, and with 0 failures and 0 retries under the 1024 ceiling,
the retry path was not what fixed it. The second run confirmed it. No failure
reason code was produced, because nothing failed, so none is recorded here.

**Cost note.** The mean cost per summary rose from $0.000136 to $0.000176
because the replies are now complete. Both means cover successful calls only:
the first run's $0.000136 is its $0.002314 total over the 17 calls that returned
a summary, and this run's $0.000176 is over all 24.
