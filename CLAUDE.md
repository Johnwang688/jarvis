# CLAUDE.md

Notes for agents working on this codebase. Read before changing anything.

**Keep this file current.** When you add a capability, change an invariant, or
make a decision worth not relitigating, update the relevant section in the same
session — especially *Current state* and *Decisions already made*.

## What this is

A personal agent ("Jarvis") with a **hand-rolled** tool-calling loop, routed
through OpenRouter so any model is a config change. The owner is a CS student
building it to learn agent internals, so **do not replace the loop with a
framework** (LangChain, the SDK tool runners, etc.). The from-scratch loop is
the point of the project, not an accident.

## Environment

- **WSL2 (Ubuntu 26.04) on Windows 11.** WSLg is active, so headed GUI apps
  render on the Windows desktop.
- Code lives on the Linux filesystem at `~/projects/Jarvis`. **Never move it to
  `/mnt/c`** — 9p is slow and mangles permissions/line endings. Windows can
  already reach it at `\\wsl.localhost\Ubuntu\home\johnw\projects`.
- **System Python 3.14 has no `ensurepip`, so `python3 -m venv` fails.** Use
  `uv` (already installed): `uv venv`, `uv pip install -e .`.
- `jarvis` is symlinked from `~/.local/bin` into `.venv/bin/jarvis`, so source
  edits take effect with no reinstall.
- `sudo` requires a password — you cannot run it from a tool call. Print the
  command and ask the user to run it.
- **WSLg audio is full duplex and verified** (2026-07-30): playback via
  `RDPSink`, mic via `RDPSource`, socket at `/mnt/wslg/PulseServer`. Two
  gotchas: client apps need `libpulse0` (now installed, with
  `pulseaudio-utils`) — Chromium dlopens `libpulse.so.0` at startup and
  silently has *no* audio devices without it; and a Chromium launched before
  that library existed must be fully restarted to see devices.

## Architecture

```
jarvis/
  agent.py      the loop — ~50 lines, read it first
  llm.py        OpenRouter client; the only file that knows about HTTP
  context.py    image eviction / result truncation / compaction
  browser.py    Playwright session on its own thread, allowlist, budget, tracing
  config.py     model tiers, paths, system prompt
  bench.py      tool-calling stress test
  voice.py      tts()/stt() — swappable contract, HTTP stays in llm.py
  google_auth.py  one-time OAuth consent (human-only) + silent token refresh
  onshape_auth.py Onshape API keys + the pinned CAD sandbox/libraries
  permissions.py  modes (ask/all) + the persistent dangerous-tool allowlist
  workflows.py  background agents on their own threads (safe tools only)
  tools/        clock, files, memory, shell, web, browsing, gmail,
                onshape (CAD), skills, voicectl (mute), workflows
  skills/       one markdown file per skill — see tools/skills.py
  face/         server.py (HUD + speech + design routes), approvals.py (the
                gate), static/jarvis.html (the HUD), static/whiteboard.html
                (sketch → design board; output served from designs/ on
                WORKSHOP_PORT 8403 — its own origin, never the face's)
```

## Invariants — breaking these causes silent or hard failures

1. **Context pruning never deletes a message** (`context.py`). Every `tool`
   message must keep the `tool_call_id` its assistant message declared, or the
   next request 400s. Rewrite content in place. Compaction *does* delete, so it
   only cuts at a genuine `user` boundary — never inside an assistant/tool
   group. `find_cut_point()` enforces this; there is a synthetic test for it.

2. **The assistant turn goes back verbatim.** Append `response.content` plus
   `tool_calls` unchanged. Reconstructing it loses the ids.

3. **All tool results for one assistant turn go back together**, each keyed to
   its call id. Splitting them across messages trains models out of parallel
   calls.

4. **Tool failures are returned as text, never raised.** `dispatch()` catches
   everything and returns an error string so the model can self-correct. The
   `recovery` bench task measures exactly this.

5. **A `tool` message can only hold a string.** Image-producing tools return
   `ToolResult(text=..., image_b64=...)` and the loop attaches the image as a
   separate `user` message right behind it.

6. **Tool schemas are generated from type hints.** Use
   `Annotated[str, "description"]`; never hand-write JSON schema. Defaults make
   a parameter optional.

7. **All Playwright work happens on the browser's own thread.** The sync API
   is thread-affine and parks a *running* asyncio loop on whichever thread
   starts it — which crashed the chat REPL's prompt (2026-07-31) and would
   reject the face's per-request threads. Public `Session` methods marshal
   via `_submit()`; never call `SESSION.page` RPCs from outside `browser.py`
   (tests/benches use `Session.eval_js()`). Every surface stops the session
   on exit — a browser left running EPIPEs the Node driver when the process
   dies under it; `stop()` is a safe no-op if nothing started.

