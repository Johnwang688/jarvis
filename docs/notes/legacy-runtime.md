# Legacy Runtime — preserved notes

Verbatim from `CLAUDE.md` at `5ef4e545432ff632fa47343743fd4b075828aa46`.
Archived instruction/history snapshot, not a new deployment receipt.
Both current obligations and dated/superseded findings appear here.
Moving a rule does not revoke it. Start with [the working guide](../../CLAUDE.md),
read this area's complete rules before changing a boundary, and resolve older
conflicts using the latest explicit owner decision and current implementation.
Paths in preserved prose are repository-relative. Source labels refer to the
original file. Keep payloads verbatim; later notes belong outside the markers.

## Original CLAUDE.md lines 7936–8065

<!-- claude-source:007936-008065:begin -->
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
critical path). `tests/voice_local_check.py` is the free suite — synthesis
shape and speed, the cloud fallback, and (2026-08-06) **which** voice speaks:
the avatar's, its every degradation path, and the voice→language map. It
pins `AVATAR_ENV` to the default first, because otherwise the owner's saved
avatar decides what the suite asserts. STT remains cloud (parakeet); local
whisper stays the next swap if it ever needs to be.

<!-- claude-source:007936-008065:end -->

## Original CLAUDE.md lines 8534–8612

<!-- claude-source:008534-008612:begin -->
**`jarvis daemon` shipped (2026-08-03) — away-agent phase 1** (the plan:
`docs/away-agent-roadmap.md`; the Discord core moved to `discord_agent.py`
so face and daemon share one implementation of the spoken-turns-never-
authorize rule). The daemon is the always-on half of the face with the
window cut away: Discord gateway + an ApprovalBroker whose `viewers` is
pinned to 0, so every dangerous-tool question DMs the owner or denies
`nowhere-to-ask`. No HUD, no workshop, no browser window, no TTS pre-warm
(voice notes load Kokoro lazily). **Exactly one process owns the gateway:**
the daemon refuses to start while a face is serving (double-IDENTIFY would
double every reply), and a face started while the daemon runs skips the
gateway AND calls `APPROVALS.detach_remote()` — DM answers land in the
*daemon's* broker, so a face-made DM could only ever time out; its
questions stay on the card, correct for the at-the-desk surface. The
read-only `/status` endpoint on `DAEMON_PORT` (8405) is both the
single-instance lock and how the face detects the daemon. Provisioning is
human-only: `jarvis daemon install` prints the systemd user unit
(`WorkingDirectory=%h` — tool-relative paths must not resolve against `/`),
the `loginctl enable-linger` step, and the WSL keepalive options (Task
Scheduler `wsl --exec sleep infinity`, or `.wslconfig` `vmIdleTimeout=-1`);
it writes nothing. `tests/daemon_check.py` is the free suite (responder
approval isolation, nowhere-to-ask/timeout/detach denial paths, health
lock + single instance, refusing to start over a face, shutdown releasing
blocked waiters). Still pending from phase 1's exit gate: the 72-hour soak
under systemd with real sleep/wake cycles.

**Background goals shipped (2026-08-03) — away-agent phase 2.** A goal is a
loop of turns, not a turn: `goals.py` is the durable store
(`~/.local/share/jarvis/goals/<id>/` — `goal.json` plus an append-only
`journal.jsonl` written by the *runner*, never the model, so "what did you
do while I was gone" is answered from disk), and `goalrunner.py` is one
serial worker thread — one goal at a time on purpose (singleton browser,
predictable spend) — running slices of `run_turn` on the goal's own
session, which is what makes daemon-restart resume free. Between slices it
checks shutdown, cancel, then ceilings (dollars / hours / slices, defaults
in config, per-goal overrides on `jarvis goal`); any ceiling parks the goal
with a DM saying what was spent. **Ending is a tool call**: the goal agent
carries `goal_report(done|blocked)` (`tools/goalctl.py`, armed only inside
a slice — the step-budget lesson: state outside the transcript gets
confabulated, so completion must be an act *in* it). **Steering is barge-in
for goals**: a `steer:` DM queues text, sets the interrupt Event behind the
agent's `should_stop` (turn ends whole at the next step boundary, invariant
3), and the text arrives as the next slice's `[owner steering]` message.
Progress DMs: start, terminal states, and an interval digest
(`GOAL_UPDATE_MINUTES`, default 10) with slices/spend/elapsed/last
activity/the live plan slot — assembled from the runner's records, never
asked of the model. DM verbs (`goal: …`, `steer:`/`redirect:`,
`goal status`, `goal cancel`) are parsed in the daemon's `_route` ahead of
the conversation agent, **typed messages only** — a misheard voice note
must not steer or cancel, same isolation as approvals. Intake: `goal:` DM
or `jarvis goal "…" [--dollars --hours --slices]`; `jarvis goals` lists.
The goal toolset is the full registry minus desktop (foreground-stealing is
a desk feature) and window controls; the approver is the daemon broker's
remote gate, so dangerous calls DM the owner exactly as at the desk, and a
deny is handled by the model (park-and-continue and per-goal scopes are
phase 3). Free suite: `tests/goals_check.py`; `daemon_check` now pins
GOALS_DIR to a temp dir for the daemon's whole lifetime — a test daemon
must never pick up real queued goals with a stubbed approval channel.

