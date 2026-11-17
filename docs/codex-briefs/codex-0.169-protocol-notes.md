# Codex app-server protocol: 0.161.0 → 0.169.0 (2026-11-17)

Why this exists: GPT-6.2 Astra and GPT-6.2 Sol shipped on 2026-11-17, and the
account's `model/list` did not offer them to the owner's CLI (0.161.0). The
catalog Codex caches (`~/.codex/models_cache.json`) records the
`client_version` it was fetched for, and the server filters by it; a
third-party report puts the minimum client for both models at 0.166.0. The
owner chose to verify the newest release (0.169.0) before updating the
installed CLI. This file is that evidence, in the same form as
`codex-0.161-protocol-notes.md`.

## How it was checked

The 0.169.0 release (`@openai/codex@0.169.0-linux-x64` from npm, the musl
binary under `vendor/x86_64-unknown-linux-musl/bin/codex`) was unpacked into
its own folder. The installed CLI was not touched. For each version, with a
scratch `CODEX_HOME` (no login, no network, no turn):

```sh
codex app-server generate-ts --experimental --out <dir>
codex app-server generate-json-schema --experimental --out <dir>
```

Every definition was rendered with `$ref`s expanded, ignoring
`description`/`title`, and diffed between the two versions. `codex
app-server --help` still lists `--stdio` and `--strict-config`.

Then, on the real 0.169.0 binary:

- `tests/v2/fixtures/cg2_probe.py`, hermetic, in a rootless network namespace:
  **45/45 expectations held, ALL PASS**. The run printed `CODEX
  …/codex-0.169/…/bin/codex codex-cli 0.169.0`. The fixture's
  `record_codex.CODEX` now honours `JARVIS_CODEX_CLI` (it was hardcoded to
  `~/.local/bin/codex`, so an earlier run had silently judged 0.161.0), and
  the probe prints the binary and version it judged.
- An independent GPT-6.1 Sol review re-derived the diff over every
  definition the adapter reaches (242 → 246) and the 11 server requests,
  injected 96 `humanCommand` cases (all refused before `permit`), and
  confirmed all 865 test assertions kept: APPROVE, with the field-level
  corrections folded into the table below.
- `tests/v2/codex_provider_check.py` (144 tests), `router_check.py`,
  `thread_model_check.py` and `hud_backend_check.py`: all OK. These use a
  fake app-server and do not depend on the binary.

## Method sets

| | 0.161.0 | 0.169.0 | change |
|---|---|---|---|
| server requests | 11 | 11 | **none** |
| server notifications | 84 | 86 | + `thread/readState/changed`, `thread/section/changed` |
| client requests | 169 | 184 | + account GCP/security setup, account sessions, `feedback/save`, model-provider list/config write, thread attachment owners, guardian review, items/memory read, approved-plan prep, read state, sampling. None removed. |
| client notifications | 1 | 1 | none (`initialized`) |

New notifications fall through `_notification` without effect, as every
unrecognised one always has.

## The approval surface

Unchanged, rendered identical: `FileChangeRequestApprovalParams` and
`…Response`, `PermissionsRequestApprovalParams` and `…Response`,
`CommandExecutionRequestApprovalResponse`, `ToolRequestUserInputParams` and
`…Response`, `McpServerElicitationRequestParams`, `CurrentTimeRead*`, the
legacy `ApplyPatchApprovalParams` / `ExecCommandApprovalParams`,
`DynamicToolCallParams`, `ChatgptAuthTokensRefreshParams`,
`AttestationGenerateParams`.