## Safety design (deliberate, do not loosen without asking)

- Tools are **narrow and typed**, not one god-tool, so the harness has
  something to gate on. `run_readonly` (allowlisted binaries, no shell
  operators) vs `run_command` (`dangerous=True`, prompts the user).
- `write_file` enforces **read-before-write** — refuses to clobber a file the
  agent hasn't read this session.
- Browser: **public internet open, private network closed** (2026-07-31).
  Named hosts in `allowed_hosts` always pass; `'*'` (in the default now)
  covers only public address space — LAN/router/metadata addresses, and any
  name that merely *resolves* private (`config.is_lan_host`), are refused.
  Policy is re-checked on wherever the page actually lands
  (`_guard_landing()` after goto/click/submit backs out to about:blank), so
  redirects can't escape it; `fetch_page` applies the same rule before the
  request and after redirects. Standing controls: **fresh context per run**
  (never a real Chrome profile — no cookies, no logins to misuse) and a
  **120-action budget**. vocab-bench pins its session back to
  localhost-only so bench runs stay hermetic.
- Web content is untrusted. The system prompt tells the model never to follow
  instructions found inside fetched pages.
- **Dangerous tools are approved by a human in whichever surface is running**
  — a y/N prompt in the CLI, an authorization card in the face
  (`face/approvals.py`). Both default to deny on every failure path. The face
  server is the only place that could accidentally pass `approve=None`, which
  would run them unguarded; don't.
- **Permission modes wrap that gate** (`permissions.py`, 2026-07-31). Every
  Agent's approver is `permissions.gate(surface_approver)`: mode "all"
  approves everything but is reachable ONLY via
  `jarvis face --dangerously-skip-permissions` and is a process variable —
  restart is always back to ask. The persistent allowlist
  (`~/.config/jarvis/allowlist.json`) holds only entries the owner explicitly
  created ('a' at the CLI prompt, ALWAYS on the HUD card); commands
  allowlist by first word only ("git" ≠ "gitfoo"). Tests must point
  `config.ALLOWLIST_PATH` at a temp file — a UI test once wrote `apt` onto
  the real allowlist by clicking the wrong button.
- **Background workflows cannot ask, so they cannot do** (`workflows.py`):
  workflow agents get SAFE_TOOLS (no browser — one shared Playwright page —
  and nothing dangerous) plus a deny-all approver. Monitoring is via
  workflow_status / workflow_log tools.
- **Jarvis may not touch his own control plane.** `config.is_face_origin()` —
  the browser and `fetch_page` refuse it, ahead of `allowed_hosts='*'`.
  Otherwise he could drive his own HUD and approve himself.
- **External accounts follow use-but-never-see** (Gmail, 2026-07-31, the
  template for every future integration): a *scoped OAuth refresh token* —
  never a password — lives in `~/.config/jarvis/google_token.json` (mode
  600, outside the repo), is loaded only inside `google_auth.py`, and goes
  into an Authorization header; the model only ever sees API responses.
  Consent is human-only (`jarvis auth google`, a CLI subcommand — never a
  tool, so the agent cannot initiate or widen its own access), scopes are
  minimal (gmail.readonly + gmail.send), the token is revocable at
  myaccount.google.com, and outward actions (`gmail_send`) are
  `dangerous=True` so every send needs the owner's approval. The token file
  is covered by all three secrets layers below.
- **Onshape follows the same template with a sharper write boundary**
  (2026-07-31): API keys — never the password — live in
  `~/.config/jarvis/onshape_keys.json` (600, all three secrets layers),
  pasted once via `jarvis auth onshape` (human-only) and revocable in
  Onshape under My account → Developer → API keys (NOT the dev portal —
  that page is OAuth-apps only now). CAD writes are confined
  *structurally*: every
  write URL is built from the one sandbox document pinned in that bundle,
  and no cad_ tool accepts a document id for a write target — so no CAD
  tool needs `dangerous=True`. Parts libraries are read-only sources.
  `tests/onshape_check.py` asserts both properties stay true.
- **`.env` / `.env.local` are unreadable** (`tools/secrets.py`, added
  2026-07-30 after a real leak — see below). Three layers: `read_file` refuses
  them by name; both shell tools refuse a command naming one, including a
  dotted glob (`cat .env*`) and *including approved* `run_command`, because
  approving a command is not consent to what it prints; and `dispatch()`
  scrubs every tool result before it becomes a `tool` message. Covered
  names: `.env`, `.env.local`, `google_token.json`, and `onshape_keys.json`
  (JSON values pulled by sensitive key names) — `.env.example` stays
  readable on purpose.

