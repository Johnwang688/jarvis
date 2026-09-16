# WP9 — Router and UsageLedger (design §8, all five subsections)

You are Codex, implementing work package 9 of the Jarvis v2 redesign, in a
git worktree on branch `jarvis/wp9-router`. Claude leads; this brief is
your entire scope. Read first, in this order:

1. `docs/jarvis-v2-design.md` — **§8 in full**, §5.5, §4, §14 row WP9, §15
   R6 (open), and the WP2 note under §8.1 about the fast path's `usage()`.
2. `jarvis/v2/model.py` (fixed: `Routing`, `RoutingDecision`, `Status`,
   `Project.routing`, `Task.provider_override`), `provider.py` (fixed:
   `Usage`, `Provider.health`), `stores.py`, `daemon.py` + `bus.py`,
   `providers/fastpath.py` (`FAST_TOOLS`, the proposal on `TURN_FINISHED`),
   `providers/codex.py` (`_cooldown`, usage offsets — `WP4-notes.md`),
   `providers/claude.py` (`WP3-notes.md`: `cost_usd` is an equivalent).
3. `jarvis/models.py` (`tier()`, the roster) for how the Claude model
   selection is read; do not change it.

## Deliverables

### `jarvis/v2/router.py`

- **Stage one** (§8.1): `classify(incoming) -> Destination` implementing
  the four-row table exactly: verbs (`yes|no|always <code>`, `status`,
  `cancel [id]`, `steer: …`, `redirect: …`, `projects`, `tasks`, `resume
  <id> [on <provider>]`) → `Verb`; explicit intake (`task:` prefix, a
  surface flag `intake=True`) → `NewTask`; a message whose context names a
  task thread → `Steer(task_id)`; else → `FastPath`. Pure function over an
  `Incoming` dataclass (text, surface, project_id, thread_id, task_id,
  intake flag, spoken flag); **spoken messages never classify as a verb**
  (the v1 spoken-turns-never-authorize rule) — they fall to FastPath or
  Steer.
- **Proposals**: `on_turn_finished(event)` — when a fast-path
  `TURN_FINISHED` carries a `proposal`, place it (explicit project → the
  thread's project → the surface's project → `NeedsProject`), open a task
  in INTAKE via the stores, journal `proposed_by_fastpath` with the brief,
  and return the one-line reply text (§8.1); a `cancel <id>` within
  `PROPOSAL_GRACE_S = 60` cancels it before any CLI session starts
  (transition INTAKE → CANCELLED).
- **Stage two** (§8.2): `resolve(role, task, project, ledger, providers)
  -> RoutingDecision` walking the seven-step ladder in order, each step
  named in `reason`: owner override (task.provider_override, or a
  `use <provider>` parsed from the brief/steer — strip it from the text),
  project table, global defaults (`~/.config/jarvis/routing.json`, env
  `JARVIS_ROUTING`, shipped default = the §8.2 table incl. models/effort
  per role per provider), capability filter (strict → codex only; vision
  → `Brief` images present and provider model known image-capable — read
  the OpenRouter catalog cache for Claude via `models.cached_info` if
  available, else assume capable; MCP servers required), availability
  filter (ledger state ∉ {available}; `health()` not ok → reason text),
  fallback chain, and **nothing survived → `RoutingBlocked` naming every
  candidate and why**, never the fast path. Then the two soft preferences
  (reviewer ≠ implementer's provider; one provider per task when one is
  over threshold) applied only among survivors. Every decision appended to
  `task.status.routing` and journaled.
- **Health cache**: `health()` results cached 60 s so a resolve never
  spawns a subprocess per role.

### `jarvis/v2/ledger.py` — `UsageLedger(stores_root)`

- Records per provider per task per day: `work_tokens` (Usage.work_tokens),
  `cost_usd` (Claude: recorded as `equivalent_usd`, never `spend_usd`;
  OpenRouter fast path: `spend_usd`; Codex: tokens only), from `USAGE`
  events. **Take deltas for Codex** (cumulative counters — WP7 already
  converts; consume what the daemon publishes and say which convention you
  rely on), increments for the fast path.
- Publishes the four states (§8.3) per provider: `available`,
  `over_threshold` (fraction of the *visible window* when a provider
  reports one — R6: none does yet, so until then the window is a local
  daily allowance per provider in `routing.json`, default Codex 15 M
  work tokens/day (the firm's), Claude 4 M/day, fast path $5/day; state
  the assumption in the file), `cooling` (a rate-limit/capacity `ERROR`
  event in the last 15 min — read Codex's cooldown metadata off its
  `ERROR` events), `unavailable` (health). Durable: `ledger/<day>.jsonl`
  under `V2_DATA_DIR`; a restart re-reads today.
- Per-task ceilings (`Task.ceilings`: `usd`, `hours`, `tokens`): the
  ledger exposes `ceiling_hit(task) -> str | None`; the runner (WP11) acts
  on it. **Mid-turn check** is the runner's job; you provide the query.

### §8.4 — pinned providers

`Thread.provider` never changes. `router.next_worker(task, role)` routes
fresh; `router.orchestrator_unavailable(task)` returns the blocked-message
text offering `resume <id> on <fallback>` and the durable state the new
orchestrator restarts from (spec, plan, status, worker reports) — the
actual restart is WP11's.

### `jarvis route` CLI (additive subcommand in `jarvis/__main__.py`)

`jarvis route` prints the effective table, ledger states, and the last ten
decisions with reasons; `jarvis route set <role> <chain>` (`claude,codex`)
globally or `--project <id>`; `jarvis route models <role> <provider>
<model>/<effort>`. Writes `routing.json` atomically. Also `GET /route` and
`POST /route` on the daemon (additive) returning the same.

## Tests: `tests/v2/router_check.py` and `tests/v2/ledger_check.py`

Free, temp roots, fake providers with scripted `health()`. Router: every
stage-one row incl. spoken never a verb, `use codex` stripped and applied,
placement chain incl. `NeedsProject`, proposal opened in INTAKE with the
reply text and cancelled inside the grace window / not after; the ladder
step by step with `reason` asserted, capability and availability drops,
chain walk, `RoutingBlocked` naming each drop, both soft preferences and
that they never override the ladder, decisions journaled and on
`status.routing`, health cached. Ledger: deltas vs increments, equivalent
vs spend kept apart, each of the four states and their transitions,
restart re-reads today, ceilings per kind, day rollover (America/Chicago
as the firm does — state it). CLI and routes happy/400 paths. Then run
`tests/v2/daemon_check.py`, `tests/v2/fastpath_check.py`.

## Rules

Stay inside `jarvis/v2/router.py`, `jarvis/v2/ledger.py`,
`jarvis/v2/daemon.py` (two additive routes), `jarvis/__main__.py` (one
additive subcommand), `jarvis/config.py` (one path constant), `tests/v2/`,
`docs/codex-briefs/WP9-notes.md`. Fixed interfaces and merged modules
otherwise untouched; stdlib only; no CLAUDE.md edits. When green, commit
with a message starting `v2 WP9: router and usage ledger` ending with
`Co-Authored-By: Codex <noreply@openai.com>`. Do not push. Finish with
`WP9-notes.md` (under 70 lines): what you built, the shipped
`routing.json` defaults, test commands and last lines, workarounds,
proposed interface changes.
