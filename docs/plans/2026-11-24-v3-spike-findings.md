# Jarvis v3: Phase-0 spike findings (S3, S5)

Findings for the PR map's Track S, recorded 2026-11-24. The owner chose to run
S3 and S5 against the running T3 Code server instead of a throwaway one
(2026-11-24), so no CLI login was needed. Nothing in T3's settings was changed.
Test threads ran in a scratch project and were archived afterwards.

Probe script: `scripts/spikes/v3/s3_drive_t3.py`. Fixtures (shapes and fake
values only): `tests/v3/fixtures/t3/thread_cli_shapes.json`,
`tests/v3/fixtures/hooks/claude_stop.json`,
`tests/v3/fixtures/hooks/codex_turn_complete.json`.

## S3: driving T3 from the daemon

**It works, for both providers, with the documented `t3 thread` CLI.**

| What | Command | Result |
|---|---|---|
| Start and wait | `t3 thread start --json --wait --provider <id> --model <slug> <project> <message>` | `{threadId, runId, status, reply, error}`; about 4 s for a one-word reply |
| Follow-up and wait | `t3 thread send --json --wait <thread> <message>` | same shape; about 3 s |
| Follow-up, no wait | `t3 thread send --json <thread> <message>` | `{threadId, runId, messageId, delivery, status, link}` in about 0.6 s |
| Read state | `t3 thread show --json --messages N <thread>` | `{thread: {...}, messages: [{role, runId, text}]}` in about 0.6 s |
| List | `t3 thread list --json --project <project>` | a list of thread rows, newest first |

- **Exit codes:** 0 finished, 1 failed or bad arguments (text on stderr, no
  JSON), **2 waiting for an answer** (`status: "waiting"`, `reply: null`).
- **"Turn ended" by polling:** `thread show` reports `thread.status`
  (`running` → `completed`); one poll costs about 0.6 s, so a 2–5 s poll
  interval is cheap. `thread.pendingRequestCount` and `status: "waiting"`
  give "waiting on you".
- **Choosing where and how a thread runs:** the project is a project id or a
  path inside a project (`t3 project add <path>` registers one). The provider
  and model are `--provider` with `--model`; **a project with no default model
  refuses a start that names neither.** `--worktree <ref>` and `--branch` run
  it in a new git worktree. The permission mode is not a CLI flag: a thread
  takes the server's configured runtime mode.
- **The binary is not on PATH.** It lives under T3's own runtime folder,
  versioned by a content hash, so T1 must resolve it at call time and not pin
  a path.
- **Not probed:** the `/mcp` event stream. It needs an access grant added to
  the server, which is a settings change. Polling is the recommended signal
  for T1; the event stream stays a later optimisation. What a T3 restart does
  to the in-memory event buffer was therefore not measured.

**Recommendation for T1:** a thin client over the CLI: `start`/`send` without
`--wait`, then poll `show`. Treat exit 2 and `status: "waiting"` as "needs the
owner". Keep the fixture shapes as the contract and fail closed on a shape it
does not recognise.

## S5: do T3-started sessions load Jarvis's additions

**Turn-end hooks fire for both providers under T3.**

- **Claude:** a `Stop` hook in the user's Claude settings ran at the end of a
  T3-started turn. T3 starts Claude with its own extra `--settings` value,
  which carried no hooks and did not replace the user's settings. The payload
  (stdin, JSON) has `session_id`, `transcript_path`, `cwd`,
  `hook_event_name: "Stop"`, `stop_hook_active`, `permission_mode`,
  `last_assistant_message`, and a few fields L1 does not need.
- **Codex:** the `notify` program in the user's Codex config ran at the end of
  a T3-started turn. The payload is the **last argv entry** (JSON), with
  `type: "agent-turn-complete"`, `thread-id`, `turn-id`, `cwd`, `client`,
  `input-messages` and `last-assistant-message`. **It has no transcript path:**
  L1 must find the session file from `thread-id` under Codex's sessions
  folder.
- **Both payloads carry the last assistant message** (and Codex the input
  messages). L1's hook must not copy them into the review queue: contract 1.5
  holds references only.
- **Every Codex run calls `notify`,** headless `codex exec` jobs included, so
  L1's recursion guard (`JARVIS_JOB`) matters for Jarvis's own Codex jobs too.

**MCP servers:** T3 starts Claude with `--allowedTools=mcp__t3-code__*` but
without a strict MCP configuration, and a T3-started Claude session listed
tools from servers other than T3's own. So user-scope MCP servers are loaded
on the Claude side. Whether the `jarvis-brain` server is listed, for Claude and
for Codex, and whether its tools ask for permission, is **still open**: it is
checked right after the owner runs `jarvis brain install`.

## What this unblocks

- **L1** (turn-end hooks → review queue): the payload fixtures exist.
- **T1** (T3 client): the commands, shapes and exit codes exist.
- **OG1** still needs S1 (display), S4 (headless service) and S6 (parity).
