# WP3 — ClaudeProvider over the Claude Agent SDK

Work package 3 of the Jarvis v2 redesign, implemented in a git worktree on
branch `jarvis/wp3-claude-provider`. Claude leads; this brief is your entire
scope. Read first, in this order:

1. `docs/jarvis-v2-design.md` — §2 D5/D6/D7, §5.1–5.3, §6 (esp. 6.1 and
   the R2 note: **the `PreToolUse` hook is the gate; `can_use_tool` is never
   consulted under `auto`**), §7, §15 R1 (answered), R2 (answered), R3
   (yours to answer), R8.
2. `jarvis/v2/provider.py`, `jarvis/v2/model.py` — **fixed**. Propose
   changes in your notes; do not edit.
3. `jarvis/v2/providers/codex.py` and `fastpath.py` (merged) — the two
   sibling providers; match their event conventions exactly so a surface
   cannot tell which provider produced an event. Read
   `docs/codex-briefs/WP4-notes.md` and `WP2-notes.md`.
4. `tests/spikes/r1_sdk_login.py` — the spike that answered R1/R2; its
   findings are the facts this package rests on. The SDK is
   `claude-agent-sdk` 0.2.153, installed in the venv; read its
   `ClaudeAgentOptions` fields, `ClaudeSDKClient`, `HookMatcher`, the
   message types (`AssistantMessage`, `UserMessage`, `ResultMessage`,
   `SystemMessage`, `StreamEvent`) and the hook input/output types from the
   installed source, not from memory.
5. `CLAUDE.md` *Safety design* — the never-print-secrets rule applies to
   the SDK's stderr and to every error string.

## Deliverable: `jarvis/v2/providers/claude.py` — `class ClaudeProvider`

