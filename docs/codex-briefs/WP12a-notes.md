# WP12a — HUD backend

Built the API on DAEMON_PORT and FACE_PORT with one handler class; the
DAEMON_PORT bind remains the instance lock. WORKSHOP_PORT is read-only,
project-scoped, excludes cache/internal directories, and never proxies URLs.
HUD static files are loaded from hud/dist; absent builds get a placeholder.
No hud files, v1 code, or fixed interfaces were edited.

Added tree/file/platform, task diff/diff-file/threads/journal, transcripts,
attachments, usage, schedules, speech/pickers, and `jarvis hud --no-window`.
Normal `jarvis hud` uses v1 launch_window/find_browser and Windows app mode.
Host/Origin checks protect controls from the separate preview origin.
File writes use v2 denied_file, mtime conflicts, a 2 MiB cap and replacement.
Diffs compare the recorded base's merge-base to the checkout, including
committed, staged, unstaged and untracked changes; protected paths are omitted.
User messages are durable; transcript construction excludes provider deltas.
Schedules persist under the store's schedules directory; a 60-second worker
uses Chicago cron, creates tasks, journals scheduled_by/overlap skips, and
calls TaskControl.start. Disabled run-now returns 409; overlap returns the
unchanged last task, journals schedule_skipped, and advances the next time.

## Codex rate-limit finding

Verified installed `codex --version`: `codex-cli 0.153.4`.
Ran `codex app-server generate-ts --out /tmp/wp12a-codex-protocol`.
ServerNotification declares `account/rateLimits/updated`, whose params are
`{rateLimits: RateLimitSnapshot}`. RateLimitWindow contains `usedPercent`,
`windowDurationMins`, `resetsAt`; primary/secondary and metadata are nullable.
The generated notification comment specifies sparse merging, not clearing
known metadata on null. This is protocol verification, not a paid live turn.
CodexProvider emits additive USAGE.provider_reported.rate_limits, including
account notifications without thread/turn ids. HUDLedger subclasses the
unchanged ledger, persists reports in its rows and merges them after restart.
Only Codex reports produce quota windows; Claude/fast quota remains null.
Synchronous daemon accounting plus event ids prevents runner double counting;
usage_updated is published for totals, quota or cooling changes.

## Checks

Interpreter: `/home/johnw/projects/Jarvis/.venv/bin/python` (system Python
lacks httpx). Set `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.`. Loopback socket
checks required sandbox escalation. All providers/audio are faked.
Commands: `$PY tests/v2/hud_backend_check.py`; then
`for f in tests/v2/*_check.py tests/face/controls_check.py; do "$PY" "$f"; done`.
Final sweep used temporary JARVIS_MODELS/JARVIS_ROUTING/JARVIS_ALLOWLIST.
Last lines: HUD `Ran 17 tests ... OK`; Codex `Ran 24 tests ... OK`;
daemon `Ran 15 tests ... OK`; ledger `Ran 8 tests ... OK`;
runner `Ran 38 tests ... OK`; face `all control checks passed`.
All 16 suites passed. Full logs: `/tmp/wp12a-check-logs/`.
Also passed compileall and `git diff --check`.
Cron table covers 23 valid expressions plus invalid syntax, leap years,
spring gaps (skipped) and repeated autumn times (one fire per wall time).
Legacy daemon/ledger assertions were updated for user log records, the new
/model route, usage_updated and the contract's 409 operational errors.

## Workarounds / contract clarifications proposed (contract unchanged)

- Specify schedule response/status shapes: implemented POST 201, PATCH/run-now
  200 record, DELETE 200 {ok:true}; require exactly one cron/positive every_s.
- Specify DST, overlap and disabled run-now behavior explicitly (above).
- Tree depth is flattened into relative entry names, keeping the exact entry
  fields; absolute paths address extra_dirs. Clarify this representation.
- New file PUT uses expected_mtime:null. Define that convention explicitly.
- Define quota resets_at units (provider Unix seconds here), window names,
  and error mapping: v1 500/503 conflicts with the contract's allowed codes.
- JSON request cap is 48 MiB to accommodate eight base64-encoded 4 MiB files;
  preview/assets cap at 32 MiB. Clarify these transport limits if needed.
- Test side effect: the first legacy daemon run still probed POST /model as
  a nonexistent route. Now implemented, its empty body reset the owner's
  fast-path selection to the configured default before test isolation was
  added. The previous selection was not captured; no restoration was guessed.
  Subsequent runs isolated configuration and removed that obsolete probe.
