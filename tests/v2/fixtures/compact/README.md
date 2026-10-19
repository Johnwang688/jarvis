# Codex compaction recording (WP0)

`codex-0.161.0.jsonl` is one recorded JSON-RPC session of `codex app-server
--stdio` (codex-cli 0.161.0), taken 2026-10-19 for the HUD compact button plan
(`docs/plans/2026-10-19-compact-button-plan.md`, WP0, owner answer CB-3). One
object per line: `{"dir": "send"|"recv", "msg": {...}}`, in wire order (the
messages carry no `jsonrpc` field).

Recording: a standalone driver (not in the repo) started the app-server in an
empty temp cwd with the owner's default Codex login, then sent `initialize`,
`initialized`, `thread/start` (read-only sandbox, network off, approvalPolicy
`never`), two `turn/start` turns ("Reply with the single word OK."), and one
`thread/compact/start {threadId}`. It read until the compaction turn's
`turn/completed`, then shut down. One thread, one compaction, one run.

Scrubbed to `"[redacted]"`: `planType`, credit `balance`, `serverName`,
`installationId`, and the OS username in the `codexHome`/rollout `path`.
Thread/turn/item ids are kept as recorded (random, not secrets).

## Observed answers

- `thread/compact/start` replies `{"id":5,"result":{}}` immediately, before
  any notification for it.
- `turn/started` DOES arrive for the compaction, with its own new turn id
  (`...4cd6-70d0-...`, different from both OK turns), `items: []`.
- Items: `item/started` then `item/completed` of type `contextCompaction`
  (`{"type":"contextCompaction","id":...}`, no other fields; it carries
  `turnId` of the compaction turn). No `thread/compacted` (deprecated
  `ContextCompactedNotification`) notification was sent.
- `thread/tokenUsage/updated` arrives once, after `item/started`. Its `total`
  is unchanged (26604, cumulative), and `last` is `{totalTokens: 4699, others 0}`
  (apparently the post-compaction context size); `modelContextWindow` 258400.
- `turn/completed` for the compaction turn: `status: "completed"`, `error:
  null`, `items: []`, `durationMs` ~1.3 s.
- Other notifications: `thread/status/changed` (`active` before, `idle`
  after the compaction turn), `account/rateLimits/updated` (during the
  compaction turn), and a late `mcpServer/startupStatus/updated` after
  `turn/completed`. Ordinary turns also had `turn/moderationMetadata`,
  `item/agentMessage/delta`, `thread/started`, `remoteControl/status/changed`
  and `account/updated`.