## Testing

- `jarvis bench` makes **real API calls and costs money** (~$0.004 for the full
  6-model sweep). Use `-t <task>` and a single model while iterating.
- Context-management tests are synthetic and free — no API, fully repeatable.
  Prefer that pattern for new logic.
- Browser smoke tests should use a **local HTTP server**, not a live site.
- `tests/secrets_check.py` — free synthetic checks for the `.env` protection,
  against a throwaway dir holding a fake key. Includes a replay of the actual
  leak (a recursive grep that never names `.env`). Run it after touching
  `secrets.py`, `dispatch()`, or either shell tool.
- `tests/gmail_check.py` — free synthetic checks for the Gmail integration:
  refresh-token exchange and caching against a fake transport, search/read/
  send API shapes with MIME round-trip, 401→refresh→retry-once, gmail_send
  registered dangerous with a denial never touching the network, and the
  token file refused by name/glob/read_file with values scrubbed. Run after
  touching `google_auth.py`, `tools/gmail.py`, or `secrets.py`.
- `tests/onshape_check.py` — free synthetic checks for the CAD integration:
  key-bundle load and document-URL parsing, inch/degree → meter/row-major
  matrix transforms and readback, every cad_ tool's request shape against a
  fake transport, library latest-version resolution + caching, the sandbox
  write pin (every write URL targets the pinned document), and the key
  bundle refused/scrubbed by all three secrets layers. Run after touching
  `onshape_auth.py`, `tools/onshape.py`, or `secrets.py`.
- `tests/permissions_check.py` — free checks for modes and the allowlist:
  gate ordering, prefix vs whole-tool matching, persistence without
  duplicates, mode "all" bypass, and the broker's ALWAYS path writing an
  entry. Run after touching `permissions.py` or `approvals.py`.
- `tests/skills_check.py` — free checks for skill tools (round-trip, bad
  names, starter skills parse) and the index: render/refresh/cap, plus
  agent-level injection against a faked `llm.chat` (present and per-turn
  fresh for skill-armed agents, absent for others, mid-session skills
  visible next turn).
- `tests/workflows_check.py` — free checks with a faked `llm.chat`:
  background lifecycle, status/log tools, deny-all approver, safe toolset,
  concurrency cap. Run after touching `workflows.py`.
- `tests/face/controls_check.py` — free server-level checks: /mute flips and
  broadcasts, /config reports mute+permissions, and /approve with
  always=true both unblocks the agent and persists the entry.
- `tests/face/approval_check.py` — free synthetic checks for the approval
  gate: the broker (approve/deny/timeout/no-window/replay/window-closed), the
  `/approve` route against a real server with a real SSE subscriber, that
  `dispatch()` neither runs a denied tool nor blocks an approved one, and that
  the face's origin is refused by the browser and web tools.
- `tests/face/hud_approval_check.py` — free end-to-end check of the card in
  the real `jarvis.html`, headless: SSE → card → click → the blocked agent
  thread wakes with the answer. Also covers Escape-denies, PTT being inert
  while a card is up, and timeout withdrawal. Run both after touching
  `approvals.py`, the face server, or the HUD.
- `tests/face/whiteboard_check.py` — free end-to-end check of the whiteboard
  with a stubbed designer: real page JS in headless Chromium (draw → export →
  POST /design → reply + preview iframe), shape tools via real pointer drags
  (rect/circle/line commit ops, Shift constrains, undo pops, letterbox
  coordinate math), the ATTACH toggle (sketch vs
  text-only), the route's mtime-diff file detection, the workshop origin with
  no-store, and `Agent.run_turn(images=…)` building a proper multimodal user
  message against a fake `llm.chat`. Run after touching `/design`, the
  whiteboard, or the workshop server.
- `tests/face/whiteboard_smoke.py` — real sketch → Luna → served design
  (~$0.002-0.02/run); scripted sketch for comparability, human grades the
  rendered PNG. First run 2026-07-31: 4 steps, $0.0024, honored both layout
  and the color annotation.
- `tests/face/hud_converse_check.py` — drives the real `jarvis.html`
  conversation path in headless Chromium (real API calls, ~$0.001). Exists
  because a client-side bug ("Fault: HTTP 200" — the error check matched
  "json" against the `application/x-ndjson` success type) sailed past the
  Python-client streaming test. Lesson: test the HUD's own JS, not just the
  routes it calls. Run after touching `/converse` or the HUD's converse().
