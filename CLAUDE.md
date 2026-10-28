# CLAUDE.md

Working instructions for agents changing Jarvis. Read this guide, then the area
notes and design/decision documents for the code you will touch. Update current
rules here; put implementation history, review findings and measured results in
the appropriate area note. Keep this guide near 38–40 KB.

The previous file is preserved verbatim in [area notes](#area-index), with a
[migration manifest](docs/notes/claude-migration.json). Those notes mix live
rules, dated findings and superseded behavior. Moving a rule does **not** revoke
its safety obligations. Summaries never permit omitting a guard: read
the complete relevant notes before changing a boundary. Resolve
conflicts by the newest explicit owner decision and current code.
Historical “live” claims are not new deployment receipts.

Invariants and decisions are retained byte for byte; their named paragraph
references are in the notes. Preserved paths are repository-relative.

Tool-call labels are untrusted text, shell-quoting splits included
([HUD notes](docs/notes/hud-workspace.md)).

<!-- retained:chat-git-and-v2:begin -->
## Chat git worktrees (2026-10-13)

Claude and Codex chats share the project checkout with the owner and must use
separate worktrees for real edits. Codex chat configs explicitly grant only
validated repository git/common directories and config.CHAT_WORKTREES_DIR
(default ~/.local/share/jarvis/chat-worktrees, override JARVIS_CHAT_WORKTREES);
the root is created privately (0700). Task briefs get no extra grants.
Symlinked/malformed metadata, credential directories, protected state and their
ancestors are excluded; uncertain credential-name scans fail closed. Sandbox
networking stays off, including effective-config and returned-policy checks.
Push/gh therefore request escalation through the existing Jarvis gate; a
sandbox-widening request still asks the owner. Writable git metadata permits
hooks/config writes, an explicit owner-approved risk. Provider regressions
are in tests/v2/codex_provider_check.py; deployed/live status belongs in the
work checkpoint, not an inference from unit tests.

## Jarvis v2 is underway (2026-09-15) — read `docs/jarvis-v2-design.md` first

The v1 notes below remain the truth for everything **not** under
`jarvis/v2/`. v2 turns Jarvis into a control plane: Claude Code and Codex are
the brains for real work, the v1 loop survives only as the chat/voice fast
path, and the design document holds every decision, the work-package table,
and the live findings (R1–R8). Do not relitigate what its §2 table settles.

<!-- retained:chat-git-and-v2:end -->

<!-- retained:purpose-and-environment:begin -->
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

<!-- retained:purpose-and-environment:end -->

## Working on v2

Codex chats hold no GitHub login (CG-0): push/PRs use Codex-only `chat_git`
tools, each asked; escalated push/gh is declined (supersedes the note
above; [plan](docs/plans/2026-10-15-codex-git-credentials.md)).

Claude's `PreToolUse` hook is its gate; its SDK sandbox does not confine on
this WSL2. Preserve native CLI resolution/version floors and startup/resume
policy checks. Sandbox widening needs a human, never Always; deny without one.
Layer 1 refuses protected/credential grants. Unknown/malformed approvals fail
closed. Both providers' `answer()` only denies.

Use `TaskControl` for task verbs. Tasks pin their root. Archive keeps records;
permanent delete is owner HUD-only, archived-only, into trash, never project
folders/worktrees. Never `_Store._delete` a project. `Daemon.start()` never calls
a model. Quota, title, cost and cache claims need evidence.

The HUD uses **Graphite**: warm off-black/glacier-cyan, amber pending/red error,
overriding the global palette. Preserve dimensions, input behavior and owner
workspace/editor/Browser decisions. Driving uses owner switches.
Owner HTTP Basic/Digest auth uses only the exact private tab socket;
credentials never enter tools, replay, status, logs or config. Pending
navigation and cleanup: Browser note and HTTP-auth plan.

## Inbox

`/inbox` routes are owner-only; agents only post via `owner_post`, never read,
answer, mark or grant. Marks hide ranks 5–8 only. Quiet-hour asks defer
(rank 4); owner Approve and retry grants one exact call once.
[Design](docs/plans/2026-10-24-owner-inbox-design.md); rules: control-plane notes.

## Safety design (deliberate, do not loosen without asking)

Read [all safety rules/counterexamples](docs/notes/safety-permissions.md) and
[control-plane rules](docs/notes/control-plane.md) for boundary changes. These
summaries do not replace detailed matchers, exceptions or owner choices.

- **Enforced toolsets.** Tools stay narrow and typed. Keep `run_readonly`
  separate from `run_command`. Dispatch refuses unbound/out-of-toolset calls
  before argument parsing and approval, dangerous or not. New callers bind
  their tools. Deferred groups cannot widen explicit toolsets and require a
  nonempty held core. ContextVars/copied worker contexts carry the caller's
  approver, tools and limits. `unconfined` is code-only, never a model setting.
- **Commands are judged in full.** Every quote-aware segment is judged; DENY
  wins and uncertain work asks. Keep separate rules for inline source,
  substitution, writes/redirects and runner wrappers. A binary allowlist is
  valid only with its original constraints. ALLOW skips a prompt only for a
  human-backed approver; deny-all workers stay deny-all. Allowlist coverage
  requires every segment; substitutions/fetch-execute never gain static
  coverage. Preserve whole-token stem/full-path matching.
- **Permission state cannot authorize its replacement.** Known allowlist
  writes are DENY even in mode “all”; other protected-state writes and uncertain
  paths retain their ASK/deny behavior despite allowlisting. Keep the shared
  detector, v1/v2 parity, indirect/ancestor/symlink checks, routing/model/default
  files, Discord state and saved Codex catalog. Do not silently alter the
  owner's explicit SELF_PROTECTED decisions.
- **Secrets are use-but-never-see.** Agents read no credential `.env*` files;
  they write only permitted `*.example` files. The owner can edit credentials
  in the owner-only HUD editor. Use the shared env-name/path helpers including
  case, Windows trimming, symlink names/targets and fail-closed uncertainty.
  Approved shell commands still cannot print credentials. Retain name refusal,
  command refusal and output scrubbing. Read/search/diff/attachment/preview/
  terminal-read paths never become another credential route. File writes keep
  read-before-write. Tests always use temporary config, never real tokens.
- **No operation of one's own gate.** Only the broker asks the owner. Keep
  identity/listener/Origin/place checks and `config.control_ports()` parity.
  Browser/fetch refuse the control plane before actions, reads and every
  redirect; HUD/API responses refuse framing, and write tools cannot alter
  the installed HUD. Owner restart/rebuild, daemon-unit and daemon-kill
  operations remain agent-refused. A test pass is no authorization to restart.
- **Public web does not open the LAN.** Keep address, redirect/landing and host
  checks, fresh contexts and action budgets. v1 never takes the owner's real
  browser profile. The v2 URL judge, SOCKS gate and action reviewer do different
  jobs: read [Browser notes](docs/notes/browser.md) before changing any. Only
  owner navigation admits a port while its tab remains; last departure revokes
  local tunnels too (BR-1c replaces the archived lifetime rule). The gate is
  shared. Auto-open is Direct only and never admits a port. No frame takes focus without the owner's gesture,
  including behind cards or in hidden panes.
- **Untrusted input stays data.** Strip hidden/invisible web text while keeping
  visible content, fence fetched text and retain provenance. Script review is
  an accident guard, not an adversarial boundary: unsafe denies, errors/unclear
  ask, safe auto-approves only from configured trusted install hosts. Keep its
  opt-out. SVGs are allowlist-sanitized and rendered as noninteractive images;
  avatars cannot repaint pending/error meaning or disguise the HUD title.
- **Delegation inherits limits.** Children intersect tools and inherit the
  approver and depth bound. Workflows keep safe tools/deny-all. Attended tasks
  carry the actual approver object/origin and human-backed flag, and forbid
  spawn recursion/foreground stealing. Shared-browser jobs serialize and keep
  the existing page/action budget; re-snapshot afterward. Session tools read,
  never switch the owner's live session. Speech never authorizes an approval.
- **Background git has its own boundary.** `gitops` judges the full invocation,
  writes owned worktrees/branches only and never merges. Keep hooks, fsmonitor,
  recursive submodule execution, executable config and transports hardened,
  and protected-file commit/push checks. Generic writes cannot alter `.git` or
  global git config. The approved v2 chat grants are not a blanket exception
  for v1 workers, tasks or other paths.
- **Integrations stay scoped.** Consent/setup is human-only and credentials stay
  inside the integration. Gmail sends remain gated. Spotify's intentionally
  ungated reversible playback is absent from unattended workflows, and rotating
  tokens persist before use. Onshape writes target the pinned sandbox; libraries
  are read-only. Desktop tools accept registered app names, not arbitrary
  paths/windows, and exclude terminals/file managers/approval browsers. The
  owner starts the bridge. Keep local voice privacy/offline and owner-only
  stores. VEX tests use fakes; compilation is not a verified robot download.

## Testing and operating the checkout

Use focused free checks, then the required integration sweep. Independent review
and meaningful tests precede merging. Preserve negative cases and mutation
evidence when a subtle invariant guards a boundary. Read actual suite summaries,
not just a wrapper's exit code. Report whether a result is committed, merged,
built, browser-tested, installed or live-validated.

In this WSL workflow, cap each test/build tree with
`systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0`. Use the
worktree's PYTHONPATH/shared venv. Tests use temporary HOME/config,
fake providers and ephemeral ports, never live 8402/8403/8405 or owner state.
Browser tests need an isolated mock server; do not operate the live daemon.

Under load, wait for the actual state rather than sleeping. Measure computation
with a clock appropriate to the path; calling-thread CPU excludes off-thread
work. A post-return wall assertion cannot interrupt a hang. Keep active deadlines
where needed, pin guard-strength constants and prove costly mutations still fail.
Expiry from a fake monotonic clock must be relative, not assume system uptime.
Count product faults separately from test-owned teardown.

Budget paid probes. Agent-bench, long-bench and SWE-bench answer different
questions; do not pool scores. Use the [SWE-bench runbook](swecompare/RUNBOOK.md)
and [test inventory](docs/notes/testing-benchmarks.md). Verify old prices/model
comparisons. The [old validation list](docs/notes/live-validation.md) is history,
not current completion evidence.

## Area index

| Area | Notes and current entry point |
|---|---|
| Purpose, WSL and v1 architecture | [Orientation](docs/notes/orientation.md) |
| v2 providers, steering, accounting | [Runtime](docs/notes/providers-runtime.md), [design](docs/jarvis-v2-design.md) |
| Gates, owner routes, session MCP | [Control plane](docs/notes/control-plane.md), [MCP decisions](docs/plans/2026-10-10-terminal-read-mcp-decisions.md) |
| Permissions, secrets, runners, Git | [Safety](docs/notes/safety-permissions.md), [env rule](docs/plans/2026-10-11-env-files.md) |
| Discord commands/channels/reports | [Discord](docs/notes/discord.md), [plan](docs/plans/2026-10-07-discord-plan.md) |
| HUD chats/layout/installed app | [Workspace](docs/notes/hud-workspace.md), [decisions](docs/plans/2026-10-09-hud-workspace-decisions.md) |
| Files, editor, diffs and settings | [Editor](docs/notes/editor.md), [decisions](docs/plans/2026-10-09-hud-editor-decisions.md) |
| Browser, preview, auto-open and looks | [Browser](docs/notes/browser.md), [decisions](docs/plans/2026-10-10-hud-browser-decisions.md), [amendment 2](docs/plans/2026-10-12-hud-browser-plan-amendment-2.md) |
| PTYs and terminal reads | [Terminals](docs/notes/terminals.md) |
| VEX build/device/project work | [VEX](docs/notes/vex.md) |
| Loop/transcript contracts | [Invariant snapshot](docs/notes/invariants.md); active copy below |
| Owner decisions | [Decision snapshot](docs/notes/decisions.md); active copy below |
| Suites and benchmark results | [Tests/benchmarks](docs/notes/testing-benchmarks.md) |
| Routing, reasoning and models | [Models](docs/notes/models.md) |
| Voice, avatars and older face | [Voice/face](docs/notes/voice-face.md) |
| Gmail, CAD, Spotify, schoolwork | [Integrations](docs/notes/integrations.md) |
| Windows app bridge | [Desktop](docs/notes/desktop.md) |
| Earlier goals/sessions/runtime | [Earlier runtime](docs/notes/legacy-runtime.md) |
| Old outstanding probes | [Historical validation](docs/notes/live-validation.md) |

`python scripts/slim_claude.py --check` rebuilds the original bytes and checks
retained copies and this guide's size. Keep archive payloads unchanged; update
the retained-copy check if an invariant or decision changes.

<!-- retained:invariants:begin -->
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
   fire across turns (or at an image-carrier boundary). **Except under
   steering** (v2's fast path, 2026-10-08): `Agent.take_steering` appends an
   `[owner steering]` user message at a step boundary, mid-turn, and that is
   a legal cut point, so compaction can now cut *inside* a turn. Still safe for
   the same reason as everywhere: the steer lands only where every tool_call
   already has its result, and `find_cut_point` walks the pending ids anyway,
   so the cut never orphans one; `messages[1]` stays pinned. What it can cost
   is the turn's own request being summarised while the turn still runs.

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

   **That now holds for the system prompt too** (2026-10-11, the owner's
   answer 5 on #34's follow-up). `config.SYSTEM_PROMPT` is written for the
   full conversation toolset and names plan_write, run_subagent, task_start,
   write_file and the rest, which the fast path, workflows, tasks, goals and
   the benches do not hold. Since #34 each such mention costs a refused step.
   `config.PROMPT_TOOL_PARAGRAPHS` lists every paragraph that names a tool,
   keyed by its opening words, with the versions it may take.
   `config.narrow_prompt(text, names)` picks, per paragraph, the first version
   whose tools are all held, and drops the paragraph when none is
   (`config.system_prompt(names)` is it applied to SYSTEM_PROMPT).
   `Agent.__init__` applies it to the whole of any prompt that starts with
   SYSTEM_PROMPT — a surface's own paragraphs after it included, so a
   surface paragraph that names tools gets a rule in the same table (the
   workflow's "For a change to a repository" paragraph, which agent-bench's
   sandboxed workflows hold no git tools for) — and no surface needs code of
   its own. "The browser" counts as a tool (`browser_goto` and
   `browser_snapshot`): its paragraph and the fence's "and the browser" are
   narrowed for agents without it, its conduct rules kept. The narrower
   versions are the original's own sentences, with a sentence or a tool's
   name taken out. With every tool held, the result is the text byte for byte
   (the wording is tuned). A paragraph that names a tool but has no rule fails
   `tests/prompt_tools_check.py`, so **adding a tool name to the prompt means
   adding its rule** — and the narrower versions are copies, held to their
   original sentence by sentence there, so **a change to a paragraph that has
   them is a change to each of them** (or the check fails, naming the
   sentence). Sub-agent types do the same through
   `AgentType.needs`: a child that lost a tool its brief names to the parent
   intersection is not sent, and says which. The case was a builder under
   long-bench, which holds no git tools.

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

   Three properties hold it together:

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
     a child — and since 2026-10-10 that is enforced, not only advertised:
     `load_tools` refuses a group whose core the caller does not hold, and
     `_sync_tools` folds in no such group however it reached `loaded_groups`.
     It had to be: the set `_sync_tools` re-binds is the one dispatch enforces.
   - **The toolset is enforced at dispatch** (2026-10-10; see *A toolset is a
     boundary* in Safety design). The bound `tool_names` is the agent's
     *current* toolset, expansions included, and `dispatch` refuses any tool
     outside it — as text, before the arguments are read. A deferred tool the
     agent *could* load (it holds `load_tools` and the group's core) gets the
     pointer to `load_tools`, because a model guessing a deferred tool's *name*
     has guessed its *arguments* too; anything else gets "not available to
     this agent". This paragraph used to say the opposite — "dispatch has
     always let any agent call any registered tool by name (the approver is
     the actual gate)" — which was only true while every tool that mattered
     to a boundary was `dangerous`, and it never was. **Unbound refuses**
     (2026-10-11; it failed open until then): no real path dispatches unbound
     (`tests/dispatch_toolset_check.py` walks them), so an unbound dispatch is
     a bug and runs nothing. Scripts and tests opt in with
     `runtime.tools_bound(names)` or `runtime.unconfined()`. A group with an
     **empty core** is refused by `register_group`, `load_tools` and
     `_sync_tools` alike — an empty core is "held" by every agent.

<!-- retained:invariants:end -->

<!-- retained:decisions:begin -->
## Decisions already made — don't relitigate

- **The owner's Browser keeps this machine closed like the LAN
  (2026-10-12, the lead's call on PR #69's MED 2, recorded for the owner).**
  The gate admits a loopback port only after the owner opened it in a tab
  through the owner-only routes, for the host's lifetime; `local_servers` in
  `browser.json` (off) opens them all. Pages and agents never open one.
  Auto-open never opens one (amendment 2, L2). Details in the BR-1a
  paragraph.
- **No Browser frame takes the keyboard without the owner's gesture
  (2026-10-12, R-1).** One that gains focus otherwise is removed at once
  (removal is the only thing that works); never under a card or from a
  hidden pane. Details in the "A framed page never takes the keyboard"
  paragraph.
- **A toolset is enforced at dispatch, not only by what the request offers
  (2026-10-10).** An agent's bound toolset is the set it may call; naming a
  registered tool outside it is refused, dangerous or not. A deferred tool
  gets the `load_tools` pointer only when the agent could load it. **With no
  toolset bound, dispatch refuses** (2026-10-11, owner's answer 4). Every
  real caller binds first, and a new caller of `dispatch` must too. Scripts
  and tests opt in with `runtime.tools_bound(names)` or
  `runtime.unconfined()`, in code only. Details in *A toolset is a boundary*
  (Safety design).
- **A runner is judged by what it starts; an allowlist entry for its stem is
  still a yes to what it runs (2026-10-12).** `decide`, the owner ops, the
  gate-state check and v2's always-ask rules read through `uv run`, `npx`,
  `npm exec -c` and their kin from one table (`rules.starts`); the
  allowlist's stems (`command_targets`, `unwrap`) deliberately do not, so a
  `uv` entry covers `uv run rm -rf build` as it always did. A DENY still
  wins before any entry is read.
- **Killing the v2 daemon by pattern is DENY for agents (2026-10-11, the
  owner's "yes add the daemon2 kill rule" on PR #48).** A `pkill -f`,
  `killall -r` or `pgrep -f`/`pidof`-fed `kill` whose pattern matches the
  daemon's command line at the word `daemon2`, and (review 2) a `kill` fed
  the daemon's PID by `systemctl show|status`, a D-Bus read, its cgroup,
  `lsof`/`fuser` on its ports or `ps | grep`, plus `fuser -k` on its ports
  and `pkill --cgroup` naming its unit (and, since the delta review, a grep
  over `/proc/*/cmdline` or `ss -p`/`netstat -p` on its ports). A source is
  judged only when a fed kill is on the line, and a kill pattern whose
  matching cost cannot be bounded is never matched: too complex to judge, so
  asked, never DENY (`kill_unsure`). `pkill -f jarvis` stays ALLOW (the
  2026-08-09 matrix); a literal `kill <pid>` and kills by process name
  (`pkill python`) are not seen. Details with the restart commands above.
- **Each agent's prompt names only the tools it holds (2026-10-11, owner's
  answer 5).** `config.narrow_prompt` narrows any prompt built on
  SYSTEM_PROMPT paragraph by paragraph, a surface's own paragraphs included
  (invariant 7). cad-bench holds `skill_list` and `skill_read`,
  so the CAD skill it measures can load. runtime.py and toolgroups.py are
  **not** SELF_PROTECTED: the owner declined it (answer 3).
- **Auto-opened dev servers are Direct only, and never open a port in the
  Remote browser's gate (2026-10-12, the lead's decision L2,
  `docs/plans/2026-10-12-hud-browser-plan-amendment-2.md`).** Since the
  owner asked for servers that Jarvis, Claude and Codex start to open too,
  an agent can trigger an auto-open. Counting one as the owner's own
  navigation would hand agents a lever on the gate. Only the owner's typed
  URL, click or "Open in Remote" opens a loopback port there. This
  **reverses** the note BR-1a's review first carried: that BR-8b's auto-open
  counted as the owner's opening. #69 merged with that note already
  corrected, in the BR-1a paragraph and the "closed like the LAN" bullet.

- **Agents that cannot ask get a typed git tool, not a broader shell
  (2026-10-09).** `git` left `run_readonly`; workflows and sub-agents reach
  git through `gitops.py`, which judges the whole invocation and writes only
  in worktrees it made, on `jarvis/` branches. The foreground still has
  `run_command` (asks) for anything else, and v2 tasks (a worktree each, Claude
  Code's own classifier) remain the better home for large changes — this is for
  the v1 loop's background agents. Plain push is allowed for the worktree's own
  branch only; force, delete, tags and anything to `main` are not, and a pull
  request is the only way work lands.

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
  `openai/gpt-5.6-luna`. That Luna default is superseded by the owner's
  choice of Haiku 5.5 on 2026-10-12. Do not route personal data (chat, transcripts,
  compaction summaries) to Chinese-hosted models — the cheap tier moved
  qwen3.7-flash → gpt-oss-20b for exactly this reason. Multi-model
  comparisons remain opt-in by naming models explicitly on `jarvis bench`.
  If Luna hits a capability wall, escalate the orchestrator to GPT-5.6 Terra
  or Claude Sonnet 5.
- **Model default update (2026-10-12).** The owner chose
  `anthropic/claude-haiku-5.5`. Below 100k prompt tokens it lists at
  `openai/gpt-6-luna`'s price: $0.10 in and $0.50 out per M, cache reads $0.01.
  From 100k, the whole request costs 5x: $0.50 in and $2.50 out per M.
  Compaction therefore defaults to 90k (`JARVIS_COMPACT_AT_TOKENS` overrides;
  a non-empty value must be an integer of at least 1000; empty means unset).
  A turn can grow past the threshold before a safe cut point exists (invariant 1).
  Default threads, sub-agents, compaction summaries and the review tier now
  run on Haiku 5.5. Saved HUD selections still take precedence. A bench given
  `-m` still runs its children and summaries through `models.tier()`; pin
  `JARVIS_ORCHESTRATOR` for comparable runs. Numbers taken before this PR
  are not comparable with numbers after it.
- **Claude prompt caching (2026-10-12).** Up to four explicit ephemeral breakpoint
  positions: the system message, separate image and full-tool-result window
  boundaries from `context.manage()`, and the newest markable message before
  the volatile tail. The earlier middle prefix is already truncated/evicted
  and stays unchanged on subsequent steps. The later middle prefix can be
  invalidated by the other window, but reuses more result text between sparse
  image evictions; a single minimum boundary let old live images hold it back.
  Compaction can invalidate either prefix. Each window contributes only once
  its keep count is reached; coincident boundaries use one point.
  Breakpoints walk back to nonblank text only, never image parts or assistant
  messages with tool calls. Caller boundaries are deduped and capped at four,
  dropping oldest middle points while preserving the system and tail.
  The caller's transcript stays verbatim. One-shot summaries, delegation and
  reviews opt out with `cache=False`. The handoff prompt follows the context
  block, so its `volatile_tail` is 0.
  The old top-level breakpoint landed on the working-context tail, rebuilt
  every step (invariant 7), so it never hit for real agents with that block.
  A live Haiku 5.5 probe with the default toolset, about 20k prompt tokens and
  six steps read zero cached tokens each step and paid a 1.25x cache write,
  $0.0026 per step. Explicit breakpoints read about 19.2k of 20k tokens from
  step 2 on, $0.00035 per step. That was a short turn with small results;
  in long turns with big results, middle breakpoints preserve reusable prefixes
  as context management rewrites older messages. A breakpoint on a tool
  message worked. The separate-window fix has synthetic regression coverage;
  its provider cache-read improvement still needs a live long-turn probe.
  A live three-step tool turn on Haiku 5.5 at a reasoning effort ran without
  error although `_stream_once` does not pass `reasoning_details` back.
  Nothing changed for reasoning.
- **Semantic/RAG memory deferred.** Memory is one markdown file per fact, pulled
  on demand via tools, so nothing is auto-injected. The upgrade ladder is
  substring → SQLite FTS5/BM25 → embeddings, and the tool interface
  (`memory_search(query) -> text`) is the contract, so swapping backends changes
  nothing else. Revisit when search visibly misses.

<!-- retained:decisions:end -->
