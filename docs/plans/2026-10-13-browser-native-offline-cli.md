# Offline installed CLI timeout and cancellation fixture

Status: both installed CLIs passed root-approved offline 115-second transport
fixtures: Claude 2.1.295 and Codex 0.161.0. Further lifecycle probes require their
specific root grant. All replies are scripted synthetic model responses;
subscription authentication and model judgment remain UNRUN.

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

## Measured evidence and remaining gates

The isolated stage-zero receipt is `batch2-native-offline-stage0-result.json`
in the external `codex-oct13` evidence directory. Rootless network namespaces
worked with only loopback, and both installed versions were read with temporary
configuration and dummy credentials. No owner CLI state was read.

At source `eb1ca6d9bc6991a7fabae143738b9b6e7d73681a`, Claude's actual private
card waited 115.000291578 seconds and the next synthetic provider request contained
the returned Browser result. Wrapper exit was 0 after 116.697 seconds, with its
owned scope inactive. See `batch2-native-offline-claude-result.json`, matching
wrapper receipt and full log. The Browser action itself was substituted in this
transport fixture; the two separately passing real-Chrome fixtures cover actual
Browser/CDP behavior through the production host, with fixture owner visibility.
They do not prove production HUD attachment or subscription/account acceptance.

Codex's `batch2-native-codex-longwait-codex-result.json` records an actual
private-card wait of 115.000295365 seconds, the returned Browser result in the
next native provider request, and matching actual parent thread/turn identity
with `subagent=false`. Wrapper exit was 0 after 116.708 seconds, with its owned
scope inactive and empty ControlGroup. Runner SHA-256 was
`9f1e1b644b6651e5b6967a32af53edcc97a32818049c35d62ed927fb0e249f27`.

The installed Codex protocol first emits exact Jarvis startup notifications from
`starting` to `ready`; its status-list runtimeStatus is null despite the complete
22-tool catalog. Its outbound model schema exposes raw-JavaScript
`functions.exec` custom tools in typed `additional_tools`. The corrected fixture
uses actual `custom_tool_call`, discovers the Browser function from `ALL_TOOLS`,
and sets the documented 120000-ms exec yield to keep the result in that call.
The short actual MCP receipt precedes the long probe and confirms real admission
and bridge dispatch. Earlier direct-function and readiness-field fixture failures
remain preserved in separate receipts; they are not product refusal evidence.

At integrated runtime `4acf2ed082ad6fec8c87de8abdddfd6c06f96e31`, actual
native steering passed for both CLIs. Codex's three exact successful, nonabandoned
`turn/steer` replies preceded owner-word updates; Claude's actual SDK writes
returned before those updates. Latest desk words stayed bounded, off-desk text
revoked captured desk authority and carried uncertainty into one successor,
and a pending owner card queued words without granting authority. Stop dropped
that queue. See `batch2-native-steer-ready-{claude,codex}-result.json`.

The steering acceptance review found and fixed an unconfirmed-receipt seam:
Codex still counts an unanswered steer as delivered under its existing queue
policy, but returns False so it cannot authorize Browser owner words. Confirmed
True and Claude's legacy successful None retain authority. Late replies cannot
retroactively update it. Commit `9a9f455` passed the three focused provider,
Browser-MCP and steering suites before the clean current-main integration.

Actual Codex child metadata (`subagent=true`, distinct child thread/turn) reached
the production host and was refused before dispatch; the exact parent then
acted once and completed cleanly. The child's model request used an opaque
encrypted message carrier: nothing was decrypted. The derived
`batch2-native-child-identity-codex-addendum.json` corrects only an overwritten
mechanism label in the preserved original receipt; this proves host child
denial, not catalog omission. Claude 2.1.295's actual child omitted Browser
capability from its catalog, and its parent acted cleanly. A separate Driving-ON
hook test and guard mutant verify denial before permission/dispatch; literal
live child Act-hook invocation remains unobserved.

The real isolated Face/HUD attachment and heartbeat component passed separately.
Then actual native CLI + real HUD guarded look/ref/action/banner and native
close/resume passed in every supported profile: Claude ASK and AUTO, Codex AUTO.
Codex ASK is refused before startup by the production AUTO-only project policy;
no fixture bypass was added. Each supported cell performed two judged real CDP
input actions, required exact per-call native tool results and page values,
retired its old DriveTurn, rotated its carrier, refused the old carrier with
403, refused an old ref, and obtained a fresh guarded look before acting again.
See `batch2-native-hud-classify-*-result.json`. Backend source is `4acf2ed`;
read-only tested public HUD assets are from `f26848d`, with identical complete
HUD source and a finite asset manifest. No installed file was changed.

Both native AUTO providers also passed actual sensitive Browser action cards
through the real HUD. The deterministic synthetic reviewer requested a sensitive
category; the production Browser gate made each private card. Actual DENY
produced no page input; a fresh turn's actual AUTHORIZE allowed one input.
Exactly two distinct cards, two judgments and one real execution were observed,
with private-detail/request identity, exact native results and clean completions.
See `batch2-native-hud-card-ready-*-auto-result.json`.

Claude's native interruption independently closed its exact relay socket while
all other admission predicates stayed live. Raw-wire Codex interruption finished
its native turn but did not close that socket within the bounded probe; this is
a measured CLI limitation, not a socket PASS. The normal production Codex Stop
path separately revoked admission and withdrew the real card before daemon
cleanup; actual natural completion also retired admission without Stop. Preserve
these distinct mechanisms in `batch2-native-interrupt-acceptance.json` and the
composed Codex Stop/completion receipts.

Actual HUD pane Stop/Resume passed for both CLIs while a real CDP type was in
flight. The first 32-character insertion completed, its exact task and two
activity holds stayed owned after Stop, and no following input attempt occurred.
After settlement and the real Resume acknowledgement, a new native turn refused
the old ref and used a fresh guarded look. Production Chat Stop also passed for
both CLIs via `Daemon.interrupt` (not a clicked HUD Chat Stop button), with actual
native interrupted completion and revoked admission while CDP work settled.

Codex's existing incomplete-accounting policy intentionally rejects continuation
in the same native thread after Chat Stop. The classification probe observed the
exact fatal accounting category, false accounting-complete flag and zero new
API/action/input; an actual fresh owner chat then refused the old ref, looked
again and acted cleanly. This policy predates BR9d and was not weakened. Normal
completed-session resume and pane Stop/Resume remain separately supported.
See `batch2-native-hud-stop-*-result.json` and
`batch2-native-hud-stop-accounting-observe-codex-chat-result.json`.

The final evidence index is `batch2-native-acceptance-final.json`; original
protocol, readiness, prerequisite and unsupported-profile failures remain
preserved. The earlier 137-suite integration receipt and reviewed corrections
remain separate from the final current-main regression: eleven affected suites
passed on runtime `4acf2ed`, while a stale Auth callback-name audit expectation
was corrected in test-only `944dace`. No later docs/test change alters `jarvis/`
or `hud/`; the dispatch-only final check returned 0 with its existing guard
mutants and exact scope cleanup. All receipts remain separately retained. Subscription/account
authentication and Codex production subscription startup, paid-model judgment,
physical owner/device behavior and installed deployment remain UNRUN.
