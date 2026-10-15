# Providers Runtime — preserved notes

Verbatim from `CLAUDE.md` at `5ef4e545432ff632fa47343743fd4b075828aa46`.
Archived instruction/history snapshot, not a new deployment receipt.
Both current obligations and dated/superseded findings appear here.
Moving a rule does not revoke it. Start with [the working guide](../../CLAUDE.md),
read this area's complete rules before changing a boundary, and resolve older
conflicts using the latest explicit owner decision and current implementation.
Paths in preserved prose are repository-relative. Source labels refer to the
original file. Keep payloads verbatim; later notes belong outside the markers.

## Original CLAUDE.md lines 25–99

<!-- claude-source:000025-000099:begin -->
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
`JARVIS_CLAUDE_CLI` (absolute paths only — a relative one would be spawned in
the task worktree, outside the hook), else the first real `claude` on an
absolute PATH entry not under `/mnt/`, else `~/.local/bin/claude`, else the
SDK's bundled CLI with a warning, re-searched every 60 s — because unset, the
SDK runs its own lagging bundled copy, which refused a newer model live on
2026-10-08; never pin the version, it self-updates: `health()` checks a
floor, `CLAUDE_MIN` = 2.1.280 (the default model's requirement, so the bundled
2.1.273 reads unhealthy), and only warns once per process
past `CLAUDE_VERIFIED`, with `JARVIS_CLAUDE_STRICT=1` restoring the old
major.minor match, and a failed `--version` probe is retried after 60 s
rather than cached),
the Codex provider over the app-server (WP4 — lifted from the trading firm's
transport; task `auto_review` has design R8's callback limit, while owner
chats route approvals to native `user` review; **a version floor,
not a pin** since 2026-10-08 — `providers/codex_cli.py` runs codex ≥ 0.153.4,
warns once above the verified 0.161.0, `JARVIS_CODEX_STRICT=1` restores the
exact match, `JARVIS_CODEX_CLI` picks the binary and nothing under `/mnt/`
ever runs. Unlike Claude's resolver it *refuses* a relative/Windows/broken
override rather than warning, launches the realpath it checked rather than
the symlink, and resolves per session rather than per process — on purpose,
design §5.4. **Owner Codex chats use `approvals_reviewer = "user"`**
(2026-10-13): private config, effective config and start/resume receipts must
agree. Tasks retain `auto_review`. Every chat command/file approval, even a
bare callback, takes the owner-required widening path before AUTO or static
ALLOW; normal sandboxed work needs no callback. A task's explicit widening
— extra permissions, network, grant root, `writeStdin`, or any field
outside the verified schema — also goes to a human via `permit(...,
widening=...)` with no Always and a "SANDBOX WIDENING: …" headline, and
declines with no human, and its grants pass layer 1 first — a grant that is
or contains protected state or touches a credential dir is DENY unasked
(`denied_grant`); the headline is Codex-supplied text, so it is one cleaned,
capped line everywhere; unknown methods get an error, malformed approvals
and unknown `kind`s a decline; null fields never overwrite the item's
command; `answer()` can only deny an approval — the Claude provider's too since
BR-0, below — keep all of it), a worktree per
task (WP6), the daemon and local API (WP7, `jarvis daemon2`), one skills
folder for three consumers plus `jarvis-mcp` (WP8), and Discord rendering
(WP10a), Discord routing with thread-scoped approvals (WP10b), and the
task runner with the intake gate (WP11 — `TaskControl` in
`jarvis/v2/control.py` is the verb interface every surface uses). **The
backend ran one real task end to end on 2026-09-16** (design §17): Claude
orchestrated, Codex implemented in a worktree, a fresh Claude reviewer
failed it once and passed it second time, DONE in 198 s. **The HUD v2 landed the same
day** (WP12a backend + WP12b frontend under `hud/`, `jarvis hud` opens it, and since ED-4 the daemon serves only what `jarvis hud install` installed — `jarvis hud rebuild` and `jarvis restart` are the owner's two operation commands, DENY for agents;
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

<!-- claude-source:000025-000099:end -->

## Original CLAUDE.md lines 646–778

<!-- claude-source:000646-000778:begin -->
**A message sent while a turn runs is never a dead end (2026-10-08, design
§5.1/§11.8/§18; free suite `tests/v2/steer_check.py`).** It used to 409, and
the HUD's catch then dropped its hold on the still-running turn (no Stop, no
orb interrupt). `Daemon.deliver` — the HUD's send route, Discord's
`mirror.submit` and the escape hatch — starts a turn, **steers** the running
chat turn through the provider's optional `steer()`, or **queues** it (three,
in order, one queue for the HUD and Discord — the mirror's O-C6 queue moved
into the daemon). Per provider:
- **Claude:** a stdin user message with a uuid and `priority: "next"`, taken
  only once the turn's own CLI turn has visibly begun (so it never reaches
  stdin ahead of the message it steers). Every turn message carries a uuid
  too, and `--replay-user-messages` echoes each as it starts a turn, so
  `_read` knows which CLI turn is whose: a steer the CLI runs as a fresh turn
  is read inside the turn that sent it, and one that starts too late for that
  is absorbed by the next send — never mistaken for its answer. The session
  remembers unseen steers across turns, and a message that may be queued
  behind one goes in at `priority: "later"`, which 2.1.295 never folds. Once
  the CLI is known to echo, a CLI turn nobody sent (a background notice)
  before a message's own echo is never its answer, and a stopped turn's steer
  whose CLI turn starts late is interrupted when read, its words dropped
  (round 2; it still goes through `permit` in the gap, so a card can appear
  on an idle-looking thread). A single pull-based reader per client is never
  cancelled mid-read, so nothing the CLI says between turns is lost.
