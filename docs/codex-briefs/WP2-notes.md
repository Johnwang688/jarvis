# WP2 — FastPathProvider

## Built
- `jarvis/v2/providers/fastpath.py` — `FastPathProvider` over a v1 `Agent`: one
  agent + v1 `sessions.Session` per handle, `FAST_MAX_STEPS = 8`, system =
  `SYSTEM_PROMPT + FAST_PROMPT + brief.system_append`. `send()` runs `run_turn`
  on a worker thread and yields `Event`s off a queue, so `TEXT_DELTA` arrives
  *during* the model call; the worker's `finally` always posts the sentinel, so
  the generator cannot hang.
- `FAST_TOOLS` (22 names) with `_validate_toolset()` at import: a missing,
  dangerous or §8.1-excluded entry raises rather than shrinking silently
  (invariant 10's failure mode). Exclusions live as data, not in a reader's eye.
- `jarvis/v2/tools/propose.py` — `task_propose`, not dangerous, writing into
  `runtime.proposal_slot()`; a second call replaces the first. Imported by
  `fastpath.py`, so registering is a side effect of using the provider.
- `jarvis/runtime.py`, additive only: `_PROPOSAL` ContextVar, `proposal=` on
  `bind()`, `proposal_slot()`. A ContextVar rather than `goalctl`'s module
  global because chat is not serial — HUD, Discord and daemon each hold a handle.
- Proposal channel: `TURN_FINISHED.data["proposal"]` from an explicit
  `task_propose`, else from `stopped_early` as `"[fast path ran out of steps] "
  + turn.text` (v1's tool-free handoff, pointed at a task). Explicit wins.

## Validation
```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /home/johnw/projects/Jarvis/.venv/bin/python tests/v2/fastpath_check.py
ok  isolation: two concurrent handles share no proposal and no transcript
all fast-path checks passed
... tests/loop_check.py        -> all loop checks passed
... tests/longhorizon_check.py -> all long-horizon checks passed
... tests/sessions_check.py    -> all session checks passed
```
Also green: `tests/v2/{stores,worktrees}_check.py`, `permissions_check.py`,
`self_improve_check.py`, `tasks_check.py`. `git diff --check` clean. No network.

## Judgement calls worth reviewing
- **`ok` on `TOOL_FINISHED` also excludes a decline.** v1's denied-approval
  result starts "The user declined…", and a refused call must not render as one
  that happened. `_NOT_OK = ("Error", "Refused", "The user declined")`.
- **`USAGE.cost_usd` is the increment**, not v1's cumulative-per-turn figure —
  summing the raw events would over-count. The cumulative value rides
  `provider_reported.turn_cost_usd`; `usage()` sums increments.
- **`find_files` added** (same read-only family, on no exclusion list).
  `memory_delete`, `skill_write`, `web_search`, `query_sqlite`,
  `compact_context`, `discord_dm_owner`, `whiteboard_close` left out — each
  writes or reaches outward, and none was in the brief's list.
- **`task_status` excluded.** §8.1's prose lists it; this brief's exclusions say
  `task_*`. Followed the brief — settle it in WP9.
- **`allowed_tools` naming anything outside `FAST_TOOLS` is `BriefRefused`**
  (§5.1: refuse, don't narrow silently); a subset is honoured. So is
  `mcp_servers`. Test-only widening is a `_toolset()` subclass override.
- **Deferred groups (spotify) are filtered by availability at `start()`**, not
  in `FAST_TOOLS`: the constant stays machine-independent for tests while a
  machine without the account sends no schemas.

## Proposed interface changes (nothing edited)
- **`Brief.effort` has nowhere to go** — v1 resolves it per call via
  `models.effort_for(self.model)` and `Agent` takes no argument. Ignored.
- **`Brief.cwd` is not honoured** — v1 file tools resolve against the process
  CWD and `os.chdir` is global while handles run concurrently. Read-only, so
  the blast radius is reading the wrong tree; the real fix is a root-aware path
  resolver in v1's file tools, outside this package.
- **`Brief.max_turns` ignored** — it counts turns; this budget is steps.
- **`usage()` tokens are 0.** `TokenMeter` keeps only the latest
  `prompt_tokens`, so there is no honest running total; the v1 BYOK lesson says
  a wrong number is worse than a missing one. If §8.3's ledger needs it, `Turn`
  should carry summed tokens.
- Registering `task_propose` widens `tools.default_names()` for v1 agents built
  after this module is imported in the same process. Harmless (it refuses with
  no slot bound), but worth knowing when the daemon holds both surfaces.
