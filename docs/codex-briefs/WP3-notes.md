# WP3 — ClaudeProvider (Claude Agent SDK)

## Built
`jarvis/v2/providers/claude.py`; `CLAUDE_PIN = "2.1.233"` on major.minor.
- Each handle owns a private asyncio loop on its own thread; `send()` runs the
  turn as a coroutine pushing `Event`s into a queue the generator drains, so
  `TEXT_DELTA` arrives during the model call (the fastpath pattern). Its
  `finally` always posts the sentinel; an SDK raise is `ERROR{fatal}` then stop;
  an abandoned iterator interrupts rather than leaving a model editing.
- The `PreToolUse` hook (matcher `None`, 660s) is the gate. Under AUTO an ALLOW
  returns `{}` so the classifier still runs; under ASK (`"default"`) it is an
  explicit allow since §6.2 skips layer 3 (`can_use_tool` backs it up); a DENY
  carries a reason the model reads.
- Every `permit` call is announced `APPROVAL_REQUESTED{req_id, tool, args,
  command}` and closed `APPROVAL_RESOLVED` — Codex's convention, so no surface
  can tell the providers apart; `answer()` races the callback and wins.
- A tool result starting with the classifier template yields both
  `REVIEWER_DECLINED{tool, args, command, reason}` and `TOOL_FINISHED{ok:
  False}`; the whole command travels with it, never a summary.
- Every string out goes through `_safe` (`secrets.scrub` + token/JWT shapes);
  the bundle is read for one integer, stderr only enriches a fatal error.
  `usage()` sums `ResultMessage`s; `cost_usd` is an *equivalent* (R1).

## SDK facts verified from installed source / binary, not memory
- `HookMatcher.timeout` is **seconds, default 60** (`types.py`); the SDK awaits
  the callback with no timeout of its own, so that is the whole budget. **R2's
  sub-question: a hook may block for minutes, no deny-and-requeue needed.**
- `options.effort` exists (low|medium|high|xhigh|max) → `--effort`; off-ladder
  is `BriefRefused`. `permission_mode` includes `"auto"` (docstring omits it,
  the `Literal` has it). `allowed_tools` auto-approves *before* callbacks so it
  stays `[]`; `Brief.allowed_tools` maps to `tools` (what exists). `sandbox` is
  merged into `--settings` by the transport.
- `StreamEvent.event` is the raw API event (`content_block_delta` /
  `delta.text_delta`); tool results are `ToolResultBlock` in a `UserMessage`;
  `ResultMessage` has `subtype`, `is_error`, `terminal_reason`
  (`aborted_streaming`/`aborted_tools`/`max_turns`), `total_cost_usd`, `usage`.
- **No `system/init` arrives at connect** (probed live on 2.1.273, no prompt, no
  tokens) and `get_server_info()` has no session id, so `start()` mints a UUID
  and passes `session_id=`; an id the CLI reports later overrides it. `resume`
  passes `resume=` alone. Denial template: `"…denied by the Claude Code auto
  mode classifier. Reason: <…>. If you…"`.

## Validation
```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python tests/v2/claude_provider_check.py
all claude-provider checks passed          # 109 checks, exit 0, no claude process
tests/v2/fastpath_check.py       -> all fast-path checks passed    (exit 0)
tests/v2/codex_provider_check.py -> Ran 23 tests in 5.673s / OK    (exit 0)
```
Verified to bite: three mutations (explicit allow under auto, decline detection
removed, `_safe` made identity) each fail their own checks. `r3_claude_live.py`
is manual, prints "live; spends quota", and answers R3 by *difference*: one turn
writes inside and outside the workspace with the sandbox off then on, because a
flag that changes nothing is the failure mode. **Not run; R3 stays open.**

## Workarounds
- `permit` runs on a **daemon** thread, never `asyncio.to_thread`: executor
  threads are joined at exit, so a ten-minute approval would block shutdown for
  ten minutes — the free suite hung on exactly that before the fix.
- health reads `refreshTokenExpiresAt` as the login's expiry when present, since
  `expiresAt` is the hourly access token and would read "expired" most of every
  hour. No expiry field → *unknown*, not refused.

## Proposed interface changes (nothing fixed was edited)
- `SessionHandle.provider_session_id` is **provisional** until the first turn;
  persist it at `start()` but re-read after. Add `Brief.sandbox` once R3 answers.
- `Brief.allowed_tools` is ambiguous between "tools that exist" and "tools that
  run unasked": two SDK options with opposite safety meaning. Rename to `tools`.
- `PermissionCallback` cannot be cancelled (WP4 said the same): `answer()` frees
  the turn, not the thread. And `cost_usd` is an equivalent, not spend.