- **Codex:** `turn/steer`, in both verified schemas. "Method not found" means
  the interrupt fallback; 0.161.0's own steer errors queue; no answer in time
  counts as delivered (logged), never requeued — and still unanswered at the
  turn's end, the log says it may not have reached the model.
- **Fast path:** `[owner steering]` at v1's next step boundary via
  `Agent.take_steering`; a steer the turn never takes is handed back by
  `undelivered()` and run next.

A steer is logged before the provider has it; one the provider refuses waits
under the same id (`steer_queued`). A turn waiting on an approval or a
question is never steered, so a message can never read as its answer. The
owner's Stop drops what waits (`queue_cleared`; stop means stop), and so
does a steer the provider still held when Stop landed and then refused
(`"dropped"`, judged by `stopped_turns`, the turn it was aimed at — never
requeued). The HUD hands words back only into the box of the thread they
were typed in (`lib/giveback.ts`: held per thread until it is opened, each
taken once by nonce). A
`turn_finished` with `next` (messages about to run) keeps the HUD on the
thread: no idle flash, no follow-up mic window. `Daemon.send`
— the task runner's — still refuses while a turn runs, because the runner
retries on "already running", and a task's running thread still 409s
(through `deliver` too). A provider's own "a turn is already running" error is
**non-fatal**: fatal makes the daemon drop and close the session, which there
is the running one.

**A Claude turn the CLI says somebody else started is never the message's
answer, and "Prompt is too long" is never a reply (2026-10-12, live on a
Claude chat; free checks `injected_turn_checks` and `context_full_checks` in
`tests/v2/claude_provider_check.py`).** After every daemon restart the
first message to a Claude thread showed nothing and the next message got its
answer. A resumed CLI (2.1.295) runs a queued background-task notice as a
transcript-only turn of its own *before* the message — an empty, zero-usage
`success` result — and on a fresh session, where the CLI is not yet known to
echo (`prompt_echo`), `_read` took that result as the message's. Meanwhile
the real answer sat unread in the SDK's buffer, and once that filled, the
CLI stalled in its next PreToolUse hook until the next send drained it. The
CLI stamps an injected turn's `origin` (kind not `"human"`) on the user
message it replays and on its `ResultMessage`; `_read` now reads such a CLI
turn (`owner = "injected"`) and never ends the message's turn on its result
— whatever is known about echoing — unless the message's own echo came
inside it (a message absorbed into a notice's turn is answered there; its
result is taken, never waited past). **"Full" is an `is_error` result
together with the words "Prompt is too long"** — in the result, or in that
CLI turn's own top-level synthetic error (`error` set or model
`<synthetic>`); a sub-agent's error (`parent_tool_use_id`) and an error the
CLI recovered from (then a success result) are not. The synthetic text is
never shown as a reply. `_context_full` runs the CLI's own `/compact` once (a
stdin user message, as the Agent SDK documents; shown as one `compact` tool
call; bounded by `COMPACT_TIMEOUT_S`, then interrupted), and on a
`compact_boundary` **sends the message again only when the overflowing CLI
turn was the message's own and used no tool and took in no steer** (review
of PR #57: a `git push` ran, the context filled, and the push was sent again;
a steer's own CLI turn overflowed and the answered message was sent in its
place). Otherwise nothing is re-sent. The owner gets one non-fatal `ERROR`
naming what happened, and the turn ends as an error:
- `CONTEXT_COMPACTED_SEND_AGAIN`: compacted, not re-sent, send it again;
- `CONTEXT_NOT_COMPACTED`: no boundary, an error or the time limit;
- `CONTEXT_STILL_FULL`: re-sent and still full, start a New thread.
The session is kept. **Stop means stop**: it is looked for every
`INTERRUPT_POLL_S` during a compaction, and the turn finishes as
`interrupted` at once, never as an error and never after the 600 s bound.
`origin`, `error` and `parent_tool_use_id` are read through `getattr`
(the SDK is not pinned). Logs carry thread ids, origin kinds and outcomes
only. **Not fixed here**: the provider passes the CLI's
`total_cost_usd`, which is the session's running total, as each turn's cost,
and the daemon adds it per turn — a Claude thread's `cost_usd` is the sum of
running totals (a thread whose CLI total was 23.22 read 109.97).

