# SV-0 findings: native sub-agent protocol, recorded

**Status:** 2026-10-15. Probes and fixtures only; no product code changed.
This settles the **(unverified)** claims in
[the sub-agent view plan](2026-10-13-subagent-view.md) for the installed
**Claude Code 2.1.295** (SDK 0.2.153, the owner's venv) and **Codex 0.161.0**.
Code references are to `main` at `cb62b66`.

## How this was recorded

- **Offline, the established method**
  ([offline CLI plan](2026-10-13-browser-native-offline-cli.md)). Each run:
  - used the installed Linux binaries;
  - ran inside `unshare -Urn`, with loopback the only interface and no route
    (the recorders refuse to start otherwise);
  - was capped by `systemd-run --scope -p MemoryMax=3G -p MemorySwapMax=0`;
  - had a fresh temporary `HOME`/`CLAUDE_CONFIG_DIR`/`CODEX_HOME`/XDG
    directories, a constructed environment and dummy keys.

  No owner config, auth file, keyring or credential was read or inherited.
  No paid or subscription call was made: **$0**.
- **The model is scripted.** It is a local Anthropic Messages / OpenAI
  Responses endpoint that picks a fixed reply from a marker in the
  conversation. Every child prompt, command and text reply is a fixture
  string.
- **Recorders:**
  - `tests/v2/fixtures/subagents/record_claude.py` drives `ClaudeSDKClient`
    with the options Jarvis's provider uses where they matter:
    `include_partial_messages`, `replay-user-messages` and
    `permission_mode="default"`, plus a PreToolUse hook that allows.
    `setting_sources` is empty and no MCP server is attached. It also
    records PostToolUse, SubagentStart and SubagentStop.
  - `record_codex.py` drives `codex app-server --stdio` over raw JSON-RPC.
    It uses `approval_policy="on-request"`, `approvals_reviewer="user"`
    (as owner chats do) and `workspace-write` without network.
  - The production `CodexProvider` was not used: it requires ChatGPT login
    (offline plan §"Scripted provider"). The shapes below are the native
    protocol's. The provider's handling of them is read from code.
- **Recordings:** `tests/v2/fixtures/subagents/claude/*.jsonl` (15
  scenarios) and `codex/*.jsonl` (12).
  - Each line is `{seq, ms, src, data}`. `src` is `cli` (raw CLI stdout
    JSON), `hook`, `rpc_in`/`rpc_out` (every JSON-RPC message), `model` (what
    the scripted endpoint was asked and answered) or `harness` (what the
    recorder did, such as `stop_task` or `turn/interrupt`).
  - Scrubbed: temporary paths, the user name and installation ids. Nothing
    else needed scrubbing.
  - `claude/catalog.json` holds each agent's tool list and the Agent tool's
    input schema.
  - `codex/schema-0.161.0.json` holds the child-related types from
    `codex app-server generate-ts --experimental` and the
    notification/request method lists.
- **Pinned:** `tests/v2/subagent_fixtures_check.py` (23 tests, free, under a
  second) states each finding below as an assertion over the recordings.
  - Every `cli` row parses with the installed SDK's `parse_message`.
  - A newer CLI that changes a shape fails there, by name.

## Verdicts on the plan's (unverified) claims

### Claude

1. **"Whether CLI 2.1.295 streams sub-agent partials."** **REFUTED: it does
   not.**
   - Across all 15 recordings, run with `include_partial_messages=True`, no
     `stream_event` carries a `parent_tool_use_id`.
   - Sub-agent output arrives only as complete `assistant`/`user` messages
     tagged with `parent_tool_use_id`. They also carry `agent_id`,
     `subagent_type` and `task_description`, keys that SDK 0.2.153 does not
     model.
   - Today's TEXT_DELTA leak (plan "Deltas") does not happen. Whole-block
     TEXT/TOOL_* misattribution does.
2. **"Whether a *foreground* Agent sub-agent has a `task_id`, and whether
   `stop_task` stops it while the turn continues."** **CONFIRMED, both.**
   - A foreground spawn (`run_in_background: false`) emits `task_started`
     with `task_type: "local_agent"`, `tool_use_id` set to the spawn call id
     and `is_backgrounded: false`.
   - `stop_task(task_id)` gives `task_updated` `{status: killed}`, then
     `task_notification` `stopped`. The Agent call's `tool_result` is the
     error "[Request interrupted by user for tool use]".
   - The orchestrator's turn then continues: a new model request, then
     `result` `success`.
   - A sibling foreground agent running at the time finished normally
     (`stop_fg_parallel`). `stop_task` on a background agent works the same
     (`stop_bg`).
3. **Approval-card label ordering (Q7, D2): "the hook records `tool_use_id →
   agent_id`; the tagged block with that id gives the sid".** **NARROWED.**
   - The order of the hook and the tagged `assistant` message is **not
     stable**. A foreground child's hook came first; a background child's
     tagged message came first.
   - The ordering is not needed. The hook input's `agent_id` equals the
     `task_id` of a `task_started` that always arrived **before** the hook,
     and that `task_started.tool_use_id` is the sid. This was checked for
     every hook in every recording.
   - So map the label as `agent_id → task_id → tool_use_id`.
   - The SubagentStart/SubagentStop hooks' own `tool_use_id` is a random
     uuid, not the spawn id.
4. **"Claude Code documents that sub-agents cannot spawn sub-agents" (D2,
   L-C5).** **REFUTED for 2.1.295.**
   - General-purpose children are offered the `Agent` tool (Explore is not).
     A nested spawn ran.
   - The nested spawn's `task_started` has `spawn_depth: 2` and
     `parent_task_id` naming the outer agent. The `result`'s
     `subagent_stats.refused` lists `depth_limit` and `concurrency_limit`
     counters.
   - **Depth-2 messages are not forwarded:** only the grandchild's `task_*`
     lifecycle reaches the SDK stream. Its inner pane can show lifecycle and
     summary only.
5. **"Whether a Claude steer waits for a running Agent call" (L-C6).**
   **CONFIRMED: it waits.**
   - A `query()` sent while a foreground agent ran was taken in after the
     Agent call's `tool_result`.
   - Its replay echo came then, and the next orchestrator request carried it
     as "The user sent a new message while you were working".
6. **"A background sub-agent that outlives the turn would stall the same
   way" (L-C7).** **CONFIRMED, and larger than the plan assumed.**
   - **Background is the default.** The 2.1.295 Agent schema says "Agents
     run in the background by default … Set to false only when your very
     next action depends on this agent's result".
     - An Agent call that omits `run_in_background` got the immediate
       result "Async agent launched successfully" (`tool_use_result.status:
       "async_launched"`).
     - The orchestrator's turn ended with the agent still running.
     - Real orchestrators will usually background their agents.
   - **A finished background agent starts a new orchestrator turn by
     itself.** The CLI emits `init`, a full streamed assistant turn and a
     second `result` whose `origin` is `{kind: "task-notification"}`, with
     no `query()` sent. This happened in `one_default`, `parallel_default`,
     `stop_bg` and `background_unread`, once per finished agent:
     `parallel_default`'s two agents gave two injected turns.
   - **The stall reproduces.** In `background_flood` nobody read for 12 s
     after the first `result`.
     - The auto-turn's ~160 stream events overran the SDK's 100-message
       buffer (`_internal/query.py:174-176`).
     - A second background agent whose `sleep 4` had ended did not make its
       next model request until the recorder disconnected, 9.3 s after
       reading stopped.
     - The same probe with a small auto-turn (`background_unread`) did not
       stall (3.0 s).
   - **`client.interrupt()` kills background agents too**, even when no turn
     is running (`interrupt_bg`: `task_notification` `stopped`).
7. **"`killed` means stopped"; `TERMINAL_TASK_STATUSES`.** **CONFIRMED.**
   - `task_updated.patch.status` is `killed` (or `completed`/`failed`, with
     `error` text). `task_notification.status` is `stopped`/`completed`/
     `failed`.
   - Both arrive for every agent recorded, foreground and background.
   - `task_notification.summary` depends on the outcome: the child's final
     text on success, the spawn description on stop, the error on failure.

### Codex

8. **"Whether Codex sends a child agent's events to Jarvis at all" (L-X1).**
   **CONFIRMED.**
   - Every child notification arrives on the parent's app-server connection
     under the child's `threadId`: `thread/status/changed`, `turn/started`,
     `item/*` (command, agentMessage …), `thread/tokenUsage/updated`,
     `error` and `turn/completed`.
   - The same holds for grandchildren.
9. **"Upstream announces a child as a `collabAgentToolCall` item (tool
   spawnAgent, `receiverThreadIds`, `agentsStates`)… (unverified) for
   0.161."** **REFUTED as the announcement.**
   - The 0.161 schema has `collabAgentToolCall`, in the stable schema too,
     but no recording shows it for a spawn.
   - The spawn is a **`subAgentActivity`** item on the spawner's thread:
     `{id: <spawn call_id>, kind: started, agentThreadId, agentPath:
     "/root/<task_name>"}`.
   - `kind: completed` (id `subagent-completed-<child turn id>`) follows
     when the child finishes, and `kind: interrupted` when the model's own
     `interrupt_agent` tool stops it.
   - `collabAgentToolCall` appeared only for `wait` (`wait_agent`), with
     empty `receiverThreadIds` and `agentsStates`. The emptiness is partly an
     artifact of the scripted call (`wait_agent` with only `timeout_ms`, no
     targets); a real model naming targets may fill them. So SV-2 must not
     rely on the `agentsStates` fallback, nor assume it is always empty
     (review of #193).
   - The same shapes were recorded with `features.multi_agent_v2 = true`
     (`one_multi_agent_v2`). `multi_agent` is stable and **on by default** in
     0.161 (`codex features list`), and production config does not change
     it.
10. **The `thread/started` fallback (D3).** **REFUTED: there is no
    `thread/started` for a child**, in any recording. The only reliable
    identity source is `subAgentActivity` **`item/started`**.
    - It precedes the child's first notification.
    - The matching `item/completed` does **not**: it arrives after the
      child's first `thread/status/changed`.
    - A grandchild is announced on its parent child's thread, with
      `agentPath` `/root/sv0_outer/sv0_inner`.
11. **"If a child's approval reaches this connection (unverified), today it
    kills the orchestrator's turn."** **CONFIRMED that it reaches it.**
    - `item/commandExecution/requestApproval` arrives with the **child's**
      `threadId`/`turnId`.
    - It carries no parent marker beyond that.
    - Its `availableDecisions` lists `accept`, `acceptWithExecpolicyAmendment`
      and `cancel`. `decline` was nevertheless accepted.
    - Both `decline` and `accept` were non-fatal to the child (`approval`,
      `approval_accept`).
    - The consequence in Jarvis is read from code, not run. The non-MCP
      server-request branch replies with an error and raises
      `RpcError("Codex server request identity mismatch")` when
      `threadId != s.thread_id` (`codex.py:905-909`). The MCP-elicitation
      (`codex.py:896-898`) and `currentTime/read` (`codex.py:886-887`)
      branches do the same for a child's thread, so **any** server request
      from a child thread, of any method, is fatal to the orchestrator's turn
      today (review of #193); the fix must cover all three branches.
12. **"`turn/interrupt` targets the parent only. Whether it accepts a child's
    thread" (L-X3).** **CONFIRMED that it does, NARROWED in effect.**
    - `turn/interrupt {threadId: child, turnId: child turn}` returns `{}`.
      The child's turn completes `interrupted`, and the parent's turn goes on
      to `completed`.
    - **The child's running command is not killed:** `sleep 25` ran to
      completion and reported `item/completed` 23 s after the interrupt.
    - **The parent is not told:** no `subAgentActivity` `interrupted` or
      `completed`, and `wait_agent` timed out.
    - Compare the model-side `interrupt_agent`, which yields
      `subAgentActivity` `interrupted`.
13. **"Child against parent token totals" (L-X4).** **CONFIRMED: the parent
    excludes children.**
    - The parent's `thread/tokenUsage/updated.total` counted only its own
      requests (110 × 4).
    - Each child reports its own totals on its own `threadId`, with its own
      `turnId`.
14. **"Codex's limit on concurrent children" (D8).** **NARROWED, not
    probed.**
    - Two children ran concurrently, and a nested one spawned.
    - The binary carries the config keys `agents.max_concurrent_threads_per_session`
      and `agents.max_depth`. Their defaults were not measured.

### Also recorded (not plan claims, but SV-1/SV-2 must know)

- **Claude task messages that are not sub-agents.** A child's Bash that runs
  more than about 2 s becomes a task:
  - `task_started` with `task_type: "local_bash"`, `owned_by_subagent: true`,
    `parent_task_id` and `tool_use_id` set to the **child's Bash call id**;
  - then its own `task_notification`.

  Only `task_type == "local_agent"` whose `tool_use_id` is a spawn call the
  provider saw creates a sid.
- **Claude names and tokens.**
  - `task_started` carries `description` (the row's name), `subagent_type`,
    `prompt` (the brief, for Q3), `spawn_depth` and `is_backgrounded`.
  - `task_progress.description` is the **current activity** (the child's
    tool description), not the name.
  - `task_progress.usage` and `task_notification.usage` carry `total_tokens`,
    `tool_uses` and `duration_ms`.
  - `result.subagent_stats` summarises a turn's agents.
- **Claude cost.**
  - `result.usage` is the orchestrator's own tokens.
  - `modelUsage`/`total_cost_usd` include sub-agent requests and remain the
    process's running total, as the provider docstring already says.
- **Claude interrupt of a foreground agent** ends the turn with `result`
  `error_during_execution`, `terminal_reason: "aborted_tools"`, after the
  agent's `stopped` notification. The session then takes a new `query()`
  normally.
- **Codex children outlive the parent's turn.**
  - After a parent `turn/interrupt` (`parent_interrupt`) or a parent turn that
    did not wait (`no_wait`), the child kept running. It finished with
    `subAgentActivity` `completed` on the parent's thread, **stamped with the
    parent's already-finished `turnId`**.
  - No new parent turn started (unlike Claude).
- **Codex steer during `wait_agent`** ends the wait at once ("Wait
  interrupted"). The steer is handled while the child runs on.
- **A failed Codex child** emits `error` (`willRetry: false`) and
  `turn/completed` `failed` on its own thread. The parent gets **no**
  `subAgentActivity` for it.
- **Codex child model requests** carry `x-openai-subagent: collab_spawn` and
  `x-codex-parent-thread-id`. Their `x-codex-turn-metadata` has
  `subagent_kind: thread_spawn`, `thread_source: subagent` and
  `agent_name: /root/<task_name>`. This is consistent with WP-3.

## What SV-1 (Claude) can rely on

- Identity:
  - sid = `cl:<spawn tool_use_id>`;
  - `task_id` = `agent_id`, the key between hook input, tagged messages and
    `task_*`;
  - `task_started` precedes all of a sub-agent's messages and hooks.
- Tagged `assistant`/`user` messages (by `parent_tool_use_id`) are the only
  sub-agent content.
  - There are no sub-agent deltas to drop.
  - Nested agents' content never arrives; show lifecycle only.
- Terminal status comes from `task_notification`/`task_updated` for every
  agent. The spawn's `tool_result` is **not** a finish signal for background
  agents: it is the "async launched" receipt.
- `stop_task` stops exactly one agent, foreground or background, and the
  turn continues. `CLAUDE_STOP_VERIFIED` can be set **for this CLI version**
  on this evidence. Whether it should also need a live subscription run is
  the owner's call: nothing in the stop path depends on auth or the model.

## What SV-2 (Codex) can rely on

- Identity:
  - the child set comes from `subAgentActivity` `item/started` on the
    session's own thread or a known child's thread (`agentThreadId`,
    `agentPath`, `id` = spawn call id);
  - never from `thread/started` or `collabAgentToolCall`.
- Child notifications arrive and can be routed by `threadId`.
  - A child's `turn/completed` gives its status (`completed`, `failed`,
    `interrupted`).
  - The parent-side `subAgentActivity` `completed` is absent for failed and
    externally interrupted children.
- A child's command approval arrives with the child's `threadId` and can be
  answered without harming the child. Q2's design choice is safe
  protocol-wise. FileChange and permissions requests are expected to route
  the same way but were not recorded.
- `turn/interrupt` on a child thread ends that child's turn and nothing else.
  - It leaves a running command alive and does not tell the parent model.
  - `stoppable` may be offered only with that caveat stated, or not at all
    (the owner's call; see below).
- Child tokens are separate and must never enter `s.accounting`. The parent
  totals already exclude them.

## Recommended scope adjustments

- **SV-1: add background handling and treat SV-6 as required.** The plan
  treated background agents as the edge case. On 2.1.295 they are the
  default, and their completion **injects a whole orchestrator turn**
  between Jarvis's sends.
  - So SV-1 must not mark live sids `stopped` at `turn_finished` when
    `is_backgrounded` is true.
  - SV-6, the between-turn reader, moves from "decide after SV-0" (Q6) to
    **needed**. Without it, any owner chat whose model backgrounds an agent:
    - would, by reading `_read`/`_Pump`, attribute the injected turn to the
      next owner message (that turn's stale `result` is read first; not run
      through the provider);
    - can stall once that turn streams more than 100 messages, as
      `background_flood` shows.

  An interim alternative for the owner to weigh: Jarvis could ask the
  orchestrator to set `run_in_background: false` (via `system_append`).
  That is a behaviour change to the model's tool use, not a protocol fix.
- **SV-1: say what Stop does.** Jarvis's Stop (`client.interrupt()`) also
  kills background agents. Say so in the UI, or the "stopped (left running
  in the CLI)" wording in D8 is wrong.
- **SV-1: two small fixes to D2.** Label approval cards via `agent_id →
  task_id`, and ignore `local_bash` tasks.
- **SV-2: narrower.**
  - Drop the `agentsStates` fallback and the `thread/started` fallback.
  - Register children on `subAgentActivity` `item/started`.
  - A child's status comes from its own `turn/completed`.
  - The non-fatal child request is the most urgent piece: today a single
    child server request (an approval, an MCP elicitation, `currentTime/read`)
    kills the orchestrator's turn. It could ship ahead of the
    view as a small safety fix.
  - Because children outlive the parent turn, the RPC queue (4096,
    `codex_rpc.py`) fills with child traffic between turns. SV-6's Codex
    drain is needed for the same reason as Claude's.
- **SV-4 (Codex stop):** offer per-child Stop only if the owner accepts "ends
  the child's turn; a running command may finish". Otherwise keep it absent
  with the D3 reason.
- **Nothing here needs a live or paid run.** The open items are:
  - a real model's choice to background agents, which affects frequency, not
    shapes;
  - Codex's concurrency defaults;
  - subscription acceptance, already UNRUN for the offline method.

  None blocks SV-1 or SV-2. A live owner-PC run (L-C1…L-X5) remains the
  acceptance check after SV-1/SV-2 land, as the plan already says.
