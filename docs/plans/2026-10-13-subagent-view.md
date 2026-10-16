# Sub-agent view (SV)

**Status:** plan, 2026-10-13. Docs only. Line numbers are from `claude/cloud-plans-10-13` at be982b8. **(unverified)** marks a claim about a CLI that a live probe must settle before code relies on it.

## For the owner

- **What you get.** When your orchestrator chat launches its own sub-agents (Claude Code's Agent tool, Codex child agents), each one shows as **one live row** in the chat, at the point it was launched. The row shows:
  - name and status (running, done, failed, stopped);
  - the tool it is running now and how long it has run;
  - its tokens, when the CLI reports them.
- **Click the row** to open that sub-agent's full live transcript in a read-only split pane beside the chat.
- **Stop one sub-agent** with its own button, where the CLI allows it. Where it doesn't, the button is missing and the row says why. Steering stays with the orchestrator.
- **The orchestrator's chat gets clean again.**
  - Today a Claude sub-agent's words and tool calls land in the orchestrator's transcript, its Discord reply and its task report as if the orchestrator had written them.
  - Once the voice rework lands, they would also be read aloud as the orchestrator's.
- **Voice:** sub-agents are silent. Only "<name> finished" or "<name> failed" is spoken.
- **Not proven yet:**
  - whether Claude can stop one foreground sub-agent without stopping the turn;
  - whether Codex sends a child agent's events to Jarvis at all;
  - what happens to sub-agents that outlive the orchestrator's turn.

  One recorded probe per CLI (SV-0) settles these before any code.
- **What I need from you:** eight answers at the end. The main ones are Q2 (a Codex child asks for approval) and Q6 (sub-agents that keep running after the turn).

## Decisions recorded (owner, 2026-10-13)

- **SV-1. Scope: native CLI sub-agents only.**
  - This means Claude Code's Agent/Task sub-agents and Codex child agents inside a HUD chat.
  - Out of scope: v2 tasks, v1 `run_subagent`, workflows and goals.
  - The provider change still applies to every Claude and Codex session, task threads included (§D4), because the separation lives in the provider.
- **SV-2. Placement: inline plus "open in pane".** A compact live row sits at the spawn point. A click opens the full live transcript in a split pane, through the existing split machinery, as a new read-only view.
- **SV-3. Control: watch plus stop one.**
  - Stop is offered per sub-agent only where the CLI supports it.
  - No capability is invented: where unsupported, the button is absent or explained.
  - Steering stays with the orchestrator.
- **SV-4. Voice: sub-agents are silent.** Only a one-line "<name> finished" or "<name> failed" goes through the shared speech queue. The orchestrator's prose is read as usual.

## What the system does today (verified)

**Claude** (`jarvis/v2/providers/claude.py`).

The SDK was checked in `claude-agent-sdk` 0.2.159, the version installed in this cloud container. The owner's venv version is not pinned in `pyproject.toml`; design §5.3 last cites 0.2.153.

- **`_translate` (`:1561-1622`) never reads `parent_tool_use_id`.**
  - Every text, thinking and tool-use block becomes TEXT, THINKING or TOOL_STARTED on the parent thread (`:1573-1601`).
  - Every tool result becomes TOOL_FINISHED (`:1605-1610`, `_tool_result` `:1632-1688`).
  - So sub-agent activity is interleaved with the orchestrator's own.
  - The field is read once, at `:1341`, so that a sub-agent's "Prompt is too long" is not taken as the parent's context-full.
- **Deltas.** `StreamEvent` has `parent_tool_use_id` (SDK `types.py:1381-1387`). `_translate` makes every text delta a TEXT_DELTA on the parent (`:1567-1572`, `_delta_text` `:2111-2120`) without checking it. Whether CLI 2.1.295 streams sub-agent partials is **(unverified)**; if it does, they reach the orchestrator's draft today.
- **Task messages are dropped.** Any `SystemMessage` yields no event (`:1564-1566`). In SDK 0.2.159 these four are `SystemMessage` subclasses (`types.py:1162-1282`):
  - `TaskStartedMessage`: `task_id`, `description`, `tool_use_id`, `task_type`.
  - `TaskProgressMessage`: `usage {total_tokens, tool_uses, duration_ms}`, `last_tool_name`.
  - `TaskNotificationMessage`: status completed, failed or stopped, plus `summary` and `usage`.
  - `TaskUpdatedMessage`: `patch.status`, which can be `killed`.

  Each keeps the raw `subtype` and `data`, so code that reads `subtype` also works on older SDKs.
- **The gate already sees sub-agent calls.** The PreToolUse input carries `agent_id` for them (SDK `types.py:308-329`). Jarvis uses it only to refuse `foreground_only` session tools (`claude.py:888-907`).
- **Stop.** `ClaudeSDKClient.stop_task(task_id)` exists (SDK `client.py:435-456`). Afterwards the CLI emits `task_notification` with status `stopped`. Jarvis calls only `client.interrupt()`, which stops the whole turn (`:1823-1831`).
  - **(unverified)** whether a *foreground* Agent sub-agent has a `task_id`, and whether `stop_task` stops it while the turn continues.
- **Nothing reads the stream between turns.** `_Pump` (`:2063-2108`) reads only on a request from inside `send`.
  - The providers-runtime notes (2026-10-12) record the effect: an unread notice filled the SDK buffer, and the CLI "stalled in its next PreToolUse hook until the next send drained it".
  - A background sub-agent that outlives the turn would stall the same way.

**Codex** (`jarvis/v2/providers/codex.py`, 0.161):

- **Child notifications are dropped.** After the turn/start reply, a notification whose `threadId` is not the session's is skipped (`:938-939`). So is one from another turn (`:940-942`). Before the reply, only items for our own thread are kept (`:933-936`).
- **A server request from another thread is fatal.** It is answered with an error and raises `RpcError` (`:905-909`), which closes the session (`:974-977`).
  - Owner chats route every native approval to Jarvis (`approvals_reviewer="user"`, `codex_config.py:210-212`).
  - If a child's approval reaches this connection **(unverified)**, today it kills the orchestrator's turn.
- **Child markers are known.**
  - A child's MCP call carries `parent_thread_id`, `subagent_kind` or `thread_source` in `x-codex-turn-metadata` (`mcp_session.py:131-132`). The provider uses this to refuse child terminal reads (`codex.py:1097-1107`).
  - WP-3's live probes confirmed parent/child identity (terminal-read plan `:536-550`; control-plane notes, WP-3).
- **Spawn items are ignored.** `_notification` reads only command, fileChange, MCP, agentMessage, reasoning and plan items (`:1033-1066`).
  - Upstream Codex announces a child on the parent as a `collabAgentToolCall` item: `tool` spawnAgent/sendInput/wait/closeAgent, `receiverThreadIds`, `prompt`, `agentsStates`.
  - This is **(unverified)** for 0.161: the protocol notes do not list it.
- **Limits.**
  - The RPC queue holds 4096 messages, and overflow invalidates the session (`codex_rpc.py:72, 100-109`).
  - `turn/interrupt` targets the parent only (`:1456-1463`). Whether it accepts a child's thread is **(unverified)**.

**Who misreads interleaved events today.**

- `Daemon._record` logs and publishes them (`daemon.py:1371-1460`, thread check `:1386-1387`).
- The transcript route shows `text` rows as the assistant's (`hud_api.py:1867-1879`).
- Discord posts every `text` of a turn as the reply and footers every tool name (`discord/mirror.py:1066-1072`).
- The runner joins every `text` into the task result (`runner.py:698-701`).
- The HUD settles every `text` into the pane (`App.tsx:1087-1123`).
- The voice rework reads `text_delta`/`text` for any shown thread (`docs/plans/2026-10-13-voice-rework.md` §3.7, V-3).

So separation fixes today's transcript, Discord and task-report misattribution, and the voice bug before it ships (§D7).

## Design

### D1. Identity and the event envelope

- **`sid`**, stable and scoped to the provider:
  - Claude: `cl:<parent ToolUseBlock.id>`, the id that children's `parent_tool_use_id` names.
  - Codex: `cx:<child threadId>`.
  - Form: `^(cl|cx):[A-Za-z0-9_-]{1,128}$`.
- **Two new kinds, not the old kinds with a tag.**
  - A tag on TEXT or TOOL_STARTED would be misread by every consumer that doesn't know about it, which is today's bug.
  - New kinds fail closed: Discord's loop, the runner, the ledger, `running_cost`, titles and the transcript route all filter by kind, so they ignore them unchanged.
  - **`subagent`** (lifecycle) carries `{sid, op: started|progress|finished, parent_sid, parent_call_id, provider, kind, name, status, current_tool, tokens, tool_uses, stoppable, stop_reason, started_at, finished_at}`.
  - **`subagent_event`** carries `{sid, event: {kind, data}}`. The inner kind is one of `text`, `thinking`, `tool_started`, `tool_finished`, `reviewer_declined`, `plan_updated` or a non-fatal `error`, with today's §5.2 data.
  - Both are added to `EventKind` (`provider.py:66-80`).
  - `Event.thread_id` stays the orchestrator's thread.
- **No sub-agent deltas.** The pane shows each text block when it completes. This keeps the bus small with many agents in parallel.
- **Never a `usage` or `turn_finished` for a sub-agent.** Its tokens ride `subagent` progress, so the ledger, `thread.tokens` and `running_cost` are untouched.
- **The parent keeps its spawn call.** Claude's Agent call stays an ordinary tool pair on the parent, so Discord's footer and the runner see it as today. Codex's spawn item yields only `subagent` started.

### D2. Claude provider

- **Tagged messages.** In `_translate`, read `parent_tool_use_id` on `AssistantMessage`, `SdkUserMessage` and `StreamEvent`.
  - When it is set, build events exactly as now (`_safe`, the private summaries, the denial mapping), then wrap each as `subagent_event`.
  - Drop deltas.
  - The first message for a new sid emits `subagent` started. Its `name` is the spawn call's `description` and its `kind` is `subagent_type`, both from `session.tools_seen` (`:1594`).
- **Task messages.** Read `task_*` messages by `subtype`/`data`.
  - One whose `tool_use_id` is a known sid adds its `task_id`, tokens, tool uses, last tool and status.
  - One with no sid (a background Bash) emits nothing.
  - A terminal status from either message counts (`TERMINAL_TASK_STATUSES`, `types.py:1189-1191`); `killed` means stopped.
- **Finish.**
  - The parent's result for the spawn id finishes a sid that no task status has finished.
  - At the orchestrator's `turn_finished`, live foreground sids become `stopped`.
  - A closed or replaced client (`_stop_pump`, `set_model`, close, loss) stops every live sid with "session ended".
- **Untouched:** `_read`'s owner, `turn_work` and `turn_full` logic (`:1290-1424`), which still reads the raw messages.
- **Approval-card label (Q7).** The hook records `tool_use_id → agent_id`. The tagged block with that id gives the sid. The ordering is **(unverified)**.
- **Nesting.** Claude Code documents that sub-agents cannot spawn sub-agents (L-C5 checks). A nested sid still gets `parent_sid` and is drawn only in its parent's pane.
- **`stoppable`** needs both a `task_id` and `CLAUDE_STOP_VERIFIED`, a constant flipped only after L-C3 (as `CLAUDE_VERIFIED` is). Otherwise the reason is "Claude Code can stop this sub-agent only by stopping the whole turn."

### D3. Codex provider (gated on SV-0's `generate-ts --experimental` dump)

- **`s.children`** maps `threadId` to `{sid, turn_id, parent_sid, name, kind}`. Entries come from spawn items on our own thread, or from a known child (nesting). The fallback is `thread/started` whose source names our thread. An unknown thread id stays dropped.
- **Routing.** Before the thread and turn filters (`:938-942`), a known child's notification goes through the existing `_notification` and is wrapped.
  - The child's `turn/completed` finishes its sid, never the parent's turn.
  - The child's token usage feeds the sid, **never** `s.accounting` (`:946-951`).
  - The child's deltas are dropped.
- **A known child's server request is never fatal.**
  - What it does is Q2. Recommended: the same `permit` path, with the card naming the sub-agent. The minimum is a non-fatal decline with an ERROR line, as MCP elicitation is handled (`:889-903`).
  - An unknown thread's request is unchanged.
- **If child notifications are not delivered,** the spawn item's `agentsStates` give a coarse status. The pane then says "Codex did not send this agent's activity".
- **`stoppable`** only if L-X3 shows that child `turn/interrupt` stops that child alone. Otherwise the reason is "Codex can stop a child agent only through the orchestrator."

### D4. Daemon, routes, persistence

- **`_record`.**
  - Inner tool label and privacy: an inner `tool_started` gets its label outside the lock, with the same `tool_calls.label` and `hud_browser_act` rewrite (`daemon.py:1373-1382, 1416-1425`).
  - Lifecycle text: `subagent` name, kind and `current_tool` go through `secrets.scrub` and `clean_line(…, 40)`.
  - Logging: both kinds are logged with `turn_id` and published.
  - Inner `args` are logged as parent tool rows are today; the same data is already logged untagged.
  - Unchanged privacy: Claude's `_safe`, the private `terminal_read`/`hud_browser_view` summaries (`claude.py:1624-1630`), and routes that never return `summary` or `args` (T-4, T-9).
- **The main transcript route** (`?tools=1&subagents=1`).
  - It adds `{role: "subagent", sid, name, kind, provider, status, turn_id, at, finished_at?, tokens?, tool_uses?, stoppable, stop_reason?}`.
  - The row goes where the spawn call was, which it absorbs (Claude), or at the `subagent` started row (Codex).
  - `running` only while the live session reports it; otherwise the logged status; otherwise `stopped`.
  - Inner rows never appear in the main transcript.
  - A log with no sub-agent rows is byte-identical (golden).
- **The sub-agent transcript route** (`GET /threads/{id}/subagents/{sid}/transcript`).
  - It is `owner_only` (`projects.py:169-173`).
  - It returns `{subagent, messages}`: inner rows folded by `transcript_tools` and the text rule, with nested summary rows.
  - An unknown sid is 404.
  - MockDaemon enforces `owner_only` too. Control-plane notes: "a gate checked only against a mock is a gate checked against the mock's idea of the browser."
- **Stop** (`POST /threads/{id}/subagents/{sid}/stop`, body `{}`).
  - It is `owner_only`, checked before the body is read.
  - It calls the provider's new optional `stop_subagent(h, sid)`. If that raises `SubagentStopUnsupported(reason)`, the route answers `409 {supported: false, reason}`; for a stale sid, `409 {stale: true}`.
  - It is never on the API listener, session MCP, Discord or the CLI, unlike `/threads/{id}/interrupt` (`daemon.py:2198-2204`).
- **Runner.** A task's result loses sub-agent prose, which is a fix. `_note_tool` (`runner.py:768`) also reads inner `tool_started`, so `last_tool`/`last_file` still move.
- **Discord.** Nothing new is mirrored and no code changes: the new kinds are outside `TURN_KINDS` (`mirror.py:97-100`).
- **Cost.**
  - Claude: `total_cost_usd` already includes sub-agents. Rows show `TaskUsage` tokens and tool uses, no dollars.
  - Codex: child tokens are shown on the row. Whether the parent's totals include them is L-X4; a gap is a ledger follow-up.

### D5. HUD

- **Store.** `state/subagents.ts`, outside React like `state/toolGroups.ts`, keyed `threadId|sid`.
  - Rows subscribe through `useSyncExternalStore`, so a tick redraws one row.
  - Inner entries are kept only for sids shown in a pane, plus a ring of the last 20 heard; panes load the rest.
- **Reducer** (`lib/subagents.ts`, pure).
  - Started inserts `ChatMessage {role: "subagent", sid, turn_id}` at the spawn point.
  - It removes the spawn call from its group by `call_id`, and splits the group as text does (T-2).
  - Fold equals live (property test).
  - `subagent_event` never touches `ops`, the status line or the orb. The orchestrator's running Agent call keeps `RUNNING · Agent` as now.
  - An inner `tool_started` still calls `buffers.noteTouch` (`App.tsx:1099-1102`). The writer is "<name> (sub-agent of <chat>)".
- **The row** (`SubagentRow.tsx`, memoized; `data-testid="subagent-row" data-status`) reads `◇ <name> · <kind> · running · Bash: npm test · 2m 14s · 18.2k tokens`. It has two sibling buttons:
  - **Open:** `aria-label="Open sub-agent <name> in a pane"`, `noPointerFocus`, Enter and Space native.
  - **Stop:** shown when stoppable, `aria-label="Stop sub-agent <name>"`. Otherwise a muted "Can't stop", with the reason as `title`.

  Graphite:
  - Status is always a word: `--amber` running (as `.tc-status.running`), `--green` done (Graphite's one green), `--red` failed, muted stopped.
  - The kind badge uses `--badge-claude` or `--badge-codex`.
  - Type is 11 px mono, as `.tool-group`.
  - Text nodes only, re-cleaned with `toolLabel`.

  The row is inert under a card (WP-B). More than six consecutive rows collapse into "Sub-agents · 8 (5 running)" with `scmWindow` (T-12).
- **The view.**
  - `View` gains `"subagent"`. It is **not** a strip button; it is offered only when `spec.subagent` is set, like `offersVex` (`workspace.ts:48-54`).
  - `PaneSpec.subagent?: {threadId, sid}` is validated in `parsePane` (`:347`). A bad value falls back to the default view.
  - It is stored in `jarvis.hud.workspace`; entries never are.
- **Opening**, in order:
  1. Focus the pane that already shows the sid (one sid per pane).
  2. Else split from the orchestrator's pane (`App.tsx:1770-1795`, `splitVerdict`, `allocate`).
  3. Else reuse the last sub-agent pane.
  4. Else show the notice "No room for another pane: close one or use ⊞".

  It never takes a chat pane. "A thread is open in at most one pane" (`lib/chats.ts:13`) is unchanged.
- **The pane.**
  - Header: name, kind, status, elapsed (ticking only while drawn, as `ToolGroupRow.tsx:10-18`), tokens, Stop, and "Show in chat", which focuses and scrolls to the row.
  - Body: `Markdown` text and `ToolGroupRow` calls, which show labels and outcomes, never results.
  - No input box. It follows the bottom only when the reader is already there (T-7).
- **Reload.** The pane re-fetches. A 404 or a gone thread shows "This sub-agent's record is gone" with Close. A 400 from an older daemon shows "Update Jarvis to see sub-agents".
- **Daemon restart.** The CLI dies with the daemon, so every sid is `stopped`. Rows and panes re-fetch on SSE reconnect.

### D6. Safety

- **Unchanged refusals:**
  - Claude's `agent_id` refusal (`claude.py:888-907`);
  - Codex's child terminal refusal (`codex.py:1097-1107`);
  - H12's `sub_agent` browser rule (`browser/act_rules.py:373, 832`);
  - every gate layer.
- **No model-facing route.** The view gives no model a way to read, start, steer or stop anything.
- **Stop only stops.** It is owner-only, with the same listener, Origin and Host checks as the editor routes. It never answers a card. A pending card for a stopped sub-agent resolves as today's interrupt resolves it (`_drop_waiting`).
- **Sub-agent prose is untrusted data:** existing `Markdown` sanitising, no `innerHTML`, and T-13 cleaning.

### D7. Voice (voice rework V-3, V-8, §3.7, §3.9)

- **Silent by construction.**
  - V-3 reads `text_delta`/`text`. `subagent_event` is neither, so sub-agent prose never reaches `ProseStream`.
  - This also fixes the misattribution described above for speech.
  - V6's tests should pin it.
- **The one line.**
  - `subagent` finished `done` → "<name> finished"; `failed` → "<name> failed".
  - It is queued as `kind: "notice"` in the thread's voice, under V-8 (the thread must be in an open chat pane), the floor and stale-drop rules.
  - `stopped` is silent (Q5).
  - Finishes within 3 s coalesce ("3 sub-agents finished, 1 failed").
  - Names are capped at 40 characters and pass the speakable filter.
- **When:** SV-5, after voice V7.
- **As built (SV-5, 2026-10-16):** `hud/src/lib/speech/notices.ts`; counts only ("Two sub-agents finished, one failed."), no name. See `docs/notes/voice-face.md`, "Sub-agent notices".

### D8. Edge cases

- **Parallel.** Several spawn blocks in one Claude message give consecutive rows. Codex's limit on concurrent children is **(unverified)**.
- **Nested.** A nested sub-agent is drawn only in its parent sub-agent's pane, under the same Stop rule.
- **The orchestrator's turn is interrupted.** Today's interrupt (`daemon.py:1628-1660`) is unchanged.
  - Foreground sub-agents end with the turn and are marked `stopped`.
  - Without SV-6, a background one is marked "stopped (left running in the CLI)", since Jarvis can no longer see it.
- **Steering.** It goes to the orchestrator only. Whether a Claude steer waits for a running Agent call is **(unverified)** (L-C6).
- **Compaction.** The pane reads Jarvis's log, so CLI compaction changes nothing shown. `:1341`'s context-full rule is unchanged.
- **Model switch.** A Claude reconnect stops background sub-agents ("session restarted"). The model chip warns first (Q8).
- **Resume.** A new CLI process means no old sid can be live; history comes from the log.

## Packages and order

| Pkg | What | Needs | Size | Risk |
|---|---|---|---|---|
| SV-0 | Probes, no product code, on the owner's PC with synthetic prompts. **Claude:** one, two parallel and one `run_in_background` agent; `stop_task` on each; interrupt mid-agent; a nesting attempt; a steer during an Agent call. **Codex:** the `generate-ts --experimental` dump (collab items, thread source); a two-child spawn; delivery of child notifications and approvals; child `turn/interrupt`; token totals. Recordings are sanitised into `tests/v2/fixtures/subagents/{claude,codex}/*.jsonl`; findings go to the providers-runtime and protocol notes. | — | M | none |
| SV-1 | Claude tagging and the new kinds, `_record`, both routes, the runner change, `docs/hud-api.md`, design §5.2 | SV-0 (Claude) | M | medium |
| SV-2 | Codex child set, routing, non-fatal child requests (Q2), spawn items | SV-0 (Codex), SV-1 | M–L | high: WP-3 identity filters |
| SV-3 | HUD store, reducer, row, view, `PaneSpec`, mock routes, headless suite | SV-1 | M–L | medium |
| SV-4 | Stop: `stop_subagent`, the owner route, the button, the verified constants | SV-1, SV-3, L-C3/L-X3 | S–M | medium |
| SV-5 | Voice notices | SV-1, voice V7 | S | low |
| SV-6 | Between-turn reader. Claude: a pump drain that publishes sub-agent and task messages and holds main-thread ones for the next `_read`. Codex: a queue drain. | SV-1/2, Q6 | L | high |

- SV-3 can start against SV-1's merged shapes.
- If Codex does not deliver child notifications, SV-2 shrinks to spawn items, coarse status and the non-fatal request.
- **Deploying is the owner's step, never an agent's:**
  - after SV-1, SV-2 and SV-4: `jarvis restart`;
  - after SV-3: `jarvis hud rebuild`, then reload the window.

## Tests (free), and the mutation each must catch

- **`claude_provider_check`** (`FakeClient`, `:125-202`, replaying SV-0 fixtures):
  - Tagged text, thinking, tool use and tool results come out only as `subagent_event`. Must catch: the `parent_tool_use_id` check dropped.
  - Tagged deltas yield nothing.
  - `started` precedes the first event.
  - `killed` maps to stopped. A task with no sid emits nothing.
  - `turn_finished` and close stop live sids.
  - The existing steer, injected-turn and context-full checks pass unchanged.
  - `_safe` and the private summaries apply inside the envelope. Must catch: wrapping before `_safe`.
  - `stoppable` stays false without the constant.
- **Codex suite** (`FakeRpc`, `codex_provider_check.py:304`):
  - A known child is wrapped; an unknown thread is still dropped. Must catch: admitting any `threadId`.
  - A child's `turn/completed` does not end the parent's turn.
  - Child usage never reaches `s.accounting`.
  - A child request is non-fatal.
  - The WP-3 checks still pass.
- **`subagent_routes_check.py`:**
  - The inner label is computed outside the lock, under a blocking fake scrub.
  - `hud_browser_act` is rewritten inside the envelope.
  - The summary row sits at the spawn index and absorbs the call. The golden without the flag is unchanged.
  - Both new routes return 403 on the API listener, with a foreign Origin and with a bad Host.
  - Unknown sids give 404; stop gives its 409s and calls the provider exactly once.
  - No `summary` or `args` key appears.
  - Ledger, `running_cost` and `thread.tokens` are unchanged. Must catch: a sub-agent `usage` row.
  - Discord and the runner's result carry no inner text, while `last_tool` still moves.
- **vitest:**
  - `lib/subagents.test.ts`: live equals fold over 1,000 random sequences with nesting, parallel agents, stops and steers. The spawn call is absorbed once. Must catch: it is also left in its group.
  - `ChatTab.test.ts`: one progress tick renders one row.
  - `workspace.test.ts`: `parsePane` rejects a bad sid, and the view is not in the strip.
- **Headless** (`tests/face/hud_v2_subagents_check.py`): its own `MockDaemon(0)`, `guard_live` and `watch_csp`, with a mock that enforces `owner_only`.
  1. A row appears at the spawn point, with no inner text or call in the log.
  2. Progress updates the row.
  3. Opening splits, a second click focuses, and eight panes reuse a sub-agent pane.
  4. Keys and Ctrl+D behave as in T-11.
  5. Under a card, nothing acts.
  6. Stop calls the route once; an unsupported Stop is absent, with its reason shown.
  7. Reload restores the pane; a restart turns running into stopped; a gone record shows its message.
  8. Markup and bidi characters render as text.
  9. Twelve agents collapse into one bounded group.
  10. 2,000 inner calls cause no long task while typing.

## Live checks (owner's PC)

- **Claude** (installed CLI at or above 2.1.295):
  - L-C1: the stream for one Agent call.
  - L-C2: three parallel rows.
  - L-C3: stopping one foreground agent. Does the turn and its siblings go on?
  - L-C4: an interrupt mid-agent.
  - L-C5: nesting is refused.
  - L-C6: a steer during an Agent call.
  - L-C7: a background agent past the turn's end; is there a stall?
  - L-C8: the Discord reply holds no sub-agent prose.
- **Codex** (0.161, owner chat, AUTO):
  - L-X1: child notifications are delivered.
  - L-X2: a child approval gives a card or a decline, never a dead turn.
  - L-X3: child `turn/interrupt`.
  - L-X4: child against parent token totals.
  - L-X5: WP-3's child terminal refusal still holds.
- **Both:**
  - reload and `jarvis restart` with a pane open;
  - with voice V7, only finished and failed lines are spoken.

## Decisions recorded (owner, 2026-10-15, after SV-0/SV-1/SV-2)

These answer the open questions below where they overlap, and the SV-0 findings
(`2026-10-15-sv0-findings.md`) where those asked for an owner call.

- **Codex per-child Stop (SV-4): offered, with the caveat shown.** Stop ends the
  child's turn; the UI says a command the child is already running may still
  finish, and the orchestrator's model is not told.
- **The main Stop also kills Claude's background sub-agents: keep it**, and the
  UI says so.
- **Show the sub-agent's brief in its pane: yes** (open question 3; collapsed,
  scrubbed, at most 4,000 characters).
- **No interim mitigation for the background-agent stall.** Wait for SV-6, which
  drains the stream between turns and records an injected CLI turn as its own
  system-originated turn.
- **Turns the CLI starts by itself after a background sub-agent finishes are
  neither posted to Discord nor spoken.** Sub-agents are spawned to do a task
  and go away; their completion turns stay quiet (SV-4 voice rule extended).
- **Claude per-child Stop: enable it** on the SV-0 offline evidence for CLI
  2.1.295 (`CLAUDE_STOP_VERIFIED`); no separate live run is required first.
- **Sub-agent thinking and the reviewer's decline reason are not kept** in the
  child record: accepted.
- **Codex child approval cards carry the child's sid** (as Claude's do), so the
  HUD can link a card to its row; added with SV-3.