**One change: `CommandExecutionApprovalKind` gains `humanCommand`.** Codex
sends it only to a client that opts in with the new
`InitializeCapabilities.requiredHumanApproval` ("Present `humanCommand`
approvals to a person and return only a one-time decision. Requires
`experimentalApi`."). Jarvis never sends that capability. Should one arrive
anyway, `_PLAIN_KINDS` is `(None, "command")` and `humanCommand` is not in
`_WIDENING_KINDS`, so it is an unknown kind and is **declined outright**,
unasked. That fails closed. Supporting it would be a separate decision.

## What we use, field by field

**Unchanged:** `InitializeResponse`, `ThreadStartParams`, `TurnSteerResponse`,
`TurnInterruptParams`, `ThreadCompactStart*`, `ConfigReadParams`,
`GetAccountParams`, `ModelListResponse`, `Model`, `TurnStatus`,
`ThreadTokenUsageUpdatedNotification` / `TokenUsageBreakdown`,
`TurnPlanUpdatedNotification`, `AgentMessageDeltaNotification`,
`FileChangePatchUpdatedNotification`, both reasoning delta notifications,
`ModelReroutedNotification`, `ServerRequestResolvedNotification`,
`ThreadClosedNotification`, `ItemGuardianApprovalReviewCompletedNotification`,
and the enums `AskForApproval`, `SandboxMode`, `ApprovalsReviewer`, `AuthMode`,
`CommandExecutionStatus`, `PatchApplyStatus`, `GuardianApprovalReviewStatus`.

**Changed, none of which needs a code change:**

| Where | Change | Effect on us |
|---|---|---|
| `InitializeParams.capabilities` | optional `requiredHumanApproval` | Not sent (above). |
| `ThreadStartResponse` / `ThreadResumeResponse` | `thread` gains optional `attachedToThreadId`; `Turn` gains optional `rootTurnId` everywhere a turn appears (responses, notifications, stored turns) | The required fields we check are unchanged. |
| `TurnStartParams` / `TurnSteerParams` | optional `parentTurnId`, `rootTurnId` (start only), `responsesapiClientMetadataPatch`, `additionalContext.*.name`; `TurnToolOutput` gains optional `retain` | Not sent. |
| `UserInput` audio (`turn/start`, `turn/steer`) | `url` or `fileId` (as an `anyOf`), as image already was in 0.161.0 | We send only text and `{type: "image", url: "data:…"}`, both unchanged. |
| `ThreadItem` | optional `startedAtMs`/`completedAtMs` on every item; `agentMessage.phase` gains `partial_answer`; `moderation` on agent messages; `imageGeneration` takes `fileId` or `result` (one is required), plus `generationId`/`inputImages`; `ItemCompletedNotification` gains optional `contentTokenCount`; `subAgentActivity` gains optional `model`/`reasoningEffort` | We read none of these fields: not `phase`, nothing of image generation, and only the `subAgentActivity` fields we already read (kind, thread ids, status). |
| `CodexErrorInfo` (in `error`, `turn/completed`) | new string values `modelNotFound`, `policyCapacityUnavailable`, `inferenceHookDenied` | Capacity detection compares against three named strings (`serverOverloaded`, `rateLimitExceeded`, `usageLimitExceeded`), all still present; anything else is "not capacity". |
| `ErrorNotification.error.misalignment` | optional `reviewTarget` | Not read. |
| `GetAccountResponse` / `Account` | optional `userId`; the ChatGPT variant gains optional `workspaceName`; a fourth account variant | We read only `account.type == "chatgpt"`. |
| rate-limit `planType` | gains `sci`, `sci_plus` | The meter does not read `planType`. |
| `ConfigReadResponse.config` | optional `client_search_provider` | Not among the effective-config keys we compare. |
| `ModelListParams` | optional `modelProvider` | Not sent. |
| Raw Responses items (`ResponseItem`, `ContentItem`, `FunctionCallOutputContentItem`) | new `additional_tools` variant; `status` on `message`/`function_call`; `encrypted_content` on several call items; `input_audio` takes `file_id` or `audio_url`; `output_text` gains `annotations`/`logprobs` | Reached only through history and raw items, which we never send and do not read. |

## Model catalog

The bundled catalog in 0.169.0 (`codex debug models`, no login) still ends at
GPT-6.1. The live list comes from the server, so whether 6.2 appears is
checked only after the installed CLI is updated, by reading
`~/.local/share/jarvis/codex-models.json` after the HUD's next metadata
refresh. Nothing in Jarvis lists Codex models by name: `router.CLI_MODELS
["codex"]` is the account's `model/list`, replaced whole.

## 0.169.1 (same day)

During this work the owner's installed CLI updated itself from 0.161.0 to
0.169.1. A `codex exec` run with the owner's `~/.codex` most likely triggered
the CLI's own update check; nobody ran `codex update`. The catalog it then
fetched (`client_version: 0.169.1`) lists `gpt-6.2-sol` (service tiers
`priority` "Fast" and `ultrafast` "Ultrafast") and `gpt-6.2-astra`
(`priority`), and the HUD's refresh saved them to
`~/.local/share/jarvis/codex-models.json`. That confirms the
client-version filter.

Checked on the installed 0.169.1 binary
(`~/.codex/packages/standalone/releases/0.169.1-…/bin/codex`):

- `generate-json-schema --experimental` and `generate-ts --experimental`
  output is **byte-identical** to 0.169.0 (`diff -rq`, both trees).
- `cg2_probe.py` with `JARVIS_CODEX_CLI` set to it: **45/45, ALL PASS**,
  printed `codex-cli 0.169.1`.

So `CODEX_VERIFIED` is 0.169.1, the version actually running.

## Not verified

- No real turn was run on 0.169.0, and the owner's login was not used with it.
  As with 0.161.0, runtime behaviour the schema does not describe is
  unobserved.
- `--strict-config` acceptance of every key `codex_config.config_text`
  writes was exercised only by the cg2 probe's own app-server (which passed
  `config/read` and the thread receipts on 0.169.0), not by a daemon start.
- Updating the installed CLI (`codex update`) and restarting daemon2 are the
  owner-approved next steps after this merges. They are not part of this change.
