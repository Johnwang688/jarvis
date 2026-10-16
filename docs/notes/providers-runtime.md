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
  request arrives with the child's `threadId`, which every `codex.py`
  server-request branch (approvals, MCP elicitation, `currentTime/read`)
  treated as a fatal identity mismatch until SV-2a (below). Child
  `turn/interrupt` ends only that child's turn and does not kill its running
  command; parent token totals exclude children.

## A Codex rename's destination is judged (2026-10-15)

Until this fix, `codex.py`'s `_tool` built an `apply_patch` call's `paths`
from `changes[].path` alone, and `permissions.file_targets` never reads
`changes`. Codex 0.161's `FileUpdateChange.kind` is `add`, `delete` or
`{type: update, move_path}` (generate-ts v2 schema; `move_path` is its only
destination field). So a patch renaming `notes.txt` to `.env`, or into the
gate's own state, showed layer 1 only `notes.txt`. In an owner chat the card
named only the source. In a task (`auto_review`, AUTO profile), the session's
own file approval was **accepted with no human**: the probe with a deny-all
asker on `main` replied `accept`.

Now `_change_paths` lists every path a change writes, the `move_path`
destination included. Layer 1 judges them all and the card shows them all. A
file approval is **declined unasked**, with a non-fatal notice, when any of
these holds:

- `changes` is not a list;
- a change has no path;
- a change's `kind` is not an object of type `add`, `delete` or `update`;
- a `move_path` is not a string or rides a kind other than `update`;
- the kind carries a field the schema does not have (it might be another
  destination).

The legacy v1 `applyPatchApproval` request, whose `FileChange` also has
`move_path`, is not handled at all: it is refused as an unsupported method,
as before.

Claude has no analogous gap. Its file tools (Write, Edit, MultiEdit,
NotebookEdit) name one `file_path`/`notebook_path`, and it has no rename
tool. A shell `mv`/`cp`/`git mv` onto `.env` or the allowlist is refused by
`denied_command` (checked in-process).

**Closed since (SV-2a, below):** a file approval whose `fileChange` item was
never seen had no paths to judge, and in a task it was accepted with no
human. It is now declined unasked.

Checks: `tests/v2/codex_provider_check.py`:
- `test_file_rename_destination_is_judged_by_layer_1` covers `.env`, a
  relative `.env.local` and the allowlist, each in a chat and in a task, plus
  a benign rename that shows both paths;
- `test_file_change_kind_that_cannot_be_read_is_declined` covers nine shapes.

Both fail on `main` (15 failures), and 5 of 5 mutants are killed. The fake
peer's file items now carry the `kind` the schema requires.

## Codex sub-agents' server requests are not fatal (SV-2a, 2026-10-15)

