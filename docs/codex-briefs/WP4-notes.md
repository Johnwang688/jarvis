# WP4 — CodexProvider (app-server)

## Built
- `providers/codex.py` implements the fixed Provider interface; pin 0.153.4.
- Credited copy of the firm's RPC transport, with serialized writes, independent
  pipe drains, bounded queues, overflow refusal and bounded SIGINT/TERM/KILL.
- Private per-thread config/auth link, native tools, workspace-write,
  on-request, auto_review, explicit MCP servers, trusted cwd and installed skills.
  Child environment uses an allowlist; no daemon keys or provider overrides.
- Health checks binary/version/local ChatGPT login without inference. Startup
  also verifies account type, effective config and returned thread settings.
- Persistent start/resume, image data URLs, text/reasoning/tool/plan events,
  reviewer declines, approvals and both requestUserInput spellings. Questions
  in a bundle get individual request ids and separate answers.
- Command/file approvals call the supplied callback on the send caller thread.
  `args.req_id` correlates it with approval events; permissions expansion is
  denied; unknown requests receive an error reply and ERROR event.
- Shared process lock serializes turns and startup while allowing open sessions.
  Per-model durable cooldown begins at 15 minutes, doubles to two hours, clears
  on success. No automatic turn replay: transport loss may follow tool execution.
- Atomic cumulative usage metadata preserves retained/reset resume offsets,
  ignores old-turn replays, refuses decreases/unknown baselines/missing usage;
  dollar cost is always None. Daily admission belongs to the WP9 ledger.
- Manual `tests/spikes/r4_codex_live.py` prints "live; spends quota", checks
  health, runs one shell/file turn in temporary storage and reports events/usage.

## Protocol verification (installed codex-cli 0.153.4)
- Generated experimental TypeScript bindings with `app-server generate-ts`.
  Verified request/response fields, image URLs, accept/decline, permission-denial
  shape, question answer maps, reviewer notifications and turn status values.
- Ran initialize/config-read/thread-start against the actual binary with a
  temporary home and NO login: strict config accepted; workspaceWrite,
  on-request, auto_review and high effort returned; AGENTS.md appeared in
  instructionSources. No turn submitted in this config probe.
- Separate localhost fake HTTP provider captured two synthetic requests with
  NO login and NO inference. `baseInstructions` replaces a ~21 KB native prompt
  with the supplied marker. `developerInstructions` preserves that prompt and
  appends the brief; AGENTS.md was present in both captures. This is why the
  implementation deliberately leaves baseInstructions unset.
- Omitted environments selects native tools; an empty array disables them.
- Real ChatGPT approval routing remains a manual R4 live check. The live smoke
  was not run; only its `--help` path was exercised. No subscription quota used.

## Validation
Command (exit 0):
```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /home/johnw/projects/Jarvis/.venv/bin/python tests/v2/codex_provider_check.py
```
Last lines:
```text
Ran 23 tests in 5.086s

OK
```
Real-pipe FakeRpc/Brain peers cover event data/order, late blocking callbacks
with >1 MiB notifications, approvals, questions, identity errors, health,
resume offsets, serialization, cancellation, transport death/overflow/stalls,
missing accounting and stubborn-process shutdown. `git diff --check` passed.

## Workarounds / proposed interface changes
- STRICT and ASK, explicit allowed_tools and nonempty always_ask are refused:
  on-request/auto_review exposes no universal pre-tool callback. Do not claim
  always-ask enforcement from approval callbacks alone; WP5 needs a native hook
  or a separately designed execution gate. No five-layer policy lives here.
- `answer()` records the first approval answer; the synchronous permission
  callback must return before send can publish resolution. Questions wake
  immediately. Propose a cancellable callback/request context for WP5 so broker
  cancellation releases its own wait; Python cannot cancel arbitrary callbacks.
- Usage is cumulative per session, including prior saved turns. WP9 should take
  deltas. Add completeness/cooldown fields to Usage/health or a status interface;
  currently cooldown metadata is carried by ERROR. Failed/interrupted accounting
  is retained but cannot be reused/resumed as complete; start a fresh thread.
- These are RPC counters. The firm's durable rollout reconciliation for omitted
  compaction tokens is not included; ledger integration must address that gap.
- Fixed provider/model, other work packages, v1 and the trading firm unchanged.