`name = ProviderName.CLAUDE`, pin `CLAUDE_PIN = "2.1.233"` (major.minor
match is enough; record the exact version in health's reason).

- `health()`: `claude` on PATH; `claude --version` matches the pin; a login
  exists — read `~/.claude/.credentials.json` **only for its expiry field**
  (never log or return its contents), report `unavailable: login expired`
  when past, `unknown` when the field is absent. Never spends tokens.
- `start(thread, brief, permit)`: a `ClaudeSDKClient` with options built
  from the brief: `cwd=brief.cwd`; `model=brief.model`; `effort=brief.effort`
  if the SDK field accepts it; `system_prompt` as the **preset**
  `{"type": "preset", "preset": "claude_code", "append": brief.system_append}`
  so Claude Code's own prompt, CLAUDE.md and skills keep loading;
  `setting_sources=["user", "project"]`; `mcp_servers=brief.mcp_servers`
  with `strict_mcp_config=True`; `max_turns=brief.max_turns`;
  `include_partial_messages=True` (deltas); **never `allowed_tools`** for
  anything gated (R1 finding: it bypasses every callback). Permission mode
  by profile: `AUTO` → `"auto"` + the hook below; `ASK` → `"default"` with
  `can_use_tool` calling `permit`; `STRICT` → `BriefRefused`. Connect,
  capture the session id from the init `SystemMessage`, store it in
  `provider_session_id`. `resume`: same, with `resume=thread.provider_session_id`.
- **The `PreToolUse` hook** (matcher `None`, every tool): calls
  `permit(tool_name, tool_input, brief)`. `DENY` → return
  `{"hookSpecificOutput": {"hookEventName": "PreToolUse",
  "permissionDecision": "deny", "permissionDecisionReason": <reason>}}` so
  the model sees why. `ALLOW` → return `{}` and let auto mode decide
  (an explicit owner yes for an always-ask tool still meets the classifier;
  if the classifier then declines, that is the §6.1 path below). The
  callback may block for minutes: verify what timeout the SDK applies to
  hooks and set it to at least 600 s; record what you found.
- **`reviewer_declined`** (§6.1): detect the classifier's denial in the
  tool result (the text begins "Permission for this action was denied by the
  Claude Code auto mode classifier" — confirm against the SDK/CLI) and
  yield `REVIEWER_DECLINED{tool, args, command, reason}` in addition to the
  `TOOL_FINISHED{ok: False}`.
- `send(h, message)`: `client.query(...)` with text and images as content
  blocks, then translate `receive_response()` into §5.2 events:
  `StreamEvent` text deltas → `TEXT_DELTA`; `AssistantMessage` blocks →
  `TEXT` / `THINKING` / `TOOL_STARTED{call_id=tool_use id, name, args}`;
  `UserMessage` tool results → `TOOL_FINISHED{call_id, name, ok, summary}`
  (`ok` false on `is_error` and on a declined result); `ResultMessage` →
  `USAGE{input, output, cached, cost_usd=total_cost_usd,
  provider_reported=usage}` then `TURN_FINISHED{stop}` with `stop` mapped
  from `is_error` / `max_turns` reached / interrupted. Run the async client
  on a private event loop thread per handle and yield synchronously off a
  queue (the fastpath pattern); the generator must never hang; an SDK
  exception → `ERROR{fatal: True}` then stop.
- `interrupt`: `client.interrupt()`. `answer`: resolves a pending
  `can_use_tool` decision (ASK profile) by `req_id`; the hook path never
  needs it. `usage`: summed from `ResultMessage`s since start. `close`:
  disconnect, idempotent.
- `cost_usd` from the SDK is an **equivalent** figure on a subscription
  (R1); pass it through unchanged and say so in the docstring — the ledger
  decides what it means.

## Tests: `tests/v2/claude_provider_check.py`

Free: no `claude` process. Inject a fake client through a module-level
factory (`_client_factory`) the test replaces, yielding scripted SDK
message objects (construct the real dataclasses from the installed SDK).
Cover: health on missing binary / wrong version / expired / absent-expiry
login (patch `subprocess` and a temp credentials file that is **not** the
owner's); options built from each profile (preset prompt with append, no
`allowed_tools`, hook installed under AUTO, `can_use_tool` under ASK,
STRICT refused, MCP servers strict); the exact event sequence for a
scripted turn with deltas, a tool call, a tool result, a result message;
the hook returning deny-with-reason on `DENY` and `{}` on `ALLOW`, calling
`permit` with the tool name and input; a declined-by-classifier result
producing `REVIEWER_DECLINED` + `TOOL_FINISHED{ok: False}`; resume passing
the session id; interrupt → `TURN_FINISHED{stop: "interrupted"}`; a raising
client → `ERROR(fatal)` and a terminating generator; two handles isolated;
`usage()` sums; no credential value ever appears in any event, reason or
exception string (grep the captured output for the fake token).

Also `tests/spikes/r3_claude_live.py` (manual, prints "live; spends
quota"): health, start in a temp dir under AUTO, one turn that runs a
command and writes a file, prints the event kinds seen, whether the hook
fired and for what, the usage — and **answers R3**: repeat the turn with the
SDK's `sandbox` option enabled and report whether the command ran inside
the sandbox on this WSL2 machine (the design's fallback depends on it).

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /home/johnw/projects/Jarvis/.venv/bin/python tests/v2/claude_provider_check.py`
Then `tests/v2/fastpath_check.py` and `tests/v2/codex_provider_check.py`
(they must not move).

## Rules

Stay inside `jarvis/v2/providers/claude.py`, `tests/v2/claude_provider_check.py`,
`tests/spikes/r3_claude_live.py`, and `docs/codex-briefs/WP3-notes.md`. No
other edits, no new dependencies. When green, commit on your branch with a
message starting `v2 WP3: ClaudeProvider` ending with
`Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`. Do not push or
merge. Finish with `WP3-notes.md` (under 70 lines): what you built, SDK
facts you verified from source (hook timeout, effort field, sandbox
option, stream event shapes), test commands and last lines, workarounds,
proposed interface changes. Report the branch name, worktree path and notes
path as your final message.
