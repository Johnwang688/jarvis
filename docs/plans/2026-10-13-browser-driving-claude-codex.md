# Browser driving for Claude and Codex chats (BR-9d)

**Status:** original design snapshot, 2026-10-13. The implementation contract is
now recorded in [Browser notes](../notes/browser.md#native-parent-browser-driving-br9d)
and [measured native acceptance](2026-10-13-browser-native-offline-cli.md).
The original observations and questions below remain historical evidence.

It **depends on** `2026-10-13-browser-driving-release.md`, which makes the switch real. It builds on:
- BR-6: `hud_browser_view` over the session MCP, PR #123;
- WP-2: Claude session MCP, PR #117;
- WP-3: Codex session MCP, PR #118;
- amendment 1 §B.7, §B.10 and §I (L6/L7).

## For the owner

- **What changes.** A Claude or Codex chat can drive a Browser pane the same way Jarvis's fast path does: on a turn you typed at the HUD, in a visible Remote pane, through the same gate.
- **One gate.** The same switches, refusals, classifier, private cards, budgets and takeover apply. There is no second gate and no new kind of card.
- **Who can't.** Sub-agents of either CLI are refused, and tasks never get the tool.
- **Where the text goes.**
  - Each judged action's details still go to the Browser classifier on OpenRouter (the `browser_review` tier, Haiku 5.5 by default).
  - The action's result, including the page's refs, goes to Anthropic or OpenAI as part of that chat.
- **What I need from you.** Five answers, at the end. The main ones:
  - Do chats share the fast path's 40-judged-actions-per-turn budget?
  - Does one switch govern all agents?

## Decisions recorded

- **DD-0. Owner, 2026-10-13: allow Claude and Codex chats to drive (BR-9d).**
- **DD-1. One choke point.**
  - Chats reach `BrowserDrive.act` only through `hud_browser_act` → `act_for_tool`, run in the daemon by the session bridge's single `tools.dispatch` (`jarvis/v2/bridge.py`).
  - Every BR-9b check runs unchanged: H1–H12, ASK floors, private cards, the 5 s owner quiet, budgets, post-input reconciliation, uploads, held downloads, no local-port lease.
- **DD-2. Parent-only, desk-only**, exactly as in BR-6. Claude's hook refuses an `agent_id` call. Codex's native identity check refuses children, replays and stale turns.
- **DD-3. Amendment 1 §B.7 stands.** An `owner_gated` session flag and a `jarvis-gated` permit layer; a Claude hook explicit allow under both profiles; Codex `approval_mode = "approve"` with truthful annotations; an always-ask rule naming the tool still asks first.

## What the code did at the original review (historical snapshot)

- **The catalog.** `session_tools.SESSION_TOOLS` offers `hud_browser_view` as `read_only`, `foreground_only` and `desk_only` (`session_tools.py:58-60`). `hud_browser_act` is not offered.
- **Turn binding is fast-only.**
  - `drive_state.current_turn()` requires `caller["provider"] == "fast"` (`drive_state.py:53-61`).
  - A `DriveTurn` is bound only in `fastpath.py:659-670`.
  - So `publish_look` publishes no refs for a chat look, and any chat act would refuse.
- **"fast" is hard-coded.** `drive._audit` and `drive._card` write `provider="fast"` (`drive.py:202-203`, `219-223`). `host.drive_notice` labels the banner driver `"fast"` (`host.py:890`).
- **The permit.**
  - A delegated tool that is neither dangerous nor read-only passes layer 2 (always-ask), then ALLOWs at `jarvis-session-delegate` (`permissions.py:910-985`).
  - Neither `owner_gated` nor `jarvis-gated` exists yet.
- **The Claude hook.**
  - It denies a `foreground_only` session tool when `agent_id` is present, before `permit`.
  - On a delegated ALLOW it returns an explicit allow under both profiles (`providers/claude.py:865-915`).
- **Codex.**
  - `bridge.server_entry` gives every offered tool `approval_mode = "approve"`, `enabled_tools`, and `tool_timeout_sec = 900` (`bridge.py:75-82`).
  - The bridge preflights the session permit for Codex's nondangerous calls.
  - `foreground_only` entries must pass `admit_foreground_mcp`, which checks the exact live handle, turn and one-shot call id with no child marker (`providers/codex.py:1096-1122`).
  - Annotations for a non-read-only entry are only `{readOnlyHint: false}` (`bridge._annotations`).
- **Timeouts.**
  - The relay's `CALL_TIMEOUT_S` is 900 (`mcp_session.py:41`).
  - WP-3's native synthetic gate passed a **65-second** owner approval wait on Codex 0.161 (`docs/notes/control-plane.md`, the WP-3 section).
  - No live Claude long-wait figure is recorded. WP-2's native L1/L2/L5 probes are still a release gate.
- **Events.**
  - `Daemon._record` makes `hud_browser_act` private only under that exact name (`daemon.py:1416-1424`).
  - Claude reports `mcp__jarvis__hud_browser_act`. Its private-summary lists name only `terminal_read` and `hud_browser_view` (`claude.py:1627`, `1646`).
  - Codex's lists are at `codex.py:1079`, `1151` and `1183`.
- **Cards.**
  - The HUD already accepts the private delegation card `{kind: "delegation", tool: "hud_browser_act"}` with empty args and null command (`hud/src/lib/browserapproval.ts:40-48`, BR9c notes).
  - No daemon code emits that card yet.
- **Owner words.** No owner message text is kept on `_Session`. `session.desk.begin(brief, message, turn_id)` sees it at turn start (`daemon.py:1192-1193`).

## Design

**1. The catalog.** Add a new frozen flag `owner_gated`:

```
SessionTool("hud_browser_act", providers=CHAT, read_only=False,
            foreground_only=True, desk_only=True, owner_gated=True)
```

- `register` refuses `owner_gated` unless the entry is foreground-only, desk-only, not read-only, and its registry tool is not `dangerous`.
- It is always listed, like the other desk tools (D-4). It refuses cleanly while a switch is off, because Codex's enabled tools are fixed at start or resume (see Q3).
- Codex annotations become `readOnlyHint: false`, `destructiveHint: true` and `openWorldHint: true`.

**2. The permit layer.** Add `L_JARVIS_GATED = "jarvis-gated"`.

- It sits after layer 2 and before the generic delegate branch. It answers ALLOW with "the Browser gate judges each action", which is not a human decision.
- Strict answers DENY.
- **An always-ask rule** naming `hud_browser_act` or `mcp__jarvis__hud_browser_act` asks at layer 2 with **the private delegation card**:
  - `hud_only=True`, `allowlistable=False`, `max_wait_s=120`;
  - `browser={kind: "delegation", tool: "hud_browser_act"}`;
  - empty args and no command in the owner projection.
- That card lets the call reach the Browser gate and authorizes no page action. The gate may then raise its own action card. Both are said plainly in the HUD API notes.
- Claude asks once, in the hook. Codex asks once, in the bridge preflight. The bridge must not preflight Claude again.

**3. The turn scope.**

- At chat turn start (`daemon.py:1192`), the daemon creates a memory-only `DriveTurn` on the session. It holds:
  - the thread;
  - the turn id;
  - the provider (a new field);
  - strict, meaning profile STRICT;
  - `project_root = brief.cwd`;
  - `owner_words`: the desk message's text, capped at 500 characters, and empty off the desk.
- `retire_turn` runs wherever the turn ends: `desk.end()`, a worker exit, close, retire or lost.
- `bridge.call`'s `run()` enters `turn_scope(drive_turn)` for `hud_browser_view` and `hud_browser_act`, only when the bridge's captured `turn_id` equals the DriveTurn's.
- `current_turn()` accepts `fast`, `claude` or `codex`, and requires the caller's provider to equal the DriveTurn's provider.
- Refs therefore flow from a chat's own guarded Remote look to that same chat turn, and nowhere else.
- The "eight steps, say go on" ending stays fast-only, because a chat CLI has no such budget.

**4. Provider identity.** `_audit`, `_card` and `drive_notice` take the provider from the turn. The banner then reads "Driven by the chat in pane N (Claude)". `stream.agent_driver` already carries a provider.

**5. Stopping.**

- **The owner's Stop on the chat** sets `session.cancelled`. The bridge's `current()` then becomes false, and so does `runtime.should_stop`.
- **The guard stops before the next CDP event.** An event already dispatched settles. The result is `Stopped:` with a truthful partial count. A pending card is withdrawn through `approvals.ask(cancelled=…)`.
- **A steer:**
  - an accepted desk steer keeps the turn;
  - a non-desk steer (Discord, the DM) takes the turn off the desk under the existing carry rule, and the guard's `desk()` check stops the action;
  - a successor turn never inherits refs, leases or a DriveTurn.
- **Codex:** `admit_foreground_mcp`'s predicate also fails on native cancel or turn change.
- **A CLI that abandons a call** (an interrupt, or the relay dying) without the daemon turn ending is not detected today. Recommend binding `should_stop` for this tool to the relay request's connection as well, if the handler can observe it. This is unverified (see Q5).

**6. Budgets and concurrency.**

- One shared `DriveState`.
- One driver per tab across fast, Claude and Codex (H9).
- 200 actions per tab per hour, across all drivers.
- 40 judged actions per (thread, turn) (see Q1).
- Two parallel MCP calls in one turn on one tab: the second refuses at reservation.

**7. Timeouts.** A Browser card waits at most 120 s, plus the classifier's 8 s and the execution timeout.

- **Codex:** 900 s for the tool and the relay. Live check LB-4 must show about a 115 s card wait (amendment L6). WP-3's 65 s figure came from the dangerous-tool card.
- **Claude:** the CLI's MCP call timeout under a long wait is unmeasured here. If LB-4 fails, set the installed CLI's documented MCP timeout in the session's private settings; do not shorten the card.

**8. Event privacy.**

- Canonicalize `mcp__jarvis__hud_browser_act` and Codex's `(jarvis, hud_browser_act)` in `Daemon._record`: label "Browser action", args `{}`, summary only `acted`, `refused`, `asked` or `stopped`.
- Add the tool to Claude's `_tool_summary` and private set, and to Codex's three sites. Interrupted and unmatched completions use the fixed words (the BR6 pattern).
- The tool result (outcome line plus fenced refs) still reaches the model and its provider's transcript (amendment §B.12).

**9. Unchanged.**
- Tasks, workers and reviewers get no session server.
- External MCP stays deny-all.
- Discord, schedules and the escape hatch never drive.
- Codex remains AUTO-only.

## Packages and order

1. **BD-a, daemon:**
   - the flag and validation;
   - the `jarvis-gated` layer and the private delegation card;
   - DriveTurn creation and retirement;
   - the bridge scope and provider fields;
   - event canonicalization.

   SELF_PROTECTED files (`drive.py`, `hud_browser_act.py`, `drive_state.py`) are owner-applied.
2. **BD-b, providers:** Claude summaries; Codex annotations, summaries and config readback.
3. **BD-c, docs:**
   - the control-plane notes;
   - the browser notes (a BR9d section);
   - `hud-api.md`;
   - one CLAUDE.md line in Working on v2: "chats drive through the same gate".
4. **Order:** after plan 1 merges and its live validation passes. Then independent review with mutation evidence, the memory-capped sweep, live checks LB-1 to LB-6, and merge.

## Free tests

They extend `tests/v2/browser_mcp_check.py`, `terminal_read_mcp_check.py`, `codex_session_mcp_check.py`, `mcp_session_check.py` and the `browser_drive_*` suites. Each uses fake providers, ephemeral ports and temporary HOME, and one real headless Chrome where needed.

| Check | Mutation it must catch |
|---|---|
| Offered to both providers with foreground, desk and owner_gated set, not read-only; `register` refuses inconsistent flags | entry marked read_only |
| Permit: `jarvis-gated` ALLOW; strict DENY; layer recorded in `decisions.jsonl` | falls through to the generic delegate |
| An always-ask rule (either name) raises exactly one private delegation card: no Always; not on `/events`, `GET /approvals` or Discord; a deny means no browser IO | public card, or a double card on Claude |
| Claude hook: `agent_id` denied without `permit`; explicit allow under ASK and AUTO | `{}` under AUTO |
| Codex: child marker, replayed call id, stale turn or missing metadata refuse before dispatch; the fixture's event log stays empty | `foreground_only` dropped |
| Refs from a chat look work in the same turn only; not in the next turn, another thread or another provider | provider check removed; turn not retired |
| Chat Stop mid-`type` gives a partial report and no further events; an off-desk steer stops it | `should_stop` unbound in the bridge |
| Card, audit and banner carry the real provider | hard-coded `fast` |
| No args, URL or typed text in the bus, `events.jsonl` or the log under either tool name | prefixed name not canonicalized |
| A fast-path driver holding a tab refuses a Claude act on it | per-provider state |
| A 115 s card wait (fake clock) completes over the real relay; Codex config `tool_timeout_sec ≥ 180` | timeout below the card |
| Switch off → refused with no browser IO; the 41st judged act is refused | enabled bypassed for chats |

## Live native checks (by hand, real CLIs, real Chrome, fixture pages)

Fake side effects only; no account actions.

- **LB-1, Claude.** In a HUD chat under ASK and under AUTO: look, then an ordinary act. Expect no CLI prompt and no classifier refusal; the banner says Claude.
- **LB-2, Claude.** A sub-agent's act is refused in the hook.
- **LB-3, Codex.**
  - `--strict-config` accepts the annotations with `approve`.
  - No elicitation or `requestUserInput` (amendment L7).
  - The parent acts; a native child is refused.
- **LB-4, both.** A sensitive act raises the private card. Deny, Authorize and about a 115 s wait all complete without the CLI timing out (amendment L6).
- **LB-5, both.**
  - Chat Stop mid-`type`.
  - A Discord steer takes the turn off the desk.
  - The next turn's act needs a fresh look.
  - The owner's takeover, Stop and Resume in the pane.
- **LB-6, both.** Resume with a new token: the old token is refused and no drive state carries over.

## Open questions for the owner

1. **Do chat turns get the fast path's 40 judged actions per turn?** Recommend **yes** to start. Claude and Codex turns can be long, so revisit with the action log if 40 proves short.
2. **When you steer a turn mid-way, should the classifier also see your steer?** Recommend **yes**: add your latest desk steer to the original message, 500 characters in all. Never add off-desk text.
3. **List the tool to chats even while driving is off?** Recommend **yes**. Codex's tool list is fixed when a session starts, and the tool refuses cleanly while off.
4. **One switch for all agents, or a separate "Claude and Codex may drive"?** Recommend **one switch**. You allowed both, and each pane already has its own switch.
5. **If the CLI abandons a call, should the daemon stop the action?** Recommend **yes**, by tying the action's stop check to the relay connection, provided the handler can observe it. Otherwise the action finishes or stops on its own guards, and the CLI discards the result.
