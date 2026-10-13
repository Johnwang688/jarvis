# Jarvis v2 — design

Status: **draft for review**, written 2026-09-15 from the redesign discussion.
Owner: John. Lead: Claude (interfaces, safety layer, integration). Codex takes
well-specified work packages (§14). This document is also the brief Codex reads
before touching anything, so it states decisions, not options — the options and
the reasoning behind each choice are in §2.

---

## 1. Why

Jarvis v1 was a hand-rolled tool-calling loop over OpenRouter, built to learn
agent internals. It did that. Three problems have not gone away with tuning:

1. **Disorganized surface.** One conversation at a time per window, no notion
   of a project, sessions as a flat list. Creating and organizing parallel work
   is harder than the work.
2. **Trivial agentic reach.** Luna scores 100% on agent-bench and 95% on
   long-bench and still only completes small tasks. The model is the larger
   cause; the harness is the smaller one. Neither is fixable by prompting.
3. **Discord is a bad remote.** Updates are infrequent, then long and wordy
   when they arrive, because the model writes them as free text.

## 1.1 Goals

- **G1.** Given a task, Jarvis executes it end to end with minimal input, and
  asks explicitly, up front, when the directions are underspecified.
- **G2.** Jarvis works independently and reaches the owner on Discord when a
  decision is needed, scoped to the task the decision is about.
- **G3.** At the desk, the interface combines the good parts of the Claude Code
  and Codex desktop apps, with the sci-fi theme kept. (Owner to elaborate;
  placeholder in §12.)
- **G4.** Progress updates are standardized, frequent, concise.

## 1.2 Non-goals

- Replacing the CLIs' own loops with a better hand-rolled one.
- Using Claude Code's leaked source (standing decision).
- Doing schoolwork or Membean submissions (standing decision).
- An adversarial security boundary against a hostile model. Confinement is
  against a *confused* agent, as in v1 and in the trading firm.

---

## 2. Decisions made (2026-09-15)

| # | Decision | Alternatives considered | Why |
|---|---|---|---|
| D1 | **Jarvis becomes a control plane; Claude Code and Codex are the brains.** Every unit of real work is a CLI session Jarvis spawns, watches, reports on. | CLIs as tools inside the v1 loop; full replacement by one CLI. | The thing deciding what to do next must be the strongest model available. A weak orchestrator spawning strong workers keeps the weakest model in charge. |
| D2 | **The OpenRouter loop survives only as the conversation fast path.** Chat, voice, quick questions. Anything resembling work is handed to a CLI thread. | Retire it; keep it as a third provider. | A CLI session costs seconds to start and subscription quota per turn; the voice surface's whole budget is latency. |
| D3 | **HUD is rebuilt as a proper frontend.** The orb, wake word, mic capture and theme are carried over as components. | Evolve `jarvis.html`. | v1 HUD is a prototype with features taped on. Projects, threads, diff panes and an approval queue are an application. |
| D4 | **Discord: one channel per project, one thread per task.** DMs remain for cross-project and intake. | DM-only; one channel for everything. | Scope. A reply in a thread steers that task; an approval in a thread belongs to that task. |
| D5 | **Workers run under the CLIs' own sandboxes and permission systems (B), one git worktree per task (D).** Firm-style full bwrap confinement is an optional *strict* profile, not the default. | A: firm-style tools-off bwrap. C: whole CLI inside our bwrap. | A throws away the native capability we are buying and forbids network. C must allow network anyway and nests Landlock inside bwrap on WSL2. |
| D6 | **Auto mode by default on both CLIs.** The owner is asked only for a short *always-ask* list (prod deploys, prod migrations, pushes to protected branches, payments, credential changes) and for anything the CLI's reviewer declines. | Ask for every dangerous call, as v1. | The classifiers are good enough that a card per `git commit` teaches approve-without-reading, which is the failure the gate exists to prevent. |
| D7 | **A classifier refusal has an escape hatch through the Jarvis harness.** If the CLI declines a command and the owner approves it anyway, Jarvis runs it itself in the task worktree and feeds the result back to the session. Jarvis's own DENY list and secrets layer still apply. | Accept the refusal. | The owner's judgement outranks a classifier, but only *explicitly*, per command, with the full command shown. |
| D8 | **Skills are `SKILL.md` directories in one repo folder, symlinked into both CLIs.** Jarvis integrations become one MCP server both CLIs load. | Per-provider copies. | One source; the same skill and the same tools work in `claude`, `codex` and Jarvis. |
| D9 | **Build order:** provider layer + project/thread model → Discord threads → roles and intake gate → HUD. | HUD first. | The owner has more to say about the HUD; the rest is settled. |
| D10 | **Lift the trading firm's Codex transport and usage ledger; do not lift its confinement policy.** | Rewrite from scratch. | `codex_rpc.py`, the resume-with-`excludeTurns` handling, the shared-login serialization and the daily admission allowance are exactly the Codex provider's hard parts. Its tools-off, no-network policy exists because nobody is awake and strategies are untrusted code, neither of which holds here. |

Standing v1 decisions that remain: no leaked source; no Membean; Discord as a
bot, never the owner's account; use-but-never-see credentials; the owner
speaks for the owner and nothing fetched does.

---

## 3. Architecture

```
                 ┌──────────────────────────────────────────────────────┐
                 │  jarvis daemon  (one process, always on, port 8405)  │
                 │                                                      │
  HUD v2 ──ws──▶ │  API (HTTP+WS)      ProjectStore   TaskRunner        │
  Discord ─gw──▶ │  Router             ThreadStore    ApprovalBroker    │
  jarvis CLI ──▶ │  Reporter           UsageLedger    Rules / Secrets   │
                 │                                                      │
                 │  Providers:  ClaudeProvider  CodexProvider  FastPath │
                 └──────┬───────────────┬──────────────┬───────────────┘
                        │               │              │
                 claude-agent-sdk   codex app-server   llm.chat (OpenRouter)
                 (spawns `claude`)  (spawns `codex`)   v1 loop, chat only
                        │               │
                 ┌──────▼───────────────▼──────┐
                 │ per-task git worktree        │   jarvis-mcp ◀── both CLIs
                 │ CLI's own sandbox + reviewer │   (memory, gmail, spotify,
                 └──────────────────────────────┘    onshape, desktop, discord)
```

**One daemon.** v1 had `jarvis face` and `jarvis daemon` as alternatives with
a lock. v2 has one long-running process that owns everything with state: the
Discord gateway, the project and thread stores, running tasks, the approval
broker, the usage ledger, the provider processes. The HUD, the Discord
gateway and the `jarvis` CLI are *clients* of it. Killing the HUD kills
nothing.

**Surfaces are thin.** A surface renders threads and tasks, sends messages,
answers approvals. It holds no agent. This is what makes the same task
visible from the desk and from the phone at once.

**Providers are the only thing that talk to a model.** Three, behind one
interface (§5). The fast path is v1's `agent.py`, kept for chat.

---

## 4. Domain model

Everything the surfaces show and everything Discord mirrors is one of these.
Stored under `~/.local/share/jarvis/` as JSON + append-only JSONL, the v1
sessions pattern, outside the repo.

- **Project.** A directory (usually a git repo) plus configuration: default
  provider per role, permission profile (`auto` / `strict` / `ask`), Discord
  channel id, allowed extra directories, always-ask additions. Created from
  any surface; creating one creates or adopts its Discord channel.
- **Thread.** A conversation. Belongs to a project (or to the *inbox* project
  for unplaced chat). Has a provider, a provider-side session id (Claude
  session id or `codex:<thread id>`), a transcript log, a title, a cost.
  v1 sessions migrate to threads in the inbox project.
- **Task.** A unit of work with a lifecycle (§10): `intake → clarifying →
  planned → running → verifying → done | blocked | failed | cancelled`. Has a
  spec, a plan, one or more worker threads, a worktree, a Discord thread, a
  status record the Reporter renders, and a final report. Replaces v1 goals,
  workflows and attended tasks, which were three copies of this idea.
- **Turn.** One user message and everything until the model yields. Emits
  events (§5.2) that the Reporter and surfaces consume.
- **Approval.** One-shot, coded, scoped to a thread or task, answered from any
  surface, first answer wins. v1's `ApprovalBroker` unchanged in kind; the
  request now carries `task_id` and `origin` so the card and the Discord
  message land in the right place.
- **Report.** The structured end-of-task deliverable (§10.4).

---

## 5. Provider layer

### 5.1 Interface

```python
class Provider(Protocol):
    name: str                                   # "claude" | "codex" | "fast"
    def start(self, thread: Thread, brief: Brief) -> SessionHandle
    def resume(self, thread: Thread) -> SessionHandle
    def send(self, h: SessionHandle, message: UserMessage) -> Iterator[Event]
    def interrupt(self, h: SessionHandle) -> None
    def answer_approval(self, h: SessionHandle, req_id: str, decision: Decision) -> None
    def usage(self, h: SessionHandle) -> Usage             # tokens, $ if known
    def close(self, h: SessionHandle) -> None
```

`Brief` is the role brief (§10.2): system-prompt append, allowed tools,
working directory (the worktree), permission profile, model, effort. A
provider maps it onto its own flags and refuses a brief it cannot honour
rather than silently narrowing it.