Shipped ahead of the sub-agent view, from SV-2's non-fatal-request piece
(plan D3, Q2; SV-0 findings 9–11). `codex.py` learns its children as it
reads the wire (`_note_child`, before the early queue and the thread/turn
filters): a `subAgentActivity` **`item/started`** of kind `started`, on the
session's thread or a known child's (a grandchild is announced on its
parent's), registers `agentThreadId` with a label for the session's life —
children outlive the turn that spawned them. The label is the model-chosen
`agentPath` reduced to `[A-Za-z0-9_./-]` (anything else becomes `_`) and
capped at 40, shown as `‹label›`: it cannot pose as the headline's own
` · ` parts, and the child part of the headline is never cut. Nothing else
registers one: not `item/completed`, `thread/started` or
`collabAgentToolCall`. At `CHILD_LIMIT` (256) the oldest child whose turn
has completed is forgotten (with its items) to make room. A child with a turn
running is never dropped, so with none idle the newcomer stays unknown.

A known child's server request never ends the orchestrator's turn, and its
`turnId` never becomes the session's:

- **command/file approval: the same `permit` gate, always as a widening.**
  The headline leads with "Codex sub-agent ‹label› (not the orchestrator)
  asks to run beyond its sandbox"; the owner decides on the
  usual card, Always is never offered, and deny-all, strict, a timeout or a
  callback without the widening keyword declines. This holds for tasks too
  (their own plain approvals keep `auto_review`'s fallback; a child's
  don't). Chosen over a decline-only fix because the identity needed is one
  map, and the plan's recommended answer to Q2 is this card. The approval is
  judged on that child's own in-progress command/fileChange item
  (`child_items`: id, type, command, cwd and changes only; dropped on
  `item/completed`; bounded at 512, oldest out). A file approval names no
  paths, so one whose item is unseen, belongs to another thread, cannot be
  read or writes nothing is declined unasked, never asked blind.
  `APPROVAL_REQUESTED` carries `subagent`.
- **file approvals, session and child alike: no readable file item, no
  ask.** A file approval names no paths; only its `fileChange` item says
  what it writes. So in every profile it is declined unasked, with a
  non-fatal notice, when:
  - its item was never seen;
  - the item is not a `fileChange` (for a command approval, an item that is
    not a `commandExecution`, which would be judged as a patch);
  - the item names no file (`changes: []`);
  - a change cannot be read (#196's `_change_paths`: kind, `move_path`,
    unknown field).

  Before this, a task under AUTO accepted such an approval with nobody
  asked (`paths: []`). A command approval whose item is unseen is unchanged:
  it carries its own command, which is judged.

  The ordering this rests on is now recorded, offline, with the SV-0
  harness (`record_codex.py --only patch_approval patch_approval_child`):
  `patch_approval.jsonl` and `patch_approval_child.jsonl`. In both, Codex
  0.161.0 sends `item/started` for the `fileChange` item, under the asking
  thread, before `item/fileChange/requestApproval`. The item carries `kind:
  {type: update, move_path}`, and the request carries no paths
  (`subagent_fixtures_check.py` pins this). A child's file approval routes
  like its command approvals.
- **MCP elicitation:** declined, with the non-fatal `MCP_DECLINED` notice.
- **`currentTime/read`:** answered, as for the session (a clock read).
- **a question (`requestUserInput`):** refused with an error and a notice;
  the owner could not tell it from the orchestrator's until SV-3.
- **permissions requests and anything else:** as for the session (empty
  grant; unsupported method refused), with "(from Codex sub-agent …)" on the
  notice. A child request naming no `turnId` is refused, not fatal.

A request from any other thread is still a fatal identity mismatch.
Child notifications were still skipped then; SV-2 (below) routes them.

**Known limit (MED-2 of the #195 review): a long wait on a child's card can
close the session.** While the owner decides on any card, the turn loop does
not read the transport. A child's card is the likelier case, because the
orchestrator and sibling children keep running while it waits. Their traffic
piles up in the RPC queue (`CODEX_RPC_QUEUE_LIMIT`, 4096 messages). On
overflow the transport is marked failed, the approval is declined
whatever the owner chose, and the session closes with a fatal error. That
fails safe: nothing is approved. But the turn is lost. SV-6's between-turn reader/drain owns the
real fix. Checks: `tests/v2/codex_provider_check.py` replays the SV-0
recordings (`fixture_turn`, peer mode `replay`), thirteen tests, plus
`test_file_approval_without_a_readable_file_item_is_declined_unasked` (16
cases, all failing on `main`). The child tests fail on `main` with the
identity mismatch. The two that pin unchanged behavior
(unknown thread, the session's own request) pass on `main` as they should,
so they were proven with mutants instead.

## A file approval is judged on every version of its item (2026-10-15)

Follow-up to #195's review. A `fileChange` item can change after it starts:
`item/fileChange/patchUpdated` replaces its `changes`. Until now the session
kept only the latest version (`s.items`) and a child only its latest
(`child_items`). So a later, partial update could hide a destination an
earlier version had reported, and an unreadable version was forgotten once a
readable one followed.

Now every stored `fileChange` item carries `_PATHS_SEEN`: the union of every
path any version reported (`item/started`, each `patchUpdated`,
`item/completed`) and the first reason any version could not be read
(`_remember`, on the early path, in `_notification` and in `_note_child`).
A file approval is judged on that union, and it is declined unasked if any
version was unreadable; the latest version's kind checks still apply.

**Bounded cost (review MED).** `_remember` runs on the turn loop's thread
for every update. The first version of this union scanned a list on every
update, which is cubic over a streamed patch that resends every change each
time: measured at 0.7 s for 1k files, 5.4 s for 2k and 42.7 s for 4k. That
stalls the reader and overflows the RPC queue. The union is now an
insertion-ordered dict, with two caps per item:
- `CHANGE_PATH_CAP`, 4096 distinct paths;
- `CHANGE_WORK_CAP`, 1,000,000 changes listed across all of an item's
  versions.

Past either cap the item is marked past judging, later updates are not
read, and its approval is declined unasked ("names more than 4096 files" /
"updates are too large to judge"). On the owner's machine, 5000 cumulative
updates cost 183 CPU ms and 5000 one-file updates cost 3.
`test_the_union_of_an_items_versions_is_bounded_work` runs them in a child
process with a 30 s deadline and a 0.4 CPU-s `loadclock` bound. The old
list scan costs 1.72 s there and no work cap 1.46 s, both past the 3x the
bound may be excused under load.

`args["paths"]` and `args["changes"]` are set from the stored item *after*
the request's own fields are merged. A request carrying a `paths` field can no longer replace what layer
1 judges: before this, such a field (an unknown field, so a widening) became
the judged paths.

Checks in `tests/v2/codex_provider_check.py`:
- `test_file_approval_is_judged_on_every_version_of_its_item` (session, chat
  and task);
- `test_file_approval_before_the_turn_start_reply_sees_every_version` (the
  recorded `patch_approval`, with the update and request ahead of the reply);
- `test_child_file_approval_is_judged_on_every_version_of_its_item`.

The "destination added by an update" cases pin `patchUpdated` on each path
and pass on `main`; the union, sticky-unreadable and request-`paths` cases
fail there. Six mutants are killed: `patchUpdated` ignored on the session's
early path, on its later path and on the child's; latest version only; an
unreadable version forgotten; the request's `paths` kept.

**When does `patchUpdated` occur?** Recorded offline with the SV-0 harness:
`record_codex.py --only patch_multi --enable apply_patch_streaming_events`
gives `codex/patch_multi.jsonl`. A four-file patch (three adds and a
rename out of the workspace) arrives whole in one `item/started`, before
the approval, with **no** `item/fileChange/patchUpdated`, even with that
feature on. In 0.161.0's code mode (`code_mode_host`, stable and on),
`apply_patch` runs inside `exec`, so its input is not streamed.
`apply_patch_streaming_events` is "under development" and off by default.
The union and caps are defence for a Codex that does stream patches.
`subagent_fixtures_check.test_a_multi_file_patch_arrives_whole` pins the
recording, so a newer CLI that starts streaming fails there, by name.

## Claude sub-agents are their own (SV-1, 2026-10-15)

Before this, every message a Claude Agent child sent became the parent's
TEXT/THINKING/TOOL_* (`_translate` never read `parent_tool_use_id`), so a
child's words and calls landed in the transcript the owner reads as Jarvis's
reply, the Discord mirror, a task's result and, after the voice rework, the
voice. Now (plan `docs/plans/2026-10-13-subagent-view.md` SV-1, adjusted by
the SV-0 findings):

**The provider** (`claude.py`, "native sub-agents"). A child is a sid,
`cl:<spawning Agent call id>` (`child_sid`; an id the pattern does not fit is
hashed, never dropped). A message tagged with `parent_tool_use_id` is built
exactly as the parent's would be (`_blocks`, `_tool_result`: `_safe`, the
private summaries, the denial mapping) and then each event is wrapped as
`SUBAGENT_EVENT {sid, event: {kind, data}}`. Nothing tagged is ever the
parent's: an unannounced tag still gets a sid of its own, and a tagged stream
event (2.1.295 sends none) is dropped. The `task_*` system messages, read by
`subtype` and raw `data`, give `SUBAGENT` lifecycle events: `task_started`
with `task_type: local_agent` starts a child (name = `description`, kind =
`subagent_type`, `is_backgrounded`, `spawn_depth`, `parent_task_id` → parent
sid); `local_bash` and anything else is ignored. `task_progress` gives
tokens/tool uses/last tool; `task_updated` (`killed` → stopped) and
`task_notification` give the end, said once; the notification's `summary`
becomes the child's `result` entry. Children are session state, not turn
state: they outlive turns, and `child_tools`/`child_denied` keep their calls'
names and refusals across turns (bounded: `CHILD_LIMIT` 256 children, oldest
finished out; `CHILD_TOOL_LIMIT` 1024). In `_read`, a tagged message no longer
opens an "unknown" CLI turn (a background child talks between turns as a
matter of course); it still counts as `turn_work`.

**Background children are normal.** Status `backgrounded` (live, not waited
for) is never ended by the turn. Only a top-level foreground child still live
when its message's turn ends is stopped, inferred ("the turn ended"); the
CLI's later word replaces that. The daemon ends live children when their
session goes (`_cleanup`: close, archive, loss; a Claude model switch, which
reconnects) and reads any still marked live back as stopped after a restart.

**Approval cards.** The hook's `agent_id` is the child's `task_id`, whose
`task_started` always comes first (SV-0 finding 3), so `agent_id → task_id →
sid → name` gives the card's label (`child_label`: scrubbed, `[A-Za-z0-9
_./-]` only, 40 characters). `_decide` passes it as `permit(...,
subagent=label)` only to a callback that takes the keyword (a raise would
deny). `build_permit` uses it for the card's `origin` alone ("Claude sub-agent
‹name› (not the orchestrator)", then " in task …"), re-cleaned there. Every
layer reads exactly what it reads without it: a child is never answered more
loosely, or more strictly, than its orchestrator would be. Codex keeps its
SV-2a widening path; the two differ because a Codex child's approval *is* a
widening of its sandbox, while a Claude child runs under the parent's hook.
The logged gate question carries `subagent: sid`.

**The daemon** (`subagents.py`, `Daemon._record_subagent`). The new kinds
never reach `log.jsonl`, the ledger or the generic publish. They are cleaned
outside the daemon lock (the scrub and a tool's label may read credential
files) into a per-thread record: memory (`THREAD_LIMIT` 32 threads LRU,
`CHILD_LIMIT` 64 children, `ENTRY_LIMIT` 300 entries each, `TEXT_CAP` 4000)
and the thread's own `subagents.jsonl` (a cold read takes the last
`LOAD_BYTES`, 4 MiB). Tool entries keep a label and an outcome, never
arguments or a result summary; `hud_browser_act` is "Browser action";
thinking is not kept. The bus gets one `subagent` record per change with ids,
status, times and a count only. Routes: owner-only `GET
/threads/{id}/subagents` and `.../subagents/{sid}/transcript`
(`docs/hud-api.md`, "Native sub-agents"). The runner, Discord and titles
needed no change: they read `text`/`tool_*`, which children no longer are.

**Bounds in bytes, and on disk (review of #198, M1).** A count alone let a
thread hold 64 × 300 × 4000 characters. Each entry now carries its JSON size,
and a thread's entries together are held to `THREAD_BYTES` (1 MiB, a quarter
of `LOAD_BYTES`): past it, the oldest entry of any of its children goes
first, so an idle child's old words go before a busy child's new ones; rows
are never dropped for bytes. The file was unbounded like `log.jsonl`; now,
past `FILE_CAP` (4 MiB), it is rewritten as one `snapshot` line per child of
what memory holds — written beside it (0600, `O_EXCL`), fsynced and renamed
over it, under the record's lock — so it stays under the cap and a cold read
(at most its last 4 MiB) rebuilds memory exactly. The file is created 0600
(L6), and every append happens under the record's lock, so the file's order
is memory's (L3: a restart could otherwise replay updates out of order).

**A child's reviewer decline has no "run anyway" (decision, review of #198,
M2).** Claude's reviewer declining a child's call is kept as the child's
`declined` entry (the tool's name only) and a `declined` count on its row
and bus record. It never reaches the escape hatch: the hatch runs the
declined command and feeds its output to the session's next user message,
which for a child's call would hand a child's command output to the
orchestrator. The hatch subscribes to `reviewer_declined` on the bus, which
children no longer produce. No thread-level notice: an `error` line would
read as the orchestrator failing (ledger, Discord footer, a task's result),
and a new kind needs SV-3's HUD.

**Ends are said once, even out of memory (L2, L4).** A model switch is
reconciled with the provider whether or not it was refused:
`ClaudeProvider.set_model` marks every live child stopped once the old CLI is
disconnected (so dead children hold no `CHILD_LIMIT` slot), `live_subagents`
says which still run, and `Daemon._switch_ended_subagents` ends the rest in
the record (a switch refused before the disconnect, such as "a turn is
running", keeps them). A thread read back from disk with a child marked live
and no session of its key ends it with a written and published `finished`
record, so `end_session` on a thread out of memory and a restart both say it
once.

**Did SV-1 make the between-turn stall worse? No.** It changes only how a
message already read is translated. It adds no read, no buffering and no work
on the SDK's reader task; the stall's cause — nobody reads the CLI between
sends, so the SDK's 100-message buffer fills and the CLI blocks in its next
hook — is untouched. A child's messages that arrive between turns are still
read by the next send, now recorded as the child's (with that send's
`turn_id`).

### What SV-6 must do for Claude's injected turns

Claude Code 2.1.295 starts an orchestrator turn by itself when a background
child finishes: `init`, a full streamed assistant turn (deltas included),
and a `result` whose `origin.kind` is `task-notification`, with no `query()`
(SV-0 finding 6; `one_default`, `parallel_default` — one per finished child —
`stop_bg`, `background_unread`). Today `_read` reads that turn at the next
send: its result is recognised as `injected` and does not end the send, but
its **words and tool calls are handed out as the next owner message's turn**
(pinned: `claude_subagents_check.test_known_gap_an_injected_turns_words_land_in_the_next_send`),
and its usage is booked to that turn. Attributing it to the next owner
message is wrong, and leaving it unread is what stalls. SV-6 must:

1. **Drain the stream continuously.** One reader per client, always reading
   while connected (not on request, as `_Pump` does now), so the SDK's buffer
   never fills and a hook is never left unanswered between turns. It must
   never block on a consumer: what it hands on goes to bounded queues, and
   past the bound it drops `stream_event` deltas first, never whole messages,
   results or `task_*`.
2. **Route what it reads by owner**, keeping `_read`'s rules for a send:
   - tagged messages and `task_*` → translated (`_translate` already does it)
     and handed to the daemon at once through a session-level sink, since no
     turn queue exists between sends (`session.events is None`); the daemon's
     `_record_subagent` already takes them with no turn;
   - a CLI turn that starts with no send waiting for it (an unechoed `init`
     or assistant message while no send reads, or a replayed user message
     whose `origin.kind` is not human) → **its own turn**, never the next
     owner message's: the daemon opens a system-originated turn (a fresh
     `turn_id`, a `user`-like record saying a background agent finished,
     `via: "system"`), records its text/tools/usage under it and closes it
     with its own `turn_finished`. While it runs, an owner message queues or
     steers by the existing rules (the CLI queues our `query()` behind it
     anyway; `_read` takes over at our echo).
   - everything else → held, in order, for the send that owns it (the
     message's echo still decides, as now).
3. **Decide what surfaces show** (owner questions): whether an injected turn
   posts to Discord and is spoken; how the HUD marks it ("after sub-agent X
   finished").
4. **Keep the existing rules**: `_read`'s injected/leftover/stopped logic
   stays the fallback for a turn that starts while a send is reading; the
   owner's Stop is `client.interrupt()`, which also kills background agents
   (say so on the Stop button); a model switch or close still ends them.
5. **Prove it offline** with the SV-0 harness: `background_flood` re-recorded
   under the drain must show the late child's next request about 4 s after
   its `sleep`, not after disconnect; and a replay test must show an injected
   turn's text under its own `turn_id`, never the next send's.

**Interim options (proposed, not shipped).** (a) Ask the orchestrator, through
`system_append`, to pass `run_in_background: false` unless it means to
continue without the result: no injected turns, no stall, but slower turns
and a change to how the model uses its tool. (b) A between-turn drain that
only reads into a bounded in-memory deque the next `_read` consumes first:
prevents the stall without fixing attribution, but `_Pump`'s read-on-request
contract and the steer holds' "any frame is life" timing would change, so it
needs its own review. (c) Mark the injected turn's words inside the next
turn with a system line ("a background agent finished while you were
away"): cheap, attribution still wrong in the record. None is in SV-1.

Checks (free, offline): `tests/v2/claude_subagents_check.py` (provider, from
the recordings via `tests/v2/claude_replay.py`: one, parallel, background,
nested, failed, stopped, interrupted, labels, privacy, bounds) and
`tests/v2/subagent_routes_check.py` (daemon, routes, readers, permit
matrix): 30 and 20 tests at first (31 and 28 after the review of #198, whose
nine new tests all fail on the commit before it, and whose 13 mutants are
all killed). On `main` 26 of the 30 and 19 of the 20 fail; the
five that pass there pin unchanged behaviour (the gate's answer, a permit
without the keyword, the ledger) and are proven by mutants instead. 24
mutants, all killed: tagged messages as the parent's, a child's text before
`_safe`, a tagged delta, turn end stopping background or nested children,
`local_bash` as a child, the daemon logging/publishing the new kinds as
ordinary records, a name on the bus, routes not owner-only, the label
computed under the daemon's lock, `hud_browser_act` not rewritten, `args` in
a transcript, unbounded entries, a closed session or a restart leaving
children live; and for the label: none, a widening (stricter), an outright
allow (looser), ignored by the card, uncleaned in permit or provider, forced
on a permit without the keyword, riding in `args`, taken from the task's
current activity.

## Codex sub-agents are their own (SV-2, 2026-10-15)

Before this, a known Codex child's notifications were dropped by the turn
loop's thread filter: the owner saw nothing of what a child did. Now (plan
`docs/plans/2026-10-13-subagent-view.md` SV-2, narrowed by the SV-0 findings
8–14) they feed the record and routes SV-1 built, as the same two kinds.

**The provider** (`codex.py`, "What a child says and does"). SV-2a's
registration stays the only identity source: a `subAgentActivity`
`item/started` of kind `started` on the session's thread or a known child's
(`_note_child`, at wire-read time). It now also makes the child's
`_ChildView` (sid `cx:<threadId>`, `child_sid`, hashed if the id does not
fit; name = SV-2a's cleaned label; depth and parent sid from the announcing
child), forgotten with its `children` entry at `CHILD_LIMIT`. In `send`,
after the early queue and before the thread/turn filters, a message on a
known child's thread goes to `_child_notification` and never further; a
message on the session's thread first passes `_announce`, which emits the
child's `started` (whatever turn the item names) and notes an
`interrupted` (the model's `interrupt_agent`). What a child sends becomes:

- its turn: `turn/started` sets `running` (an ended child given another turn
  — `sendInput`, `followupTask` — is announced `started` again);
  `turn/completed` gives `done`/`failed`/`stopped` once per turn, with
  `duration_ms`, a stop reason, and for `failed` Codex's error message as a
  `result` entry. `thread/closed` stops a live
  one. The parent-side `completed` is never read (SV-0: absent for failed and
  externally interrupted children);
- `thread/tokenUsage/updated`: the child's `tokens` (its thread's total) on
  a lifecycle `progress`. Never `s.accounting`, never a USAGE event;
- a command/file/MCP item: `tool_started` with only what the daemon's label
  reads (a command and folder; a patch's paths, at most 20, never its diff;
  MCP arguments, none for `hud_browser_act`), then `tool_finished` with an
  outcome (`denied` for `declined`), never a result. Open calls per child are
  bounded (`CHILD_CALL_LIMIT`, 256, oldest out);
- a completed `agentMessage`: `text`, cut at 8000 (twice the record's cap,
  so the record scrubs before it cuts and no credential is halved into an
  unknown shape);
- a reviewer's denial (`item/autoApprovalReview/completed`, tasks only):
  `reviewer_declined` with the tool's name alone;
- anything else — deltas, reasoning, plans, its brief, `wait` calls, status
  changes, `error` (the turn's end carries it), `model/rerouted` — nothing.
  A malformed shape is dropped, never fatal.

The parent's turn, TEXT/TOOL_*, USAGE, `s.items` and accounting see none of
it. The turn's end ends no child: every Codex child may outlive it (SV-0),
so `background` is always false and status stays `running` until its own
turn ends. `live_subagents` lets the daemon's model-switch reconciliation
keep them (a Codex switch rides the next `turn/start`). SV-2a's request
handling is unchanged.

**The daemon** needed no new path: `_record_subagent`, `subagents.py`, the
two owner routes, the bus record and `end_session` (close, archive, loss, a
fatal send, restart) take Codex's events as they are. Two rules changed in
the record (the second from the review of #199):

- **Revival, Codex only.** An ended `cx:` child is live again on a
  `started` from the session that ran it (its key), and only then; any other
  live word after an end stays stale, as SV-1 had it. A `cl:` child is never
  revived: Claude has no second turn for a child.
- **One scrub for both providers.** `SubagentRecords.prepare` scrubs every
  `text` and `result` entry with `redact.scrub_text` (outside the daemon's
  lock): `secrets.scrub` (Jarvis's credential values), every
  `credential_patterns.PATTERNS` shape, and the bearer, JWT and jarvis-mcp
  shapes that were Claude's provider's own (moved to `jarvis/v2/redact.py`;
  `claude._safe` imports them and is otherwise unchanged). A scrub that
  raises keeps `[withheld]`. Before the review, a Codex child's text had
  `secrets.scrub` alone, less than Claude's `_safe`.

SV-2a's eviction at `CHILD_LIMIT` now prefers an idle child whose record has
also ended (its `turn/completed` handled, not only read off the wire), then
any idle child, as before.

**The owner's Stop does not stop Codex children.** It interrupts the
orchestrator's turn; its children run on (SV-0). Before #200 the next send
was then fatal (an interrupted turn's accounting was left incomplete), which
closed the app-server and the children with it; since #200 the chat
continues, and in the replay of `parent_interrupt` the child's later
`turn/completed` lands as `done` in its own record during the next send.
SV-4 adds a per-child Stop. The fatal-send path (the session's end) is
pinned with a turn that completes without token usage.

**Between turns nobody reads the connection** (SV-6). Child traffic that
arrives after the parent's turn is read by the next send, recorded under
that send's `turn_id`; the 4096-message queue limit (SV-2a's MED-2) is
unchanged.

**For SV-4 (owner decision).** Codex's `turn/interrupt` on a child's thread
ends that child's turn (`stopped`, "its turn was interrupted"), but a
command it is running runs to its end, and the orchestrator's model is not
told: no `subAgentActivity`, and its `wait_agent` times out (SV-0 finding
12). Offer per-child Stop with "ends its turn; a running command may
finish", or keep it absent.

Checks (free, offline): `tests/v2/codex_subagents_check.py`, 34 tests:
the 13 recordings (one, parallel, nested, fail, child_interrupt,
close_agent, approval, approval_accept, patch_approval_child,
one_multi_agent_v2, and no_wait, parent_interrupt, steer_parent over two
sends) through the real `CodexProvider` over real pipes
(`codex_provider_check`'s replaying peer), then those events through a real
daemon: isolation (parent events, accounting, log, transcript route,
Discord, titles, bus, ledger), lifecycle, privacy, the shared scrub (a
Claude OAuth token, a jarvis-mcp token, a JWT, a GitHub token and an owned
credential value in a Codex child's message and failure reason, and the
same words in a Claude and a Codex record alike), bounds and the record's
revival rule. On `main` 29 of the 34 fail; the five that pass pin unchanged behaviour
(parent accounting, ledger, no deltas, malformed shapes not fatal, a Claude
child never revived) and are proven by mutants. 31 mutants, all killed: a child's
items as the parent's, its tokens into the accounting or as USAGE, its turn
end ending the parent's, an unannounced thread recorded, the end read from
the parent's notice, the parent's turn end stopping children, unscrubbed
text, a diff or `hud_browser_act`'s arguments in the envelope, a delta
passed on, the record never reviving or reviving on another session's
word, no second `started`, interrupted read as done, a reviewer's rationale
kept, a forgotten child keeping its record, unbounded open calls, a name on
the bus, the daemon logging the kinds, `live_subagents` empty, a failure's
reason dropped, a malformed turn fatal, `interrupt_agent` not told apart, a
nested child's parent dropped; and from the review: no scrub in the record,
the shared scrub without the bearer shapes or without `credential_patterns`,
a scrub failure showing the words, a Claude child revived, the newest open
call evicted first; and after #200, the session's end leaving its children
live. `codex_provider_check` (111),
`claude_subagents_check` (31), `subagent_routes_check` (28),
`subagent_fixtures_check` (25) and `codex_session_mcp_check` (34) pass
unchanged.

## A Stop does not end a Codex chat (2026-10-15)

Until this fix, a Codex turn that ended `interrupted` or `failed` left
`usage.json` at `complete: false` (WP4: "Failed/interrupted accounting is
retained but cannot be reused/resumed as complete; start a fresh thread").
Nothing started a fresh thread. Through the real daemon and `CodexProvider`
over pipes, the owner's Stop followed by another message on `main` gave:

1. The Stop: `turn_finished` `interrupted`.
2. The next message: a fatal error ("Codex previous turn accounting
   incomplete; start a fresh thread"), so the turn ended `error`. The daemon
   dropped the session.
3. Every later message, and every resume after a daemon restart: HTTP 409
   `request failed (RpcError)`. The resume read `complete: false` and refused
   ("Codex resume accounting missing, mismatched or incomplete").

The chat stayed dead and its context could not be reached. A capacity
failure (`failed`) did the same, so its cooldown never applied to that
thread. The same session failed on the accounting check before reaching the
cooldown check.

`turn/completed` is Codex's last word on a turn, and its token counters are
cumulative. In the SV-0 recording `parent_interrupt` (0.161.0), the stopped
turn's last `thread/tokenUsage/updated` arrives before the `turn/interrupt`
reply and `turn/completed` `interrupted` (seq 27–31). The refusal waited for
numbers that never come: a later cumulative total adds anything Codex
counted, and nothing can add what it never counted. So `interrupted` and
`failed` now close the accounting, as `completed` does. Such a turn
increments `partial_turns` in `usage.json`. A sampling request cut off
mid-stream may never have been reported by Codex: whether it was is
**unverified** (the SV-0 recordings' fake model answered before the
interrupt), so these totals may be low by an unknown amount. Nothing is
estimated or added for it. Older files without the field load as 0.

What still fails closed, as before:

- a turn that ended with no `turn/completed` (abandoned stream, transport
  loss, interrupt deadline) leaves `complete: false`, and that thread does
  not resume;
- a `turn/completed` whose status is not `completed`, `interrupted` or
  `failed` does the same. That includes `inProgress`, the one defined
  TurnStatus that is not an ending;
- a `completed` turn with no usage is fatal;
- a `thread/tokenUsage/updated` for a turn, read after that turn's
  `turn/completed`, is dropped as a replay. The SV-0 recordings show none, so
  0.161.0 is not known to send one. If one came and the session closed before
  the next turn's usage caught `reported` up, a later resume of a thread
  whose counters Codex retained would find its baseline mismatched. It would
  then fail closed ("resume usage baseline unknown").

Stop does not stop a Codex sub-agent. `turn/interrupt` ends only the
parent's turn, and a running child goes on in the surviving session (SV-0
`parent_interrupt`). SV-4 adds per-child Stop.

A thread already saved at `complete: false` before this fix cannot be told
apart from a lost transport, so it stays unresumable.

Checks:

- `tests/v2/codex_provider_check.py`:
  - `test_a_stopped_turn_closes_its_accounting` uses the peer modes
    `stop_once` (usage, then the reply, then `interrupted`, as recorded) and
    `stop_once_quiet` (no usage). It checks the next send on the same
    session, the totals, `partial_turns` and a resume.
  - `test_a_failed_turn_leaves_the_session_to_its_cooldown`.
  - `test_an_unfinished_turn_cannot_be_certified_by_later_success` covers an
    abandoned turn and the `odd_status` peer mode.
  - `test_a_saved_partial_turn_count_is_checked`.
- `tests/v2/hud_backend_check.py`:
  `test_a_stopped_codex_chat_takes_the_next_message` runs Stop, then a send,
  then a close and a resume through the daemon.

The first, second and daemon checks fail on `main`. Four mutants are killed:
`main`'s rule, closing the accounting on any status, not counting partial
turns, and closing it only on `interrupted`.

### Partial turns are disclosed (2026-10-15 follow-up)

A Codex `usage` event carries `partial_turns`, the session's count from
`usage.json`. A turn that ends `interrupted` or `failed` yields one more
`usage` event before its `turn_finished`, with the totals already reported
and `partial_turns` raised. Its `provider_reported` is `{}`. The event is
logged in the thread's `log.jsonl`. The ledger takes the count as a
high-water delta per thread, as it takes the tokens:

- a row's `partial_turns` is the delta;
- its `partial_mark` is restored on reload.

`UsageLedger.partial_turns()` sums the deltas. `state()` puts the day's count
in `totals`, so `GET /usage` has `today.partial_turns` for every provider.
When the count is nonzero, `state()`'s reason reads
`local daily work_tokens: ≥N/limit`. The daemon publishes `usage_updated`
when the count changes, even if no total did.

The HUD's Usage panel shows "tokens may be low (≥)" under Codex while the
day's count is nonzero. Its title names the count. Codex's own weekly meter
is its report and is unaffected. `allowanceBar` (not drawn on the rail)
writes "≥ N / limit tok (may be low)". No number is added or estimated
anywhere.

A task's `tokens` ceiling (`ceiling_hit`) and the daily allowance compare
these same counts, so with partial turns they are lower bounds: a ceiling
can be passed by what Codex never reported.

Checks:

- `ledger_check.test_codex_partial_turns_are_a_highwater_count_and_a_lower_bound`;
- `codex_provider_check`: the stopped-turn event in
  `test_a_stopped_turn_closes_its_accounting`, the failed turn's event, and
  `odd_status` as both `inProgress` and `paused`;
- `hud_backend_check`: `/usage`, `usage_updated` and the log in the daemon
  test;
- `hud/src/lib/quota.test.ts`;
- the face check's `quota_checks`.

Ten mutants are killed:

- no stopped-turn event;
- `inProgress` closing the accounting;
- no high-water;
- no "≥" in the reason;
- no `usage_updated`;
- the mark not restored on reload;
- three in `quota.ts`;
- the panel's qualifier not drawn (face check).

## Sub-agents for the HUD view: the brief and the card's sid (SV-3, 2026-10-15)

- **Claude's brief**: `_task` puts `task_started.prompt` (through `_safe`,
  at most `CHILD_BRIEF_CHARS`) on that one `subagent` lifecycle event as
  `brief`. `subagents.py` keeps it on the child (scrubbed, 4000 characters,
  16 KiB JSON, snapshotted) and serves it only on the owner's transcript
  route — never `summary()` (the list), `bus()`, the thread's log or a log
  line. **Codex sends no brief** to the app-server client (SV-0's recordings:
  no prompt on `subAgentActivity`, no user message on the child's thread), so
  none is kept for a `cx:` child.
- **The card's sid**: both providers pass the asking child's sid to the
  permit callback as `subagent_sid`, only when the callback takes it (Claude
  beside `subagent=<label>`; Codex on its children's widenings, whose
  headline already names them), and the logged gate question's `subagent`
  is now the sid for Codex too (it was the label). `build_permit` puts a sid
  matching the pattern on `ApprovalRequest.subagent`, in the public JSON only
  when set. No layer reads it: `subagent_routes_check` runs every profile ×
  asker × case with and without it and compares the answers.

## The owner stops one native sub-agent (SV-4, 2026-10-15)

Plan: `docs/plans/2026-10-13-subagent-view.md` (SV-4, D4 "Stop"); protocol:
`docs/plans/2026-10-15-sv0-findings.md` (verdicts 2, 7, 12); API:
`docs/hud-api.md` "`POST /threads/{id}/subagents/{sid}/stop`".

**Decisions used** (the owner's, 2026-10-15, plan "Decisions recorded
(owner, 2026-10-15, after SV-0/SV-1/SV-2)" and open questions 4, 5):

- Claude per-child Stop is enabled on SV-0's offline evidence for CLI
  2.1.295 (`CLAUDE_STOP_VERIFIED`); no live run was required first.
- Codex per-child Stop is offered, with its caveat shown: it ends the child's
  turn; a command it is already running may still finish; the
  orchestrator's model is not told.
- The thread's own Stop keeps killing Claude's background sub-agents, and
  the HUD says so on that button.
- One click, with "stopping…" until the CLI confirms (Q4); "<name> stopped"
  is never spoken (Q5): nothing about a Stop is spoken or posted.

**Providers** (optional `subagent_stop_refusal(h, sid)` and
`stop_subagent(h, sid)`, `provider.py`; `SubagentNotLive`,
`SubagentStopUnsupported`, each raised before anything is sent):

- **Claude** (`claude.py`): `client.stop_task(task_id)` on the session's
  loop — never `interrupt()`. It needs the child live in this session, a
  `task_id` (from `task_started`) and `CLAUDE_STOP_VERIFIED`; a CLI version
  Jarvis has already read below it is refused (`_known_cli_version` reads
  the cache only: a route never spawns `claude --version`). The child is
  marked `owner_stop`, so the CLI's later `killed`/`stopped` gives
  `stop_reason` "stopped by the owner". `STOP_CONTROL_TIMEOUT_S` (10 s)
  bounds the wait for the CLI to take the request: between turns nobody
  reads the stream (SV-6), and a full SDK buffer could hold back its answer;
  past it the future is cancelled and `TimeoutError` raised (the CLI may
  still act on it). Pinned on the real provider with SV-0's
  `stop_fg_parallel` replayed through a client that waits, where the recorder
  called `stop_task`, for the provider to call it with the recorded task id:
  the stopped child ends "stopped by the owner", its sibling finishes `done`,
  the turn ends `end`, and `interrupt()` is never called. `stop_bg` pins the
  between-turns case: `stop_task` goes out at once, the child stays live, and
  the next send's reading confirms it.
- **Codex** (`codex.py`): `turn/interrupt {threadId: child, turnId: its
  running turn}` (`_ChildView.turn`), sent under `s.mutex` and registered in
  `s.child_stops` (at most `CHILD_STOPS`, 64) under that same lock, as a
  steer is, so the answer cannot be read before it is known. The turn loop's
  response branch takes it (`_child_stop_answered`) before "Codex control
  request failed": an error answer is logged, never fatal. The child's own
  `turn/completed` `interrupted` then gives "stopped by the owner" (the
  orchestrator's `interrupt_agent` still gives "the orchestrator stopped it",
  an outside `turn/interrupt` "its turn was interrupted"). Pinned over real
  pipes on SV-0's `child_interrupt` recording, with the recorder's own
  `turn/interrupt` replaced by a wait for the provider's (the peer's
  `$WAIT`; one aimed at the parent ends the parent's turn `interrupted`).

**The daemon** (`Daemon.stop_subagent`, `subagent_stop_offers`;
`SubagentRecords.begin_stop`/`stop_sent`/`stop_failed`). The route is
`owner_only` before anything else (`hud_api.subagent_route`). The session
must be open and the child live under its `subagent_key`; the record is
marked `stop: "stopping"` (written and published) **before** the provider is
asked, so a second press finds it and sends nothing. A refusal clears the
mark; a transport failure or timeout leaves `stop: "unconfirmed"`. After a
send, a timer (`STOP_CONFIRM_S`, 15 s, one per child, tokened so a late
timer never touches a newer Stop, cancelled when the CLI's word lands and at
`Daemon.stop`) turns a still-pending mark into "unconfirmed". The mark lives
only while the child is live: any end, a Codex revival, a restart's
read-back and a snapshot restore all clear it, and `prepare` never lets a
provider event set it. The list and transcript routes ask the provider
whether Stop can be sent now (`stoppable`, or `stop_unavailable` with the
sentence); the child's key rides `list(keys=True)` as `_key`, which never
leaves the daemon.

**Not reachable by agents.** Like every owner route: refused on the API
listener, the workshop and any foreign or opaque Origin; no session MCP
tool, Discord verb or CLI command calls `Daemon.stop_subagent`
(`subagent_stop_check.Reach` greps `jarvis/` for callers: only
`hud_api.py`).

Checks (free, offline): `tests/v2/subagent_stop_check.py` (19 tests:
Claude replay, Codex pipes, route, record, reach), `codex_provider_check.py`
(the peer's `$WAIT`). See the HUD half in `docs/notes/hud-workspace.md`.

**Review of #216 (2026-10-15).** The CLI's explicit refusal of a Claude stop
(the SDK raises a bare `Exception` carrying the error control response) is
`SubagentStopDeclined`: the route's 409 says "The CLI refused the stop: …"
(scrubbed by `redact.scrub_text`, one line, 120 characters) with `refused`,
and the record says `stop: "refused"` with the reason as `stop_error` (owner
routes only) — not "unconfirmed", and no "Stop again". The SDK's own control
timeout ("Control request timeout: …") is not a refusal. On our 10 s
timeout the request is left to finish rather than cancelled: cancelling skips
the SDK's own `pending_control_responses` cleanup (private state, not touched
from here); its 60 s timeout or the late answer clears it, and the late
outcome is logged. Codex: a refused `turn/interrupt` takes `owner_stop` off
the child under `s.mutex`, and every read and reset of `owner_stop` is now
under that lock too, so a later `interrupt_agent` says "the orchestrator
stopped it". Claude's `owner_stop` is likewise set under `session.mutex`. The
record refuses to begin a Stop (`"closed"`) and writes nothing from
`stop_failed`/`stop_sent` once `close()` ran. The 2.1.295 floor still allows
Stop when the CLI's version is unknown (documented; the lead raised it with
the owner). 23 tests now; six more mutants, all killed.

**Delta review of #216 (2026-10-15).** A refusal takes the owner's mark
off only when it is the last word: Claude counts the owner's Stops still on
their way (`_Child.owner_stops`) and notes one the CLI took
(`owner_stop_taken`); `_stop_settled` clears `owner_stop` for a refusal only
when neither holds. A refusal that arrives after our 10 s timeout counts too
(`_late_stop` is handed the child). In time or late, one rule settles each
answer (`_stop_outcome`): none is taken; the SDK's own "Control request
timeout" is unknown (the CLI may still act, so the mark stays); a refusal
or an undelivered request is refused. Codex keys `child_stops` by `(sid, turn)` and
clears the mark on a refused `turn/interrupt` only when no other Stop of
that turn is outstanding and none was taken, so two Stops, the first refused
and the second taken (either order), still end "stopped by the owner"; an
answer for an earlier turn settles nothing on the next. A
snapshot's `stop_error` is cleaned and capped as a live line's is. The HUD
lays the 409's reason on a row whose `refused` the bus said first, and a
"stopping…" Stop under an approval card keeps its dashed amber look (no
`button:disabled` fade). 32 tests now; twelve more mutants (two in the
HUD), all killed.

## Codex chat git tools (2026-10-15, CG-0/CG-1)

Measured offline for [the plan](../plans/2026-10-15-codex-git-credentials.md)
(§2): a Codex app-server's commands inherit `clean_env` (private HOME,
`/usr/local/bin:/usr/bin:/bin`), so neither `gh` nor the owner's gh credential
helper is reachable and an approved escalated `git push`/`gh` exits 128.
Escalated commands do run unsandboxed with the network on; plain ones have no
network. Plain sandboxed commands can still *open* `~/.config/gh/hosts.yml`,
`.env`, `~/.codex/auth.json` and Claude's credentials (workspace-write has no
read roots) — the CG-2 gap, owned by phase 2 of the plan.

Phase 1 (`jarvis/v2/chat_git.py`, `jarvis/v2/tools/chat_git_tools.py`): Codex
owner chats get `chat_git_push`, `chat_pr_create`, `chat_pr_merge`,
`chat_pr_close` (dangerous: the owner every time, no Always; merges and closes
carry merge_gate's headline) and `chat_git_fetch`, foreground only. The
permit reads the worktree's branch and origin from files, refuses what it
cannot read, and binds its ALLOW to the exact `gate_line`; the tool refuses if
the repository no longer matches. The daemon copies the branch into a private
bare mirror (`config.chat_git_mirrors_dir()/<key>`, beside V2_DATA_DIR since
phase 2) with no credential in the
environment, then pushes from the mirror to the pinned `github.com` origin
with only gh's credential helper; gh runs with `--repo` in the mirror.
Codex's environment gains only `GIT_AUTHOR_*`/`GIT_COMMITTER_*`. An escalated
raw push/fetch/pull/ls-remote to a remote, or any `gh`, from a Codex chat is
declined unasked (CG-1); the decline carries no text to the model, so the git
note says the same and the owner sees the pointer in the HUD. Tests:
`tests/v2/chat_git_check.py` (incl. the malicious repo-config suite with
positive controls) and `tests/v2/chat_git_mutants.py`.

## Codex permission profile (2026-10-16, CG phase 2)

Plan §5. Codex sessions (chats and tasks) no longer use
`sandbox_mode = "workspace-write"`, which confined writes and the network but
not reads. `codex_config.config_text` writes `default_permissions =
"jarvis-chat"` (`extends = ":workspace"`, network off), turns on
`[features] exec_permission_approvals` and leaves `request_permissions_tool`
off. Its filesystem table: "write" for the chat's validated git metadata,
the chat worktree root, the commands' own HOME (`<private home>/sandbox-home`,
set through `shell_environment_policy.set`; npm needs a writable cache) and
the session's `$CODEX_HOME/skills` (now a real folder of per-skill links,
remade each start, since Codex lists skill paths there and the model reads
them with shell commands);
"deny" for the credential folders, `~/.claude`, every entry of `~/.codex`
but `packages` and `skills`, `~/.gitconfig`, `~/.git-credentials`, `~/.netrc`,
V2_DATA_DIR (the private Codex homes and their jarvis-mcp tokens),
SESSIONS_DIR, SPILL_DIR, SPEAKER_DIR and the gate's protected files; and
case-insensitive `.env*` glob denies (depth 4) under the owner's home, the
session folder, Jarvis's checkout and the chat worktrees. `thread/start` and
`thread/resume` send no `sandbox` field. `codex_config.check_effective` reads
`config/read` (profile name, no legacy mode, the feature on and the turn-wide
one off, the exact filesystem map, network off, the command HOME) and
`check_receipt` the start/resume receipt (`activePermissionProfile`, legacy
`networkAccess` false, the write roots); anything else is `BriefRefused`.
Approvals are answered only `accept` or `decline` (`codex.APPROVAL_DECISIONS`),
never `acceptForSession` or `acceptWithExecpolicyAmendment`.

Measured on 0.161 (offline, `tests/v2/fixtures/cg2_probe.py`, a scripted
local model inside `unshare -Urn`; opens were open-and-close only), and
relied on:

- one owner `accept` of a `with_additional_permissions` network request
  gives that one command the network, still under bwrap/seccomp, with every
  deny holding and writes confined; the next command has none;
  `require_escalated` gets no network in profile mode;
- a "write" entry under a denied folder is mounted back; a "read" entry is
  not. So the chat-git mirrors moved out of V2_DATA_DIR to
  `chat_git_mirrors_dir()` (default `~/.local/share/jarvis/chat-git-mirrors`,
  `JARVIS_CHAT_GIT_MIRRORS`), readable to Codex's `git fetch <mirror>`; a
  mirrors folder inside a write root or V2_DATA_DIR is refused. pins.json
  stays in V2_DATA_DIR (hidden);
- a deny nested in a denied folder, or a glob match inside one, makes bwrap
  fail every command and every thread start, so nested denies are pruned and
  the `.env*` globs are split around denied folders
  (`codex_config.env_globs`); a deny naming a symlink also fails everything,
  so deny paths are resolved (the owner's `~/.aws` links into /mnt/c);
- `~/.codex` cannot be denied whole: the binary lives in
  `~/.codex/packages` and re-executes itself as its sandbox helper;
- a glob matches regular files only, to 4 components below its literal base,
  expanded at each command start; a glob ending in `*` reaches into
  sub-folders, so none is used for `~/.codex`.

Residuals: an accepted `require_escalated` command cannot read the skills
folder (its sandbox drops that write entry, measured); a `.env` that is a symlink, or a folder named `.env`, is not
covered by the glob; `.env.example` files are hidden too (a glob only
denies); entries added to `~/.codex` and folders added beside a denied one
after a session starts are covered from the next start. Tests:
`tests/v2/codex_provider_check.py` (the profile checks),
`tests/v2/codex_profile_probe_check.py` (the real app-server, skipped without
the CLI) and `tests/v2/codex_profile_mutants.py`.