## SV-6 design: the between-turn reader (2026-10-16)

Built on SV-0 findings 6 and 8–13, the providers-runtime notes "What SV-6 must
do for Claude's injected turns", and the owner's 2026-10-15 decisions above.

**Who reads, when.** Every live Claude or Codex session has exactly one reader
of its stream at any moment:

- during a send, the send (as today);
- between sends, an **idle reader**, started when a turn ends and only when the
  daemon watches the session (`provider.watch(h)`, below). A session nobody
  watches reads only during sends, as before.

**Hand-off.**

- *Claude.* The send, the idle reader and an adopted CLI turn are all
  coroutines on the session's own event loop, and they share one lock-step
  `_Pump`. A turn's coroutine first **parks** the idle reader: it cancels the
  idle task and awaits its end, before it writes its query or reads anything.
  The idle reader's only suspension point that touches the stream is
  `pump.next()`, which is cancel-safe: the pump's outstanding request stays
  for whoever asks next, and an item is never half-taken. Between two awaits
  everything is atomic on one loop, so no message is read twice or lost. The
  turn's own `finally`, on the loop, starts the idle reader again.
- *Codex.* The idle reader is the daemon's watcher thread inside
  `CodexProvider.watch`. One condition variable decides who reads the
  transport: the idle drain takes it per message and gives it up between
  messages; `send` raises "a send is waiting", waits until the drain lets go,
  and then holds it to the turn's end. A child's approval card that the drain
  is waiting on holds the reader, so a send waits for that card (the owner can
  see it; a Stop ends the wait).