**The Codex model table is the account's catalog, and drift degrades — never
breaks, never rewrites (PR #20, 2026-10-09; owner's decisions: options
1+2+3, no union).** `router.CLI_MODELS["codex"]` is the signed-in account's
last good `model/list` (`codex_catalog.parse`: slug ids, one capped name
line, Codex's effort words, hidden rows dropped, vision only when
`inputModalities` says `image`), **replaced whole**, else the built-in
`CODEX_FALLBACK` (ladders synced to what the account advertised 2026-10-08:
astra, both Sols and Terra reach `ultra`). It is saved atomically to
`config.CODEX_CATALOG_PATH` and loaded by `daemon2`'s `main()` (not
`Daemon.start`, so no test daemon reads the owner's file); a missing or
corrupt file is the fallback. The refresh runs on a **background thread**
from HUD reads (`/usage`, `/thread-models`), which answer from the last
snapshot at once; one at a time, 5 min TTL (failures too), 30 s when a turn
holds the login lock (`MetadataBusy`, checked **before** the CLI is probed —
`send` holds `_auth_lock` for a whole turn), one 10 s deadline for the whole
app-server session, the lock released in a nested `finally`, the catalog and
quota halves independent, and quota reads **merged** (an empty read wipes
nothing). **`load_routing` never raises**: a saved entry naming a model the
table lacks runs the role's default, an effort the model lost is clamped down
the provider's ladder, a malformed part takes its default — each with a note
(`GET /route` → `notes`, logged once) and **routing.json is never rewritten**,
so the owner's choice returns with the model. A project's entry and a HUD
Codex default degrade the same way; `POST /route` builds what it writes from
the file's well-formed parts (it repairs a bad file and keeps untouched
entries verbatim); `/usage` reads only the allowances. **`ultra` is Codex's
alone** (`router.CODEX_EFFORT_LADDER`; never on `models.EFFORT_LADDER`):
refused for the fast path and Claude (`roster/ultra` on Claude too), never
offered by the HUD for them, and a record holding it runs the model's
default — the fast path drops it before `llm.chat`. The default effort
stays **`high`** within the ladder (A4); Codex's advertised per-model default
is kept only as `advertised_effort`. Re-review round (same day): the catalog
file is **protected state** (v2 `protected_paths`, v1 `_protected_state`);
it is loaded with `O_NOFOLLOW|O_NONBLOCK`, a regular file only and never
read past its cap — a FIFO or a `/dev/zero` symlink planted there hung or
crashed boot — and `main()` falls back on *any* load failure;
`Daemon.stop` stops the ledger first (nothing saved or swapped after), then
`CodexProvider.cancel_metadata()` ends the read in flight, then waits;
`PATCH /projects` judges only the `routing.models` entries that changed, so a
stale one round-trips; and `GET /route` notes a built-in default the catalog
lacks (it still runs — there is nothing to fall back to). Tests point
`config.CODEX_CATALOG_PATH` at a temp file and restore the table.