**Steering (2026-10-08), optional like `set_model`.** `steer(h, message)`
hands an owner message to the turn running on `h`, to be taken at the
provider's next safe point — never inside a tool batch, never as the answer
to anything the turn waits on — or raises `SteerRefused(reason,
fallback="queue"|"interrupt")`. `undelivered(h)` hands back steers taken but
never delivered (the fast path's final answer came first). Each provider
uses its CLI's own mechanism:

- **Claude** writes a stdin user message with a uuid and `priority: "next"`
  (Claude Code 2.1.295 folds `now`/`next` messages in at its next boundary,
  "The user sent a new message while you were working", and never `later`
  ones). It does so only once the turn's own CLI turn has begun, so a steer
  never reaches stdin ahead of the message it steers. Every turn message
  carries a uuid too, and `--replay-user-messages` echoes each message as it
  starts a turn, so the reader knows whose every CLI turn is: a steer the CLI
  ran as a fresh turn after the result is read inside the same turn; one
  whose turn starts later than the hold (STEER_QUIET_S of silence, any frame
  extending it, STEER_HOLD_MAX_S in all) is remembered on the session and
  absorbed by the next send, which goes in at `later` so it cannot be folded
  into that late turn. Nothing shifts the next send by one (review of PR #22:
  it used to, for good). Once the CLI is known to echo, a CLI turn nobody
  sent (a background task's notice) before a message's own echo is read but
  never taken as that message's answer. One pull-based reader per client,
  never cancelled mid-read, keeps anything the CLI says between turns. A CLI
  that does not echo leaves nothing to tell apart, and every unseen steer
  counts as folded.
- **Codex** sends `turn/steer {threadId, input, expectedTurnId}` (in both the
  0.153.4 and 0.161.0 schemas), answered on the turn loop's thread, a late
  refusal ignored. "Method not found" (or an invalid request naming
  `turn/steer`) means the interrupt fallback; 0.161.0's own steer errors queue;
  no answer within STEER_TIMEOUT counts as delivered, logged, never requeued
  — and if the turn then ends with it still unanswered, the log says it may
  not have reached the model.
- The **fast path** appends `[owner steering] …` at v1's next step boundary
  (invariant 3).

The daemon logs a steer before the provider has it; one the provider refuses
waits under the same id (`steer_queued`). A second
`send` on a busy handle is refused with a **non-fatal** error, never a fatal
one: a fatal error makes the daemon drop and close the session, which here
is the one still running.

### 5.2 Events (the one stream every surface reads)

```
turn_started · text_delta · text · thinking · tool_started · tool_finished
approval_requested · approval_resolved · reviewer_declined
plan_updated · usage · turn_finished · error
```

Providers translate their native streams into these. Nothing above the
provider layer knows what a `stream-json` line or an app-server notification
looks like. `reviewer_declined` is the event D7 hangs on. A provider's
`approval_requested`/`approval_resolved` mean only "the gate was consulted"
and never reach the bus (2026-10-08): the daemon logs them as
`gate_requested`/`gate_resolved`, and the bus's `approval_*` come from the
broker alone.

### 5.3 ClaudeProvider

- Built on the **Python `claude-agent-sdk`**, which spawns the installed
  `claude` binary. Verified against the docs 2026-09-15: `can_use_tool`
  (permission callback), `PreToolUse`/`PostToolUse` hooks, streaming input,
  `resume=`. The raw CLI's `--permission-prompt-tool` no longer exists in
  2.1.233, so the SDK is the only supported path for headless permission
  routing.
- **Which `claude` it spawns is set explicitly** (2026-10-08). With
  `cli_path` unset the SDK does *not* run the installed binary: it runs the
  CLI bundled in its wheel (2.1.273 in SDK 0.2.153), which lags the owner's
  self-updating install and refused a newer model live ("version 2.1.280 or
  newer is required"). `providers/claude.py:resolve_cli()` picks, once per
  process: `JARVIS_CLAUDE_CLI` if it is an absolute path to an executable (a
  relative one is refused: it is checked against the daemon's cwd but the SDK
  spawns it with `cwd=<task worktree>`, where a planted `bin/claude` would run
  outside the `PreToolUse` gate; an explicit `/mnt/` or `.cmd` path is used,
  with a warning); else the first executable `claude` on an absolute PATH
  entry that is not under `/mnt/` (never the Windows npm shim, nor a
  `.cmd`/`.bat`/`.exe` behind a symlink); else `~/.local/bin/claude` (a
  systemd unit's PATH rarely has it); else the bundled CLI, with one warning,
  and the search is repeated at most every 60 s so a later install needs no
  restart.
  Every `ClaudeAgentOptions` — start, resume and `set_model`'s reconnect —
  carries it. The version is not pinned. `health()` probes `--version` once
  per real binary rather than per `/status`; a failed probe is not cached and
  is retried after 60 s. The version is judged against a floor:
  `CLAUDE_MIN` (2.1.280, what `claude-opus-5-5` requires — so the bundled
  2.1.273 reads as unhealthy instead of healthy-but-failing) or newer is
  healthy, 2.2 and 3.x included, and anything past `CLAUDE_VERIFIED` (2.1.295)
  logs one warning per process. An older or unparseable version is not-ok.
  `JARVIS_CLAUDE_STRICT=1` restores the old major.minor match. `/status`
  reports the CLI's path and version under `providers.claude.cli`.
- Permission mode `auto`. **Jarvis's rules (§6) live in a `PreToolUse`
  hook, not in `can_use_tool`** — verified 2026-09-15 (R2): under `auto`
  the classifier settles every call and the callback is never consulted,
  while the hook fires for each tool use. The hook evaluates layers 1, 2 and
  4 of §6 and returns `permissionDecision: deny` (with the reason the model
  sees) or `allow`; for an always-ask tool it blocks on the broker and
  returns the owner's answer. The hook's timeout must therefore be at least
  the broker's (10 minutes remote), or a slow owner reads as a deny.
- One session per thread, `resume` across daemon restarts.
- Worktree via the SDK's `cwd` pointed at the task worktree (Jarvis creates
  it, §7), not the CLI's `--worktree`, so the path is the same on both
  providers.
- `--strict-mcp-config` with Jarvis's own MCP config, so a worker loads
  `jarvis-mcp` and the owner's project MCP servers, and nothing else by
  accident.
- **Verify first (R1):** that the SDK runs on the CLI's subscription login
  with no `ANTHROPIC_API_KEY`. The docs say the CLI's login is used by default
  and that third parties may not *offer* claude.ai login in their products.
  This is the owner's own machine driving the owner's own Claude Code, which
  is the ordinary case, but it is confirmed by running it, not by reading.
  Fallback is API-key billing for workers.
- **Verify (R2):** that `can_use_tool` is still consulted under `auto` mode
  for the always-ask list, or whether that has to be a `PreToolUse` hook
  returning `ask`. The design tolerates either.

### 5.4 CodexProvider

- Built on the **app-server JSON-RPC protocol over stdio**, lifting
  `firm/codex_rpc.py`, `codex_config.py` and the usage accounting from
  `~/projects/jarvis-trading-firm` (D10). ~~Pinned to `codex-cli 0.153.4`~~
  **A version floor, not a pin (2026-10-08):** the CLI auto-updates, and the
  exact pin turned the update to 0.161.0 into "Could not send" for every
  Codex thread. `providers/codex_cli.py` now accepts `CODEX_MIN` (0.153.4)
  and newer, warns once per process above `CODEX_VERIFIED` (0.161.0), and
  refuses older; `JARVIS_CODEX_STRICT=1` restores an exact match against the
  verified versions. The binary is `JARVIS_CODEX_CLI`, else the first `codex`
  on PATH outside `/mnt/` (the Windows npm shim must never run), else
  `~/.local/bin/codex`; its realpath is version-checked and launched, so an
  update between the two cannot swap it. The version check before each
  session stays. The 0.153.4 → 0.161.0 protocol diff is
  `docs/codex-briefs/codex-0.161-protocol-notes.md`.

  **Deliberate differences from the Claude resolver (#14),** so nobody
  "aligns" them by accident: an override that is relative, under `/mnt/`,
  `.cmd`/`.bat`/`.exe`, or not executable is **refused** (health fails with a
  sentence) where Claude warns and falls back or uses it — Codex has a
  sandbox to protect and no bundled CLI to fall back on, so a wrong binary
  must be loud. Codex **launches the realpath** it version-checked where
  Claude launches the symlink (Claude's SDK spawns later and the updater
  re-points the link; for Codex the check and the spawn are moments apart and
  must be the same file). And Codex **resolves per session** (every `_open`
  and every `/status`) where Claude resolves once per process, so an update
  is picked up by the next Codex thread without a restart.
- **What actually keeps the floor from loosening the gate** (corrected
  2026-10-08 after review — the first draft claimed "anything not understood
  gets an error or a decline", which was not the whole truth). A Codex
  approval reaches us only *after* its `auto_review` reviewer passed it, and
  under AUTO `permit`'s layer 5 answered ALLOW, so a sandbox-widening grant
  riding on an approval (extra permissions, network, a grant root, terminal
  input) was accepted with no human — and the `{decision}` reply cannot strip
  it. Now: unknown server methods get an error; malformed approvals and
  unknown `kind`s are declined unasked; **a widening approval is always asked
  of a human** (`permit(..., widening=...)`, no Always, headline "SANDBOX
  WIDENING: …" first on the card and the Discord post; deny-all, timeout or
  strict declines), and so is one carrying a field outside the verified
  schema. The headline is one cleaned, capped line on every surface (each
  part through `approvals.clean_line`, ~400 overall, markdown escaped for
  Discord, kept inline when the post overflows). Layer 1 judges what a
  widening opens: a grant that is or contains protected state, a
  SELF_PROTECTED file, or touches a credential directory is DENY unasked
  (`permissions.denied_grant`). A command that differs from its item is
  declined only for a plain approval; for `writeStdin` and `approvalId`
  subcommands both commands are judged and shown, and a human asked. A
  `command: null` can no longer erase the item's real command, and
  a command approval with none is declined; `provider.answer()` can only
  deny an approval. A plain in-sandbox approval under AUTO is still accepted
  on the reviewer's word — that is R8, not a regression.
- Approval requests `item/commandExecution/requestApproval`,
  `item/fileChange/requestApproval`, `item/permissions/requestApproval` are
  **routed to the broker**, where the firm denies them. `requestUserInput`
  becomes a clarification question (§10.1). Unknown server requests still fail
  closed.
- Sandbox `workspace-write`, approval policy `on-request`, and Codex's own
  automatic reviewer (`--approve-for-me` / `approvals_reviewer =
  "auto_review"`, already in the owner's config) as the auto-mode analogue.
  On the CLI, `--approve-for-me` *implies* the workspace-write sandbox and
  refuses an explicit `-s` beside it (verified 0.153.4); over the app-server
  the two are separate fields and the provider sets both.
- Native shell, file, web and MCP tools **enabled** (the firm disables them).
- Thread per Jarvis thread; resume with `excludeTurns=true`.
- Shared-login serialization and the daily admission allowance carried over as
  the Codex half of the UsageLedger (§8).

### 5.5 FastPath

- v1 `agent.py` + `context.py` + the safe half of `tools/`, OpenRouter model,
  `models.tier()` selection, unchanged.
- Used for: HUD voice turns, Discord chat that is not a task verb, quick
  questions in any thread.
- **Escalation rule**: it may not edit files, run commands or browse. If a
  turn needs any of that it calls `task_propose(brief)` and the Router opens
  a task on a CLI provider. The full decision procedure, the structural
  boundary that makes a misjudgement harmless, and the step-budget rule are
  §8.1.

---

## 6. Permissions

Five layers, evaluated in this order for every tool call from every provider.
The first layer to answer wins.

1. **DENY** — v1 `rules.py`'s never-approvable set (sudo, dd, `rm -rf` at a
   root, fork bombs, raw block writes, and since ENV-1 a shell write onto a
   credential env file) and the secrets layer (every credential env file —
   `.env`, `.env.<anything>`, not `*.example` — token bundles, the allowlist
   file), for file tools, read tools (Claude's Read, Grep, Glob,
   NotebookRead) and sandbox grants that are or hold one alike. Not
   approvable by anyone, from any path, including the escape hatch.
2. **ALWAYS-ASK** — a short, explicit list that goes to the owner regardless
   of the reviewer: prod deploys (`vercel --prod`, `gh release`, anything
   the project marks as a deploy), prod database migrations, pushes to
   protected branches, payments and purchases, credential creation or
   rotation, outward-facing sends (`gmail_send`, `discord_send` to non-owner
   channels). Per project, additions allowed, removals not.
3. **CLI reviewer** — Claude auto mode / Codex auto_review. Decides the grey
   zone. An approval: runs. A decline: emits `reviewer_declined`. On Claude,
   layers 1, 2 and 4 run in a `PreToolUse` hook *before* the classifier sees
   the call (R2); on Codex they run in the app-server approval handler
   for layers 1 and 4, and layer 2 is open — see R8.
4. **Jarvis ALLOW** — v1 rules' ALLOW verdicts and the owner's persistent
   allowlist, honoured only for a human-backed approver (v1 invariant kept;
   a strict-profile task has a deny-all approver and nothing auto-runs).
5. **Human** — the broker: HUD card, Discord thread message, CLI prompt.
   Denies on every failure path, as before.

### 6.1 The escape hatch (D7)

```
reviewer_declined(cmd)
  → broker.request(task, "reviewer declined: <exact cmd>", origin=task)
  → owner: no   → the worker is told "declined, not run", continues
  → owner: yes  → rules.verdict(cmd) is DENY?  → refused, owner told why
               → else Jarvis runs cmd itself: task worktree as cwd,
                 v1 run_command path (secrets scrub, output cap, spill)
               → result is sent to the worker session as the next user
                 message: "[owner ran: <cmd>]\n<output>"
```

The DM and the card show the **entire command**, never a summary. The
decision log records `reviewer-declined-owner-ran`. No allowlist entry is
minted from this path: a command the reviewer refused is not one to
auto-approve next time.

### 6.2 Profiles

- `auto` (default): as above.
- `ask`: layer 3 is skipped; every dangerous call reaches the owner (v1
  behaviour). For projects the owner marks sensitive.
- `strict`: firm-style. Provider native tools off, Jarvis tools only, bwrap
  with no network, deny-all approver. For a task marked unattended-and-
  untrusted. Only the CodexProvider supports it at first (the firm's code).

---

## 7. Workers and worktrees

- **Worktree per task**: `git worktree add <repo>/.jarvis/worktrees/<task>`
  on branch `jarvis/<task-slug>`. Created by Jarvis before the first worker
  starts, so both providers see the same path. Not the CLI's `--worktree`.
- A task's workers all share its worktree; parallel tasks never collide.
- Done means: branch pushed or merged as the spec says, or left for the owner
  to review with the diff linked from the report. A cancelled or failed task
  leaves its branch; Jarvis never deletes a worktree with uncommitted changes
  unless the owner says so.
- Non-git projects get a plain directory and no branch; the report says so.
- The CLI's own sandbox does the process confinement. Claude's bubblewrap
  sandbox is enabled via settings for headless runs (**verify on WSL2, R3**);
  Codex's `workspace-write` Landlock sandbox is default.

---

## 8. Brain selection and routing

Two decisions, made in two places, both deterministic and both logged. The
first picks **which brain handles a message** (fast path or a CLI task). The
second picks **which provider runs each role of a task**. Neither is left to a
model's judgement alone: the structural boundary in 8.1 makes a wrong call
harmless, and the policy table in 8.2 makes a routing choice reproducible.

### 8.1 Stage one: fast path or task

The Router runs these rules in order on every incoming message. The first
match wins.

| # | condition | goes to |
|---|---|---|
| 1 | a verb: `yes/no/always <code>`, `status`, `cancel`, `steer:`, `projects`, `tasks` | the Router itself; no brain |
| 2 | explicit task intake: `task:` prefix on Discord, the HUD's New Task, `jarvis task "…"` | a new task (§10); the orchestrator brain |
| 3 | the message is in a **task thread** (Discord thread, HUD task view) | that task's orchestrator thread, as steering |
| 4 | a voice turn on the HUD, or any message in a chat thread | the **fast path** |

Rule 4 is where judgement enters, and it is bounded structurally: **the fast
path has no tool that can change anything.** Its toolset is read-only files,
`grep_files`, a single-page `fetch_page`, memory, threads/sessions read,
`task_status`, and one new tool, `task_propose` — plus, since WP-F and only
for the owner's own chat turn typed in the HUD, the read-only
`terminal_read` (§18). It cannot edit, run a
command, browse interactively, or spawn anything. So a fast-path turn that
misjudges a request as chat can only *answer badly*; it cannot half-do work.

The fast path's prompt carries the escalation test, and the loop enforces the
budget half of it:

- **Propose a task when** the request needs a file edit, a command, more than
  one page fetched, a browser, a deliverable (a file, a PR, a report), or
  cannot be finished in the fast path's step budget (8 steps). Any one of
  those is sufficient.
- **Answer directly when** it is a question answerable from memory, the
  transcript, one read or one fetch; a control request (mute, avatar, music);
  or conversation.
- **Budget rule:** a fast-path turn that exhausts its steps with the request
  unmet does not report "stopped"; the loop makes the proposal itself from
  the transcript. The v1 exhaustion handoff, pointed at a task instead of at
  the owner.

`task_propose(brief, project=None, provider=None)` returns a task id. The
Router places it: explicit `project` → that one; otherwise the thread's
project, the Discord channel's project, or the HUD's selected project; if
none, the fast path is told to ask which. The reply to the owner is one line
("Opened task 12 in Jarvis: convert the skills folder. It will ask if
anything is unclear."), and a `cancel 12` in the next 60 seconds withdraws it
before any CLI session starts. A `provider` named in the brief ("use codex")
is an owner override carried into 8.2.

There is no path in the other direction: a CLI task never answers voice, and
a task thread's messages never reach the fast path.

*Settled by WP2 (2026-09-15):* the fast path's toolset is `FAST_TOOLS` in
`jarvis/v2/providers/fastpath.py`, validated at import so it cannot shrink
silently. v1's `task_*` tools are excluded (they are the v1 attended-task
mechanism, not the v2 task model); the v2 `task_status` read tool joins the
set when the daemon (WP7) provides it. Three `Brief` fields have no v1
counterpart and are ignored by this provider — `effort` (v1 resolves it per
model), `max_turns` (the budget here is steps) and **`cwd`** (v1 file tools
resolve against the process directory, so a chat thread reads the daemon's
tree, never a task worktree; read-only, so the cost is a wrong answer, not
a wrong write). `usage()` reports dollars only: v1 keeps no token total, and
a wrong count is worse than none (the BYOK lesson). WP9's ledger must
therefore not require tokens from the fast path.

*Amended 2026-10-06 (decisions A1, A4, A5): three providers per chat thread.*
Rule 4 now reads "any message in a chat thread → **that thread's provider**".
A chat thread is opened on OpenRouter (the fast path, as above), **Claude**
or **Codex**, chosen in the HUD while composing and fixed by the first
message, because a provider session cannot change provider. So "the fast path
cannot change anything" still describes the fast path, and **no longer
describes every chat thread**: a Claude or Codex chat thread is a full agent
working in the folder the thread was opened in (its `cwd`, the project root
at open), with that provider's whole native toolset and the chat role's
brief (`roles.BRIEFS[Role.CHAT]`). It stays behind the same gate as every
task worker — the project's permission profile and always-ask list on its
brief, the §6 permit built by `build_permit` (Claude's `PreToolUse` hook,
Codex's approval handler), and approvals that reach the owner through the
daemon's broker. Nothing runs ungated; Codex still refuses a brief it cannot
enforce (an `ask` profile or always-ask additions, R8), loudly. There is no
task, worktree or reviewer around a chat thread: that is the owner's choice
to make per thread, and the fast path remains the default.

The model and effort of a chat thread live on the Thread record and change
from the next message (`PATCH /threads/{id}`); `brief.json` is never
rewritten. The daemon hands the provider the thread's effective choice when
it opens or resumes the session and, at the start of each turn, calls the
provider's `set_model` if the choice moved. A default thread therefore
follows the global Model picker on every turn (it used to keep the model it
was opened with until the daemon restarted). Claude and Codex default
threads follow their provider's HUD default the same way (2026-10-08,
`POST /thread-models`): that default, when set, beats the built-in Opus 5.5
and, for Codex chat threads, routing's orchestrator default — which still
decides every task role. The WP2 note above changes in
one place: the fast path **honours `effort`** now (`Agent.effort`; `None`
keeps v1's per-model resolution, so v1 surfaces are unchanged), and the
v2 default is `high` within the model's own ladder, or the roster's pin for
that model (A4) — v1's global `JARVIS_REASONING_EFFORT` is left alone. How
each provider switches: the fast path reads the pair at the next turn's
start; Codex sends `model`/`effort` with the next `turn/start` (which
override that turn and the ones after it); Claude disconnects and resumes the
same session with the new options, and puts the old client back if that
fails.

### 8.2 Stage two: which provider runs each role

When a task starts, and again whenever it needs a new worker thread, the
Router resolves `role → provider` through this ladder and records the result
with its reason on the task's status record.

1. **Owner override** on the brief or in a steer (`use codex`, `use claude`).
   Applies to every role of that task unless the override names a role.
2. **Project routing table**, `project.routing[role]`, if set.
3. **Global defaults** (`~/.config/jarvis/routing.json`; initial values
   below).
4. **Capability filter.** A candidate is dropped if it cannot honour the
   brief: `strict` profile → Codex only (the firm's confinement exists there);
   vision needed (a screenshot, a whiteboard image) → a provider whose
   configured model accepts images; a required MCP server the provider
   cannot load.
5. **Availability filter.** A candidate is dropped if the UsageLedger says
   *unavailable* or *over threshold* (8.3), or the health check fails (CLI
   missing, version outside the pin, login expired — each a stated reason).
6. **Fallback chain** for the role, walked in order over what survived 4–5.
7. **Nothing survived** → the task blocks with a `blocked` milestone naming
   every candidate and why each was dropped. It never falls through to the
   fast path.

Initial global defaults, to be confirmed by the owner:

| role | chain | model / effort |
|---|---|---|
| orchestrator | claude → codex | Claude: the roster selection; Codex: `gpt-6-astra` / `xhigh` |
| implementer | codex → claude | Codex: `gpt-5.6-sol` / `high`; Claude: roster selection |
| reviewer | claude → codex | as orchestrator |
| researcher | codex → claude | as implementer |

Two preferences applied after the ladder, never overriding it: **the reviewer
prefers a different provider from the implementer** (two model families miss
different things), and **all roles of one task prefer the same provider when
one is over threshold**, so a task does not straddle a limit that is about to
close. A model and effort are per role and per provider, settable from the
same routing file; the picker in the HUD edits the Claude entry (the v1
model selector, re-homed).

### 8.3 What availability means

The UsageLedger keeps, per provider: tokens per task and per day (uncached
input plus output, the firm's `work_tokens`), dollars where reported
(OpenRouter, BYOK upstream cost — the v1 lesson: a number that arrives as
zero is not zero), and the provider's own signals. Every usage event carries
what that one result spent: Claude's figure arrives as the CLI's running total
for the conversation (continued, not restarted, by a resume), so the provider
turns it into a per-result delta before it reaches the ledger or a thread's
total (2026-10-12, `running_cost.py`); Codex's cumulative counters are
differenced the same way against the session's baseline. From those it
publishes one of four states, shown on the HUD's SYSTEMS panel and in
`status`:

- **available.**
- **over threshold** — above the project's or global `no_new_work` fraction
  of the visible window (initial: 85%). No new tasks or worker threads start
  here; running threads finish. The visible window is whatever the provider
  actually reports (R6); until that is verified it is the local daily
  allowance, the firm's mechanism.
- **cooling** — a rate-limit or capacity response in the last N minutes
  (initial 15; the firm's per-model cooldown). Treated as over threshold.
- **unavailable** — health check failed, with the reason.

### 8.4 Once a thread is running

A thread's provider is pinned at start and does not change. The rules for
what happens when its provider stops being available:

- **A worker thread** (implementer, reviewer, researcher): the running turn
  finishes or fails on its own. The next worker thread the task needs is
  routed fresh through 8.2, which will land on the fallback. The status
  record shows the change and the reason.
- **The orchestrator thread**: the task blocks. Moving an orchestrator loses
  its conversation, so it is an owner decision, offered in the blocked
  message as `resume 12 on codex`. On yes, a new orchestrator thread starts
  on the fallback from the durable state — spec, plan, status record, worker
  reports — not from the old transcript. Everything the new orchestrator
  needs to continue is, by construction, on disk (§10.3).
- **Ceilings** (dollars, hours, per-task tokens) park the task as in v1
  goals, spend shown; `resume` requeues it on the same provider if available.
- **An owner message while a chat turn runs** (2026-10-08) is steered into
  it or queued behind it on the same provider, never refused and never
  routed elsewhere (§5.1 for the mechanism, §18 for the surfaces). A task's
  threads are unchanged: the owner steers a task through the task verbs.

### 8.5 Owner controls

- `jarvis route` — print the effective table, ledger states, and the last
  ten routing decisions with reasons. `jarvis route set <role> <chain>`
  globally or `--project`. The same view and controls in the HUD.
- `use <provider>` in a brief or steer, as above.
- Every status embed and the HUD task view show the routing line, e.g.
  `orchestrator: claude · implementer: codex (claude over threshold)`. A
  routing decision the owner cannot see is one they will assume was wrong.

---

## 9. Skills and tools across providers

### 9.1 Skills

- Format: **`skills/<name>/SKILL.md`** in the Jarvis repo, YAML frontmatter
  (`name`, `description` written as a trigger, optional `allowed-tools`),
  markdown body. This is the Agent Skills format both CLIs load.
- `~/.claude/skills/<name>` and `~/.codex/skills/<name>` are **symlinks** into
  the repo folder, created by `jarvis skills link` (idempotent; refuses to
  replace a real directory). `~/.codex/skills` currently holds only
  `.system`; the v1 flat `.md` files are converted by a one-time script.
- Jarvis's own `skill_read`/`skill_write`/index read the same folders.
- Skills that drive Jarvis-only surfaces (`whiteboard`, `self-improve`)
  declare it in frontmatter so a bare `claude` session knows to skip them.

### 9.2 Tools: `jarvis-mcp`

One MCP server (stdio), started by the CLIs from the MCP config Jarvis
writes, exposing the integrations that only Jarvis has: memory
(search/write), gmail, spotify, onshape/CAD, desktop control, `discord_dm_owner`,
sessions/threads read, `task_status`. Each tool keeps its v1 `dangerous`
flag, expressed as the MCP server asking the daemon's broker before acting,
so a CLI worker calling `gmail_send` produces the same card as v1 did. The
server talks to the daemon over its local API; it holds no credentials of its
own.

---

## 10. Tasks, roles and the intake gate

### 10.1 Lifecycle

```
intake       owner gives a brief (any surface). Router assigns a project
             (asks if it cannot).
clarifying   the orchestrator writes a SPEC: goal, deliverable, acceptance
             criteria, constraints, and OPEN QUESTIONS each marked blocking
             or assumable. Blocking questions are asked — in the task's
             Discord thread and on the HUD — and the task waits. Assumable
             ones are stated as assumptions in the spec and proceed.
planned      a PLAN with numbered steps, the verification step included.
             Posted as the status message's plan section.
running      implementer worker(s) execute in the worktree.
verifying    a separate reviewer worker, fresh thread, checks the
             deliverable against the acceptance criteria. Failing → back to
             running with the findings; the second failure blocks.
done         REPORT posted; branch/deliverable linked.
blocked      needs an owner decision; the status message says exactly what.
failed       ceiling hit or unrecoverable error; spend and last state shown.
```

The clarification step is what buys G1, and it has a rule: **a task may not
leave `clarifying` with a blocking question unanswered**, and a question may
be marked blocking only if two reasonable answers lead to materially
different work. The v1 lesson from the VEX goal applies: acceptance criteria
go into the spec before any implementation, or "done" is unpinned.

### 10.2 Roles

A role is a brief: system-prompt append, tool allowance, default provider,
step/turn cap. Never the same session as another role on the same task.

| role | does | tools | default provider |
|---|---|---|---|
| orchestrator | intake, spec, plan, dispatch, reporting; one per task | read-only + task control + jarvis-mcp | Claude |
| implementer | executes plan steps in the worktree | full CLI native + jarvis-mcp | Codex |
| reviewer | verifies deliverable vs acceptance criteria; never edits | read-only + test running | Claude |
| researcher | reads, browses, summarizes; returns findings only | read + web | either |

The orchestrator does not implement. The v1 agent-bench finding that every
non-Luna model "did it inline instead of delegating" is the failure this
separation makes structural rather than prompted.

### 10.3 Status record → Reporter

The runner, not the model, maintains the status record: phase, step `i/n`,
elapsed, cost, last tool, last file touched, open question, worker provider.
The Reporter renders it to each surface. Model prose enters only as the
spec, the plan text, questions, and the final report, each capped.

### 10.4 Report schema

```
DONE      what was delivered, one line each
CHANGED   files / branch / deploy URL
VERIFIED  how, by whom (reviewer thread id), result
OPEN      anything not done, and why
NEXT      the one thing the owner should do now, if any
COST      tokens/$ per provider
```

Same shape as v1's exhaustion handoff, which worked. Hard cap 1500
characters on Discord; the HUD shows the full version.

---

## 11. Discord

### 11.1 Topology (D4)

- Guild: the owner's private server. Bot already a member.
- **Channel per project**, created or adopted when the project is created;
  id stored on the project.
- **Thread per task**, created in the project channel when the task enters
  `clarifying`; id stored on the task. The bot is a member by creation.
  Archived threads reopen when the bot posts.
- **DMs** for: intake without a project, `status`, `projects`, `tasks`, and
  anything the Router cannot place. Voice notes stay DM-only (they cannot
  carry a mention; unchanged).

### 11.2 Routing

- In a **task thread**: any owner message is addressed to that task, no
  mention needed. Verbs (`yes/no <code>`, `steer: …`, `cancel`, `status`)
  are parsed first. Then, **if the task is `clarifying` with an open
  blocking question, typed text answers it** (`control.answer_question`,
  through the same `_answer` as `/answer` — S1, bug 2); a voice note never
  answers one. Anything else is a steering message to the orchestrator.
- In a **project channel** (not a thread): `task: …` opens a task; other
  owner messages go to the project's chat thread on the fast path.
- Everywhere: owner only, typed only for approvals and verbs (the v1
  spoken-turns-never-authorize rule stands).
- Approval codes are accepted **only in the thread the request was posted
  in**, the v1 same-channel rule applied to threads.
- **Keywords are on their way out (S1 → S2, decisions S-3).** They still
  work for one release, and every keyword reply ends with a nudge such as
  "(next time: `/cancel`)". S2 removes them and keeps a tripwire that holds
  the old forms without acting.

### 11.3 Update protocol (G4)

- **One status message per task, edited in place.** An embed: title, phase,
  step `i/n`, elapsed, cost, last action, open question. Edits coalesce to at
  most one per 5 s per channel (Discord's edit rate limit).
- **Milestone messages**, new posts, fixed templates, ≤ 400 characters:
  `started`, `question` (blocking, with the choices), `approval` (full
  command + code), `blocked`, `verified`, `done` (the report), `failed`.
- **Nothing else is posted.** Tool chatter, thinking and interim text stay
  on the HUD and in the log; `task_log` is a verb for when the owner wants
  them.
- Long content (a diff, a report over the cap) is attached as a file, not
  split across messages.
- **Approval posts carry Approve and Deny buttons (S1).** Never an Always
  button: a standing rule is the typed `/always` only (decisions S-2), and
  `/always` is offered and accepted only where `permissions.entry_for` can
  mint one. A press must match the owner, the posted message id and its
  channel; the custom id carries the 4-character code, never the broker's
  request id. Whatever resolves the request — HUD, timeout, slash, button —
  strips the buttons.

### 11.4 Slash commands (S1, 2026-10-07)

Plan: `docs/plans/2026-10-07-slash-commands-plan.md`; owner decisions S-1..S-3
in `docs/plans/2026-10-07-discord-decisions.md`.

- **One registry, static code.** `jarvis/v2/commands.py` holds `Opt`, `Cmd`,
  `REGISTRY`, the completers and `invocable_skills()`. No skill, project,
  task or code ever enters the *registered* payload; they reach the owner
  only as autocomplete suggestions, and every handler re-checks what it is
  given. S1 registers eleven: `/task` (an empty brief opens a multi-line
  modal), `/status`, `/cancel`, `/steer`, `/answer`, `/yes`, `/no`,
  `/always`, `/resume`, `/skill`, `/project list`. The rest of `/project`
  and `/channel` arrive with B1/B2 — no stubs.
- **Registration is global** with `contexts [0, 1]`, `integration_types [0]`
  and `default_member_permissions "0"` (guild commands never appear in a
  DM). The daemon syncs once, on the first READY (which carries the
  application id): GET, diff, and one bulk PUT only if they differ — never a
  DELETE. `jarvis discord commands [--check | --sync]` is the human CLI.
  `/applications/` is spelled only in `jarvis/v2/discord/commands.py`; a test
  greps the tree.
- **The gate, on every interaction, autocomplete included:** our
  application id; the owner (`member.user.id` / `user.id`); `context` 0 or
  1; the configured guild once B1 sets one; then the place — an archived
  project's channel refuses, a channel Jarvis does not own refuses, and
  `Cmd.places` is enforced on the server.
- **The 3-second rule.** Read-only refusals answer at once (type 4,
  ephemeral); autocomplete answers at once (type 8, ≤ 25). Everything else
  sends type 5 **before** its first store write or control call, then edits
  `@original`; past 14 minutes or on a 401/404 a public reply becomes a
  channel post. A private one never does: its content is dropped and one
  short public pointer ("That reply expired — run `/status` again") is
  posted instead. Lookups (`/status`, `/project list`) and refusals are
  ephemeral; actions are public. An unexpected exception after the deferral
  says "That failed (`<Class>`); it may not have run." — never "Done." — and
  a malformed id is an "I don't know" reply, never silence. A button or modal
  with no `context` derives it from `guild_id` or a DM-typed channel; a
  command never does, and a group DM is refused either way.
- **Button posts survive a restart as plain posts.** The approval-post map
  is also kept in `<v2 data>/discord/approval-posts.json`; on start, every
  post the previous process left open loses its buttons (its request died
  with that process — the broker denies everything at shutdown).
- **One implementation per verb.** Verbs take a `Reply` sink —
  `ChannelReply` for typed messages, `InteractionReply` for commands — so
  the keyword and slash paths cannot drift.
- **`/skill`** suggests the repo skills minus `jarvis-only`. In a DM or
  project channel it is a chat turn with `UserMessage.skill` set: the fast
  path injects the body (refused above 8k characters), Claude and Codex get
  the directive `Use the "<name>" skill for this request.` (a headless
  `/<name>` prompt and a Codex skill input item are unverified spikes). In a
  task thread it is a steer. The thread log records `skill`.
- **Never logged:** the interaction payload and its token. `DiscordRest`
  redacts the bot token and every interaction token from its errors, and no
  error carries a path. `GET /discord` reports `{connected, commands:
  {state, count, synced_at, error}}` — states only.
- `"discord_"` is a forbidden fast-path tool prefix.

### 11.5 The update poster (PR A, 2026-10-07)

Plan: `docs/plans/2026-10-07-discord-plan.md` §1; owner decisions D1, D6, D8
and O1 in `docs/plans/2026-10-07-discord-decisions.md`. Code:
`jarvis/v2/discord/{surface,reporter}.py`.

- **One `DiscordSurface`** owns `DiscordRest`, the router (with S1's
  interaction router) and the `Reporter`; `start_discord(daemon)` returns it
  and sets `daemon.discord`. **Start:** `daemon.start` → the Reporter
  subscribes → `runner.serve()` → the gateway, so a task the runner re-admits
  at boot is heard. **Stop:** gateway → runner → `reporter.close(flush=True)`
  (≤ 2 s) → hatch → daemon. Both are idempotent.
- **The thread is created at the first snapshot whose phase is not
  `intake`** (D6) — a proposal withdrawn in its grace window never gets one —
  with `auto_archive_duration=10080`, the owner added as a member, then
  "Started" and the status card.
- **What notifies (D1).** Every guild post carries `SUPPRESS_NOTIFICATIONS`
  (4096): the card and its edits (≤ 1 per 5 s per thread), Started,
  Verified, Cancelled (D8), and every milestone's own text. Question,
  approval, blocked, failed and done are followed by a separate post whose
  content is exactly `<@owner>` with `allowed_mentions {"parse": [],
  "users": [owner]}` (`DiscordRest.ping_owner`) — the only thing that pings.
  A DM gets neither the flag nor the ping line. Milestones ≤ 400 characters;
  the report ≤ 1500 with the rest as `report.txt`. A question comes from
  `task_question` (or a new `open_question` on a snapshot), with its choices
  and "Answer here or with `/answer`", once per question.
- **The DM is the safety net (O1).** A project with no channel (all of them
  until B1), or a channel Discord calls broken (10003, 50001, 50013), sends
  the attention milestones — question, blocked, failed, done with its report
  — to the owner's DM, prefixed `[<project> · task <id>]`. A 50013 also DMs
  the owner once per channel per 24 h. A deleted thread (10003) is marked
  `thread_gone` and never recreated. Approvals do the same: the task thread
  first, and if that post fails the DM is asked *and recorded as where it
  was asked*, so only the DM's answer counts; the DM post enters S1's
  persisted approval-post map like any other.
- **Restarts.** `tasks/<id>/discord.json` holds the thread and message ids,
  `phase`, `open_question`, `embed_sha` and `thread_gone`, and is read
  before `Task.discord_thread_id`. `TaskStore.save` never replaces a stored
  thread id with None (bug 1: `worktrees.ensure` saved a stale copy across
  `git worktree add`). The worker reconciles from disk before its first
  event, again when the bus drops events, after the breaker, and shortly
  after a transient failure (or an unreachable DM): a task **created before
  the poster started** and never seen is **seeded silently** (no DM storm) —
  one created later, during an outage or behind dropped events, is reported
  late instead — active channel tasks get a thread and a card at ≤ 1 create
  per second, pre-existing terminal tasks are never backfilled, and a stale
  `embed_sha` gets one edit. The pass reads ids and skips an unreadable
  task, so one bad `task.json` cannot stop it. A milestone is marked done
  only once delivered (or refused for good), so a missed one is posted
  late, never twice. The thread id is written to the task record before the
  sidecar (sidecar writes are best-effort); a create POST that succeeds at
  Discord but whose reply is lost can still make a second thread — POST is
  not idempotent.
- **Failures.** `DiscordHTTPError` carries `status`, `code` and
  `retry_after`; a 429 is slept at most 10 s *in total* per call (and
  `close()` interrupts the wait), longer raises and the caller defers. Edits
  go first and reopen a thread only on 50083. Only transient failures
  (transport, long 429, 5xx) are retried; any other refusal of a thread
  create or an attention post goes to the DM. Three transient failures open
  a breaker: 30 s, 60 s, doubling to 5 min, then a reconcile; events are
  deferred only while it is open. A broken-place entry clears when its task
  ends and ages out after an hour. 50001 and 50013 DM the owner once per
  channel per 24 h, stamped only once sent. Logs name the operation, HTTP
  status and Discord code — never a URL, a body or a token — and the
  daemon's `configure_logging()` holds httpx, httpcore, urllib3, websocket,
  anthropic and openai to WARNING, because httpx logs every request URL at
  INFO and an interaction reply's URL holds its token. `DiscordRest` has no
  delete method and nothing in `jarvis/v2/discord/` spells `"DELETE"` (D3;
  a test greps).
- **Scrub.** Every text field the poster sends — thread name, milestones,
  report and `report.txt`, question and choices, the card — and the
  approval text are run through `secrets.scrub` before rendering, so a
  credential value from a protected file never reaches Discord (the full
  command still shows, minus the value).
- **The light.** `GET /discord` gains `reporter: {state ok|degraded|down,
  reason, counters, dropped, last_error{op,status,code,at}}`; a
  `discord_status` SSE event fires on every state change; the HUD's
  `DiscordPanel` shows green, amber with the reason, or red. A surface that
  failed to start is recorded by class name and shown red, not "pending".
- **Bugbot fixes (2026-10-08).** *No rewind:* the sidecar keeps
  `seen_updated`, the newest `Task.updated` delivered (a transition always
  moves it forward); an event snapshot older than that is skipped, so a
  reconcile that ran ahead of queued snapshots can no longer rewind `phase`
  and post Verified, Done and the ping twice. *Breaker:* the pass that
  re-closes it also clears the failure count and back-off (the light goes
  back to ok), and a drop while it is open schedules the reconcile at its
  end, never "now" (that was a busy loop). *Relink to another channel
  (decisions, Interpretation under O-C5):* the sidecar records the thread's
  parent (`channel_id`); when a project's channel changes under a live task,
  the task gets a new thread in the new channel opening with "Continued
  from <#old>" (standing in for Started; an open question is asked again
  there), the old thread gets "Moved to <#new>" and is kept, never deleted,
  and its id goes to `retired_threads`. A sidecar from before the field
  takes the event's `previous` channel, else asks Discord for the thread's
  parent (and if Discord cannot say, the task stays put and a warning is
  logged once). *Questions in one phase* share a stamp, so an event snapshot
  posts or clears a question only while it is still the one open on disk
  (PR #12 review F3).

### 11.6 The server and project channels (B1, 2026-10-07)

Plan: `docs/plans/2026-10-07-discord-plan.md` §2–§3; owner decisions D3, D4,
D5, O1, O6 and O7 in `docs/plans/2026-10-07-discord-decisions.md`. Code:
`jarvis/v2/discord/{perms,guild,setup,linker,routes}.py`.

- **Setup is human-only:** `jarvis auth discord-guild` lists the servers the
  bot is in, prints the invite pinned to the chosen one
  (`scope=bot+applications.commands`, `permissions=309237763088`,
  `guild_id`, `disable_guild_select=true`), checks the bot's permissions with
  Discord's own algorithm (`perms.effective`) — a **missing** one blocks
  (fix it, press Enter, it checks again), an **excess** one warns,
  Administrator loudly (D5: never Administrator) — and only after a y/N (O6)
  creates the **Jarvis** and **Jarvis Archive** categories and **#ungrouped**
  under Jarvis, reusing any a previous run made. It writes
  `~/.config/jarvis/discord_guild.json` (`config.DISCORD_GUILD_PATH`, env
  `JARVIS_DISCORD_GUILD`) `{guild_id, category_id, archive_category_id,
  ungrouped_channel_id}`, mode 600, atomically. The file is in
  `permissions.protected_paths()`; the daemon re-reads it (cached on mtime)
  whenever it needs it, so no restart. The v1 invite carries the same
  integer.
- **Every channel links to a project whose folder exists** (O1). A link
  needs `Path(root).is_dir()` and a project that is not archived.
  `Project.discord_channel_origin` (`created` | `linked`) is display only.
  The slug is lowercase ASCII with hyphens, ≤ 90 characters, `project-<id>`
  when empty, `-<id[:4]>` on a clash; the topic is `Jarvis project · <name> ·
  <id>`. The daemon links the **Inbox** to #ungrouped as soon as the guild
  file exists; nothing else can relink, unlink or give it a channel.
- **Owner-only routes** (`discord/routes.py`, mounted beside
  `projects.route`): `GET /projects/{id}/discord` (the light, cached 60 s,
  each read ≤ 5 s and never sleeping out a 429), `POST
  /projects/{id}/discord {action: create | link | unlink}`, and `POST
  /discord/backfill` (one create a second). Link validation, one function
  (`ChannelLinker.validate_link`, for B2's `/project link` too): a
  snowflake; not #ungrouped or a Jarvis category; the project live, not the
  Inbox, its folder present; not linked to another project (409); the bot
  can see it; same guild; a text channel; no missing permission there.
  `POST`/`PATCH /projects` refuse `discord_channel_id` (400 "link a channel
  from the project dialog").
- **Who may change a channel (D4).** `project_updated` gains `by: "owner" |
  "api"` (`projects.is_owner`, the non-raising `owner_only`) and `previous`.
  The `ChannelLinker` (its own worker) renames at once on an owner rename
  — a long 429 keeps it *pending* and retries when Discord said, shown as
  "rename pending" — and asks first on an `api` one:
  `ApprovalRequest(tool="discord_channel", args={action, channel, from, to,
  why}, origin="Jarvis housekeeping", allowlistable=False)`, Approve/Deny and
  never Always, on a separate asker so an open question never holds up the
  owner's own renames. A deny or a timeout does nothing; a yes re-checks
  that the project still has that name and channel. Archive moves the
  channel to Jarvis Archive by `parent_id` alone (`modify_channel` cannot
  send `lock_permissions` or `permission_overwrites`) and posts a note;
  restore moves it back, renaming only if PR #4 renumbered the name, and
  posts "Restored."; a permanent delete posts a note (the event now carries
  `discord_channel_id`) and the channel is **kept**.
- **Crowding (D3, O7).** At 45 of 50 channels in Jarvis, one approval lists
  the channels of projects idle 30+ days ("Move these N to Jarvis Archive?";
  the projects stay active). At 45 in the archive target, one approval to
  create "Jarvis Archive 2"; later archives go to the newest overflow
  category (kept in `<v2 data>/discord/linker.json`). An ask is never open
  twice, and after a deny it waits a day.
- **Placement.** Once a guild is configured, `_locate` and S1's interaction
  gate place only messages and interactions whose `guild_id` matches; an
  archived project's channel keeps its fixed reply.
- **The safety net steps aside.** The Reporter hears the linker's
  `project_updated` (`changed: ["discord_channel_id"]`) and reconciles that
  project at once: an active or blocked task gets its thread and card in the
  new channel, and one whose thread Discord deleted (`thread_gone`) gets a
  fresh one (the old id kept in `retired_threads`). The DM stays the net only
  for a channel that later breaks.
- **`GET /discord`** gains `guild {configured, id}`, `linker {state ok |
  degraded | unconfigured, reason, pending_renames, awaiting_approval}` and
  `permissions {missing, excess, administrator, checked_at}` (refreshed by the
  linker's worker, never on the HTTP thread), copied field by field. The HUD
  light goes amber on a missing permission or Administrator.
- **No DELETE anywhere** still holds: `DiscordRest` has no delete method,
  nothing in `jarvis/v2/discord/` spells `"DELETE"`, and B1's fake Discord
  fails the suite on one. Only `linker.py` and `setup.py` call
  `create_channel`/`modify_channel` (a grep test with an allowlist B2 extends).
- **Review fixes (PR #8).**
  - *Only a sanctioned name is applied.* The linker keeps, per project, the
    last name the owner's action or an approval chose (`linker.json`
    `sanctioned`). A pending rename retries its **own** name and is dropped
    once the project's name has moved on — the newer change then takes its
    own owner/api path, so an `api` rename can never ride an owner's 429
    retry. A restore applies a new name only if it was sanctioned or is PR
    #4's renumbering of the archived name (`project_restored` carries
    `previous_name`); anything else moves the channel back and asks.
  - *Nothing is lost to a failure.* A rename or move that meets a 429, a 5xx
    or a transport failure is kept (`pending_renames`, `pending_moves`) and
    retried when due; restore sends the move and the rename as separate
    requests. A start-up reconcile moves an archived project's channel out
    of Jarvis, and a live project's channel back out of the archive when it
    went there with its project — never one moved for crowding
    (`archived_by: project | crowding`). Open asks and the time each ask was
    answered persist, so a restart asks an unanswered question again and
    does not re-ask an answered one for a day. `GET /discord`'s linker gains
    `pending_moves`.
  - *Names.* Clashes are judged on the channel names Discord has (the cached
    guild channel list), and a channel already carrying the bare or the
    suffixed slug keeps it, so whoever had a name first keeps it.
  - *Idempotent create.* A create first adopts an unlinked channel in the
    Jarvis category whose topic ends with `· <project id>` — the residue of a
    create whose answer was lost.
  - The crowding note says how a channel comes back (`/channel restore`, B2,
    or by hand).
- **Bugbot fixes (2026-10-08): nothing is marked done before it worked.**
  `reconcile()` and `check_crowding()` return None when the guild's channels
  cannot be listed, and the worker (`_housekeep`) marks the reconcile done or
  clears crowding only on success, trying again after `RETRY_S`; the Inbox
  link is marked done only once written. A restore says "Restored." only when
  the channel moved, and otherwise that the move will be retried (or that
  Discord refused it). `/channel archive|restore` drops an older pending move
  only after its own lookup succeeded. A failed listing is retried after 30 s,
  doubling to 10 min, never sooner than Discord's `retry_after` (review F5).
  A housekeeping ask that was approved
  is settled even if a shutdown began meanwhile, so a restart never replays
  an action that already ran (a second archive category).

### 11.7 Projects and folders from Discord (B2, 2026-10-07)

Plan: `docs/plans/2026-10-07-discord-plan.md` §5; owner decisions D2, D4, O3,
O4, O5, S-1/S-2 and B11. Code: `jarvis/v2/folders.py`,
`jarvis/v2/discord/project_commands.py` (the handlers), and
`projects.create_project`.

- **Commands** (static registry, synced by the daemon): `/project new
  name folder` (both empty opens a form with Name and Folder), `/project link
  project`, `/project unlink`, `/project channel project`, and `/channel
  archive|restore`. No typed keyword forms (S2). **Archiving or deleting a
  project stays HUD-only** (B11): no command does either, and a test holds the
  subcommand list to that.
- **Places.** A server channel no project owns ("other") now takes
  `/project new`, `/project link`, and `/yes`/`/no` plus the approval buttons
  for a request asked there (the post offers exactly those; a button still
  matches the very message and channel; `/always` stays refused, as nothing
  asked there is allowlistable); everything else there
  keeps "This channel isn't a Jarvis place." `/project new` from the DM makes
  the channel under Jarvis; from an unlinked channel it links *that* channel
  (validated first, so an unlinkable one refuses with nothing made); from a
  linked channel it refuses. `/project link` refuses a project that already
  has another channel ("`/project unlink` there first") rather than moving
  the link silently.
- **The folder** (`folders.py`): a bare name → `config.PROJECT_WORK_DIR/
  <slug>` (`~/jarvis-work/<slug>`; the name picks it when no folder is
  given); a leading `~/` expanded once; otherwise absolute. Refused: control
  and format characters, `.`/`..`, > 4096 characters or > 255 bytes a
  component, any dot component, anything not strictly below a
  `config.PROJECT_FOLDER_ROOTS` root (`~`, `/mnt/c/Users/johnw`, env
  `JARVIS_PROJECT_FOLDER_ROOTS`), the work dir itself; checked on the
  input *before* any trimming — any whitespace but an ASCII space, invisible
  fillers (U+3164, U+115F, U+1160, U+FFA0, U+2800), input that is not already
  NFC, a component starting with a combining mark, inside *or holding*
  `V2_CREDENTIAL_DIRS`, `V2_DATA_DIR`, `REPO_ROOT` or `/mnt/c/Users/johnw/
  AppData` (case-insensitive on `/mnt/<drive>`), Windows-reserved names and
  characters under `/mnt/<drive>`, another project's root, a file or a
  symlink at the path, and a missing parent (O4). Every rule runs on the
  typed path **and** on the realpath of its nearest existing ancestor, so a
  symlinked parent cannot carry it out. The check lists names only, never
  contents.
- **Confirmation.** A missing folder (`create`) or one with something in it
  (`adopt`, with its entry count and whether it is a git repo) asks first:
  `ApprovalRequest(tool="project_folder", args={action, path, name[,
  entries, git]}, origin="Discord: new project", allowlistable=False)`,
  posted with Approve/Deny in the channel the command was typed in
  (`ApprovalRequest.discord_channel_id`, still the DM if that post fails) and
  shown as a HUD card. One-shot, never Always, never by voice, and a timeout
  denies. An existing empty folder is used without asking (O5). After the yes
  `make_project_folder` re-runs the check; anything different — a folder that
  appeared, a count that changed — raises `FolderChanged` and the owner is
  asked again about what is there now.
- **Making it.** Exactly one `os.mkdir(path, 0o755)`, no parents, no
  `exist_ok`, nothing written inside. `make_project_folder` is called only by
  the confirmation handler: not a tool, not MCP, not a route (a grep test).
  Then `projects.create_project` — the one create path `POST /projects` now
  uses too, so PR #4's numbering applies — then the channel.
- **`/project unlink`** asks Approve/Deny first (`tool="discord_channel"`,
  `action: "unlink"`) and keeps the channel. **`/project link`**,
  **`/project channel`** and **`/channel archive|restore`** act at once: the
  owner typed them (D4). `/channel archive` moves the channel to the archive
  target by `parent_id` alone (`ChannelLinker.move_channel`); the project
  stays active and its threads keep working. The move goes through B1's
  `move()` bookkeeping with reason `owner` (`archived_by` `owner`, which a
  restart's reconcile leaves alone) or `restore` (clears it), and **first
  drops any older pending move for that channel** — a 429'd restore or
  crowding move would otherwise undo the owner's command when it came due. A
  transient failure keeps the owner's move pending and says so.
- **B2b, deferred:** `project_propose` and the Claude Sonnet/Opus rule (O3)
  wait for the peers plan's phase 0.

### 11.8 Every chat is a Discord thread (PR C, 2026-10-07)

Plan `docs/plans/2026-10-07-discord-plan.md` §4; decisions C1, O-C1…O-C7.
`ChatMirror` (`jarvis/v2/discord/mirror.py`, on the `DiscordSurface`, its own
worker) is the one place a chat meets Discord, both ways.

- **`Thread.surface`**: `None`, `"dm"`, `"dm:retired"` or
  `"discord:<Discord thread id>"`. Only `ThreadStore.set_surface` writes it,
  under the store lock; **`ThreadStore.save` never changes it** — not just
  "never back to None": a stale copy holding an *older* surface (before a
  move) cannot put that back either. The daemon saves its session's
  in-memory thread on every usage event, which is bug 1's lost update.
  Sidecar `threads/<id>/discord.json`: `{surface, retired, moved_to, name,
  mirrored_through}`.
- **`user_message`.** `daemon.send` publishes it (before the worker starts,
  so it leads its turn) with `{text, typed, via, origin, images,
  attachments, spoken, discord_message_id, discord_channel_id}`, and the
  `user` log record holds the same fields. `UserMessage` gained `via`,
  `typed`, `attachments` (names), `spoken` and the two Discord ids, all
  defaulted and never shown to a provider. `via` defaults to `hud` for an
  owner message and `system` otherwise. `assemble_turn` fills `typed`,
  `attachments` and `spoken` (the HUD's dictation sends `spoken: true`).
- **Out.** Only `Role.CHAT` threads with no task. A chat gets its thread in
  its project's channel (Inbox → #ungrouped) at its **first owner message**,
  named by its title or `<id> · <first 50 chars>` (≤100), renamed on the
  owner's HUD rename. Posted, scrubbed and silent (4096): "You (HUD): <typed
  words>" (or "You (HUD, voice)"), attachments by name, images as `[N
  images, in the HUD]`; settled `text` joined per turn and split at 2000 —
  never a delta or thinking; one footer (`· 5 tools: read_file, grep_files
  +2`, `· turn failed (<class>)`, `· interrupted`). Speech for a voice
  reply is synthesized from the *scrubbed* text. A system message (an
  escape-hatch result) shows its **first line only** — `[owner ran: <cmd>]`
  — and never the command's output. A Codex question is posted at once with
  a ping line, and is cleared when it is answered anywhere (the daemon
  publishes `question_answered`); a proposal's sentence follows the reply,
  notifying in the DM when the turn came from the DM (O-C3). A chat whose
  project has no channel is passed over (no backfill later).
- **Progress is the log.** A bus event only marks a chat dirty; the worker
  walks `log.jsonl` from `mirrored_through`, which advances only when a post
  is delivered. A turn still running is waited for; a turn cut short by the
  next user message is "interrupted". On start a chat ≤6 messages behind
  gets them, further behind gets "(N messages while Discord was
  unavailable, see the HUD)". Chats older than the mirror are seeded
  silently.
- **Pacing.** One outbox per Discord thread, 4 posts per 5 s; above 12
  pending the rest become "(N more messages, open the HUD)" with
  `reply.txt` — never a question, its ping line, or a rename/archive. 429/5xx
  retry with back-off, and so does making a chat's thread (per chat, 5 s
  doubling to 60 s, never sooner than Discord's `retry_after`); 50083
  unarchives and retries once; **10003 unlinks the chat** (surface back to
  None) so its next message makes a fresh thread; **50001/50013 on a post
  re-sends it to the owner's DM**, prefixed `[<project> · chat <id>]` and
  notifying — the DM is the safety net (C1, O1).
- **In (gateway `_locate`/`_chat`).** A DM is the DM conversation: one
  persisted Inbox chat with `surface="dm"` (an archived one is retired to
  `dm:retired` and a new one opened, so the DM never stops answering;
  **restoring the old chat in the HUD leaves it detached from Discord** — it
  keeps `dm:retired` and is a HUD-only chat from then on). A top-level
  message whose Discord thread cannot be started opens no chat (the one
  `open_thread` made is removed again). A
  chat's thread — current or retired — runs that chat. A top-level owner
  message in a project channel or #ungrouped starts a **new chat** via Start
  Thread from Message (a slash command, which has no message, gets a plain
  thread). An open provider question is answered by the next typed message;
  if that answer fails (answered in the HUD meanwhile), the message runs as
  an ordinary turn. Anything else is ignored, mention or not; `_chat_threads` is
  gone. The reply is never posted by the gateway: the mirror posts it from
  the log, so Discord and HUD turns come back the same way. A Discord turn
  is `UserMessage(via="discord" | "dm")`, never echoed; images and text
  files follow `assemble_turn`'s caps and protected names (`paths=False`:
  no @path read), anything else is refused with a note and never
  downloaded. Voice notes run with `spoken=True` and get speech back; they
  never approve and never answer. While a turn runs, a message is **steered**
  into it ("Got it. I'll work that into what I'm doing.") or waits behind it
  ("I'll take this next."), up to three; the fourth is refused. Since
  2026-10-08 the queue is the daemon's (`Daemon.deliver`), one queue and one
  cap for Discord and the HUD alike; the mirror's own O-C6 queue and its
  polling drain are gone. A voice note steers like a typed message and
  still answers nothing (D7). The owner's Stop drops what waits, and the
  mirror says so after the turn: "Not sent, the turn was stopped: …". A HUD
  steer is mirrored where it landed in the reply ("You (HUD) · steering:
  …"); waiting messages are posted after the turn they waited on, which is
  when they ran. The last 500 message ids are remembered. An open provider question is answered by the next *typed*
  message (`daemon.answer`).
- **Questions, Bugbot fixes (2026-10-08).** A Discord message with files and
  no words now reaches the turn path in an owned place (v1's `should_respond`
  knew only text and voice), and it **never answers** an open question —
  only typed words do (D7); it runs as an ordinary turn, queued behind the
  waiting one (O-C6), the same fall-through as a failed answer. A question is
  recorded (`pending_question`) even when there is nowhere to post it yet
  (no DM, a thread still being made) and posted once there is.
  `daemon.answer` logs `question_answered` beside the question, and the
  mirror's sync re-posts an open question (asked, with no later
  `question_answered`) of the turn **this process is running**, with its D1
  ping outside DMs, so a dropped bus event is recovered; a question from
  before a restart died with its provider and is never resurrected. Either
  path posts it **once**: the bus event checks what the sync already asked
  (the daemon logs before it publishes, review F1).
- **Approvals** raised in a chat go to its thread (or the DM for the DM
  chat), buttons and ping line as for tasks; the approval worker waits up
  to 3 s for a thread being made at that moment.
- **Lifecycle.** A move makes a thread in the new channel ("Continued from
  <#old>"), says "Moved to <project> → <#new>" in the old one and renames
  it `↪ moved to <project> · <name>` (O-C5, kept on later renames); the old
  one stays an alias and its replies point at the new one. Archive posts a
  note and archives the thread as its creator — a 403 turns the light amber
  ("add Manage Threads", O-P1); restore says "Restored."; a permanent delete
  posts a note and unlinks. **Nothing is ever deleted on Discord.**
- **Slash commands** work in a chat's thread (`commands.CHAT` place);
  `/skill` there runs in that chat.
- **HUD.** `GET /threads` carries `surface` and, with one, `discord: {kind,
  channel, name, url}`; the chat header reads "On Discord: #school › name"
  (links only to `https://discord.com/channels/<guild>/<id>`); a Discord
  message is labelled "via Discord" live (`user_message`) and from the
  transcript (`via`). `GET /discord` gains `mirror: {state, reason, queued,
  last_error}`.

---

## 12. HUD v2 (G3) — specified 2026-09-16 from the owner's elaboration

**What to take from each app, in the owner's words, mapped onto v2.**
From Codex: the built-in browser/preview pane, the diff viewer, editing
markdown in place with a rendered preview, reading docs rendered rather
than as source. From Claude Code: WSL and Windows projects side by side
without restarting, scheduled tasks, orchestration made visible, and the
project organization. From both: Codex's per-project access folders inside
Claude's project tree. Plus: auto permissions by default, usage for both
providers, and dictation with an adjustable send mode.

### 12.1 Decisions

- **Stack:** Vite + React + TypeScript under `hud/`, built to static files
  the daemon serves; opened in the owner's Windows Chrome in app mode on
  **`FACE_PORT` 8402** (a second listener on the daemon, same origin as v1,
  so the mic grant survives). Monaco for the editor and the diff view; a
  sanitized markdown renderer (markdown-it + DOMPurify, links `_blank`,
  no raw HTML) for docs and replies. Headless Playwright suites as v1.
- **Windows projects are `/mnt/c` paths worked from WSL** for now, shown
  with a Windows badge in the sidebar and a one-line caution about git on
  9p. A native Windows worker over a bridge is a later package, not this
  one.
- **Two sub-packages in parallel against one contract** (`docs/hud-api.md`):
  WP12a backend routes (Codex), WP12b the frontend (Opus). The frontend
  develops against a mock of the contract and is verified against the real
  daemon at the end.
- **The model picker chooses the fast path's model only.** Provider and
  model per role come from the routing table (§8), shown read-only beside
  it with a link to the routing editor. The global picker still never
  touches a task's Claude or Codex settings. *Amended 2026-10-06 (decisions
  A1–A5):* each **chat thread** has its own provider, model and effort,
  chosen with three chips beside `in: <project>` in the input bar
  (provider ▾ · model ▾ · effort ▾). The provider is picked while
  composing and fixed after the first message; model and effort change from
  the next message. OpenRouter lists the roster plus "search catalogue…"
  (a model used from the catalogue is pinned to the roster); Claude and
  Codex list `router.CLI_MODELS`. A pinned model that leaves the roster
  stays pinned, shown "(not on roster)". Every change is a system line in
  the chat. Task threads show their model read-only. No tool can change a
  thread's model or provider — only the owner, in the HUD.
  *Amended 2026-10-08:* the Model picker edits the roster and **chooses the
  default**. Each row has Set as default and an × (unpin; never selects);
  the model in use carries a `default` badge; the env model is shown as
  "config default (JARVIS_ORCHESTRATOR)", used only while nothing is
  chosen, with "Reset to config default". The catalogue's pinned rows offer
  Unpin. Any model unpins, the env model included; the backend refuses
  (409, shown inline) only an unpin that would empty the roster or leave a
  default thread on an unlisted model. ~~Claude's and Codex's chat defaults
  are unchanged and not HUD-editable.~~
  *Amended again 2026-10-08:* Claude's and Codex's chat defaults are
  HUD-editable too, from the model chip's `default ▾` menu (Set as default
  per model, a `default` badge, the default's effort, "Reset to built-in
  default"). Stored in `~/.config/jarvis/provider_defaults.json`, beside
  models.json and refused to agent writes like it (v2's permit and v1's
  write tools alike). The Codex one **wins over
  routing for chat threads only** and never writes routing.json; role
  routing stays Settings', and "Reset" returns to it.
- **Permissions default to `auto`** (D6); the project header carries the
  profile switch (auto / ask / strict) and the always-ask additions.
- **Previews and agent-written pages are a separate origin**
  (`WORKSHOP_PORT` 8403, v1's rule): the HUD is the approval surface, and
  an iframe that could reach `/approvals` would let an agent approve
  itself. The preview pane is an iframe onto that origin or onto a
  `localhost` dev server the owner names; never onto the HUD's own origin,
  nor (WP-E, §18) the API listener's. The HUD and API refuse to be framed at
  all (`frame-ancestors 'none'`), which is what lets a dev server's page keep
  its own origin when the owner switches that on for its pane; and the
  workshop sandboxes every document it serves (`sandbox allow-scripts
  allow-forms`), so an agent-written page there always runs with an opaque
  origin, wherever it is opened.
- **Dictation send mode is a three-position control in the input bar:**
  AUTO (send when speech ends), REVIEW (transcript lands in the box, the
  owner clicks send), OFF (v1's mic mute). Persisted; a fresh window boots
  into **REVIEW**, the mic-fails-toward-muted reasoning applied to sending.
  The v1 capture pipeline (always-open mic, ring buffer, adaptive
  threshold, wake word, pre-roll, his-own-speech suppression) carries over
  as a component unchanged in behaviour; the mode only decides what
  happens to a finished utterance.
- **Usage panel:** subscription meters only. Claude shows its 5-hour and
  weekly windows (from Claude Code's own usage endpoint; the login token
  never leaves the daemon) and Codex shows its weekly window. Under 75%
  green, from 75% yellow, from 90% red. A window the provider did not
  report is a dash, never a bar invented from the ledger. Role routing
  is in Settings. Last routing decisions are a collapsed log.
- **Scheduled tasks** are a backend feature the HUD manages: a schedule is
  a project, a brief, a cron or interval, enabled or not; the daemon's
  scheduler creates and starts a task when due, through the same intake
  gate, so a scheduled task can still stop and ask.

### 12.2 Layout

```
┌ sidebar ──────┬ main ──────────────────────────────┬ right ────────────┐
│ projects       │ tabs: Chat · Task · File · Diff ·   │ Task: plan, status│
│  ▸ threads     │       Preview · Doc                 │ record, routing   │
│  ▸ tasks       │ chat: rendered replies, tool stream,│ line, threads     │
│    ▸ threads   │       live draft, input bar with    │ Approvals queue   │
│ schedules      │       dictation mode + attachments  │ Usage (both)      │
│ usage · route  │ orb (PTT + state) docked bottom-left│ Schedules         │
└────────────────┴─────────────────────────────────────┴───────────────────┘
```

Skin: the **Graphite** theme (2026-10-06, amending the original "cyan
palette, angular panels" choice — the blue field tired the owner's eyes over
long sessions). A flat warm off-black (`#141413` window, `#1a1a19` panes,
`#222221` raised) with warm-grey text, panes meeting at 1px rules with no
notched corners, grid or glow, and one sparing glacier-cyan accent
(`#6fc3df`) for focus, selection, the active tab, links and "running". Amber
(tool running, authorization pending) and red (error) keep their exact values
and meanings; the authorization card stays the loudest thing on screen. The
orb with its ring sets and avatar face carries the sci-fi identity alone (the
default avatar's accent moved to `#6fc3df` with it). The tokens live on
`:root` in `hud/src/theme.css`; every text/background pair passes WCAG AA,
the tightest at 4.6:1. The information
architecture above is the desktop apps'. Carried over as components: orb, avatar face, wake word,
capture pipeline, TTS speculation, approval card semantics (deny cheap,
authorize deliberate, nothing keyboard-defaulted, `textContent` only),
model / voice / avatar pickers, session (now thread) picker.

**Invariants that do not move:** no agent-reachable lever closes, drives
or navigates the HUD; `<title>` stays `J.A.R.V.I.S.`; `is_face_origin`
and `FORBIDDEN_TITLES` still refuse it; avatar SVGs still go through the
sanitizer and an `<img>`.

## 13. What carries over, migrates, retires

**Carries over unchanged in kind:** `ApprovalBroker` + Discord approvals,
`rules.py` (DENY / ALLOW / ASK + always-ask additions), `permissions.py`
allowlist, `secrets.py` and the dispatch scrub, memory tools, avatars, voice
(`tts`/`stt`, Kokoro, Pocket), the mic capture design, the model selector,
Gmail/Spotify/Onshape/desktop integrations (re-homed behind `jarvis-mcp`),
`longbench/` (it never imported Jarvis; it is now the cross-harness ruler),
the trading firm's Codex transport and ledger (lifted, D10).

**Migrates:** sessions → threads in the inbox project; goals, workflows,
attended tasks → tasks (three copies of one idea become one); skills → the
`SKILL.md` folder format; `jarvis face` + `jarvis daemon` → one daemon plus
a HUD client.

**Retires from the main path:** `agent.py`/`context.py` as the work loop
(kept as FastPath), most of `tools/` (the CLIs have their own file, shell,
search and browser tools; `browser.py` stays for the fast path's read-only
fetch and for `whiteboard`), agent-bench and cad-bench as gates (they test
the v1 loop; re-pointed at the fast path or archived), `tasks.py`,
`workflows.py`, `goalrunner.py` (subsumed).

---

## 14. Work packages

Each package has an owner, an interface it must not change, and an acceptance
test that a fresh session can run. **Claude owns the interfaces and safety
layers; Codex takes packages whose interface is fixed before it starts.**
Order follows D9.

| WP | Package | Owner | Depends on | Acceptance |
|---|---|---|---|---|
| 0 | **Spikes R1–R5** (§15): SDK on subscription login; `can_use_tool` under auto; Claude sandbox on WSL2; Codex approval routing to a fake broker; Discord thread create/post/edit under the bot's permissions | Claude | — | each spike is a script under `tests/spikes/` with its finding written into this doc |
| 1 | **Domain stores**: Project, Thread, Task, Report; JSON+JSONL; v1 session migration | Codex | interfaces in `jarvis/v2/model.py` written by Claude first | free synthetic suite: round-trip, migration of a real v1 session dir copy, no empty dirs created |
| 2 | **Provider interface + FastPath adapter** | Claude | — | fake-`llm.chat` suite: events emitted in order, escalation to `task_propose` on any file/shell/browser intent |
| 3 | **ClaudeProvider** (Agent SDK) | Claude | WP0, WP2 | live: a 3-turn resumed thread; `always-ask` reaches the broker; `reviewer_declined` → escape hatch runs a command in the worktree and the reply reaches the session |
| 4 | **CodexProvider** (app-server, lifted from the firm) | Codex | WP0, WP2; the firm's `codex_rpc.py` | live: same three checks as WP3, plus resume after daemon restart and the daily allowance parking a task |
| 5 | **Permissions v2**: five layers, always-ask list, profiles, escape hatch, decision log | Claude | WP2 | `rules_check`/`permissions_check` extended; a strict-profile task auto-runs nothing; DENY unreachable via the hatch |
| 6 | **Worktrees**: create/adopt/cleanup, non-git fallback | Codex | WP1 | two parallel tasks on one repo never touch each other; uncommitted work is never deleted |
| 7 | **Daemon v2 + local API**: one process, WS event fan-out, HUD and CLI as clients, single-instance lock | Codex | WP1, WP2 | daemon_check rewritten: two clients see the same task events; killing a client kills no task |
| 8 | **Skills folder + `jarvis skills link` + `jarvis-mcp`** | Codex | WP7 | `claude -p` and `codex exec` each list a Jarvis skill and call a Jarvis MCP tool; a dangerous MCP tool raises a card |
| 9 | **Router + UsageLedger** (§8, all five subsections) | Codex (ledger, ladder), Claude (stage-one rules, fast-path toolset boundary) | WP2, WP3, WP4 | fake providers: every stage-one rule in the 8.1 table lands where stated; the fast path has no mutating tool and step exhaustion yields a proposal; the 8.2 ladder logs a reason per role; over-threshold drops a candidate and an empty result blocks rather than falling to the fast path; a running worker thread never changes provider; an orchestrator move needs an owner yes and restarts from disk; BYOK/upstream cost counted (v1 lesson) |
| 10 | **Discord v2**: guild routing, channel per project, thread per task, status embed, milestone templates, thread-scoped approvals | Claude (routing + approvals), Codex (embeds/templates) | WP7 | discord_gateway_check extended: a `yes` in the wrong thread authorizes nothing; status message edited not re-posted; every milestone ≤ cap |
| 11 | **Task runner + roles + intake gate** | Claude | WP3–WP6, WP10 | scripted providers: a brief with a blocking question stops in `clarifying`; verify failure loops once then blocks; report schema enforced |
| 12 | **HUD v2** | Codex (build), Claude (spec + approval-surface invariants) | WP7, owner's elaboration | headless suites equal to v1's for: approval card, wake, capture, markdown, pickers; plus projects/threads/tasks navigation |
| 13 | **Long-bench cell**: v2 Jarvis on `sweep` vs bare `claude -p` and bare `codex exec` | Claude | WP11 | a row in `longbench/README.md`; the number that says whether this was worth it |

A package is done when its acceptance test is green in a fresh session and
this document's section for it has been updated. CLAUDE.md gets a v2 section
pointing here once WP7 lands; until then v1's CLAUDE.md is still the truth
for everything not under `jarvis/v2/`.

---

## 15. Risks to retire first (WP0)

- **R1** Agent SDK on the subscription login, no API key. If not: workers on
  Claude are API-billed, and the routing policy defaults flip to Codex for
  more roles. *Partial finding 2026-09-15:* the SDK (0.2.153) spawns the
  installed CLI and inherits its login; the spike (`tests/spikes/r1_sdk_login.py`)
  could not complete because the CLI's stored OAuth session had expired and
  a session run from inside the Claude desktop app cannot lend its own
  host-managed auth to a child process. Two consequences already: the daemon
  must run where a valid `claude login` exists, and `ClaudeProvider.health()`
  must detect an expired login and report it as *unavailable* rather than
  letting every task fail one turn in. **Answered after `claude login`
  (same day): the SDK works on the subscription login with no API key.**
  Two more facts from that run: `ResultMessage.total_cost_usd` is reported
  (0.28 for a 3-tool turn) even though nothing is billed on a subscription,
  so the ledger records Claude's figure as *equivalent* cost, never as
  spend; and passing `allowed_tools` auto-approves those tools **before**
  `can_use_tool` is consulted (the SDK warns), so the provider must not use
  `allowed_tools` for anything it wants gated — R2 is re-run without it.
- **R2** ~~Whether `can_use_tool` is consulted under `--permission-mode
  auto`, or whether always-ask has to be a `PreToolUse` hook.~~ **Answered
  2026-09-15:** under `auto`, `can_use_tool` was consulted for nothing and
  the `PreToolUse` hook fired for every call (spike output: callback NONE,
  hook `['Bash']`). The hook is the gate on the Claude side; see §5.3.
  Sub-question closed by WP3: the hook matcher's timeout is the entire
  budget (the SDK applies none of its own), so the provider sets 660 s and
  a hook may block while the owner is asked; no deny-and-requeue.
- **R3** ~~Claude Code's bubblewrap sandbox in headless mode under WSL2.~~
  **Answered 2026-09-15, negatively**, by `tests/spikes/r3_claude_live.py`
  on CLI 2.1.273: with the SDK's `sandbox` setting enabled, a turn still
  wrote a file outside the workspace, so the flag confined nothing here.
  The fallback is now the rule: **Claude workers run under auto mode
  without a sandbox; a task that needs confinement routes to Codex**
  (Landlock, `workspace-write`, on by default — §8.2 capability filter).
  The live run confirmed the rest of the provider: the `PreToolUse` hook
  fired for all three commands, `approval_requested`/`approval_resolved`
  surfaced, and usage arrived with the equivalent cost. Left open as a
  follow-up, not a blocker: `bwrap` is installed but `socat` is not, and
  the docs name both for Linux — worth one more run after installing it
  before treating the negative as final.
- **R4** ~~Codex app-server approval requests answered by our client end to
  end, including `requestUserInput` as a clarification question, on
  0.153.4.~~ **Answered 2026-09-15** by `tests/spikes/r4_codex_live.py` on
  the owner's ChatGPT login: health ok, one real turn, two tool calls
  (`tool_started`/`tool_finished` pairs), the written file verified, usage
  31129 in / 115 out / 27392 cached with `cost_usd=None`. **No approval
  request reached the callback**: `auto_review` approved the shell command
  itself, which is R8 observed live. The approval and `requestUserInput`
  paths remain covered only by the fake-server suite until a turn provokes
  an escalation.
- **R5** Discord: thread creation and message editing under the bot's current
  permissions and intents; auto-archive reopen behaviour.
- **R7** Launching a CLI worker is itself an action a classifier may
  refuse. Verified 2026-09-15: from a Claude Code session in auto mode, the
  command that starts `codex exec --approve-for-me` on the WP1 brief was
  blocked by the auto-mode classifier as spawning an autonomous agent. The
  daemon is not a Claude Code session, so this does not affect v2 at runtime,
  but it is the D7 case in miniature and it is why the escape hatch shows the
  whole command to the owner rather than trying to get around the reviewer.
- **R8** (from WP4, 2026-09-15) **Codex has no universal pre-tool callback
  under `auto_review`.** The provider only sees the approval requests the
  reviewer chooses to escalate, so §6 layer 2 (always-ask) cannot be
  enforced from those alone; `CodexProvider` refuses a brief with a
  non-empty `always_ask` rather than claim an enforcement it lacks. WP5 must
  find the Codex analogue of Claude's `PreToolUse` hook (0.153.4 ships a
  hooks mechanism — `--dangerously-bypass-hook-trust` exists — so a
  pre-execution hook is the first thing to verify) or gate always-ask
  tools structurally (a `jarvis-mcp` tool that asks, with the native
  equivalent removed from the toolset). Until then, a project with
  always-ask additions routes its tasks to Claude.
- **R6, Codex half answered (WP12a, 2026-09-16):** the app-server protocol
  on 0.153.4 (unchanged in 0.161.0 but for an optional `normalModelSlug`)
  declares `account/rateLimits/updated` with `usedPercent`,
  `windowDurationMins` and `resetsAt` per window (primary/secondary,
  sparse-merged). `CodexProvider` surfaces it as
  `USAGE.provider_reported.rate_limits`; `/usage` shows it as `quota`.
  Claude's half is the same endpoint Claude Code's `/usage` command
  calls (`GET /api/oauth/usage`: `five_hour` and `seven_day`), cached
  for a few minutes. No login, a 401, or a body without those windows
  stays `quota: null` — still never invented from the ledger.
- **R6** Both subscriptions' *actual* headless limits. The firm's ledger
  counts local admission, not quota; the visible window on Claude is what the
  routing threshold reads, and it has to come from somewhere real.

---

## 16. Open questions for the owner

1. HUD elaboration (§12): which behaviours from each desktop app.
2. The initial always-ask list (§6, layer 2): confirm or extend.
3. Routing defaults (§8): orchestration + review on Claude, implementation on
   Codex, given Claude is at 89% today. Or the reverse while it is.
4. Should the inbox project's chat threads also live on Discord (a `#jarvis`
   channel), or DM only.

---

## 17. First live task (2026-09-16) — the backend works end to end

Every package from WP1 to WP11 merged, all fourteen `tests/v2/*_check.py`
green, and then the check no suite can make: one real task through
`jarvis daemon2` on the live providers, in a throwaway git repo holding a
two-line `calc.py`. Brief: add `multiply`, write unittest tests for both
functions, make them pass, commit as `add multiply`.

**Result: DONE in 198 s**, one commit on the task branch in the task
worktree, both tests passing when run by hand afterwards. What happened,
from the status record and journal:

- **Orchestrator on Claude** returned a valid SPEC (goal, deliverable, five
  acceptance criteria, no blocking questions — the brief said to assume) in
  ~9 s, then a five-step PLAN with the verification step last.
- **Implementer on Codex** worked in the worktree: `shell` and `apply_patch`
  tool events, steps 1–5 relayed by the orchestrator's `{next, done}`
  instructions, ~130 s.
- **Reviewer on Claude, a fresh thread**, ran the acceptance commands and
  **failed the first review**; the findings went back to the implementer as
  the next instruction, a second fresh reviewer passed, and the orchestrator
  produced the §10.4 REPORT. The verify loop worked as designed on its first
  real outing.
- **Routing line**: `orchestrator: claude · implementer: codex · reviewer:
  claude`, each with its seven-step reason journaled.
- **Cost as recorded: $2.06**, almost all of it Claude's *equivalent* figure
  (not billed on the subscription); Codex reports tokens only. That number
  is what the ledger will show, so it is worth knowing it overstates spend.

**The first attempt blocked in 3 s, and that was a real bug.** The router's
`roster/default` for Claude resolved `models.tier("orchestrator")` — the v1
HUD roster, which holds **OpenRouter ids** — and handed
`deepseek/deepseek-v4-flash-0731` to Claude Code as its model. The
orchestrator answered "there's an issue with the selected model" twice and
the runner blocked the task with that reason quoted, which is exactly the
behaviour the intake gate specifies. Fixed in `router.model_settings`:
`roster/default` on Claude now means *no model argument* (the CLI's own
default), with effort from `routing.json` only. The §8.2 table's "Claude:
the roster selection" was wrong and this section supersedes it.

Two observations for later rounds: **a five-step plan for a two-function
change is over-planned** — the orchestrator's brief should say plans have as
few steps as the work has, and the reviewer's first failure was on a
formatting nit rather than an acceptance criterion; both are prompt work in
`roles.py`, not mechanism. And **the fast path is currently selected onto
`deepseek-v4-flash` in the HUD roster**, which the v1 routing policy keeps
personal chat off; the owner should reset that selection.

## 18. HUD v2 landed (2026-09-16)

TC-1 (2026-10-12): opt-in `transcript?tools=1` adds safe labels and provider outcomes; legacy bytes stay unchanged, unfinished calls track the current turn, and old-label derivation is capped at 500 (contract `docs/hud-api.md`).

2026-10-12: Settings installs the HUD as a Chrome app with orb PNG icons; `jarvis hud` pins the Default profile and launches its installed app id when present.

V2 boot (2026-10-12): the approved seeded binary-dust/orb and sharp pen-drawn J.A.R.V.I.S. plate play once per window from the first frame, silently; automation and reduced motion suppress it, clicks are consumed, keys keep working, approvals take precedence, and `hud_v2_boot_check.py` checks it under CSP.

AO-1b-i (2026-10-12): `devservers.py` watches tagged Claude/Codex listeners, holds the memory-only registry and owner-only claims/leases; id-only `dev_server` events and `/status.dev_watch` follow amendment 2 §B–§D.

WP12a (Codex) and WP12b (Opus) merged the same day, built against
`docs/hud-api.md` in parallel and joined at the end: the built HUD served
by `jarvis daemon2` on 8402 passed all 14 live checks against the real
backend on the first run, and `jarvis hud` opened it in the owner's Windows
browser. Two contract gaps the frontend found were closed in the backend
the same hour (`timeout_s` and `allowlistable` on the approval wire,
`task_id` on `proposal_reply`). The frontend's own suite found two real
bugs before merge — the wake recognizer surviving the OFF mode, and the
schedules form seeding from an unloaded list — and the backend's early
runs reset the live fast-path selection once, which is the allowlist
lesson again and is now isolated. R6's Codex half is answered by
`account/rateLimits/updated`.

**WP12c (same day, from the owner's first use):** a real New Project
button with a directory picker (`GET /fs/dirs`, home and `/mnt/<drive>`
only), chat threads draggable between projects (`PATCH /threads/{id}`;
task threads stay with their task), quota drawn as bars per reported
window beside a thinner local-allowance bar, and schedules as a dialog
with presets and a live preview (`POST /schedules/preview`) — plus
`schedule_create/list/delete` tools so "schedule a morning briefing at 8
on weekdays" works from chat and both CLIs, with plain-English `when`
parsing that refuses rather than guesses. One contract assumption the
frontend made (no path means home) was met in the backend on merge.

**First-use failure, same day:** the owner's first two chat messages did
nothing. New Thread sent `{project_id, role}`, the daemon required
`provider` and `brief` too, the 400 was swallowed by a `.catch(() => null)`,
and the daemon logged nothing. Three fixes: chat threads default to the
fast path server-side; the HUD opens a thread on first send and shows
every failure (`Could not send: …`) and every phase (SENDING · OPENING
THREAD · THINKING · RUNNING · tool · RESPONDING · FAILED); the daemon logs
its startup and every 4xx. Verified live: thread opened with the minimal
body, a message answered by the fast path in ~1 s. The lesson is old —
**a surface that can fail silently will, on the owner's first message.**

**New thread, and where a message goes (2026-10-06).** The state-map
review found the window kept a selected project and a selected thread that
nothing kept in step: clicking a project, creating one, or clicking
"+ new chat thread" under a project other than the selected one all sent
the next message somewhere other than where the sidebar pointed. The rule
now: **the chat pane decides where a message goes; the sidebar only shows
it.** The conversation is exactly one of an existing thread or a *compose*
row (a new thread not yet on the server). The project is derived from it,
never stored (`hud/src/lib/compose.ts`). As in the Claude Code desktop app,
**New thread** is a button pinned at the top of the sidebar. It opens a
compose row in the last project worked in, and the window boots into one.
The project chip in the input bar, or dragging the compose row, re-aims it
before the first message, and nothing reaches the server until that
message. After it, a thread moves by drag or menu, but **its working
directory stays the folder it was opened in**, along with its permission
rules: the brief is frozen at open, and the fast path refuses a strict
brief, so letting permissions follow a move would break chats moved into a
strict project. `Thread.cwd` makes the folder visible, and a moved thread's
row shows `↪ <folder>`. Two turn-tracking fixes came with it. `busy` follows
the thread whose turn this window started (`turnThreadId`), so switching
threads mid-turn no longer leaves the window on THINKING with the mic
suppressed. Another thread's `proposal_reply` and `turn_finished` no longer
reach the open chat or open the HUD's follow-up listening window.

**The model a thread runs on (2026-10-06).** Decisions part A, design
§8.1 and §12.1 amended. A chat thread picks its provider (OpenRouter,
Claude, Codex) while composing and its model and effort at any time, from
three chips in the input bar; the choice lives on the Thread record and
`brief.json` is never rewritten. It came with a bug fix that predates it:
the HUD had been sending `{id}`, `{name}` and `{mute}` to `/model`, `/voice`
and `/mute`, which read `model`, `voice` and `muted`, so every click reset
what it meant to set — and the mock accepted the wrong keys, so the suite
passed. Both the daemon and the mock now refuse an unknown key by name. And
a default thread follows the global Model picker on every turn; before, an
open thread kept its opening model until the daemon restarted.

**Projects: rename, edit, archive (2026-10-06, decisions part B).** A
project row has a `⋯` menu (Edit…, Rename…, Archive…), and project and
thread rows rename inline. A colliding name is numbered (`name (1)`), never
refused. A project's root can change: existing threads keep their frozen
`cwd`, and **tasks pin the root they were started under** (`Task.root`), so
a task already running or blocked creates, commits and removes its worktree
in the original repo. **Nothing is deleted by archiving**: an archived
project or thread keeps every record and disappears from the lists,
placement, schedule tools and the scheduler until restored. Permanent delete
exists only in the HUD's Archive view, only for something archived, and
moves Jarvis's records to a trash (`trash.py`: the Recycle Bin for
`/mnt/<drive>/`, else the freedesktop home trash, purged after 30 days); it
never touches the project's folder or a worktree. Archive and delete answer
only the HUD's own listener, and no tool reaches them (B11). Routes in
`docs/hud-api.md`.

**Zoom, folding panes and resizable edges (2026-10-08).** The owner asked to
enlarge or shrink the whole HUD and to fold or resize the side panes. The
rules live in `hud/src/lib/layout.ts`; `components/Layout.tsx` applies them.

- **Zoom** runs from 70% to 160% in 10% steps, default 100%. It is set from
  `− 100% +` (the percentage resets it) — in the window-wide title bar since
  2026-10-09; it used to sit in the status pane's header and folded away
  with it — or with
  Ctrl+= / Ctrl+- / Ctrl+0. The keys are the HUD's everywhere, including the
  input bar and Monaco. Neither binds them, and a key let through is
  Chrome's page zoom, which the HUD's control cannot see and Chrome
  remembers per site. It is CSS `zoom` on `#root`, because a page cannot set
  the browser's zoom and every size in `theme.css` is px. Viewport units
  inside `#root` scale with it, so every `vh`/`vw` cap divides by
  `--ui-zoom`. Undivided, the approval card's `86vh` was 138% of the screen
  at 160%. A menu placed from a screen rect divides by the zoom. Monaco
  already inverts the scale it measures.
- **The approval card is not changed otherwise, deliberately.** Its button
  row is *not* sticky: with AUTHORIZE always in reach, the tail of a long
  command (the `curl … | sh` on line 45) could stay unread. Reaching
  AUTHORIZE still means scrolling past the whole command. While a card is up,
  **every layout key is inert**, as push-to-talk and the wake word already
  are, so no zoom or fold re-lays the card out under a pointer that has not
  moved.
- **Folding.** Each pane has a `«`/`»` button and a shortcut: Ctrl+B for the
  left pane, Ctrl+Alt+B for the right. The shortcuts work from the input
  bar. They do nothing inside Monaco, where Ctrl+B completes the Ctrl+K
  Ctrl+B chord, on auto-repeat, or during IME composition. Ctrl+Shift+B is
  left to the browser's bookmarks bar, and AltGr's characters never match.
  A folded pane leaves a 36px rail with an expand button. The left rail also
  carries **+ (New thread)** and the orb, scaled into the rail's foot so it
  never sits on the input bar. The right rail shows pending authorizations
  (amber) and an error (red), so a folded pane hides nothing that wants the
  owner. A folded pane is hidden, never unmounted, so its expanded projects,
  selected thread, compose draft and inline renames survive.
- **Resizing.** The inner edge of each pane is a `role="separator"`. It can
  be dragged (pointer capture) or moved with the arrows (Shift for a bigger
  step, Home/End for the ends), and a double-click resets it. Space on a
  focused separator does nothing. The left pane runs 180–480px and
  the right 240–560px, **in the HUD's own unzoomed pixels**, so a pane keeps
  its proportion to its text as the zoom changes. Monaco relayouts through
  `automaticLayout`.
- **A small window keeps a usable centre.** The centre is held at 480px in
  two steps (`fitLayout`). First, the open panes give back their slack above
  their minimums. If even the minimums do not fit, the window folds a pane
  **for the render only**: right first, then left. Examples: the right pane
  at 150% on 1280px, both panes at 160% on 1024×700. The stored widths and
  folded flags are never rewritten, so the owner's layout returns when there
  is room. Opening a pane the window folded opens it and folds the other
  instead. Only a window under about 552 zoomed px leaves the centre
  narrower than 480, because both rails are already in place.
- **Persistence.** Zoom and layout persist in localStorage
  (`jarvis.hud.zoom`, `jarvis.hud.layout`). Every read and write is guarded,
  each field falls back to its own default, and only a literal `true` folds
  a pane, so a mangled value never hides one. Storage that throws on every
  call still opens at the defaults and simply forgets.
- **Wrapping.** The tab bar, the tools group inside it and the input bar's
  chip row wrap instead of clipping. On a narrow centre, Model · Voice ·
  Avatar · Settings move to another row, and at 1024×700 and 160% every tab
  and tool is checked to be on screen and clickable.

**The title bar, the layout toggles and a split centre (2026-10-09, WP-A of
`docs/plans/2026-10-09-hud-workspace-plan.md`; decisions in
`…-decisions.md`).** The owner asked for VS Code's layout buttons and for the
centre to hold two, three or four things at once. The rules live in
`hud/src/lib/workspace.ts` (pure, tested in `workspace.test.ts`);
`components/Workspace.tsx` draws the panes and `components/Layout.tsx` the
title bar.

- **A window-wide title bar, 28px, above the shell**, right-aligned: Model ·
  Voice · Avatar · Settings │ `− 100% +` │ ⊞ ◧ ⬓ ◨. Its right end is the
  window's top-right in every fold state, so the control that brings a pane
  back never folds away with it. The toggles are `aria-pressed` 16×16 window
  outlines with the area filled when it is **drawn** open; a pane the window
  folded reads closed, carries `data-auto` and a dashed outline, and opening
  it folds the other pane instead (the rail's rule). Under 560 zoomed px the
  four words fold into `⋯`; the zoom control and the toggles never hide, and
  the bar wraps rather than clipping.
- **⊞ Customize Layout is the one entry point for presets**: six tiles
  (single, two side by side, two stacked, three side by side, one large plus
  two stacked, 2×2; eight since WP-G1, below, adding 3×2 and 4×2, four to a
  row), the three toggles written out with their shortcuts,
  Reset layout (one pane, default widths, panel closed) and, when the window
  is drawing fewer panes than the preset has, "Showing 2 of 3 panes: the
  window is too narrow" plus a badge on ⊞. It is placed from the button's
  rect through `toCss()`, right-aligned; Escape closes it and gives focus
  back; the arrows move between tiles; Space on a tile presses it.
- **The pane model.** All four pane specs (eight since WP-G1, by id, with
  the drawn ids' reading order beside them) are always stored
  (`jarvis.hud.workspace`, apart from `jarvis.hud.layout` so damage to one
  never resets the other; each field falls back on its own, an unknown
  preset is `single`, the panel opens only on a literal `true`). A pane is
  mounted the first time a preset draws it and is then **hidden, never
  unmounted**. Edges are stored as fractions per preset and clamped to a
  360×200 pane minimum when drawn; they drag, take the arrows (Shift for a
  bigger step, Home/End) and reset to equal on a double-click. Ctrl+Alt+1 … 4
  (… 8 since WP-G1, by slot) focus a pane of the preset, bringing back one the window dropped
  (AltGr+digit types a symbol, so it never fires mid-word); the focused pane
  of a split has an accent line under its header.
- **Chat stayed a singleton in WP-A** (lifted by WP-B, below): choosing
  chat in another pane swapped the two panes' **whole specs**. A sidebar
  click goes to the pane already showing
  that kind of thing, else to the focused pane — a task click leaves a drawn
  task or diff pane following the selection. "Read" means shown in any
  drawn pane. The profile select rides the chat pane's header (the focused
  pane's when no chat is drawn), so the single layout is unchanged.
- **A File pane is one project's.** `FileTab` used to save to whatever
  project was current at save time; a file pane left open while the chat
  moved would have written into the wrong project. A File pane now pins its
  project when it opens a file (Preview when it loads the project root),
  `FileTab` is keyed by that project, and the header says `in: <project> ·
  follow chat` whenever the pin differs from the chat's. Preview keeps its
  URL per pane and judges it again before loading it again. **An unsaved
  edit is never dropped silently** (the minimal guard, before the editor
  plan): `FileTab` reports a dirty buffer, "follow chat" is disabled while
  it is (its tooltip says why), a pinned project that goes away keeps the
  pane pinned with "project gone · unsaved edit kept · discard edit" until
  the owner discards it, and closing the window asks (`beforeunload`).
  Switching a pane's view away from File still closes the buffer, as the
  old tab switch did.
- **Fitting** (`fitWorkspace`) changes only the render. Across, the side
  panes give back their slack and fold first (right, then left, `prefer`
  honoured, now against the drawn shape's own centre minimum: 480 / 720 /
  480 / 1080 / 840 / 720); only then are columns dropped, keeping the focused
  pane's column — the focused pane is never dropped. Down, an open panel
  shrinks to its 120px minimum, then folds, and only then are rows dropped;
  opening a panel the window folded drops rows instead. One worked case
  differs from the plan's table and follows its stated order: 2×2 plus the
  panel at 1024×700 and 160% draws the focused column (two stacked) with the
  panel folded; opening the panel by hand gives one pane and the panel.
  **A dropped set is sticky**: which panes survive a drop is chosen from the
  focused pane, and focus follows every click, so clicking the other pane
  of three-columns-drawn-as-two used to redraw a different pair under the
  pointer. The last render's set (a render-only ref, never stored) is kept
  while the preset and the drawn shape are the same and it still holds the
  focused pane; focusing a pane it does not draw draws one that does.
- **The bottom panel holds the terminals (WP-D, below)**: ⬓ and Ctrl+` (no
  Alt — AltGr+7 is a backtick on French layouts; a dead backtick matches on
  `code`) show and hide it under the panes, between the side panes; its top
  edge is a horizontal separator (Up/Down, double-click resets to 260px).
- **Under a card nothing moves**: every title-bar button, fold button and
  rail button is disabled (the veil stops the pointer, but a button that kept
  focus still answered Enter — on main it folded a pane behind the card), an
  open menu closes, a focused separator ignores its keys, and Ctrl+` and
  Ctrl+Alt+N are swallowed like the other layout keys. **The card takes the
  keys**: a focused preview iframe got its own key events, so Escape never
  reached the card and keys went on into the framed page, and Monaco kept
  typing into the buffer behind it. When a card comes up while focus is in a
  frame, the workspace or the panel, the card container (`tabIndex=-1`, an
  `alertdialog`) takes focus — never a button, nothing is keyboard-defaulted
  — and gives it back when the last card goes. A 2×2 grid with Monaco and a
  preview frame under the card keeps the card wholly on screen with every
  button topmost, at 160% and 70%; that AUTHORIZE is below the fold for a
  long command is asserted at 160%.
- **Free checks**: `workspace.test.ts`, `layout.test.ts` (the new keys,
  AltGr symbols, `keyStep`) and `hud_v2_layout_check`'s `_titlebar_checks`,
  `_workspace_checks`, `_grid_card_checks`, `_sticky_checks`,
  `_card_focus_checks` and `_seen_checks` — the last three were each shown
  to fail against the bug they guard. The layout section runs against a
  `MockDaemon` of its own on an ephemeral port, so its saves, `/seen` posts,
  approval decisions and preview hits never reach the world the main suite
  asserts on. Every existing suite passes unchanged in the single layout.

**Several chats at once (2026-10-09, WP-B of the same plan, §2.2; decisions
W-6).** WP-A kept one conversation; the owner asked for two chats side by side
as two separate conversations. The pure rules are `hud/src/lib/chats.ts`
(tested in `chats.test.ts`); the store holds `chats[pane]`, `selectedChat`
and `selectedOrder`.

- **Each chat pane is its own conversation**, with its own input box,
  project chip, model chips, Send/Steer and Stop. Where you type is where it
  goes — a shared box would need a "which pane?" indicator and would be one
  keystroke from the wrong thread. The per-pane state is exactly what was
  window-wide: thread or compose row, the turn tracked, busy, status,
  transcript, draft, tool ticker, hand-backs, a REVIEW transcript, and the
  box's text and staged files. Send, steer, Stop, give-back, held-back words
  and the 15-second reconcile are the same code keyed by pane, so the single
  layout is unchanged.
- **What a conversation owns moves with the conversation, never with the
  pane** (review of PR #27). A pane that trades conversations with another
  takes its box text, files and REVIEW transcript along. A pane that changes
  thread parks its unsent text under the conversation it leaves (keyed by
  thread, or by compose row) and gets back what the new one had, so a draft
  is never left in a pane now showing another thread. `send()` re-finds its
  pane by conversation after every await — a pane traded away or moved on
  while the request was out is not patched (this also fixed New thread
  pressed while a compose send was in flight, in one pane or two). The
  transcript loader patches whichever pane shows that thread when it
  arrives and drops it if none does; a pane counts as loaded only once its
  transcript has arrived. A hand-back goes to a drawn pane showing its
  thread; if none does, it is held and comes back when one does — never into
  another conversation, and never into the selected chat as a fallback. A
  first send that fails after another thread was opened in its pane parks its
  words under its compose row; the next new thread in that pane gets them
  back, and the error says so (re-review of PR #27). Files read in for a box
  (a drop, the picker, a paste) are added, once ready, to the pane holding
  the conversation they were dropped for, else to its parked draft. A first
  send's turn is tracked under a placeholder until its thread exists; that
  id never reaches the daemon — a Stop or orb press meanwhile is held and
  interrupts the thread once the message is in. A model change whose answer
  arrives late updates the compose row holding that thread at that moment,
  not the pane it was asked from.
- **The single layout changes in one way, deliberately: a draft belongs to
  its conversation.** Typing in a thread and then opening another thread, or
  pressing New thread, parks the words (main left them in the box); they
  come back when that conversation is reopened. New thread while composing
  still keeps them. There is no sidebar marker for a parked draft yet.
- **Typing re-renders the window**, because the box's words are the store's
  (they must move with the conversation), so the transcript (`ChatTab`) is
  memoized with stable props: redrawing a long transcript per keystroke cost
  milliseconds per character, per chat pane.
- **Routing.** An SSE event with a `thread_id` reaches the panes *showing*
  that thread (deltas, tools, replies, Discord messages, model lines,
  proposals) and those *tracking its turn* (the finish, an error), so a pane
  that moved on mid-turn still hears it end. An event with no thread goes to
  the selected chat. Approvals, activity, task and project lifecycle stay
  window-wide.
- **Ambiguous input goes to the selected chat** — the owner's rule (W-6):
  "any input that is ambiguous and may go into the wrong chat goes into the
  currently selected chat: with one chat open, that one; with four, the one
  I most recently clicked on." The selected chat is the chat pane most
  recently clicked: a pointer or keyboard focus into it, Ctrl+Alt+N, or a
  sidebar click that opens a thread in it. Clicking a pane that is not a chat
  (Preview, File, a task) does not change it. It is always a drawn chat
  pane: when the layout drops it, the chat pane selected before it takes
  over; one chat drawn is the selected one; with none drawn there is none.
  Ambiguous means a dictated transcript (AUTO sends it, REVIEW puts it in the
  box), the orb's press and interrupt, push-to-talk, the follow-up window,
  files dropped on the window outside any pane, and an SSE event with no
  thread. A transcript goes to **the selected chat as it is when the
  transcript lands**: clicking another chat while STT runs sends it there.
  Input that belongs to a thread — typed text, queued and steered messages,
  hand-backs and held-back words, a send's own result, a transcript load —
  stays with its thread; a hand-back whose thread is off screen is held,
  never put in the selected chat. With no chat drawn, nothing is ever sent:
  speech waits, unsent, in the box of the next chat selected, and a press or
  a dropped file is refused with a sentence.
- **The selected chat is marked and drives the voice.** Only its input bar
  draws the dictation strip (AUTO / REVIEW / OFF, the meter, the hint), so
  the strip shows where speech lands; another chat pane shows only its own
  turn's status. In a split its header carries an accent bar and a mic mark
  (`pane-N-mic`, `data-selected`). The orb follows and interrupts its turn,
  the follow-up window opens only when its own turn finishes, capture is
  suppressed only while *its* turn runs — a long turn in another pane no
  longer silences the mic — and a capture status ("LISTENING · SPEAK NOW",
  "TRANSCRIBING", "STT FAILED" …) moves to the newly selected chat rather
  than staying stale in a pane that no longer draws the strip.
- **A thread is open in one pane at most.** A click on a thread a drawn pane
  shows focuses that pane. A thread held off screen — by a pane the preset
  hides, or a pane now showing another view — trades conversations with the
  pane it is opened in, so nothing either held (a running turn, hand-backs,
  the box) is dropped. A pane that switched threads mid-turn hands that
  turn, Stop included, to the pane that opens the thread; if that pane is
  itself waiting on a turn of its own, the two trade tracked turns, so
  neither turn is left without a Stop (it used to refuse, which stranded the
  first). A thread click lands in the pane showing it, else the focused chat
  pane, else the selected chat, else the focused pane switches to chat.
  **Alt+click, or "Open beside" in the row's ⋯ menu**, opens the next pane
  to the right: from one pane two columns, from the right of two a third
  column, and round to the first in other shapes.
- **The sidebar** marks every drawn chat pane's thread — the selected one
  with the accent, the others with a dimmer mark, each row carrying
  `data-pane` — and the selected chat's conversation even when its pane shows
  something else, as the one conversation always was. Each composing pane
  has its own compose row, numbered by pane ("New thread · 2") when more than
  one is marked. A pane that shows chat for the first time opens a new thread
  in the last project worked in.
- **"Read"** covers every drawn chat pane's thread, focused or not.
- **An unsaved edit is never switched away.** A File pane holding a dirty
  buffer refuses every view switch — its own tabs, Open beside, a sidebar
  thread or task landing there, New thread — and says so where the owner
  clicked: in its own header ("unsaved edit · save or reload first",
  `pane-N-refused`), or, when the refusing pane is not drawn, in the focused
  pane ("pane N has an unsaved edit"). The WP-A guard covered follow-chat and
  a gone project; a tab switch still closed the buffer.
- **Nothing outside the card can be reached while it is up.** The card takes
  focus off anything outside it, not only a frame, the workspace or the
  panel (a sidebar button that kept focus still answered Enter behind the
  veil), and everything outside the card is made `inert` until the last card
  goes, while Tab and Shift+Tab stay on the card — Tab used to walk focus
  back out behind the veil. AUTHORIZE and ALWAYS are out of the Tab order
  (`tabIndex=-1`), so no Tab or Shift+Tab puts one under an Enter: they take
  a click, and DENY is the only stop. Focus goes to the card container,
  never a button, and comes back (after `inert` is lifted) when the last
  card goes — to the chat box too, which is no longer `disabled` under a
  card (inert covers it; a disabled box lost focus before the card could
  record it). Each card is keyed by its request, so a queued card is a card
  of its own, with live buttons and its own countdown, and takes the focus
  (the answered card's busy state used to carry over; main had this too).
- **A file dropped on a sidebar project row** is staged in the selected
  chat like any other drop outside a box; the row used to swallow it.
- **Two single-layout quirks fixed on the way**: a chat pane's project chip
  and profile select read its own conversation's project (after a task was
  picked they showed the task's while the message went elsewhere), and a
  project archived with only its *task* open no longer clears an unrelated
  chat's transcript (its messages were emptied and never reloaded).
- **Free checks**: `chats.test.ts`, per-pane cases in `store.test.ts` and
  `compose.test.ts`, and `tests/face/hud_v2_multichat_check.py` — two
  threads side by side, a turn in one pane while the owner sends in the
  other, every event kind to its pane, one pane's Stop never reaching the
  other, voice to the selected chat, never twice, `/seen` for both, the
  steer and give-back cases re-run in pane 2, the card taking focus from a
  sidebar button, and a dirty File pane refusing every switch; then one
  `review:` check per PR #27 finding and one `W-6:` check per case of the
  rule (four chats in a 2×2 grid, a click during STT, a selected chat
  dropped by a narrow window, one chat and none, an off-screen hand-back),
  each shown to fail on the commit before the fixes; and `review 2:` checks
  for the re-review (placeholder Stop, two queued cards, the Tab round,
  late chip answers, a failed send after its pane moved, file drops), each
  failing on feb61ff and naming the mutation it kills. It runs after the
  layout section on a `MockDaemon` of its own, or alone. WP-A's three checks
  of the single-chat swap were rewritten for the new rule.

**Steering a running turn (2026-10-08).** The owner: "I can't steer claude or codex sessions while they are working
if I want them to do something differently or whatnot and it just throws an
error and I can't continue the session." A send during a turn answered 409,
and the HUD's catch reset `busy` and `turnThreadId`: the running turn lost
its Stop (the orb interrupts only while busy), the mic was unsuppressed under
it, and the window believed nothing was running until the turn ended on its
own — minutes, for a Claude or Codex turn. (The daemon itself never wedged,
and the providers' fatal "already running" error was unreachable through it,
but it would have dropped and closed the running session; it is non-fatal
now.)

- **One rule, every surface.** `POST /threads/{id}/send` and a Discord
  message go through `Daemon.deliver`: start a turn, **steer** the running
  one (§5.1), or **queue** behind it (three at most, in order; a turn waiting
  on an approval or a question is never steered, so a message can never read
  as its answer — the card stays until the owner answers it). A provider with
  no way to steer has its turn interrupted for the message, which runs next
  with a note to the model. The escape hatch's result goes the same way, so
  it is no longer lost when a chat turn happens to be running. A task's
  threads keep refusing: their turns are the runner's, and `Daemon.send` (the
  runner's) still answers "already running", which it retries on.
- **Stop means stop.** The owner's interrupt drops what waits
  (`queue_cleared`), as Claude Code hands queued messages back to the input
  on Esc; the HUD puts the words it sent back in the box — into the box of
  the thread they were typed in: words for a thread not on screen wait until
  the owner opens it, and each hand-back is taken exactly once. A steer the
  provider still held when Stop landed, and then refused, is dropped the
  same way (`"dropped"`), judged by the turn it was aimed at even if a newer
  turn has started — never requeued, never an interrupt (Bugbot on PR
  #22). A steer already in
  the CLI's own queue when Stop lands makes a fresh CLI turn, which Claude's
  provider interrupts too — and when that turn starts only after the stopped
  turn has ended, the next send that reads it interrupts it and drops its
  words (review of PR #22, round 2). Such a late turn still runs every tool
  call through `permit`, so in that gap an approval card can appear on a
  thread that looks idle: it is the stopped steer asking. Deny is the safe
  answer, and the next send stops that turn.
- **HUD.** While a turn runs in the open thread, Enter steers it: the line
  is drawn at once marked *steering* (or *queued · will run when this turn
  ends*, cleared by `queued_started`; *not sent* after
  a Stop), the box clears, and the window keeps tracking the running turn. A
  **Stop** button sits beside the input while a turn runs (the orb still
  interrupts too). A refused send is said inline and costs nothing: its
  bubble alone is taken back (a reply that settled meanwhile stays) and its
  words and files return to the box. An `error` event no longer ends the
  window's turn — `turn_finished` does — and a missed `turn_finished` is
  caught from the thread record's live `running`, on every SSE reconnect and
  every 15 s while busy. A `turn_finished` carrying `next` (messages that
  run at once) keeps the window on the thread: no idle flash, no follow-up
  mic window.

**The terminal backend (2026-10-09, WP-C of
`docs/plans/2026-10-09-hud-workspace-plan.md`; decisions W-2, W-3, W-5).**
The owner's terminals now exist on the daemon; the panel and view are WP-D.
A terminal is the owner's login shell on a real PTY, started through
util-linux `setsid --ctty` (never `pty.fork()` in a threaded daemon), in a
clean environment (nothing from `.env`, no `OPENROUTER_API_KEY`,
`HF_HUB_OFFLINE`, `VIRTUAL_ENV` or venv `PATH`), in the folder of a
thread, task, project or home — resolved from ids, never a path. At most
six; each keeps 1 MiB of output in memory, replayed when a window
reattaches; closing one SIGHUPs its whole session, then SIGKILLs; they end
with the daemon (no tmux). The transport is a WebSocket on the HUD listener,
hand-rolled (`jarvis/v2/ws.py`, no new dependency). **The agent never gets a
lever on a terminal**: no tool names the module or its routes (asserted in
`tests/v2/terminal_check.py`), every route is owner-only, the socket also
needs a single-use 30-second ticket bound to one terminal, and moving a
terminal to another window asks the window that shows it (refused after
20 s). That is stricter than `/approvals`, which accepts a local client that
sends no Origin, and it is still not a boundary against a program running as
the owner, which can forge an Origin, fetch a ticket and attach to a
terminal no window shows. Three things limit such a program: the takeover
question, `sudo -k`, and a `terminal_attached` bus record on every attach
(the terminal's id and the time, nothing else), so the HUD can say when an
attach was not its own. Output exists in the ring and the browser only:
never on the bus (lifecycle ids only), in a log, a thread log, Discord or
disk. Input goes through a bounded queue and a writer thread per terminal,
so a program that never reads cannot stall the socket; once a paste frame is
dropped for want of room the socket refuses all input but Ctrl-C until the
queue drains and the window sends `input_resume`, so a program gets a
prefix of the paste, never a spliced one. A closed session is matched on its
leader's start time, so a reused pid is never signalled. The shell's
realpath runs under the name it was given, so `rbash` stays restricted. The
startup file (`terminal_rc.bash` for bash; the POSIX `terminal_rc.sh` as
`$ENV` for an `sh`-family shell, which marks only the prompt; nothing for
zsh, fish and the like, which the listing reports as `integration: "none"`)
never touches disk — it reaches the shell through a pipe the shell drains
before running anything — then reads the login files, sets `alias
sudo='sudo -k'`, and emits OSC 133 marks signed with a per-terminal nonce
that nothing the terminal runs has an ordinary way to find (a same-uid
program outside it could race the shell for the pipe, at the price of
leaving the shell unintegrated). The daemon turns them into
per-command spans over the ring for WP-F's `terminal_read`. **The spans are
advisory, not a boundary**: a program can print any bytes between real
marks, a nested shell, `sudo -i`, `ssh` or `python` puts everything under
the outer span, and a line kept out of history records only its first
command — so WP-F's text-pattern refusals of `env`, `gh auth token` and the
rest (W-2, item 3) must apply to **every** read, not only when no marks
were seen. Each terminal carries the owner's `readable` switch, on by
default. Contract: `docs/hud-api.md`. Since WP-D the listing also says
whether the startup file *took* (`marked`, on the first signed prompt mark):
`integration` is only what was configured, and a profile that `exec`s
another shell configures "bash" and marks nothing. The startup file must fit
8 KiB and is written without blocking (a blocking write past a small pipe
would have wedged every terminal route), and the launcher always runs the
checked realpath, refusing a renamed shell where `env` cannot set argv[0].

**The terminal panel and view (2026-10-09, WP-D).** The owner's terminals
are drawn with xterm.js (pinned exactly, lazy-loaded, no clipboard addon, no
window reports) in the bottom panel — a tab each, `+` in the focused pane's
folder, `▾` for Home or a project — and in any pane's terminal view; **one
terminal is drawn in one place at a time** (W-1), and its panel tab says
which pane has it. A window keeps one session per terminal: it attaches on
first draw with a fresh ticket and a URL from its own location, resizes only
after the replay, and stays attached while hidden, so moving a terminal
between the panel and a pane, or hiding the panel, never reattaches. Opening
the panel — Ctrl+`, ⬓ or the layout menu's Panel row — opens a terminal when
there is none, and so does switching a pane to the terminal view (the
owner's request, 2026-10-11: one click, not two; WP-D had ⬓ only show the
panel). Only on that owner gesture: never on a load, a restored layout, a
reload or a reattach, never under a card (nor once one has come and gone
since), never on a failed listing or when any terminal is listed, never once
the panel was closed again or the pane switched away (the terminal then stays
in the list, shown nowhere), and one per gesture — a double-click is one. A dot on ⬓ says one in the
hidden panel exited. In a terminal Ctrl+B, Ctrl+Alt+B and
Ctrl+_ are the shell's, and so is Ctrl+D (end of input, never push-to-talk);
the zoom keys, Ctrl+` and Ctrl+Alt+N stay the HUD's. **Under a card nothing reaches the shell** —
input held, a paste in flight paused, every key left to bubble so Escape
denies — and output keeps drawing. **A paste never splices**: it goes out in
paced 16 KiB chunks from a queue that belongs to one socket, stops the
moment the daemon drops a frame, waits for the owner's Resume before typing
goes again, and dies with its socket rather than continuing on the next one;
and it is inert as a control stream (ESC stripped), so a pasted end-of-paste
marker cannot end bracketed paste early. **A replay is never answered**:
xterm answers some output (a cursor-position or colour query) through the
input channel, so nothing is sent from a new socket until its replay has been
parsed, and an older socket's queued output is never parsed into a newer one.
**A terminal another window shows is never taken unasked** — it reads "in
another window · Show it here" until the owner says so. Another window's
takeover is asked here (Let it / Keep it, unanswerable
under a card, so kept); exit, Jarvis's restart, "taken" and "refused" each
say so with the next step offered; a busy terminal is asked about before it
closes; an attach this window did not make is a quiet notice. Output is
hostile bytes: a title it sets is text in the pane header, never the
window's title (the desktop bridge matches on `J.A.R.V.I.S.`), and a link
opens only for http(s) on Ctrl+click. xterm under the HUD's CSS zoom is
counter-zoomed with its font scaled instead; the 160% and 70% checks guard
fit and selection. The headless suite plays the PTY inside the browser
(`route_web_socket`), so no shell ever runs, and the live-port guard now
refuses WebSockets too. "Open in Preview" for a dev server's link is WP-E.

**A terminal that ended with Jarvis continues where it was (2026-10-12, the
owner's option (a)).** The owner typed into a terminal whose history was
still on screen after a restart and got only "New terminal here". Now a
typing key there, a paste or **Continue here** starts a new shell in the same
folder, with the same "Jarvis can read" switch (off when unknown), in the
same place and the same xterm, under the old text and a `── restarted HH:MM
──` line; what was typed waits (4 KiB) until the new shell's replay has been
parsed, and is then **typed, never run** (the lead's decision on review):
the keys up to the first Enter, and no paste with a line break, because the
line was typed at the old context's prompt (a robot's shell, `>>>`, a root
shell) and must not run in a fresh local one; the window says so. Any
refusal while held — a key that does not type, past 4 KiB, a card, the
terminal leaving the screen — drops all of it. The old text is rebuilt from
xterm's cells (characters and colours, the last 1 MiB, read from the bottom),
kept in this page's memory only, and written back after the RIS of every
later attach of the new terminal. Running programs do not survive —
only their text — and a page reload loses the text: real survival would need
a pty host outside the daemon, which would reverse W-3 (option (b), not
taken). Only the owner's gesture in that terminal continues it: never a
card, a crash, a load, a reattach, another window's terminal, a shell that
exited on its own, a program terminal, Ctrl+D or a HUD key. No route changed.

**Jarvis can read a terminal (2026-10-10, WP-F; decisions W-2).**
`terminal_read(terminal, lines=200)` returns the last lines (at most 1000) of
one terminal's **normal buffer** as plain text — escape sequences stripped,
CR/backspace/cursor redraws collapsed to what the terminal shows, the
alternate screen (vim, less, top) never read, even when the switch into it has
left the ring (`terminal_text.AltTracker` is fed every byte the ring drops) —
fenced as untrusted like a fetched page, and through `dispatch()`'s scrub. It
is read-only structurally: the tool imports exactly `terminals.read_for_tool`.
**Three rules refuse the whole read** with one sentence, "possible credential
in this output", that never says what, where or which rule: an exact known
value (every `.env` and token-bundle value Jarvis reaches plus the terminal
folder's `.env` files, and their base64 at any alignment and URL-encodings),
a known credential format (`jarvis/credential_patterns.py`), or a
secret-printing command (`env`, `printenv`, `cat .env`, `gh auth token`, …,
one list in `terminal_guard`). The last has two halves and the second always
runs: a signed span overlapping the read (or one that backgrounds a printer),
and the text — any line drawn in the read, or in the 32 KiB before it, that
shows such a command after a prompt, judged on every state a line was in (a
cleared command line still counts) — narrowed only at a boundary the signed
marks prove, never at a span record below `spans_from`. A heuristic withholds
lines (a keyword with a long high-entropy token after it; a bare hash or UUID
stays) and says how many. The owner's per-terminal switch refuses by name,
and every read that reaches a terminal publishes `terminal_read`
`{terminal_id, lines, at, refused}` so its bar says "Jarvis read 200 lines ·
15:42". **Who holds it**: the fast path, for an owner's chat turn the HUD's
own window sent (`UserMessage.desk`, set only through the HUD listener with
its Origin) — not a task's thread, a Discord or DM turn (the owner is not at
the desk), the escape hatch, a schedule, a sub-agent, a workflow, a goal or
any v1 surface (`tools.EXPLICIT_ONLY` keeps it out of "the whole registry"),
and not jarvis-mcp, which cannot tell a chat from a task worker until the
peers plan's per-session tokens land, so it fails closed. A steer from
anywhere but the desk takes the running turn off it. The owner confirmed both
limits on 2026-10-10: fast-path desk turns only for now (Claude and Codex
chats via jarvis-mcp are a follow-up), and never Discord, DM or scheduled
turns. **Where read text goes**: like any tool result, into the fast-path
transcript (and so OpenRouter), that conversation's v1 session
`messages.json`, `SPILL_DIR` if truncation cuts it later, and whatever the
model's reply repeats (mirrored to Discord, spoken). The tool's ticker
summary is fixed ("read N lines"), so the tool's own `tool_finished` record
carries none of it — that is all the fixed summary promises: the model's
reply is not filtered, and goes to the thread's `log.jsonl`, the bus, Discord
and speech like any reply, so whatever it repeats lands there too.
**The review round (2026-10-10)** gave the renderer the terminal's width
(xterm's autowrap, clamped cursor numbers, capped rows), refused a read that
starts inside a private key's block or shows its END footer, taught rule 3
the shell's compound shapes (`then`/`do`/`!`/`case … )`, redirections,
substitutions, `find -exec`/`xargs` readers, `.env.*`), made every scan
linear (a 1 MiB adversarial ring is judged in well under 2 s), handed a
switch the ring's cut split in two over whole, executed C0 controls inside a
sequence as xterm does, re-checked the switch after the render, and read
folder env files so a FIFO cannot hang a read. **The re-review
(2026-10-11)** made the headline read readable again — a dev server started
with `node --env-file=.env` (an `--env-file` loads a file; an interpreter
prints one only from inline source, an environment read, or as its script),
`wc -l < .env`, `docker inspect` with a safe `--format`, and a reader's name
in a commit message or banner were refused — and closed the other side:
copies onto the terminal, `file:` URLs, archive members and compressors to
stdout, whole-environment dumps, and every parse bound, which now fails
closed when the part it leaves unread could hold a printer. A read parses
at most a 128 KiB budget of commands it has not judged before and is refused
past it (an adversarial screen took 18.6 s and 85.7 s). **The limit**: a
secret with no recognisable shape and no keyword beside it, or one printed in
pieces or encoded otherwise, is not caught, and rule 3 cannot see through an
alias, a shell function, a script, a symlink, a script handed a credential
file as an argument, or a program piped in from anything but a literal
`echo`/`printf`; the switch and the note are the backstops. The delta review
(same day) made node's and bun's `-pe` take the next argument as its source
(eb718b5 read `node -pe process.env`, and `node --env-file=.env -pe
process.env`, refused at 3393daf), judged source fed on standard input and
REPL input lines, and kept the `env $(cat .env | xargs)` loading idioms
refused as an accepted cost.

**Preview and dev servers (2026-10-10, WP-E; decisions W-4).** **No HUD or
API response may be framed**: every response on 8402 and 8405 — the page,
JSON, SSE, every error, http.server's own refusals of a malformed request
(answered as HTTP/1.0, never a header-less 0.9 body) and the terminal
socket's hand-written `101` — carries `Content-Security-Policy:
frame-ancestors 'none'` and `X-Frame-Options: DENY`, added in the daemon
handler's `end_headers`, the one place every status line passes; one
function builds the policy (`hud_api.content_security_policy`, which the
editor's full CSP will extend, and which refuses a response's own
directives holding a comma — a second policy — anything outside printable
ASCII, or a reserved
`frame-ancestors`/`sandbox`/`report-*`). The preview origin (8403) carries
neither, but sandboxes every document it serves with its own CSP (`sandbox
allow-scripts allow-forms`), so a keep-origin frame that navigates itself
to the workshop lands opaque (PR #31 review).
`/status` reports `hud_port`, `api_port` and `frame_hardened`, and the
Preview pane refuses the HUD's port, the API's port (both as reported and
the live defaults) and the window's own. **A dev app may keep its own
origin**, per pane, off by default: the "Keep its origin" switch adds
`allow-same-origin`, offered only for a loopback port that is none of the
daemon's three, and only while `/status` says `frame_hardened` — so a HUD
served by an older daemon never offers it. It is stored as the origin it
was given for, so another port, host or scheme turns it off, and a stored
one is judged again on every load and dropped when it no longer passes. It
is safe because scripts plus same-origin are no sandbox only for a page on
the parent's origin: the port check refuses that up front, and
`frame-ancestors` stops a frame from navigating itself into the HUD later.
The frame never gets top navigation, popups or downloads. A Ctrl+clicked
link to a server on this machine opens a small menu — **Open in Preview**
(a drawn Preview pane, else the focused pane, by the click rule; refused,
with the reason, for the daemon's ports) or **Open in a browser tab**; any
other link opens in a new tab as before; the menu flips above a click on
the panel terminal's last row and stays inside the window at any zoom.
Free suites: `tests/v2/
frame_check.py` (every route family on both listeners, checked against the
route modules' source), `preview.test.ts`, `hud_v2_preview_check.py` (now `browser.test.ts` and `hud_v2_browser_check.py`, BR-3a) and
the terminal suite's link section. Not yet: a "dev servers in your
terminals" list (§2.5 item 5). **No framed page takes the keyboard on its
own** (2026-10-12, R-1, AO-2's guard): a Browser frame that gains focus
without the owner's gesture (the pointer over it, Tab, their own load, a
card's give-back) is removed at once, focus goes back only to the HUD element
that lost it to that theft (else to nothing that types or sends), and the
pane says "This page took the keyboard · Reload" — always under a card or
from a hidden pane. While a HUD text control has the keyboard, only a click on
a page gives it the keyboard (the first click only focuses it).

**Dev servers announced in a terminal (2026-10-12, BR-8a of
`docs/plans/2026-10-10-hud-browser-plan.md`, amendment 1 §D.1; decisions
B-2).** The daemon half of "a dev server started in a HUD terminal opens in a
Browser pane by itself"; BR-8b, the window half, opens the pane
(`lib/devopen.ts`, BR-2). While the owner's **Open dev servers
automatically** switch is on (`browser.json` → `auto_open`, on by default,
protected state, changed only by the owner-only `PATCH /browser/settings`;
one file beside BR-1a's `lan` and `local_servers`, read and written only by
`jarvis/v2/browser/settings.py`, which the gate, the host, `/status` and the
terminals all share),
`Terminal._output` feeds each shell terminal's **live** output to
`jarvis/v2/devurl.py`, under the lock it already holds. It reads only a
command's output — from the shell's signed OSC 133 marks it skips the prompt
and the typed line (bash: `B` to `C`; a shell that marks no command, like the
POSIX startup file: `B` to the end of that line), so typing `curl
http://localhost:3000` opens nothing — never the alternate screen, and it
strips escapes by `terminal_text`'s own grammar. A line that announces a
server in one of the known shapes (Vite, Next, Create React App, Angular,
webpack-dev-server, `python -m http.server`, Flask, uvicorn, Django, Puma,
Hugo, Jekyll, Streamlit, Gradio, `php -S`, `serve`, Expo, Jupyter, Astro,
Storybook …) gives its URL, kept only if it is loopback http(s) with an
explicit port, no user name, and none of Jarvis's ports
(`config.control_ports()`, the workshop's, and the daemon's listeners as
bound), with the query and fragment dropped and the host always written
`localhost` (the window can frame no other loopback name). Two cheap tests
run first and change no answer — a loopback host spelling and one of the
shapes' words, as substring searches over a chunk's whole run of lines — and
a chunk matches at most 32 lines against the table, so URL-dense output stays
far above what xterm draws (PR #70 review: another host's URLs had slowed
the terminal to ~9 MB/s). It is sent
as one `{"type": "dev_url", "url"}` frame **on that terminal's own attached
socket**, after the bytes that carried it — once per origin among the last
four, at most one a second and four a minute per terminal, recorded only once
the socket has taken the frame; with no window attached it is dropped and not
remembered, and a ring replay never sends one. **Nothing else ever carries it**: no bus record,
log line, thread log, Discord post or file, and Jarvis never sees it (the
"Jarvis can read" switch is unrelated: this is the owner's window).
**Auto-open never opens a port in the Remote gate** (amendment 2): the
window loads the URL only in a Direct frame, and only the owner's own
navigation opens a loopback port there. The sniffer holds at most a 512-byte line, a 64-byte escape carry and four
origins. Its limits: a server started with `&` in bash whose banner lands
while the prompt is up is skipped (it sits between `B` and `C`); input typed
ahead while a command runs is echoed inside its output and read as output;
text that only looks like a banner (`cat` of a log) is read as one, and the
window judges every URL again. Free suites: `tests/v2/devurl_check.py` (every
shape split anywhere with escapes, the refusals, the marks, the bounds, the
rate limit, the settings file) and `terminal_check`'s `DevUrls` (the frame
on that socket alone; nothing on the bus, in a log or a file; the switch
off feeds the sniffer nothing; the switch is the owner's).

**The VEX backend (2026-10-11, VX-1 of
`docs/plans/2026-10-09-hud-editor-plan.md`; decisions V-1 to V-5).** The HUD
can build a VEX V5 C++ project, download it to a slot on the brain, open the
brain's user port as a terminal and make a new project from VEX's own
templates — with the Windows `vexcom.exe` and toolchain the owner's VS Code
VEX extension already downloaded, **run in place through WSL interop**:
nothing copied, bundled or committed (VEX's licence forbids redistribution
and decompiling, so the only `vexcom` flags used are DishPy's: `--name
--slot --write --progress` and `--user` — plus the two reads the owner's
own `vexcom --help` lists, `--json` and `--programs`). **Which files those are is
`vex.json`**, written only by the human-run `jarvis vex setup` (it looks in
the extension's own folders for VS Code and Cursor, prints what it found,
checks it by the daemon's own rule, asks y/N, writes mode 600) and refused to
every agent write tool, v1 and v2, as gate state; **every path in it is
re-checked at each use** — absolute, no symlink anywhere in it, a regular
executable (or folder), inside the extension's downloads or a
`vexrobotics.vexcode-*` install — or the action is refused with "run `jarvis
vex setup` again". Because those folders are under AppData, which an agent can
write, setup also records each tool's SHA-256 and every spawn hashes the tools
it is about to run first (review round 1); a swap between the final check and
`exec`, the tools `make.exe` opens later, and a DLL planted beside a tool in
its AppData folder (Windows loads a program's DLLs from its own folder first;
the fingerprint covers the executable only) remain. This is the narrow, deliberate exception to "nothing under
`/mnt/` ever runs" (which is about resolving the Claude and Codex CLIs). A
request never names an executable, a path or an argument (the `DESKTOP_APPS`
pattern): the argv tables live in `vex.py`, a project comes from its id, a
build takes `{action}` and a download `{slot, name}`, validated first; Windows
paths come from one function that refuses a character a command line or
make's `cmd.exe` recipes could misread, and anything outside ASCII (Windows'
"best fit" maps fullwidth quotes and slashes to syntax). Programs run as **program terminals**
(`terminals.create_program`, called from `vex.py` alone): the same PTY, ring,
attach and close as the owner's shells, with an environment that empties
`WSLENV` so nothing of the daemon's crosses into Windows, no typed input but
Ctrl-C except the brain's, and an exit hook that parses `file:line:col:
error|warning:` into an errors list. One build per project (a lock file shared
with `vexbuild`), one device action at a time (download, info, the brain
port; a reservation released only by its holder), a 5-minute build timeout,
and a build refuses a project whose make files name `vexcom`, failing closed
wherever it cannot read them all (a glob too complex to check, an `include`
continued onto the next line read as make reads it, one that does not resolve
in the project refused rather than skipped) — a makefile is project content
`make` runs, and one that downloaded would make a build a download. **Download is only the owner's
click** (V-2, V-3): no tool, MCP tool, CLI or Discord verb reaches it, no "run
after download", and **any agent command line that names `vexcom` is DENY in
`rules.decide`** — so in v1's dispatch and v2's layer 1 alike, under an
allowlist entry or `--dangerously-skip-permissions`, never put to the owner —
in every spelling the rule lists: a path (the `/mnt/c` interop path, the Linux
binary, `C:\…`), a wrapper, a `sh -c`/`cmd /c`/`powershell -c` string, quote
and escape splits (bash, cmd's `^` and `%X%`, PowerShell's backtick), `$'…'`,
empty and default-value expansions, brace expansion, a glob argument that keeps
at least four of its letters, and a
PowerShell `-EncodedCommand` that decodes to it — **and any line that runs
something that could be it** (review round 2): a glob or an expansion in
program position, past wrappers, as the program a runner starts (options
parsed), in `sh -c` or `$(…)`, with no letter threshold; a program that cannot
be told and could be vexcom is asked whatever stands, never allowlist-covered,
always-ask in v2; a glob past the caps is too complex to judge, asked the same
way. A here-document's body is data, read as commands only where a shell is
fed it (review round 3: a `* item` in a commit message or a C doc comment
written through `cat > f <<'EOF'` had made the whole line DENY). An allowlist
entry for `uv` or `npx` still covers `uv run vexbuild` (the general stem-entry
rule), and `terminal_read` can read the brain terminal, the owner's echoed
typing included, while its "Jarvis can read" switch is on. Like every DENY it is an
accident guard, not a boundary — and the button is not one either: an agent
running as the owner can still reach a plugged-in brain through a script, a
shell fed through a pipe, cmd's or PowerShell's own wildcards, `make -f`, a
DLL beside the tool, or a forged-Origin POST to the route. Each device action therefore
publishes an id-only `vex_device {action, at}` record, so one nobody clicked
is visible. **Agents build through `vexbuild`** (`python -I -m jarvis.v2.vex
build [--rebuild|--clean]`, isolated so a package planted in the project folder
never runs, the same argv table and lock, no other subcommand), which asks like
any command unless the owner allowlists exactly it, as does running it or its
module through `python -m`, `uv run` or `npx`. A new project goes through the
same `project_folder` card Discord's `/project new` raises —
`projects.confirm_folder` is now the one confirmation handler both call — and
the template is checked whole (no absolute paths, `..`, drive letters,
symlinks or `.git`; at most 200 entries, 1 MB each, 10 MB in all, read with a
cap) before anyone is asked, then written by Jarvis's code with `O_EXCL |
O_NOFOLLOW`, then the settings file in the extension's v2 shape. **Device
reads** (2026-10-12): the owner's `vexcom --help` lists `--json` ("system
info in json format") and `--programs` ("shows programs") as its reads, so
device info runs exactly `vexcom --json` (pretty-printed as text) and a
second read, `vexcom --programs`, lists the programs on the brain — the same
device reservation and fingerprint check, and nothing else from that help.
Not yet, and said so by the routes: Python download (its flag is a spike
item). Spike items, the
owner's: the build against VS Code's own build log, and whether stopping the
Linux side of an interop process ends `make.exe` on Windows. Agents' task
worktrees live on the Linux filesystem, so `vexbuild` builds only in a chat's
own project folder; Codex workers may not reach interop from their sandbox.
Contract: `docs/hud-api.md`; free suite `tests/v2/vex_check.py`.

**The VEX view (2026-10-11, VX-2 of the same plan).** A pane view, `vex`,
that the view strip offers **only when the pane's project is a VEX project**
(its settings file parses — or is there and cannot be used, so the view can
say why). Switching a pane to it pins the pane to that project, so choosing
another chat never moves a build or a download to another robot; its header
has a project picker. It shows the project's card (name, language, slot,
SDK), Build / Rebuild / Clean with **✓ or the errors list**, Download (a slot
picker 1–8 defaulting to the project's, a name box defaulting to the
project's name and held to the daemon's rule), the brain terminal and device
info. The build's output, a download's progress and the brain's user port
are VX-1's **program terminals in the WP-D panel** (opened there for the
click that started them; no Restart on one that ended — it would open a
shell). **An error click opens the file at its line** through ED-2's
buffers: a drawn File pane on that project, else a new pane beside the VEX
view (a split, so the list stays in sight), else the VEX pane itself; an
error outside the project (an SDK header) is text only. **Download is only a
click on the labelled button** — "Download to slot 3: replaces whatever is in
slot 3 on the brain" — and the button counts only a trusted pointer click
(`lib/vex.ts` `isExplicitClick`: Enter or Space on it and a script's
`.click()` make clicks with `detail` 0, and a double click's second click
`detail` 2; only `detail` 1 counts), takes no focus from a press and is
out of the Tab order; there is no key and no palette entry. One click is one
chain (`state/vex.ts`): the brain terminal paused when it holds the port, a
build first when the daemon's 409 carries `code: "build_first"`, the
download, then the brain terminal again. **One chain per window**: while it
runs no other download starts, for any project; every VEX view shows it with
a Cancel that stops its build and sends nothing; the VEX pickers are off.
**Under an approval card every VEX control is disabled and inert, and the
store refuses; a card that comes up between the click and a step drops the
rest of the chain** — nothing is built or sent behind it, the brain terminal
is not reopened — and the view says so. The daemon counts only C/C++
sources, headers, assembly and make files as sources (a notes file never
asks for a build), refuses while a build is unfinished (a mark on disk only a
success clears), trusts a binary its last build wrote only while the
sources' digest is unchanged since that build started, and never lets a make
that found nothing to do vouch for one: an edited header the makefile does
not list makes the click's chain run one rebuild, then download (review
rounds M2 and MED-1). With no `vex.json` the view says the one command to
run, `jarvis vex setup`, and checks again every few seconds and on focus. Device
info, the programs list (**Read device info** and **List programs**, one
device read at a time, headed by which it was and a non-zero exit) and every
error message are drawn as text. A `vex_device` record this
window cannot match to its own click is said in the view. Not yet: a Python
project's download (VX-1's spike), a "dev servers" style list of past builds (only the last build per
project is kept, across a reload by id). Free checks: `lib/vex.test.ts`,
`tests/face/hud_v2_vex_check.py`.

**New VEX project (2026-10-12, VX-3 of the same plan; decisions V-4).** New
project's dialog offers "New VEX project from a template…", and the palette
offers **New VEX Project from a Template…** — the palette's one VEX entry,
which only opens the dialog. The dialog asks for a name (held to the daemon's
rule: letters, digits, `_` and `-`, not starting with `-`, at most 64 — VEX's
make refuses spaces), the language (C++ or Python), a template from the
extension's own VS Code templates (the daemon's list, labelled from the
extension's `index.json`), and the folder it goes in, defaulting to the one
`jarvis vex setup` recorded (V-4). **It shows the exact folder it will create,
and its Windows spelling**, as the daemon answers for exactly those inputs (a
check that asks nothing and makes nothing); a folder the daemon refuses is
said in its own words and Create stays off — **including a folder VEX's
Windows tools could never build in** (a `(`, an `&`, anything outside ASCII:
`My (1)`, `Документы`), which used to pass and be made, and then refuse every
build. **Create raises the
`project_folder` card** — the exact path, Approve/Deny, never Always — and the
dialog waits on it: **Deny makes nothing** and the dialog stays open saying
so; Approve makes the folder, unpacks the template (Jarvis's own code), writes
the settings file in the extension's v2 shape so the project also opens in VS
Code, and the new project appears in the sidebar and opens as a new thread. A
Python template gives `src/main.py` and the settings file. A template write
that fails after the yes takes back what it wrote — only what it made, judged
by identity, never anything renamed or swapped into its place — and says what
the folder holds now (empty again, or what is still there). While the card is up the dialog
reads its request one read at a time and rides out a failed read, so a blip
never turns an approved project into a failed one. An existing empty
folder is used without a card, and the dialog says so before Create. **Under
any approval card every control of the dialog is disabled and inert, and
Escape belongs to the card** (it denies; the dialog stays, its words kept).
With no `vex.json` it names `jarvis vex setup` and creates nothing. Free
checks: `lib/vexNew.test.ts`, `tests/face/hud_v2_vex_new_check.py`,
`tests/v2/vex_check.py`.

**Up to eight panes (2026-10-10, WP-G1 of
`docs/plans/2026-10-10-hud-split-panes-plan.md`; decisions beside it).** The
owner asked for 6- and 8-pane layouts ("I'm not seeing the 6 and 8 panel
layouts"). Ten shapes now: WP-A's six plus 3 over 2, 3×2, 4 over 3 and 4×2
(decisions S-1); ⊞ offers eight tiles, four to a row, 3×2 and 4×2 among them,
with "Layout · N panes" at the head and no tile checked while a 5- or 7-pane
shape is current (it then opens on the first tile, never on a toggle row). 5 and 7 are reached by split and close (WP-G2,
below; `LADDER` is the shape per count, three being three columns, S-3) or a
stored value. Every pane can show chat, task, file, diff, preview or a
terminal (panes 5–8 default to chat), and the chats, terminals and dirty
guards of WP-B and WP-D work in all eight.

- **Ids are not slots.** A pane keeps its id for life — `pane-<id>`, its React
  key (with a render-only incarnation WP-G2's close bumps), `chats[id]`,
  its dirty flag — and `ws.order` lists the ids drawn in reading order; `kept`
  holds the ones ⊞ hid. Everything the owner reads is the **slot**: "New
  thread · N", the panel's "in pane N", the refusal chip, aria labels and
  Ctrl+Alt+1 … 8. A stored WP-A/WP-B value has no `order` and reads as ids 1…n,
  so it draws exactly what it drew (a golden file of 300 fits made by the old
  code holds that). ⊞ to fewer panes hides the rest; more panes bring the
  hidden ones back first, so 8 → 1 → 8 returns every pane where it was.
- **The grid is derived from rows.** A shape is its panes per row (main2
  aside); the CSS grid lays each row on the union of all rows' column edges,
  so 3 over 2 and 4 over 3 have bottom-row edges of their own that drag
  independently, and each pane's width (for the compact view menu and the
  folded tree) is read from its own row.
- **Fitting is generic.** A column drop keeps, in every row, a window one
  narrower than the widest row — the focused pane's row the leftmost window
  that holds it — and a row drop keeps the focused pane's row, a four-wide one
  losing a column too. WP-A's drop tables hold unchanged, the focused pane is
  never dropped (a property over every shape, focus, order and window size),
  and a dropped set stays sticky only while these panes in this order could
  draw it. Eight panes want ~1512px at 100%: 1920×1080 draws all eight (the
  side panes squeezed to 480, columns at the 360 minimum), 1280×800 draws six
  with the badge and "Showing 6 of 8", and 150% on that screen draws four;
  zooming out is the lever on a laptop.
- **Free checks**: `workspace.test.ts`, `layout.test.ts`, `chats.test.ts`,
  `hud_v2_layout_check._eight_checks` (⊞ → 6 and → 8 at 1920×1080 with every
  pane at the minimum and no sideways scroll, every view in a 4×2 grid, the
  smaller windows, Ctrl+Alt+7 bringing a dropped pane back, a stored `[1, 5,
  2]` focusing id 5 on Ctrl+Alt+2, 3 over 2's bottom edge, and eight panes
  with Monaco, a frame and a terminal under a card at 160% and 70%) and
  `hud_v2_multichat_check._eight_chats_checks` (eight chats on eight threads:
  a turn in pane 7 draws only there, Stop in 8 leaves 7, voice lands in the
  selected pane 6).

**The editor's missing half (2026-10-10, ED-1 of
`docs/plans/2026-10-09-hud-editor-plan.md`; decisions E-1, E-4, E-10).** The
File tab had loaded only Monaco's bare API: no language was registered, so no
file had colour, Ctrl+F was Chrome's find, Ctrl+S Chrome's "Save page as",
and none of find/replace, folding, comment toggle, word suggestions or the
command list existed. Monaco is now 0.57.0 (pinned) on its supported entry
points, with a curated feature list and every Monarch definition (each
tokenizer fetched only when a file of its language opens) — a whole editor
**without intellisense**: no language server, and no TypeScript, JSON, CSS or
HTML worker built or loaded ("Claude Code is my intellisense"). The language
comes from Monaco's registry by the model's URI; `.h` is C++ for VEX, `.json`
and `.toml` get small definitions of ours, a shebang names its language.
Links open only http(s), with `noopener`. Until ED-3's Graphite scheme (below),
vs-dark's salmon strings, its red tokens and the orange and gold chrome of
find matches, bracket pairs and the unicode box are moved off amber and red.
Under an approval card every editor is read-only and unfocused, and the
editor host is its own stacking context so Monaco's overlays (context menu,
suggest list, quick input) stay under the card; the checks open each and
raise a card over it at 100% and 160%. Keys stay in the window's one capture
handler: Ctrl+B folds the sidebar from the editor too, except as the second
key of a Ctrl+K chord, which is Monaco's whatever it is (Ctrl+K Ctrl+0 folds
all, as in VS Code; a lone Ctrl+0 is still the HUD's zoom reset, and neither
reaches Chrome's zoom); the window's chord tracker follows Monaco's, counting
only keys typed in an editor and leaving the chord after 5 s or when the
window loses focus. Ctrl+S saves the editor it is pressed in — an action
scoped to that editor and disposed with it, never Monaco's global
`addCommand`, whose last-created editor owned Ctrl+S everywhere and outlived
itself (the review of PR #43 found Ctrl+S in a diff writing a file blank) —
and never opens Chrome's dialog; a save's answer lands only on the buffer it
was sent from. Opening another file, or Reload, over an unsaved edit asks
first. Each File pane picks its own project (decision E-13): a `Project ▾` in
its header lists every project and Follow chat, re-pins that pane only,
refuses while the pane holds an unsaved edit — known from the keystroke's own
change event, never a frame later — and is disabled under a card.
Monaco's right-click menu is placed in viewport pixels, which the HUD's CSS
zoom scaled a second time (off the window at 160%), and flipped by its
unzoomed size; it is laid out again after Monaco's own layout, in drawn
pixels, and opens at the pointer at any zoom and edge. The diff view
disposes its models (it leaked two per file viewed).
Measured: the editor chunk went from 2.30 MB (bare API) to 3.63 MB, the
worker from 232 KB to 277 KB, the first-paint chunk +5 KB. EditContext stays
on: headless Chromium types into it. One defect is left on purpose: a space
typed in Monaco still starts push-to-talk and is lost (confirmed headless);
the owner moved push-to-talk to Ctrl+D (2026-10-10), which its own PR
delivers. The `monaco-vim` spike is ED-8's (E-11), not this package's.

**The HUD's CSP and installed build (2026-10-11, ED-4 of the same plan;
decision C-1; contract in `docs/hud-api.md`).** Every HUD and API response
carries one full Content-Security-Policy (`hud_api.HUD_POLICY` with WP-E's
`frame-ancestors 'none'`, in the one header): only bundled script, no inline
script, no `eval`, no `blob:` worker, same-origin `connect-src` (the terminal
socket included), loopback `frame-src`, and — since ED-5's tree-sitter, below
— `'wasm-unsafe-eval'`: WebAssembly compiles, a string never becomes
JavaScript. A response may only narrow it
(the avatar). The window that gates approvals is no longer served from the
repo's agent-writable `hud/dist`: `jarvis hud install` — the owner's or the
lead's, from their own shell; Jarvis is refused it in every spelling —
builds `hud/` from a **clean** checkout (uncommitted `hud/` changes are
refused unless `--allow-dirty`) and installs a read-only copy with a SHA-256
manifest recording the commit under `~/.local/share/jarvis/hud/<build-id>/`.
The daemon serves the newest **intact** build, checking every file's hash on
every read, falls back past a newer build whose page fails, says on `GET /`
whether nothing is installed or nothing passes, reports what it serves on
`/status` (`hud_build`, `hud_git_head`, `hud_state`), and warns at start-up
when that build's commit is not the checkout's HEAD — so a forgotten install
shows up in the log rather than as a silently old window. An install needs a
window reload, not a restart (an open window's lazy chunks are still served
from the older build). Every headless HUD suite runs under the policy with a
violation watch; `tests/face/hud_v2_csp_check.py` proves the HUD's needs work
and an attacker's are refused.

**Restart and rebuild from the CLI (2026-10-11).** `jarvis restart` (alias
`jarvis daemon2 restart`) restarts systemd's `jarvis-daemon2` — refused while a
turn runs or a broker approval waits (`/status` → `approvals_pending`) unless
`--force`, and refused the same way when systemd says the daemon is up but it
does not answer `/status` twice ("can't check" is never "nothing running");
then it waits for a new daemon to answer `/status` and reports it. Typed in a
HUD terminal, which lives in the daemon's own cgroup, it checks there and hands
the restart itself to a transient unit (`systemd-run --user`), so the restart
does not kill the command doing it. `jarvis hud rebuild [--pull] [--restart]`
refuses an uncommitted `hud/` (an untracked or ignored `hud/.npmrc` included)
**before** `npm ci` runs package.json's scripts, runs `npm ci` when the lock
file moved, installs through `jarvis hud install`'s own path and checks
`/status` serves the new build. Both warn of uncommitted Python in the
daemon's checkout. Both are the owner's and DENY for every agent
(`jarvis/v2/owner_ops.py`), as is changing the daemon's unit through
`systemctl` or D-Bus, and — the owner's choice the same day — killing it by a
`pkill -f`/`killall -r`/`pgrep -f`-fed `kill` pattern that matches its command
line at `daemon2` (`pkill -f jarvis` stays ALLOW; a literal `kill <pid>` and
kills by process name are not seen) or, since the re-review, by a `kill` fed
its PID from its unit, cgroup, ports or `/proc`. A kill pattern whose
matching cost cannot be bounded is never matched, since `re` holds the GIL:
it is too complex to judge, so it is asked, never refused; `/status`
reports the daemon's `pid`, and `jarvis restart` trusts a `/status` only from
systemd's MainPID.

**Push-to-talk is Ctrl+D (2026-10-10).** The owner: "can we change the push
to talk button? change it to Ctrl + D just like it is in claude." It was
Space, which is a character in every box the owner types in: each typing
surface had to keep Space from bubbling to a document handler, and Monaco
did not, so a space typed in the editor started the mic and never appeared.
Now Ctrl+D is held to talk and let go — D or Ctrl, in either order — to
send (`lib/talkkey.ts`, App.tsx). Ctrl and D only: Shift, Alt or Meta make
it another key, AltGr (Ctrl+Alt) never matches, auto-repeat never presses
twice, and nothing fires mid-composition; a layout whose D key types no
Latin letter (Cyrillic, Greek) is matched on the physical key, as Chrome's
own Ctrl+D is. The window takes it in the capture phase, so it works from the
chat box, Monaco, a menu, a chip, the sidebar or a pane header, and nothing
under the cursor acts on it: its default (Chrome's bookmark dialog) is always
prevented and it stops at the window — under a card, muted or after a crash
too. What it then does is what Space did: it goes to the selected chat
(W-6), interrupts that chat's turn first, records nothing while the mic is
muted, refuses under a card, with a picker open, with no chat on screen and
after a crash. A window that loses focus mid-hold lets go (its keyup never
comes). A hold is decided where it began: once Ctrl+D began as push-to-talk,
or as a press refused under a card, its auto-repeat stays the window's when
focus lands in a terminal mid-hold (a card answered elsewhere hands focus
back, or the owner clicks into one), so the shell never gets a run of
end-of-input from it — and a window blur mid-hold (a Preview frame taking
focus) releases the mic but not the hold, so the repeats after it are still
the window's, until D or Ctrl comes up or a fresh press, which always decides
afresh (`TalkHold`). **In a focused terminal Ctrl+D is the shell's** end of input, left to
xterm untouched (the owner's answer), the way Ctrl+B, Ctrl+Alt+B and Ctrl+_
are; from a terminal push-to-talk is the orb, whose tooltip names the key.
Under a card the window takes it there too, so it is never an EOF behind the
card. **In Monaco Ctrl+D is push-to-talk**, and Monaco's own Ctrl+D, add
selection to next find match, moves to **Ctrl+Shift+D** (`editorKeyRules`,
applied in ED-1's `loadMonaco` after its extras; the old binding is removed,
not shadowed, so Monaco's own record says where it went). Nothing in Monaco
or the HUD binds Ctrl+Shift+D (D is otherwise only a Mac Ctrl binding in
Monaco), and outside the editor it is Chrome's "bookmark all tabs", which the
editor's binding prevents. **A pending Ctrl+K chord's Ctrl+D is Monaco's**
(decisions E-10, as for Ctrl+B): the window's one chord tracker
(`lib/editorkeys.ts`, now shared by the layout keys and push-to-talk, a
keydown stepped once whichever asks first) says so, and push-to-talk then
keeps the key from Chrome and leaves it to Monaco — Ctrl+K Ctrl+D, move last
selection to next match, keeps its key. Taking it would leave Monaco's chord
waiting to swallow the next key typed. (Before ED-1 loaded Monaco's chords,
this PR loaded multi-cursor and find itself and unbound the chord; ED-1's
bundle replaced both.) **Space is no longer push-to-talk anywhere**:
it types a space, presses a focused button, and does nothing on a separator.
So a button whose second press matters never takes focus from a pointer (its
click still works): the approval card's AUTHORIZE and ALWAYS (an aborted
click — pressed, slid off — used to leave focus there, and the next Space or
Enter authorized), the terminal's "Jarvis can read" switch, the folded mic's
mode cycle and the Preview's "Keep its origin". The first Space pressed
outside a typing target says once, under the orb, that push-to-talk is
Ctrl+D now; a card that arrives meanwhile takes the line at once. A preview frame is another document, so Ctrl+D inside one is the
framed page's. Free checks: `talkkey.test.ts`, `monaco.test.ts` and
`tests/face/hud_v2_ptt_check.py` (run by `hud_v2_check` after the terminal
and preview sections, on a `MockDaemon(0)` of its own, or alone), plus the
ported checks in the terminal (`\x04` to the PTY, nothing under a card, no
EOF from a hold that moved into a terminal, the readable switch), multichat
(W-6), layout (a separator) and preview (Keep its origin) suites.

**The editor's colours (2026-10-11, ED-3 of the editor plan, §2.5;
decisions E-4).** One Graphite scheme, in the spirit of Neovim 0.10's
default: most code is the HUD's own text colour, told apart by weight and
slant — keywords bold, comments italic and muted, punctuation dimmer — and
colour is kept for four kinds of thing, in four new tokens beside the HUD's on
`:root`: functions, tags and headings in glacier (`--syn-fn`), strings in
sage (`--syn-string`), numbers, constants and escapes in lavender
(`--syn-const`), types, modules, directives and keys in steel
(`--syn-type`). Each is a quieter sibling of a HUD hue rather than the hue,
so the accent still means only focus, selection, links and "running", and
green only "done". No syntax colour is amber or red, and in the chrome red
appears only for an error squiggle (nothing raises one) and a diff's
low-alpha "removed"; every colour Monaco would otherwise draw orange, gold
or amber — find matches, the first bracket pair, the unicode box, warnings,
and the rest of its registry — is pinned off it. Every pair passes WCAG AA on
the pane and on the current line. The scheme is one table keyed by
nvim-treesitter's capture names, ready for ED-5's tree-sitter colour, and
the same rows style Monaco's own Monarch tokens, which stay as the fallback;
Monarch's C, C++ and Python are refined so a call, a builtin, a type and a
constant show there too. The colours are written once, in theme.css: the
editor's theme is built from the tokens when Monaco loads, and the tests
recompute every contrast and hue from that file and read the rendered
tokens back against it.

**Buffers, tabs and the guarded save (2026-10-11, ED-2 of the editor plan,
§2.3–§2.4; decisions E-7, E-8, E-13; the owner's `.env*` rule,
`docs/plans/2026-10-11-env-files.md`).** A file's text lives in a buffer
registry outside React (`hud/src/state/buffers.ts`): one Monaco model per
file on disk, keyed by the absolute path the read route now returns, so one
file reached from two projects (the Inbox is rooted at `~`) is one buffer, and
the same file in two panes shares the text, the dirty dot and the undo stack
while each pane keeps its own cursor and scroll. A view that unmounts never
takes an edit with it, and the text is read once, at save — never per
keystroke (#43's L3). Dirty is "not the version last read or saved" (the
model's alternative version id), so undoing back to the saved text reads
clean. Each File pane has tabs (`PaneSpec.editors {paths, active, preview}`,
stored with per-field fallback, re-read from disk after a reload; unsaved
text is never stored, and `beforeunload` asks while anything is dirty): a
single click opens an italic preview tab the next single click replaces, a
double click or an edit pins it, middle-click or × closes, and the last tab
on an unsaved edit asks Save / Don't save / Cancel. One editor per pane, its
model swapped. **Saving is guarded by content, not by mtime**: a read returns
an `etag` (the first 128 bits of SHA-256), a save sends `expected_etag`
(`null`: must not exist), and the daemon hashes the disk again under its lock
and answers 409 with the current etag — an mtime missed a same-size rewrite
on the 9p `/mnt/c` mount (or one `utime`d back) and called a bare `touch` a
conflict. **The HUD polls** (`POST …/files/stat`, every 2 s while visible and
on focus, hashing again only when the stat moved, because inotify cannot see
a write made from Windows): a clean buffer whose file moved reloads in place
as one undo step, with a toast naming the writer when the activity feed saw a
running turn's tool call name the file; an edited one gets an amber conflict
bar — Compare (the disk read-only on the left, the buffer on the right, side
by side even in a narrow pane), Keep mine (saved against the disk etag the
bar was shown, so a write meanwhile brings the bar back), Take theirs (asks
once; Ctrl+Z brings the edit back); a gone file offers Save to recreate or
Close. There is no force-save anywhere. **One classifier,
`jarvis/v2/editor_policy.py`**, decides what the editor may do: withheld (a
credential bundle, anything in a credential folder, Claude's and Codex's
login files, git's credential store, `.netrc` — no content, ever; a withheld
folder is never listed, except the gate's own files inside
`~/.config/jarvis`), read-only (the gate's state, the safety layer and the
served HUD build, bytes that are not UTF-8 — which a save never overwrites),
the owner's env files, and text. **Every editor file route is owner-only,
reads included** (E-7: the HUD's listener and its own page). **The owner
edits `.env*` files here, and Jarvis never reads them**: `secrets.is_env_credential`
is the one name rule (`.env` or `.env.<anything>` at any depth, not ending
`.example`, case-insensitive, symlinks resolved both ways); such a buffer
carries a lock — "Jarvis can't read this file" — Add to chat is disabled for
it, its model has a language of its own so its words are never suggested in
another file, and its text goes nowhere but its own save: not the bus, a log,
localStorage, an `@path` attachment, a diff or the preview origin (which
also refuse withheld files now — the preview origin had served a key out of
the Inbox's `.ssh`). The Jarvis-side refusals of env files (v1 tools, the
shell, the scrub, Claude's hook, Codex's grants, terminal_read) are ENV-1's,
on this rule, built 2026-10-12 (CLAUDE.md's ENV-1 paragraph).
"Add to chat" is here in its smallest form (the selection or the line, as
`path:Lx-y` and a fence, into the selected chat's box, unsent) so the env
rule has something to disable; ED-7 extends it.

**ED-2's review round (2026-10-11, PR #49).** An env file is never demoted
to `binary`: one that is not UTF-8 (UTF-16 from PowerShell 5.1 on `/mnt/c`,
Latin-1) stays `env`, read-only, and every read now says `editable`, which
the HUD follows rather than the kind. The gate's readable exemption holds
only for a gate path that resolves to itself and is no credential by another
rule, and only in the editor's own routes — an `@path`, a diff and the preview
origin have none (a `models.json` symlinked to a key had made the key
readable through all three). Withheld now also covers docker's, kube's,
npm's and PyPI's credential stores and v2's session carriers, and **the
preview origin refuses any dotfile path but `.well-known` and anything in
Jarvis's own folders** — it answers every loopback client. In the window: a
pane's × lets go of that pane's buffers only; a project going away keeps
every pane on it that holds an edit no pane outside them holds (two panes
sharing one edit used to let go together and orphan it); past 40 tabs only a
clean one closes; Compare re-reads once per new conflict, never in a loop;
one file reached through two projects at once is one model; a kind the window
does not know is read-only and closed to Jarvis; the crash prompt can save
every unsaved edit; a reload in place never reads as an edit; and the "changed
on disk by …" toast names only a write tool, by absolute path.

**The editor's navigation (2026-10-11, ED-7 of the editor plan, §2.8 and
§2.10).** What made the HUD's editor a replacement for VS Code at the robotics
lab: **quick open** (Ctrl+P: a fuzzy list of the project's files —
`git ls-files` in a repository, gitignored output left out, dotfiles in;
`file:line` jumps), **find in files** (Ctrl+Shift+F: the File pane's tree
column turns to a search with case, word, regex and a `.gitignore` toggle and
a file filter; a hit opens at its line; at most 2,000 matches and 10 s; never
on the Inbox), **the command palette** (Ctrl+Shift+P or F1: the HUD's commands
plus Monaco's own actions when an editor had focus; never a download or a VEX
device), **the explorer's** new file, new folder, rename and delete (a
right-click menu; a delete moves to the trash after asking), **the status
bar** (Ln/Col and selection, language, indentation, encoding, line endings,
saved/unsaved/saving), and **Add selection to chat** extended to every
selection and to the palette and Monaco's context menu. Three owner-only
routes carry it (`docs/hud-api.md`); find in files runs on `grep_files`'
own core, so the two refuse exactly the same credential files, and the
explorer refuses through `editor_policy`, `scope` and the trash — never a
copy of a rule. A rename moves the open buffer with it (its unsaved edit and
undo stack included) and every pane's tab follows; a delete leaves an
unsaved edit in its tab, where Save puts the file back. Under an approval
card quick open, the palette and the explorer's menu close, their keys do
nothing, and the search is inert with the rest of the window.

**Split and close (2026-10-11, WP-G2 of
`docs/plans/2026-10-10-hud-split-panes-plan.md`; decisions S-2, S-6–S-8).**
The owner asked for a × on every pane, more than once, and a VS Code split
button. Every pane's strip now carries both: the split button
(the title bar's icon button, an outline with one divider) in every layout,
the × whenever more than one pane is drawn. A split adds a **copy of the
pane right after it** and focuses it, on the ladder's shape for the new
count (2 columns, 3 columns, 2×2, 3 over 2, 3×2, 4 over 3, 4×2); a × removes
exactly that pane and the rest reflow onto the ladder's shape for one fewer.
⊞ still hides and restores; × discards — the pane's spec goes back to its
default, and an id a later split reuses mounts fresh.

- **What a copy shows**: a chat, a new thread in the same project (a thread
  is open in one pane only); a file, its tabs on the same buffers (since
  ED-2, below: one text, dirty dot and undo stack for both, so a pane with
  an unsaved edit splits too — S-8's "save first" was for two copies that
  could diverge); a diff, the same task and file; a task, that task's
  diff (S-7); a preview, the same URL, judged again, never the keep-origin
  grant; a terminal, a new shell in the folder the source was opened in
  (S-6), unreadable to Jarvis if the source is — switched off before it is
  ever shown (`POST /terminals` takes `readable`). Split is refused at
  eight, when the next shape would not be drawn whole in this window (at
  1280×800, six cannot become seven), and when the only ids left are hidden
  panes holding unsaved edits. A split takes a free id first, then an empty
  hidden pane, then one whose compose row holds unsent words, which wait for
  the next new thread; a stored pre-G1 workspace's panes 2–4 count as
  hidden, so a split never writes over them.
- **What × keeps**: a thread's turn keeps running, its sidebar dot stays,
  and the pane that next opens that thread has its Stop; words in its box and
  words a Stop handed back wait for the thread; a composing chat's unsent
  words and a File pane's unsaved edit — one no other pane holds — are asked
  about first, inline, never in a modal (for an edit: Save / Don't save /
  Cancel, as closing the last tab on one asks; the question goes when it no
  longer holds); a terminal goes back to the panel, alive, attached, as the
  selected tab. A compose pane closed while its first message opens the
  thread leaves that turn for the pane that opens the thread next, Stop and
  all. Focus goes where the mic is: the selected chat while it is still
  drawn, else the chat selected before it, else the pane now in that slot —
  never the page, and never a chat that merely slid into the closed slot.
  One click closes one pane, even when a double-click's second click lands
  on the × that moved under the pointer.
- **Nothing assumes pane 1**: after it is closed, the window's chat falls
  back to the pane in slot 1, the header ids and Ctrl+Alt+1 follow the slot,
  and a reload keeps the order.
- **Under a card** every split, × and close question is disabled and inert,
  the card keeps focus and stays on top at 160% and 70% over eight panes.
- **Free checks**: `workspace.test.ts`, `chats.test.ts`, `store.test.ts`,
  and sections of the layout, multichat, terminal and preview suites; each
  was shown to fail against its mutation (the PR's table). WP-G3's shared
  buffer came with ED-2's buffers (its `filesync` went with it); not built:
  WP-H's thinking line.

**Syntax colour the Neovim way (2026-10-11, ED-5 of the editor plan, §2.6).**
C/C++ (VEX's `.h` and `.c` included), Python, JavaScript, TypeScript and TSX,
JSON, Bash, CSS, HTML, YAML, TOML and Markdown are coloured from a real parse
tree: tree-sitter (`web-tree-sitter` 0.27.0 and each language's MIT grammar,
pinned exactly) in a syntax worker of its own, with nvim-treesitter's own
highlight and injection queries, vendored at a pinned commit and translated
once by a dev-run script. Monaco asks for the visible lines, the worker
answers with semantic tokens styled by ED-3's table, and Monarch stays
underneath, so colour is never missing — only quieter. Edits reparse
incrementally (tested against a fresh parse over 300 random edit sequences);
a Markdown fence is coloured as its language, Markdown paragraphs by the
inline grammar, HTML's `<script>` and `<style>` as JavaScript and CSS. Past a
budget a file stays on Monarch and its editor says so in a corner: over 1 MB
("syntax: basic (large file)"), a first parse over 1 s, or reparses cancelled
twice running. The grammars are code the worker runs, so the HUD's policy
gained `'wasm-unsafe-eval'` (WebAssembly only) and the worker's loader fetches
only the runtime and the grammars built into its map, from the HUD's own
`/assets/`, refusing any other URL before a request is made. Nothing loads
until the first file of a covered language opens: first paint grows by 168 bytes,
the worker is 213 KB, and the grammars (about 10 MB in all) load one by one
as their languages first appear. Folding and sticky scroll from the tree
followed in ED-6 (below).

**Folding and sticky scroll from the tree (2026-10-12, ED-6 of the editor
plan, §2.6).** In the same languages, folds follow the code rather than its
indentation: nvim-treesitter's own `folds.scm` (vendored at ED-5's pin, each
file's size and SHA-256 in a manifest) is run by the syntax worker over the
tree it already holds, so a C++ file folds at its includes, classes,
functions (at the signature, even with the brace on a line of its own),
`if`s, `for`s, lambdas and block comments, a Python file at its defs,
classes, docstrings and multi-line brackets, and a Markdown file at each
heading's whole section and each fence. As in VS Code, a block's own closing
`}` (or `]`, `)`, end tag) stays visible below a folded block, so `class A {`
folds to `class A { ⋯` over its `};`. **Sticky scroll reads the same
ranges**: deep in a function, the line held at the top is the function's
signature. Wherever tree-sitter is not serving a file — past 1 MB, on a slow
parse, with the worker gone, in a language it does not cover, and always for
an env file, which never reaches the worker — Monaco folds by indentation, as
before; and the fold query has the colour queries' 250 ms budget (twice over
it, that file folds by indentation from then on).

**Chat titles (2026-10-12).** The owner: "right now each chat just displays
its id. we should keep the id in the three dots but have the main display be
the title and have the title wrap after a certain length." A chat names
itself from the owner's first message (`jarvis/v2/titles.py`): Codex
`gpt-6-luna` at effort low when Codex is healthy and the account lists it,
in a tool-less, read-only, one-turn app-server that never waits on the login
lock (a busy lock is tried again over ~90 s, then OpenRouter); else the
default OpenRouter model. The Codex turn has no environment
(`environments: []`), which no live run has verified: if a ChatGPT-auth turn
ignores it, a read-only command can run before the turn is refused; its
output is discarded, and the title comes from OpenRouter. Chats from before are titled by a backfill at
start-up. A title is written only onto an empty one, so **a rename always
wins**, before or during generation, and it follows to the chat's Discord
thread like a rename — except for a chat archived in the HUD, whose Discord
thread keeps its name (and stays archived) until the chat or its project is
restored. The Archive view names archived chats by title as well. In the sidebar the title **wraps**, clamped at three
lines with the whole title as its tooltip; an untitled chat reads "New
thread" (or "Untitled" for an older one the backfill has not reached), muted
and italic, never its id — the id is "Copy id" in the row's ⋯ menu. A split's
chat header names it the same way. No agent can set or regenerate a title.

**The git gutter and the explorer's git colours (2026-10-12, ED-9 of the
editor plan, §2.5 and §2.8).** Beside the line numbers each open file shows
what changed since HEAD, as VS Code does: a bar in sage (`--syn-string`) on
added lines, in glacier (`--syn-fn`) on modified ones, and a small red wedge
at 60% where lines were removed — the diff's "removed" colour, the only red,
never amber. It is diffed against the **buffer**, not the disk, so it follows
an unsaved edit as it is typed (after a short pause) and is clean again once
the edit is undone; a file HEAD does not hold, a binary one or one past
100,000 lines has none, and an env file never has one. The File pane's tree
colours names the same way — modified glacier, added and untracked sage, a
merge conflict lavender, ignored dimmed — and a folder takes the strongest
state inside it. HEAD's text and the status come from two owner-only routes
(`docs/hud-api.md`) that refuse what the editor withholds, every env file and
the gate's state, by path and by the name git has for a renamed file (which
must also lie in the project), and that refuse a repository whose own config
names a program; the status never goes into a submodule. The status is polled
every 3 s per project while something shows it (the daemon caches it 2 s); a
poll that sees nothing new redraws nothing, and typing never redraws the
tree. A file in another repository than the project's — a nested robot repo,
a file reached through the Inbox — checks its own HEAD every 10 s while the
window is visible, so a commit there clears its marks.

**The Browser's engine (2026-10-12, BR-1a of
`docs/plans/2026-10-10-hud-browser-plan.md`; amendment 1 and the decisions
file win).** The daemon owns a Google Chrome (stable, from apt) for the
HUD's coming Browser pane, headless and streamed later (BR-1b); this package
is its engine and its fences, no HUD yet. Chrome starts at the first
`POST /browser/tabs`, one instance per profile — **Main** at first, plus one
**Private** instance whose panes are each an off-the-record context — and
stops with the daemon (anything left is killed by exact PID). Every
connection it makes, loopback included, goes through a loopback **SOCKS5
gate** in the daemon that resolves a name once and connects to the address
it judged: Jarvis's own ports (the control ports, the HUD and API ports,
8403 unless it is this daemon's workshop) are refused on every address in
every spelling, a public name that resolves to loopback is refused, the LAN
only with the owner's switch (`browser.json`, off), and **this machine like
the LAN**: a loopback port only once the owner opened it in a tab (the
lead's decision on PR #69's review; a page or an agent never opens one), and
then only by a literal or `localhost`. A name keeps the kind of address it
was first allowed on. Chrome sends no UDP at all — no QUIC, WebRTC held to
proxied transports and its mDNS responder off — checked by its effect, a
packet sniffer in a private network namespace seeing nothing leave but on
loopback. Typed URLs pass a **judge** first (http,
https or about:blank; no user name, no `file:` and friends, which never
touch the network), and wherever a page lands is judged again and replaced
with an explanation when refused. Its folder (`~/.local/share/jarvis/
browser`) is a 0700 credential folder holding the profiles, Chrome's HOME and
the Private run folder; the launch keeps Chrome's sandbox, Safe Browsing,
component updates and sign-in and sync (BD-5), and drops automation, the
DevTools port, extensions and permission prompts; Chrome's environment is
built from nothing. Every `/browser` route is owner-only on the HUD
listener, and no URL, title or profile label reaches `/status`, the bus or
the log. `/status` gains `browser` and `refused_ports`. The same review
closed v1's file tools (`read_file`, `grep_files` and the write tools) to
every credential folder, the Browser's profile included.

**The Browser's stream and the owner's input (2026-10-12, BR-1b of the
browser plan, §2 "The stream", §3, §8; amendment 1 §B.8–§B.9).** Backend
only; BR-3b draws it. A HUD window sees one tab over a socket of its own —
the terminal's pattern: a single-use 30 s ticket for that tab from an
owner-only route, and a tab another window shows is asked for first — as a
JPEG screencast, **one frame in flight and the newest next**, so a slow
window never falls behind; a tab no window shows is not streamed. The
owner's keys, pointer, wheel and pasted text go back in BR-2's shapes and
become real CDP input events; every message is held to its shape and bounds
by the daemon, whatever the window sent. **While any approval card is up,
nothing reaches a page**: input is dropped (the window hears `held`), the
owner's own navigation routes answer 409, and the moment a card is raised
everything queued is dropped, so nothing sent before or during it is ever
delivered after. A press a card cut off ends with no click anywhere: Chrome
would click the page's root for a release outside it (measured), so the
daemon ends such a press as a pointer move with no button down. The page's
dialogs (alert, confirm, prompt, leave-page) go to the window and are
answered there, dismissed after 60 s or at once when no window shows the
tab. A pop-up opens as a tab and its opener's window is told, to place it in
the same pane; past the cap of 12 tabs in all the pop-up is refused rather
than an older tab closed. Downloads are saved owner-only (0600, a folder of
their own, a cleaned name) in the profile's private folder and offered to
the window as an attachment that can never run on the HUD's origin; an
executable is held until the owner's card (BR-9b). A page's file chooser is
answered only through an owner-only route with files the owner picked in a
project, judged by BR-9a's upload rule. The owner's input pauses a driving
agent before it is delivered and is timestamped for BR-9b's gate; the
banner's channel (`driver`, `action`, `paused`, `list`) is there for BR-9c.
On the bus, only `browser_attached` (a tab id and a time); no URL, title,
typed text or frame reaches the bus or the log.

**Editor settings, `.editorconfig` and vim (2026-10-12, ED-8 of the editor
plan, §2.8; decisions E-9, E-11).** A File pane's ⚙, or "Editor Settings…" in
the palette, opens one dialog for every editor in the window: font family
(names only — nothing that could end a CSS value), size, tab size, spaces or
tabs, word wrap (Markdown wraps by its own setting, on), minimap (off), sticky
scroll, whitespace, bracket-pair colours, cursor style and vim keys (off).
A change applies to every open editor as it is made and is kept in the
window's storage, each setting checked alone, so a bad stored value costs
only itself. Auto-save and format on save stay off (E-9). A project's
`.editorconfig` — read through the editor's own owner-only routes, from the
file's folder up to the project's root and never above, stopping at `root =
true` — sets a matching file's indentation and the line ending a file with
no line break takes for its new lines; it never touches a file's existing
line endings and **never rewrites anything on save**, whatever its
`trim_trailing_whitespace` or `insert_final_newline` say. An `.editorconfig`
is project content an agent can write, so judging one **never holds the
window**: one file's judgement is one task, stopped by a 5 ms clock, and a
config that runs out of time twice, or is past a limit no real config
reaches, configures nothing until its content changes. Vim (E-11) is
`monaco-vim` 0.4.4, loaded only when switched on, with its mode and `:`
command line under the editor; `:w` saves that editor's buffer through ED-2's
guarded save. **Push-to-talk keeps Ctrl+D in the editor in vim mode too —
the owner's decision (2026-10-12)**; vim loses its half-page-down. Under an
approval card the dialog closes and the ⚙ is disabled with the rest of the
window.

**File reads as the editor, Preview is the Browser (2026-10-12, BR-3a of
`docs/plans/2026-10-10-hud-browser-plan.md` §7, cut by its amendment 1 to the
rename and the migration).** The owner: "also rename "File" to "Editor" and
"Preview" to "Browser"". The strip says **Editor** and **Browser**. The
Browser changes in name and id both — view `"browser"`, `browserUrl`,
`lib/browser.ts`, `BrowserTab.tsx`, `browser-*` test ids — and the terminal
link menu says **Open in Browser** (`term-link-pane`) and **Open in a Chrome
tab** (`term-link-tab`); which links get the menu is unchanged until Remote
mode (B-11). A layout stored before the rename migrates as it is read, under
the same key: a Preview pane opens as the Browser with its URL, and its
keep-origin grant only while that grant is still exactly the URL's origin;
the next save writes only the new names. The editor changes in name only:
its id stays `"file"`, so stored layouts and the editor suites need nothing,
and one label table (`VIEW_LABEL`) names every view for the strip, its
compact menu and the pane's `aria-label`; the palette's "Show Files" is
"Show Editor". The workshop listener (8403) keeps its Python names
(`preview_route`, `preview_only`) and is "the workshop origin" in the docs.
Free checks: `workspace.test.ts` (the migration, the grant, the label table,
a stored `"file"` pane) and `tests/face/hud_v2_browser_check.py` (was
`hud_v2_preview_check.py`; its `_rename_checks` seed a pre-rename layout).

**The Diff pane shows the project's uncommitted changes (2026-10-12, the
owner: "the Diff doesn't work right now it just says to select a Task", and
yes to VS Code's Source Control view).** With no task to show, a Diff pane
lists what changed in its project since HEAD — modified, added, untracked,
renamed, deleted and conflicted files, and untracked folders, each with its
letter (M, A, U, R, D, C) in the explorer's colours and the editor's file
icon; ignored files never — headed "Uncommitted changes in <project>". A
click shows HEAD's text on the left and, on the right, the file as the
editor shows it: **the open buffer itself** when the file is open in a File
pane, so an unsaved edit is there and the diff follows typing, else the file
as read; a deleted file's right side is empty, and a binary, too-large or
non-regular file says so. **An env file is listed and never diffed** — the
pane says so and offers Open, and nothing about it is asked of the daemon.
Open puts the file in the editor where every other open does (a drawn File
pane on the project, else a split beside — never over this view or a VEX
view). It is **read-only**: no staging, reverting or committing here. Which
it shows: **never another task than a drawn Task pane** — while one is drawn
and shows a task, the Diff shows that task's diff, even after a thread is
picked, and its picker is off; else a task picked more recently than a
thread shows the task's diff, exactly as before (so a task click always
shows in a drawn Diff pane); else the project the pane is pinned to (its
header's picker, as the File pane's; a File pane that opened a file keeps its
pin when it becomes a Diff pane); else the chat's project. The caption says
which and why ("the Task pane's task", "the task picked last", "this pane's
project", "the chat's project"). Picking a project in that picker takes the
window off the task, as picking a thread does. Under a card its picker, Open
and ↻ are disabled. The list
is ED-9's status poll (only while the pane is drawn), and typing anywhere
else in the window never redraws it; HEAD's text is ED-9's
base route, which now also answers for a file deleted from the working tree.
A file opened and edited while its diff is still being computed swaps to the
buffer quietly (the computation is cancelled first), and a buffer that goes
away while shown reads "…" until the file is read again, never as an empty
side.

**The Diff pane's follow-ups (2026-10-12).** The change list draws only the
rows in view, so the status's 20,000-file cap costs typing elsewhere in the
window nothing (about 2 ms a character beside it, where every row drawn cost
~100 ms). It takes keys: ↑ ↓, Page Up/Down, Home and End move the selection
and scroll it into view, Enter reads it again; a selection scrolled out of
view stays selected, its diff still shown, and the keys move on from it; a
reload scrolls the pane's file into view. A held key moves the highlight at
once and reads only the row it stops on (after 150 ms of no keys), and a
read no longer wanted is dropped, so on a slow repository the last row's
diff is not stuck behind every row passed. A screen reader hears each row's
place in the whole list and the selected row as the keys move. **The Task pane's header clears its
task** ("task <id> · clear", off under a card): the 2×2 grid draws a Task
pane, and while it shows a task every Diff pane shows that task's diff, so
clearing is the way back to a project's uncommitted changes — a Diff pane
still never shows another task than the Task pane. The task diff and Compare
cancel a diff still being computed when they close, as the uncommitted
changes' diff already did, and never touch the buffer Compare shows; and the
uncommitted changes' diff is drawn with the owner's editor settings (font,
size, whitespace, bracket colours, sticky scroll), live, as the task diff and
Compare are.

**The Browser's action rules and reviewer (2026-10-12, BR-9a; amendment 1 to
the Browser plan, §B.3–§B.5).** Before an agent's click, keystroke or
navigation in a Browser pane runs, fixed rules refuse what no model may allow
(typing into a password, one-time-code or card field, a credential in what it
types or where it goes, an upload from outside the chat's project, acting
while a card is up or the owner holds the pane) and ask about logins,
payments, uploads and executables; a reviewer model judges the rest, and
every failure of it asks. Not wired yet: nothing drives a pane until BR-9b.

**Finding the dev servers an agent started (2026-10-12, AO-1a; amendment 2
to the Browser plan, §B).** A pure module, not wired yet: nothing opens until
AO-1b and AO-3. It finds new loopback listeners in `/proc/net` and refuses
Jarvis's ports, service ports and the ephemeral range first. It attributes a
listener to a chat or task only by the origin tag in its owning process's
environment, read as raw bytes and never decoded or logged. It also judges
the probe's answer and picks the one window that acts.

**A thinking line at the foot of each chat (2026-10-10, WP-H of
`docs/plans/2026-10-10-hud-split-panes-plan.md`, §4; decisions S-4).** The
owner asked for Claude Code's animated logo in each chat: while a chat's turn
runs, its foot — right after the transcript's scroller, before the ticker and
the input bar — reads mini orb · word · elapsed time: "Thinking…", "Running
<tool>…" (one cleaned line, capped at 40), "Waiting for approval", "Waiting
for your answer", "Error", or the static "Ended on an error" that stays until
the next turn or send. The precedence is a pure table (`lib/thinking.ts`):
an error in the turn, the ended error, an approval for this thread, a
question, a tool call this turn runs, the turn itself. Times read `12s`,
`3m 07s`, `1h 02m`.

- **The turn is keyed by thread, window-wide** (`State.turns`, H-1), fed by
  the turn events *and* the activity records, so the line follows its thread
  across a trade of conversations, a turn started from Discord before any
  pane showed it, and a reload. Only a *change* of a thread's activity
  counts (the daemon publishes on change, and the record that turns a thread
  idle follows its `turn_finished`, so a view still saying "working" must not
  revive a turn whose finish was heard). "Working" reaches the line through
  the entry it makes, never directly.
- **A running turn no pane tracks is adopted** (H-3) by the drawn chat pane
  showing it, if that pane waits on nothing: Stop and Enter-to-steer come back
  after a reload or a Discord-started turn. Only for a live entry, so a pane a
  finish just freed is never adopted back before the idle record arrives; the
  15 s reconcile undoes a wrong one.
- **The daemon says since when** (H-b): every thread record carries
  `running_since` (ISO) while `running`, never stored, so a reloaded line
  counts the true elapsed time.
- **The mini orb is an 18px inline SVG, not a canvas** (H-2): a 24-tick ring
  and a two-arc ring turning opposite ways and a flexing core, all CSS
  `transform` keyframes the compositor runs — no requestAnimationFrame, so a
  pane full of them costs the main thread nothing. Its colour is the big orb's
  palette, now one module (`lib/orbpalette.ts`) both orbs import: the avatar's
  accent recolours "Thinking…", never amber (a tool, an approval) or red (an
  error). `aria-hidden`; the word is the `role="status"`, the time is not.
  Reduced motion stops every animation and leaves the colour, the word and the
  ticking time. The times share one 1 s clock that runs only while a drawn line
  shows one, and only those spans redraw on a tick.
- **The card stays the loudest thing on screen.** The line is in the pane's
  flow — no position, no z-index, no button or link, `pointer-events: none` —
  inert under a card like everything else, and it never says Authorize,
  Approve or Deny. It is outside `[data-testid=log]`, so the log says the
  conversation and nothing else. The input bar's own status words are left as
  they are (the suites read them); removing the duplicate is a follow-up. The
  input bar re-measures its box's cap when the line comes and goes, so a long
  draft held at the conversation's minimum (PR #29) is not pushed below it.

Remaining: WP13 (the long-bench comparison, the owner's call on cost), a
native Windows worker, the R8 hook on Codex, and prompt tuning in
`roles.py` (§17's over-planning note). The daemon started by hand for the
live check runs under a 30-minute timeout; a durable instance is
`jarvis hud` from a terminal, or the systemd unit `jarvis daemon install`
prints, re-pointed at `daemon2`.