**What the idle reader does with each message.**

- *Claude, with a watcher:*
  - a sub-agent's message (`parent_tool_use_id`) or a `task_*` system message
    is translated (the same `_translate` as in a turn) and handed to the
    watcher at once;
  - a main-thread system message (`init`, `status`, …) is read for its side
    effects (the session id) and kept only if something is already held;
  - a CLI turn nobody sent — an untagged stream event or assistant message,
    a replayed user message with a non-human `origin`, or a result with one —
    is **offered**: it and every main-thread message after it are **held** in
    order, and the watcher hears "the CLI started a turn" once;
  - the echo of a late steer, or anything else main-thread, is held for the
    next send, whose `_read` judges it by today's rules (leftover, stopped,
    injected);
  - the end of the stream or an SDK error is held for the next send, which
    reports it exactly as today.

  Held messages live on the session (`session.held`), are read before the
  pump by every reader, and are bounded: past `HELD_LIMIT` a held stream
  event (a delta) is dropped; whole messages, results and `task_*` never are.
  With no watcher, everything is held (today's attribution, with the buffer
  drained).
- *Codex:* a known child's notification goes through the same
  `_child_notification`; a `subAgentActivity` on the session's thread through
  `_announce`; anything else on the session's thread is dropped, as the next
  turn's filters drop it today. A known child's server request (approval,
  `currentTime/read`, MCP elicitation, question) is answered exactly as in a
  turn (`_server_request`). The session's own request between turns is
  refused, not fatal; an unknown thread's stays a fatal identity mismatch. An
  owner Stop on a child (`_child_stop_answered`) and a stale steer's answer
  are read as in a turn.

