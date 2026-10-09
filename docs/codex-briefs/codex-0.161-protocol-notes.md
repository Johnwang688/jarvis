# Codex app-server protocol: 0.153.4 → 0.161.0 (2026-10-08)

Why this exists: `CodexProvider` was pinned to exactly `codex-cli 0.153.4`. The
owner's CLI (`~/.local/bin/codex` → `~/.codex/packages/standalone/current`)
auto-updated to 0.161.0, and every Codex send failed with "codex 0.161.0,
pinned 0.153.4". The pin is now a floor (`jarvis/v2/providers/codex_cli.py`:
`CODEX_MIN = "0.153.4"`, `CODEX_VERIFIED = "0.161.0"`). This file is the
evidence that 0.161.0 speaks the protocol the adapter uses.

## How it was checked

Both release binaries were still on disk under
`~/.codex/packages/standalone/releases/`, so the diff is generated, not
remembered. For each version, with a scratch `CODEX_HOME` (no login, no
network, no turn):

```sh
codex app-server generate-ts --experimental --out <dir>
codex app-server generate-json-schema --experimental --out <dir>
```

Then every method, notification, server request and field that
`providers/codex.py` and `codex_rpc.py` send or read was rendered from
`codex_app_server_protocol.v2.schemas.json` with `$ref`s expanded and diffed
between the two versions. `codex app-server --help` still lists `--stdio` and
`--strict-config`; `codex login status` still prints "Logged in using
ChatGPT" (string present in the binary).

## Method sets

| | 0.153.4 | 0.161.0 | change |
|---|---|---|---|
| server requests | 11 | 11 | **none** |
| server notifications | 81 | 84 | + `account/gatewayOAuth/changed`, `thread/attachment/updated`, `thread/prediction/updated` |
| client requests | 155 | 169 | − `thread/rollback`; + gateway OAuth, attachments, prediction, `memory/status`, `rollout/compress`, `userVerification/*`, Bedrock GovCloud; `account/rateLimits/read` gained optional params |

The 11 server requests in both: `item/commandExecution/requestApproval`,
`item/fileChange/requestApproval`, `item/permissions/requestApproval`,
`item/tool/requestUserInput`, `item/tool/call`, `mcpServer/elicitation/request`,
`account/chatgptAuthTokens/refresh`, `attestation/generate`, `currentTime/read`,
and the legacy `applyPatchApproval` / `execCommandApproval`.

## What we use, field by field

**Unchanged** (rendered identical): `CommandExecutionRequestApprovalParams` and
`…Response` (decision enum still includes `accept` / `decline`),
`FileChangeRequestApprovalParams` and `…Response`,
`PermissionsRequestApprovalResponse`, `ToolRequestUserInputParams` and
`…Response`, `ThreadTokenUsageUpdatedNotification` / `TokenUsageBreakdown`,
`TurnPlanUpdatedNotification`, `AgentMessageDeltaNotification`,
`FileChangePatchUpdatedNotification`, both reasoning delta notifications,
`ModelReroutedNotification`, `InitializeResponse`, `TurnInterruptParams`,
`ConfigReadParams`, `ConfigReadResponse` (the effective-config keys we compare),
`GetAccountParams`, `Turn`, `TurnStatus`, `ServerRequestResolvedNotification`,
and the `ThreadItem` variants we read (`commandExecution`, `fileChange`,
`agentMessage`, `reasoning`, `plan`). Enums `AskForApproval`, `SandboxMode`,
`ApprovalsReviewer`, `AuthMode`, `CommandExecutionStatus`, `PatchApplyStatus`,
`GuardianApprovalReviewStatus`: unchanged.

**Changed, none of which needed a code change:**

