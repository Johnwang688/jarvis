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
  `~/projects/jarvis-trading-firm` (D10). Pinned to `codex-cli 0.153.4`; the
  version check before each session stays.
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
   root, fork bombs, raw block writes) and the secrets layer (`.env`, token
   bundles, the allowlist file). Not approvable by anyone, from any path,
   including the escape hatch.
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
`task_status`, and one new tool, `task_propose`. It cannot edit, run a
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
zero is not zero), and the provider's own signals. From those it publishes
one of four states, shown on the HUD's SYSTEMS panel and in `status`:

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
  never approve and never answer. While a turn runs, up to three wait
  ("I'll take this next."), the fourth is refused. The last 500 message ids
  are remembered. An open provider question is answered by the next *typed*
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
  `localhost` dev server the owner names; never onto the HUD's own origin.
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
  on 0.153.4 declares `account/rateLimits/updated` with `usedPercent`,
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
  `− 100% +` in the status pane's header (the percentage resets it) or with
  Ctrl+= / Ctrl+- / Ctrl+0. The keys stand aside in an input, a textarea, a
  select and Monaco, where the browser keeps its own. Everywhere else they
  are the HUD's, not the browser's page zoom. It is CSS `zoom` on `#root`,
  because a page cannot set the browser's zoom and every size in `theme.css`
  is px. Viewport units inside `#root` scale with it, so every `vh`/`vw` cap
  divides by `--ui-zoom`. Undivided, the approval card's `86vh` was 138% of
  the screen at 160%. The card's button row is also sticky, so a long
  command scrolls under the buttons and never pushes them off the card. A
  menu placed from a screen rect divides by the zoom. Monaco already inverts
  the scale it measures.
- **Folding.** Each pane has a `«`/`»` button and a shortcut: Ctrl+B for the
  left pane, Ctrl+Alt+B for the right. A shortcut works even from the input
  bar, and AltGr's characters never match it. A folded pane leaves a 36px
  rail with an expand button. The left rail also carries **+ (New thread)**
  and the orb, scaled into the rail's foot so it never sits on the input
  bar. The right rail shows pending authorizations (amber) and an error (red),
  so a folded pane hides nothing that wants the owner. A folded pane is
  hidden, never unmounted, so its expanded projects, selected thread,
  compose draft and inline renames survive.
- **Resizing.** The inner edge of each pane is a `role="separator"` that can
  be dragged (pointer capture) or moved with the arrows (Shift for a bigger
  step, Home/End for the ends). Double-click resets it. The left pane runs
  180–480px and the right 240–560px, **in the HUD's own unzoomed pixels**,
  so a pane keeps its proportion to its text as the zoom changes. The centre
  keeps at least 480px. When the panes and that minimum do not fit, as with
  both panes open at 150% on a 1280px screen, the open panes give back their
  slack in proportion and the stored widths are left alone, so folding a
  pane or zooming out returns them. Monaco relayouts through
  `automaticLayout`.
- **Persistence.** Zoom and layout persist in localStorage
  (`jarvis.hud.zoom`, `jarvis.hud.layout`). Every read and write is guarded,
  each field falls back to its own default, and only a literal `true` folds
  a pane, so a mangled value never hides one.
- **Wrapping.** The tab bar and the input bar's chip row now wrap instead of
  clipping, so a narrow centre puts Model · Voice · Avatar · Settings on a
  second row rather than off the edge.

Remaining: WP13 (the long-bench comparison, the owner's call on cost), a
native Windows worker, the R8 hook on Codex, and prompt tuning in
`roles.py` (§17's over-planning note). The daemon started by hand for the
live check runs under a 30-minute timeout; a durable instance is
`jarvis hud` from a terminal, or the systemd unit `jarvis daemon install`
prints, re-pointed at `daemon2`.
