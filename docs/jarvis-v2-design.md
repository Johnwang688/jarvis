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
looks like. `reviewer_declined` is the event D7 hangs on.

### 5.3 ClaudeProvider

- Built on the **Python `claude-agent-sdk`**, which spawns the installed
  `claude` binary. Verified against the docs 2026-09-15: `can_use_tool`
  (permission callback), `PreToolUse`/`PostToolUse` hooks, streaming input,
  `resume=`. The raw CLI's `--permission-prompt-tool` no longer exists in
  2.1.233, so the SDK is the only supported path for headless permission
  routing.
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
   the call (R2); on Codex they run in the app-server approval handler.
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
  are parsed first; anything else is a steering message to the orchestrator.
- In a **project channel** (not a thread): `task: …` opens a task; other
  owner messages go to the project's chat thread on the fast path.
- Everywhere: owner only, typed only for approvals and verbs (the v1
  spoken-turns-never-authorize rule stands).
- Approval codes are accepted **only in the thread the request was posted
  in**, the v1 same-channel rule applied to threads.

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

---

## 12. HUD v2 (G3) — placeholder pending the owner's elaboration

Settled so far:

- **Rebuilt** as a real frontend served by the daemon, still opened in the
  owner's Windows Chrome in app mode on the fixed port so the mic grant
  survives. Stack to be chosen in the HUD work package; the requirement is a
  component model and a test harness equal to v1's headless suites.
- **Layout** in the shape of the Claude Code / Codex desktop apps: left rail
  of projects → threads/tasks; main pane the active thread (rendered
  markdown, tool stream, diff view for file changes); right pane the task's
  plan, status record and approval queue. The orb stays as the push-to-talk
  control and state indicator.
- **Carried over as components:** orb + ring sets, avatar face, wake word and
  the always-open mic capture pipeline, TTS speculation, markdown renderer
  (DOM-built, never `innerHTML`), approval card semantics (deny cheap,
  authorize deliberate, nothing keyboard-defaulted), model and voice pickers.
- **Invariants that do not move:** the HUD is the approval surface, so no
  agent-reachable lever closes it, drives it or navigates it
  (`is_face_origin`, `FORBIDDEN_TITLES`, avatar SVG sanitizer); `<title>`
  stays `J.A.R.V.I.S.`.
- **To be specified by the owner:** which Claude Code app behaviours and which
  Codex app behaviours to take, and what the sci-fi theme must keep.

---

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
  Open sub-question for WP3: whether a hook may block for minutes while an
  owner is asked, or whether the provider must deny-and-requeue instead.
- **R3** Claude Code's bubblewrap sandbox in headless mode under WSL2.
  Fallback: rely on auto mode without the sandbox on Claude workers, and
  prefer Codex (Landlock, default on) for tasks that need confinement.
- **R4** Codex app-server approval requests answered by our client end to
  end, including `requestUserInput` as a clarification question, on 0.153.4.
- **R5** Discord: thread creation and message editing under the bot's current
  permissions and intents; auto-archive reopen behaviour.
- **R7** Launching a CLI worker is itself an action a classifier may
  refuse. Verified 2026-09-15: from a Claude Code session in auto mode, the
  command that starts `codex exec --approve-for-me` on the WP1 brief was
  blocked by the auto-mode classifier as spawning an autonomous agent. The
  daemon is not a Claude Code session, so this does not affect v2 at runtime,
  but it is the D7 case in miniature and it is why the escape hatch shows the
  whole command to the owner rather than trying to get around the reviewer.
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