<!-- claude-source:000646-000778:end -->

## Original CLAUDE.md lines 939–997

<!-- claude-source:000939-000997:begin -->
**A Claude thread's cost is the CLI's running total, counted once
(2026-10-12; `jarvis/v2/running_cost.py`).** `ResultMessage.total_cost_usd`
is the CLI process's running total for the conversation, not the turn's, and a
resumed CLI continues from the total its transcript saved (the 2.1.295 CLI's
own field text: "read the latest result rather than summing across results").
The provider passed it through as each turn's cost and the daemon summed it,
so thread 447ff25e read $109.97 for $23.22. Now every result's USAGE carries
what that result added (`running_cost.step`: a new session measured from 0, a
resume from `Thread.provider_cost_total`, which the daemon saves from the
event's `cost_total`, and zeroed by the CLI's `ConversationResetMessage`; a
positive figure below the baseline is a total that started again from zero; a
missing figure stays `None`, and so does a zero below a positive baseline,
which the baseline outlives), so a steer's, a background notice's, a stopped
and a `/compact` turn's result each count once. Tokens need none of it
(`usage` is per turn). **History:** a Claude thread the daemon opens is born
`cost_recount: 1`; one from before the fix (0) is recounted once at daemon
start (`recount_threads`) from its own log — derived with `step` from the
pre-fix running totals, while a record carrying `cost_total` keeps its logged
delta (the provider saw a `/clear` the log never records; review of PR #63) —
only when the logged figures add up to the stored total, and marked 1; one
whose log is unreadable or does not add up is marked -1, its cost left as it
was, said once. Never a Codex or fast-path thread. The ledger's pre-fix Claude
rows are not rewritten: `ceiling_hit` leaves out a Claude row's dollars
without `per_result` (each held a running total) and reads the task's threads
as stored now rather than a status saved before the recount; the HUD's daily
`equivalent_usd` still includes them for the day of the deploy. Free checks:
`tests/v2/running_cost_check.py`, `running_cost_checks`/`daemon_cost_checks`
in `claude_provider_check.py` and the ledger suite's
`test_ceiling_reads_recounted_threads_not_pre_fix_claude_rows`.

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

**A task's question is answered by index (2026-10-11).** `POST
/tasks/{id}/answer` takes exactly `{index, text}`, `index` the place in
`spec.questions`, and only a CLARIFYING task asks (`hud/src/lib/
taskQuestions.ts`, the daemon's `open_questions` rule). The HUD sent
`{question, answer}` and swallowed the 400, so answering from the window
never worked — and the mock took any body. A refusal now shows its sentence
under the box and keeps the words, and the mock calls the daemon's
`task_verb_body` and `control.answer_refusal` instead of answering `ok`.
Free checks: `tests/face/hud_v2_task_check.py` (own mock), `TaskView.test.ts`,
`taskQuestions.test.ts`, `api.test.ts`. The other task verbs (steer, cancel,
resume, start) still swallow a refusal in `App.tsx`.

<!-- claude-source:000939-000997:end -->

## Fast path: the owner's desk turn edits directly (2026-10-14)

Decisions: `docs/plans/2026-10-14-fast-path-edits-decisions.md` (F-1..F-7);
design §2 D2 and §8.1 carry the amendment. Code: `jarvis/v2/providers/fastpath.py`.

- **Who.** `EDIT_TOOLS` (`write_file`, `edit_file`, `run_command`,
  `run_readonly`) join the turn exactly where `DESK_TOOLS` do: an owner's chat
  brief (`_owners_chat`), a message from the HUD window (`desk_message`), not
  steered from elsewhere since. `_toolset` refuses a non-owner brief that names
  one; `_turn_tools` drops them (schemas and prompt wording) for every other
  turn; `_take_steering` drops them at the next step boundary after an
  off-desk steer and re-binds the toolset dispatch enforces. `FAST_TOOLS`
  itself is unchanged and still holds nothing dangerous; `_validate_toolset`
  allows a dangerous tool only in `EDIT_TOOLS`.
- **The gate.** `_approver` sends `run_command` and the `ASKED_TOOLS` edits
  (`write_file`, `edit_file`, `run_readonly`, through `runtime.ask_tools()`) to the daemon's
  §6 permit, with **no v1 `jarvis_human_backed` flag** and **AUTO asked as
  ASK** (the fast path has no reviewer). The permit applies layer 1, the
  always-ask rules, then the allowlist and rules' ALLOW at layer 4 only for a
  human-backed asker. Cards go through the broker and are answered by
  `POST /approvals/{id}`; `answer()` still raises. An edit tool is denied
  off the desk, before and after the permit, and a yes that lands after Stop
  never runs. `run_readonly` is asked too (review H1): rules' ALLOW still runs
  an ordinary read unasked, and `run_readonly` itself now refuses an operand
  that is or reaches a credential folder (symlinks and globs followed, a
  recursive grep of a folder holding one, any expandable `$`, grep's other
  recursion spellings, and the symlink-following grep `-R`/rg `-L` modes),
  on every surface. Commands get an empty stdin, so `rg TODO` with no folder
  searches its folder instead of waiting on a pipe.
- **Where.** `runtime.working_dir()` is bound per turn to the brief's `cwd`
  resolved to its physical folder (`realpath`, review M1; the prompt states
  it). A brief whose folder cannot be bound (not absolute, not an existing
  folder, or a backtick/control character) gets no edit tools (review M2). `files.in_working_dir` places a
  relative path before any check; `shell._run` starts there; grep_files'
  root, `protected_state.command_touches(cwd=None)` and the `.env` scrubber's
  search folders use it. Unbound, everything is as before.
- **Budget and prompt.** `config.FAST_STEPS` (8) and `FAST_EDIT_STEPS` (30),
  chosen per turn in `_run`; exhaustion proposes at both. The driving ending
  states the turn's own budget (`_drive_ending`). The prompt's three edit
  paragraphs live in `config` (`FAST_LIMITS_PARAGRAPH`,
  `FAST_DIRECT_PARAGRAPH`, `FAST_PROPOSE_PARAGRAPH`) with
  `PROMPT_TOOL_PARAGRAPHS` rules; without the edit tools the fast path's
  prompt is the pre-change text byte for byte.
- **Known limits.** A desk turn steered off mid-turn keeps the budget it
  started with while its prompt states eight. A card still pending at Stop
  stays open until answered or timed out (the permit takes no cancel), but
  the turn's approver turns a late yes into a denial. A brief may pin the
  four edit tools together or not at all. The permit's layer 1 judges
  commands at the brief's raw `cwd`, not its physical folder (every
  provider; v1's check beneath it uses the physical one here).
- **Environment.** The `.env` scrubber also withholds this process's own
  credential-named environment values (`*_API_KEY`, `*_TOKEN` …), so
  `run_readonly env` cannot print the daemon's provider keys (open on v1
  before).

Free suites: `tests/v2/fastpath_edits_check.py` (see the PR for the
mutation evidence),
`tests/v2/fastpath_check.py`, `tests/prompt_tools_check.py`.

## Native sub-agent protocol, recorded (SV-0, 2026-10-15)

Offline recordings of Claude Code 2.1.295 and Codex 0.161.0 sub-agents
(scripted local model, rootless netns, temporary config, $0) are in
`tests/v2/fixtures/subagents/`, pinned by `tests/v2/subagent_fixtures_check.py`;
the verdicts are in `docs/plans/2026-10-15-sv0-findings.md`. The facts most
likely to bite:

- **Claude agents run in the background by default** (`run_in_background`
  omitted), and a finished background agent makes the CLI start a whole
  orchestrator turn on its own (`result.origin.kind == "task-notification"`)
  between Jarvis's sends. Unread, such a turn's stream events overrun the
  SDK's 100-message buffer and stall other background agents' hooks — the
  2026-10-12 stall, reproduced in `background_flood`.
- **Claude never streams sub-agent deltas**; tagged whole messages only, and
  depth-2 (nested) agents' messages not at all. `stop_task` stops one agent
  and the turn continues; `client.interrupt()` also kills background agents.
- **Codex delivers every child notification** on the parent's connection
  under the child's `threadId`; a child is announced by a `subAgentActivity`
  item (not `collabAgentToolCall`, not `thread/started`). A child's approval
  request arrives with the child's `threadId`, which `codex.py`'s non-MCP
  server-request branch treats as a fatal identity mismatch today. Child
  `turn/interrupt` ends only that child's turn and does not kill its running
  command; parent token totals exclude children.
