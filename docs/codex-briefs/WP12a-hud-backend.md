# WP12a — HUD backend: routes, preview origin, files, usage, schedules, speech

You are Codex, implementing the backend half of work package 12, in a git
worktree on branch `jarvis/wp12a-hud-backend`. Claude leads; the frontend
(WP12b) is being built in parallel against the same contract, so **the
contract is fixed**: `docs/hud-api.md`. Read first:

1. `docs/hud-api.md` — every route, shape and status code. Implement it
   exactly; propose changes in your notes, do not deviate.
2. `docs/jarvis-v2-design.md` §12 (esp. 12.1), §6, §9.2, §17.
3. Merged v2: `daemon.py` (routes, `_route`, the bus, the two listeners
   you add), `stores.py`, `worktrees.py` (`status`, base ref), `ledger.py`,
   `router.py`, `runner.py` (`TaskControl`, `task_question`), `control.py`
   (fixed), `providers/codex.py` (where rate-limit notifications would
   surface — see below).
4. v1 you re-home: `jarvis/face/server.py` for `/say`, `/stt` (the STT
   half of `_converse`), `/avatar*`, `/voices`, `/voice`, `/models*`,
   `/model`, `/mute`, the `_assemble_turn` attachment rules, the workshop
   server (`WORKSHOP_PORT`, `no-store`, separate origin) and
   `find_browser()`; `jarvis/tools/secrets.py` (`is_protected`, `scrub`);
   `jarvis/tools/files.py` (`_self_protected`); `jarvis/tools/search.py`
   (`SKIP_DIRS`); `jarvis/models.py`; `jarvis/avatars.py`; `jarvis/voice.py`.

## Deliverables

- **Two listeners.** The daemon serves the API on `DAEMON_PORT` (8405, as
  now) **and** on `FACE_PORT` (8402) together with the static HUD from
  `hud/dist/` (`GET /` and `/assets/*`; if `hud/dist` is absent serve a
  one-line placeholder page saying so). Same handler class; the
  single-instance lock stays on 8405. A third listener on `WORKSHOP_PORT`
  (8403) serves `/p/{project_id}/<rel>` read-only per the contract and
  nothing else — refuse anything outside a project's scope (root +
  `extra_dirs`, resolved, no symlink escape), skip `SKIP_DIRS`, `no-store`.
- **Files:** `tree`, `file` GET/PUT, `platform`, task `diff` and
  `diff/file`, exactly as the contract; scope check, protected names,
  SELF_PROTECTED / allowlist / v2 config trio refused on PUT (reuse
  `jarvis.v2.permissions`' file-target check if it is importable; else the
  v1 helpers), mtime conflict 409, 2 MB cap 413, atomic write.
- **Threads:** `transcript`; `send` with attachments per the v1 rules.
- **Tasks:** `threads`, `journal`.
- **Usage:** `GET /usage` from the ledger; `quota` **only** from a
  provider's own report. **Verify on codex-cli 0.153.4** whether the
  app-server emits rate-limit notifications (the TUI shows 5h/weekly
  windows, so the protocol carries them somewhere — check the generated
  TS bindings as WP4 did). If it does, have `CodexProvider` surface them
  as a `USAGE` event field `provider_reported.rate_limits` (additive; the
  fixed interface allows it) and the ledger keep the latest; if it does
  not, `quota: null` and say so in the notes. Claude: `quota: null`
  always in this package. Publish `usage_updated` on the bus when the
  figures change.
- **Schedules:** store under `V2_DATA_DIR/schedules/<id>.json`; a
  scheduler thread in the daemon (60 s tick, `America/Chicago`, 5-field
  cron parsed by a small stdlib parser you write — minute, hour, dom,
  month, dow with `*`, lists, ranges, `*/n`), creating a task via
  `stores.tasks.create` and `runner.start`, journaling `scheduled_by`,
  publishing `schedule_fired`; a disabled schedule never fires; a fire
  while the previous task from that schedule is still active is skipped
  and journaled. `run-now` bypasses the clock.
- **Speech and pickers:** `/stt`, `/say`, `/avatar*`, `/voices`,
  `/voice`, `/models*`, `/model` (fast path only — it writes the v1
  roster selection, which only `FastPathProvider` reads), `/mute`, each
  with v1's semantics and broadcast kinds on the bus.
- **`jarvis hud`** subcommand: starts the daemon (if not running) and
  opens `http://localhost:8402/` in the owner's Windows browser via v1
  `find_browser()` in app mode; `--no-window` skips the browser.
- **`hud/dist` is not yours**; do not create it beyond the placeholder
  logic.

## Tests: `tests/v2/hud_backend_check.py`

Free, fake providers, temp roots, a temp project with real files and a
real git worktree from `worktrees.ensure`: every route's happy path and
each documented error code; scope escape via `..` and symlink refused;
protected name → no content; PUT conflict; diff shapes from a real
commit on the task branch; transcript excludes deltas; attachments rules;
usage shape with `quota: null` and with a fake provider reporting
rate limits; schedules: cron parser table (≥ 20 expressions incl. `*/15`,
`0 9 * * 1-5`, DST day in Chicago), fire/skip/disabled/run-now, task
created and started through a fake `TaskControl`; the preview origin
serving a project file with `no-store` and refusing another project's
path and any HUD-origin URL; both listeners answering `/status`; STT with
`voice.stt` stubbed; `/model` changing only the fast path. Then every
existing `tests/v2/*_check.py` and `tests/face/controls_check.py`.

## Rules

Stay inside `jarvis/v2/daemon.py`, `jarvis/v2/hud_api.py` (put the new
handlers there and mount them from the daemon), `jarvis/v2/schedules.py`,
`jarvis/v2/providers/codex.py` (rate limits, additive), `jarvis/__main__.py`
(one subcommand), `tests/v2/`, `docs/codex-briefs/WP12a-notes.md`. No
fixed-interface edits, no v1 edits, stdlib only (httpx is already present).
When green, commit with a message starting `v2 WP12a: HUD backend` ending
with `Co-Authored-By: Codex <noreply@openai.com>`. Do not push. Finish with
`WP12a-notes.md` (under 80 lines): what you built, the rate-limit finding,
test commands and last lines, workarounds, proposed contract changes.