| Where | Change | Effect on us |
|---|---|---|
| `CodexErrorInfo` (in `error`, `turn/completed`) | `oneOf` → `anyOf`; new string values `flexUnavailable`, `tooManyDenials`; a catch-all `string \| object` variant | Capacity detection compares against three named strings, so new values and objects fall through to "not capacity". `flexUnavailable` is a flex-service-tier error; we never request flex, so it is not treated as capacity. |
| `UserInput` image (`turn/start` input) | `url` no longer required on its own: either `url` or `fileId` | We send `{type: "image", url: "data:…"}`, still the first alternative. |
| `TurnStartParams` | optional `disabledPluginIds` | Not sent. |
| `ThreadStartParams` | optional `daybreakEnabled` | Not sent. |
| `ThreadStartResponse` / `ThreadResumeResponse` | optional `disabledPluginIds`; `Thread` gains optional `daybreakEnabled`, `environments`, `originator` | Required fields we check (`model`, `modelProvider`, `approvalPolicy`, `approvalsReviewer`, `sandbox`, `thread.id`, optional `reasoningEffort`) unchanged. |
| `ThreadResumeParams` history | new `configuration_update` history item | We never send history (`excludeTurns`). |
| `InitializeParams.capabilities` | optional `explicitGatewayOauth` | Not sent. |
| `GetAccountResponse` | optional `workspaceRouting`; `PlanType` gains `promax` | We read only `account.type == "chatgpt"`. |
| `AccountUpdatedNotification` | `PlanType` gains `promax` | We read only `authMode`. |
| `AccountRateLimitsUpdatedNotification` | `RateLimitSnapshot` gains optional `normalModelSlug`; `promax` | The HUD meter reads `limitId`, `limitName`, `primary`/`secondary` `usedPercent`/`windowDurationMins`/`resetsAt` — unchanged. |
| `PermissionsRequestApprovalParams.cwd`, `GuardianApprovalReviewAction` `cwd`/`files` | type alias `AbsolutePathBuf` → `LegacyAppPathString` | Still JSON strings. We answer permissions with an empty grant and only pass review actions through. |
| `ThreadItem.mcpToolCall` | optional `mcpAppUi` | We do not render MCP tool calls. |
| `thread/rollback` | removed | Never used. |

New notifications fall through `_notification` without effect, as every
unrecognised notification always has.

## Config keys (`--strict-config`)

Not verifiable without starting an app-server, which this change did not do.
Every key `codex_config.config_text` writes (`model_provider`,
`forced_login_method`, `cli_auth_credentials_store`, `sandbox_mode`,
`approval_policy`, `approvals_reviewer`, `web_search`,
`check_for_update_on_startup`, `shell_environment_policy`, `projects.*.
trust_level`, `model`, `model_reasoning_effort`, `mcp_servers`) is still a
string in the 0.161.0 binary, and the schema's `Config` view is identical for
the subset it covers. If 0.161.0 rejected one, `thread/start` would fail
closed at startup with "Codex RPC … failed", not run unconfigured.

## Fail-closed hardening shipped with the floor

The floor is only as safe as what happens to a message the adapter does not
understand, so that was re-checked and tightened (`tests/v2/codex_provider_check.py`,
`test_unknown_and_malformed_requests_fail_closed`, run against a fake
app-server over real pipes):

- An unrecognised server request — including an approval- or
  permission-shaped method a newer Codex might add — is answered with a
  JSON-RPC error and an ERROR event; the permission callback is never asked.
  (This was already true; the test now pins it for two such methods.)
- **New:** a command/file approval whose shape does not parse (no `itemId`,
  a non-string `command`/`cwd`/`reason`/`grantRoot`/`approvalId`, a
  non-object `additionalPermissions`, a non-list `availableDecisions`) is
  answered `{"decision": "decline"}` and never reaches the owner. Before, a
  request with no `itemId` and `command: "rm -rf ~"` was put to the callback,
  so an ALLOW-returning callback would have accepted it.
- **New:** server-request params that are not an object get an error reply
  before the session closes (before, an AttributeError closed the session
  with Codex left waiting on an unanswered request).
- **New:** any exception while handling a server request — a question with no
  `question`, a dead transport, an abandoned generator — answers that request
  with an error if it had not been answered, then fails the turn as before.

Both new paths were verified to bite: with `_approval_problem` stubbed to
accept everything, five malformed-approval subtests fail (four by reaching the
callback); with the refusal stubbed out, the malformed-question subtest fails
with no reply sent.

## Not verified

- No real turn was run on 0.161.0 (no paid calls). Runtime behaviour that the
  schema does not describe — e.g. whether `auto_review` now escalates more or
  fewer approvals, or what `tooManyDenials` is triggered by — is unobserved.
- `currentTime/read` carries a `threadId` but no `turnId`, so if 0.161.0
  starts sending it mid-turn, the identity check refuses it and closes the
  turn. That is fail-closed and unchanged from 0.153.4, but it would surface
  as a failed turn rather than a working one.
- Command approvals carry an optional `additionalPermissions` grant (both
  versions). An owner's "allow" accepts the command with it; it is shown in
  the approval's args, but nothing strips it. Unchanged by this work.