**Goal protocol upgrades from the first live runs (2026-08-05), all
committed same-day:** slice 0 is **planning only** (plan DMed to the owner
before implementation; steer within a step); the first `goal_report(done)`
buys a **verification slice**, not the exit — and a verify slice that keeps
working instead of confirming **re-arms the gate**, because the first live
run reported done after phase 1 of 7 and the real completion must not sail
through on the spent pass. `goal_report` is documented as ENTIRE-goal-only.
`goal resume` requeues the newest parked goal, and `steer:` with nothing
running targets the newest parked/queued goal — parked requeues immediately
with the steering as its first resumed message (one DM unblocks and aims).
Live validation on a real 7-phase VEX strategy goal (11 slices, $0.42,
~35min, daemon killed and restarted mid-goal with clean session resume —
phase 2's exit gate): the verify pass caught real defects both times it ran
(a missed 8-point scoring rule; stale docs + malformed coordinates), the
model used plan_write throughout (PENDING #2 answered for goal runs),
delegated verification reads to run_subagent, and honestly blocked on data
the game db doesn't contain rather than inventing it. Lesson worth keeping:
**write acceptance criteria into the goal statement** — the first VEX run
produced a complete-looking skeleton in one slice because nothing pinned
what "done" meant; the surgical follow-up produced correct code.

<!-- claude-source:008534-008612:end -->

## Original CLAUDE.md lines 8732–8943

<!-- claude-source:008732-008943:begin -->
**Session memory (2026-07-31).** Conversations now survive a restart, and
Jarvis can look into the ones before this one. `sessions.py` keeps three
things per session under `~/.local/share/jarvis/sessions/<id>/` — outside the
repo, unlike memory/ and skills/, because transcripts are bulk, personal, and
rewritten every turn:

- `messages.json` — the live transcript *as the context manager left it*, so
  resuming inherits the pruned history rather than re-inflating it. Live
  image payloads are swapped for the eviction placeholder on the way to disk
  (a screenshot is 1.5MB of base64 no future turn will look at), and
  `messages[0]` is never persisted — the system message is rebuilt on load, so
  a resumed conversation gets today's prompt and today's indexes.
- `log.jsonl` — append-only user/reply text. This is the durable record:
  compaction *deletes* from the transcript, and the log still has it. It is
  what `session_search` greps and what a summary is built from.
- `meta.json` — title, timestamps, turns, cost, and the cached summary.

`Agent(session=…)` is the whole integration: it restores on construction and
calls `session.record()` after every `run_turn`, so all three surfaces persist
for free (a save that fails is caught — persistence must never break a turn).
A **cancelled turn is saved too**: the transcript really does end at that user
message, which is what makes an interrupted conversation immediately
reusable.

Cross-session recall follows the skills two-tier design: recent session
**titles** are rebuilt into `messages[0]` every turn (only for agents armed
with `session_summary` — same rule as skills), and the content of one loads
only when Jarvis calls a tool. `session_summary` is the "inject that
conversation" path — its result lands in the transcript as a tool message,
which is what injection *is* in a chat loop; the summary is cheap-tier,
generated on demand and cached until the session gains a turn.
`session_search` greps every log, `session_read` returns exact wording. All
four are read-only, none is dangerous, and all four are in
`workflows.SAFE_TOOLS`.

Decisions worth not relitigating:

- **New session by default, resume explicitly** (`jarvis chat -c` / `-r <id>`,
  `jarvis face -c`, or the SESSION row in the HUD). Silently resuming is how
  you end up talking into a transcript you have forgotten. Discord is the one
  exception — it continues its own last session, because it is the
  away-from-desk channel with no UI out there to pick one.
- **Jarvis can read sessions but not switch them.** Which conversation is live
  is the owner's choice; a tool that swapped the transcript mid-turn would
  also have to decide where its own tool result belongs.
- **A session with nothing said in it never touches disk** (`sessions.new()`
  builds the object; `record()` creates the files). Every chat invocation and
  every window launch mints one, and empty directories would flood the index
  and the picker with conversations that never happened.
- **The titler and summarizer wrap their input in delimiters and label it
  data.** A conversation is full of imperatives, and the first version of the
  title prompt produced the title "Acknowledged" — the cheap tier answered the
  message instead of describing it. Related: gpt-oss-20b is a reasoning model
  and returns *empty content* if the token budget is spent thinking, so a
  6-word title needs `max_tokens=200`, not 24.

Verified live: two CLI turns across a restart (`-c` recalled a codeword from
the saved transcript), and a third, fresh session that answered "what codeword
did I give you earlier" by calling `session_search` off the injected index and
citing both session ids.

**Long-horizon work, part 1 (2026-08-01).** Three changes aimed at the same
failure — a run that goes long enough to forget what it was doing.

- **The cut-point orphan bug** (invariant 1) — found by inspection, reproduced,
  fixed, and now covered by the `context.py` test suite that this file had been
  claiming existed. Worth internalizing: it lived in a code path that *only*
  executes past 90k tokens by default, so every short test in the repo ran straight past
  it. Long-horizon code needs long-horizon tests.
- **The working plan** (`tools/plan.py`). One tool, `plan_write`, holding a
  markdown checklist rewritten whole each time. It renders into `messages[0]`,
  which nothing in `context.py` touches, so it is the one part of a long run
  that cannot be pruned away — and it is re-rendered every *step*, not every
  turn. The skills index proved the mechanism; this reuses it. Wired into the
  main agent (all tools), workflows, and the designer.
- **Sub-agents** (`tools/subagent.py`). `delegate()` was the cost lever; this
  is the *context* lever, which is a different problem. Every tool result a run
  produces lives in the orchestrator's transcript forever — fetch four pages to
  answer one question and 40k tokens of page dump outlive the answer by the
  whole session. `run_subagent` does the job on its own transcript and returns
  only its final text. Safety in "Safety design"; it is synchronous, which is
  what lets it hold the browser when a background workflow cannot.

Not done, in the order I would do them next (the full list came out of a review
on 2026-08-01; **spill-don't-drop truncation and real `prompt_tokens` were done
2026-08-09**, and **step-budget awareness plus a resumable handoff 2026-08-12**
— see *Harness round 2* below and *Hitting the wall is survivable* above):
repetition detection (the gpt-oss-20b vocab-bench
failure — 16 rounds of no valid action — is invisible to the loop today);
durable workflow journals; and ~~**`long-bench`**~~ — **done 2026-08-15**, see
*long-bench* below. Repetition detection is the last one that matters, and
long-bench now reports a `redundant_calls` count, so the signal exists before
the loop acts on it.

**Harness round 2 (2026-08-09) — the gaps a Claude Code comparison exposed.**
Six defects, all found by reading Jarvis's loop against a harness built for the
same job, all fixed together. All 42 free suites pass. (`tests/browser/
audio_check.py` matches the `*_check.py` glob but is **not** one — it opens the
HUD window and serves until Ctrl-C, so it hangs a sweep by design.)

- **There was no edit primitive.** `write_file` took the whole file, so changing
  three lines in a long one cost the model the entire file — and models drop
  content when re-emitting at length, which the read-before-write stamp cannot
  catch (it detects *staleness*, never *truncation*). `edit_file` does anchored
  replacement: exact match, unique unless `replace_all`, refuses ambiguity
  rather than guessing. **It honours SELF_PROTECTED, and that is the part to
  never lose** — a one-line replacement in `permissions.py` disarms the gate as
  thoroughly as overwriting it and looks like far less, so any future write
  tool has to be added to `self_improve_check.py` too.
- **`read_file` was capped at 40k characters with no way to ask for the rest.**
  CLAUDE.md is 116KB, so `skills/self-improve.md`'s first instruction ("read
  CLAUDE.md first") had been reading 34% of the file and reporting no problem.
  Reads are paged now (`offset`/`limit`, footer names where to resume) and
  line-numbered. The numbering has a matching hazard in each direction, and
  both are handled: `edit_file` retries once with the prefixes stripped when a
  quoted excerpt fails to match, and `write_file` strips them when a whole-file
  rewrite carries them back in — the strip only fires on read_file's own
  6-wide right-aligned field, so a TSV counting 1, 2, 3 survives.
- **Search was unbounded.** `run_readonly` allows grep but forbids pipes, so
  there was no `| head`: the model took the whole dump and it lived in the
  transcript forever. `grep_files` bounds its own output (`mode` =
  content/files/count, `max_results`, glob, context lines), ripgrep with a
  pure-Python fallback that the suite exercises by forcing it. It lives in
  `tools/search.py` rather than `files.py` on purpose — SELF_PROTECTED exists
  to freeze the *write* guard, and freezing a read-only search tool beside it
  buys nothing.
- **Tool calls ran one at a time.** Invariant 3 exists to keep models emitting
  parallel calls, and the loop then executed them serially — so the only thing
  parallelism bought was fewer round trips. Now consecutive parallel-safe calls
  run together (measured: 3×0.3s tools in 0.30s). See invariants 3 and 8 for
  the two things that make it safe: call-order results, and one fresh
  `copy_context()` per worker.
- **`finish_reason` was captured and never read.** `llm.Reply` has carried it
  since the beginning; nothing in the codebase looked at it, so a reply the
  provider cut off at `max_tokens` was appended and returned as the finished
  answer — a sentence stopping mid-word, indistinguishable from a completed
  one. This is the **same bug as the invisible step budget**, and the third
  time that shape has appeared: *a state the harness can produce that the model
  cannot see is a state the model will confabulate around.* The loop now flags
  it (`Turn.truncated`), continues up to `MAX_CONTINUATIONS` times joining the
  halves, and says `[cut off at the token limit]` if it still cannot finish. A
  cut that landed mid-tool-call gets a note **after** the results, never
  before — invariant 3 again. `max_tokens` also went 4096 → 8192
  (`config.MAX_TOKENS`): 4096 is ~16k characters, which any substantial
  whole-file write exceeded.
- **Compaction was judged on chars÷4.** The estimate counts no tool schemas at
  all, so it reads low by thousands on a full toolset and the threshold did not
  mean what it said. `context.TokenMeter` keeps the provider's real
  `prompt_tokens` for the measured prefix and estimates only what was appended
  since — measured immediately after `llm.chat` returns, when `self.messages`
  is still exactly the request. Every measurement is provisional, so
  `discount()` covers in-place rewrites (eviction, truncation) and
  `invalidate()` covers compaction, which deletes messages the measurement
  counted and has no honest adjustment.
- **Truncation was the one context operation with no way back.** Eviction
  leaves a placeholder, compaction leaves a summary; a cut result left nothing,
  so a page fetched at step 4 was gone by step 20. It now spills the full body
  to `config.SPILL_DIR` (content-hashed, so identical results share a file and
  re-truncating is idempotent) and leaves a `read_file`-able pointer. The
  pointer goes *before* the `TRUNCATED` marker, because that suffix is how the
  next pass recognises its own work. A spill that cannot be written degrades to
  a plain cut — it is a bonus, never a reason for a turn to fail.

**Follow-ups closed 2026-08-10**, each one a smaller version of a lesson
already in this file:

- **A compaction summary that overran was silently cut.** `_summarize` had its
  own `max_tokens` and never looked at `finish_reason` — the exact bug the loop
  had just been fixed for, one function away. It matters more here than
  anywhere else: the summary *becomes the record*, and the sections it loses
  are the last ones, which is where OPEN and FILES live. It now re-asks once,
  much tighter, and if that still overruns it says so inside the summary.
- **The goal dollar ceiling is checked mid-turn.** It was checked only between
  slices, which assumed a slice costs roughly one slice. `run_fleet` broke that
  (six children, each with its own step budget, inside one turn) but the
  assumption was always thin — a 40-step turn that goes in circles overspends
  the same way, slower. `Agent` emits an `on_event("cost", …)` per step and the
  goal runner sets its interrupt, so the turn ends whole at the next step
  boundary and the existing between-slices check parks it.
- **`chmod`/`chown` narrowed.** Still allowed for ordinary work, but a
  credential directory (`~/.ssh`, `.gnupg`, `.aws`, the jarvis config dir) or a
  world-writable mode falls to ASK. ssh *fails closed* on loose permissions, so
  the damage shows up later and somewhere else.
- **Two honesty corrections.** `command_review`'s docstring now states the
  TOCTOU gap plainly — it fetches the URL and then `curl` fetches it again, so
  the review describes *a* script from that URL, not necessarily the one that
  runs; that is why a trusted host is also required. And `rules.py` no longer
  implies DENY is a boundary: it is token matching over a shell line, `rm -rf
  "/"` falls through to ASK, and the real boundary is still filesystem
  permissions.

Also: `run_subagent` was pinned to the orchestrator tier **by omission** (it
constructed its child with no `model`), now `config.TIERS["subagent"]`,
defaulting to the same model so behaviour is unchanged but movable.

Deliberately **not** done in this round, because they are decisions rather than
defects and are the owner's to make:

- ~~Permission rule matchers.~~ **Done 2026-08-09** — see *Command rules* in
  Safety design. Still not done: **hooks**, i.e. running the owner's own
  programs at PreToolUse/PostToolUse. The rules engine covers the cases hooks
  were wanted for here; a general hook runner is a bigger idea and wants its
  own conversation.
- ~~A typed sub-agent fleet.~~ **Done 2026-08-09** — see *Sub-agents are typed*
  in Safety design. The known blocker was handled rather than dodged: the
  browser type is `concurrent_safe=False`, so the fleet serialises exactly the
  children that share the Playwright singleton and runs everything else
  together.
- ~~Moving the durable slot off `messages[0]`.~~ **Done 2026-08-09** — see
  invariant 7. The volatile blocks ride a rebuilt-every-step block at the tail
  now, so a `plan_write` no longer invalidates the whole prefix cache.

<!-- claude-source:008732-008943:end -->

## Original CLAUDE.md lines 9287–9312

<!-- claude-source:009287-009312:begin -->
**Attended background tasks shipped (2026-08-20)** — the conversation agent
stays a responsive chat thread and hands real work to background "task" agents
whose approvals reach the owner. Details and safety reasoning in *Safety
design* ("Attended background tasks can ask"); mechanics: `tasks.py` (the
workflows.py shape with the captured approver and a `cancel` Event),
`tools/tasks.py` (task_start / task_status / task_log / task_cancel — the
start tool captures `runtime.approver()`), `runtime.origin()` (the
attribution ContextVar the broker reads at request time), an origin line on
the HUD card and the Discord DM, task lifecycle SSE (`tasks.set_notify`, the
face broadcasts it into the OPERATIONS ticker), and a `## Background tasks`
section in the working-context block for task-armed agents — the
confabulation lesson applied: task status the surface can see is status the
transcript must carry too. Decisions made with the owner, recorded so they
are not relitigated: **tasks inherit the foreground gate whole** (allowlist +
ALLOW rules auto-run off-screen; wrapping the approver to force asks would
both strip the human-backed flag and teach approve-without-reading), and
**both verbs stay** — `task_start` is the default background delegation,
`workflow_start` the never-interrupt variant (agent-bench's `orchestrate`
pins its own toolset, so its multiagent rating is unaffected). The delegation
policy lives in the `config.py` system prompt: inline for
quick/conversational/interactive work, `task_start` for self-contained jobs
with a clear deliverable, `run_subagent` when the answer is needed before
replying, `workflow_start` only when the owner must not be pinged.
`tests/tasks_check.py` is the free suite (16 suites re-run green after the
change, including every approval, workflow, goal, daemon and HUD suite).

<!-- claude-source:009287-009312:end -->
