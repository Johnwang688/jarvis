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
and the legacy `applyPatchApproval` / `execCommandApproval`. The method set is
the same, but one *params* type changed that the first pass missed (found in
review): **`McpServerElicitationRequestParams` gained an
`openai/userVerification` mode** — a device-authenticated approval whose
accepted response carries a proof. It has no effect on us: every
`mcpServer/elicitation/request` is refused (it carries no `turnId`, so the
identity check answers it with an error; with one, it falls to the
unsupported-request error). Nothing ever answers an elicitation with
`accept`.

`currentTime/read` appears only in the `--experimental` schema, which is the
one that applies: `initialize` opts into `experimentalApi: true`. It is a
side-effect-free clock read that carries a `threadId` and no `turnId`. Before
2026-10-08 that made it fail the turn-identity check and kill the turn; it is
now answered `{"currentTimeAt": <unix seconds>}` after checking only that the
`threadId` is ours (another thread's read is still refused and fatal).

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

## What the gate does with a newer Codex — exactly

The first draft of this file said the floor was safe "because anything not
understood gets an error or a decline". Review (2026-10-08) showed that was
not true, and that the real hole predated the floor: **an approval that
reached us was accepted with nobody deciding.** A request only reaches the
adapter after Codex's own `auto_review` reviewer has passed it, and on an AUTO
brief `permit`'s layer 5 answers ALLOW ("the provider's reviewer decides") — so
the reviewer's yes was taken as ours. The reviewer drove the real
`_server_request` with a real `build_permit`, an AUTO brief and a deny-all
asker, and every one of these came back `accept`: `additionalPermissions`
{network on, write /home}, `kind: writeStdin`, an unknown future `kind` with
`command: null`, a fileChange with `grantRoot: "/"`, and a request carrying
`networkApprovalContext`. The reply is a bare `{decision}`, so a grant riding
on an approval cannot be stripped: it is accepted whole or not at all.

What is true now (`tests/v2/codex_provider_check.py`, fake app-server over
real pipes, the daemon's own `build_permit` where it matters):

- **Unknown server methods** get a JSON-RPC error and never reach the permit
  callback (true before; now pinned for two approval/permission-shaped
  methods). `item/permissions/requestApproval` is still answered with an
  empty grant.
- **Malformed approvals are declined, unasked**: no `itemId`; a non-string
  `command`/`cwd`/`reason`/`grantRoot`/`approvalId`/`kind`; a non-object
  `additionalPermissions`/`networkApprovalContext`; a non-list
  `availableDecisions`. Params that are not an object get an error; any
  exception while handling a request answers it with an error, exactly once.
- **An unknown `kind`** is declined outright — we cannot describe it to the
  owner.
- **A sandbox-widening approval is asked of a human every time.** Widening
  means: non-empty `additionalPermissions` (`network: {enabled: false}` is
  not), `networkApprovalContext`, `grantRoot`, `kind: writeStdin`, or any
  non-null field outside the verified schema (a new field might be a new
  grant). Codex calls `permit(..., widening="SANDBOX WIDENING: network on ·
  write /home · grant root /")`; `build_permit` then skips layers 2–5, asks
  with `allowlistable=False` (no Always) and the line as the request's
  `headline`, which the HUD card and the Discord post show first. A deny-all
  asker, no surface, a timeout, or a strict profile declines. Layer 1 still
  refuses first. It is a keyword, not an arg, so the model cannot set it; a
  callback that does not accept it raises, which denies.
- **The headline is one clean, capped line** (re-review). It is built from
  Codex-supplied strings — `grantRoot`, file-system paths, the network host,
  commands — so each part goes through `approvals.clean_line` (`label()`'s
  rule: whitespace, control and format characters collapse to one space,
  backticks go) capped at 120, and the line at ~400 with "… (+N more)"; the
  full detail stays in `args`. `permit` cleans it again, the HUD card
  (`lib/approval.ts` `headlineLine`, no `pre-wrap`) cleans it again, and the
  Discord renderer escapes `\ * _ ~ | > # [ ] ( ) <` and defuses `@`. A widening
  too long for one Discord message keeps its headline, tool, origin and
  answers inline and attaches the whole request as `approval.txt` — before,
  a 5,000-char `grantRoot` drew a fake "Approval required: Read" line above
  the real one, or hid the headline in `message.txt`.
- **Layer 1 judges what a widening opens.** Codex passes each filesystem
  grant as `(access, path)` (`grants=`; relative paths placed under the
  approval's cwd, a shape it cannot place as "/") and `build_permit` refuses,
  unasked, any grant that **is or contains** Jarvis's permission state
  (`protected_paths()`, `files._protected_state()`), a SELF_PROTECTED file, or
  that touches a credential directory at all — reads included, since a read
  is how a key leaves (`permissions.denied_grant`). "grant root /" and
  "write ~/.config/jarvis" are DENY, not a question.
- **A null `command` cannot hide the real one.** Approval params are merged
  over the item's fields non-null values only, so `command: null` no longer
  erases an item's `sudo rm -rf /` and leaves the never-approvable rules
  judging "". A command approval with no command string anywhere is declined.
- **A command that differs from its item** (whitespace aside) is declined
  only for a plain approval (`kind` null/`command`, no `approvalId`). For
  `writeStdin` — on by default in 0.161.0 with `unified_exec`, carrying the
  text typed into a running terminal — and for an `approvalId` subcommand
  (the zsh exec bridge) a difference is expected: both commands go in the
  headline and `args` (`item_command`), layer 1 judges both
  (`also_commands=`), and a human is asked, no Always.
- **`answer()` can only withdraw an approval.** `POST /threads/<id>/answer`
  lands on `provider.answer`; it used to replace whatever `permit` returned,
  so a racing `allow` turned a hard-denied `sudo rm -rf /` into `accept`. It
  now raises for anything but DENY on an approval, and the handler honours a
  pending DENY only. Questions still take free text.
- **`currentTime/read`** is answered (above).

**What is still not covered.** A plain in-sandbox approval under AUTO is
still accepted on the strength of Codex's reviewer — that is the AUTO
contract (design R8), not something this adapter can fix. A field we *know*
whose meaning a later Codex broadens is judged by its old meaning. And
`proposedExecpolicyAmendment` / `proposedNetworkPolicyAmendments` are treated
as inert because they apply only to an `acceptWith…Amendment` answer, which we
never send — verified in the schema, not at runtime.

The re-review's fixes add 10 more (all killed): the Discord headline
unescaped, `@` not defused, the headline cleaned nowhere, a long widening
attached whole, the grant check by equality only, grants not passed, the
extra commands not judged, a differing command always or never declined, and
whitespace counting as a difference.

Each new path was verified to bite by mutation (16 mutants, all killed): no
widening detected, the `widening` keyword ignored, a widening offered Always,
an unrecognised field ignored,
an unknown kind treated as plain, `args.update(p)` restored, the differs
check removed, `answer()` allowing, a planted pending yes honoured,
`currentTime/read` unhandled or not thread-checked, a second reply after the
first, a string version compare, `prepare` re-resolving the binary, a loose
`JARVIS_CODEX_STRICT` parse, and a changed `/mnt` default.

## Not verified

- No real turn was run on 0.161.0 (no paid calls), and no app-server was
  started. Runtime behaviour the schema does not describe — whether
  `auto_review` escalates more or fewer approvals, what `tooManyDenials` is
  triggered by, whether the item's `command` and the approval's `command` are
  ever spelled differently for the same exec (if so, such approvals are now
  declined) — is unobserved. R4 saw no approval escalate live at all.
