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
