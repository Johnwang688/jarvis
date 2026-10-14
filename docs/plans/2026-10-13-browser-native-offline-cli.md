# Offline installed CLI timeout and cancellation fixture

Status: proposed; no installed CLI has been invoked for this batch. Root approval
is required before execution. This is native transport acceptance with scripted
synthetic model replies, not subscription authentication or model judgment.

## Isolation and stage zero

Use only installed Linux Claude/Codex executable bytes, the shared Python venv,
a new temporary HOME/XDG/CODEX_HOME/CLAUDE_CONFIG_DIR and dummy keys. Do not copy,
open, symlink or inherit owner CLI config, auth files, credentials or environment.
No owner CLI credentials/keyring/account requests are needed. The probe process
environment is explicitly constructed, never copied wholesale. Existing installed
code may be read for option/schema documentation; configuration is generated in
the temporary fixture directory only. Disable nonessential telemetry/update
requests using documented installed options where available.

First attempt a rootless isolated network namespace running BOTH the fixture HTTP
provider and CLI within it, with loopback brought up, to make external egress
impossible. This preflight is one short capability check; do not change host
network policy or use sudo. If WSL refuses netns (as some prior fixtures did),
report the exact failure and ask root to choose whether bounded loopback model
endpoint overrides plus dummy keys are sufficient or whether to stop. Do not
silently run an unconfined model endpoint. Native versions/help/schema are
inspected under the same temporary environment. No native call starts until
configuration parses and both model endpoints visibly point to the fixture.

## Scripted provider HTTP fixture

One ephemeral loopback HTTP server implements only the required synthetic
Anthropic Messages / OpenAI Responses streaming shapes. It accepts dummy headers,
rejects unexpected paths and never forwards a request anywhere. It records
bounded tool-name and stage/outcome metadata, never inherited secrets. All text,
URLs and tool arguments are fixed fixture strings. Model responses select only
the exact Browser MCP tool name discovered in the CLI's outbound tool catalog;
no paid model or cloud endpoint participates.

Claude uses ANTHROPIC_BASE_URL at that local listener and ANTHROPIC_API_KEY=dummy,
plus strict MCP config carrying the real Jarvis stdio relay. Actual native
PreToolUse hooks run through the current ClaudeProvider hook where feasible.
Codex uses a temporary custom model-provider table with local base_url,
wire_api=responses and a dummy env_key, no subscription auth. Per-tool native
approval_mode=approve and truthful annotations come from bridge.server_entry.
The raw app-server route may be necessary: production CodexProvider deliberately
requires model_provider=openai and forced_login_method=chatgpt. If so, explicitly
record that only the actual native MCP transport/metadata/timeouts are tested;
production subscription startup/readback remains covered by its unchanged fake
policy tests and is NOT authenticated by this offline fixture. No production
config validator is loosened to accommodate the test provider.

## Probe sequence and bounds

1. Each actual CLI initializes the real relay and lists the production tool with
   non-read-only/destructive/open-world annotations. Script a harmless synthetic
   parent call. Confirm native host metadata matches its active parent identity.
2. Script one sensitive Browser call whose synthetic private broker card waits
   **115 actual wall-clock seconds** before the fixture owner resolves it. Keep
   the 120-second card budget and 900-second relay/Codex tool timeout. Measure
   monotonic elapsed from card insertion to resolution and require >=115 seconds.
   The actual CLI must submit that exact returned tool result in its next local
   model request, proving the MCP call survived; a relay-only receipt is not enough.
3. Script a second Browser call, cancel through the native CLI turn interruption,
   and verify the exact relay socket closes/card withdraws/no later CDP event.
   The actual MCP cancellation wire behavior is recorded rather than assumed.
4. Parent-only policy is still checked: actual native child identity where the
   fixture can drive it deterministically, otherwise retain that item UNRUN and
   rely only on the separate fake-host denial tests for the narrower claim.

One sequential slot2 job, <=180 seconds/provider for each long-wait probe with
active process watchdog, at most 2 provider runs/6 minutes total plus a short
capability preflight. Three GiB/no swap and >=4 GiB available. Fixture servers,
relay and CLI children stay in that scope; cleanup only exact owned processes.
Preserve executable version, redacted configuration, actual exit and monotonic
card/CLI-result timings. Do not claim owner account acceptance, subscription
billing, model quality, real-world browser side effects or installation.