- `tests/face/cancel_check.py` — **free** checks for mid-turn cancellation,
  with `llm.chat` stubbed so the cancel can be dropped at an exact point in
  the loop. The one that matters: cancelling while a tool is running must
  still return every tool result (invariant 3). Run it after touching
  `run_turn`.
- `tests/face/attach_check.py` — free checks for typed input + attachments
  on `/converse`: message assembly (speech+typed+files into one user
  message, @path resolution, images as multimodal parts, caps surfacing as
  notes), protected-file refusal and credential-value scrubbing, typed-only
  turns skipping STT, and the legacy raw-webm body still working. Run after
  touching `_assemble_turn`, `_converse`, or `run_turn(images=…)`.
- `tests/face/hud_input_check.py` — free headless checks of the input bar
  in the real `jarvis.html` (hud_state_check's puppet pattern): Enter sends
  the JSON envelope and the words render at send time, staged files ride
  the next send and clear after, empty Enter is inert, and Space typed in
  the box does not trigger push-to-talk.
- `tests/face/hud_state_check.py` — **free** checks for the HUD turn state
  machine, and the pattern to copy for anything else in the window: a
  *scripted* `/converse` and `/events` on `queue.Queue` puppet strings serve
  the real `jarvis.html`, so every phase transition is driven on cue with no
  API calls and no timing luck. Covers phase order, the mid-turn transcript,
  tool start/finish, the elapsed clock, and that a late SSE tool event cannot
  rewind a later phase.
- `tests/browser/math_drill_smoke.py` — headed end-to-end browser test: serves
  a local JS-rendered form wizard (`tests/browser/pages/math-drill/`) and has
  the agent complete it. Real API calls (~$0.001/run); the window stays open
  after the run so a human can grade via the page's Copy JSON button. Verify
  page changes free first with a headless Playwright click-through.
- `tests/browser/policy_check.py` — free synthetic checks for the internet
  policy: public-vs-private address matrix (IP literals, no DNS), resolver
  cases via a monkeypatched `getaddrinfo` (a public name resolving to
  loopback/LAN is refused), `fetch_page` refusals before the request and
  after a redirect, and a headless redirect off a pinned allowlist that must
  back out to about:blank. Run after touching `BrowserPolicy`,
  `config.is_lan_host`, or `fetch_page`.
- `tests/browser/thread_check.py` — free checks for invariant 7: after a
  browser action no event loop is parked on the caller (prompt_toolkit must
  still prompt — the chat-REPL crash), fresh threads can reuse the session
  (the face's request threads), and errors cross the thread boundary. Run
  after touching `Session._submit` or the browser lifecycle.
- `tests/browser/vocab_drill_check.py` — free synthetic checks for the vocab
  drill page: determinism (same seed ⇒ identical sequence), a full solve of
  all four question types, the idle detector, and the session timer. Run it
  after any change to `vocabbench/index.html`. It solves via the
  `window.__answerKey` test backdoor, which agents cannot reach (no JS-eval
  tool).

## Model facts (verified 2026-07-30 via the OpenRouter catalog)

| Model | Tool calling | Vision | Context |
|---|---|---|---|
| `openai/gpt-5.6-luna` | 8/8 | yes | 1.05M |
| `openai/gpt-oss-20b` | 8/8 | **no** | 131k |
| `qwen/qwen3.7-flash` | 8/8 | yes | 1M |
| `mistralai/mistral-nemo` | 6/8 (fails multi-step) | no | 131k |
| `meta-llama/llama-3.1-8b-instruct` | 6/8, flails | no | 131k |
| `google/gemma-3-12b-it` | 2/8 — emits ` ```tool_code ` text, not native calls | yes | 131k |

Two findings worth keeping:

- **Cheap per token ≠ cheap per task.** llama-3.1-8b is priced below Luna and
  cost 65% *more* on the bench, because wasted turns re-send the whole
  transcript. Optimize `$/completed task`.
- **Tool-calling support varies by provider**, not just by model. Gemma's
  behavior depended on which backend OpenRouter routed to.

## Decisions already made — don't relitigate

- **No framework.** See "What this is".
- **Did not fork Grok Build** (xAI's open-sourced Rust coding agent). Useful to
  *read*; wrong base — 845k lines of Rust, coding-agent-shaped, and inheriting
  it defeats the learning goal.
- **Will not use leaked Claude Code source.** Proprietary code obtained without
  authorization; the Agent SDK and public docs cover the same design ground.
- **Membean bench was declined.** Membean is teacher-assigned, engagement-
  monitored schoolwork; an agent completing it produces a false report to a
  teacher, and every eval run would be a real submission. Building
  **`vocab-bench`** instead — a local, seeded, deterministic replica of the same
  difficulty profile. That is also the better benchmark: repeatable, shareable,
  CI-able, no bans.
- **Model routing policy (2026-07-30).** Default all real work to
  `openai/gpt-5.6-luna`. Do not route personal data (chat, transcripts,
  compaction summaries) to Chinese-hosted models — the cheap tier moved
  qwen3.7-flash → gpt-oss-20b for exactly this reason. Multi-model
  comparisons remain opt-in by naming models explicitly on `jarvis bench`.
  If Luna hits a capability wall, escalate the orchestrator to GPT-5.6 Terra
  or Claude Sonnet 5.
- **Semantic/RAG memory deferred.** Memory is one markdown file per fact, pulled
  on demand via tools, so nothing is auto-injected. The upgrade ladder is
  substring → SQLite FTS5/BM25 → embeddings, and the tool interface
  (`memory_search(query) -> text`) is the contract, so swapping backends changes
  nothing else. Revisit when search visibly misses.

## Current state (2026-07-30)

Working: agent loop, tier routing, memory tools, file/shell tools with gating,
web search + page fetch, context management, browser control (Playwright,
headed via WSLg, snapshot + screenshot channels), tool-calling bench.

Browser control validated end-to-end 2026-07-30: Luna completed a randomized
4-question JS-rendered form wizard (`tests/browser/`) on snapshots alone —
21 steps, 27.8s, $0.0013, 4/4 correct, zero stale-ref or retry errors. The
snapshot→act→re-snapshot discipline held without prompting beyond the task
text. De-risks the vocab-bench browser loop.

**`vocab-bench` is built** (2026-07-30). `vocabbench/index.html` is the seeded
drill — timed session, idle-detector overlay, four JS-rendered question types
(multiple choice, fill-in-blank, matching, spelling), every random draw routed
through a `mulberry32(seed)` PRNG so one seed fixes the exact question
sequence for every model. `jarvis bench --family vocab` runs it with the real
browser and real agent loop (`jarvis/vocabbench.py`, headless by default,
`JARVIS_BROWSER_HEADLESS=0` to watch); scoring is read back from
`window.__vocabResults`, which the agent can neither see nor set.

First roster sweep (`vocab-snap`, seed 42, 2026-07-30): **Luna 6/6** (33 steps,
37.5s, $0.0023) and **qwen3.7-flash 6/6** (30 steps, 50.7s, $0.0021);
**gpt-oss-20b failed** — the trace shows it opened the page and then produced
no valid browser action for 16 straight rounds before giving up, despite being
8/8 on the canned tool bench. New finding to keep: **single-shot tool-call
success does not predict long-horizon browser loops** — the canned bench and
vocab-bench measure different capabilities.

**Snapshot vs vision, same seed, Luna (2026-07-30).** Vision mode is honest
now: `browser_screenshot` draws set-of-marks badges (the same refs snapshot
assigns, so clicking needs no text channel) and `vocab-vision` runs with no
`browser_snapshot` at all. Result — both modes 6/6, but vision took 39 steps
vs 33, 98s vs 38s, and $0.0117 vs $0.0023: **~5× the cost for equal
accuracy**. Confirms the design bet that text snapshots are the default
channel for web work; screenshots are for when layout or rendering actually
matters. Caveat: the vision run finished at 39 of 40 steps — raise
`max_steps` for vision tasks before longer sweeps.

**Secret-leak incident (2026-07-30).** Asked which provider serves Luna,
Jarvis ran an approved `grep -RIn 'provider|openrouter' ~/projects/Jarvis` and
printed the live `OPENROUTER_API_KEY` from `.env` into the transcript — so the
key went to OpenRouter as context and landed in `traces/` and
`.jarvis_history`. **Key rotated 2026-07-30; protection added** (*Safety
design*). The leaked key is dead but still sits verbatim in the older
`traces/` files and `.jarvis_history` — those predate the scrubber, which only
protects what is in `.env` now. The lesson worth keeping: **the
dangerous read was the one that never named the file.** A path denylist alone
would have waved this through, which is why the scrub sits at `dispatch()` —
the last point before a string becomes a `tool` message — and not in
`read_file`. Layers 1–2 are ergonomics (a clear refusal the model can act on);
layer 3 is the real protection, and it is an accident backstop, not a sandbox:
a model that wanted to defeat it could still base64 a value past it. The
actual boundary would be filesystem permissions on the key.

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
(persisted in localStorage); hearing "jarvis" chimes and starts a recording
that self-stops on ~1.4s of silence. Saying "Jarvis" while he speaks barges
in. `talk.html` was retired — it spoke the old single-JSON `/converse`
shape.

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
is trusted UI, so never `innerHTML` there.

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

Worst-case latency is one in-flight model call: the OpenRouter request cannot
be aborted, so the loop stops at the next step boundary. In practice the owner
is still talking when it unwinds.

**Gmail shipped (2026-07-31)** — the first real integration, and the
template for the rest: `jarvis auth google <client.json>` runs the one-time
OAuth consent (PKCE + state, loopback redirect, human-only CLI command),
writes the scoped refresh token to `~/.config/jarvis/google_token.json`
(600), and `tools/gmail.py` exposes `gmail_search` / `gmail_read` /
`gmail_send` — send is `dangerous=True`, so it hits the CLI y/N or the HUD
authorization card. `google_auth.access_token()` refreshes behind a cache;
tools retry once on 401. Token bundle covered by all three secrets layers
(name, glob, scrub — see Safety design). Free synthetic suite:
`tests/gmail_check.py`. Setup steps live in `jarvis auth google` (no
argument). Google-side gotchas: the OAuth app must be **published to
production** or refresh tokens die after 7 days; a re-consent needs the old
grant revoked at myaccount.google.com/permissions or Google returns no new
refresh token. Validated end-to-end by the owner 2026-07-31: a gated send
(draft → CLI y/N showing exact to/subject/body → delivered) and a 7-day
inbox summarization (parallel gmail_reads, $0.0017). Note that mailbox
content now flows through the orchestrator tier, so the model routing
policy applies to it.

**Internet access opened (2026-07-31).** `web_search`/`fetch_page` were
already unrestricted; the browser was the localhost-only piece. The default
allowlist is now `["localhost", "127.0.0.1", "*"]` where `'*'` means the
*public* internet: `config.is_lan_host()` refuses private/CGNAT/link-local
space and unresolvable names, judged by what a hostname resolves to (so
`lvh.me` → 127.0.0.1 is not a route to local services or the face). Enforced
on the requested URL and re-checked after goto/click/submit
(`_guard_landing()`); `fetch_page` gained the same LAN block plus a
post-redirect check — it had been wide open to the LAN. System prompt gained
open-web conduct rules (no credentials or personal data into pages, no
outward-facing submissions unless asked). Verified live (example.com loads
and snapshots; 192.168.1.1 refused with a clear message) and synthetically
(`tests/browser/policy_check.py`); approval + secrets suites still pass.

**Browser moved to its own thread (2026-07-31).** The first real internet use
in `jarvis chat` crashed the REPL after the turn: Playwright's sync API parks
a running asyncio loop on the thread that starts it, and prompt_toolkit
refuses to prompt on a thread with a live loop ("asyncio.run() cannot be
called from a running event loop"). Fix is invariant 7: a dedicated
`jarvis-browser` thread owns all Playwright work, which also makes browser
tools safe from the face's per-request handler threads (they would otherwise
break on the second voice turn that browsed). External page access goes
through `Session.eval_js()` now; chat/ask/face stop the session on exit.
Verified by `tests/browser/thread_check.py` and a live pty'd chat run
(browse example.com → answer → prompt again → clean exit, no EPIPE).

**Design board shipped (2026-07-31).** `whiteboard.html` on the face server:
paper canvas (pen, straight line, rect, ellipse — Shift constrains to 45°,
square, circle — text, eraser, undo; ops-list model, WYSIWYG PNG export),
prompt bar with an ATTACH SKETCH toggle, live tool ticker, and a preview
panel. POST `/design` runs a separate persistent designer Agent
(`DESIGNER_SYSTEM`, files+browser+web tools, max_steps 24, vision via the new
`run_turn(images=…)` — owner-supplied PNGs ride the user message; context
eviction handles them like any image). Output files are found by mtime diff
under `designs/` and served by the **workshop server on port 8403** — a
separate origin so agent-written pages can never reach `/approve`, and
browsable by Jarvis so he can screenshot his own work to self-check. Open it
with `jarvis face whiteboard.html`, or browse localhost:8402/whiteboard.html
while the face runs. Smoke-validated (see Testing): sketch annotations
override drawing colors, as prompted. First real-owner run caught a bug the
smoke could not: **paths in agent prompts must be absolute** — the prompt
said `designs/…`, `write_file` resolves against $CWD, and the owner launches
`jarvis face` from `~`, so output landed in `~/designs` while the workshop
served the repo's. (The smoke passed because tests run from the repo root —
a CWD-dependent bug needs a CWD-varied test, or no CWD dependence at all.)
The prompt now carries `config.DESIGNS_DIR` verbatim plus the exact preview
URL root; `whiteboard_check.py` asserts both. Known niggle: the whiteboard's SSE
subscription counts as a "viewer" for the approval broker but the page has
no card UI — moot today (designer tools are all safe), fix if the designer
ever gains a dangerous tool.

**Skills, workflows, permission modes, mute (2026-07-31).** Skills follow the
memory pattern — `skills/<name>.md` (frontmatter description + instructions),
pulled on demand via skill_list/skill_read, recordable via skill_write, four
starters checked in (email-triage, morning-briefing, skill-creator — the
meta-skill that interviews the owner and drafts trigger descriptions — and
whiteboard, which opens the board via `run_command jarvis face
whiteboard.html` since the face origin is refused to his own browser, and
closes it via `whiteboard_close`). **Window close is opt-in by page**
(`tools/whiteboardctl.py`, voicectl pattern): the tool broadcasts SSE
`wb_close`; whiteboard.html calls `window.close()` (app-mode windows have a
single history entry, so it is honored — with a blank-page fallback), and
jarvis.html deliberately ignores the event: the HUD is the approval
surface, and the agent must not hold a lever that closes the window gating
him. `whiteboard_check.py` asserts both sides. **Two-tier index
(Claude Code's design): `skills.index()` — every skill's name + trigger
description — is rebuilt into `messages[0]` at the top of each `run_turn`,
but only for agents whose toolset includes `skill_read`** (designer/benches
never see references to tools they lack). Bodies still load only via
skill_read. Descriptions are written as triggers ("Use when the owner …");
the index is cached on (mtime_ns, size) per file — mtime alone misses
quick same-tick rewrites — and skill_write busts it directly. Capped at 30
skills / 2k chars with a skill_list pointer. Rewriting messages[0] is
invariant-safe: pruning/compaction never touch it. Workflows: workflow_start/status/log run a task
on a background agent with safe tools and a deny-all approver (see Safety
design); in-memory registry, restart forgets them. Permissions: see Safety
design — ask (default) / persistent allowlist ('a' in CLI, ALWAYS card
button in the HUD, `approved-always` in the decision log) /
`--dangerously-skip-permissions` (process-only). Mute: MUTE row in the HUD
SYSTEMS panel, `set_voice_mute` tool for "jarvis, mute yourself", one SSE
broadcast keeps them in sync; muted turns skip TTS synthesis entirely (text
still renders). HUD SYSTEMS also shows PERMISSIONS state (SKIP ⚠ in red
under the flag).

**Typed input + attachments in the HUD (2026-07-31).** An input bar at the
bottom of jarvis.html: type + Enter sends a text turn (same cut-off rules
as push-to-talk — barge-in while speaking, cancel while thinking, inert
while an authorization card is up); files stage as removable chips via the
picker, drag-drop, or paste; `@path` in the text attaches server-side
files. The staging contract: whatever is staged when a turn goes out —
typed *or spoken* — rides that turn, so you can stage context and just
start talking. `/converse` now also takes an application/json envelope
`{audio_b64?, audio_mime?, text?, attachments: [{name, mime, data_b64}]}`
(the legacy raw-webm body still works); `_assemble_turn` folds it all into
one user message — images become multimodal parts via `run_turn(images=…)`,
which now accepts `{b64, mime}` dicts alongside plain PNG strings, and text
files inline as fenced blocks. Limits: 8 files/turn, 4MB/file, 100k chars
inlined — every refusal becomes a bracketed note in the message, never a
silent drop. Secrets rules carry over: protected credential files are
refused at @path by name, and inlined text is scrubbed. Typing Space must
not trigger push-to-talk — the input stops propagation, and
hud_input_check pins that. Typed words hit the COMMS log at send time;
`meta.heard` is non-empty for typed turns so the client's no-signal path
stays voice-only.

**Voice latency work (2026-07-31).** The owner heard a multi-second gap
between reply text appearing and speech starting. Root cause: `llm.py` used
bare `httpx.post`, so *every* OpenRouter call — each agent step, STT, and
each TTS sentence — opened a fresh TCP+TLS connection. Measured: cold TTS
call 3013ms vs 377-685ms through a keep-alive pool. Three fixes: (1) one
module-level `httpx.Client` shared by chat/speech/transcribe — the chat call
that produced the reply now leaves a warm connection for the TTS that speaks
it; (2) `_sentences()` clamps the first chunk to ≤90 chars at a clause
boundary (synthesis time scales with length; 196→83 chars measured 1217→
491ms) — `tests/face/sentences_check.py` guards clamping + lossless rejoin;
(3) TTS chunks synthesize two-in-flight via a ThreadPoolExecutor and stream
in order, with cancel_futures on barge-in so abandoned speech stops costing
money. **And (4), the one that actually mattered when the gap persisted:
provider routing.** Kokoro is served by DeepInfra and Together; OpenRouter's
default route (DeepInfra) was serving identical requests in 2.8-23.7s while
Together served them in 0.5-1.8s. `config.TTS_PROVIDER` (default Together)
now sets a routing preference in `llm.speech()`, fallbacks allowed. Wired
path verified: 5 consecutive turns at 451-583ms. Same lesson as the bench
finding: **the model is not the product — the provider serving it is.**
Re-probe per provider (payload `{"provider": {"order": [...],
"allow_fallbacks": false}}`) before blaming the model or the code.
**Epilogue: the stalls persisted intermittently on both providers via
OpenRouter while the owner measured both providers fast directly and
`tests/net_probe.py` showed the local network clean — so the tail lived in
OpenRouter's audio proxy path, and TTS went local (2026-07-31): kokoro-onnx
on CPU, same model, same bm_george voice, measured 308-351ms per sentence,
flat.** `voice.tts()` branches on `config.TTS_BACKEND` ("local" default,
falls back to cloud with a one-time warning if deps/models missing); local
emits WAV, cloud MP3 — consumers sniff (`RIFF`), never trust a label. Model
files: ~/.cache/jarvis-tts/{kokoro-v1.0.onnx,voices-v1.0.bin} (github
thewh1teagle/kokoro-onnx releases, ~340MB); dep via `uv pip install -e
.[voice]`. The face pre-warms the model at startup (~3s ONNX load, off the
critical path). `tests/voice_local_check.py` is the free suite. STT remains
cloud (parakeet); local whisper stays the next swap if it ever needs to be.

**Onshape CAD shipped (2026-07-31), live validation pending.** The second
use-but-never-see integration (see Safety design). `jarvis auth onshape`
pastes API keys created under My account → Developer → API keys (Basic
auth on the wire — no HMAC needed for first-party use; individual accounts
are capped at 2 active keys), then creates or adopts the one sandbox
document Jarvis may write to and records read-only parts-library documents.
`tools/onshape.py`: cad_status / cad_find_part / cad_create_assembly /
cad_insert / cad_assembly / cad_move / cad_delete / cad_render (shaded PNG
via ToolResult.image_b64 — the model's eyes; text readback in inches is
ground truth). API facts verified against the docs, worth keeping:
cross-document inserts must reference a *version* of the source document,
so cad_find_part resolves each library's latest version and errors usefully
on never-versioned docs; insert-with-placement is a single call
(`transformedinstances`: 16-float row-major matrix, meters, absolute in
root-assembly coords); `shadedviews` requires a 12-float view matrix — it
*rejects* named views despite what the generated client docs say, so
cad_render carries a `_VIEWS` name→matrix table — and `pixelSize=0` means
zoom-to-fit; `occurrencetransforms` takes the flat
`{occurrences, transform, isRelative}` body (the `transformDefinitions`
wrapper belongs to `/modify` and 400s). The tools speak inches/degrees
(VEX lives on a 1/2" grid) and convert internally.

**Validated live 2026-07-31** on the owner's account: sandbox created
(public — free plan), and the 14 official VEX V5 library documents
auto-discovered and written into the bundle via the documents-search API
(`q='description:"Official VEX V5 Library"'`, `filter=4` = public, then
keep only owner == "Onshape" — a name search surfaces user copies first
and misses the real ones, which are matched by *description*). Full
pipeline exercised tool-by-tool: find "c-channel" across 14 libraries →
create assembly → insert two 35-hole aluminum c-channels → readback →
move → iso/top renders. The renders earned their keep immediately: a 12"
Y-offset overlapped the 17.5"-long rails (channels run lengthwise along Y
in their local frame), which the readback stated and the render made
obvious; one cad_move fixed it. Still to build: mates via mate
connectors, configurable cut-to-length (the "(Configurable)" library
docs), whiteboard→CAD wiring, and a cad-bench scored by assembly
readback. API-key note: keys live under My account → Developer → API
keys (the dev-portal URL is OAuth-apps only now); individual accounts cap
at 2 active keys.

Later: real integrations (calendar/email), scheduled proactive runs, and a
Windows-side bridge (`windows/bridge.py`) for desktop GUI automation — only
needed for non-browser apps, since Playwright already runs natively in WSL.