**The CLI's own turn (Claude only; Codex starts none, SV-0).** The daemon
adopts an offered turn as a turn of the thread:

- `Daemon._begin_cli` (under the daemon lock, only with no turn running and
  `provider.cli_turn_pending(h)` true) mints a `turn_id`, logs and publishes a
  `cli_turn` system line ("A background sub-agent finished, and Claude Code
  carries on."), and starts the ordinary turn worker on
  `provider.adopt(h)` instead of `send`. `adopt` reads the held CLI turn to
  its result: text, tools, gate questions, usage and `turn_finished`, under
  that `turn_id`, through the same `_record`.
- Every record of that turn carries `self_started: true`. The Discord mirror
  posts nothing for such a turn (owner decision); the HUD's speech
  controller ignores its records (owner decision); the task runner does not
  read its text as its own turn's reply. The HUD shows it like any turn.
- While it runs, an owner message queues (Claude refuses a steer into a CLI
  turn), and Stop interrupts it as any turn. When it ends, a further offered
  turn is adopted before the queue drains, in the CLI's own order.
- Usage: its `result` is translated once, by `adopt`, so its tokens and cost
  go to the ledger once, under its own turn, and no longer into the next
  owner turn.
- An offer the daemon does not take in time (an owner send started first)
  stays held, and that send reads it under today's `injected` rules: the
  fallback, never a loss.

**Approvals between turns** go to the session's own `permit`, the thread's
broker asker, exactly as in a turn. Nothing here answers one. The provider's
gate events between turns (not in an adopted turn) are logged with no
`turn_id`, so no reader takes them for a finished turn's.

**Lifecycle.** `close` (close, archive, loss, shutdown) parks the idle reader
before it stops the pump and the loop, and ends the watcher's iterator. A
Claude model switch parks it, drops what the old CLI's stream held, and the
next turn's end starts it on the new client. A resume is a new session with
its own watcher. A watcher thread ends with its session; `_cleanup` joins it,
bounded.

**Not covered.** A message the owner sends in the instant the CLI starts its
own turn may still be read by today's fallback. A CLI turn that overflows the
context is not compacted (the next owner turn is). Codex's 4096-message queue
can still overflow while the owner decides on a child's card.

## Open questions for the owner

1. **Inline rows or a strip under the transcript?** Recommend **inline at the spawn point**, collapsing above six. The row sits beside the words that launched it, and history reads the same.
2. **A Codex child asks for approval. What happens?** Today, if Codex sends it to Jarvis, the orchestrator's turn dies. Recommend **the same owner card and gate layers, naming the sub-agent**. A request from a thread Jarvis doesn't know stays refused.
3. **Show the orchestrator's brief to the sub-agent at the top of its pane?** Recommend **yes**: collapsed, scrubbed, at most 4,000 characters. It is the clearest record of what the sub-agent was asked to do.
4. **Stop: one click or confirm?** Recommend **one click**, like the turn's Stop, with "stopping…" until the CLI confirms.
5. **Speak "<name> stopped"?** Recommend **no**, since you did it. Finishes within 3 s coalesce into one line.
6. **Support sub-agents that outlive the orchestrator's turn (SV-6)?** It is a large, risky change to how Jarvis reads the CLIs between turns. Recommend **deciding after SV-0**. Build it only if L-C7 shows your orchestrator uses background agents, or that they stall.
7. **Label approval cards from a Claude sub-agent ("from sub-agent: <name>")?** Recommend **yes**. It is informational only, and no decision or button changes.
8. **Old history and model switches.** Recommend leaving pre-feature transcripts as they are, still mixed, rather than rewriting logs from the CLI's sub-agent files (SDK `get_subagent_messages`, `_internal/sessions.py:1328`). Also recommend the model chip warn "Switching stops N running sub-agents" before a Claude reconnect.
